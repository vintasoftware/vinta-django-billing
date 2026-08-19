# vinta-django-billing

Subscriptions, plan limits, entitlements, metered usage and dunning for
multi-organization Django applications.

Built on [vinta-django-orgs](https://github.com/vintasoftware/vinta-django-orgs):
every table here is scoped to that library's swappable organization model, and
the engine reads nothing from it beyond the swappable model reference and the
tenancy mixin.

> **Status: alpha.** The API will change before 1.0.

## What it is

A billing engine that does not know what it is billing for.

It knows how to resolve an organization's ceiling for a resource, pool usage
across a reseller subtree, refuse a create that would exceed a limit, meter
post-paid usage, run a dunning ladder over a failed charge, and close a billing
period. It does not know what a "seat" is, or a "calendar", or an "API token" —
those are the host application's, and they come in through registries.

| Ships here | Stays in your project |
| --- | --- |
| `Subscription`, `BillingPlan`, `PlanLimit`, `PlanEntitlement`, `BillingProfile`, `Payment`, `Refund`, `MeteredOccurrence`, … | The models being limited |
| The limit / entitlement / pooling engine | Which resources exist, and how to count them |
| Stripe and MercadoPago adapters | Provider credentials |
| Dunning ladder and usage warnings | The notification transport |
| The billing-root protocol | Whether your organizations even have a hierarchy |

## Install

```bash
pip install vinta-django-billing            # or: uv add vinta-django-billing
pip install "vinta-django-billing[stripe]"  # provider SDKs are extras
```

Extras: `stripe`, `mercadopago`, `openapi`.

The distribution is typed (PEP 561): it ships a `py.typed` marker from 0.5.0 on,
so mypy reads the annotations instead of treating the package as `Any`. If you
listed `vinta_billing` under `ignore_missing_imports` to silence it, drop that
entry — while it is there the annotations are still discarded, and a
`from vinta_billing.models import *` re-export out of an `Any` module re-exports
nothing.

```python
INSTALLED_APPS = [
    ...,
    "rest_framework",
    "vinta_orgs.apps.OrganizationsConfig",  # vinta-django-orgs
    "vinta_billing.apps.BillingConfig",
]

MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After `AuthenticationMiddleware`. `vinta-django-orgs` refuses an
    # organization the caller holds no active membership in, and it needs
    # `request.user` to do that; placed earlier the check silently does nothing.
    # Its `vinta_orgs.W001` system check reports the wrong order.
    "vinta_orgs.middleware.OrganizationMiddleware",
]
```

## Register what you bill for

The closed `TextChoices` the engine was extracted with became two registries.
Register from your `AppConfig.ready()` — the only hook that runs after the app
registry is populated and before anything serves a request.

```python
# myproject/billing_setup.py
from django.utils.translation import gettext_lazy as _

from vinta_billing.constants import LimitKind, LimitRemedy
from vinta_billing.counting import count_by_organization, merge_breakdowns
from vinta_billing.registry import entitlements, resources
from vinta_billing.services.entitlement_service import count_metered_occurrences


def count_seats(context):
    """Memberships plus still-open invitations, per organization."""
    return merge_breakdowns(
        count_by_organization(
            Membership.objects.filter(organization_id__in=context.organization_ids)
        ),
        count_by_organization(Invitation.objects.pending(context.organization_ids)),
    )


resources.register(
    "seats",
    label=_("Seats"),
    kind=LimitKind.PREPAID,
    counter=count_seats,
    remedy=LimitRemedy.UPGRADE_PLAN,
    # The `usage_extra` keys `count_seats` reads. Optional; see below.
    usage_extra_keys={"exclude_invitation_id"},
)
resources.register(
    "events",
    label=_("Events"),
    kind=LimitKind.POSTPAID,
    counter=count_metered_occurrences,
    # Reads nothing per call, and says so.
    usage_extra_keys=frozenset(),
)

entitlements.register("white_label", label=_("White-label branding"))
```

A counter takes a [`UsageContext`](vinta_billing/counting.py) and returns
`{organization_id: count}`. Organizations at zero must be **absent** from the
mapping rather than present with a zero — `GROUP BY` never emits a row for them,
and `count_by_organization` preserves that.

> **Read unscoped.** On a model using `SingleOrganizationModelMixin`, count
> through `Model.objects.unscoped()` or `Model.original_manager`, never the
> scoped default manager. Usage pools across a whole billing subtree, so a
> counter is asked about several organizations at once and must not be narrowed
> to whichever one is bound to the current context — `organization_id__in` is
> the tenant boundary here, and it is the counter's own filter.
>
> In a background sweep nothing is bound at all, and a scoped read then depends
> on `vinta-django-orgs`' `STRICT_ORGANIZATION_FILTER`: `True` (its default since
> 0.3) raises `OrganizationNotFoundError` and the sweep dies; `False` reports
> zero for everybody and every ceiling silently reads as empty. Neither is a
> counter you want.

