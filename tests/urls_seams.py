"""The same mounting as ``tests.urls``, for a project that supplied the seams.

A urlconf of its own, and reloaded by the tests that point ``ROOT_URLCONF``
here: ``get_routes()`` reads ``VINTA_BILLING['VIEW_MIXIN']`` while it builds the
route table, so the table has to be built while the override is in force.
Reloading ``tests.urls`` in place would leave the rest of the suite mounted on
classes built for one test.
"""

from django.urls import include, path

from vinta_billing.routing import billing_router, get_extra_patterns


billing_patterns = ([*billing_router().urls, *get_extra_patterns()], "billing")

urlpatterns = [path("api/", include(billing_patterns, namespace="billing"))]
