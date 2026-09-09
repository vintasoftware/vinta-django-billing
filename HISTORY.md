# History

## 0.8.0

- **Billing hangs off a swappable scope instead of an organization, and
  `vinta-django-orgs` is no longer a dependency.** This package already let you
  swap the organization model, so the problem was never a hardcoded target. It
  was that `Meta.swappable` resolves exactly **one** model per project: you
  could sell a plan to an organization *or* to a user, never both, and a solo
  customer had to be forced into a one-member organization. Every table now
  points at a `BillingScope` — a row that names whoever is paying — and
  `scope_type` says what kind of payer that is. The shipped model uses a
  generic key, so `BillingScope.objects.get_or_create_for(request.user)` and
  `get_or_create_for(organization)` both work with no project code, in one
  project, at the same time. A project wanting real foreign keys and per-kind
  constraints points `BILLING_SCOPE_MODEL` at its own `AbstractBillingScope`
  subclass instead. **What an adopter must do:** quite a lot — see the upgrade
  below. **What changes in the document:** `?organization=` becomes `?scope=`,
  `by_organization[].organization_id` becomes `by_scope[].scope_id`, and
  `billing_root_organization_id` becomes `billing_root_scope_id`.

### Upgrading from 0.7

Set `VINTA_BILLING["LEGACY_SCOPE_MODEL"]` to whatever your `ORGANIZATION_MODEL`
named, then migrate:

```python
VINTA_BILLING = {"LEGACY_SCOPE_MODEL": "vinta_orgs.Organization"}
```

Migration 0005 creates one scope per organization and re-points every billing
row at it. With rows to migrate and that setting unset it refuses rather than
guessing at a content type, because a scope naming the wrong one is both silent
and very hard to unpick afterwards. 0004 through 0006 are additive, then
backfill, then drop, and the sequence reverses cleanly — an operator who needs
to stop partway can.

**Keep `ORGANIZATION_MODEL` defined** until you prune the replaced migrations.
Django loads every migration file to build the graph whether or not it will run
one, and 0001 reads that setting at import time. It falls back to
`AUTH_USER_MODEL` when absent, so a *fresh* install needs neither the setting
nor the package.

Renamed settings and API:

| Was | Becomes |
| --- | --- |
| `ORGANIZATION_MODEL` (for billing) | `BILLING_SCOPE_MODEL`, top-level, optional |
| — | `VINTA_BILLING["SCOPE_RESOLVER"]` |
| — | `VINTA_BILLING["LEGACY_SCOPE_MODEL"]` (upgrade only) |
| `?organization=` | `?scope=` |
| `by_organization[].organization_id` | `by_scope[].scope_id` |
| `billing_root_organization_id` | `billing_root_scope_id` |
| `request.organization` | `request.scope` |
| `resolve_organization` | `resolve_scope` |
| `organization_required` | `scope_required` |
| `pooled_organization_ids` | `pooled_scope_ids` |
| `for_organizations` | `for_scopes` |
| `is_default_for_new_organizations` | `is_default_for_new_scopes` |

There is no `organization` alias. Two vocabularies for one concept, in a package
where "organization" would then mean "scope, but only sometimes", is worse than
one honest break before 1.0.

- **The permission and recipient defaults read `scope.owner`.** They used to be
  "any member of the organization", which a scope has no way to answer — it may
  name a user, or a workspace, or something with no members at all. The new
  default is least privilege and already right for a personal plan, where
  `get_or_create_for(user)` has set the owner. **What an adopter must do:** if
  your payers are `vinta-django-orgs` organizations, install the `orgs` extra
  and point both settings at `vinta_billing.contrib.orgs`, which carries the
  four membership-backed functions unchanged. Otherwise populate `scope.owner`
  with the billing contact, or configure the two seams. Do check one of those
  happened: a scope with no owner and no configured resolver tells **nobody**
  about a failed charge, which turns the dunning ladder into a suspension the
  payer was never warned about.

