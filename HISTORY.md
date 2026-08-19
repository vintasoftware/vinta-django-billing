# History

## Unreleased

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
