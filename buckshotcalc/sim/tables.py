"""Item-value reference table generation (SPEC.md §11, §14 M8).

Pure stdlib (SPEC.md §0.1 #3). This module answers a narrower question than the
solver itself: not "what should I do right now", but "roughly what is each
item worth, in the kinds of positions that come up early in a round" — a
static cheat sheet an operator can read once and internalise, rather than a
per-position query.

SPEC.md §14 says only "item-value tables. Generated cheat sheet for common
positions" and defines "common" nowhere. This module does not invent a
definition silently, and it does not pad the sheet to look comprehensive
(the M8 brief). It covers exactly this family, defined by a rule
rather than by example:

    A *fresh, unresolved load* (no chamber slot resolved) at *full health for
    both players* (hp = hp = max_hp), for every `max_hp` in
    `rules.health.possible_max_hp`, across the `_POSITION_TOTAL_COUNT`
    smallest legal shell totals in `[rules.shells.min_total,
    rules.shells.max_total]` and every legal live/blank split of each such
    total (SPEC.md §3.1 step 3's split rule, same as the solver's own
    `_shell_configs`). Neither player holds any item except the one being
    valuated, which the PLAYER holds in a single unit; the DEALER holds
    nothing. Only `GameMode.BASE`'s item list (`rules.items.base_items`) is
    covered — Double or Nothing's and multiplayer's item lists are excluded.

Explicitly OUT of scope, and not silently extrapolated to: any position with
a resolved chamber slot (a shot has already happened), unequal or non-full
starting HP, holding more than one item at a time (so this sheet cannot show
item *combinations*, e.g. saw+handcuffs synergy), Adrenaline's steal target
(no second item exists to steal in a single-item holding), and any GameMode
other than BASE. `docs/decisions-pending.md` records this choice, what it
costs, and why — a cheat sheet's danger is that it outlives the session that
made it and gets trusted outside the range it covers, so both the module
docstring and the generated sheet's own header state the rule on their face.

Reload model (the hazard named in the M8 brief): `Solver.reload_value`'s real
table (SPEC.md §7.4) has never completed a build at shipped `rules.toml`
scale — ~19 min/sweep, an unknown number of sweeps, dying on memory around an
extrapolated ~15M memo entries (SPEC.md §7.7, docs/decisions-pending.md items
1-3). This module must therefore never call `Solver.reload_value` in a way
that could trigger `_compute_reload_table`. It injects the same closed-form
`hp_player / (hp_player + hp_dealer)` stand-in `sim/corpus.py` uses
(`_health_ratio_reload_table`, imported from there rather than duplicated —
both live in `sim/`, so there is no `sim/`-depends-on-`tests/` problem the
way there was for `corpus.py` borrowing from `tests/test_solver.py`) directly
into `Solver._reload_table` before any `evaluate()` call. This is a
stand-in, not the shipped model, and every rendered sheet says so in its
header: a number computed against a stand-in table is not the number the
shipped solver would give once the real `V_reload` exists, and a sheet that
hides that fact is worse than no sheet.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from buckshotcalc.engine.rules import Rules, default_rules, load_rules
from buckshotcalc.engine.solver import Solver, _shell_configs
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
)
from buckshotcalc.sim.corpus import _health_ratio_reload_table
from buckshotcalc.tui.analysis import action_label, item_valuations

# The number of smallest legal shell totals covered — an arbitrary bound
# (not a game rule) picked to keep the sheet "a glance", per the module
# docstring; see docs/decisions-pending.md for what was measured/considered.
_POSITION_TOTAL_COUNT = 3

#: Which `GameMode`'s item list this sheet covers. Base game only — see the
#: module docstring's "Explicitly OUT of scope" list.
_MODE = GameMode.BASE

_RELOAD_MODEL_NAME = (
    "health-ratio stand-in: P = hp_player / (hp_player + hp_dealer) "
    "(NOT the shipped V_reload fixed point; see docs/decisions-pending.md "
    "items 1-3 and SPEC.md §7.7)"
)


# ---------------------------------------------------------------------------
# Position family
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TablePosition:
    """One fresh-load position in the covered family: `max_hp`, and a shell split."""

    max_hp: int
    live: int
    blank: int

    @property
    def total(self) -> int:
        return self.live + self.blank

    @property
    def label(self) -> str:
        return f"HP {self.max_hp}/{self.max_hp}  {self.live}L{self.blank}B"


def _covered_totals(rules: Rules) -> tuple[int, ...]:
    """The `_POSITION_TOTAL_COUNT` smallest legal shell totals, ascending."""
    all_totals = list(range(rules.shells.min_total, rules.shells.max_total + 1))
    if not all_totals:
        raise ValueError("tables: no legal shell total in rules.shells' range")
    return tuple(all_totals[:_POSITION_TOTAL_COUNT])


def enumerate_positions(rules: Rules) -> tuple[TablePosition, ...]:
    """Every position in the covered family, in a fixed deterministic order.

    Order: `rules.health.possible_max_hp` as loaded, then shell splits in
    `_shell_configs`'s own order (ascending total; an odd total's extra shell
    is blank — SPEC.md §3.1 step 3) filtered to
    `_covered_totals`. Same `rules.toml` in, same order out (required for
    the byte-identical determinism the M8 brief asks for).
    """
    totals = _covered_totals(rules)
    splits = tuple(
        (live, blank) for _, live, blank in _shell_configs(rules.shells) if live + blank in totals
    )
    return tuple(
        TablePosition(max_hp=max_hp, live=live, blank=blank)
        for max_hp in rules.health.possible_max_hp
        for live, blank in splits
    )


def _fresh_state(position: TablePosition, rules: Rules) -> GameState:
    """A canonical `GameState` for `position`: fresh load, full HP, no items, PLAYER to move."""
    chamber = ChamberState.create((None,) * position.total, position.live, position.blank)
    return GameState.create(
        chamber=chamber,
        hp=(position.max_hp, position.max_hp),
        max_hp=position.max_hp,
        turn=Player.PLAYER,
    )


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemRow:
    """One item's valuation at one `TablePosition`."""

    position: TablePosition
    item: Item
    baseline_value: float  # best value with NO items held
    item_value: float  # value of HOLDING the item and playing best from there
    delta: float  # item_value - baseline_value: what the item is worth
    use_now_value: float  # value of USE(item) as the immediate move
    use_now_is_best: bool  # whether using it immediately is the best line here

    """Two different questions live here, and conflating them was a real bug.

    `item_value`/`delta` answer *"what is this item worth?"* — the value of
    holding it and playing well, which is the question a cheat sheet exists
    to answer. `use_now_value`/`use_now_is_best` answer the narrower *"should
    I spend it right now?"*.

    They diverge whenever an item is worth keeping for later. An earlier
    revision of this module reported the USE-now value in the `value` column,
    which understated every such item — the handsaw at HP 2/2 2L2B by half
    (+0.0833 reported against +0.1667 actual) and Cigarettes at full HP by
    all of it (+0.0000 reported in every position against +0.0972 actual,
    because using a heal at full HP does nothing while *holding* one across
    the round is worth a great deal).
    """