- **`SCOPE_RESOLVER` replaces the reads off `vinta-django-orgs`' middleware and
  context state.** The default takes whatever already set `request.scope`, then
  falls back to the caller's own scope — which is what makes a personal plan
  work unconfigured. It stops there rather than picking among scopes a caller
  merely *owns*: choosing arbitrarily between tenants is how one customer reads
  another's billing. **What an adopter must do:** a project whose tenancy is
  resolved some other way configures the seam;
  `vinta_billing.contrib.orgs.resolve_scope_from_organization` bridges
  `request.organization` for projects staying on that library.

- **`ParentFieldHierarchy` needs no project code now.** `AbstractBillingScope`
  ships a self-referential `parent`, so a reseller chain is one setting rather
  than a field on a model this package does not own. `FlatHierarchy` stays the
  default: over an all-NULL `parent` column a walk reaches the same answer and
  costs a descendant query per pooled read. **What an adopter must do:** nothing,
  unless you wrote a hierarchy against your organization model's parent field, in
  which case it keeps working — `parent_field` is still configurable.

- **`BillingProfile` has a surrogate primary key.** It used to *be* its payer
  (`organization` was `primary_key=True`), which meant re-pointing a profile at
  a different payer would rewrite its pk and every `Payment` row hanging off it.
  The upgrade carries the existing values across untouched, so no payment is
  re-pointed and nothing outside this package's own tables notices. **What an
  adopter must do:** nothing, unless you query `BillingProfile` by organization
  id, which is now `scope_id` and no longer the pk.

- **`OrganizationBaseModel` is gone.** Nothing in this package ever subclassed
  it. **What an adopter must do:** nothing, unless you imported it, in which case
  declare the foreign key yourself.

- **Deliberately not adopted: an identity model.**
  `vinta-django-audit-logs` swaps one alongside its scope, and it does not fit
  here — no billing model records who acted, so it would be a swappable model
  nothing points at. If an actor trail arrives on subscription and payment
  transitions, the right move is probably to depend on that package rather than
  grow a second identity model.

## 0.7.0

- **The two inbound webhooks declare their path parameters, and an adopter's
  `--deploy` check stops failing on them.** `get_extra_patterns` binds both
  routes with `re_path` and a bare `(?P<name>[^/.]+)` group, deliberately: an
  `@action` can spell a parameterised segment either as a regex or as a path
  converter, and whichever one it picks is emitted literally by half the routers
  mounting it. What that left behind was a segment drf-spectacular had no type
  to infer -- and `PaymentsViewSet` is a plain `ViewSet`, so there was no
  queryset to reach a model through either. It warned twice per generation and
  defaulted both parameters to `string`. That is not cosmetic for an adopter:
  drf-spectacular re-emits those warnings as `drf_spectacular.W001` from its
  `--deploy` system check, so a project whose build runs
  `manage.py check --deploy --fail-level WARNING` -- the shape both Render's and
  Heroku's Django buildpacks ship -- failed its deploy on two warnings it did
  not cause and could not fix without annotating this package's class from the
  outside. Both parameters are now declared on the `@extend_schema` annotations
  the two actions already carried. **What an adopter must do:** nothing, unless
  you wrote that annotation yourself against 0.6.0, in which case it is now
  redundant and can go. **What changes in the document:** `id` is `integer`
  rather than `string`. That is what it always was -- it is a `Payment` id, and
  `Payment` inherits `BaseModel` under this app's `BigAutoField` default -- so
  the inference would have reached the same answer had a queryset been
  available. Regenerate your client if you had a typed binding for that segment.
  The wire behaviour of both endpoints is unchanged, and both keep accepting
  whatever `[^/.]+` accepted: nothing about the route changed, only its
  description.
- `tests/test_openapi_schema.py` generates the schema and fails on any warning
  the package raises, so the next one is caught here rather than in an adopter's
  build. Two enum-naming warnings remain and are listed there explicitly rather
  than silenced -- closing them means either naming those choice sets from
  inside the serializers or documenting the `ENUM_NAME_OVERRIDES` entries an
  adopter has to add, since nothing in a library can set a project setting.

