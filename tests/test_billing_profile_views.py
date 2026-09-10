"""``BillingProfileViewSet``'s four actions, driven as real requests.

The suite had no functional coverage of these endpoints at all -- ``tests/
test_routing.py`` asserts the four routes reverse, and nothing sent a request
through them. That is how ``get_billing_profile`` came to look a profile up by
``pk=scope.pk``: correct while ``BillingProfile.organization`` was
``primary_key=True``, silently wrong once the model gained a surrogate key, and
invisible to a suite that never called it.

The regression that matters is
:func:`test_retrieve_finds_the_profile_when_its_pk_differs_from_the_scope_pk`.
Every other test here would pass against the broken lookup on a fresh database,
because both sequences start at 1 and the two ids collide by luck. That test
forces them apart first, which is the *normal* state of an installation
upgraded from 0.7 -- profiles keep their original organization-derived primary
keys while scopes are created fresh and numbered independently.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from tests.conftest import make_scope
from tests.payers import make_payer
from vinta_billing.conf import get_scope_model
from vinta_billing.constants import DocumentTypes
from vinta_billing.models import BillingAddress, BillingProfile


pytestmark = pytest.mark.django_db


def resolve_scope_by_owner(request):
    """``SCOPE_RESOLVER`` for this module: the scope the caller owns.

    Same stand-in ``tests/test_viewset_permissions.py`` uses -- it keeps the
    request-level plumbing out of the way so these tests measure the lookup.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return get_scope_model()._default_manager.filter(owner=user).first()


@pytest.fixture(autouse=True)
def _resolve_by_owner():
    with override_settings(
        VINTA_BILLING={"SCOPE_RESOLVER": "tests.test_billing_profile_views.resolve_scope_by_owner"}
    ):
        yield


@pytest.fixture
def client(scope):
    api_client = APIClient()
    api_client.force_login(scope.owner)
    return api_client


def _make_address():
    return BillingAddress.objects.create(
        street_name="Main Street",
        street_number="1",
        city="Springfield",
        state="SP",
        country="BR",
        zip_code="00000-000",
    )


def _make_profile(scope, **kwargs):
    defaults = {
        "contact_first_name": "Ada",
        "contact_last_name": "Lovelace",
        "contact_email": "billing@example.com",
        "document_type": DocumentTypes.OTHER,
        "document_number": "1",
        "billing_address": _make_address(),
    }
    return BillingProfile.objects.create(scope=scope, **{**defaults, **kwargs})


def test_retrieve_returns_the_active_scopes_profile(client, scope):
    _make_profile(scope)

    response = client.get(reverse("billing:BillingProfile-retrieve"))

    assert response.status_code == status.HTTP_200_OK
    # `id` is the *scope* id on the wire, not the profile's own -- the
    # serializer sources it from `scope_id` deliberately, so the payload keeps
    # the shape it had when a profile's pk was its payer's. Only the lookup
    # behind it changed.
    assert response.data["id"] == scope.pk
    assert response.data["contact_email"] == "billing@example.com"


def test_retrieve_finds_the_profile_when_its_pk_differs_from_the_scope_pk(db):
    """The regression. A profile is reachable by *its scope*, not by its id.

    Built without the shared ``scope`` fixture on purpose: the two id sequences
    have to be pulled apart *before* the caller's scope exists, and by the time
    a test body runs that fixture has already taken id 1. Burning a few scope
    rows first reproduces the state every installation upgraded from 0.7 is
    already in -- profiles keep their original organization-derived primary
    keys while scopes are created fresh and numbered independently.

    Against the ``pk=scope.pk`` lookup this 404s.
    """
    for name in ("Filler One", "Filler Two", "Filler Three"):
        make_scope(make_payer(name))

    owner = get_user_model().objects.create_user(username="grace", password="pw")
    scope = make_scope(make_payer("Late Tenant"))
    scope.owner = owner
    scope.save(update_fields=["owner", "modified"])
    profile = _make_profile(scope)

    assert profile.pk != scope.pk, "the two id spaces were not separated"

    api_client = APIClient()
    api_client.force_login(owner)
    response = api_client.get(reverse("billing:BillingProfile-retrieve"))

    assert response.status_code == status.HTTP_200_OK
    assert response.data["id"] == scope.pk
    assert response.data["contact_email"] == profile.contact_email


def test_retrieve_404s_when_the_scope_has_no_profile(client):
    response = client.get(reverse("billing:BillingProfile-retrieve"))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_retrieve_never_serves_another_scopes_profile(client, other_scope):
    """The caller's own scope has no profile; another tenant's does.

    Pins that the fix did not widen the lookup: dropping the ``pk`` filter must
    leave the ``scope`` filter doing the isolating.
    """
    _make_profile(other_scope)

    response = client.get(reverse("billing:BillingProfile-retrieve"))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_update_writes_to_the_active_scopes_profile(client, scope):
    profile = _make_profile(scope)

    response = client.patch(
        reverse("billing:BillingProfile-partial_update"),
        {"contact_first_name": "Grace"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    profile.refresh_from_db()
    assert profile.contact_first_name == "Grace"


def test_update_leaves_another_scopes_profile_alone(client, scope, other_scope):
    """Two profiles exist; the write must land on the caller's."""
    mine = _make_profile(scope)
    theirs = _make_profile(other_scope, contact_first_name="Untouched")

    response = client.patch(
        reverse("billing:BillingProfile-partial_update"),
        {"contact_first_name": "Grace"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.contact_first_name == "Grace"
    assert theirs.contact_first_name == "Untouched"
