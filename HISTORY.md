# History

## 0.4.0

Everything in this release came out of a host application migrating its own
billing engine onto this package, and finding that the parts it had to touch
first were the parts nothing here had ever exercised. The suite tested the
viewsets' methods and never their routes, so two of these had shipped through a
whole release unnoticed.

- **The shipped routes could not be mounted.** Every viewset declared its
  service as a keyword-only constructor argument with no default, and
  `as_view()` offers no way to supply one -- so the mounting this package's own
  README documents resolved a URL and then raised `TypeError` on the first
  request that reached it. Each of those arguments now defaults to `None` and
  falls back to `vinta_billing.services.container`, resolved inside `__init__`
  rather than at import time so the app registry and `VINTA_BILLING` are ready
  by the time a service is built. A project running its own DI container keeps
  passing every service by keyword and never reaches a factory; nothing about
  that call changes. `get_payment_provider_resolver()` is new in the container,
  which had a factory for every other service already. If you worked around this
  with a subclass supplying the defaults, you can drop it.
- **The provider webhooks no longer come out of the router.** Both of them carry
  the provider slug as a URL segment, and a DRF `@action` can spell that segment
  only one way: written as a regex it is emitted literally by a router built
  with `use_regex_path=False`, and written as a path converter it is emitted
  literally by the default regex router. Either way half of the projects
  mounting these got a route that can never match, and the only fix available to
  them was to change their router's mode -- a project-wide decision affecting
  their own URLs, made for them by a billing dependency. So the two webhooks are
  bound by `get_extra_patterns()` now, with `re_path`, which no router is
  involved in and which Django mixes with `path()` in one urlconf without
  complaint. The URLs and the reversible names are unchanged, character for
  character, so the callback URLs already published to Stripe and MercadoPago
  keep working. What did change is that `get_routes()` no longer lists
  `PaymentsViewSet`: **a project mounting `get_routes()` without also mounting
  `get_extra_patterns()` will lose its webhooks.** Mount both -- the README
  always showed both. `get_extra_patterns()` gained a `trailing_slash` argument
  for a project whose router was built with `trailing_slash=False`, since these
  patterns used to inherit that choice from the router and no longer can, and
  `billing_router()` now takes `use_regex_path` so the standalone router can be
  built either way too. `vinta_billing.routes`, which shipped a second,
  hand-maintained copy of the route table that had already drifted from
  `routing`'s, is now a thin alias onto it.
- **`check_limit` takes a `usage_extra_resolver`.** `check_postpaid_allowance`
  has had `delta_resolver` -- a lazy alternative, called at most once and only
  after the ceiling is known to be finite -- since the extraction; the prepaid
  side had only the eager `usage_extra`, and the difference matters here for the
  same reason it did there. Deciding *which* invitation to exclude from a seat
  count is itself a query, and on the unlimited path usage is not counted at
  all, so that query buys an answer nobody reads. Every organization is
  unlimited for the length of a rollout, which is exactly when the cost would be
  paid on every request. The new argument mirrors `delta_resolver`: mutually
  exclusive with `usage_extra` (passing both raises rather than silently
  discarding one), called at most once, never on the unlimited path, and merged
  into `UsageContext.extra` identically.
- **A resource can declare which `usage_extra` keys its counter reads.**
  `resources.register(...)` takes `usage_extra_keys`, and `check_limit` -- along
  with `get_current_usage` and `get_usage_breakdown` -- refuses a key outside
  what the resource declared, raising the new
  `vinta_billing.exceptions.InapplicableUsageExtraError`. This closes a failure
  that was otherwise silent and silently wrong: a per-call extra is read by
  exactly one counter, and every other counter takes the same `UsageContext` and
  ignores it, so aiming one at the wrong resource returns a count computed as
  though nothing had been passed, with nothing in the answer to say so. On a
  seat-accept path that means refusing a member the organization has room for.
  Logging it would leave the caller holding a wrong number it believes.

  Nothing starts raising on upgrade. `usage_extra_keys` defaults to `None`,
  meaning *undeclared*, and an undeclared resource is not checked at all --
  which is what every registration written before this release says. Opting in
  is per-resource and explicit. `None` and an empty declaration are deliberately
  different things: `frozenset()` means "this counter reads no per-call data",
  and that is the declaration which makes a key intended for some *other*
  resource visible when it is misrouted here. A project wanting the check
  everywhere declares it on every resource, the empty ones included. A global
  strictness setting was considered and rejected: it would only have helped
  projects that pass no `usage_extra` at all, for whom it does nothing, while
  any project that does pass one has to name the keys per resource regardless.

  The error inherits `BillingError` rather than `PaymentError`, so the
  `except ValueError` wrappers that surround service calls cannot flatten a
  call-site bug into a user-facing validation message. It has no HTTP status
  mapping: there is nothing a client did wrong and nothing it could do
  differently, so it renders as the 500 default.
