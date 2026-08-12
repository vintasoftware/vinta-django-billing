"""A project's own organization and membership models, swapped in.

The whole point of this app is to be *different* from the concrete models
``vinta-django-orgs`` ships: a different app label, a different table, and an
extra field neither of the stock models has. If any foreign key in ``billing``
still pointed at ``organizations.Organization``, the swap would take the stock
model's table out of existence underneath it and every one of those relations
would break — which is exactly what ``tests/test_swappable_models.py`` asserts
does *not* happen.
"""

from django.db import models
from organizations.models import AbstractOrganization, AbstractOrganizationMembership


class Tenant(AbstractOrganization):
    """The project's organization model, under a name of its own."""

    #: Not on ``AbstractOrganization``. Its presence on the model every billing
    #: foreign key resolves to is what proves the swap actually took.
    external_reference = models.CharField(max_length=64, blank=True)

    class Meta(AbstractOrganization.Meta):
        app_label = "swapped_orgs"
        db_table = "swapped_orgs_tenant"
        swappable = "ORGANIZATION_MODEL"


class TenantMembership(AbstractOrganizationMembership):
    class Meta(AbstractOrganizationMembership.Meta):
        app_label = "swapped_orgs"
        db_table = "swapped_orgs_tenantmembership"
        swappable = "ORGANIZATION_MEMBERSHIP_MODEL"
