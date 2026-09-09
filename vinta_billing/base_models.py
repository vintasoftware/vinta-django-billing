"""The abstract bases every table in this app is built on.

Ported rather than imported: the application this came from kept these in a
shared ``common`` app, which a library cannot depend on. Kept field-for-field
identical so a project migrating off that app sees no schema change.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField, AutoLastModifiedField


class IndexedTimeStampedModel(models.Model):
    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    class Meta:
        abstract = True


class MetaJsonFieldModel(models.Model):
    meta = models.JSONField(_("meta"), default=dict, blank=True)

    class Meta:
        abstract = True


class BaseModel(IndexedTimeStampedModel, MetaJsonFieldModel):
    """Timestamps and a free-form ``meta`` blob. Not scoped."""

    class Meta(IndexedTimeStampedModel.Meta, MetaJsonFieldModel.Meta):
        abstract = True