- The test project now declares the three throttle scopes the shipped viewsets
  name (`payment-webhook`, `payment-provider`, `billing-write`), and the README
  says a project must too. DRF's `ScopedRateThrottle` raises
  `ImproperlyConfigured` for a scope with no rate, so a project mounting these
  routes without all three gets a 500 on the endpoint rather than a throttle. No
  rate ships here -- a rate limit is a deployment's decision -- but the
  requirement is written down now, and the suite would have found it a release
  ago had anything ever sent a request.

## 0.3.0

- Requires `vinta-django-orgs>=0.5,<0.6`, up from `>=0.2,<0.3`. Three of the
  intervening minors carried a breaking change, and the host applications this
  package is built for have already moved:
  - `vinta_orgs.state`'s module-level organization-context functions were deleted
    in 0.4 in favour of a class-specialized `OrganizationState`. Resolving the
    acting organization now goes through `vinta_billing.utils
    .get_organization_state()`, which binds the base class to the configured
    `ORGANIZATION_MODEL` -- the typed project-specific subclass is exactly what a
    library cannot declare.
  - Public signatures that annotated an organization as the concrete
    `vinta_orgs.Organization` now say `AbstractOrganization`, matching what 0.4
    did to the same signatures upstream. Under a swapped `ORGANIZATION_MODEL` the
    old annotation named a class the instances were never of.
  - `STRICT_ORGANIZATION_FILTER` defaults to `True` from 0.3, which changes how a
    usage counter fails when it reads a scoped model with nothing bound: it now
    raises instead of quietly counting zero. The README's counter guidance says
    so, and names `unscoped()` alongside `original_manager`.
  - The test settings run `OrganizationMiddleware` after
    `AuthenticationMiddleware`, which `vinta_orgs.W001` checks for: from 0.3 the
    middleware refuses an organization the caller holds no active membership in,
    and it needs `request.user` to do it. The README's install snippet shows the
    order.
- `Subscription` declares a `manage_billing` permission, and
  `vinta_billing.permissions.member_holding_manage_billing` /
  `vinta_billing.recipients.members_holding_manage_billing` answer "who may
  manage billing" and "who is told about it" from that one grant. Neither is a
  default and nothing here grants the permission: a project selects them through
  `BILLING_MANAGER_PREDICATE` / `BILLING_RECIPIENTS` once a group carries it. The
  permissive defaults (any member / every member) are unchanged.
- Code comments no longer point at the implementation plans of the application
  this was extracted from -- a reader of this package cannot open them.

## 0.2.0

- Renamed the app package and label from `billing` to `vinta_billing` so it no
  longer collides with a host project's own `billing` app. Update
  `INSTALLED_APPS` (`vinta_billing.apps.BillingConfig`) and every
  `from billing...` import to `from vinta_billing...`. The app label change moves
  default table names from `billing_*` to `vinta_billing_*`; there is no
  in-place data migration, so treat this as a fresh install.
- Requires `vinta-django-orgs>=0.2.0,<0.3`, which renamed its apps: every
  `organizations.*` import, the `INSTALLED_APPS` and middleware paths, and the
  app label in the initial migration's dependency are now `vinta_orgs.*`. The
  dependency is upper-bounded from here on — it is pre-1.0 and moving, and an
  uncapped floor let the rename reach CI unannounced.

## 0.1.0 (unreleased)

Initial extraction from the `payments` app of a multi-organization scheduling
application, generalised to run against any project built on
[vinta-django-orgs](https://github.com/vintasoftware/vinta-django-orgs).

- Subscriptions, plans, plan limits, entitlements, add-ons and billing profiles.
- Prepaid limit enforcement with usage pooled across a billing subtree, and
  `SELECT ... FOR UPDATE` on the billing root for concurrent creates.
- Postpaid metering with an idempotent occurrence ledger, overage pricing and
  period reconciliation.
- Dunning ladder, grace and restricted states, and usage warnings.
- Stripe and MercadoPago payment and subscription adapters.
- Open registries for limited resources and entitlements, replacing the closed
  enums the code was extracted with.
- Pluggable billing-root hierarchy, notifier, billing-manager predicate and job
  dispatcher. No Celery dependency and no notification-transport dependency: how
  a sweep is scheduled and how a message is delivered are the project's.
- Every organization relation resolves through `ORGANIZATION_MODEL`, exercised
  by a dedicated test environment (`tox -e swapped`) that runs the suite against
  a project-defined organization model.
- Provider credentials and the default provider live in `VINTA_BILLING`, and the
  adapter registries hand back adapters built from them.
- `vinta_billing.exception_handling.billing_exception_handler` renders the typed
  errors as HTTP responses.
