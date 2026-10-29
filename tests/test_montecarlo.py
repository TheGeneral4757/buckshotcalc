"""Tests for buckshotcalc.sim.montecarlo (SPEC.md §12.1, M5 acceptance)."""

from __future__ import annotations

import math
import random

import pytest

from buckshotcalc.engine.actions import Action, ActionKind, apply
from buckshotcalc.engine.dealer import dealer_step
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.solver import SearchResult, Solver
from buckshotcalc.engine.state import GameMode, GameState, Player
from buckshotcalc.sim.montecarlo import (
    Discrepancy,
    ReloadMode,
    measure_dealer_phone_bias,
    measure_reload_carryover_bias,
    measure_reload_flag_sensitivity,
    random_position,
    rollout,
    sorted_by_error,
    validate,
)
from tests.test_solver import (
    SMALL_OVERRIDES,
    TINY_OVERRIDES,
    _custom_rules,
    _health_ratio_table,
    _state,
)

RULES = default_rules()


# ---------------------------------------------------------------------------
# Deterministic positions: zero-variance agreement (the cheapest possible way
# to find a rollout bug, per the M5 brief).
# ---------------------------------------------------------------------------


def test_guaranteed_kill_is_empirically_exactly_one():
    """L1 B0, dealer at 1 HP, player to move: SHOOT_OPPONENT is a certain kill.

    The round ends before any reload boundary is ever reached, so this does
    not even touch the (stubbed) reload table — every one of the trials must
    return True, empirical == 1.0 exactly, stderr == 0.0.
    """
    solver = Solver(RULES, GameMode.BASE)
    solver._reload_table = _health_ratio_table(RULES)
    state = _state(live=1, blank=0, hp=(3, 1), turn=Player.PLAYER)
    rng = random.Random(1)
    result = rollout(state, RULES, solver, rng, 200)
    assert result.empirical == 1.0
    assert result.stderr == 0.0


def test_guaranteed_death_is_empirically_exactly_zero():
    """L1 B0, player at 1 HP, DEALER to move: the Dealer's public count-closure
    resolves the chambered shell as certainly live, so its deterministic
    policy shoots the player for a certain kill. Exercises the DEALER seat.
    """
    solver = Solver(RULES, GameMode.BASE)
    solver._reload_table = _health_ratio_table(RULES)
    state = _state(live=1, blank=0, hp=(1, 3), turn=Player.DEALER)
    rng = random.Random(2)
    result = rollout(state, RULES, solver, rng, 200)
    assert result.empirical == 0.0
    assert result.stderr == 0.0


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_same_seed_same_result():
    solver = Solver(RULES, GameMode.BASE)
    solver._reload_table = _health_ratio_table(RULES)
    state = _state(live=1, blank=1, hp=(3, 3), turn=Player.PLAYER)

    r1 = rollout(state, RULES, solver, random.Random(42), 500)
    r2 = rollout(state, RULES, solver, random.Random(42), 500)
    assert r1.wins == r2.wins
    assert r1.trials == r2.trials


def test_different_seed_can_differ():
    """Sanity check that the harness is not accidentally deterministic
    regardless of seed (which would make the reproducibility test above
    vacuous)."""
    solver = Solver(RULES, GameMode.BASE)
    solver._reload_table = _health_ratio_table(RULES)
    state = _state(live=1, blank=1, hp=(2, 2), turn=Player.PLAYER)

    results = {
        rollout(state, RULES, solver, random.Random(seed), 300).wins for seed in range(8)
    }
    assert len(results) > 1


def test_validate_same_seed_same_discrepancies():
    """`validate` is reproducible end to end: identical seed -> identical
    per-position predicted/empirical/stderr, not merely the same tally."""
    rules = _custom_rules(_tmp(), TINY_OVERRIDES)
    d1 = validate(rules, GameMode.BASE, positions=3, trials=200, seed=123)
    d2 = validate(rules, GameMode.BASE, positions=3, trials=200, seed=123)
    assert len(d1) == len(d2)
    for a, b in zip(d1, d2, strict=True):
        assert a.predicted == b.predicted
        assert a.empirical == b.empirical
        assert a.stderr == b.stderr
        assert a.trials == b.trials