## 0.6.0

The host application's second pass over this package, and everything it found
was either a seam that stopped at the edge of the request cycle or a behaviour
documented at length and enforced nowhere. One item below changes what an
endpoint returns, and it is the one to read first.

- **The published OpenAPI schema said `409` for `PaymentProviderNotConfiguredError`
  and the handler returned `503`. The `503` is what stayed.** The status table
  has rendered that error `503` since it existed -- its own comment reasons it
  out as a deployment fault, and the README said `503` too -- but the
  `@extend_schema` annotations on `change-plan`, `cancel` and the add-on
  purchase declared it a `409`, named the class, gave its `code`, and said it
  was "mapped centrally by" the very handler they were contradicting. So the
  document adopters generate their clients from described a status this API has
  never returned, and the status it does return reached those clients as an
  unexpected server error. Both sides were deliberate, which is why neither was
  noticed. `503` won on three grounds: nothing the caller sent is wrong and the
  same request will fail identically until an operator fixes the deployment, so
  a 4xx would be an invitation to retry something that cannot work; the two
  sibling deployment faults (`NoDefaultBillingPlanError`,
  `IncompleteBillingPlanError`) already rendered `503`, and promoting one member
  of that family to `409` would have split it -- `change-plan` can raise
  `IncompleteBillingPlanError` too, so one endpoint would have answered two
  different statuses for two equally operator-caused faults; and a 5xx is what
  an adopter's error monitoring already watches, which is the difference between
  noticing an unconfigured provider within the hour and filing it under "clients
  are sending bad requests" for a week. **What an adopter must do:** regenerate
  your client, and if you wrote a `409` branch for
  `payment_provider_not_configured` against the 0.5.0 schema, move it to `503`.
  Your deployment was already answering `503`; that branch was dead code. The
  wire behaviour of those three endpoints is unchanged.
- **`GET /billing/payment-provider/` really did answer `409`, and now answers
  `503`.** This is the one genuine behaviour change in the release. That
  endpoint caught `PaymentProviderNotConfiguredError` itself and returned a
  hand-written `409`, while its unauthenticated sibling
  `GET /billing/payment-provider/default/` caught the same error, three hundred
  lines away in the same module, and returned `503`. If a frontend branches on
  `409` there to mean "this deployment has no provider configured", move it to
  `503`. The body is unchanged: that endpoint does not route through the central
  handler, so it still carries DRF's `{"detail": ...}` and no machine-readable
  `code`. Both endpoints are now asserted through real requests, and
  `tests/test_exception_handling.py` generates the OpenAPI document and fails
  when any status an annotation declares for a named error class contradicts
  `BILLING_ERROR_STATUS` -- so this class of divergence cannot reappear quietly.
  No other exception had the same disagreement: every other class the shipped
  annotations name (`ChargeDeclinedError`, `OverLimitError`,
  `UnconfirmedPlanChangeError`, `RetryPaymentNotApplicableError`,
  `SubscriptionNotAttachedError`, `NoOutstandingBalanceError`,
  `CollectionNotSupportedError`, `PaymentTokenRequiredError`,
  `AddOnNotPurchasableError`, `UnknownPaymentProviderError`) was already
  declared under the status the table renders it as.
- **`VINTA_BILLING['SERVICE_CONTAINER']` reaches the background jobs.** 0.5.0
  added the setting for the views and the admin; `vinta_billing/jobs.py`
  imported this package's own factories directly and never consulted it, so a
  project pointing the setting at its own container got its services on the
  request path and a second, parallel set of this package's own on the beat
  path. In the case that found this, that meant a Stripe adapter the project had
  deliberately crippled with an empty API key -- so a stray real call would fail
  loudly -- being silently replaced by one holding the real `STRIPE_SECRET_KEY`
  from its environment. The test believed it was driving a fake. The workaround
  that project reached for, resolving each service itself and passing it into
  every job, works but costs `JOB_DISPATCHER`: a sweep hands its dispatcher
  nothing but a job and a subscription id, so a queued job has no injected
  service to carry with it. All four sweeps default through `resolve_service`
  now, the same lookup the views use, and an explicitly passed service still
  wins over it exactly as it does for a viewset. `vinta_billing/jobs.py` had no
  tests at all before this release; `tests/test_jobs.py` covers the four sweeps,
  the precedence rule, and the two seams composing.
