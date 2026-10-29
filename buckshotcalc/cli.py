"""Headless query: rank the moves for one position, no TUI, no third-party deps.

    python -m buckshotcalc --live 3 --blank 2 --hp 4 4 --max-hp 4 --items beer=1,handsaw=1
"""

from __future__ import annotations

import argparse

from buckshotcalc.engine.rules import load_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    ItemCounts,
    Player,
)


def _items(text: str) -> ItemCounts:
    pairs = (p.split("=") for p in text.split(",") if p)
    return ItemCounts.of(**{name: int(n) for name, n in pairs})


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="buckshotcalc", description=__doc__.splitlines()[0])
    p.add_argument("--live", type=int, required=True, help="live shells left (all unseen)")
    p.add_argument("--blank", type=int, required=True, help="blank shells left (all unseen)")
    p.add_argument("--hp", type=int, nargs=2, metavar=("YOU", "DEALER"), required=True)
    p.add_argument("--max-hp", type=int, required=True)
    p.add_argument("--items", default="", help="your items, e.g. beer=1,handsaw=2")
    p.add_argument("--dealer-items", default="", help="the Dealer's items, same format")
    p.add_argument("--mode", default="double_or_nothing", choices=[m.config_name for m in GameMode])
    a = p.parse_args(argv)

    n = a.live + a.blank
    state = GameState.create(
        chamber=ChamberState.create((None,) * n, a.live, a.blank),
        hp=tuple(a.hp),
        max_hp=a.max_hp,
        turn=Player.PLAYER,
        items=(_items(a.items), _items(a.dealer_items)),
    )
    solver = Solver(load_rules(), GameMode.from_config_name(a.mode))
    result = solver.evaluate(state)
    print(f"P(win round) = {result.value:.1%}")
    for c in result.children:
        print(f"  {c.value:6.1%}  {c.action}{'  (wasted)' if c.wasted else ''}")


if __name__ == "__main__":
    main()
