from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Value
from django.utils.functional import cached_property

if TYPE_CHECKING:
    _MixinBase = models.Field
else:
    _MixinBase = object


class Identity:
    ALWAYS = "ALWAYS"
    BY_DEFAULT = "BY DEFAULT"


class IdentityMixin(_MixinBase):
    # `generated` makes Django treat this like a `GeneratedField`, so the rest of that interface has to be answered too.
    generated = True
    # Required for the `alter_field` precheck.
    db_persist = True

    def __init__(self, identity=Identity.BY_DEFAULT, *args, **kwargs):
        self.identity = identity
        kwargs["blank"] = True
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        del kwargs["blank"]
        if self.identity != Identity.BY_DEFAULT:
            kwargs["identity"] = self.identity
        return name, path, args, kwargs

    @property
    def db_returning(self):
        return True

    @property
    def expression(self):
        # Depends on nothing. Django walks the expression of every generated
        # field when a sibling field is removed, so anything referencing other
        # fields here would block dropping them.
        return Value(None, output_field=self)

    @property
    def output_field(self):
        return self

    def generated_sql(self, connection):
        # Only reached by the `alter_field` precheck, which compares old and new
        # to reject in-place changes; the DDL itself comes from `identity_sql()`.
        return self.identity_sql()

    def identity_sql(self) -> tuple[str, tuple]:
        return f"GENERATED {self.identity} AS IDENTITY", ()

    @cached_property
    def referenced_fields(self):
        # Django intersects this with the fields an UPDATE touches to decide what
        # to RETURNING. An identity value never changes on UPDATE.
        return frozenset()


class IdentityField(IdentityMixin, models.IntegerField):
    pass


class BigIdentityField(IdentityMixin, models.BigIntegerField):
    pass


class PositiveIdentityField(IdentityMixin, models.PositiveIntegerField):
    pass


class PositiveBigIdentityField(IdentityMixin, models.PositiveBigIntegerField):
    pass