- **`SubscriptionService.retry_failed_charge` is tested, starting with the guard
  that keeps it off the zero-dollar money path.** Its docstring runs to eighty
  lines about four properties and enforced none of them: the package's own
  dunning tests drive a `FakeSubscriptionService` whose `retry_failed_charge` is
  a no-op recorder, and that fake was the only occurrence of the name anywhere
  in `tests/`. The load-bearing property is the provider guard -- a
  `CollectionNotSupportedError` raised for anything other than MercadoPago must
  re-raise rather than fall back into `_ensure_provider_plan` +
  `change_subscription_plan`, because that pair is the operation a live Stripe
  probe proved collects nothing at all against a real past-due invoice while
  flipping the provider-side subscription to `active`, with an `INFO` log the
  only way to notice. Nothing raises that error for Stripe today, which is
  precisely why it needed a test rather than an argument. Also pinned now:
  MercadoPago's fallback ladder order and the provider it drives;
  `NoOutstandingBalanceError` and a declined charge both being swallowed rather
  than raised out of a beat tick that would otherwise be redelivered and fail
  identically forever; a blank `external_id` being tolerated on that path; and
  the deliberate asymmetry with `retry_payment`, which raises for the same
  conditions because somebody is waiting on the answer. `retry_payment` gets its
  own coverage of the two things only it can get wrong -- attaching the new
  instrument before driving the charge, and namespacing its idempotency key away
  from the dunning ladder's, so a payer's new card is never deduplicated against
  the scheduled attempt that just failed on the old one.
  `EntitlementService.has_payment_method` had no test in this package either,
  and `DunningService.enter_grace` leaving `PaymentMethod` alone -- the thing
  that keeps a GRACE organization accruing postpaid usage -- is now asserted
  through the real transition rather than around it. Five of these were carried
  up from the host, which had been holding them on this package's behalf and
  said so in its module docstring. No behaviour changed.
- **The cycle-close row lock has a concurrency test, and the suite can run
  against a real database.** `CycleCloseService.close_subscription` takes a
  `SELECT ... FOR UPDATE` on the subscription row, and its docstring leans on
  that lock to promise a period is charged at most once under concurrency --
  the highest-severity failure this service has, because getting it wrong means
  charging a customer twice. There was no concurrency test in the package; a
  repo-wide grep of `tests/` for `threading` returned nothing. There could not
  usefully have been one, either, because the suite runs on SQLite, which has no
  row locks: Django notices and drops the clause rather than raising, so the
  lock is silently never taken there and a two-thread test would have passed
  against nothing. `tests/settings.py` switches to Postgres when
  `VINTA_BILLING_TEST_POSTGRES_HOST` is set, `tox -e postgres` sets it, CI runs
  that environment against a service container, and the new test skips itself
  wherever the database cannot take the lock. Two threads on two connections
  close the same subscription at once; exactly one finds a period to close, and
  the provider is asked to charge exactly once. With the lock patched out both
  threads close it and the assertion reports `[1, 1]`. `psycopg` joins the test
  dependency group -- nothing was added to the package's runtime dependencies,
  and the default `pytest` run is unchanged.

## 0.5.0

Another release driven by the host application migrating onto this package, and
this time what it found first was an authorization bypass. Everything else here
is what stood between that project and deleting its own copy of the REST layer.

