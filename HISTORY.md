# History

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
- Pluggable billing-root hierarchy, notifier, and billing-manager predicate.
