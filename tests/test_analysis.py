"""Tests for Solver.evaluate(progress=...) and the watch-mode analysis helpers."""

from __future__ import annotations

import threading

import pytest

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.solver import SearchCancelled
from buckshotcalc.engine.state import ChamberState, GameState, Item, ItemCounts, Player, Shell
from buckshotcalc.tui import analysis
from tests.test_tui import RULES, _solver, _state

GLASS = (ItemCounts.from_mapping({Item.MAGNIFYING_GLASS: 1}), ItemCounts.empty())
SHOOT = Action(ActionKind.SHOOT_OPPONENT)


def _calls(state):
    calls: list[tuple[int, int, int]] = []
    res = _solver().evaluate(state, progress=lambda d, t, n: calls.append((d, t, n)))
    return res, calls


@pytest.mark.parametrize(
    "state",
    [
        _state(2, 2, items=GLASS),
        _state(2, 1, turn=Player.DEALER),
        _state(1, 1, hp=(0, 3)),
    ],
)
def test_progress_same_result_and_monotone(state):
    plain = _solver().evaluate(state)
    res, calls = _calls(state)
    assert (res.value, res.children, res.cap_reached) == (
        plain.value,
        plain.children,
        plain.cap_reached,
    )
    assert calls[0][0] == 0
    assert calls[-1][0] == calls[-1][1]
    assert all(c[1] == calls[0][1] for c in calls)
    assert [c[0] for c in calls] == sorted(c[0] for c in calls)
    assert [c[2] for c in calls] == sorted(c[2] for c in calls)


def test_progress_player_node_total_is_action_count():
    res, calls = _calls(_state(2, 2, items=GLASS))
    assert calls[0][1] == len(res.children) == 3
    assert len(calls) == 4


def test_cancel_mid_progress():
    ev = threading.Event()
    solver = _solver()

    def prog(done, total, nodes):
        if done == 1:
            ev.set()

    state = _state(3, 3, items=GLASS)
    with pytest.raises(SearchCancelled):
        solver.evaluate(state, cancel=ev, progress=prog)
    # the memo stays valid after a cancel
    assert solver.evaluate(state).value == pytest.approx(_solver().evaluate(state).value)


def test_plan_tree_glass_then_shoot_by_outcome():
    # 1 live + 1 blank, holding a glass. Live revealed: shoot the Dealer.
    # Blank revealed: shoot self (free, keeps the turn, leaves a known live).
    tree = analysis.plan_tree(_solver(), _state(1, 1, items=GLASS))
    assert tree.action == "USE MAGNIFYING_GLASS"
    by = {c.label: c for c in tree.children}
    assert set(by) == {"live", "blank"}
    assert by["live"].action == "SHOOT DEALER" and by["live"].weight == pytest.approx(0.5)
    assert by["blank"].action == "SHOOT SELF"
    assert by["live"].children[0].end == "dealer's turn"


def test_plan_tree_depth_limits_and_terminal():
    tree = analysis.plan_tree(_solver(), _state(1, 1, items=GLASS), depth=1)
    assert all(c.action is None and c.end == "..." for c in tree.children)
    t = analysis.plan_tree(_solver(), _state(1, 1, turn=Player.DEALER))
    assert t.action is None and t.end == "dealer's turn"


def test_plan_tree_weights_sum_to_one():
    tree = analysis.plan_tree(_solver(), _state(2, 0))
    assert sum(c.weight for c in tree.children) == pytest.approx(1.0)


def test_dealer_forecast_merges_and_sums_to_one():
    st = _state(1, 1, turn=Player.DEALER)
    rows = analysis.dealer_forecast(st, RULES, _solver())
    assert sum(r.probability for r in rows) == pytest.approx(1.0)
    assert len({r.label for r in rows}) == len(rows)
    probs = [r.probability for r in rows]
    assert probs == sorted(probs, reverse=True)
    assert {r.label for r in rows} == {"shoots you", "shoots itself"}
    assert all(r.win_after is not None for r in rows)
    assert analysis.dealer_forecast(st, RULES)[0].win_after is None


def test_round_recap_summary():
    rc = analysis.RoundRecap()
    st = _state(1, 1)
    rc.record(st, SHOOT, 0.6, SHOOT, 0.6)
    rc.record(st, SHOOT, 0.6, Action(ActionKind.SHOOT_SELF), 0.4)
    assert rc.summary() == (1, 2)
    rows = rc.rows()
    assert rows[0].followed and not rows[1].followed
    assert rows[1].recommended == "SHOOT DEALER" and rows[1].taken == "SHOOT SELF"


# -- item 15(a): the chance a move itself kills the player ---------------------


def test_death_chance_shoot_self_and_dealer():
    s = _state(2, 3, hp=(1, 3))
    assert analysis.death_chance(s, Action(ActionKind.SHOOT_SELF), RULES) == pytest.approx(0.4)
    assert analysis.death_chance(s, Action(ActionKind.SHOOT_OPPONENT), RULES) == 0.0


def test_death_chance_sawed_known_live():
    chamber = ChamberState.create((Shell.LIVE, None, None), 1, 1)
    s = GameState.create(chamber, (2, 3), 4, Player.PLAYER, None, True)
    assert analysis.death_chance(s, Action(ActionKind.SHOOT_SELF), RULES) == pytest.approx(1.0)


def test_death_chance_expired_medicine_at_one_hp():
    chamber = ChamberState.create((None, None), 1, 1)
    items = (ItemCounts.of(expired_medicine=1), ItemCounts.empty())
    s = GameState.create(chamber, (1, 3), 4, Player.PLAYER, items)
    act = Action(ActionKind.USE, Item.EXPIRED_MEDICINE)
    assert analysis.death_chance(s, act, RULES) == pytest.approx(0.5)


def test_ranked_alternatives_carry_dies():
    s = _state(2, 3, hp=(1, 3))
    alts = analysis.ranked_alternatives(_solver().evaluate(s), Player.PLAYER, s, RULES)
    by = {a.label: a.dies for a in alts}
    assert by["SHOOT SELF"] == pytest.approx(0.4) and by["SHOOT DEALER"] == 0.0
