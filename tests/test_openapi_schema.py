"""The schema this package generates must be warning-free for its adopters.

drf-spectacular re-emits every warning raised while generating a schema as a
``drf_spectacular.W001`` from its ``--deploy`` system check. A project whose
build runs ``manage.py check --deploy --fail-level WARNING`` -- the shape both
Render's and Heroku's Django buildpacks ship -- therefore fails its deploy on
any warning this package's own viewsets provoke, in a package it did not write
and cannot annotate without reaching into it from the outside.

That is not hypothetical. Through 0.6.0 the two inbound webhooks emitted one
warning each, for exactly this reason: `get_extra_patterns` binds them with
`re_path` and a bare `(?P<name>[^/.]+)` group, and `PaymentsViewSet` is a plain
`ViewSet` with no queryset, so nothing was left to infer a path parameter type
from. An adopter's deploy failed on two warnings about a route only a payment
provider ever calls.

Asserted over the whole document rather than only those two routes: any viewset
here that later leaves drf-spectacular guessing breaks an adopter's build the
same way, and it should break this suite first. The two enum-naming warnings
this package still raises are listed below rather than silenced -- see
``KNOWN_WARNINGS``.
"""

import pytest


def _generate_schema():
    """The rendered document, plus every warning generating it raised.

    ``GENERATOR_STATS``'s caches are class attributes that nothing resets
    between generations, so any earlier test that built a schema (there are
    several) would otherwise leak its warnings into this one's result -- and
    this one's into whatever runs next. Reset on both sides.
    """
    from drf_spectacular.drainage import GENERATOR_STATS
    from drf_spectacular.generators import SchemaGenerator

    GENERATOR_STATS.reset()
    try:
        with GENERATOR_STATS.silence():
            schema = SchemaGenerator(urlconf="tests.urls").get_schema(request=None, public=True)
        return schema, sorted(GENERATOR_STATS._warn_cache)
    finally:
        GENERATOR_STATS.reset()


#: Warnings this package still provokes, recorded rather than silenced.
#:
#: Both are the same adopter-facing problem as the path parameters this module
#: was added for -- an adopter gating its build on `--fail-level WARNING` fails
#: on these too -- but the fix is not the same and does not belong in the same
#: change: nothing here can set `ENUM_NAME_OVERRIDES`, which is a project
#: setting, so closing them means either naming those choice sets from inside
#: the serializers or documenting the two entries an adopter has to add. Listed
#: explicitly so the gap is visible and every *other* warning still fails, and
#: written as a subset check so fixing one does not fail this test.
KNOWN_WARNINGS = frozenset(
    {
        "Warning: encountered multiple names for the same choice set (PaymentProviderEnum). "
        "This may be unwanted even though the generated schema is technically correct. "
        "Add an entry to ENUM_NAME_OVERRIDES to fix the naming.",
        "Warning: encountered multiple names for the same choice set (PendingBillingIntervalEnum). "
        "This may be unwanted even though the generated schema is technically correct. "
        "Add an entry to ENUM_NAME_OVERRIDES to fix the naming.",
    }
)


def test_generating_the_schema_raises_no_new_warnings():
    pytest.importorskip("drf_spectacular")

    _, warnings = _generate_schema()

    unexpected = sorted(set(warnings) - KNOWN_WARNINGS)
    assert not unexpected, "schema generation raised new warnings:\n  " + "\n  ".join(unexpected)


WEBHOOK_PATHS = [
    "/api/billing/payments/{id}/payment-update/{provider}/",
    "/api/billing/payments/{id}/subscription-payment-update/{provider}/",
]


@pytest.mark.parametrize("path", WEBHOOK_PATHS)
def test_webhook_path_parameters_are_typed(path):
    """``id`` is the ``Payment`` id the ``notification_url`` embedded, and
    ``Payment`` inherits ``BaseModel`` under this app's ``BigAutoField``
    default -- so it is an integer, and a client generated off this document
    should be told so rather than handed the ``string`` the warning defaulted
    to. Asserted separately from the gate above so a regression names the
    parameter that lost its type."""
    pytest.importorskip("drf_spectacular")

    schema, _ = _generate_schema()

    parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"][path]["post"]["parameters"]
        if parameter["in"] == "path"
    }

    assert set(parameters) == {"id", "provider"}
    assert parameters["id"]["schema"] == {"type": "integer"}
    assert parameters["provider"]["schema"] == {"type": "string"}
    assert all(parameter["required"] for parameter in parameters.values())