Registering a resource never asks for a migration: the fields storing resource
keys take their `choices` by callable reference, so the migration state does not
change when the registry does.

### Per-call data, and declaring it

A counter that needs something the call site knows — "the invitation currently
being accepted, which must not be double-counted" — reads it out of
`UsageContext.extra`, which the caller fills through `usage_extra`. The engine
never reads the values.

It does check the keys, but only if you asked it to. `usage_extra_keys` names
what a counter reads; omit it and nothing is checked, which is how every
registration behaved before 0.4.0. Declare it — on **every** resource, including
the ones that read nothing, as `frozenset()` — and a key aimed at the wrong
resource raises `InapplicableUsageExtraError` instead of being ignored. That is
worth doing because the mistake is otherwise invisible: a counter that does not
read a key ignores it, so the caller gets a count computed as though they had
passed nothing and no part of the answer says so.

## Enforce a limit

```python
from vinta_billing.services.container import get_entitlement_service

result = get_entitlement_service().check_limit(organization, "seats", delta=1, lock=True)
if not result.allowed:
    raise OverLimitError.build(result)
```

`lock=True` takes `SELECT ... FOR UPDATE` on the billing root's subscription row
before counting, so two racing creates for the last unit of capacity serialize
and exactly one sees room. It requires an open transaction.

Three rules the engine holds to, and which are easy to break by accident:

1. **NULL is unlimited, never zero.** A missing limit row means the same. Both
   fail open — a data gap must never lock a customer out of something they could
   do yesterday.
2. **Usage pools at the billing root.** A child organization's usage counts
   against its root's ceiling, together with the rest of the subtree.
3. **Counting and checking are inseparable under concurrency.** See `lock`.

When the per-call data your counter needs is itself a query, pass
`usage_extra_resolver` rather than `usage_extra`:

```python
result = get_entitlement_service().check_limit(
    organization,
    "seats",
    usage_extra_resolver=lambda: {"exclude_invitation_id": find_the_invitation()},
)
```

It is called at most once, and only once the ceiling is known to be finite — so
an organization on an unlimited plan, which skips counting entirely, never pays
for the query. Pass one or the other, never both. `check_postpaid_allowance`'s
`delta_resolver` is the same idea for a delta that costs a query to work out.

## Configure the seams

Everything the engine cannot know, in one settings dict. Every key has a default
that works, so a flat single-tenant project configures nothing.

```python
VINTA_BILLING = {
    # Who pays for whom. Default: every organization is its own billing root.
    "HIERARCHY": "myproject.billing.ResellerHierarchy",
    # Who may see and change billing. Default: any member of the organization.
    "BILLING_MANAGER_PREDICATE": "myproject.billing.is_billing_owner",
    # Who hears about a failed charge or an approaching limit. Default: every
    # member of the organization.
    "BILLING_RECIPIENTS": "myproject.billing.owners_and_admins",
    # Where dunning and warning messages go. Default: log and drop.
    "NOTIFIER": "myproject.billing.Notifier",
    # What the meter bills. Default: the single registered postpaid resource.
    "OCCURRENCE_SOURCE": "myproject.billing.EventOccurrenceSource",
    "METERED_RESOURCE_KEY": "events",
    # How a sweep hands each per-subscription job over. Default: run it inline.
    "JOB_DISPATCHER": "myproject.billing.enqueue",
    # Per-provider credentials. A provider absent here stays registered -- its
    # inbound webhook route keeps resolving -- but every outbound call site
    # refuses it rather than authenticating with an empty credential.
    "PROVIDERS": {
        "stripe": {
            "API_KEY": env("STRIPE_SECRET_KEY"),
            "WEBHOOK_SECRET": env("STRIPE_WEBHOOK_SECRET"),
            "PUBLISHABLE_KEY": env("STRIPE_PUBLISHABLE_KEY"),
        },
    },
    "DEFAULT_PROVIDER": "stripe",
    # Absolute base the provider callback URLs are built against.
    "SITE_DOMAIN": "api.example.com",
}
```

The full list of keys, each with the default it falls back to, is in
[`vinta_billing/conf.py`](vinta_billing/conf.py). An unknown key raises rather than being
ignored, so a typo cannot silently leave you on a default.

### Rendering errors

The services raise typed errors carrying a machine-readable `code`. Point DRF at
the shipped handler to render them, or call it from your own:

```python
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "vinta_billing.exception_handling.billing_exception_handler",
}
```