@dataclass(frozen=True, slots=True)
class PositionSummary:
    """All covered items' valuations at one `TablePosition`, plus the no-item baseline."""

    position: TablePosition
    baseline_action_label: str
    baseline_value: float
    rows: tuple[ItemRow, ...]


def valuate_position(
    position: TablePosition, rules: Rules, solver: Solver
) -> PositionSummary:
    """Value every `rules.items.base_items` item at `position` against `solver`.

    `solver` must already have a reload table injected (see `build_solver`)
    — this function never triggers `_compute_reload_table` itself, it only
    calls `evaluate()`.
    """
    baseline_state = _fresh_state(position, rules)
    baseline_result = solver.evaluate(baseline_state)
    baseline_value = baseline_result.value
    baseline_label = (
        action_label(baseline_result.children[0].action, Player.PLAYER)
        if baseline_result.children
        else "n/a"
    )

    rows: list[ItemRow] = []
    for item in rules.items.base_items:
        held = ItemCounts.empty().with_delta(item, 1)
        item_state = baseline_state.with_items(Player.PLAYER, held)
        result = solver.evaluate(item_state)
        valuations = item_valuations(result, Player.PLAYER)
        # Exactly one USE child exists: this single held item, no steal target
        # available (Adrenaline needs a second item to steal, which this
        # single-item family never holds).
        matches = [v for v in valuations if v.item is item]
        assert len(matches) == 1, (
            f"tables: expected exactly one USE({item.name}) child at {position.label}, "
            f"got {len(matches)}"
        )
        match = matches[0]
        rows.append(
            ItemRow(
                position=position,
                item=item,
                baseline_value=baseline_value,
                item_value=result.value,
                delta=result.value - baseline_value,
                use_now_value=match.value,
                use_now_is_best=not match.wasted,
            )
        )
    return PositionSummary(
        position=position,
        baseline_action_label=baseline_label,
        baseline_value=baseline_value,
        rows=tuple(rows),
    )


def build_solver(
    rules: Rules, reload_table: dict[tuple[int, int, int], float] | None = None
) -> Solver:
    """A `Solver` for `_MODE` with a reload table injected — never a built one.

    `reload_table` defaults to the health-ratio stand-in (see the module
    docstring). Passing one in lets a caller (e.g. tests) supply its own and
    assert it was actually used, per the M8 brief's test list.
    """
    solver = Solver(rules, _MODE)
    solver._reload_table = (
        reload_table if reload_table is not None else _health_ratio_reload_table(rules)
    )
    return solver


