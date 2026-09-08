from __future__ import annotations

from typing import ClassVar

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.operations.fields import RemoveField
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ModelState, ProjectState

from identityfield import Identity, IdentityField


def _detect(before: ModelState, after: ModelState):
    from_state, to_state = ProjectState(), ProjectState()
    from_state.add_model(before)
    to_state.add_model(after)
    autodetector = MigrationAutodetector(
        from_state,
        to_state,
        NonInteractiveMigrationQuestioner(specified_apps=set(), dry_run=True),
    )
    changes = autodetector._detect_changes()  # ty: ignore[unresolved-attribute]
    return [
        op for migration in changes.get("testapp", []) for op in migration.operations
    ]


def _model_state(*fields):
    return ModelState(
        "testapp",
        "Doomed",
        [("id", models.AutoField(primary_key=True)), *fields],
    )


def test_sibling_field_can_be_removed():
    """An identity field must not hold a sibling column hostage.

    Django walks the expression of every generated field on the model when a
    field is removed, to order the operations.
    """
    operations = _detect(
        _model_state(
            ("doomed", models.IntegerField(null=True)), ("sequence", IdentityField())
        ),
        _model_state(("sequence", IdentityField())),
    )

    assert len(operations) == 1
    assert isinstance(operations[0], RemoveField)
    assert operations[0].name == "doomed"


def test_identity_field_itself_can_be_removed():
    operations = _detect(
        _model_state(("sequence", IdentityField())),
        _model_state(),
    )

    assert len(operations) == 1
    assert isinstance(operations[0], RemoveField)
    assert operations[0].name == "sequence"


def test_removed_sibling_is_not_reported_as_referenced():
    removal = RemoveField("Doomed", "doomed")
    field = IdentityField()
    field.set_attributes_from_name("sequence")

    assert not RemoveField("Doomed", "sequence", field).references_field(
        "Doomed", "doomed", "testapp"
    )
    assert removal.references_field("Doomed", "doomed", "testapp")


class AlterModel(models.Model):
    class Meta:
        app_label = "testapp"

    sequence = IdentityField()


class ConstrainedModel(models.Model):
    class Meta:
        app_label = "testapp"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="amount_positive"
            )
        ]

    amount = models.IntegerField(default=0)
    sequence = IdentityField()


class DropColumnModel(models.Model):
    class Meta:
        app_label = "testapp"

    objects: ClassVar[models.Manager[DropColumnModel]] = models.Manager()

    doomed = models.IntegerField(null=True)
    sequence = IdentityField()


@pytest.fixture(scope="module")
def schema(django_db_blocker):
    """The tests below reach the database; the ones above are pure autodetector."""
    with django_db_blocker.unblock():
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(ConstrainedModel)
            schema_editor.create_model(DropColumnModel)

        yield


@pytest.mark.parametrize(
    "new_field",
    [
        pytest.param(IdentityField(identity=Identity.ALWAYS), id="identity-clause"),
        pytest.param(models.IntegerField(null=True), id="plain-integer"),
    ],
)
def test_altering_an_identity_field_is_rejected_clearly(new_field, schema):
    """Postgres cannot alter these in place, so Django must say so, not crash."""
    old_field = AlterModel._meta.get_field("sequence")
    assert isinstance(old_field, IdentityField)
    new_field.set_attributes_from_name("sequence")
    new_field.model = AlterModel

    with (
        connection.schema_editor(collect_sql=True) as schema_editor,
        pytest.raises(ValueError, match="must be removed and re-added"),
    ):
        schema_editor.alter_field(AlterModel, old_field, new_field)


def test_full_clean_resolves_generated_field_expressions(schema):
    """`Model._get_field_expression_map()` wraps every generated field's
    expression in its own `output_field`."""
    field_map = ConstrainedModel(amount=1)._get_field_expression_map(  # ty: ignore[unresolved-attribute]
        ConstrainedModel._meta
    )

    assert "sequence" in field_map

    with pytest.raises(ValidationError, match="amount_positive"):
        ConstrainedModel(amount=-1).full_clean()


def test_column_is_dropped_and_identity_keeps_counting(schema):
    """End to end: the column really goes away and the sequence survives it.

    Inserts after the drop go through raw SQL, because this model still carries
    the removed field in its `_meta` -- a real migration would rebuild it.
    """
    table = DropColumnModel._meta.db_table
    doomed = DropColumnModel._meta.get_field("doomed")
    assert isinstance(doomed, models.IntegerField)

    before = DropColumnModel.objects.create(doomed=1)

    with connection.schema_editor() as schema_editor:
        schema_editor.remove_field(DropColumnModel, doomed)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            [table],
        )
        assert {row[0] for row in cursor} == {"id", "sequence"}

        cursor.execute(f"INSERT INTO {table} DEFAULT VALUES RETURNING sequence")
        assert cursor.fetchone()[0] == before.sequence + 1
