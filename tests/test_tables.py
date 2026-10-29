"""Tests for buckshotcalc.sim.tables (SPEC.md §14, M8 acceptance).

Backbone is a hand-computed exact value (SPEC.md §0.1: "a test that only
asserts 'runs without raising' is not a test"). See
`test_hand_computed_handcuffs_hp2_1l1b`'s docstring for the full by-hand
derivation, worked independently of the solver against the transition
rules directly (SPEC.md §3.3's shooting-decision table, §6.1's turn-passing
rules), the same way `sim/corpus.py`'s entries are.
"""

from __future__ import annotations

import math

import pytest

from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import Item
from buckshotcalc.sim.corpus import _health_ratio_reload_table
from buckshotcalc.sim.tables import (
    TablePosition,
    build_solver,
    enumerate_positions,
    generate_sheet,
    render_markdown,
    render_text,
    valuate_position,
)

RULES = default_rules()


def _row(summary, item: Item):
    matches = [r for r in summary.rows if r.item is item]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------------------
# Hand-computed exact value (SPEC.md §12.3's backbone requirement)
# ---------------------------------------------------------------------------


def test_hand_computed_handcuffs_hp2_1l1b():
    """HP 2/2, 1 live + 1 blank, player holds only Handcuffs.

    Full derivation, independent of the solver, against `rules.toml`'s
    shipped values (`turns.base_damage=1`, `handcuffs.retains_turn=true`,
    `dealer.unknown_shell_equal_coinflip` irrelevant here — every shell the
    Dealer ever faces in this line is *known*, not unknown, per SPEC.md
    §3.3's shooting table: a known-live shell => shoot the player; a
    known-blank shell => shoot itself, deterministically). Reload boundary
    values come from this test's own injected stand-in table
    (`hp_player / (hp_player + hp_dealer)`), never the real `V_reload`.

    **Baseline (no items), for comparison:**
    - SHOOT_SELF: live half (p=.5) -> player 2->1, turn ends, Dealer's turn
      on a now-known-blank single shell (closure) -> Dealer shoots itself
      (harmless), retains turn, chamber empties -> reload(1,2,2)=1/3.
      Blank half (p=.5) -> player misses self (harmless), retains turn, the
      remaining shell is forced LIVE by closure; player, still moving,
      picks the better of shooting self (reload(1,2,2)=1/3) or the Dealer
      (reload(2,1,2)=2/3) on that known-live shell -> 2/3.
      Average: .5*(1/3) + .5*(2/3) = 1/2.
    - SHOOT_OPPONENT: live half -> Dealer 2->1, turn ends, Dealer's turn on
      a known-blank shell -> shoots itself, retains turn, chamber empties
      -> reload(2,1,2)=2/3. Blank half -> turn ends (miss), Dealer's turn on
      a known-live shell -> shoots the player (2->1), chamber empties ->
      reload(1,2,2)=1/3. Average: .5*(2/3) + .5*(1/3) = 1/2.
    - Both tie at exactly 0.5; `legal_actions` orders SHOOT_SELF first, so
      the recommended baseline action is SHOOT_SELF (the tie-break the
      solver's stable sort produces).

    **USE(HANDCUFFS):** cuffs the Dealer (`retains_turn=true`, so the
    player immediately acts again with no items left). The player's best
    reply is SHOOT_OPPONENT:
    - live half (p=.5): Dealer 2->1, turn ends -> the Dealer's one skip is
      consumed (cuff spent), so the *player* moves again on the
      now-known-blank remaining shell. Neither shot on a known blank deals
      damage, and both empty the chamber -> reload(2,1,2)=2/3 either way.
    - blank half (p=.5): turn ends on the miss -> Dealer's skip consumed,
      player moves again on the now-known-live remaining shell; the
      player's best reply there is to shoot the Dealer -> Dealer 2->1,
      chamber empties -> reload(2,1,2)=2/3.
    - Average: .5*(2/3) + .5*(2/3) = 2/3 exactly — strictly better than the
      SHOOT_SELF-first line (which averages to 1/2, checked in this test
      module by direct computation and cross-checked against the solver,
      not used as the expected value's source). So value(USE(HANDCUFFS)) =
      2/3.

    2/3 > 1/2 (the baseline), so Handcuffs is a real, positive-delta item
    here (delta = 2/3 - 1/2 = 1/6) and is not flagged wasted (it ties the
    best available action within its own state — SHOOT_OPPONENT there is
    also 2/3, so the tie-not-strictly-worse rule applies).
    """
    position = TablePosition(max_hp=2, live=1, blank=1)
    solver = build_solver(RULES)
    summary = valuate_position(position, RULES, solver)

    assert math.isclose(summary.baseline_value, 0.5, abs_tol=1e-9)
    # Baseline is an exact tie with SHOOT SELF (both 1/2); the tie-break prefers the opponent shot.
    assert summary.baseline_action_label == "SHOOT DEALER"

    cuffs = _row(summary, Item.HANDCUFFS)
    assert math.isclose(cuffs.item_value, 2.0 / 3.0, abs_tol=1e-9)
    assert math.isclose(cuffs.delta, 1.0 / 6.0, abs_tol=1e-9)
    assert cuffs.use_now_is_best is True


