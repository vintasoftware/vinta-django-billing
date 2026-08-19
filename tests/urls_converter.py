"""The same mounting as ``tests.urls``, on a path-converter router.

A project that builds its routers with ``use_regex_path=False`` mounts the
shipped endpoints exactly like anybody else. This urlconf is what
``tests/test_routing.py`` points ``ROOT_URLCONF`` at to prove the URLs come out
usable rather than carrying an unrendered regex.
"""

from django.urls import include, path

from vinta_billing.routing import billing_router, get_extra_patterns


billing_patterns = (
    [*billing_router(use_regex_path=False).urls, *get_extra_patterns()],
    "billing",
)

urlpatterns = [path("api/", include(billing_patterns, namespace="billing"))]
