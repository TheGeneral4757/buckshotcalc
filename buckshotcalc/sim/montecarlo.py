"""Monte Carlo validation harness (SPEC.md §12.1, M5).

Pure stdlib, like `buckshotcalc.engine` (SPEC.md §0.1 (3)). This
module answers a single question the solver never checks itself: does the
number `Solver.value()`/`Solver.evaluate()` reports actually match what
happens when the position is played out many times?

**Independence is the whole point.** The rollout path below never imports or
calls `buckshotcalc.engine.solver`'s transition logic. It routes every state
change through `buckshotcalc.engine.actions.apply` and
`buckshotcalc.engine.dealer.dealer_step` — the same M2/M3 primitives the
solver itself is built on, but exercised here by direct sampling rather than
by exact expectimax. `Solver` is consulted for exactly one thing: which
action the player takes at a player node (`Solver.evaluate(state).children[0]`,
SPEC.md §7.1's argmax-over-root-children). That is what makes the empirical
win rate comparable to `solver.value(state)` at all — `solver.value` is
defined as the value *under optimal play*, so sampling anything other than
the solver's own recommended action at every player node would measure a
different quantity and the whole comparison would be meaningless. See
`rollout`'s docstring for the point-by-point protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import multiprocessing as mp
import random
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from buckshotcalc.engine.actions import apply
from buckshotcalc.engine.dealer import dealer_step
from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.solver import ProgressFn, Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)

# ---------------------------------------------------------------------------
# Reload handling — the two honest modes (see the module docstring's "How a
# rollout works" section in the M5 brief).
# ---------------------------------------------------------------------------


class ReloadMode(IntEnum):
    """How `rollout` handles an empty chamber (SPEC.md §7.4).

    `TABLE` stops the trial at the reload boundary and scores it as a single
    Bernoulli draw with probability `solver.reload_value(...)` — this
    validates the *search above the reload boundary* while trusting the
    reload table itself. Its empirical mean over many trials is, by
    construction, exactly `reload_value(...)`: it proves nothing about the
    table's accuracy, only about whether the search that fed into that
    boundary was reached correctly.

    `SIMULATE` instead builds a genuine fresh load — a real shell config draw
    (SPEC.md §3.1 step 3) and a real item draw on top of whatever each side
    still holds (SPEC.md §3.1 step 1) — and keeps playing. This is the only
    mode that can validate the reload table itself, and the only one that can
    measure SPEC.md §15.13 (the table's blindness to carried-over items).

    Default is `TABLE`. A harness that silently used the table to validate
    the table would prove nothing about the table — that is the trap this
    enum exists to make impossible to fall into by accident.
    """

    TABLE = 0
    SIMULATE = 1


# ---------------------------------------------------------------------------
# Result types (pinned API — see the M5 brief)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RolloutResult:
    """The outcome of `rollout`: `wins` out of `trials` independent playouts."""

    trials: int
    wins: int

    @property
    def empirical(self) -> float:
        """The empirical win rate, `wins / trials`."""
        return self.wins / self.trials

    @property
    def stderr(self) -> float:
        """`sqrt(p(1-p)/n)`, the Bernoulli standard error of `empirical`."""
        if self.trials == 0:
            return 0.0
        p = self.empirical
        return math.sqrt(p * (1.0 - p) / self.trials)


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One position's solver-prediction-vs-rollout comparison (SPEC.md §12.1)."""

    state: GameState
    predicted: float
    empirical: float
    trials: int
    stderr: float

    @property
    def error(self) -> float:
        """`|predicted - empirical|`."""
        return abs(self.predicted - self.empirical)

    @property
    def sigma(self) -> float:
        """`error / stderr`, the number of standard errors the gap represents.

        Guarded at `stderr == 0` (a position with an exactly deterministic
        empirical outcome, e.g. `p` at 0 or 1 exactly): returns `0.0` if
        `error` is also exactly zero (perfect agreement, no discrepancy to
        report), else `math.inf` (any nonzero gap against zero variance is an
        infinitely significant disagreement, not a divide-by-zero to hide).
        """
        if self.stderr == 0.0:
            return 0.0 if self.error == 0.0 else math.inf
        return self.error / self.stderr


# ---------------------------------------------------------------------------
# Sampling primitives — the only place `rng.random()` is called for a branch
# choice, so every rollout is reproducible end-to-end from its seed.
# ---------------------------------------------------------------------------


def _sample_weighted(outcomes: Sequence[tuple[float, GameState]], rng: random.Random) -> GameState:
    """Pick one successor from a `(weight, GameState)` distribution.

    A single `rng.random()` draw, walked against the cumulative weights.
    Floating-point round-off can leave the cumulative sum a hair under 1.0,
    so the last outcome is the fallback rather than letting that raise.
    """
    r = rng.random()
    cumulative = 0.0
    for weight, state in outcomes:
        cumulative += weight
        if r < cumulative:
            return state
    return outcomes[-1][1]


# ---------------------------------------------------------------------------
# Independent reload simulation (SPEC.md §3.1 steps 1 and 3) — deliberately
# NOT sharing code with `solver.py`'s `_shell_configs`/`_draw_items`. Those
# exist to build the reload *table*; this exists to validate it, so it must
# not reuse its reasoning (the M5 brief's "do not reuse solver.py anywhere in
# the rollout path").
# ---------------------------------------------------------------------------


def _sample_shell_config(rules: Rules, rng: random.Random) -> tuple[int, int]:
    """`(live, blank)` for a fresh load, sampled independently of `solver.py`.

    Uniform total in `[min_total, max_total]`; even totals split equally,
    odd totals split with one extra of a uniformly-chosen type — SPEC.md
    §3.1 step 3, read directly from `rules.shells` rather than any literal.
    Raises `NotImplementedError` if the rules file asks for a split algorithm
    this harness (like `solver.py`'s `_shell_configs`) has no implementation
    for, rather than silently guessing one (SPEC.md §0.1 (2)).
    """
    shells = rules.shells
    if not shells.even_total_splits_equally or not shells.odd_total_differs_by_one:
        raise NotImplementedError(
            "montecarlo: shells.even_total_splits_equally and "
            "shells.odd_total_differs_by_one must both be true — SPEC.md §3.1 "
            "defines no alternative split rule for either being false"
        )
    total = rng.randint(shells.min_total, shells.max_total)
    if total % 2 == 0:
        half = total // 2
        return half, half
    base = total // 2
    extra = shells.odd_total_extra_shell
    if extra == "live" or (extra == "either" and rng.random() < 0.5):
        return base + 1, base
    return base, base + 1


def _item_list_for_mode(rules: Rules, mode: GameMode) -> tuple[Item, ...]:
    items = rules.items.base_items
    if mode is GameMode.DOUBLE_OR_NOTHING:
        items = items + rules.items.double_or_nothing_items
    return items


def _sample_item_draw(
    rules: Rules, item_list: tuple[Item, ...], rng: random.Random, carried: ItemCounts
) -> ItemCounts:
    """One side's post-reload rack: `carried` plus a fresh draw (SPEC.md §3.1 step 1).

    Draw count uniform in `[dealt_per_round_min, dealt_per_round_max]`; each
    drawn item uniform (with replacement) over `item_list`. Items that would
    push the rack past `rules.items.max_slots` are simply not accepted — "at
    8, no further items are accepted" (SPEC.md §3.1 step 1) — rather than
    raising, since a full rack on a real table just means the excess is
    declined, not an error.
    """
    k = rng.randint(rules.items.dealt_per_round_min, rules.items.dealt_per_round_max)
    counts = carried
    for _ in range(k):
        if counts.total() >= rules.items.max_slots:
            break
        item = item_list[rng.randrange(len(item_list))]
        counts = counts.with_delta(item, 1)
    return counts


def _fresh_reload_state(
    state: GameState, rules: Rules, mode: GameMode, rng: random.Random
) -> GameState:
    """Build the successor of an empty-chamber state under `ReloadMode.SIMULATE`.

    Health carries over (SPEC.md §3.1 step 5 — the health phase is skipped on
    a reload); a fresh shell config and fresh item draws (on top of whatever
    each side still holds — this is exactly the carry-over `solver.py`'s
    table cannot see, SPEC.md §15.13) are sampled independently of
    `solver.py`. Who moves first (§15.11) and whether cuffs survive (§15.12)
    are read straight from `rules.round_` — unlike `Solver`, which must
    refuse `cuffs_clear_on_reload = false` because its 3-D table cannot
    represent a surviving cuff (SPEC.md §15.12), this function has no such
    limitation: it carries the *full* `GameState` forward, cuffs included, so
    it can honor either setting exactly. That asymmetry is deliberate and
    documented in the M5 report — it is what lets `SIMULATE` measure
    §15.11/§15.12 sensitivity that the table-based solver structurally
    cannot represent.
    """
    live, blank = _sample_shell_config(rules, rng)
    chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
    item_list = _item_list_for_mode(rules, mode)
    items_player = _sample_item_draw(rules, item_list, rng, state.items[Player.PLAYER])
    items_dealer = _sample_item_draw(rules, item_list, rng, state.items[Player.DEALER])
    turn = Player.PLAYER if rules.round_.player_moves_first_after_reload else Player.DEALER
    cuffed = (False, False) if rules.round_.cuffs_clear_on_reload else state.cuffed
    return GameState.create(
        chamber=chamber,
        hp=state.hp,
        max_hp=state.max_hp,
        turn=turn,
        items=(items_player, items_dealer),
        sawed=False,
        cuffed=cuffed,
        dealer_knows=frozenset(),
        items_used_this_turn=0,
    )


# ---------------------------------------------------------------------------
# rollout
# ---------------------------------------------------------------------------

_MAX_STEPS_PER_TRIAL = 200_000  # safety valve against a runaway trial, never expected to bind


def _play_one_trial(
    state: GameState,
    rules: Rules,
    solver: Solver,
    rng: random.Random,
    mode: GameMode,
    reload_mode: ReloadMode,
) -> bool:
    """Play one round to completion from `state`; return whether the player won.

    The protocol (M5 brief, "how a rollout works"):
      - Terminal: `state.winner()` decides it directly.
      - Empty chamber: `ReloadMode.TABLE` ends the trial with a single
        Bernoulli draw at `solver.reload_value(...)`; `ReloadMode.SIMULATE`
        builds a genuine fresh load (`_fresh_reload_state`) and continues.
      - Player node: play the solver's own recommended action —
        `solver.evaluate(state).children[0].action` — never a random legal
        action. `solver.value` is defined as the value under *optimal* play,
        so anything else would make the empirical number measure a different
        quantity than the one being validated.
      - Dealer node: sample one branch of `dealer_step(state, rules)` by its
        weight. `dealer_step` already resolves the Dealer's entire turn (item
        uses chained through to the terminating shot) in one call, so one
        sample here is one full Dealer turn, matching the solver's own
        `_dealer_node` recursion structure.
      - Every transition is sampled from `apply`/`dealer_step`'s own weighted
        outcomes — nothing here ever constructs a successor by hand.
    """
    steps = 0
    while True:
        steps += 1
        if steps > _MAX_STEPS_PER_TRIAL:
            raise RuntimeError(
                "montecarlo: a single trial exceeded the safety step cap — "
                "this indicates a non-terminating cycle, not a slow but "
                "legitimate round"
            )

        winner = state.winner()
        if winner is not None:
            return winner is Player.PLAYER

        if state.chamber.is_empty:
            if reload_mode is ReloadMode.TABLE:
                p = solver.reload_value(state.hp[0], state.hp[1], state.max_hp)
                return rng.random() < p
            state = _fresh_reload_state(state, rules, mode, rng)
            continue

        if state.turn is Player.PLAYER:
            result = solver.evaluate(state)
            assert result.children, "player node with no legal actions is unreachable"
            action = result.children[0].action
            outcomes = apply(state, action, rules)
            state = _sample_weighted(outcomes, rng)
        else:
            branches = dealer_step(state, rules)
            outcomes = [(w, s) for w, _actions, s in branches]
            state = _sample_weighted(outcomes, rng)


def rollout(
    state: GameState,
    rules: Rules,
    solver: Solver,
    rng: random.Random,
    trials: int,
    *,
    reload_mode: ReloadMode = ReloadMode.TABLE,
    mode: GameMode = GameMode.BASE,
) -> RolloutResult:
    """Play `state` out `trials` independent times; return the win tally.

    `solver` is consulted only to pick the player's action at each player
    node (SPEC.md §7.1's argmax) — never to resolve a transition or an
    outcome; see the module docstring. `mode` selects which item list a
    `ReloadMode.SIMULATE` reload draws from and is irrelevant under
    `ReloadMode.TABLE` (default), where no fresh load is ever built. One
    `random.Random` is threaded through every sampling decision (both here
    and inside `_play_one_trial`), so a run is fully reproducible from `rng`'s
    seed — including the exact sequence of branch choices, not merely the
    final tally.
    """
    wins = 0
    for _ in range(trials):
        if _play_one_trial(state, rules, solver, rng, mode, reload_mode):
            wins += 1
    return RolloutResult(trials=trials, wins=wins)


# ---------------------------------------------------------------------------
# random_position
# ---------------------------------------------------------------------------


def _random_item_rack(rules: Rules, item_list: tuple[Item, ...], rng: random.Random) -> ItemCounts:
    k = rng.randint(0, rules.items.max_slots)
    counts = ItemCounts.empty()
    for _ in range(k):
        if counts.total() >= rules.items.max_slots:
            break
        item = item_list[rng.randrange(len(item_list))]
        counts = counts.with_delta(item, 1)
    return counts


def _random_dealer_knows(rng: random.Random, chamber: ChamberState) -> frozenset[int]:
    """A plausible `dealer_knows`, per docs/dealer-knowledge.md.

    Weighted towards indices that are still unresolved *for us* — that is
    the Dealer's genuinely private knowledge, the case the doc's addendum is
    about — but occasionally includes an index we have also resolved
    ourselves, since that is a reachable (if less interesting) state too and
    both must stay generatable. Never includes an index outside the chamber.
    """
    n = len(chamber.slots)
    if n == 0 or rng.random() < 0.5:
        return frozenset()
    unresolved = [i for i in range(n) if chamber.slots[i] is None]
    resolved = [i for i in range(n) if chamber.slots[i] is not None]
    pool = unresolved * 3 + resolved
    if not pool:
        return frozenset()
    k = rng.randint(1, min(3, n))
    chosen: set[int] = set()
    tries = 0
    while len(chosen) < k and tries < 20:
        chosen.add(rng.choice(pool))
        tries += 1
    return frozenset(chosen)


def random_position(rules: Rules, mode: GameMode, rng: random.Random) -> GameState:
    """A random, legal, reachable-looking `GameState` (M5 brief).

    Chamber: length uniform in `[1, rules.shells.max_total]` with a real
    unresolved pool (never a terminal or empty-chamber position — both
    excluded by construction, length >= 1 and HP >= 1 both sides). HP is
    drawn in `1..max_hp` for both sides, `max_hp` from
    `rules.health.possible_max_hp`. Item racks are drawn from `mode`'s item
    list within `rules.items.max_slots`. `sawed`, `cuffed`, and
    `dealer_knows` are each populated some of the time — everything read
    from `rules`, nothing hardcoded.
    """
    max_hp = rng.choice(rules.health.possible_max_hp)
    hp = (rng.randint(1, max_hp), rng.randint(1, max_hp))

    length = rng.randint(1, rules.shells.max_total)
    slots: list[Shell | None] = [rng.choice([Shell.LIVE, Shell.BLANK, None]) for _ in range(length)]
    unresolved = slots.count(None)
    unknown_live = rng.randint(0, unresolved)
    chamber = ChamberState.create(tuple(slots), unknown_live, unresolved - unknown_live)

    item_list = _item_list_for_mode(rules, mode)
    items = (
        _random_item_rack(rules, item_list, rng),
        _random_item_rack(rules, item_list, rng),
    )

    sawed = rng.random() < 0.3
    cuffed = (rng.random() < 0.2, rng.random() < 0.2)
    dealer_knows = _random_dealer_knows(rng, chamber)
    turn = rng.choice([Player.PLAYER, Player.DEALER])

    return GameState.create(
        chamber=chamber,
        hp=hp,
        max_hp=max_hp,
        turn=turn,
        items=items,
        sawed=sawed,
        cuffed=cuffed,
        dealer_knows=dealer_knows,
        items_used_this_turn=0,
    )


# ---------------------------------------------------------------------------
# validate — SPEC.md §12.1 acceptance
# ---------------------------------------------------------------------------


def _derive_seed(seed: int, index: int) -> int:
    """An independent rollout seed for position `index`, from `(seed, index)` only.

    sha256, not `hash()` (salted per process for strings) and not
    `seed + index` (adjacent Mersenne-Twister seeds are fine in practice but
    this costs nothing). Stable across interpreters, platforms and worker
    counts, which is what makes `validate` independent of `workers`.
    """
    digest = hashlib.sha256(f"buckshotcalc.montecarlo:{seed}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _check_position(
    solver: Solver, rules: Rules, mode: GameMode, trials: int, state: GameState, seed: int
) -> Discrepancy:
    """One position's predicted value against `trials` rollouts (SPEC.md §12.1)."""
    result = rollout(
        state, rules, solver, random.Random(seed), trials, reload_mode=ReloadMode.TABLE, mode=mode
    )
    return Discrepancy(
        state=state,
        predicted=solver.value(state),
        empirical=result.empirical,
        trials=trials,
        stderr=result.stderr,
    )


#: Per-worker context, set once by `_mc_init` (same pattern as
#: `engine.solver._mp_init`). Module level so "spawn" can pickle the entry
#: points; nothing here runs at import time, so re-importing this module in a
#: worker is safe.
_mc_ctx: tuple[Solver, Rules, GameMode, int] | None = None


def _mc_init(
    rules: Rules, mode: GameMode, table: dict[tuple[int, int, int], float], trials: int
) -> None:
    """Pool initializer: this worker's own `Solver`, seeded with the parent's reload table.

    The table (29 floats at shipped scale) rides in `initargs` rather than
    being read from the disk cache, so workers never build it themselves —
    even when `solver.reload_table_cache` is false.
    """
    global _mc_ctx
    solver = Solver(rules, mode)
    solver._reload_table = table
    _mc_ctx = (solver, rules, mode, trials)


def _mc_task(args: tuple[int, GameState, int]) -> tuple[int, Discrepancy]:
    index, state, seed = args
    assert _mc_ctx is not None, "_mc_task called before the pool initializer ran"
    solver, rules, mode, trials = _mc_ctx
    return index, _check_position(solver, rules, mode, trials, state, seed)


def validate(
    rules: Rules,
    mode: GameMode,
    *,
    positions: int,
    trials: int,
    seed: int,
    progress: Callable[[int, int, float], None] | None = None,
    workers: int | None = None,
    reload_progress: ProgressFn = None,
) -> tuple[Discrepancy, ...]:
    """`positions` random positions, each cross-checked at `trials` rollouts.

    All positions are generated first, serially, from `random.Random(seed)`;
    position `i`'s rollouts then use their own generator seeded from
    `(seed, i)` (`_derive_seed`). So the result depends on `seed` alone —
    not on `workers`, scheduling or completion order — and positions are
    farmed to a stdlib `multiprocessing` "spawn" pool, one `Solver` per
    worker. **The random stream differs from the pre-parallel harness**
    (which threaded one generator through everything); the same seed gives
    different positions' rollouts than before, though the statistics are the
    same. Uses `ReloadMode.TABLE` (the default), matching SPEC.md §12.1's
    acceptance check, which validates the search rather than the reload
    table (see `measure_reload_carryover_bias` for the latter).

    The reload table is built (or loaded from the disk cache) once in this
    process before any worker starts, and handed to the workers directly.
    `workers` defaults to `Solver._reload_workers()` (rules.solver.reload_workers
    / BUCKSHOTCALC_RELOAD_WORKERS / cpu_count), capped at `positions`;
    `<= 1` runs in-process with no pool.

    Returned sorted **worst-first by `sigma`** (SPEC.md §12.1's own framing:
    at 100k trials the standard error is ~0.0016, so an absolute gap of 0.01
    is roughly 6 sigma — a real failure, not noise — and a position whose
    true value sits near 0 or 1 has a small stderr, so a small absolute error
    there can still be highly significant. Sorting by raw error alone would
    miss exactly those positions). `sorted_by_error` gives the complementary
    ordering for the milestone report, which the M5 brief asks be reported
    alongside this one.

    `progress`, if given, is called as each position finishes (out of order
    when parallel) with `(positions_done, positions_total, elapsed_s)`. This
    is a long run at §12.1's mandated scale — 100 positions x 100k trials —
    and a harness that goes silent for hours cannot be told apart from one
    that has hung. Like the reload build's `ReloadProgress`, this reports
    facts only; the estimate is the caller's to make and print.
    `reload_progress` is forwarded to the one-off table build, if any.
    """
    rng = random.Random(seed)
    solver = Solver(rules, mode)
    solver.build_reload_table(reload_progress)
    table = solver._reload_table
    assert table is not None
    states = [random_position(rules, mode, rng) for _ in range(positions)]
    tasks = [(i, st, _derive_seed(seed, i)) for i, st in enumerate(states)]
    n_workers = solver._reload_workers() if workers is None else workers
    n_workers = min(n_workers, positions)

    results: dict[int, Discrepancy] = {}
    started = time.perf_counter()

    def _done(index: int, d: Discrepancy) -> None:
        results[index] = d
        if progress is not None:
            progress(len(results), positions, time.perf_counter() - started)

    if n_workers <= 1:
        for i, st, sd in tasks:
            _done(i, _check_position(solver, rules, mode, trials, st, sd))
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(
            processes=n_workers, initializer=_mc_init, initargs=(rules, mode, table, trials)
        ) as pool:
            for i, d in pool.imap_unordered(_mc_task, tasks, chunksize=1):
                _done(i, d)
    # Index order first, so sigma ties sort the same whatever the completion order.
    out = [results[i] for i in range(positions)]
    return tuple(sorted(out, key=lambda d: d.sigma, reverse=True))


def sorted_by_error(discrepancies: tuple[Discrepancy, ...]) -> tuple[Discrepancy, ...]:
    """The complementary ordering to `validate`'s sigma-sort: worst-first by raw `error`."""
    return tuple(sorted(discrepancies, key=lambda d: d.error, reverse=True))


# ---------------------------------------------------------------------------
# SPEC.md §15's three measurement obligations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReloadCarryoverBias:
    """SPEC.md §15.13: the reload table's blindness to carried-over items.

    For each of `positions` positions — one shell left, both sides holding
    at least one item, player to move — `predicted` is `solver.value(state)`
    (which internally treats the reload as if both sides arrive empty-handed:
    SPEC.md §7.4) and `empirical` is a `ReloadMode.SIMULATE` rollout (which
    honors the actual carried-over rack). `mean_bias = mean(empirical -
    predicted)`: **signed**, not absolute, because the open question is which
    direction the bias runs, not merely that one exists (SPEC.md §15.13 is
    explicit that this is unsigned/unknown a priori).


    Two numbers are reported, and the second is what makes the first mean
    anything. `mean_bias` is measured over positions that carry items;
    `control_bias` repeats the identical measurement over positions whose
    racks are **empty**, where carry-over cannot contribute by construction.
    Any gap the control shows is the reload table's own sampling error
    (`solver.reload_item_draw_samples` is finite), which lands in
    `empirical - predicted` in exactly the same place the carry-over effect
    does. Without the control the two are indistinguishable and the headline
    number is uninterpretable — the carry-over bias is
    `mean_bias - control_bias`, not `mean_bias`.

    `stderr` is the standard error **across positions**
    (`stdev(diffs) / sqrt(positions)`), not the pooled within-position
    rollout error. Position-to-position spread dominates by more than an
    order of magnitude here: the per-position rollout noise is ~0.002 at a
    few thousand trials while the diffs themselves spread by ~0.15, so
    quoting the rollout error alone overstates significance roughly 40-fold.
    The estimand is the bias over the *distribution* of positions, so the
    sampling of positions has to be in the interval.
    """

    positions: int
    trials_per_position: int
    mean_bias: float
    stderr: float  # standard error of `mean_bias` ACROSS positions, not within
    control_bias: float  # same measurement, empty racks: the table's own error floor
    control_stderr: float
    per_position: tuple[tuple[float, float], ...]  # (predicted, empirical) per position

    @property
    def carryover_bias(self) -> float:
        """The §15.13 effect proper: the measured bias net of the control floor."""
        return self.mean_bias - self.control_bias


def _random_pre_reload_position(
    rules: Rules, mode: GameMode, rng: random.Random, *, carrying_items: bool = True
) -> GameState:
    """A position one shell from a reload, both sides holding >=1 item.

    Retries with fresh draws until both racks are non-empty — needed because
    `_random_item_rack` can legitimately draw zero items, which would
    contribute nothing to a carry-over measurement.
    """
    item_list = _item_list_for_mode(rules, mode)
    while True:
        max_hp = rng.choice(rules.health.possible_max_hp)
        hp = (rng.randint(1, max_hp), rng.randint(1, max_hp))
        live = 1 if rng.random() < 0.5 else 0
        chamber = ChamberState.create((None,), live, 1 - live)
        if not carrying_items:
            # The control arm: empty racks, so nothing can be carried through
            # the reload and any measured gap is the table's own error.
            items = (ItemCounts.empty(), ItemCounts.empty())
            return GameState.create(
                chamber=chamber,
                hp=hp,
                max_hp=max_hp,
                turn=Player.PLAYER,
                items=items,
                sawed=False,
                cuffed=(False, False),
                dealer_knows=frozenset(),
                items_used_this_turn=0,
            )
        items = (
            _random_item_rack(rules, item_list, rng),
            _random_item_rack(rules, item_list, rng),
        )
        if items[0].total() == 0 or items[1].total() == 0:
            continue
        return GameState.create(
            chamber=chamber,
            hp=hp,
            max_hp=max_hp,
            turn=Player.PLAYER,
            items=items,
            sawed=False,
            cuffed=(False, False),
            dealer_knows=frozenset(),
            items_used_this_turn=0,
        )


def measure_reload_carryover_bias(
    rules: Rules, mode: GameMode, *, positions: int, trials_per_position: int, seed: int
) -> ReloadCarryoverBias:
    """Measure SPEC.md §15.13's signed bias from the table's item blindness.

    Restricted to `mode`'s reload table (a `Solver(rules, mode)` instance);
    positions are one shell from a reload with both sides holding items, so
    the reload boundary is reached almost immediately and the measured gap
    is attributable to the reload itself rather than to search elsewhere in
    the tree.
    """
    if positions < 2:
        raise ValueError("measure_reload_carryover_bias: need >= 2 positions for a stderr")
    rng = random.Random(seed)
    solver = Solver(rules, mode)

    def arm(carrying_items: bool) -> tuple[float, float, list[tuple[float, float]]]:
        pairs: list[tuple[float, float]] = []
        for _ in range(positions):
            state = _random_pre_reload_position(
                rules, mode, rng, carrying_items=carrying_items
            )
            predicted = solver.value(state)
            result = rollout(
                state,
                rules,
                solver,
                rng,
                trials_per_position,
                reload_mode=ReloadMode.SIMULATE,
                mode=mode,
            )
            pairs.append((predicted, result.empirical))
        diffs = [emp - pred for pred, emp in pairs]
        mean_bias = sum(diffs) / len(diffs)
        # Standard error ACROSS positions. The within-position rollout error
        # is real but an order of magnitude smaller than the spread between
        # positions, and the estimand is the bias over the distribution of
        # positions — so the position sampling, not the coin flipping, is
        # what the interval has to cover. Reporting the pooled Bernoulli
        # error instead overstates significance by roughly 40x here.
        spread = statistics.stdev(diffs)
        return mean_bias, spread / math.sqrt(len(diffs)), pairs

    mean_bias, stderr, pairs = arm(carrying_items=True)
    control_bias, control_stderr, _control_pairs = arm(carrying_items=False)
    return ReloadCarryoverBias(
        positions=positions,
        trials_per_position=trials_per_position,
        mean_bias=mean_bias,
        stderr=stderr,
        control_bias=control_bias,
        control_stderr=control_stderr,
        per_position=tuple(pairs),
    )


@dataclass(frozen=True, slots=True)
class DealerPhoneBias:
    """SPEC.md §15.9: the Dealer's private Burner Phone knowledge.

    Double or Nothing only, and only meaningful when the Dealer holds a
    phone. `mean_predicted_minus_empirical_with_phone` and its `_without`
    counterpart are the mean `(predicted - empirical)` over matched sets of
    positions — matched on everything except whether the Dealer holds a
    phone. `signed_difference = with - without`: docs/dealer-knowledge.md
    documents the modelling choice as biasing `P(win)` upward, i.e. a
    *positive* `predicted - empirical` gap that is larger (more positive)
    when the Dealer holds a phone.
    """

    pairs: int
    trials_per_position: int
    mean_gap_with_phone: float
    mean_gap_without_phone: float
    signed_difference: float
    stderr_with_phone: float
    stderr_without_phone: float


def _random_don_position(
    rules: Rules, rng: random.Random, *, dealer_has_phone: bool
) -> GameState:
    """A random Double or Nothing position, with the Dealer's phone count forced.

    Otherwise generated the same way as `random_position`, restricted to
    `GameMode.DOUBLE_OR_NOTHING` so the phone is even in the item pool.
    """
    item_list = _item_list_for_mode(rules, GameMode.DOUBLE_OR_NOTHING)
    while True:
        max_hp = rng.choice(rules.health.possible_max_hp)
        hp = (rng.randint(1, max_hp), rng.randint(1, max_hp))
        length = rng.randint(1, rules.shells.max_total)
        slots: list[Shell | None] = [
            rng.choice([Shell.LIVE, Shell.BLANK, None]) for _ in range(length)
        ]
        unresolved = slots.count(None)
        unknown_live = rng.randint(0, unresolved)
        chamber = ChamberState.create(tuple(slots), unknown_live, unresolved - unknown_live)

        player_items = _random_item_rack(rules, item_list, rng)
        dealer_items = _random_item_rack(rules, item_list, rng)
        if dealer_has_phone:
            dealer_items = dealer_items.with_delta(Item.BURNER_PHONE, 1)
        elif dealer_items[Item.BURNER_PHONE] > 0:
            continue  # redraw: this branch requires the Dealer NOT to hold one

        sawed = rng.random() < 0.3
        cuffed = (rng.random() < 0.2, rng.random() < 0.2)
        dealer_knows = _random_dealer_knows(rng, chamber)
        turn = rng.choice([Player.PLAYER, Player.DEALER])
        return GameState.create(
            chamber=chamber,
            hp=hp,
            max_hp=max_hp,
            turn=turn,
            items=(player_items, dealer_items),
            sawed=sawed,
            cuffed=cuffed,
            dealer_knows=dealer_knows,
            items_used_this_turn=0,
        )


def measure_dealer_phone_bias(
    rules: Rules, *, pairs: int, trials_per_position: int, seed: int
) -> DealerPhoneBias:
    """Measure SPEC.md §15.9's bias from the Dealer's un-modelled private phone knowledge.

    `pairs` positions with the Dealer holding a phone, and `pairs` positions
    without (all `GameMode.DOUBLE_OR_NOTHING`), each cross-checked with a
    `ReloadMode.TABLE` rollout (the phone-knowledge approximation is a
    Dealer-node modelling choice, unrelated to the reload table, so
    `TABLE` — the cheaper mode — is the right default here; `SIMULATE`
    would only add reload-table noise to a measurement this isn't about).
    """
    rng = random.Random(seed)
    solver = Solver(rules, GameMode.DOUBLE_OR_NOTHING)

    def _run(dealer_has_phone: bool) -> tuple[float, float]:
        gaps: list[float] = []
        variances: list[float] = []
        for _ in range(pairs):
            state = _random_don_position(rules, rng, dealer_has_phone=dealer_has_phone)
            predicted = solver.value(state)
            result = rollout(
                state,
                rules,
                solver,
                rng,
                trials_per_position,
                reload_mode=ReloadMode.TABLE,
                mode=GameMode.DOUBLE_OR_NOTHING,
            )
            gaps.append(predicted - result.empirical)
            variances.append(result.stderr**2)
        mean_gap = sum(gaps) / len(gaps)
        stderr = math.sqrt(sum(variances)) / len(variances)
        return mean_gap, stderr

    gap_with, stderr_with = _run(True)
    gap_without, stderr_without = _run(False)
    return DealerPhoneBias(
        pairs=pairs,
        trials_per_position=trials_per_position,
        mean_gap_with_phone=gap_with,
        mean_gap_without_phone=gap_without,
        signed_difference=gap_with - gap_without,
        stderr_with_phone=stderr_with,
        stderr_without_phone=stderr_without,
    )


@dataclass(frozen=True, slots=True)
class ReloadFlagSensitivity:
    """SPEC.md §15.11/§15.12: how much the reload table moves if a `round.*` flag flips.

    Built by comparing two fully-built reload tables from two `Rules`
    objects that differ in exactly one `round_` flag. This is a direct
    solver-vs-solver comparison, not a rollout — `Solver` is the right tool
    here (this is not part of the rollout path the M5 brief restricts).
    """

    flag_name: str
    entries_compared: int
    max_abs_diff: float
    mean_abs_diff: float


def measure_reload_flag_sensitivity(
    rules_a: Rules, rules_b: Rules, mode: GameMode, flag_name: str
) -> ReloadFlagSensitivity:
    """Compare `rules_a` and `rules_b`'s reload tables entry-by-entry.

    Callers build `rules_a`/`rules_b` however they like — typically the real
    `rules.toml` with one `round.*` boolean flipped — and pass the flag's
    name only for labeling the result. `SPEC.md §15.12`
    (`round.cuffs_clear_on_reload = false`) has **no table representation**:
    `Solver._build_reload_state` raises `NotImplementedError` rather than
    silently mispricing every reload (SPEC.md §15.12's own text). This
    function does not catch or work around that raise — a caller measuring
    §15.12's sensitivity will see it propagate, which *is* the answer: the
    sensitivity is not measurable under the current design, and the
    milestone report explains why rather than routing around it.
    """
    solver_a = Solver(rules_a, mode)
    solver_b = Solver(rules_b, mode)
    solver_a._ensure_reload_table()
    solver_b._ensure_reload_table()
    table_a = solver_a._reload_table
    table_b = solver_b._reload_table
    assert table_a is not None and table_b is not None
    keys = sorted(set(table_a) & set(table_b))
    diffs = [abs(table_a[k] - table_b[k]) for k in keys]
    return ReloadFlagSensitivity(
        flag_name=flag_name,
        entries_compared=len(keys),
        max_abs_diff=max(diffs) if diffs else 0.0,
        mean_abs_diff=(sum(diffs) / len(diffs)) if diffs else 0.0,
    )


# ---------------------------------------------------------------------------
# main() — the only place this module prints (SPEC.md §0.1: "No print in engine
# or sim library code. Harnesses get a main()").
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: run `validate` against the real `rules.toml` and report.

    The first call to `Solver.reload_value` builds the §7.4 fixed-point
    table, which is the expensive part of any run. Since M8.5 that sweep is
    parallel across table keys (SPEC.md §7.7 item 1) and its result is
    cached to disk keyed by the rules fingerprint, so it is paid once per
    `rules.toml` rather than once per run — but the *first* run against a
    given rules file still blocks on it. Build it deliberately first (see
    `sim/reload_bench.py`) if you would rather not discover that inside a
    long validation run.

    Note that any edit to `rules.toml` changes the fingerprint and therefore
    invalidates the cache *and* re-rolls the table's item-draw sample; see
    `docs/decisions-pending.md` item 1 for why that matters to §12.1's
    ±0.01 budget.
    """
    from buckshotcalc.engine.rules import default_rules, load_rules

    parser = argparse.ArgumentParser(
        description="BuckshotCalc Monte Carlo validation harness (SPEC.md §12.1)"
    )
    parser.add_argument("--positions", type=int, default=100)
    parser.add_argument("--trials", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=["base", "don"], default="base")
    parser.add_argument("--rules", type=str, default=None, help="path to an alternate rules.toml")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: as for the reload build; 1 = in-process)",
    )
    parser.add_argument("--top", type=int, default=10, help="how many worst positions to print")
    args = parser.parse_args()

    # `Path`, not the raw string: `load_rules` takes a `Path` and calls
    # `.open()`/`.read_bytes()` on it, so passing argparse's `str` raised
    # `AttributeError` — `--rules` had never worked.
    rules = load_rules(Path(args.rules)) if args.rules else default_rules()
    mode = GameMode.BASE if args.mode == "base" else GameMode.DOUBLE_OR_NOTHING

    from buckshotcalc.sim.reload_bench import (
        _fmt_duration,
        _hms,
        _ProgressPrinter,
        eta_text,
        header_line,
    )

    solver = Solver(rules, mode)
    workers = args.workers if args.workers is not None else solver._reload_workers()
    workers = max(1, min(workers, args.positions))
    reload_progress = None
    if not solver.has_cached_reload_table():
        # validate() builds it once in this process before any worker starts.
        print(
            header_line(
                "reload build",
                mode.name,
                f"{rules.solver.reload_item_draw_samples} samples",
                f"{solver._reload_workers()} workers",
            ),
            flush=True,
        )
        reload_progress = _ProgressPrinter()
    print(
        header_line(
            "validate",
            mode.name,
            f"{args.positions} positions",
            f"{args.trials} trials",
            f"seed {args.seed}",
            f"{workers} workers",
        ),
        flush=True,
    )

    last = [0.0]

    def _progress(done: int, total: int, elapsed: float) -> None:
        # Positions cost roughly the same, so elapsed-per-position
        # extrapolates honestly; there is no convergence rate to estimate.
        if elapsed - last[0] < 30.0 and done != total:
            return
        last[0] = elapsed
        remaining = (total - done) * (elapsed / max(1, done))
        print(
            f"[{_hms(time.time())}] {f'{done}/{total}':>7} positions  "
            f"{_fmt_duration(elapsed)} in  ETA {eta_text(remaining, time.time())}",
            flush=True,
        )

    discrepancies = validate(
        rules,
        mode,
        positions=args.positions,
        trials=args.trials,
        seed=args.seed,
        progress=_progress,
        workers=workers,
        reload_progress=reload_progress,
    )
    by_error = sorted_by_error(discrepancies)

    def _table(title: str, rows: Sequence[Discrepancy]) -> None:
        print(f"\n-- {title} --")
        print(f"  {'predicted':>9}  {'empirical':>9}  {'error':>7}  {'sigma':>6}  {'trials':>7}")
        for d in rows:
            print(
                f"  {d.predicted:>9.4f}  {d.empirical:>9.4f}  {d.error:>7.4f}  "
                f"{d.sigma:>6.2f}  {d.trials:>7}"
            )

    _table(f"worst {args.top} by sigma (statistical significance)", discrepancies[: args.top])
    _table(f"worst {args.top} by raw absolute error", by_error[: args.top])

    n_fail = sum(1 for d in discrepancies if d.error >= 0.01)
    print(
        f"\n{'PASS' if n_fail == 0 else 'FAIL'}: {n_fail}/{len(discrepancies)} positions have "
        "|predicted - empirical| >= 0.01 (SPEC.md §12.1 acceptance requires 0)"
    )


if __name__ == "__main__":
    main()
