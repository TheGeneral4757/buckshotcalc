"""The advice log writer and report (decisions-pending item 16). No reload-table builds."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from buckshotcalc.engine import memo_key
from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.tui import advice
from buckshotcalc.tui.app import BuckshotApp
from tests.test_tui import RULES, _solver, _state

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import advice_report as rep  # noqa: E402


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_path_derivation_from_record():
    assert advice.advice_path_for(Path("temp/games/X.jsonl")) == Path("temp/advice/X.jsonl")


def test_decision_row_fields(tmp_path):
    s = _state(2, 3, hp=(1, 3))
    res = _solver().evaluate(s)
    log = advice.AdviceLog(tmp_path / "a" / "g.jsonl", RULES, "BASE")
    log.decision(7, s, res, Action(ActionKind.SHOOT_SELF), None)
    (row,) = _rows(tmp_path / "a" / "g.jsonl")
    assert row["v"] == 1 and row["ev"] == "decision" and row["seq"] == 7
    assert row["game"] == "g.jsonl" and row["state"] == memo_key.encode(s, RULES)
    assert row["summary"]["live"] == 2 and row["summary"]["blank"] == 3
    assert row["summary"]["hp"] == [1, 3]
    assert row["rules_fp"] and row["engine_fp"] and row["no_rec"] is None
    by = {c["action"]: c for c in row["children"]}
    assert by["SHOOT SELF"]["dies"] == pytest.approx(0.4)
    assert by["SHOOT DEALER"]["dies"] == 0.0
    assert row["rec"] == row["children"][0]["action"]
    assert row["taken"] == "SHOOT SELF"
    assert row["taken_value"] == pytest.approx(by["SHOOT SELF"]["value"])


def test_no_rec_row_still_written(tmp_path):
    s = _state(2, 3, hp=(1, 3))
    log = advice.AdviceLog(tmp_path / "g.jsonl", RULES, "BASE")
    log.decision(1, s, _solver().evaluate(s), Action(ActionKind.SHOOT_SELF), "stale: x")
    (row,) = _rows(tmp_path / "g.jsonl")
    assert row["no_rec"] == "stale: x" and row["children"] == [] and row["taken_value"] is None


def test_round_end_and_load_rows(tmp_path):
    log = advice.AdviceLog(tmp_path / "g.jsonl", RULES, "BASE")
    log.load({"seq": 2, "kind": "new_round", "live": 2, "blank": 3, "hp": [3, 3], "max_hp": 3})
    log.round_end({"seq": 9, "who": "dealer"})
    load, end = _rows(tmp_path / "g.jsonl")
    assert load["kind"] == "new_round" and load["max_hp"] == 3
    assert end["winner"] == "player"


def test_oserror_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = advice.AdviceLog(blocker / "g.jsonl", RULES, "BASE")
    s = _state(2, 3, hp=(1, 3))
    log.decision(1, s, None, Action(ActionKind.SHOOT_SELF), "no result yet")
    log.round_end({"who": "player"})


def test_app_reports_stale_result():
    app = BuckshotApp()
    assert app._no_rec_reason() is not None  # no result yet
    s = _state(2, 3, hp=(1, 3))
    app._result, app._result_state = _solver().evaluate(s), s
    app._reload_table_missing = False
    assert "stale" in (app._no_rec_reason() or "")


# -- report -------------------------------------------------------------------


def _dec(hp, rec, taken, children, **kw):
    return {
        "ev": "decision", "summary": {"hp": [hp, 3]}, "rec": rec, "taken": taken,
        "rec_value": children[rec][0], "taken_value": children[taken][0], "no_rec": None,
        "children": [{"action": a, "value": v, "wasted": False, "dies": d}
                     for a, (v, d) in children.items()], **kw,
    }


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_calibration_bins_and_brier(tmp_path):
    def rnd(p, winner):
        return [{"ev": "load", "kind": "new_round"}, {"ev": "decision", "rec_value": p},
                {"ev": "round_end", "winner": winner}]
    f = _write(tmp_path / "a.jsonl", rnd(0.8, "player") + rnd(0.85, "dealer") + rnd(0.3, "dealer"))
    table, brier = rep.calibration(rep.rounds(rep.load_rows([f])))
    assert [(lo, n) for lo, n, _, _ in table] == [(30, 1), (80, 2)]
    assert table[1][2] == pytest.approx(0.825) and table[1][3] == 0.5 and table[0][3] == 0.0
    assert brier == pytest.approx((0.04 + 0.7225 + 0.09) / 3)


def test_deviation_costs_and_margin(tmp_path):
    rows = [
        _dec(1, "SHOOT SELF", "SHOOT DEALER",
             {"SHOOT SELF": (0.62, 0.4), "SHOOT DEALER": (0.58, 0.0)}),
        _dec(1, "SHOOT SELF", "USE HANDSAW",
             {"SHOOT SELF": (0.60, 0.4), "USE HANDSAW": (0.50, 0.0)}),
        _dec(2, "SHOOT DEALER", "SHOOT DEALER", {"SHOOT DEALER": (0.7, 0.0)}),
        _dec(3, "SHOOT DEALER", "USE BEER",
             {"SHOOT DEALER": (0.70, 0.0), "USE BEER": (0.65, 0.0)}),
        {"ev": "decision", "no_rec": "stale: x", "summary": {"hp": [1, 3]}},
    ]
    r = rep.load_rows([_write(tmp_path / "b.jsonl", rows)])
    dev = rep.deviations(r)
    assert dev["all"][:2] == (4, 3) and dev["all"][2] == pytest.approx(0.19)
    assert dev["hp=1"][:2] == (2, 2) and dev["hp=1"][2] == pytest.approx(0.14)
    assert dev["kind=item"][:2] == (2, 2) and dev["kind=item"][2] == pytest.approx(0.15)
    assert dev["rec_self=yes"][2] == pytest.approx(0.14)
    assert dev["item=USE HANDSAW"][2] == pytest.approx(0.10)
    pref = rep.preference(r)
    assert pref["hp=1"]["dev"] == 2 and pref["hp=1"]["safer"] == 2
    assert pref["hp=2"]["dev"] == 0
    assert pref["hp=1-2"]["m"] == pytest.approx(0.10)
    assert "suggested safety margin m = 10.00 points" in rep.report(r)