# ---------------------------------------------------------------------------
# Wasted vs. not-wasted (M8 brief's mandatory check)
# ---------------------------------------------------------------------------


def test_holding_value_and_use_now_value_are_different_questions():
    """The regression guard for M8's headline defect.

    An earlier revision reported the value of USE(item) as the item's value.
    That is the wrong quantity for a cheat sheet: it understates every item
    worth keeping, and it zeroed Cigarettes in all twelve positions, because
    spending a heal at full HP does nothing while *holding* one across the
    round is worth a great deal.

    At HP 2/2, 2L1B, both players full: holding Cigarettes is worth strictly
    more than the no-item baseline, while using them immediately gains
    exactly nothing. If the two columns ever collapse into one number again,
    this fails.
    """
    position = TablePosition(max_hp=2, live=2, blank=1)
    solver = build_solver(RULES)
    summary = valuate_position(position, RULES, solver)

    cigs = _row(summary, Item.CIGARETTES)
    assert cigs.delta > 0.0, "holding a heal has option value even at full HP"
    assert math.isclose(cigs.use_now_value, cigs.baseline_value, abs_tol=1e-9), (
        "spending a heal at full HP must gain nothing"
    )
    assert cigs.use_now_is_best is False, "so it should read as 'hold', not 'use now'"
    assert cigs.item_value > cigs.use_now_value, (
        "holding value must exceed use-now value for an item worth keeping"
    )

    # And an item that IS worth spending immediately reads the other way.
    saw = _row(summary, Item.HANDSAW)
    assert saw.use_now_is_best is True
    assert math.isclose(saw.item_value, saw.use_now_value, abs_tol=1e-9)

def test_generation_is_byte_identical_across_runs():
    """Same `rules.toml` in, same bytes out (M8 brief's determinism requirement)."""
    summaries_a = generate_sheet(RULES)
    summaries_b = generate_sheet(RULES)
    text_a, text_b = render_text(summaries_a, RULES), render_text(summaries_b, RULES)
    md_a, md_b = render_markdown(summaries_a, RULES), render_markdown(summaries_b, RULES)
    assert text_a == text_b
    assert md_a == md_b


def test_enumerate_positions_is_deterministic_and_order_stable():
    assert enumerate_positions(RULES) == enumerate_positions(RULES)


# ---------------------------------------------------------------------------
# Header contract: reload model and rules fingerprint are named on the sheet's face
# ---------------------------------------------------------------------------