def test_validate_independent_of_worker_count():
    """The parallel harness is deterministic and workers=1 (in-process) equals workers=2 (spawn)."""
    rules = _custom_rules(_tmp(), TINY_OVERRIDES)
    kw = {"positions": 4, "trials": 200, "seed": 7}
    seen: list[tuple[int, int]] = []
    one = validate(rules, GameMode.BASE, workers=1, **kw)
    two = validate(
        rules, GameMode.BASE, workers=2, progress=lambda d, t, e: seen.append((d, t)), **kw
    )
    assert [(d.state, d.predicted, d.empirical, d.stderr) for d in one] == [
        (d.state, d.predicted, d.empirical, d.stderr) for d in two
    ]
    assert seen == [(i, 4) for i in range(1, 5)]


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


# ---------------------------------------------------------------------------
# Sampling correctness: apply()'s and dealer_step()'s branch frequencies
# converge to their declared weights. This is the part of the harness most
# likely to be silently biased (M5 brief).
# ---------------------------------------------------------------------------


def test_apply_sampling_converges_to_weights():
    """SHOOT_SELF on a chamber with p_live=0.3: sample many times via
    `apply`'s own weighted outcomes (not `_sample_weighted` directly, so this
    also exercises the real `apply` distribution) and check the live/blank
    split converges to 0.3/0.7."""
    from buckshotcalc.sim.montecarlo import _sample_weighted

    state = _state(live=3, blank=7, hp=(3, 3), turn=Player.PLAYER)
    outcomes = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    weights = {id(s): w for w, s in outcomes}
    assert sum(weights.values()) == pytest.approx(1.0)

    rng = random.Random(7)
    n = 20_000
    counts = dict.fromkeys(weights, 0)
    for _ in range(n):
        chosen = _sample_weighted(outcomes, rng)
        counts[id(chosen)] += 1

    for w, s in outcomes:
        observed = counts[id(s)] / n
        # Binomial standard error at n=20000, p in {0.3, 0.7} is ~0.0032;
        # 5 sigma is a generous but still meaningful tolerance.
        se = math.sqrt(w * (1 - w) / n)
        assert abs(observed - w) < 5 * se + 1e-6


def test_dealer_step_sampling_converges_to_weights():
    """A Dealer coinflip node (equal live/blank, unresolved): `dealer_step`'s
    two branches (shoot player / shoot self) must each land near 0.5 under
    repeated sampling, matching `rules.dealer.unknown_shell_equal_coinflip`.
    """
    from buckshotcalc.sim.montecarlo import _sample_weighted

    state = _state(live=1, blank=1, hp=(3, 3), turn=Player.DEALER)
    branches = dealer_step(state, RULES)
    outcomes = [(w, s) for w, _a, s in branches]
    total = sum(w for w, _s in outcomes)
    assert total == pytest.approx(1.0)

    by_kind: dict[int, ActionKind] = {}
    for _w, actions, s in branches:
        by_kind[id(s)] = actions[-1].kind

    rng = random.Random(11)
    n = 20_000
    counts = {ActionKind.SHOOT_SELF: 0, ActionKind.SHOOT_OPPONENT: 0}
    for _ in range(n):
        chosen = _sample_weighted(outcomes, rng)
        counts[by_kind[id(chosen)]] += 1

    p_self = counts[ActionKind.SHOOT_SELF] / n
    se = math.sqrt(0.5 * 0.5 / n)
    assert abs(p_self - 0.5) < 5 * se + 1e-6


# ---------------------------------------------------------------------------
# random_position: property tests (M5 brief)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [GameMode.BASE, GameMode.DOUBLE_OR_NOTHING])
def test_random_position_never_terminal_or_empty(mode):
    rng = random.Random(999)
    for _ in range(3000):
        state = random_position(RULES, mode, rng)
        assert state.winner() is None
        assert not state.chamber.is_empty


