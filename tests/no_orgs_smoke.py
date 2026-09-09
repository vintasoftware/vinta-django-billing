"""Prove the package works with ``vinta-django-orgs`` absent entirely.

Run as a script, not through pytest, and against an environment where the
``orgs`` extra was *not* installed -- ``tox -e noorgs`` sets that up. It cannot
be an ordinary test: the rest of the suite needs ``vinta_orgs`` (the fixtures
build organizations, and ``tests/test_request_seams.py`` exercises
``vinta_billing.contrib.orgs``), so the claim can only be checked from a process
where the package genuinely is not importable.

What it checks is the whole point of the 0.8 change, and each line of it used to
be false:

* ``vinta_billing`` imports with no ``vinta_orgs`` on the path;
* every migration runs with no ``ORGANIZATION_MODEL`` setting defined -- the one
  that used to read it at import time is 0001;
* a plan can be sold to a **user**, with no organization anywhere.
"""

import os
import sys


def main() -> int:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings_no_orgs")
    django.setup()

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    failures = []

    def check(label, condition):
        print("%-58s %s" % (label, "ok" if condition else "FAILED"))
        if not condition:
            failures.append(label)

    try:
        import vinta_orgs  # noqa: F401

        print("vinta_orgs is importable -- run this through `tox -e noorgs`")
        return 2
    except ModuleNotFoundError:
        pass

    check("vinta_orgs is not installed", True)
    check("ORGANIZATION_MODEL is not defined", not hasattr(settings, "ORGANIZATION_MODEL"))
    check(
        "BILLING_SCOPE_MODEL defaulted itself",
        settings.BILLING_SCOPE_MODEL == "vinta_billing.BillingScope",
    )

    call_command("migrate", verbosity=0)
    check("every migration ran", True)

    from vinta_billing.models import BillingScope, Subscription

    check(
        "the scope relation points at the shipped model",
        Subscription._meta.get_field("scope").related_model is BillingScope,
    )

    user = get_user_model().objects.create_user(username="solo")
    scope, created = BillingScope.objects.get_or_create_for(user)
    check("a user became a billing scope", created and scope.scope_type == "user")
    check("the user owns it", scope.owner_id == user.pk)
    check("its key names the user", scope.scope_key == "auth.user:%d" % user.pk)

    from vinta_billing.permissions import owner_may_manage_billing

    check("the shipped predicate lets them manage it", owner_may_manage_billing(user, scope))

    if failures:
        print("\n%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("\nall checks passed with vinta-django-orgs absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
