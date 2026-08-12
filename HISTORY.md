# History

## Unreleased

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
- `billing.exception_handling.billing_exception_handler` renders the typed
  errors as HTTP responses.