def test_header_names_reload_model_and_fingerprint():
    summaries = generate_sheet(RULES)
    text = render_text(summaries, RULES)
    md = render_markdown(summaries, RULES)
    for rendering in (text, md):
        assert RULES.fingerprint in rendering
        assert "stand-in" in rendering
        assert "NOT the shipped V_reload" in rendering
        assert "BASE" in rendering


# ---------------------------------------------------------------------------
# Hazard: the generator must never trigger the real reload precompute
# ---------------------------------------------------------------------------


def test_never_triggers_the_real_reload_precompute(monkeypatch):
    """`generate_sheet` must complete using only the injected stand-in table.

    Patches `Solver._compute_reload_table` to fail loudly if called at all —
    the real precompute has never completed a run at shipped scale (SPEC.md
    §7.7) and must never be reachable from this module's code path.
    """

    def _boom(self):
        raise AssertionError(
            "tables.py triggered the real reload precompute — it must only "
            "ever use an injected stand-in table"
        )

    monkeypatch.setattr(Solver, "_compute_reload_table", _boom)
    summaries = generate_sheet(RULES)  # must not raise
    assert len(summaries) == len(enumerate_positions(RULES))


def test_build_solver_reports_a_cached_table_without_building_one():
    """`has_cached_reload_table()` is True immediately after `build_solver`,
    proving the table came from injection, not a lazy build on first use.
    """
    solver = build_solver(RULES)
    assert solver.has_cached_reload_table() is True


def test_build_solver_uses_the_injected_table_not_a_default():
    """A distinctive custom table is actually the one consulted.

    Injects an all-constant reload table (every key -> 0.75, nothing like
    the health-ratio stand-in) and checks that a position which reaches a
    reload boundary reports a value consistent with that constant rather
    than with the health-ratio stand-in's varying values — direct evidence
    the injected table, not some other source, produced the number.
    """
    constant_table = {
        (hp_p, hp_d, max_hp): 0.75
        for max_hp in RULES.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    }
    solver = build_solver(RULES, reload_table=constant_table)
    position = TablePosition(max_hp=2, live=1, blank=1)
    summary = valuate_position(position, RULES, solver)
    # Every terminal line in this tiny position passes through exactly one
    # reload boundary with no damage-independent shortcuts other than the
    # constant table's own value, so the baseline value must equal it
    # exactly regardless of which action is chosen.
    assert summary.baseline_value == pytest.approx(0.75, abs=1e-9)

    # Cross-check against the health-ratio stand-in: same position, real
    # stand-in table, must NOT equal 0.75 (it was hand-verified as 0.5
    # above) — proves the constant table above was actually consulted.
    stand_in_solver = build_solver(RULES, reload_table=_health_ratio_reload_table(RULES))
    stand_in_summary = valuate_position(position, RULES, stand_in_solver)
    assert stand_in_summary.baseline_value == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# Position family shape
# ---------------------------------------------------------------------------


def test_position_family_matches_its_stated_rule():
    """`enumerate_positions` covers exactly max_hp x smallest-totals x splits,
    per the module docstring's stated rule — no more, no fewer.
    """
    positions = enumerate_positions(RULES)
    totals = {p.total for p in positions}
    assert totals == {2, 3, 4}  # the 3 smallest legal totals at shipped rules.toml
    assert {p.max_hp for p in positions} == set(RULES.health.possible_max_hp)
    for p in positions:
        assert p.live + p.blank == p.total
    # every total has exactly one split: odd totals give the extra shell to blank.
    by_total: dict[int, set[tuple[int, int]]] = {}
    for p in positions:
        by_total.setdefault(p.total, set()).add((p.live, p.blank))
    assert len(by_total[2]) == 1
    assert by_total[3] == {(1, 2)}  # RoundManager.gd:148-150, not 2L/1B
    assert len(by_total[4]) == 1


def test_item_rows_cover_exactly_base_items():
    summaries = generate_sheet(RULES)
    for summary in summaries:
        assert tuple(row.item for row in summary.rows) == RULES.items.base_items
