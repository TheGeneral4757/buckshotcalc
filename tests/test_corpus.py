"""Tests for buckshotcalc.sim.corpus (SPEC.md §12.2, M6 acceptance)."""

from __future__ import annotations

import pytest

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
)
from buckshotcalc.sim.corpus import (
    AGREEMENT_THRESHOLD,
    CorpusEntry,
    Provenance,
    _corpus_rules,
    _health_ratio_reload_table,
    agreement_rate,
    check,
    corpus,
    run_corpus,
)

RULES = _corpus_rules(default_rules())


def _stub_solver(rules=RULES, mode: GameMode = GameMode.BASE) -> Solver:
    solver = Solver(rules, mode)
    solver._reload_table = _health_ratio_reload_table(rules)
    return solver


ENTRIES = corpus(RULES)
RESULTS = run_corpus(RULES)
BY_NAME = {e.name: e for e in ENTRIES}


# ---------------------------------------------------------------------------
# Corpus hygiene
# ---------------------------------------------------------------------------


def test_entry_names_are_unique():
    names = [e.name for e in ENTRIES]
    assert len(names) == len(set(names))


def test_every_entry_has_a_rationale():
    for e in ENTRIES:
        assert e.rationale.strip()


def test_every_entry_has_an_expected_action_or_value():
    for e in ENTRIES:
        assert e.expected_action is not None or e.expected_value is not None


def test_corpus_not_empty():
    assert len(ENTRIES) > 0


# ---------------------------------------------------------------------------
# Mandatory entries (M6 brief) are present, found by name.
# ---------------------------------------------------------------------------


def test_handcuffs_exploit_entry_present():
    assert "handcuffs_more_blanks_than_lives" in BY_NAME
    e = BY_NAME["handcuffs_more_blanks_than_lives"]
    assert e.provenance is Provenance.SPEC_EXPLOIT


def test_wasted_info_item_entry_present():
    assert "wasted_magnifying_glass_fully_determined_chamber" in BY_NAME


def test_handsaw_decisive_entries_present():
    assert "handsaw_decisive_converts_to_lethal" in BY_NAME
    assert "handsaw_not_decisive_already_lethal" in BY_NAME


def test_dealer_knowledge_entry_present():
    assert "dealer_glassed_slot_belief_weighted_mixture" in BY_NAME


# ---------------------------------------------------------------------------
# Agreement rate (SPEC.md §12.2 acceptance)
# ---------------------------------------------------------------------------


def test_agreement_rate_meets_threshold():
    rate = agreement_rate(RESULTS)
    assert rate >= AGREEMENT_THRESHOLD, [
        (r.entry.name, r.detail) for r in RESULTS if not r.agreed
    ]


def test_every_scored_result_agrees():
    """Every disagreement must be investigated by hand (SPEC.md §12.2) — for
    this corpus, that investigation happened before the entries were written
    (see corpus.py's docstrings and the M6 report), so a green run here means
    no *undocumented* disagreement slipped in.
    """
    disagreements = [
        (r.entry.name, r.detail)
        for r in RESULTS
        if r.entry.provenance is not Provenance.SOLVER_DERIVED and not r.agreed
    ]
    assert disagreements == []


def test_solver_derived_entries_excluded_from_agreement_rate():
    """The property that keeps the metric honest: a padded corpus of
    solver-echoing entries must not be able to inflate the rate. Synthetic
    check — a SOLVER_DERIVED entry marked disagreed must not move the rate.
    """
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber, hp=(3, 2), max_hp=3, turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )
    real_entry = CorpusEntry(
        name="__synthetic_scored__",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.RULE_FORCED,
        rationale="synthetic: a guaranteed-live single shell must be shot at the Dealer.",
        expected_action=Action(ActionKind.SHOOT_OPPONENT),
    )
    fake_pin = CorpusEntry(
        name="__synthetic_pin__",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.SOLVER_DERIVED,
        rationale="synthetic pin, deliberately wrong to prove it's excluded.",
        expected_action=Action(ActionKind.SHOOT_SELF),  # deliberately wrong
    )
    solver = _stub_solver()
    real_result = check(real_entry, solver, RULES)
    pin_result = check(fake_pin, solver, RULES)
    assert real_result.agreed is True
    assert pin_result.agreed is False

    # The wrong pin must not lower the rate: scoring [real_result] alone
    # and [real_result, pin_result] together must agree exactly.
    assert agreement_rate([real_result]) == 1.0
    assert agreement_rate([real_result, pin_result]) == 1.0


def test_agreement_rate_raises_with_no_scored_results():
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber, hp=(3, 2), max_hp=3, turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )
    fake_pin = CorpusEntry(
        name="__only_pin__",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.SOLVER_DERIVED,
        rationale="synthetic",
        expected_action=Action(ActionKind.SHOOT_OPPONENT),
    )
    solver = _stub_solver()
    with pytest.raises(ValueError):
        agreement_rate([check(fake_pin, solver, RULES)])


def test_solver_derived_count_reported():
    """The report must state how many SOLVER_DERIVED entries exist (M6
    brief) — this pins the actual count so the report can't silently drift
    from the corpus.
    """
    pins = [e for e in ENTRIES if e.provenance is Provenance.SOLVER_DERIVED]
    scored = [e for e in ENTRIES if e.provenance is not Provenance.SOLVER_DERIVED]
    assert len(pins) == 2
    assert len(scored) == 7


# ---------------------------------------------------------------------------
# Entry construction validation (CorpusEntry.__post_init__)
# ---------------------------------------------------------------------------


