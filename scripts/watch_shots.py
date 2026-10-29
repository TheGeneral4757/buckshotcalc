"""Headless screenshots of the watch view (review aid, not shipped in the wheel).

    .venv/bin/python scripts/watch_shots.py [OUT_DIR]

Drives the real app with Textual's pilot at 200x50 (or `size`) through hand-built sessions (a stub
reload table, so nothing is built) and writes <name>.svg and <name>.txt per screen.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import sys
import time
from pathlib import Path

from rich.console import Console

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.state import (
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
)
from buckshotcalc.tui import panels, settings
from buckshotcalc.tui.app import BuckshotApp

RULES = default_rules()
TABLE = {
    (p, d, m): p / (p + d)
    for m in RULES.health.possible_max_hp
    for p in range(1, m + 1)
    for d in range(1, m + 1)
}
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
STILL = settings.Settings(pulse=False)  # a still frame should not catch the dim half of the pulse


def player_turn(hp=(3, 3), cuffed=(0, 1), sawed=True) -> GameState:
    chamber = ChamberState.create((None, None, None, None), 2, 2)
    items = (
        ItemCounts.of(handsaw=1, beer=1, cigarettes=1, magnifying_glass=1),
        ItemCounts.of(magnifying_glass=1, handcuffs=1),
    )
    return GameState.create(chamber, hp, 4, Player.PLAYER, items, sawed, cuffed)


def dealer_turn() -> GameState:
    chamber = ChamberState.create((None, None, None), 2, 1)
    items = (ItemCounts.of(handsaw=1, beer=1), ItemCounts.of(beer=1, cigarettes=1))
    return GameState.create(chamber, (3, 3), 4, Player.DEALER, items)


def ended() -> GameState:
    chamber = ChamberState.create((None, None), 1, 1)
    return GameState.create(chamber, (2, 0), 4, Player.PLAYER)


LOG = ("dealer shot itself: blank (no damage)", "you used the handcuffs",
       "you used the handsaw")


def prep_basic(state, log=LOG):
    def prep(app):
        app.session.state = state
        app.session.log.extend(log)
        app._round_number = 2
        app._anim._hist = [50, 47, 55, 52, 61, 66, 72]  # script-only: a believable round so far
        return True
    return prep


async def settle(app, pilot) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if not app._solving and app._result_state == app.session.state:
            break
    await pilot.pause(1.8)  # let the number tween / typewriter / wipes finish


def text_export(app, st) -> str:
    w, h = app.size
    con = Console(width=w, height=h, record=True, file=io.StringIO(), force_terminal=False,
                  no_color=True)
    con.print(panels.render_watch(app._last_vm, st, app._last_frames), height=h)
    return con.export_text()


async def shoot(name, st, prep, post=None, solve=True, size=(200, 50)) -> None:
    app = BuckshotApp(settings=st)
    app.solver._reload_table = TABLE
    async with app.run_test(size=size) as pilot:
        app._bridge_seen = True
        app._bridge.status = "shot"
        prep(app)
        if solve:
            app._request_solve()
            await settle(app, pilot)
        if post:
            await post(app, pilot)
        app._refresh()
        await pilot.pause(0.1)
        app.save_screenshot(f"{name}.svg", str(OUT))
        (OUT / f"{name}.txt").write_text(text_export(app, st), encoding="utf-8")
    print("wrote", name)


async def thinking(app, pilot) -> None:
    app._solving = True
    app._root_labels = ("shoot the dealer", "shoot yourself", "use the saw", "beer", "cigarettes")
    app._progress = (2, 5, 420_000)
    app._refresh()
    app._anim._think_since = time.monotonic() - 1.2
    await pilot.pause(0.7)


async def paused(app, pilot) -> None:
    app._bridge.paused = "shells left: calc 2, game 3"


async def recap(app, pilot) -> None:
    s = player_turn()
    shoot_d, saw, beer = (Action(ActionKind.SHOOT_OPPONENT), Action(ActionKind.USE, Item.HANDSAW),
                          Action(ActionKind.USE, Item.BEER))
    app._recap.record(s, Action(ActionKind.USE, Item.MAGNIFYING_GLASS),
                      0.61, Action(ActionKind.USE, Item.MAGNIFYING_GLASS), 0.61)
    app._recap.record(s, saw, 0.67, saw, 0.72)
    app._recap.record(s, saw, 0.67, beer, 0.64)
    app._recap.record(s, shoot_d, 0.72, shoot_d, 1.0)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    amber = STILL
    await shoot("01-player-turn-amber", amber, prep_basic(player_turn()))
    await shoot("02-thinking-amber", amber, prep_basic(player_turn()), thinking)
    await shoot("03-dealer-turn-amber", amber, prep_basic(dealer_turn()))
    await shoot("04-paused-amber", amber, prep_basic(player_turn()), paused)
    await shoot("05-recap-amber", amber, prep_basic(ended()), recap)

    async def one_hp(app, pilot):
        app._anim._epoch = time.monotonic() - 0.2  # inside the static window

    await shoot("06-one-hp-amber", settings.Settings(), prep_basic(player_turn(hp=(1, 2))), one_hp)
    await shoot("07-player-turn-green", dataclasses.replace(amber, palette="green"),
                prep_basic(player_turn()))
    await shoot("08-player-turn-red", dataclasses.replace(amber, palette="red"),
                prep_basic(player_turn()))
    await shoot("09-player-turn-amber-solid", dataclasses.replace(amber, background="solid"),
                prep_basic(player_turn()))


if __name__ == "__main__":
    asyncio.run(main())