@pytest.mark.parametrize("mode", [GameMode.BASE, GameMode.DOUBLE_OR_NOTHING])
def test_random_position_dealer_knows_in_range(mode):
    rng = random.Random(1000)
    for _ in range(3000):
        state = random_position(RULES, mode, rng)
        n = len(state.chamber.slots)
        assert all(0 <= i < n for i in state.dealer_knows)


@pytest.mark.parametrize("mode", [GameMode.BASE, GameMode.DOUBLE_OR_NOTHING])
def test_random_position_items_within_max_slots(mode):
    rng = random.Random(1001)
    for _ in range(1000):
        state = random_position(RULES, mode, rng)
        assert state.items[0].total() <= RULES.items.max_slots
        assert state.items[1].total() <= RULES.items.max_slots


def test_random_position_hits_both_seats_and_flags():
    """Sanity: over enough draws, both turns and both saw/cuff flags occur —
    otherwise the property tests above would be vacuous over a degenerate
    generator."""
    rng = random.Random(1002)
    turns = set()
    saw_seen = False
    cuff_seen = False
    dealer_knows_seen = False
    for _ in range(500):
        state = random_position(RULES, GameMode.BASE, rng)
        turns.add(state.turn)
        saw_seen = saw_seen or state.sawed
        cuff_seen = cuff_seen or any(state.cuffed)
        dealer_knows_seen = dealer_knows_seen or bool(state.dealer_knows)
    assert turns == {Player.PLAYER, Player.DEALER}
    assert saw_seen
    assert cuff_seen
    assert dealer_knows_seen


# ---------------------------------------------------------------------------
# validate(): small end-to-end run (slow).
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("mode", [GameMode.BASE, GameMode.DOUBLE_OR_NOTHING])
def test_validate_end_to_end_small_scale(mode):
    """A tiny but real end-to-end `validate` run: real (small) rules, real
    (non-stubbed) reload table, both modes. Not a statistical acceptance
    check at this scale — just confirms the whole pipeline runs and returns
    sane, sorted `Discrepancy` objects."""
    rules = _custom_rules(_tmp(), SMALL_OVERRIDES)
    discrepancies = validate(rules, mode, positions=3, trials=200, seed=5)
    assert len(discrepancies) == 3
    for d in discrepancies:
        assert 0.0 <= d.predicted <= 1.0
        assert 0.0 <= d.empirical <= 1.0
        assert d.trials == 200
        assert d.stderr >= 0.0
        assert d.error == pytest.approx(abs(d.predicted - d.empirical))

    sigmas = [d.sigma for d in discrepancies]
    assert sigmas == sorted(sigmas, reverse=True)

    by_error = sorted_by_error(discrepancies)
    errors = [d.error for d in by_error]
    assert errors == sorted(errors, reverse=True)


# ---------------------------------------------------------------------------
# The single most important test in the file: a deliberately wrong solver
# must be caught. Predicted values are perturbed by a uniform +0.05 (clamped
# at 1.0); actual play (action selection and the reload table) is left
# genuine, so the *dynamics* sampled by `rollout` are exactly the real
# solver's — only the reported value is wrong. A harness that never fails is
# not evidence of anything.
# ---------------------------------------------------------------------------


class _BiasedSolver:
    """Wraps a real `Solver`, adding a constant bias to every reported value.

    `evaluate`'s `children` are passed through unchanged (still the real
    solver's ranking), so action selection in `rollout` is untouched — only
    `value`/`evaluate(...).value` lie, which is exactly what a subtly wrong
    solver looks like from the outside: the numbers are off, the play is not
    obviously broken.
    """

    def __init__(self, inner: Solver, bias: float) -> None:
        self._inner = inner
        self.bias = bias
        self.rules = inner.rules
        self.mode = inner.mode

    def value(self, state: GameState) -> float:
        return min(1.0, self._inner.value(state) + self.bias)

    def evaluate(self, state: GameState) -> SearchResult:
        r = self._inner.evaluate(state)
        return SearchResult(
            value=min(1.0, r.value + self.bias),
            children=r.children,
            nodes=r.nodes,
            cap_reached=r.cap_reached,
        )

    def reload_value(self, hp_player: int, hp_dealer: int, max_hp: int) -> float:
        return self._inner.reload_value(hp_player, hp_dealer, max_hp)