def generate_sheet(
    rules: Rules, reload_table: dict[tuple[int, int, int], float] | None = None
) -> tuple[PositionSummary, ...]:
    """The full M8 cheat sheet: every covered position, valuated.

    Never triggers `_compute_reload_table` (see `build_solver`).
    """
    solver = build_solver(rules, reload_table)
    return tuple(
        valuate_position(position, rules, solver) for position in enumerate_positions(rules)
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _header_lines(rules: Rules) -> tuple[str, ...]:
    """Lines every rendering shares: what this is, and what it is NOT (M8 brief)."""
    totals = _covered_totals(rules)
    return (
        "BuckshotCalc — item-value reference table (SPEC.md §14, M8)",
        f"rules fingerprint: {rules.fingerprint}",
        f"mode: {_MODE.name}",
        f"reload model: {_RELOAD_MODEL_NAME}",
        "position family: fresh unresolved loads, both players at full HP, "
        f"shell totals {list(totals)} (the {_POSITION_TOTAL_COUNT} smallest "
        "legal), player holds exactly one item from rules.items.base_items, "
        "dealer holds nothing.",
        "excludes: resolved chamber slots, non-full/unequal HP, multi-item "
        "holdings, Adrenaline steals, Double or Nothing / multiplayer items.",
        "columns: 'value' and 'delta' are for HOLDING the item and playing "
        "best from there — what the item is worth. 'use now?' is the "
        "narrower question of whether spending it immediately is the best "
        "move: 'hold' means it is worth having but not yet worth using.",
        "GENERATED FILE — see buckshotcalc/sim/tables.py for how, and "
        "docs/decisions-pending.md for why this family and not another.",
    )


def render_text(summaries: tuple[PositionSummary, ...], rules: Rules) -> str:
    """Fixed-width plain-text rendering, readable in a terminal."""
    lines: list[str] = list(_header_lines(rules))
    lines.append("")
    item_widths = [len(row.item.config_name) for s in summaries for row in s.rows]
    item_w = max([*item_widths, len("baseline")], default=len("baseline"))
    header = (
        f"{'position':<24}{'item':<{item_w + 2}}{'value':>8}{'delta':>8}  use now?"
    )
    for summary in summaries:
        lines.append("-" * len(header))
        lines.append(f"{summary.position.label:<24}{'baseline':<{item_w + 2}}"
                     f"{summary.baseline_value:>8.4f}{'':>8}  "
                     f"({summary.baseline_action_label})")
        for row in summary.rows:
            use_now = "yes" if row.use_now_is_best else "hold"
            lines.append(
                f"{'':<24}{row.item.config_name:<{item_w + 2}}"
                f"{row.item_value:>8.4f}{row.delta:>+8.4f}  {use_now}"
            )
    lines.append("-" * len(header))
    return "\n".join(lines) + "\n"


def render_markdown(summaries: tuple[PositionSummary, ...], rules: Rules) -> str:
    """Markdown rendering — one table per position, pastable into a doc."""
    lines: list[str] = [f"# {_header_lines(rules)[0]}", ""]
    for line in _header_lines(rules)[1:]:
        lines.append(f"- {line}")
    lines.append("")
    for summary in summaries:
        lines.append(f"## {summary.position.label}")
        lines.append("")
        lines.append(
            f"Baseline (no items): **{summary.baseline_value:.4f}** "
            f"(best action: {summary.baseline_action_label})"
        )
        lines.append("")
        lines.append("| item | value (holding) | delta vs. baseline | use now? |")
        lines.append("|---|---:|---:|:---:|")
        for row in summary.rows:
            use_now = "yes" if row.use_now_is_best else "hold"
            lines.append(
                f"| {row.item.config_name} | {row.item_value:.4f} | "
                f"{row.delta:+.4f} | {use_now} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# main() — the only place this module prints or writes a file (SPEC.md §0.1:
# "No print in engine or sim library code. Harnesses get a main()").
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: generate the sheet against the shipped `rules.toml` and write it."""
    parser = argparse.ArgumentParser(
        description="BuckshotCalc item-value reference table generator (SPEC.md §14, M8)"
    )
    parser.add_argument("--rules", type=str, default=None, help="path to an alternate rules.toml")
    parser.add_argument(
        "--out-text", type=str, default="docs/item-value-tables.txt", help="plain-text output path"
    )
    parser.add_argument(
        "--out-md", type=str, default="docs/item-value-tables.md", help="markdown output path"
    )
    args = parser.parse_args()

    rules = load_rules(Path(args.rules)) if args.rules else default_rules()

    print(
        f"tables: generating sheet for rules fingerprint {rules.fingerprint} "
        f"({len(enumerate_positions(rules))} positions, mode={_MODE.name})"
    )
    print(
        "reload model: stand-in (health ratio), NOT the shipped V_reload "
        "fixed point — see this module's docstring."
    )

    summaries = generate_sheet(rules)

    text_path = Path(args.out_text)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(render_text(summaries, rules))
    print(f"wrote {text_path}")

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(summaries, rules))
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
