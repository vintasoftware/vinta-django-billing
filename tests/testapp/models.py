"""Models a host application might bill for.

Deliberately trivial and deliberately *not* billing concepts: the point of the
suite is that the engine counts things it has never heard of. A ``Widget`` is a
prepaid resource, a ``Seat`` is the two-table kind (rows plus pending
invitations), and neither is mentioned anywhere in ``billing``.
"""

from django.conf import settings
from django.db import models
from vinta_orgs.mixins import SingleOrganizationModelMixin


class Widget(SingleOrganizationModelMixin):
    name = models.CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Seat(SingleOrganizationModelMixin):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="testapp_seats", on_delete=models.CASCADE
    )
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return "%s @ %s" % (self.user, self.organization)


class SeatInvitation(SingleOrganizationModelMixin):
    """A seat held open for somebody who has not accepted yet.

    Exists so the suite covers the two-table counter shape: a pending invitation
    occupies a seat, and must be counted alongside the real ones or an
    organization can invite past its ceiling and blow through it on accept.
    """

    email = models.EmailField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.email


class Company(models.Model):
    """An organization-like model with a parent chain.

    Not an organization -- ``vinta-django-orgs``' model has no parent, which is
    the whole reason :class:`vinta_billing.hierarchy.ParentFieldHierarchy` exists.
    This stands in for the organization model of a project that *does* nest, so
    the subtree walk is exercised against real queries rather than fakes.
    """

    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, related_name="children", on_delete=models.CASCADE
    )
    #: Marks a child that pays for its own subtree -- a reseller under a reseller.
    is_own_root = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.name
