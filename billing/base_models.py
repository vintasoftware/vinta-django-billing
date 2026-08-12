"""The abstract bases every table in this app is built on.

Ported rather than imported: the application this came from kept these in a
shared ``common`` app, which a library cannot depend on. Kept field-for-field
identical so a project migrating off that app sees no schema change.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField, AutoLastModifiedField
from organizations.mixins import SingleOrganizationModelMixin


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
    """Timestamps and a free-form ``meta`` blob. Not organization-scoped."""

    class Meta(IndexedTimeStampedModel.Meta, MetaJsonFieldModel.Meta):
        abstract = True


class OrganizationBaseModel(BaseModel, SingleOrganizationModelMixin):
    """:class:`BaseModel` plus the tenancy contract from ``vinta-django-orgs``.

    Inheriting the mixin rather than declaring a foreign key by hand is what
    makes these tables work in a project that swapped ``ORGANIZATION_MODEL``:
    the mixin resolves the target from the setting at class-definition time, and
    brings the scoped default manager and the ``(organization, pk)`` index with
    it.
    """

    class Meta(BaseModel.Meta):
        abstract = True
        # `SingleOrganizationModelMixin` sets this on its own Meta, which this
        # one replaces -- so it has to be restated or Django falls back to the
        # first-declared manager and every read goes out unscoped.
        default_manager_name = "objects"
        base_manager_name = "original_manager"
