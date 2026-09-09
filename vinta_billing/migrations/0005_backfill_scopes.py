"""Give every existing organization a scope row, and point the billing rows at it.

Idempotent and reversible. Re-running it after a partial failure resolves to the
same scopes -- ``get_or_create`` on ``(content_type, object_id)`` -- and never
duplicates one; reversing it puts every ``organization_id`` back from the scope
the row now names.

**A fresh install runs this as a no-op**, because there are no rows to migrate.
An installation upgrading from 0.7 sets ``VINTA_BILLING['LEGACY_SCOPE_MODEL']``
to whatever its ``ORGANIZATION_MODEL`` was so the scopes it creates name the
right content type::

    VINTA_BILLING = {"LEGACY_SCOPE_MODEL": "vinta_orgs.Organization"}

The setting is read here rather than taken from ``ORGANIZATION_MODEL`` directly
so that this migration carries no dependency on a package this one no longer
requires -- and so an installation that swapped the organization model names its
own.
"""

from django.db import migrations


#: Tables whose ``organization_id`` becomes a ``scope_id``.
SCOPED_MODELS = (
    "BillingProfile",
    "Subscription",
    "PaymentMethod",
    "MeteredOccurrence",
    "BillingPeriodSummary",
)


def _legacy_content_type(apps, schema_editor):
    """The content type of whatever this installation used to bill.

    ``None`` when the project never set ``LEGACY_SCOPE_MODEL``, which is the
    normal state for a fresh install and the signal to do nothing.
    """
    from vinta_billing.conf import get_setting

    label = get_setting("LEGACY_SCOPE_MODEL")
    if not label:
        return None

    app_label, model_name = label.split(".")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type, _created = ContentType.objects.get_or_create(
        app_label=app_label, model=model_name.lower()
    )
    return content_type


def forwards(apps, schema_editor):
    BillingScope = apps.get_model("vinta_billing", "BillingScope")
    db = schema_editor.connection.alias

    # Every organization id referenced anywhere, gathered before a single write
    # so one scope is created per organization rather than one per table.
    organization_ids: set[int] = set()
    for model_name in SCOPED_MODELS:
        model = apps.get_model("vinta_billing", model_name)
        organization_ids.update(
            model.objects.using(db)
            .filter(organization_id__isnull=False)
            .values_list("organization_id", flat=True)
        )

    if not organization_ids:
        # Fresh install, or an installation with no billing rows yet. Either way
        # there is nothing to name and no reason to demand LEGACY_SCOPE_MODEL.
        _migrate_usage_breakdowns(apps, db)
        return

    content_type = _legacy_content_type(apps, schema_editor)
    if content_type is None:
        raise RuntimeError(
            "vinta_billing has %d organization(s) to migrate onto scopes but "
            "VINTA_BILLING['LEGACY_SCOPE_MODEL'] is not set. Set it to the model "
            "your ORGANIZATION_MODEL named (e.g. 'vinta_orgs.Organization') and "
            "re-run this migration." % len(organization_ids)
        )

    scope_by_organization: dict[int, int] = {}
    for organization_id in sorted(organization_ids):
        scope, _created = BillingScope.objects.using(db).get_or_create(
            content_type=content_type,
            object_id=str(organization_id),
            defaults={
                "scope_type": "organization",
                # `save()` is not called through the historical model, so the
                # key is built here rather than derived. Same format as
                # `BillingScope.build_scope_key`.
                "scope_key": "%s.%s:%s" % (
                    content_type.app_label,
                    content_type.model,
                    organization_id,
                ),
                "meta": {},
            },
        )
        scope_by_organization[organization_id] = scope.pk

    for model_name in SCOPED_MODELS:
        model = apps.get_model("vinta_billing", model_name)
        for organization_id, scope_id in scope_by_organization.items():
            model.objects.using(db).filter(organization_id=organization_id).update(
                scope_id=scope_id
            )

    _migrate_usage_breakdowns(apps, db, scope_by_organization)


def _migrate_usage_breakdowns(apps, db, scope_by_organization=None):
    """Re-key ``{organization_id: count}`` into ``{scope_id: count}``.

    Keys are strings in the JSON blob (``JSONField`` gives back whatever JSON
    allows, and JSON object keys are always strings), which is why both sides
    are stringified rather than compared as integers.
    """
    Usage = apps.get_model("vinta_billing", "BillingPeriodResourceUsage")
    mapping = scope_by_organization or {}
    for usage in Usage.objects.using(db).all().iterator():
        by_organization = usage.by_organization or {}
        if not by_organization:
            continue
        usage.by_scope = {
            str(mapping[int(pk)]): count
            for pk, count in by_organization.items()
            if int(pk) in mapping
        }
        usage.save(update_fields=["by_scope"])


def backwards(apps, schema_editor):
    """Put the organization ids back, reading them off the scopes.

    The scope rows themselves are left in place: they are the thing 0003
    created, and deleting them here would take out any scope a project made for
    a payer that is not an organization.
    """
    db = schema_editor.connection.alias

    for model_name in SCOPED_MODELS:
        model = apps.get_model("vinta_billing", model_name)
        for row in model.objects.using(db).filter(scope_id__isnull=False).iterator():
            model.objects.using(db).filter(pk=row.pk).update(
                organization_id=int(row.scope.object_id)
            )

    Usage = apps.get_model("vinta_billing", "BillingPeriodResourceUsage")
    BillingScope = apps.get_model("vinta_billing", "BillingScope")
    organization_by_scope = {
        pk: int(object_id)
        for pk, object_id in BillingScope.objects.using(db).values_list("pk", "object_id")
    }
    for usage in Usage.objects.using(db).all().iterator():
        by_scope = usage.by_scope or {}
        if not by_scope:
            continue
        usage.by_organization = {
            str(organization_by_scope[int(pk)]): count
            for pk, count in by_scope.items()
            if int(pk) in organization_by_scope
        }
        usage.save(update_fields=["by_organization"])


class Migration(migrations.Migration):
    dependencies = [
        ("vinta_billing", "0004_add_scope_columns"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