- **SECURITY: the object-level billing permission decided nothing, and a child
  organization's administrator could act on a reseller root's billing.** This
  affects 0.3.0 and 0.4.0, and upgrading is the fix -- there is no configuration
  that closes it. `IsBillingManager.has_object_permission` read
  `getattr(obj, "organization", None)` and, finding nothing, fell back to
  `has_permission` -- the *request*-level check, which the caller has by
  definition already passed. Every object-level check the shipped viewsets make
  passes a billing root, and a billing root is an `Organization`, which has no
  `organization` field. So all three of them -- `MeteredOccurrenceViewSet.list`,
  `SubscriptionViewSet.get_subscription`, `AddOnViewSet.create`, each with a
  comment calling this "the real gate" -- degraded to the coarse one. An
  administrator of a child organization that bills against a reseller root it
  holds no membership in could change that root's plan, cancel its
  subscription, buy add-ons charged to its payment method, and read the whole
  pooled subtree's usage. The check now asks the predicate about the object
  itself when the object is an organization (tested against
  `vinta_orgs.models.AbstractOrganization`, so it holds under a swapped
  `ORGANIZATION_MODEL`), and is unchanged for every genuinely
  organization-scoped row. Whatever `BILLING_MANAGER_PREDICATE` names is what
  answers, so a project that configured a predicate needs no further change; a
  project on the permissive default gets "a member of that root" as the answer.
  The defect survived two releases because the suite tested the permission class
  in isolation, by handing `has_object_permission` an object it had built
  itself, and never sent a request through a mounted viewset to see what the
  class did with a real one. `tests/test_viewset_permissions.py` does that now,
  against all five root-gated endpoints, and before this fix five of them
  answered 200 or 400 to a caller they were supposed to refuse -- `cancel`
  answered 200, having genuinely cancelled another organization's subscription.
- **Stripe subscription charges resolved no payment at all, so nobody's dunning
  ever cleared.** `stripe.Invoice` has a `payments` field and has never had a
  `billing` one, and this adapter expanded `latest_invoice.payments` and then
  read `latest_invoice.billing` off the result -- as did the invoice-scoped
  lookup, which expanded `billing` too. Reading a field that does not exist
  returns nothing rather than raising, so
  `get_payment_external_id_from_subscription_payload` and
  `_get_payment_external_id_from_invoice` returned `None` for every Stripe
  subscription charge there has ever been. An `invoice.paid` webhook then
  matched no payment, the subscription was never taken out of grace, and a
  customer whose card had gone through kept being dunned toward suspension. Both
  call sites read `payments` now, which is what the surrounding docstrings said
  all along -- the rename looks like an over-eager search and replace during the
  extraction. The correct chain is
  `latest_invoice.payments.data[N].payment.payment_intent`, and the suite now
  pins each field of it against `stripe.Invoice.__annotations__` and
  `stripe.InvoicePayment.Payment.__annotations__` rather than against a
  remembered name, plus a guard that no call site in that module reads an
  invoice field the installed SDK does not have. Nothing to do on upgrade beyond
  taking it: a project on 0.3.0 or 0.4.0 charging through Stripe has
  subscriptions sitting in `grace`/`restricted` that were paid for, and their
  next successful webhook now resolves.
- **The package ships `py.typed`.** It was fully annotated and ran mypy in its
  own gate, but shipped no PEP 561 marker, so a consumer had to add it to
  `ignore_missing_imports` -- which makes every name in it `Any`. That is worse
  than untyped for a project re-exporting these models: a star re-export from an
  `Any` module re-exports nothing, and the host counted 565 extra errors from
  it -- 562 of them `[attr-defined]` on names that plainly exist. The marker is in
  the wheel and the sdist. If you added `vinta_billing` to
  `ignore_missing_imports`, remove that entry -- while it is there the
  annotations are still thrown away.