Over-limit and declined-charge errors render as `402`, subscription-state
conflicts as `409`, and a provider this deployment holds no credential for as
`503` — see [`vinta_billing/exception_handling.py`](vinta_billing/exception_handling.py) for
the table.

### Who may manage billing

Both seams above default to *every member*: the most permissive answer that is
still tenant-safe, so the shipped endpoints work before anything is wired.

If your project expresses roles as `vinta-django-orgs` organization permissions,
`Subscription` declares a codename to grant — `vinta_billing.manage_billing` —
and this package ships both halves of the question already written against it:

```python
VINTA_BILLING = {
    "BILLING_MANAGER_PREDICATE": "vinta_billing.permissions.member_holding_manage_billing",
    "BILLING_RECIPIENTS": "vinta_billing.recipients.members_holding_manage_billing",
}
```

Nothing here grants the permission, so **select these only once a group carries
it**. Until then the predicate 403s every billing endpoint and, worse, the
recipient resolver returns nobody — which turns the dunning ladder into a
suspension the payer was never warned about.

Both read the organization-scoped grant alone (`vinta_orgs.authorization`), never
`user.has_perm`: billing is routinely read against a reseller **root** that is an
ancestor of the bound organization, and `has_perm` would answer for the bound
one, union in the user's global permissions, and say yes to every superuser.

### Organization hierarchies

`vinta-django-orgs`' organization model has a name and a slug and nothing else,
so the library cannot assume a parent field exists. The default
`FlatHierarchy` treats every organization as its own billing root. A project
whose organizations nest subclasses the shipped parent-chain walk:

```python
from vinta_billing.hierarchy import ParentFieldHierarchy


class ResellerHierarchy(ParentFieldHierarchy):
    parent_field = "parent"
    root_flag_field = "can_invite_organizations"  # a flagged child pays for itself
```

The walk is cycle-guarded: `parent` is user-mutable data, and returning an
arbitrary node from a cycle would leave every organization on it billing against
a different root depending on where the walk started.

### Audit

Transitions are published as Django signals rather than written to an audit log
this package would have to invent. See [`vinta_billing/signals.py`](vinta_billing/signals.py).
They are sent inside the caller's transaction, so a receiver that raises rolls
the transition back with it.

## REST API

The shipped viewsets are offered as routes rather than a ready-made `urls.py`,
so you mount them where you want:

```python
from vinta_billing.routing import billing_router, get_extra_patterns

urlpatterns = [
    path("api/", include(billing_router().urls)),
    *get_extra_patterns(),
]
```

Mount **both halves**. The endpoints a router cannot express — the singleton
payment-provider reads, and the two inbound provider webhooks, which carry the
provider slug in the URL — come from `get_extra_patterns()`, not from the
router. Drop it and you have no webhooks, so no provider callback ever arrives.

Already have a router? `register_routes(your_router)` puts the shipped viewsets
on it. Either way the router's own mode is respected: nothing here assumes the
regex form, so a `DefaultRouter(use_regex_path=False)` works as well as the
default, and `billing_router(use_regex_path=False)` builds one. If your router
was built with `trailing_slash=False`, pass the same to
`get_extra_patterns(trailing_slash=False)` — those patterns do not come out of
the router and cannot read the choice off it.

Every service the viewsets use defaults through
`vinta_billing.services.container`. Pass your own to the constructor
(`entitlement_service=`, `payment_service=`, `subscription_service=`,
`dunning_service=`, `payment_provider_resolver=`) if you run your own DI
container; leave them out and the container supplies them.

The shipped viewsets throttle their write and unauthenticated endpoints through
three `ScopedRateThrottle` scopes, and DRF raises `ImproperlyConfigured` for a
scope with no configured rate — so all three need one, or those endpoints answer
500 instead of throttling. The numbers are yours to pick:

```python
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_RATES": {
        # Inbound provider callbacks.
        "payment-webhook": "120/min",
        # The unauthenticated provider read.
        "payment-provider": "60/min",
        # Plan changes, payment retries, add-on purchases.
        "billing-write": "30/min",
    },
}
```

## Development

```bash
uv sync --all-extras
uv run pytest
uv run tox              # the full matrix: py3.11–3.14 x Django 5.2/6.0/6.1
uv run tox -e swapped   # the suite against a swapped ORGANIZATION_MODEL
uv run pre-commit install
```

`tox -e swapped` runs everything again with `ORGANIZATION_MODEL` pointed at a
project-defined model instead of the one `vinta-django-orgs` ships. Under the
default settings those are the same class, so a foreign key hardcoded to
`vinta_orgs.Organization` passes the whole suite and only breaks in a project
that actually swapped the model — this is what catches it.

## License

MIT. See [LICENSE](LICENSE).