def test_entry_rejects_empty_rationale():
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber, hp=(3, 2), max_hp=3, turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )
    with pytest.raises(ValueError):
        CorpusEntry(
            name="bad",
            state=state,
            mode=GameMode.BASE,
            provenance=Provenance.RULE_FORCED,
            rationale="   ",
            expected_action=Action(ActionKind.SHOOT_OPPONENT),
        )


def test_entry_rejects_no_expectation():
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber, hp=(3, 2), max_hp=3, turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )
    with pytest.raises(ValueError):
        CorpusEntry(
            name="bad",
            state=state,
            mode=GameMode.BASE,
            provenance=Provenance.RULE_FORCED,
            rationale="has rationale but no expectation",
        )


def test_entry_rejects_expected_action_on_dealer_turn():
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber, hp=(3, 2), max_hp=3, turn=Player.DEALER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )
    with pytest.raises(ValueError):
        CorpusEntry(
            name="bad",
            state=state,
            mode=GameMode.BASE,
            provenance=Provenance.RULE_FORCED,
            rationale="Dealer-turn state cannot carry an expected action.",
            expected_action=Action(ActionKind.SHOOT_OPPONENT),
        )


# ---------------------------------------------------------------------------
# Sharper-form assertions the M6 brief calls for beyond plain agreement.
# ---------------------------------------------------------------------------


def test_handcuffs_tie_not_strictly_dominated():
    """M6 brief's sharper form: assert value(cuff line) <= value(best line)
    directly from evaluate()'s own children — not pinned to a magic number,
    so this is not circular. Also pins which shape the exploit takes here: an
    exact tie, not a strict loss.
    """
    entry = BY_NAME["handcuffs_more_blanks_than_lives"]
    solver = _stub_solver()
    result = solver.evaluate(entry.state)
    by_item = {c.action.item: c.value for c in result.children}
    best = max(c.value for c in result.children)
    cuffs_value = by_item[Item.HANDCUFFS]
    assert cuffs_value <= best + 1e-9
    # Measured shape: an exact tie, not a strict loss (see corpus.py's
    # rationale) — pin that too, since it's the point M4's review made.
    assert cuffs_value == pytest.approx(best)


def test_magnifying_glass_marked_wasted_when_fully_determined():
    """M6 brief: 'assert USE MAGNIFYING_GLASS is marked wasted in evaluate's
    children.' Requires the cap-tightened `RULES` (see corpus._corpus_rules)
    — under the shipped cap of 6 this can never happen (a genuinely no-op
    item weakly dominates skipping it), which is itself documented as a
    judgment call in the M6 report.
    """
    entry = BY_NAME["wasted_magnifying_glass_fully_determined_chamber"]
    solver = _stub_solver()
    result = solver.evaluate(entry.state)
    by_item = {c.action.item: c for c in result.children}
    assert by_item[Item.MAGNIFYING_GLASS].wasted is True
    assert by_item[Item.HANDCUFFS].wasted is False


def test_handsaw_decisive_is_top_and_not_wasted():
    entry = BY_NAME["handsaw_decisive_converts_to_lethal"]
    solver = _stub_solver()
    result = solver.evaluate(entry.state)
    top = result.children[0]
    assert top.action == Action(ActionKind.USE, Item.HANDSAW)
    assert top.wasted is False
    assert top.value == pytest.approx(1.0)


def test_handsaw_not_decisive_ties_and_is_not_required():
    """The saw is never *required* here: it ties the best line exactly, and
    a non-saw action is at least as good — sawing bought nothing because the
    shot was already lethal without it.
    """
    entry = BY_NAME["handsaw_not_decisive_already_lethal"]
    solver = _stub_solver()
    result = solver.evaluate(entry.state)
    by_item = {c.action.item: c for c in result.children}
    saw_child = by_item[Item.HANDSAW]
    non_saw = [c for c in result.children if c.action.item is not Item.HANDSAW]
    assert non_saw
    assert max(c.value for c in non_saw) >= saw_child.value - 1e-9
    assert saw_child.wasted is False  # a tie, not a strict loss


def test_dealer_knowledge_entry_has_no_children():
    """Dealer-turn node: SearchResult.children is empty (solver.py's pinned
    contract) — the corresponding CheckResult.actual_action must be None,
    not a fabricated Action.
    """
    entry = BY_NAME["dealer_glassed_slot_belief_weighted_mixture"]
    solver = _stub_solver()
    result = solver.evaluate(entry.state)
    assert result.children == ()
    check_result = check(entry, solver, RULES)
    assert check_result.actual_action is None
    assert check_result.actual_value == pytest.approx(0.325)


def test_beer_direction_is_position_dependent_not_uniformly_helpful():
    """Documents the finding (M6 report): SPEC.md §3.3's 'usually helps' does
    not hold in the tied-count matched pair, and does hold in the
    majority-live matched pair. Both directions are asserted here directly
    from the corpus's own pinned values, so a future change that silently
    flips either direction is caught.
    """
    no_beer_tied = BY_NAME["beer_dealer_hands_tied_count_no_beer"].expected_value
    with_beer_tied = BY_NAME["beer_dealer_hands_tied_count_with_beer"].expected_value
    assert with_beer_tied < no_beer_tied  # beer HURTS here

    no_beer_majority = BY_NAME["beer_dealer_hands_majority_live_no_beer"].expected_value
    with_beer_majority = BY_NAME["beer_dealer_hands_majority_live_with_beer"].expected_value
    assert with_beer_majority > no_beer_majority  # beer HELPS here