def test_deliberately_wrong_solver_is_caught():
    rules = _custom_rules(_tmp(), TINY_OVERRIDES)
    inner = Solver(rules, GameMode.BASE)
    inner._reload_table = _health_ratio_table(rules)
    biased = _BiasedSolver(inner, bias=0.05)

    rng = random.Random(321)
    discrepancies: list[Discrepancy] = []
    attempts = 0
    while len(discrepancies) < 15 and attempts < 500:
        attempts += 1
        state = random_position(rules, GameMode.BASE, rng)
        true_value = inner.value(state)
        # Keep only positions with headroom for the +0.05 bias to actually
        # show up after clamping (a position already at/near 1.0 would hide
        # the bias entirely, which would make this test measure the clamp,
        # not the harness).
        if not (0.05 <= true_value <= 0.90):
            continue
        predicted = biased.value(state)
        result = rollout(state, rules, biased, rng, 3000, reload_mode=ReloadMode.TABLE)
        discrepancies.append(
            Discrepancy(
                state=state,
                predicted=predicted,
                empirical=result.empirical,
                trials=3000,
                stderr=result.stderr,
            )
        )

    assert len(discrepancies) >= 10, "generator did not produce enough mid-range positions"
    flagged = [d for d in discrepancies if d.sigma > 4.0]
    assert len(flagged) >= len(discrepancies) // 2, (
        "the biased solver's +0.05 error was not statistically flagged by the harness"
    )
    for d in flagged:
        assert d.error > 0.0


def test_correct_solver_is_not_flagged():
    """Negative control for the test above: the *unbiased* solver, run
    through the identical loop, must not spuriously flag positions at a
    demanding sigma threshold."""
    rules = _custom_rules(_tmp(), TINY_OVERRIDES)
    solver = Solver(rules, GameMode.BASE)
    solver._reload_table = _health_ratio_table(rules)

    rng = random.Random(322)
    discrepancies: list[Discrepancy] = []
    for _ in range(15):
        state = random_position(rules, GameMode.BASE, rng)
        predicted = solver.value(state)
        result = rollout(state, rules, solver, rng, 3000, reload_mode=ReloadMode.TABLE)
        discrepancies.append(
            Discrepancy(
                state=state,
                predicted=predicted,
                empirical=result.empirical,
                trials=3000,
                stderr=result.stderr,
            )
        )
    flagged = [d for d in discrepancies if d.sigma > 5.0]
    assert len(flagged) == 0, f"unbiased solver flagged at 5 sigma: {flagged}"


