"""The stdlib boundary, enforced (SPEC.md §0.1 (3)).

`buckshotcalc/engine/` and `buckshotcalc/sim/` must import only the standard
library, so the solver stays importable and testable headless. Third-party
deps (`textual`, `rich`) live only in `buckshotcalc/tui/`.

Until this file existed that rule was enforced by nobody — it held for eight
milestones on manual inspection alone, which is exactly the kind of invariant
that survives right up until the change that breaks it. It matters more now
than it used to: `sim/tables.py` imports `tui/analysis.py`, which is legal
only because that module is itself stdlib-only. Nothing but this test stops a
later `import rich` there from silently making `sim` un-importable headless.

Checked by parsing imports rather than importing, so a violation is reported
as a named module in a named file instead of an ImportError somewhere down a
chain, and so the test passes in an environment where the third-party
packages happen to be installed (as this one's venv is — an import-based
check would notice nothing).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "buckshotcalc"

#: Modules required to import only the standard library (plus buckshotcalc
#: itself). `tui/session.py` and `tui/analysis.py` live under `tui/` but are
#: deliberately stdlib — that is what lets the undo/edit model be tested
#: headless and what makes `sim`'s dependency on `analysis` safe.
STDLIB_ONLY = (
    *sorted((PACKAGE_ROOT / "engine").glob("*.py")),
    *sorted((PACKAGE_ROOT / "sim").glob("*.py")),
    PACKAGE_ROOT / "tui" / "session.py",
    PACKAGE_ROOT / "tui" / "analysis.py",
)


def _imported_roots(path: Path) -> set[str]:
    """Every top-level package name `path` imports, at any statement depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # `level > 0` is a relative import: same package, never external.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


@pytest.mark.parametrize("path", STDLIB_ONLY, ids=lambda p: str(p.name))
def test_module_imports_only_stdlib(path: Path) -> None:
    allowed = set(sys.stdlib_module_names) | {"buckshotcalc", "__future__"}
    offenders = sorted(_imported_roots(path) - allowed)
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT.parent)} imports non-stdlib "
        f"{offenders} — SPEC.md §0.1 (3) reserves third-party deps for "
        "buckshotcalc/tui/ (and not tui/session.py or tui/analysis.py, which "
        "are stdlib by design)."
    )


def test_the_guard_would_actually_catch_a_violation() -> None:
    """The check must fail on a real third-party import, not just pass quietly.

    A boundary test that cannot fail is worse than none, because it reads as
    coverage. `rich` is installed here, so this also confirms the check does
    not depend on the package being absent.
    """
    offenders = _imported_roots(PACKAGE_ROOT / "tui" / "panels.py") - (
        set(sys.stdlib_module_names) | {"buckshotcalc", "__future__"}
    )
    assert "rich" in offenders, (
        "panels.py imports rich; if this does not register, the boundary "
        "check above is not actually checking anything"
    )


# ---------------------------------------------------------------------------
# Packaging: rules.toml ships with the wheel (SPEC.md §11) without
# shadowing the source tree in an editable install.
# ---------------------------------------------------------------------------


def test_rules_toml_lives_inside_the_package():
    """`packages = ["buckshotcalc"]` is what ships it — so it has to be in there.

    SPEC.md §11 and SPEC.md §0.1 both require `rules.toml` to be packaged with
    the wheel, and `engine/rules.py` resolves it relative to the package.
    Both hold for free while the file sits inside the package directory.
    """
    root = Path(__file__).resolve().parent.parent
    assert (root / "buckshotcalc" / "rules.toml").is_file()


def test_pyproject_does_not_force_include_rules_toml():
    """A `force-include` of `rules.toml` breaks editable installs (regression guard).

    Because the file is already inside the package, force-including it added
    nothing to the wheel — verified by diffing the built wheel's contents
    before and after removal: no files lost. What it *did* do was make an
    editable install materialize a real `site-packages/buckshotcalc/`
    directory containing only `rules.toml` and no `__init__.py`. That
    directory shadows the source tree for any process whose `sys.path[0]` is
    not the repo root, so `import buckshotcalc.sim` failed outright when
    running a harness from anywhere else — which is exactly how
    `sim/reload_bench.py` and `sim/montecarlo.py` are meant to be run.

    `pytest` from the repo root never saw it, which is why this survived
    eight milestones undetected.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get(
        "wheel", {}
    )
    assert "force-include" not in wheel, (
        "a wheel force-include re-creates the site-packages shadow directory "
        "that breaks `import buckshotcalc.sim` outside the repo root"
    )
