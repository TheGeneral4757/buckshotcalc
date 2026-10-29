from __future__ import annotations

from buckshotcalc import bundled
from buckshotcalc.engine.rules import load_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import GameMode


def test_shipped_table_matches_current_rules_and_engine(tmp_path, monkeypatch):
    # Fails when rules.toml or engine code changed without refreshing buckshotcalc/tables/.
    # Fix: rebuild (`buckshotcalc --build`), copy the new cache file there, delete the old.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert bundled.install() >= 1
    assert Solver(load_rules(), GameMode.DOUBLE_OR_NOTHING).has_cached_reload_table()
    assert bundled.install() == 0
