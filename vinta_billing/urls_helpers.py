"""Building the absolute callback URLs providers post back to."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from vinta_billing.conf import get_setting


def namespaced(url_name: str) -> str:
    """Prefix ``url_name`` with the configured namespace, if there is one."""
    namespace = get_setting("URL_NAMESPACE")
    if not namespace:
        return url_name
    return "%s:%s" % (namespace, url_name)


def absolute_url(url_name: str, **kwargs: object) -> str:
    """Reverse ``url_name`` and make it absolute.

    Providers call these back from outside the process, so a relative path is
    useless -- hence the hard failure rather than a silently relative URL that
    fails much later, as an unexplained missing webhook.
    """
    site_domain = get_setting("SITE_DOMAIN") or getattr(settings, "SITE_DOMAIN", None)
    if not site_domain:
        raise ImproperlyConfigured(
            "Building a provider callback URL needs an absolute base. Set "
            "VINTA_BILLING['SITE_DOMAIN'] (or a top-level SITE_DOMAIN) to "
            "something like 'https://api.example.com'."
        )
    path = reverse(namespaced(url_name), kwargs=kwargs)
    return "%s%s" % (str(site_domain).rstrip("/"), path)
