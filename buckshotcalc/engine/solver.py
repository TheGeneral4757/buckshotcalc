"""Memoized expectimax solver and reload value table (SPEC.md §7).

Pure stdlib. `Solver` is the M4 deliverable: given a loaded `Rules` and a
`GameMode` (which item list a reload draw comes from), it answers
`P(player wins the current round)` for any reachable `GameState`, plus a
ranked breakdown of the root's legal actions for the TUI.

Termination argument (required by the milestone brief, not merely assumed):
every transition in `actions.apply` and `dealer.dealer_step` strictly
decreases the lexicographic measure `(len(state.chamber.slots), total items
held by both sides)`.

- A shot (self or opponent) always pops a shell: `len(chamber.slots)`
  decreases, so the measure decreases regardless of what happens to items.
- Every item use consumes one item from the mover's rack (`_apply_use`
  decrements the held count before dispatching), so `len(chamber.slots)` is
  unchanged but total items strictly decreases. Adrenaline both consumes
  itself and, on the stolen item's own `_apply_use`, consumes that item too
  — a net decrease of (at least) one, never an increase, and it can never
  create a state where the *combined* rack is larger than before the steal
  (a transfer changes who holds an item, not how many exist, and the use
  immediately following the transfer removes it again).
- A blank self-shot pops a shell (chamber measure drops) and refills the
  item-use *budget* (`items_used_this_turn` resets to 0 via
  `_new_turn_same_mover`) but does not add any items to either rack, so the
  second component of the measure is untouched by the reset — only the
  first component (which strictly decreased) matters for that step.
- Dealer decision nodes are themselves resolved by finitely many applications
  of `apply` chained by `_evaluate_from` (at most `len(item_priority)` items
  plus one shot), each of which is one of the above cases.

So every root-to-leaf path through `_expectimax` (excluding the reload
boundary, which is a distinct, separately-terminating fixed point — see
`_compute_reload_table`) strictly decreases a measure bounded below by
`(0, 0)`, hence terminates. Reaching `(0, *)` is exactly the
`state.chamber.is_empty` check, which routes to `reload_value` rather than
recursing further within the same load.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import multiprocessing as mp
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from buckshotcalc.engine import memo_key
from buckshotcalc.engine.actions import (
    Action,
    ActionKind,
    apply,
    item_cap_reached,
    legal_actions,
)
from buckshotcalc.engine.dealer import dealer_step
from buckshotcalc.engine.memo_key import widths_for
from buckshotcalc.engine.rules import Rules, ShellsConfig
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
)

# A reload-value lookup: (hp_player, hp_dealer, max_hp) -> P(player wins).
ReloadFn = "callable[[int, int, int], float]"

_WASTE_EPSILON = 1e-12  # SPEC.md §8's `✗` marker — see ChildValue.wasted docstring.


def _is_wasted_adrenaline(action: Action) -> bool:
    return action.item is Item.ADRENALINE and action.steal is None


def _is_no_op(state: GameState, outcomes: tuple[tuple[float, GameState], ...]) -> bool:
    """The use changed nothing but the mover's own rack and item count.

    Cigarettes at full HP, a Glass on a known shell, a Phone with nothing left
    to reveal: they tie the best line whenever the spent item was worth nothing
    more (often, since a reload values held items at zero, SPEC.md §15.13), and
    must not win that tie on `Item` order.
    """
    if len(outcomes) != 1:
        return False
    mover = state.turn
    succ = outcomes[0][1].with_items(mover, state.items_of(mover))
    succ = succ.with_items_used_this_turn(state.items_used_this_turn)
    return succ.canonical() == state.canonical()


def _tie_penalty(action: Action, no_op: bool = False) -> float:
    if _is_wasted_adrenaline(action) or no_op:
        return 1e-9
    if action.kind is ActionKind.USE:
        return 2e-10
    return 1e-10 if action.kind is ActionKind.SHOOT_SELF else 0.0


# ---------------------------------------------------------------------------
# Public result types (pinned API — see the M4 brief)
# ---------------------------------------------------------------------------


class SearchCancelled(Exception):
    """Raised by `Solver.evaluate` when its `cancel` event is set mid-search."""


@dataclass(frozen=True, slots=True)
class ChildValue:
    """One root-level action and its value, for the TUI's ranked display (SPEC.md §8)."""

    action: Action
    value: float
    # `wasted` means STRICTLY worse than the best available action at this
    # node: `value < best_value - _WASTE_EPSILON`. This is a chosen
    # definition, not a semantic read of "this item's answer cannot change
    # the decision" — the milestone brief is explicit that the value gap
    # already expresses that, and inferring intent on top of it would be a
    # guess. An information item whose answer truly cannot move the needle
    # ties the best line's value (within epsilon) and is therefore NOT
    # flagged, which is correct: spending it isn't wrong, it just isn't
    # necessary either.
    wasted: bool


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The result of `Solver.evaluate` (SPEC.md §7.1, §8)."""

    value: float
    children: tuple[ChildValue, ...]  # descending by value; empty at a dealer/terminal node
    nodes: int  # nodes evaluated for this call (memo hits do not count)
    cap_reached: bool  # SPEC.md §7.5 — the item cap bound somewhere in this subtree


# ---------------------------------------------------------------------------
# Shell configuration enumeration (SPEC.md §3.1 step 3, §7.4 step 2)
# ---------------------------------------------------------------------------


def _shell_configs(shells: ShellsConfig) -> tuple[tuple[float, int, int], ...]:
    """`(weight, live, blank)` triples for every legal load, weights summing to 1.0.

    Uniform over totals in `[min_total, max_total]` (SPEC.md §3.1 step 3). An
    even total splits equally; an odd total has exactly one extra shell,
    given to `shells.odd_total_extra_shell` (VERIFIED "blank", RoundManager.gd:148-150;
    "either" splits that total's weight evenly between the two variants).
    Nothing here is a literal `2`, `8`, or a hardcoded split rule — every bound comes from `shells`.

    `shells.even_total_splits_equally` and `shells.odd_total_differs_by_one`
    are read and asserted true: SPEC.md and `rules.toml` document no
    alternative split algorithm, so a rules file that sets either to `False`
    has no implementation to fall back to. This is a judgment call — see the
    milestone report.
    """
    if not shells.even_total_splits_equally or not shells.odd_total_differs_by_one:
        raise NotImplementedError(
            "reload_value: shells.even_total_splits_equally and "
            "shells.odd_total_differs_by_one must both be true — SPEC.md §3.1 "
            "defines no alternative split rule for either being false"
        )

    totals = range(shells.min_total, shells.max_total + 1)
    totals = list(totals)
    if not totals:
        raise ValueError("shells: no legal total in [min_total, max_total]")
    w_total = 1.0 / len(totals)

    configs: list[tuple[float, int, int]] = []
    for total in totals:
        if total % 2 == 0:
            half = total // 2
            configs.append((w_total, half, half))
        else:
            base = total // 2
            extra = shells.odd_total_extra_shell
            if extra == "either":
                configs.append((w_total / 2.0, base + 1, base))
                configs.append((w_total / 2.0, base, base + 1))
            elif extra == "live":
                configs.append((w_total, base + 1, base))
            else:
                configs.append((w_total, base, base + 1))

    for _, live, blank in configs:
        assert live >= shells.min_live and blank >= shells.min_blank, (
            "reload_value: a generated shell split violates rules.shells minimums "
            f"(live={live}, blank={blank}, min_live={shells.min_live}, "
            f"min_blank={shells.min_blank})"
        )
    return tuple(configs)


def _draw_items(rng: random.Random, rules: Rules, item_list: tuple[Item, ...]) -> ItemCounts:
    """One side's item draw for a reload sample (SPEC.md §7.4 step 3).

    Count drawn uniformly from `[dealt_per_round_min, dealt_per_round_max]`;
    each item drawn uniformly (with replacement) from `item_list`. The draw
    count is cross-validated (`rules.py`) to never exceed `items.max_slots`,
    so no separate cap is applied here.
    """
    k = rng.randint(rules.items.dealt_per_round_min, rules.items.dealt_per_round_max)
    counts = [0] * len(Item)
    for _ in range(k):
        item = item_list[rng.randrange(len(item_list))]
        counts[item.value] += 1
    return ItemCounts(tuple(counts))


def _cap_truncated(state: GameState, rules: Rules) -> bool:
    """Whether `rules.solver.max_items_per_turn` cut off a real choice at `state` (SPEC.md §7.5).

    True iff the item-use cap has been reached AND at least one item action
    would otherwise have been legal. Checked by probing `legal_actions` on a
    copy of `state` with `items_used_this_turn` reset to 0 — a plain field
    reset via the public `with_items_used_this_turn`, not a reimplemented
    transition — and asking whether that probe offers anything beyond the
    two shot actions every player node always has.
    """
    if not item_cap_reached(state, rules):
        return False
    probe = state.with_items_used_this_turn(0)
    return len(legal_actions(probe, rules)) > 2


# ---------------------------------------------------------------------------
# Disk cache (SPEC.md §7.4)
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    """`$XDG_CACHE_HOME/buckshotcalc/`, falling back to `~/.cache/buckshotcalc/`."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "buckshotcalc"


#: Engine modules whose code decides the table's values. A change to any of them (other than
#: comments, docstrings or formatting) gives a new `_engine_fingerprint`, so the cached
#: table misses and `has_cached_reload_table()` reports it must be rebuilt.
_ENGINE_MODULES = ("state", "rules", "actions", "dealer", "memo_key", "solver")


def _stable_dump(node: object) -> str:
    """`ast.dump` without the per-interpreter noise: PyPy 3.12 and CPython 3.13 add or drop
    optional fields, so unset (`None`/empty) fields are skipped. Keeps a table built under
    pypy3 valid for the CPython calc."""
    if isinstance(node, ast.AST):
        fields = ",".join(
            f"{k}={_stable_dump(v)}"
            for k, v in ast.iter_fields(node)
            if v is not None and v != []
        )
        return f"{type(node).__name__}({fields})"
    if isinstance(node, list):
        return "[" + ",".join(_stable_dump(v) for v in node) + "]"
    return repr(node)


@cache
def _engine_fingerprint() -> str:
    """sha256 over the engine's code, ignoring comments, docstrings and layout."""
    h = hashlib.sha256()
    for name in _ENGINE_MODULES:
        tree = ast.parse((Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (
                isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
        h.update(_stable_dump(tree).encode())
    return h.hexdigest()


def _cache_path(rules: Rules, mode: GameMode) -> Path:
    engine = _engine_fingerprint()[:16]
    return _cache_dir() / f"reload_{mode.name.lower()}_e{engine}_{rules.fingerprint}.json"


def _read_cache(rules: Rules, mode: GameMode) -> dict[tuple[int, int, int], float] | None:
    """Read a cached reload table, or `None` on any miss/mismatch/corruption.

    Never raises: a missing file, unreadable bytes, malformed JSON, or a
    fingerprint/mode mismatch are all treated as "recompute", per the
    milestone brief ("a cache file whose fingerprint does not match is
    ignored, not repaired"; "corrupt/unreadable cache must degrade to
    recomputation, never raise"). Only the read path is guarded — a write
    failure is allowed to raise, since silently losing a cache write is not
    a correctness concern the way trusting a bad read is.
    """
    if not rules.solver.reload_table_cache:
        return None
    return _load_table(_cache_path(rules, mode), rules, mode, engine=_engine_fingerprint())


def _read_stale_cache(rules: Rules, mode: GameMode) -> dict[tuple[int, int, int], float] | None:
    """Newest cached table for these rules and mode from an *older engine*, or `None`.

    Only ever a starting point for the fixed point, never an answer: the sweeps'
    stop test is the true residual, so a stale table costs sweeps, not correctness.
    Same rules fingerprint means the same item-draw sample, so after a small
    engine edit the old table sits close to the new fixed point.
    """
    if not rules.solver.reload_table_cache:
        return None
    current = _cache_path(rules, mode)
    pattern = f"reload_{mode.name.lower()}_e*_{rules.fingerprint}.json"
    try:
        paths = sorted(
            (p for p in current.parent.glob(pattern) if p != current),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in paths:
        table = _load_table(path, rules, mode, engine=None)
        if table is not None:
            return table
    return None


def _load_table(
    path: Path, rules: Rules, mode: GameMode, engine: str | None
) -> dict[tuple[int, int, int], float] | None:
    """Parse one cache file; `engine=None` skips the engine check (stale warm start)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("fingerprint") != rules.fingerprint
        or (engine is not None and raw.get("engine") != engine)
        or raw.get("mode") != int(mode)
    ):
        return None
    try:
        table_raw = raw["table"]
        table: dict[tuple[int, int, int], float] = {}
        for k, v in table_raw.items():
            hp_p_s, hp_d_s, max_hp_s = k.split(",")
            table[(int(hp_p_s), int(hp_d_s), int(max_hp_s))] = float(v)
    except (KeyError, ValueError, AttributeError, TypeError):
        return None
    return table


def _write_cache(
    rules: Rules, mode: GameMode, table: dict[tuple[int, int, int], float]
) -> None:
    """Write the reload table to disk, gated on `rules.solver.reload_table_cache`."""
    if not rules.solver.reload_table_cache:
        return
    path = _cache_path(rules, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": rules.fingerprint,
        "engine": _engine_fingerprint(),
        "mode": int(mode),
        "table": {f"{k[0]},{k[1]},{k[2]}": v for k, v in table.items()},
    }
    # Write beside, then rename: a kill mid-write must not leave a half file that costs a rebuild.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReloadProgress:
    """One observation of the §7.4 fixed point's progress. Facts only.

    The engine reports; it never estimates and never prints (SPEC.md §0.1: no
    `print` in engine code). Turning these into a completion estimate is
    presentation, and lives in `sim/reload_bench.py` — which is also the
    only place that knows whether anyone is watching.

    `delta` is `None` for a `phase == "key"` event: the sweep's residual is
    not known until every key in it has been recomputed.
    """

    phase: str  # "key" (one table entry done) or "sweep" (a full pass done)
    sweep: int  # 1-based
    sweeps_max: int  # rules.solver.reload_max_iterations
    keys_done: int  # within this sweep
    keys_total: int
    elapsed_s: float  # since the build started
    delta: float | None  # the sweep's max residual, on "sweep" events
    tolerance: float
    stage: str = "main"  # "main", or the name of a preliminary coarse build


#: Optional sink for `ReloadProgress` events. `None` means "nobody is
#: watching", which is the default everywhere except the build harness.
ProgressFn = Callable[[ReloadProgress], None] | None


#: How closely the last two sweeps' residual ratios must agree before the
#: iteration is called linear enough to extrapolate. Only gates a speed-up.
_AITKEN_RATIO_AGREE = 0.2


def _aitken_next(
    hist: list[dict[tuple[int, int, int], float]], v: dict[tuple[int, int, int], float]
) -> tuple[dict[tuple[int, int, int], float], list[dict[tuple[int, int, int], float]]]:
    """Aitken delta-squared jump over the last sweeps (SPEC.md §7.7), or `v` unchanged.

    `hist` holds the iterates since the last jump. Once four are in hand and
    the two latest residual ratios agree (the iteration is linear, not still
    in its transient), each entry with `0 < r < 1` for step ratio `r = d2 /
    d1` moves to `x + d2 * r / (1 - r)`, clamped to a probability. The sweep
    loop's stop test is still the true residual of the next plain sweep, so a
    bad jump costs a sweep, never correctness. Returns the iterate to sweep
    from and the new history.
    """
    hist = [*hist[-3:], v]
    if len(hist) < 4:
        return v, hist
    x0, x1, x2, x3 = hist
    big = [max(abs(b[k] - a[k]) for k in b) for a, b in ((x0, x1), (x1, x2), (x2, x3))]
    if not all(big):
        return v, hist
    r2, r3 = big[1] / big[0], big[2] / big[1]
    if r3 >= 1.0 or abs(r3 - r2) > _AITKEN_RATIO_AGREE * r3:
        return v, hist
    out = {}
    for k, b in x3.items():
        d1, d2 = x2[k] - x1[k], b - x2[k]
        r = d2 / d1 if d1 else 0.0
        out[k] = min(1.0, max(0.0, b + d2 * r / (1.0 - r))) if 0.0 < r < 1.0 else b
    return out, [out]


class Solver:
    """Memoized expectimax over `GameState`, plus the reload value table (SPEC.md §7).

    One instance is valid for exactly one `(rules, mode)` pair: the memo and
    the reload table both hang off that pair's lifetime, which is why this
    is a class rather than the module-level `@lru_cache` SPEC.md §7.1
    sketches — that sketch is illustrative, this instance is the
    implementation.

    `mode` picks the item set only. The Dealer's policy (count-based shot rule, deductions)
    follows `rules.is_endless` from `rules.toml`, so `BASE` means base items with the
    endless-mode Dealer; v1 ships double_or_nothing only.
    """

    def __init__(self, rules: Rules, mode: GameMode) -> None:
        self.rules = rules
        self.mode = mode
        # Memoizes (value, cap_reached) together, not value alone — SPEC.md
        # §7.5's warning must propagate through the memo or a truncated
        # subtree silently looks exact to every node above it.
        #
        # Keyed by the bit-packed integer from `memo_key._pack_state`, not
        # the `GameState` itself (SPEC.md §7.7 item 2) — cheaper to hash and
        # cheaper to retain at the reload sweep's ~869 K-entry-per-key scale.
        # `memo_key.decode` exists to make that encoding testable, but the
        # hot path here never calls it.
        self._memo: dict[int, tuple[float, bool]] = {}
        # Resolved once, not once per node: `memo_key._pack_state` needs a
        # `_Widths`, and `memo_key.widths_for` is `lru_cache`d *by equality*
        # (deliberately — see its docstring on why an identity-keyed cache
        # was wrong), which still hashes the whole `rules` object on every
        # lookup. Fetching it once here and threading it through every
        # `_expectimax` call turns that from an O(nodes) cost into an O(1)
        # one; see the milestone report's before/after numbers.
        self._key_widths = widths_for(rules)
        self._nodes = 0
        # `evaluate(cancel=...)`'s event, per thread: a stale search and its
        # replacement share this solver, and a single attribute would let the
        # newer call overwrite (then, in its `finally`, clear) the older one's
        # event. Unset everywhere else, including the reload fixed point.
        self._tls = threading.local()
        # Total node count across the most recent parallel reload sweep, for
        # the milestone's measurement harness (SPEC.md §7.7) — quantifies the
        # cross-key memo sharing lost by farming keys to separate worker
        # processes (each worker's scratch memo dies with its task, unlike
        # the serial path's single shared `scratch_memo`). `None` until a
        # parallel sweep has run.
        self._last_parallel_node_count: int | None = None
        # Built lazily, once, on first need (`_ensure_reload_table`) — never
        # recomputed for the lifetime of this instance. `value()` only ever
        # reads the finished table via `reload_value`; the fixed-point sweep
        # itself never calls back into `value()`/`_expectimax` with this
        # table as the reload source (see `_compute_reload_table`), so there
        # is no reentrancy: the table-building recursion always reads from a
        # frozen per-sweep snapshot, not from `self`.
        self._reload_table: dict[tuple[int, int, int], float] | None = None

    # -- public API -----------------------------------------------------

    def value(self, state: GameState) -> float:
        """`P(player wins the round)` from `state` (SPEC.md §7.1)."""
        return self._expectimax(state, self._memo, self.reload_value, count_nodes=True)[0]

    def evaluate(
        self,
        state: GameState,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> SearchResult:
        """`value(state)` plus the root's ranked children (SPEC.md §7.1, §8).

        `children` is populated only at a live player decision node — empty
        at a terminal state, a reload boundary, or a Dealer node, per the
        pinned `SearchResult.children` contract.

        `progress(done, total, nodes_so_far)`, if given, is called once at the
        start (`done == 0`) and after each top-level branch: each root action
        at a player node, each `dealer_step` branch at a Dealer node, or a
        single (0, 1) then (1, 1) at a terminal or reload boundary. The
        reported value is unchanged: the root is still memoized through
        `_expectimax`, which after the branches are scored is a cheap
        memo-hit recomputation.
        """
        self._tls.cancel = cancel
        try:
            state = state.canonical()
            nodes_before = self._nodes

            def report(done: int, total: int) -> None:
                if progress is not None:
                    progress(done, total, self._nodes - nodes_before)

            live = state.winner() is None and not state.chamber.is_empty
            player_node = live and state.turn is Player.PLAYER
            actions = legal_actions(state, self.rules) if player_node else ()
            if player_node:
                assert actions, "player node with no legal actions (shot always available)"
            dealer_branches = dealer_step(state, self.rules) if live and not player_node else ()

            total = len(actions) or len(dealer_branches) or 1
            report(0, total)
            scored: list[tuple[Action, float]] = []
            no_ops: set[Action] = set()
            if player_node:
                for i, action in enumerate(actions, 1):
                    v = 0.0
                    outcomes = apply(state, action, self.rules)
                    if action.kind is ActionKind.USE and _is_no_op(state, outcomes):
                        no_ops.add(action)
                    for weight, successor in outcomes:
                        cv, _ = self._expectimax(
                            successor, self._memo, self.reload_value, count_nodes=True
                        )
                        v += weight * cv
                    scored.append((action, v))
                    report(i, total)
            elif dealer_branches:
                for i, (_w, _a, successor) in enumerate(dealer_branches, 1):
                    self._expectimax(successor, self._memo, self.reload_value, count_nodes=True)
                    report(i, total)

            val, cap = self._expectimax(state, self._memo, self.reload_value, count_nodes=True)
            if not scored:
                report(total, total)

            children: tuple[ChildValue, ...] = ()
            if scored:
                best_value = max(v for _, v in scored)
                # On an exact tie prefer the move that risks nothing: shoot the
                # opponent, then shoot self, then any item use; a wasted
                # Adrenaline or a no-op use is worst. The penalties sit below the 1e-9 tie
                # tolerance and far above float noise (~1e-16); values are untouched.
                # Remaining ties keep `legal_actions` order (stable sort).
                ranked = sorted(
                    scored,
                    key=lambda pair: pair[1] - _tie_penalty(pair[0], pair[0] in no_ops),
                    reverse=True,
                )
                children = tuple(
                    ChildValue(action=a, value=v, wasted=v < best_value - _WASTE_EPSILON)
                    for a, v in ranked
                )

            nodes = self._nodes - nodes_before
            return SearchResult(value=val, children=children, nodes=nodes, cap_reached=cap)
        finally:
            self._tls.cancel = None

    def has_cached_reload_table(self) -> bool:
        """Whether a valid reload table is already available, without building one.

        Purely additive query used by the TUI (SPEC.md §7.7, M7 brief): the
        real `rules.toml`-scale sweep takes on the order of 19 minutes and
        has never completed (docs/decisions-pending.md items 1-3), so the
        TUI must check this *before* calling `evaluate`/`value` on a
        position that might reach a reload boundary — never trigger the
        build from a render/event path. Returns `True` if the in-memory
        table is already built, or if a fingerprint-matching cache file is
        on disk; `False` otherwise. Never builds or writes anything itself.
        """
        if self._reload_table is not None:
            return True
        return _read_cache(self.rules, self.mode) is not None

    def reload_value(self, hp_player: int, hp_dealer: int, max_hp: int) -> float:
        """`P(player wins)` from a fresh, unresolved load at this health (SPEC.md §7.4).

        Lazily builds the fixed-point table on first call, then reads it.
        `(hp_player, hp_dealer, max_hp)` outside the table (hp not in
        `1..max_hp`, or `max_hp` not in `rules.health.possible_max_hp`)
        should never be reachable from a well-formed `GameState` — the
        terminal HP checks in `_expectimax` run before any reload lookup, so
        both HPs are always >= 1 by the time this is called, and `max_hp` is
        fixed for a round and only ever set from
        `rules.health.possible_max_hp`. If it happens anyway, this raises
        `KeyError` rather than silently returning a heuristic value.

        **Callers must be import-safe.** When `_reload_workers()` resolves to
        more than one worker this builds the table with
        `multiprocessing`'s "spawn" start method, which re-executes the
        caller's `__main__` by path in every worker. A caller whose module
        scope reaches this function — a script without an
        `if __name__ == "__main__":` guard — therefore re-enters it in each
        child and spawns recursively until the machine gives out. Both
        shipped entry points (`sim/reload_bench.py`, `tui/app.py`) are
        guarded; anything new that calls this must be too. Set
        `rules.solver.reload_workers = 0` (or `BUCKSHOTCALC_RELOAD_WORKERS=0`)
        to force the serial path, which has no such requirement.
        """
        self._ensure_reload_table()
        assert self._reload_table is not None
        key = (hp_player, hp_dealer, max_hp)
        if key not in self._reload_table:
            raise KeyError(
                f"reload_value: {key} is outside the reload table — this indicates "
                "a GameState reached a reload boundary with HP or max_hp the table "
                "was never built for; see Solver.reload_value's docstring"
            )
        return self._reload_table[key]

    # -- core recursion ---------------------------------------------------

    def _expectimax(
        self,
        state: GameState,
        memo: dict[int, tuple[float, bool]],
        reload_fn,
        count_nodes: bool,
    ) -> tuple[float, bool]:
        """`(value, cap_reached)` at `state`, memoized in `memo` (SPEC.md §7.1).

        Shared by the live search (`memo=self._memo`, `reload_fn=self.reload_value`)
        and the reload fixed-point's inner solves (a fresh scratch `memo` per
        sweep, `reload_fn` reading that sweep's frozen `V` snapshot) — the two
        differ only in where a reload boundary's value comes from, never in
        the recursion itself.

        `memo` is keyed by `memo_key._pack_state(state, self._key_widths)` —
        a packed `int`, not `state` itself (SPEC.md §7.7 item 2). Canonicalize
        first, encode second: encoding an uncanonicalized state would
        fragment the memo exactly as an uncanonicalized `GameState` key
        would. `_pack_state` directly (not `encode`/`encode_canonical`)
        because `state` is canonicalized right here, one line up, and
        `self._key_widths` is resolved once in `__init__` — both `encode`'s
        own canonicalize and `encode_canonical`'s per-call `_widths(rules)`
        cache lookup were measured to add real per-node cost at this call
        volume (see the milestone report's before/after numbers); see
        `memo_key.widths_for`'s docstring.

        Terminal order (pinned): Dealer HP <= 0, then player HP <= 0, then
        empty chamber. A player node with no legal actions cannot happen (a
        shot is always available), and is asserted rather than silently
        returning 0.0.
        """
        state = state.canonical()
        key = memo_key._pack_state(state, self._key_widths)
        cached = memo.get(key)
        if cached is not None:
            return cached

        # Checked only on a memo miss. Memo entries are written only after a
        # subtree completes, so a cancelled run leaves the memo valid.
        cancel = getattr(self._tls, "cancel", None)
        if cancel is not None and cancel.is_set():
            raise SearchCancelled
        if count_nodes:
            self._nodes += 1

        winner = state.winner()
        if winner is Player.PLAYER:
            result = (1.0, False)
        elif winner is Player.DEALER:
            result = (0.0, False)
        elif state.chamber.is_empty:
            result = (reload_fn(state.hp[0], state.hp[1], state.max_hp), False)
        elif state.turn is Player.PLAYER:
            result = self._player_node(state, memo, reload_fn, count_nodes)
        else:
            result = self._dealer_node(state, memo, reload_fn, count_nodes)

        memo[key] = result
        return result

    def _player_node(
        self,
        state: GameState,
        memo: dict[int, tuple[float, bool]],
        reload_fn,
        count_nodes: bool,
    ) -> tuple[float, bool]:
        """`max` over `legal_actions` (SPEC.md §7.1)."""
        actions = legal_actions(state, self.rules)
        assert actions, "player node with no legal actions (a shot is always available)"
        best_value: float | None = None
        any_cap = False
        for action in actions:
            v = 0.0
            for weight, successor in apply(state, action, self.rules):
                cv, ccap = self._expectimax(successor, memo, reload_fn, count_nodes)
                v += weight * cv
                any_cap = any_cap or ccap
            if best_value is None or v > best_value:
                best_value = v
        assert best_value is not None
        return (best_value, any_cap or _cap_truncated(state, self.rules))

    def _dealer_node(
        self,
        state: GameState,
        memo: dict[int, tuple[float, bool]],
        reload_fn,
        count_nodes: bool,
    ) -> tuple[float, bool]:
        """Policy-weighted average over `dealer_step` — NOT a min node (SPEC.md §7.2)."""
        branches = dealer_step(state, self.rules)
        total = 0.0
        any_cap = False
        for weight, _actions, successor in branches:
            cv, ccap = self._expectimax(successor, memo, reload_fn, count_nodes)
            total += weight * cv
            any_cap = any_cap or ccap
        return (total, any_cap or _cap_truncated(state, self.rules))

    # -- reload fixed point (SPEC.md §7.4) ---------------------------------

    def _ensure_reload_table(self, progress: ProgressFn = None) -> None:
        if self._reload_table is None:
            self._reload_table = self._compute_reload_table(progress)

    def build_reload_table(self, progress: ProgressFn = None) -> None:
        """Build (or load from the disk cache) the reload table, reporting to `progress`.

        The public entry for callers that want the build itself, not a value
        (`sim/`, the TUI's opt-in `ctrl+t`). Cancel by raising from
        `progress`: the exception propagates out of the sweep, the worker
        pool is terminated on the way, and no cache is written (the cache is
        written only after the last sweep), so a stopped build leaves nothing
        behind. It takes effect at the next progress event.
        """
        self._ensure_reload_table(progress)

    def _item_list(self) -> tuple[Item, ...]:
        items = self.rules.items.base_items
        if self.mode is GameMode.DOUBLE_OR_NOTHING:
            items = items + self.rules.items.double_or_nothing_items
        return items

    def _draw_samples(
        self,
        hp_player: int,
        hp_dealer: int,
        max_hp: int,
        configs: tuple[tuple[float, int, int], ...],
    ) -> list[list[tuple[ItemCounts, ItemCounts]]]:
        """Deterministic item-draw samples for one table entry, one list per config.

        Seeded from `rules.fingerprint` plus the entry key (SPEC.md §7.4 step
        3's determinism requirement) — NOT from the config or the sweep
        iteration, so the same finite sample set is reused across every
        sweep (stability: resampling every iteration would make the fixed
        point chase a moving target) and across configs (the two axes are
        independent in the real game, so sharing one draw stream across
        configs is not a correctness issue). `random.Random` with a string
        seed is documented to be stable across interpreter runs and
        platforms (it is not `hash()`-based), which is what "the same rules
        file always produces the same table" requires.
        """
        seed = f"{self.rules.fingerprint}:{int(self.mode)}:{hp_player}:{hp_dealer}:{max_hp}"
        rng = random.Random(seed)
        n = self.rules.solver.reload_item_draw_samples
        item_list = self._item_list()
        out: list[list[tuple[ItemCounts, ItemCounts]]] = []
        for _ in configs:
            draws = [
                (
                    _draw_items(rng, self.rules, item_list),
                    _draw_items(rng, self.rules, item_list),
                )
                for _ in range(n)
            ]
            out.append(draws)
        return out

    def _build_reload_state(
        self,
        hp_player: int,
        hp_dealer: int,
        max_hp: int,
        live: int,
        blank: int,
        items_player: ItemCounts,
        items_dealer: ItemCounts,
    ) -> GameState:
        """A fresh post-reload `GameState` (SPEC.md §7.4 step 4).

        `cuffed` is `(False, False)`, which is correct exactly when
        `rules.round.cuffs_clear_on_reload` is true. If it is false, a cuff
        can survive into the fresh load, and that is state the reload table's
        3-D key `(hp_player, hp_dealer, max_hp)` cannot carry — honoring it
        would need a fourth dimension the design deliberately does not have
        (SPEC.md §15.12). There is no approximation to fall back on that
        would not silently misprice every reload, so this raises rather than
        ignoring the flag, on the same principle as `_shell_configs`: a
        `rules.toml` key with no implementation behind it must fail loudly,
        never be read and discarded (SPEC.md §0.1 (2)).
        """
        if not self.rules.round_.cuffs_clear_on_reload:
            raise NotImplementedError(
                "reload_value: round.cuffs_clear_on_reload is false, but the "
                "reload table is keyed on health alone and cannot represent a "
                "cuff surviving into the next load — see SPEC.md §15.12"
            )
        chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
        turn = (
            Player.PLAYER
            if self.rules.round_.player_moves_first_after_reload
            else Player.DEALER
        )
        return GameState.create(
            chamber=chamber,
            hp=(hp_player, hp_dealer),
            max_hp=max_hp,
            turn=turn,
            items=(items_player, items_dealer),
            sawed=False,
            cuffed=(False, False),
            dealer_knows=frozenset(),
            items_used_this_turn=0,
        )

    def _reload_workers(self) -> int:
        """Worker count for the reload sweep's `for key in keys` loop (SPEC.md §7.7 item 1).

        Resolution order, so the dial is configurable without any worker
        count being hardcoded (SPEC.md §0.1 (2)):

        1. `rules.solver.reload_workers`. `-1` is the "auto" sentinel
           (TOML has no null, and a literal core count in a shared
           `rules.toml` would be a machine-specific constant); `0` and `1`
           both mean serial, per the milestone brief's contract; any other
           positive `n` means exactly `n` workers.
        2. the `BUCKSHOTCALC_RELOAD_WORKERS` environment variable, for a
           per-run override that does not touch the committed config.
        3. `os.cpu_count()` — every visible core. This is a one-time offline
           precompute whose result is cached to disk (SPEC.md §7.4), not
           interactive work sharing the machine with anything latency
           sensitive, so there is no reason to reserve a core by default.
           `os.cpu_count()` (not `os.process_cpu_count()`, Python 3.13+ only)
           is used because it is available on every CPython/PyPy >= 3.11
           this precompute must run under.
        """
        configured = getattr(self.rules.solver, "reload_workers", None)
        if configured is not None:
            if not isinstance(configured, int) or isinstance(configured, bool) or configured < -1:
                raise ValueError(
                    "rules.solver.reload_workers must be an int >= -1 (-1 = auto), "
                    f"got {configured!r}"
                )
            if configured >= 0:
                return configured
            # -1 falls through to the env var, then to auto.

        env = os.environ.get("BUCKSHOTCALC_RELOAD_WORKERS")
        if env is not None:
            try:
                n = int(env)
            except ValueError:
                raise ValueError(
                    f"BUCKSHOTCALC_RELOAD_WORKERS must be an int, got {env!r}"
                ) from None
            if n < 0:
                raise ValueError(f"BUCKSHOTCALC_RELOAD_WORKERS must be >= 0, got {n}")
            return n

        return os.cpu_count() or 1

    def _warm_start_table(
        self, progress: ProgressFn
    ) -> dict[tuple[int, int, int], float] | None:
        """A converged coarse table to seed the full build from (decisions-pending 12 / 13d).

        Built (or read) as its own table under the fingerprint
        `<fingerprint>+samplesN`, the same convention `sim/reload_bench.py`
        uses, so it caches separately and never answers for the full table.
        `None` when the key is 0 or not below the full sample count.
        """
        n = self.rules.solver.reload_warm_start_samples
        if not 0 < n < self.rules.solver.reload_item_draw_samples:
            return None
        coarse = dataclasses.replace(
            self.rules,
            solver=dataclasses.replace(self.rules.solver, reload_item_draw_samples=n),
            fingerprint=f"{self.rules.fingerprint}+samples{n}",
        )
        stage = f"warm start, {n} samples"
        sink = None if progress is None else lambda p: progress(dataclasses.replace(p, stage=stage))
        return Solver(coarse, self.mode)._compute_reload_table(sink, warm_start=False)

    def _compute_reload_table(
        self,
        progress: ProgressFn = None,
        v0: dict[tuple[int, int, int], float] | None = None,
        warm_start: bool = True,
    ) -> dict[tuple[int, int, int], float]:
        """Fixed-point solve of `V_reload` (SPEC.md §7.4), serial or parallel.

        Jacobi-style iteration: every sweep reads a frozen snapshot of the
        previous sweep's `V` (never the in-progress `new_V`), so the result
        does not depend on the order table entries — or table *keys* — are
        visited in. That is exactly what makes the `for key in keys` loop
        below the safe parallel unit (SPEC.md §7.7 item 1): no key's result
        within a sweep feeds another's, so farming keys to worker processes
        cannot perturb the fixed point. `_reload_sweeps_serial` and
        `_reload_sweeps_parallel` must therefore produce bit-for-bit
        identical tables — proven by
        `tests/test_solver.py::test_reload_table_parallel_matches_serial`.
        """
        cached = _read_cache(self.rules, self.mode)
        if cached is not None:
            return cached

        rules = self.rules
        keys = [
            (hp_p, hp_d, max_hp)
            for max_hp in rules.health.possible_max_hp
            for hp_p in range(1, max_hp + 1)
            for hp_d in range(1, max_hp + 1)
        ]
        if v0 is None and warm_start:
            v0 = _read_stale_cache(rules, self.mode) or self._warm_start_table(progress)
        if v0 is None:
            v0 = {key: key[0] / (key[0] + key[1]) for key in keys}

        workers = self._reload_workers()
        if workers <= 1:
            v = self._reload_sweeps_serial(keys, v0, progress)
        else:
            v = self._reload_sweeps_parallel(keys, v0, workers, progress)

        _write_cache(rules, self.mode, v)
        return v

    def _reload_sweeps_serial(
        self,
        keys: list[tuple[int, int, int]],
        v: dict[tuple[int, int, int], float],
        progress: ProgressFn = None,
    ) -> dict[tuple[int, int, int], float]:
        """Single-process sweep loop — the original implementation, unchanged in behaviour.

        Kept as a first-class path, not a vestigial fallback: debugging a
        wrong fixed-point value inside a worker pool is miserable, and this
        is exactly the kind of thing that will need debugging (milestone
        brief). `_reload_workers() <= 1` routes here. The per-sweep search
        memo (`scratch_memo`) is rebuilt from scratch every sweep — reusing
        it across sweeps would memoize values computed under a stale `V`,
        which is exactly the poisoning the milestone brief warns about. It
        is discarded once the sweep's `new_v` is collected; it never touches
        `self._memo`, which is reserved for the live search over the
        *finished* table.
        """
        rules = self.rules
        configs = _shell_configs(rules.shells)
        samples = {key: self._draw_samples(*key, configs) for key in keys}
        started = time.perf_counter()
        hist = [v]

        for sweep in range(1, rules.solver.reload_max_iterations + 1):
            snapshot = v  # frozen for this sweep; `new_v` never mutates it

            def reload_fn(hp_p: int, hp_d: int, mhp: int, _v=snapshot) -> float:
                return _v[(hp_p, hp_d, mhp)]

            scratch_memo: dict[int, tuple[float, bool]] = {}
            new_v: dict[tuple[int, int, int], float] = {}
            for key in keys:
                hp_p, hp_d, max_hp = key
                total = 0.0
                for (w_cfg, live, blank), draws in zip(configs, samples[key], strict=True):
                    s = 0.0
                    for items_p, items_d in draws:
                        state = self._build_reload_state(
                            hp_p, hp_d, max_hp, live, blank, items_p, items_d
                        )
                        val, _ = self._expectimax(
                            state, scratch_memo, reload_fn, count_nodes=False
                        )
                        s += val
                    total += w_cfg * (s / len(draws))
                new_v[key] = total
                if progress is not None:
                    progress(
                        ReloadProgress(
                            phase="key",
                            sweep=sweep,
                            sweeps_max=rules.solver.reload_max_iterations,
                            keys_done=len(new_v),
                            keys_total=len(keys),
                            elapsed_s=time.perf_counter() - started,
                            delta=None,
                            tolerance=rules.solver.reload_fixedpoint_tolerance,
                        )
                    )

            delta = max(abs(new_v[k] - v[k]) for k in keys)
            v = new_v
            if progress is not None:
                progress(
                    ReloadProgress(
                        phase="sweep",
                        sweep=sweep,
                        sweeps_max=rules.solver.reload_max_iterations,
                        keys_done=len(keys),
                        keys_total=len(keys),
                        elapsed_s=time.perf_counter() - started,
                        delta=delta,
                        tolerance=rules.solver.reload_fixedpoint_tolerance,
                    )
                )
            if delta < rules.solver.reload_fixedpoint_tolerance:
                break
            if rules.solver.reload_aitken:
                v, hist = _aitken_next(hist, v)

        return v

    def _reload_sweeps_parallel(
        self,
        keys: list[tuple[int, int, int]],
        v: dict[tuple[int, int, int], float],
        workers: int,
        progress: ProgressFn = None,
    ) -> dict[tuple[int, int, int], float]:
        """Same fixed point, the sweep farmed across `workers` processes.

        The unit of work is one (key, shell config) pair, heaviest first, so
        no core idles behind one key's last big config; the parent recombines
        a key's per-config parts in `_shell_configs` order (see
        `_mp_solve_config`).

        Stdlib `multiprocessing` only (SPEC.md §0.1 (3)). Each
        task ships only a key and the current sweep's small snapshot dict
        (29 floats at shipped scale) — never a `Solver`, its memo, or its
        partially-built table, which is the picklability concern the
        milestone brief flags; `_mp_init` reconstructs a fresh `Solver` from
        `(rules, mode)` once per worker process instead. Every other input a
        task needs (`configs`, that key's deterministic item-draw samples)
        is recomputed inside the worker from `(rules, key)` rather than
        pickled over, both because it is cheap to recompute and because it
        keeps what crosses the process boundary small.

        This is the trade the milestone brief asks to be measured, not
        assumed: the serial path's `scratch_memo` is shared across all keys
        within one sweep; here each worker keeps one memo for its tasks within a
        sweep (13b), but tasks on different workers cannot share, so total nodes
        evaluated goes up. `self._last_parallel_node_count` records the
        total (summed over every sweep actually run) so the benchmark
        harness can report the size of that loss instead of guessing at it.
        Memory is the flip side of the same trade: a worker never holds more
        than one key's memo at a time, which is what bounds peak memory
        instead of the union of all keys' memos (SPEC.md §7.7's ~15 M-entry
        extrapolation for the serial, single-shared-memo design).

        `mp.get_context("spawn")` rather than the platform default: spawn
        pickles `initargs` to build each worker fresh instead of forking the
        parent's memory image, which is the more portable behaviour across
        CPython and PyPy (see the milestone report's PyPy note) and avoids
        any dependence on fork's copy-on-write semantics, which this code
        does not need and should not lean on.
        """
        rules = self.rules
        ctx = mp.get_context("spawn")
        configs = _shell_configs(rules.shells)
        total_node_count = 0
        started = time.perf_counter()
        # Per-task wall time from the previous sweep, the honest weight for
        # "heaviest first". Sweep 1 has no measurement, so it falls back to
        # a static proxy (shells in the load x max_hp): the tree grows with
        # both. ASSUMED proxy; only affects scheduling, never a value.
        cost: dict[tuple[tuple[int, int, int], int], float] = {}
        hist = [v]
        with ctx.Pool(
            processes=workers, initializer=_mp_init, initargs=(rules, self.mode)
        ) as pool:
            for sweep in range(1, rules.solver.reload_max_iterations + 1):
                snapshot = v
                units = [(key, i) for key in keys for i in range(len(configs))]
                units.sort(
                    key=lambda u: -cost.get(
                        u, (configs[u[1]][1] + configs[u[1]][2]) * u[0][2]
                    )
                )
                tasks = [(key, i, snapshot, sweep) for key, i in units]
                partial: dict[tuple[int, int, int], dict[int, float]] = {k: {} for k in keys}
                new_v: dict[tuple[int, int, int], float] = {}
                tasks_done = 0
                # `imap_unordered`, not `starmap`: starmap only returns once
                # every task is done, which is precisely the blackout this
                # progress reporting exists to end. Completion order does not
                # affect the result: a key's configs are recombined below in
                # `_shell_configs` order, never in arrival order, so the
                # float sum is the serial path's, bit for bit (asserted by
                # test_reload_table_parallel_matches_serial).
                for key, i, part, memo_size, took in pool.imap_unordered(
                    _mp_solve_config_packed, tasks, chunksize=1
                ):
                    cost[(key, i)] = took
                    total_node_count += memo_size
                    tasks_done += 1
                    done = partial[key]
                    done[i] = part
                    if len(done) == len(configs):
                        total = 0.0
                        for j, (w_cfg, _live, _blank) in enumerate(configs):
                            total += w_cfg * done[j]
                        new_v[key] = total
                    # Heaviest-first scheduling finishes every key's lightest
                    # config last, so whole keys complete in one burst at the
                    # sweep's end. Count finished tasks instead, scaled to
                    # keys, so the number tracks the work actually done.
                    if progress is not None:
                        progress(
                            ReloadProgress(
                                phase="key",
                                sweep=sweep,
                                sweeps_max=rules.solver.reload_max_iterations,
                                keys_done=tasks_done * len(keys) // len(tasks),
                                keys_total=len(keys),
                                elapsed_s=time.perf_counter() - started,
                                delta=None,
                                tolerance=rules.solver.reload_fixedpoint_tolerance,
                            )
                        )

                delta = max(abs(new_v[k] - v[k]) for k in keys)
                v = new_v
                if progress is not None:
                    progress(
                        ReloadProgress(
                            phase="sweep",
                            sweep=sweep,
                            sweeps_max=rules.solver.reload_max_iterations,
                            keys_done=len(keys),
                            keys_total=len(keys),
                            elapsed_s=time.perf_counter() - started,
                            delta=delta,
                            tolerance=rules.solver.reload_fixedpoint_tolerance,
                        )
                    )
                if delta < rules.solver.reload_fixedpoint_tolerance:
                    break
                if rules.solver.reload_aitken:
                    v, hist = _aitken_next(hist, v)

        self._last_parallel_node_count = total_node_count
        return v


# ---------------------------------------------------------------------------
# Multiprocessing worker functions (SPEC.md §7.7 item 1) — module level so
# they are picklable by `multiprocessing`'s "spawn" start method.
# ---------------------------------------------------------------------------

#: One `Solver` per worker process, built once by `_mp_init`. Never the
#: parent's `Solver` — only `(rules, mode)` cross the process boundary, and a
#: fresh instance is reconstructed on the other side (see
#: `Solver._reload_sweeps_parallel`'s docstring on picklability).
_mp_solver: Solver | None = None


_mp_memo: dict[int, tuple[float, bool]] = {}
_mp_memo_sweep = 0


def _mp_init(rules: Rules, mode: GameMode) -> None:
    """Pool initializer: build this worker's own `Solver` (SPEC.md §7.7 item 1).

    Runs once per worker process, not once per task. The worker's `Solver`
    is used only for its pure recursion (`_expectimax`, `_build_reload_state`,
    `_draw_samples`) with an explicit scratch memo supplied per task — its
    own `self._memo`/`self._reload_table` are never touched here, so nothing
    grows unboundedly across the tasks one worker happens to process.
    """
    global _mp_solver
    _mp_solver = Solver(rules, mode)


def _mp_solve_config_packed(
    args: tuple[tuple[int, int, int], int, dict[tuple[int, int, int], float], int],
) -> tuple[tuple[int, int, int], int, float, int, float]:
    """`_mp_solve_config` with its arguments packed into one tuple (for `imap_unordered`)."""
    return _mp_solve_config(*args)


def _mp_solve_config(
    key: tuple[int, int, int],
    cfg_index: int,
    snapshot: dict[tuple[int, int, int], float],
    sweep: int,
) -> tuple[tuple[int, int, int], int, float, int, float]:
    """One (table key, shell config) unit of one sweep, run inside a worker (SPEC.md §7.7).

    Returns `s / len(draws)` for that config, where `s` accumulates the
    config's draws in order from `0.0` exactly as the serial loop does; the
    parent applies `total += w_cfg * part` in `_shell_configs` order, so the
    recombined key value is bit-identical to the serial path's. Also returns
    the memo size (the node proxy) and this task's wall time (the parent's
    scheduling weight for the next sweep).
    """
    t0 = time.perf_counter()
    global _mp_memo, _mp_memo_sweep
    solver = _mp_solver
    assert solver is not None, "_mp_solve_config called before the pool initializer ran"
    rules = solver.rules
    configs = _shell_configs(rules.shells)
    hp_p, hp_d, max_hp = key
    _w_cfg, live, blank = configs[cfg_index]
    draws = solver._draw_samples(hp_p, hp_d, max_hp, configs)[cfg_index]

    def reload_fn(hpp: int, hpd: int, mhp: int, _v=snapshot) -> float:
        return _v[(hpp, hpd, mhp)]

    # Per-worker memo shared across tasks within one sweep, as the serial
    # path shares `scratch_memo` across keys (13b). Entries depend on the
    # sweep's snapshot, so a new sweep starts a fresh one. A value is a pure
    # function of (state, snapshot): sharing changes cost, never the answer.
    if _mp_memo_sweep != sweep:
        _mp_memo, _mp_memo_sweep = {}, sweep
    memo = _mp_memo
    before = len(memo)
    s = 0.0
    for items_p, items_d in draws:
        state = solver._build_reload_state(hp_p, hp_d, max_hp, live, blank, items_p, items_d)
        val, _ = solver._expectimax(state, memo, reload_fn, count_nodes=False)
        s += val
    return key, cfg_index, s / len(draws), len(memo) - before, time.perf_counter() - t0
