"""Presentation-neutral derivation of what SPEC.md §8 displays.

Pure stdlib (SPEC.md §0.1, M7 brief) — turns a `SearchResult` and a `GameState`
into small, render-ready data. `panels.py` (textual/rich) consumes these
types; nothing here formats a colour or draws a box.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from buckshotcalc.engine.actions import Action, ActionKind, apply
from buckshotcalc.engine.dealer import dealer_step
from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.solver import ReloadProgress, SearchResult
from buckshotcalc.engine.state import GameState, Item, Player, Shell


def action_label(action: Action, seat: Player) -> str:
    """Seat-aware label for `action`, taken by `seat` (SPEC.md §8's mock).

    `Action.__str__` is deliberately seat-neutral ("SHOOT OPPONENT") — a
    defect fixed in M7 (it used to hardcode "SHOOT DEALER" even for the
    Dealer's own action against us). This is where seat context is applied:
    the operator's own `SHOOT_OPPONENT` reads "SHOOT DEALER"; the same
    action taken from the Dealer's seat reads "SHOOT PLAYER".
    """
    if action.kind is ActionKind.SHOOT_OPPONENT:
        target = "DEALER" if seat is Player.PLAYER else "PLAYER"
        return f"SHOOT {target}"
    return str(action)


def death_chance(state: GameState, action: Action, rules: Rules) -> float:
    """Chance `action` at `state` leaves the player at 0 HP as its direct result.

    Summed weight of `apply`'s outcomes with player HP 0 (decisions-pending
    item 15(a)). Immediate risk only, no recursion: the Dealer's reply is not
    counted, and shooting the Dealer is 0 by this definition.
    """
    return sum((w for w, s in apply(state, action, rules) if s.hp[0] <= 0), 0.0)


@dataclass(frozen=True, slots=True)
class RankedAlternative:
    """One root action, ready for the ranked-alternatives panel (SPEC.md §8)."""

    label: str
    value: float
    wasted: bool
    is_best: bool
    dies: float = 0.0  # item 15(a): chance this move itself kills the player


def ranked_alternatives(
    result: SearchResult, seat: Player, state: GameState | None = None, rules: Rules | None = None
) -> tuple[RankedAlternative, ...]:
    """`result.children` (already descending by value) as display rows.

    Empty when `result` carries no children — a terminal state, a reload
    boundary, or a Dealer-turn node (per `SearchResult.children`'s contract).
    """
    if not result.children:
        return ()
    return tuple(
        RankedAlternative(
            label=action_label(c.action, seat),
            value=c.value,
            wasted=c.wasted,
            is_best=(i == 0),
            dies=dies_for(state, c.action, rules, seat),
        )
        for i, c in enumerate(result.children)
    )


def dies_for(state: GameState | None, action: Action, rules: Rules | None, seat: Player) -> float:
    """`death_chance` for a display row; 0 when the view has no matching result (`rules` None)."""
    if state is None or rules is None or seat is not Player.PLAYER or state.turn is not seat:
        return 0.0
    return death_chance(state, action, rules)


def dies_text(dies: float) -> str:
    """Plain-English suffix for a move's immediate death chance; empty at 0."""
    return f"dies {dies * 100:.0f}%" if dies > 0 else ""


@dataclass(frozen=True, slots=True)
class ItemValuation:
    """One `USE`/Adrenaline-steal child, for the `[a]` analysis board's per-item table."""

    item: Item
    steal: Item | None
    label: str
    value: float
    wasted: bool


def item_valuations(result: SearchResult, seat: Player) -> tuple[ItemValuation, ...]:
    """The subset of `result.children` that are item uses, in `result`'s order."""
    out = []
    for c in result.children:
        if c.action.kind is ActionKind.USE:
            assert c.action.item is not None
            out.append(
                ItemValuation(
                    item=c.action.item,
                    steal=c.action.steal,
                    label=action_label(c.action, seat),
                    value=c.value,
                    wasted=c.wasted,
                )
            )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SlotProbability:
    """One chamber slot's live-probability, for the `[a]` per-slot table."""

    index: int
    p_live: float
    resolved: Shell | None  # None iff still unresolved


def slot_probabilities(state: GameState) -> tuple[SlotProbability, ...]:
    """`P(live)` for every remaining chamber slot, resolved or not."""
    chamber = state.chamber
    total = chamber.unknown_live + chamber.unknown_blank
    pool_p_live = (chamber.unknown_live / total) if total else 0.0
    out = []
    for i, s in enumerate(chamber.slots):
        if s is not None:
            out.append(SlotProbability(index=i, p_live=float(s == Shell.LIVE), resolved=s))
        else:
            out.append(SlotProbability(index=i, p_live=pool_p_live, resolved=None))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ChamberSummary:
    """P(live)/P(blank) for the chambered shell, plus the raw live/blank totals."""

    p_live: float
    total_live: int
    total_blank: int


def chamber_summary(state: GameState) -> ChamberSummary:
    chamber = state.chamber
    if chamber.is_empty:
        return ChamberSummary(p_live=0.0, total_live=0, total_blank=0)
    return ChamberSummary(
        p_live=chamber.p_live(),
        total_live=chamber.total_live(),
        total_blank=chamber.total_blank(),
    )


# ---------------------------------------------------------------------------
# Plain names — the words the operator uses out loud, not the config keys.
# Lives here rather than in `panels.py` so the log narration in `session.py`
# (stdlib) can say "a saw" too, and so the two can never drift apart.
# ---------------------------------------------------------------------------

#: `Item.config_name` -> the word shown on screen. A `config_name` absent from
#: this table falls back to itself with underscores spaced out, so a new item
#: is readable before anyone edits this.
ITEM_DISPLAY_NAMES: dict[str, str] = {
    "cigarettes": "cigarettes",
    "handcuffs": "handcuffs",
    "beer": "beer",
    "handsaw": "saw",
    "magnifying_glass": "magnifier",
    "adrenaline": "adrenaline",
    "expired_medicine": "medicine",
    "burner_phone": "phone",
    "inverter": "inverter",
    "remote": "remote",
}


def item_display_name(item: Item) -> str:
    """`magnifying_glass` -> `magnifier`."""
    return ITEM_DISPLAY_NAMES.get(item.config_name, item.config_name.replace("_", " "))


# ---------------------------------------------------------------------------
# Item letter map — operator decision (2026-09-05 redesign): items own every
# plain letter key. Nothing else on the dashboard binds a bare letter, so each
# item can keep the letter an operator would guess (`b` beer, `s` saw, `p`
# phone) instead of whatever survived a reserved-key sweep. Shots moved to the
# arrow keys and the admin commands moved to ctrl-chords precisely so this
# table could be mnemonic — see `app.BuckshotApp.on_key`.
#
# Derived from each item's own `config_name` (engine-owned, not a `rules.toml`
# value) with a preferred-initial table in front of it, never a hardcoded
# per-item dict of the whole enum: an item added to `Item` still gets a stable
# letter without editing anything here.
# ---------------------------------------------------------------------------

#: Bare letters bound to something other than an item. Empty by design: the
#: redesign moved every non-item binding onto arrows, `space`, `-` and
#: ctrl-chords so this set could stay empty and the mnemonics could win.
RESERVED_KEYS: frozenset[str] = frozenset()

#: First choice of letter per `Item.config_name`. A `config_name` absent from
#: this table (a future item) falls through to its own letters, exactly as
#: before. Every entry here is just the letter a person would reach for.
PREFERRED_KEYS: dict[str, str] = {
    "cigarettes": "c",
    "handcuffs": "h",
    "beer": "b",
    "handsaw": "s",
    "magnifying_glass": "m",
    "adrenaline": "a",
    "expired_medicine": "x",
    "burner_phone": "p",
    "inverter": "i",
    "remote": "r",
}

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def build_item_key_map(items: tuple[Item, ...] = tuple(Item)) -> dict[Item, str]:
    """A stable `Item -> single letter` map, dodging `RESERVED_KEYS`.

    Deterministic, and tried in three passes per item: its `PREFERRED_KEYS`
    entry, then the letters of its own `config_name` in order, then the first
    unused letter of the alphabet. No two items ever share a letter.
    """
    taken: set[str] = set()
    out: dict[Item, str] = {}
    for item in items:
        letter = None
        preferred = PREFERRED_KEYS.get(item.config_name)
        if preferred is not None and preferred not in RESERVED_KEYS and preferred not in taken:
            letter = preferred
        if letter is None:
            for ch in item.config_name:
                if ch.isalpha() and ch not in RESERVED_KEYS and ch not in taken:
                    letter = ch
                    break
        if letter is None:
            for ch in _ALPHABET:
                if ch not in RESERVED_KEYS and ch not in taken:
                    letter = ch
                    break
        if letter is None:  # pragma: no cover - exhausted a-z, never happens with 10 items
            raise RuntimeError("build_item_key_map: exhausted available letters")
        taken.add(letter)
        out[item] = letter
    return out


def _fmt_minutes(seconds: float) -> str:
    """`about 8 min` / `about 1 h 20 min` / `under a minute`: read across a desk."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"about {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes:02d} min"


class ReloadBuildTracker:
    """Turns the build's `ReloadProgress` events into one plain sentence (decision 5).

    It estimates the time left the same way `sim/reload_bench.py`'s printer
    does. Jacobi converges linearly (SPEC.md §7.7), so two consecutive
    residuals give the decay ratio `r`, and then `sweeps_left = log(tol /
    delta) / log(r)`. With fewer than two sweeps, or a residual that is not
    falling, it says the time is not known yet rather than guessing.
    Residuals reset when the `stage` changes, because a warm-start pass
    converges on its own table.
    """

    def __init__(self) -> None:
        self._last: ReloadProgress | None = None
        self._stage = "main"
        self._deltas: list[float] = []
        self._sweep_end_s: list[float] = []

    def update(self, event: ReloadProgress) -> None:
        stage = getattr(event, "stage", "main")
        if stage != self._stage:
            self._stage, self._deltas, self._sweep_end_s = stage, [], []
        if event.phase == "sweep" and event.delta is not None:
            self._deltas.append(event.delta)
            self._sweep_end_s.append(event.elapsed_s)
        self._last = event

    def seconds_left(self) -> float | None:
        p = self._last
        if p is None or len(self._deltas) < 2 or self._stage != "main":
            return None
        prev, cur = self._deltas[-2], self._deltas[-1]
        if cur < p.tolerance:
            return 0.0
        if not 0 < cur < prev:
            return None
        sweeps = math.log(p.tolerance / cur) / math.log(cur / prev)
        sweeps = min(sweeps, p.sweeps_max - len(self._deltas))
        first = self._sweep_end_s[0]
        per_sweep = (self._sweep_end_s[-1] - first) / (len(self._sweep_end_s) - 1) or first
        into_this_sweep = p.elapsed_s - self._sweep_end_s[-1]
        return max(0.0, sweeps * per_sweep - into_this_sweep)

    def describe(self, now: datetime | None = None) -> str:
        p = self._last
        if p is None:
            return "starting the reload-table build…"
        where = "" if self._stage == "main" else f"{self._stage}: "
        # A finished sweep means the next pass has started with nothing done yet.
        done = 0 if p.phase == "sweep" else p.keys_done
        sweep = p.sweep + 1 if p.phase == "sweep" else p.sweep
        text = f"{where}pass {sweep} · {done} of {p.keys_total} table entries done"
        left = self.seconds_left()
        if left is None:
            return text + " · time left not known yet"
        finish = (now or datetime.now()) + timedelta(seconds=left)
        return text + f" · {_fmt_minutes(left)} left (done around {finish:%H:%M})"


# ---------------------------------------------------------------------------
# Watch-mode panels: best-line plan tree, Dealer forecast, round recap.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One step of the best line.

    `label` is how we got here ("live", "blank", "" for the root or a
    deterministic action); `weight` is the probability of that outcome given
    the parent's action. `action` is what to do now (None when the line ends);
    `end` says why it ended ("dealer's turn", "reload", "you win", "you lose",
    "...") and is None while `action` is set. `children` are the outcomes of
    `action`.
    """

    label: str
    weight: float
    action: str | None
    end: str | None
    children: tuple[PlanNode, ...] = ()


def _outcome_label(before: GameState, action: Action, after: GameState) -> str:
    """Plain word for which way a chance outcome went; "" if not a reveal."""
    if action.kind is not ActionKind.USE:  # a shot: only a live shell costs HP
        return "live" if after.hp != before.hp else "blank"
    item = action.item
    if item is Item.MAGNIFYING_GLASS:
        return "live" if after.chamber.slots[0] is Shell.LIVE else "blank"
    if item is Item.INVERTER:
        return "now live" if after.chamber.slots[0] is Shell.LIVE else "now blank"
    if item is Item.BEER:
        gone_live = after.chamber.total_live() < before.chamber.total_live()
        return "ejected live" if gone_live else "ejected blank"
    if item is Item.BURNER_PHONE:
        for i, s in enumerate(after.chamber.slots):
            old = before.chamber.slots[i] if i < len(before.chamber.slots) else None
            if s is not None and old is None:
                return f"shell {i + 1} {'live' if s is Shell.LIVE else 'blank'}"
    return ""


def _plan_end(s: GameState) -> str | None:
    w = s.winner()
    if w is not None:
        return "you win" if w is Player.PLAYER else "you lose"
    if s.chamber.is_empty:
        return "reload"
    if s.turn is Player.DEALER:
        return "dealer's turn"
    return None


def _plan_key(n: PlanNode):
    return (n.action, n.end, tuple(_plan_key(c) for c in n.children))


def _merge_siblings(nodes: list[PlanNode]) -> tuple[PlanNode, ...]:
    """Fold siblings with the same next action and same subtree shape into one."""
    groups: dict = {}
    for n in nodes:
        groups.setdefault(_plan_key(n), []).append(n)
    out = []
    for grp in groups.values():
        if len(grp) == 1:
            out.append(grp[0])
            continue
        w = sum(n.weight for n in grp)
        kids = tuple(
            PlanNode(
                c.label,
                sum(n.children[i].weight * n.weight for n in grp) / w,
                c.action,
                c.end,
                c.children,
            )
            for i, c in enumerate(grp[0].children)
        )
        label = " / ".join(dict.fromkeys(n.label for n in grp))
        out.append(PlanNode(label, w, grp[0].action, grp[0].end, kids))
    return tuple(out)


def _plan_step(solver, state: GameState, label: str, weight: float, left: int) -> PlanNode:
    end = _plan_end(state)
    if end is not None:
        return PlanNode(label, weight, None, end)
    if left == 0:
        return PlanNode(label, weight, None, "...")
    best = solver.evaluate(state).children[0].action
    outs = apply(state, best, solver.rules)
    kids = [
        _plan_step(solver, s, _outcome_label(state, best, s) if len(outs) > 1 else "", w, left - 1)
        for w, s in outs
    ]
    return PlanNode(label, weight, action_label(best, Player.PLAYER), None, _merge_siblings(kids))


def plan_tree(solver, state: GameState, depth: int = 3) -> PlanNode:
    """The best line for the player's current turn, up to `depth` actions.

    Follows `evaluate`'s top child at each step, branching on chance outcomes.
    Only meaningful at a live player decision node; otherwise returns a
    single end node ("dealer's turn", "reload", ...).
    """
    return _plan_step(solver, state.canonical(), "", 1.0, depth)


@dataclass(frozen=True, slots=True)
class ForecastRow:
    """One thing the Dealer might do first, with our belief it does it."""

    label: str
    probability: float
    win_after: float | None  # our win chance afterwards, if a solver was given


def _dealer_label(action: Action) -> str:
    if action.kind is ActionKind.SHOOT_OPPONENT:
        return "shoots you"
    if action.kind is ActionKind.SHOOT_SELF:
        return "shoots itself"
    assert action.item is not None
    return f"uses {item_display_name(action.item)}"


def dealer_forecast(state: GameState, rules, solver=None) -> tuple[ForecastRow, ...]:
    """The Dealer's first action, merged by kind, most likely first.

    Weights are from our belief (`dealer_step`'s own weights). With a `solver`,
    `win_after` is the probability-weighted value of the successors.
    """
    merged: dict[str, list[float]] = {}
    for weight, actions, successor in dealer_step(state, rules):
        label = _dealer_label(actions[0])
        m = merged.setdefault(label, [0.0, 0.0])
        m[0] += weight
        if solver is not None:
            m[1] += weight * solver.value(successor)
    rows = [
        ForecastRow(label, p, (v / p) if solver is not None and p else None)
        for label, (p, v) in merged.items()
    ]
    return tuple(sorted(rows, key=lambda r: -r.probability))


@dataclass(frozen=True, slots=True)
class RecapRow:
    """One of our moves: what the solver said, what we did, what it cost."""

    recommended: str
    recommended_value: float
    taken: str
    value_after: float
    followed: bool


class RoundRecap:
    """Accumulates a round's moves for the end-of-round review."""

    def __init__(self) -> None:
        self._rows: list[RecapRow] = []

    def record(
        self,
        before_state: GameState,
        recommended_action: Action,
        recommended_value: float,
        taken_action: Action,
        value_after: float,
    ) -> None:
        seat = before_state.turn
        self._rows.append(
            RecapRow(
                action_label(recommended_action, seat),
                recommended_value,
                action_label(taken_action, seat),
                value_after,
                recommended_action == taken_action,
            )
        )

    def rows(self) -> tuple[RecapRow, ...]:
        return tuple(self._rows)

    def summary(self) -> tuple[int, int]:
        """`(moves followed, total moves)`."""
        return sum(r.followed for r in self._rows), len(self._rows)
