"""Backwards-compatible aliases for :mod:`vinta_billing.routing`.

This module predates ``routing`` and shipped a second, hand-maintained copy of
the same route table -- one that had already drifted, mounting
``PaymentsViewSet`` under a different prefix than ``routing`` did. Two lists that
must agree and are edited separately eventually disagree, and a project mounting
whichever one it happened to import got URLs the provider callbacks were never
built against.

So there is one list now, in ``routing``, and these names are views onto it. They
are evaluated at import time, exactly as before; prefer the functions in
``routing`` for new code, which resolve when they are called.
"""

from vinta_billing.routing import RouteDict, get_extra_patterns, get_routes


routes: list[RouteDict] = get_routes()

# Non-viewset routes (APIViews / manually-bound ViewSets) -- URL patterns registered
# directly with the Django URL conf, bypassing the shared router. See
# `get_extra_patterns`'s docstring for why the inbound provider webhooks and the
# organization payment-provider endpoint are bound this way instead of through
# `router.register(...)`.
extra_patterns = get_extra_patterns()
