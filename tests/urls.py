from django.contrib import admin
from django.urls import include, path

from vinta_billing.routing import billing_router, get_extra_patterns


# Mounted under the `billing` namespace, matching the `URL_NAMESPACE` default
# the provider adapters reverse their callback URLs through.
billing_patterns = ([*billing_router().urls, *get_extra_patterns()], "billing")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(billing_patterns, namespace="billing")),
]