- **`VINTA_BILLING['VIEW_MIXIN']` and `VINTA_BILLING['SERVICE_CONTAINER']`: the
  shipped routes can be mounted by a project that has its own tenancy and its
  own DI.** 0.4.0 made `get_routes()` mountable; it did not make it *usable* by
  a project whose DRF surface resolves the acting organization its own way, or
  whose services come out of its own container. Such a project had to restate
  the whole REST layer -- one subclass per viewset, plus its own route table --
  to change those two things, which is exactly what the host was still doing.
  Both are settings now. `VIEW_MIXIN` names a mixin that is mixed in *front* of
  every tenant-scoped viewset these routes mount (the plan catalogue and the two
  provider webhooks are left alone -- one answers the same for everybody, the
  others are authenticated by a provider signature). `SERVICE_CONTAINER` names
  the module or object the views and the admin build their services from: the
  service called `payment_service` is looked up as
  `container.get_payment_service()` when that exists and
  `container.payment_service()` otherwise, the second spelling being what a
  `dependency_injector` container offers, so one can be pointed at directly.
  Both default to today's behaviour, and by identity rather than by
  equivalence: `VIEW_MIXIN` defaults to the very mixin those viewsets already
  inherit, so `apply_view_mixin` hands back the same class objects and a project
  that configures nothing mounts what it always did; `SERVICE_CONTAINER`
  defaults to `vinta_billing.services.container`, which is where every one of
  those call sites already went. Passing a service to a viewset's constructor
  still wins over both. `vinta_billing.view_mixins.apply_view_mixin` and
  `vinta_billing.services.container.resolve_service` are public, for a project
  that wants to do either by hand.
- **`TenantScopedViewMixin` composes with a mixin in front of it, and stops
  stamping a lazy nothing onto the request.** Its `initial()` used to assign
  whatever `resolve_organization` returned, which broke both ways. A project
  mixin in front spells `resolve_organization` too --
  `vinta_orgs.drf.OrganizationScopedAPIViewMixin` does -- and means the opposite
  by it: it *assigns* `request.organization` and returns `None`. Its method wins
  on name resolution, so the returned `None` landed on top of the organization
  it had just resolved and every billing endpoint answered 403. Resolution moved
  into `resolve_request_organization`, which reads what is already on the
  request first, asks `resolve_organization` second, and reads the request again
  before believing a `None` -- so a project mixin needs no adapter, and its
  resolution runs once rather than twice. The same method now normalizes a
  falsy result to a real `None`: under `OrganizationMiddleware` an unresolved
  organization arrives as a `SimpleLazyObject` wrapping `None`, which is not
  `None` by identity, so every `is None` check downstream -- including the
  fail-closed branch in `filter_queryset_by_organization` -- read it as an
  organization and a request that should have been refused went on to build a
  query against it. `resolve_organization` itself is unchanged, and a project
  that overrode it is unaffected.
- **`BillingProfileAdmin.save_model` can complete a provider repoint again.** It
  took a `subscription_service` argument that nothing ever passed -- Django
  calls `save_model(request, obj, form, change)` -- and raised `RuntimeError`
  when it was missing, so every audited repoint made through the admin failed,
  in the one place a staff member is meant to be able to make one. It resolves
  the service through `SERVICE_CONTAINER` now, the same fallback 0.4.0 gave the
  viewsets and the only call site that had been left without it. The keyword
  still wins, for a subclass that hands its own service over.

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
  complaint. The reversible names are unchanged, character for character, but
  the paths move from `payments/...` to `billing/payments/...`, bringing them
  in line with every other route this package serves -- `get_extra_patterns()`
  already hardcoded its other two endpoints under `billing/payment-provider`,
  so `payments/` sitting apart from that prefix was this module's own
  inconsistency, not a project's choice to preserve. Moving it is safe because
  there is nothing running on the old path to move away from: 0.3.0's
  `PaymentsViewSet.__init__` required `payment_service`, `subscription_service`,
  and `dunning_service` as keyword-only arguments with no default -- the same
  defect fixed above -- so any request that ever reached `payments/...` raised
  `TypeError` before a provider's notification could be processed. No
  deployment has a webhook that ever worked at that path, so no callback URL
  already published to a provider is broken by the move. What did change is
  that `get_routes()` no longer lists
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
