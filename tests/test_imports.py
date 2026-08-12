"""Every shipped module imports.

Cheap, and it catches the whole class of extraction mistakes that a targeted
suite misses: a module nothing else imports (a management command, an admin, a
task) still referencing something from the application this package came out of.
"""

import importlib
import pkgutil

import pytest

import billing


def _module_names():
    return sorted(
        module.name
        for module in pkgutil.walk_packages(billing.__path__, "billing.")
        if ".migrations" not in module.name
    )


@pytest.mark.parametrize("module_name", _module_names())
def test_module_imports(module_name):
    importlib.import_module(module_name)


def test_the_package_leaks_no_host_application_imports():
    """No module may import from the project this was extracted from.

    A stale import here is invisible until the one code path that reaches it
    runs -- typically a Celery beat task, in production, at 3am.
    """
    import pathlib

    forbidden = (
        "calendar_integration",
        "webhooks.models",
        "public_api",
        "di_core",
        "vinta_schedule_api",
        "from audit",
        "from common",
    )
    offenders = []
    for path in pathlib.Path(billing.__path__[0]).rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if any(name in stripped for name in forbidden):
                offenders.append("%s:%d %s" % (path.name, number, stripped))

    assert offenders == []