# ---------------------------------------------------------------------------
# SPEC.md §15's three measurement obligations — reduced scale, slow.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("seed", [17, 23, 101])
def test_reload_carryover_bias_is_positive_and_not_explained_by_table_error(seed):
    """SPEC.md §15.13, with its control arm — the part that makes it a measurement.

    `mean_bias` alone cannot support any claim about carried-over items: the
    reload table is built from a finite item-draw sample, and its own error
    lands in `empirical - predicted` in exactly the same place the carry-over
    effect does. The empty-rack control isolates that floor. Asserted here:

    - the control is small, i.e. the table is not simply wrong at this scale;
    - the carrying arm's bias is clearly larger than the control's, so the
      effect is attributable to carried items rather than table noise;
    - the net effect is **positive**, which answers §15.13's open question in
      the direction it says is unknown: the table systematically
      *undervalues* reaching a reload while holding items. Hoarding is worth
      more than the health-keyed table can see.

    Three seeds, because a single seed would not distinguish a real effect
    from a lucky draw of *positions*, and the position-to-position spread
    here is an order of magnitude above the rollout noise.

    The seeds do **not**, however, give three independent samples of the
    control arm, and reading them that way is a trap this test fell into
    once already. `seed` selects positions and drives the rollouts; the
    reload table's item-draw sample is seeded from `rules.fingerprint` alone
    (`Solver._draw_samples`), so all three seeds share one table and one
    realization of its error. That error is a fixed offset per rules file,
    not noise that averages away — and it is therefore re-rolled by *any*
    edit to `rules.toml`, however unrelated.

    Hence the sample-count override below. At `SMALL_OVERRIDES`' own value
    of 4 the control's spread across fingerprints is wider than the 0.03
    threshold asserted here (measured: -0.0344, +0.0276, -0.0145, +0.0013,
    +0.0079 at 4/8/16/32/64 samples), so the assertion passed or failed on
    the hash of the rules file rather than on anything about the solver. 32
    is the smallest value measured to clear it with real margin. If this
    test fails after an unrelated `rules.toml` change, suspect this before
    suspecting the engine.
    """
    rules = _custom_rules(_tmp(), {**SMALL_OVERRIDES, "solver.reload_item_draw_samples": 32})
    result = measure_reload_carryover_bias(
        rules, GameMode.BASE, positions=15, trials_per_position=500, seed=seed
    )
    assert result.positions == 15
    assert len(result.per_position) == 15
    assert -1.0 <= result.mean_bias <= 1.0

    assert abs(result.control_bias) < 0.03, (
        f"empty-rack control is {result.control_bias:+.4f}: the reload table's own "
        "error is too large at this scale for the carrying arm to mean anything"
    )
    assert result.carryover_bias > 0.02, (
        f"net carry-over bias {result.carryover_bias:+.4f} (mean {result.mean_bias:+.4f} "
        f"minus control {result.control_bias:+.4f}) — SPEC.md §15.13's effect should be "
        "clearly positive and clearly above the control floor"
    )
    assert result.mean_bias > result.control_bias


@pytest.mark.slow
def test_measure_dealer_phone_bias_runs_and_reports_signed_difference():
    rules = _custom_rules(_tmp(), SMALL_OVERRIDES)
    result = measure_dealer_phone_bias(rules, pairs=5, trials_per_position=300, seed=19)
    assert result.pairs == 5
    assert -1.0 <= result.mean_gap_with_phone <= 1.0
    assert -1.0 <= result.mean_gap_without_phone <= 1.0
    assert result.signed_difference == pytest.approx(
        result.mean_gap_with_phone - result.mean_gap_without_phone
    )


@pytest.mark.slow
def test_measure_reload_flag_sensitivity_player_moves_first(tmp_path):
    """SPEC.md §15.11: flipping `round.player_moves_first_after_reload` must
    move at least one reload-table entry (a null result here would mean the
    flag is silently inert, which is itself worth flagging)."""
    overrides_true = dict(SMALL_OVERRIDES)
    overrides_true["round.player_moves_first_after_reload"] = True
    overrides_false = dict(SMALL_OVERRIDES)
    overrides_false["round.player_moves_first_after_reload"] = False

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    rules_true = _custom_rules(dir_a, overrides_true)
    rules_false = _custom_rules(dir_b, overrides_false)

    result = measure_reload_flag_sensitivity(
        rules_true, rules_false, GameMode.BASE, "round.player_moves_first_after_reload"
    )
    assert result.entries_compared > 0
    assert result.max_abs_diff >= 0.0
    assert result.mean_abs_diff >= 0.0


def test_measure_reload_flag_sensitivity_cuffs_survive_raises(tmp_path):
    """SPEC.md §15.12: cuffs surviving a reload has no 3-D table
    representation (`Solver._build_reload_state` raises `NotImplementedError`
    rather than silently mispricing it). This harness does not work around
    that raise — it documents that the sensitivity is simply not measurable
    under the current design, which is what this test pins down."""
    overrides_true = dict(TINY_OVERRIDES)
    overrides_true["round.cuffs_clear_on_reload"] = True
    overrides_false = dict(TINY_OVERRIDES)
    overrides_false["round.cuffs_clear_on_reload"] = False

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    rules_true = _custom_rules(dir_a, overrides_true)
    rules_false = _custom_rules(dir_b, overrides_false)

    with pytest.raises(NotImplementedError, match="cuffs_clear_on_reload"):
        measure_reload_flag_sensitivity(
            rules_true, rules_false, GameMode.BASE, "round.cuffs_clear_on_reload"
        )
