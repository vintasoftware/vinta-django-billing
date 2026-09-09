# vinta-django-billing

Subscriptions, plan limits, entitlements, metered usage and dunning for Django
applications — billed to a user, an organization, a workspace, or all three at
once.

Every table here hangs off a **billing scope**: a swappable row that names
whoever is paying. The engine never learns what a payer is, so nothing stops one
project selling a personal plan alongside a team plan.

> **Status: alpha.** The API will change before 1.0.

## What it is

A billing engine that does not know what it is billing for.

It knows how to resolve a scope's ceiling for a resource, pool usage
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
| The billing-root protocol | Whether your scopes even have a hierarchy |

## Install

```bash
pip install vinta-django-billing            # or: uv add vinta-django-billing
pip install "vinta-django-billing[stripe]"  # provider SDKs are extras
```

Extras: `stripe`, `mercadopago`, `openapi`, and `orgs` — the last only if your
payers are [vinta-django-orgs](https://github.com/vintasoftware/vinta-django-orgs)
organizations and you want the membership-backed policy this package used to
default to. See [Who may manage billing](#who-may-manage-billing).

The distribution is typed (PEP 561): it ships a `py.typed` marker from 0.5.0 on,
so mypy reads the annotations instead of treating the package as `Any`. If you
listed `vinta_billing` under `ignore_missing_imports` to silence it, drop that
entry — while it is there the annotations are still discarded, and a
`from vinta_billing.models import *` re-export out of an `Any` module re-exports
nothing.

```python
INSTALLED_APPS = [
    ...,
    "django.contrib.contenttypes",
    "rest_framework",
    "vinta_billing.apps.BillingConfig",
]
```

That is the whole install. No tenancy library to add, and no middleware to
order.

## What a scope is

The one concept worth reading before anything else.

A **scope** is the thing being billed. Every `Subscription`, `BillingProfile`,
`PaymentMethod`, `MeteredOccurrence` and `BillingPeriodSummary` points at one.

The shipped `BillingScope` names its payer with a generic key, so it can name
anything you already have:

```python
from vinta_billing.models import BillingScope

personal, _ = BillingScope.objects.get_or_create_for(request.user)
team, _ = BillingScope.objects.get_or_create_for(organization)
```

Both rows live in one table, `scope_type` says which is which, and a
subscription against either behaves identically. Nothing else has to change to
start selling personal plans.

A scope also carries three columns the engine reads, all optional:

| Column | Read by | If you leave it empty |
| --- | --- | --- |
| `label` | The admin, and the per-scope usage breakdown in the API | Rows show as the scope key |
| `owner` | The shipped permission and recipient defaults | Nobody may manage that scope's billing, and dunning reaches nobody |
| `parent` | `ParentFieldHierarchy` | Every scope is its own billing root |

### Bringing your own scope model

Point `BILLING_SCOPE_MODEL` at an `AbstractBillingScope` subclass when you want
real foreign keys, typed access, and constraints per kind of payer:

```python
# settings.py -- top-level, NOT inside VINTA_BILLING. `Meta.swappable` names a
# setting, not a path into one, so this follows the AUTH_USER_MODEL pattern.
BILLING_SCOPE_MODEL = "myproject.BillingScope"
```

```python
class BillingScope(AbstractBillingScope):
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)

    @property
    def scope(self):
        return self.workspace

    def build_scope_key(self) -> str:
        return f"workspace:{self.workspace_id}"
```

Give its manager a `get_or_create_for(payer)` and a `scope_for(payer)` and the
shipped request resolver keeps working too.
[`tests/swapped_scopes/models.py`](tests/swapped_scopes/models.py) has two
worked examples, one of them billing users and companies from one table.

### Which scope is this request acting on?

`SCOPE_RESOLVER` answers it. The default takes whatever already set
`request.scope`, then falls back to the caller's own scope — which is what makes
a personal plan work with nothing configured.

It deliberately stops there rather than picking among scopes a caller merely
*owns*: choosing arbitrarily between tenants is how one customer ends up reading
another's billing. A project with real tenancy points the seam at its own answer:

```python
VINTA_BILLING = {"SCOPE_RESOLVER": "myproject.billing.resolve_scope"}


def resolve_scope(request):
    return BillingScope.objects.scope_for(request.workspace)
```

Already on `vinta-django-orgs`? `vinta_billing.contrib.orgs.resolve_scope_from_organization`
bridges its `request.organization` to the scope that names it.

## Register what you bill for

The closed `TextChoices` the engine was extracted with became two registries.
Register from your `AppConfig.ready()` — the only hook that runs after the app
registry is populated and before anything serves a request.

```python
# myproject/billing_setup.py
from django.utils.translation import gettext_lazy as _

from vinta_billing.constants import LimitKind, LimitRemedy
from vinta_billing.counting import count_by_scope, merge_breakdowns
from vinta_billing.registry import entitlements, resources
from vinta_billing.services.entitlement_service import count_metered_occurrences


def count_seats(context):
    """Memberships plus still-open invitations, per scope."""
    return merge_breakdowns(
        count_by_scope(Membership.objects.filter(scope_id__in=context.scope_ids)),
        count_by_scope(Invitation.objects.pending(context.scope_ids)),
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
`{scope_id: count}`. Scopes at zero must be **absent** from the
mapping rather than present with a zero — `GROUP BY` never emits a row for them,
and `count_by_scope` preserves that.

> **Read unscoped.** On a model using `SingleOrganizationModelMixin`, count
> through `Model.objects.unscoped()` or `Model.original_manager`, never the
> scoped default manager. Usage pools across a whole billing subtree, so a
> counter is asked about several scopes at once and must not be narrowed
> to whichever one is bound to the current context — `scope_id__in` is
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

result = get_entitlement_service().check_limit(scope, "seats", delta=1, lock=True)
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
2. **Usage pools at the billing root.** A child scope's usage counts
   against its root's ceiling, together with the rest of the subtree.
3. **Counting and checking are inseparable under concurrency.** See `lock`.

When the per-call data your counter needs is itself a query, pass
`usage_extra_resolver` rather than `usage_extra`:

```python
result = get_entitlement_service().check_limit(
    scope,
    "seats",
    usage_extra_resolver=lambda: {"exclude_invitation_id": find_the_invitation()},
)
```

It is called at most once, and only once the ceiling is known to be finite — so
a scope on an unlimited plan, which skips counting entirely, never pays
for the query. Pass one or the other, never both. `check_postpaid_allowance`'s
`delta_resolver` is the same idea for a delta that costs a query to work out.

## Configure the seams

Everything the engine cannot know, in one settings dict. Every key has a default
that works, so a flat single-tenant project configures nothing.

```python
VINTA_BILLING = {
    # Who pays for whom. Default: every scope is its own billing root.
    "HIERARCHY": "vinta_billing.hierarchy.ParentFieldHierarchy",
    # Which scope a request is acting on. Default: whatever already set
    # `request.scope`, then the caller's own scope.
    "SCOPE_RESOLVER": "myproject.billing.resolve_scope",
    # Who may see and change billing. Default: the scope's `owner`.
    "BILLING_MANAGER_PREDICATE": "myproject.billing.is_billing_owner",
    # Who hears about a failed charge or an approaching limit. Default: the
    # scope's `owner`.
    "BILLING_RECIPIENTS": "myproject.billing.owners_and_admins",
    # Where dunning and warning messages go. Default: log and drop.
    "NOTIFIER": "myproject.billing.Notifier",
    # What the meter bills. Default: the single registered postpaid resource.
    "OCCURRENCE_SOURCE": "myproject.billing.EventOccurrenceSource",
    "METERED_RESOURCE_KEY": "events",
    # How a sweep hands each per-subscription job over. Default: run it inline.
    # The jobs the sweep hands over build their services through
    # `SERVICE_CONTAINER`, same as the views.
    "JOB_DISPATCHER": "myproject.billing.enqueue",
    # How your DRF surface resolves the acting scope, and where the shipped
    # views build their services. Defaults: this package's own mixin and its
    # own container. See "Mounting the routes in a project that has its own
    # tenancy and its own DI" below.
    "VIEW_MIXIN": "myproject.api.TenantScopedViewMixin",
    "SERVICE_CONTAINER": "myproject.di.container",
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
conflicts as `409`, and deployment faults as `503` — an unconfigured provider
(`PaymentProviderNotConfiguredError`), a missing default plan
(`NoDefaultBillingPlanError`), a plan with no limit row for a registered
resource (`IncompleteBillingPlanError`). See
[`vinta_billing/exception_handling.py`](vinta_billing/exception_handling.py) for the table,
which is the contract: the shipped OpenAPI annotations are checked against it by
the suite, so what you generate a client from is what the handler returns.

The 503 family is the one worth a second's thought before you wire your own
frontend to it. Nothing the caller sent is wrong and the same request will fail
identically until an operator fixes the deployment, so a 4xx would be an
invitation to retry something that cannot work — and a 5xx is what your error
monitoring is already watching, which is the difference between noticing an
unconfigured provider in an hour and filing it under "clients sending bad
requests" for a week. Three annotations in 0.5.0 documented
`PaymentProviderNotConfiguredError` as a `409`; they were wrong about what the
handler did, and 0.6.0 corrected them rather than the handler.

### Who may manage billing

Both seams above default to the scope's **`owner`**, and nobody else. Least
privilege, and already the right answer for a personal plan —
`get_or_create_for(user)` sets the owner, so the person who pays can manage
their own billing with nothing configured.

For an organization scope this is deliberately strict. Nothing here can know
which member of an organization owns its billing, so the default returns `False`
rather than opening the endpoint to every member. Either populate `scope.owner`
with the billing contact, or configure the seams:

```python
VINTA_BILLING = {"BILLING_MANAGER_PREDICATE": "myproject.billing.is_billing_owner"}


def is_billing_owner(user, scope):
    return Membership.objects.filter(
        user=user, workspace_id=scope.object_id, is_billing_owner=True
    ).exists()
```

**Watch the recipient half.** A scope with no owner and no configured resolver
tells *nobody* about a failed charge, which turns the dunning ladder into a
suspension the payer was never warned about. The shipped `LoggingNotifier` at
least records what it would have sent.

#### Already on vinta-django-orgs?

Install the `orgs` extra and two settings restore the pre-0.8 behaviour exactly:

```python
VINTA_BILLING = {
    "BILLING_MANAGER_PREDICATE": "vinta_billing.contrib.orgs.any_member_may_manage_billing",
    "BILLING_RECIPIENTS": "vinta_billing.contrib.orgs.all_members",
}
```

`vinta_billing.contrib.orgs` also carries the stricter pair keyed on the
`vinta_billing.manage_billing` codename `Subscription` declares —
`member_holding_manage_billing` and `members_holding_manage_billing`. Nothing
grants that permission, so **select those only once a group carries it**. They
read the organization-scoped grant alone, never `user.has_perm`: billing is
routinely read against a reseller **root** that is an ancestor of the bound
organization, and `has_perm` would answer for the bound one, union in the user's
global permissions, and say yes to every superuser.

Your predicate answers the object-level question too. `IsBillingManager` asks it
about the request's scope for the coarse gate, and about the object for the
object-level one — about that object's `scope` for a billing row, and about the
object itself when it *is* a scope, which is what the write actions pass (the
resolved billing root). So a predicate reading "this caller may act on this
scope" is also what refuses a child scope's administrator on a reseller root's
plan.

### Scope hierarchies

Every scope carries a `parent`, so a reseller chain needs one setting and no
project code:

```python
VINTA_BILLING = {"HIERARCHY": "vinta_billing.hierarchy.ParentFieldHierarchy"}
```

`FlatHierarchy` stays the default. On a table where every `parent_id` is NULL a
parent walk reaches the same answer, but costs a descendant query per pooled
read.

Want a child to pay for its own subtree rather than pooling into a
grandparent's ceiling? Subclass and name a flag — or point the walk at your own
tree instead of the scope tree:

```python
class ResellerHierarchy(ParentFieldHierarchy):
    parent_field = "parent"
    root_flag_field = "is_reseller"  # a flagged child pays for itself
```

The walk is cycle-guarded: `parent` is user-mutable data, and returning an
arbitrary node from a cycle would leave every scope on it billing against a
different root depending on where the walk started.

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

### Mounting the routes in a project that has its own tenancy and its own DI

Two things a project usually owns are what stopped these routes from being
mounted as they are: how a request says which scope it is acting on, and
where services come from. Both are settings now, and both default to what this
package did before they existed — so a project that configures neither mounts
exactly the classes, and builds them from exactly the container, that it always
did.

```python
VINTA_BILLING = {
    # Mixed in *front* of every tenant-scoped viewset these routes mount, so
    # your resolution runs first and this package reads what it left on the
    # request. Default: "vinta_billing.view_mixins.TenantScopedViewMixin",
    # which those viewsets already inherit — so the default mixes in nothing.
    "VIEW_MIXIN": "myproject.api.TenantScopedViewMixin",
    # Where the shipped views and the admin build their services. Names a
    # module or an object; the service called `payment_service` is looked up as
    # `container.get_payment_service()` when that exists and
    # `container.payment_service()` otherwise — the second being what a
    # `dependency_injector` container offers, so point this straight at yours.
    # Default: "vinta_billing.services.container".
    "SERVICE_CONTAINER": "myproject.di.container",
}
```

`VIEW_MIXIN` reaches the tenant-scoped viewsets only. The plan catalogue answers
the same for every caller, and the two inbound provider webhooks are
authenticated by a provider signature rather than by a member of anything;
neither takes your scoping.

Your mixin may resolve the scope the way DRF mixins usually do — in
`perform_authentication`, *assigning* `request.scope` and returning
`None`, which is `vinta_orgs.drf.OrganizationScopedAPIViewMixin`'s shape. Both
mixins then spell `resolve_scope` and yours wins on name resolution, so
this package reads the request rather than taking that `None` at face value. No
adapter of your own is needed for it.

Passing a service to a viewset's constructor still wins over the container
(`entitlement_service=`, `payment_service=`, `subscription_service=`,
`dunning_service=`, `payment_provider_resolver=`), so a project that injects
them by hand today is unaffected by `SERVICE_CONTAINER`.

`SERVICE_CONTAINER` reaches the background jobs too, from 0.6.0 on — the four
sweeps in [`vinta_billing/jobs.py`](vinta_billing/jobs.py) build their services through
the same lookup the views use. Before that they imported this package's own
factories directly, so a project running its own container got its services on
the request path and a second, parallel set on the beat path. Each
per-subscription job still takes its service as a keyword argument, and passing
one still wins, exactly as it does for a viewset.

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
uv run tox                  # the full matrix: py3.11–3.14 x Django 5.2/6.0/6.1
uv run tox -e swapped       # the payer model swapped out
uv run tox -e scopeswapped  # BILLING_SCOPE_MODEL swapped out
uv run tox -e mixed         # users and companies billed from one table
uv run tox -e noorgs        # installed without vinta-django-orgs at all
uv run tox -e postgres      # against Postgres, for the row locks
uv run pre-commit install
```

The suite runs against four scope configurations, because the interesting
failures are invisible in three of them.

Under the default settings the swappable reference and the concrete model are
the same class, so a relation hardcoded to `vinta_billing.BillingScope` passes
everything and breaks only in a project that swapped the model —
`tox -e scopeswapped` is what catches it. `tox -e swapped` swaps the model a
scope *names* instead. `tox -e mixed` points `BILLING_SCOPE_MODEL` at a scope
model holding users and companies at once, which is the configuration the rest
of the suite cannot exercise: everywhere else bills exactly one kind of payer,
so "billing works" says nothing about whether two kinds stay out of each
other's ceilings.

`tox -e noorgs` installs the package with neither the `test` group nor the
`orgs` extra and runs [`tests/no_orgs_smoke.py`](tests/no_orgs_smoke.py) from a
process where `vinta_orgs` genuinely is not importable. It is the only place
that claim can be checked — every other environment installs the test group,
which still needs the package to exercise `vinta_billing.contrib.orgs`.

`tox -e postgres` runs it against a real database, for the same kind of reason.
The suite is on SQLite by default, and SQLite has no row locks: Django notices
and drops `SELECT ... FOR UPDATE` rather than raising, so cycle close's lock is
silently not taken there and a concurrency test would pass against no lock at
all. `tests/test_cycle_close_concurrency.py` skips itself unless the database
really takes the lock. Point the environment at a server first:

```bash
docker run --rm -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16-alpine
```

## License

MIT. See [LICENSE](LICENSE).
