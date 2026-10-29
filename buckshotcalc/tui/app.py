"""The BuckshotCalc dashboard and its input loop (SPEC.md §8, §9).

Layout and interaction follow the operator's 2026-09-05 redesign brief, which
supersedes both SPEC.md §8's ASCII mock and the M7 key table. What changed and
why, because the shape of this module is entirely downstream of it:

* **One key, one meaning.** The old scheme had a bare letter mean "declare
  this item held" *or* "use it" depending on what happened to be in the rack,
  and reused `d` for both "shot the Dealer" and "the Dealer's side". Both are
  gone. Now: a bare letter always adds one of that item to *your* rack, SHIFT
  plus the letter always adds one to the *Dealer's*, `space` then the letter
  always means "it was used", `-` then the letter always takes one away. None
  of those four gestures ever changes meaning with the state.
* **Items own every letter.** Nothing else binds a bare letter, which is what
  lets `analysis.PREFERRED_KEYS` be mnemonic (`b` beer, `s` saw, `p` phone).
* **Shots are on the arrows**, still in two keystrokes and still target-first
  (`↑` at yourself, `↓` at the other one), then `←` blank / `→` live — the
  order the events actually happen in, which the operator settled in M7 and
  reaffirmed here.
* **Admin is on ctrl-chords** (`ctrl+z` undo, `ctrl+l` reload the gun,
  `ctrl+q` quit) so it can never collide with an item letter, and the
  command-bar grammar for starting a round is replaced by an inline stepper
  row (`tab` to focus it, `←/→` to change, `enter` to load).
* **There is no help screen.** Every binding is on the dashboard permanently,
  in `panels._KEY_ROWS`.

The arrow keys are shared between shot entry and the setup row, which is only
safe because exactly one of them owns the arrows at a time: the setup row owns
them while it is focused, and it is drawn with a marker and a highlighted
border while it is. `_setup_focus` is that switch.

Textual/rich only — the third-party layer SPEC.md §0.1 reserves for this
module, `panels.py`, and `theme.py`. All game logic lives in `session.py`/
`analysis.py` (stdlib) and `buckshotcalc.engine`; this module wires keypresses
to `Session` methods and renders `panels.render_dashboard` from a cached
`SearchResult`.

**The solver never runs on the render path** (SPEC.md §7.5 / M7 acceptance
criterion): every state-changing event schedules a background solve
(`_solve`, run via `asyncio.to_thread` inside a textual worker); `_refresh`
only ever reads `self._result`, a value already computed and cached. A stale
in-flight solve that finishes against an old state is discarded rather than
overwriting a newer one.

**The reload-table hazard (SPEC.md §7.7)**: at shipped `rules.toml` scale the
reload fixed point takes on the order of 19 minutes per sweep and has never
completed a run (docs/decisions-pending.md items 1-3). This app therefore
never calls `Solver.evaluate`/`value` speculatively — `_solve` checks
`Solver.has_cached_reload_table()` first, and if no table is cached it shows
a persistent warning on the banner instead of attempting (and hanging on) a
build. It does not auto-start a build in the background either: that would
silently burn CPU for the length of a session with no operator consent. The
build is opt-in instead (docs/decisions-pending.md decision 5, re-decided
2026-10-24). `ctrl+t` runs `Solver.build_reload_table` in a worker thread, the
banner shows its progress, and answers appear when it finishes. Pressing
`ctrl+t` again stops it, and a stopped build saves nothing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from buckshotcalc import bundled
from buckshotcalc.engine.actions import Action, ActionKind, legal_actions
from buckshotcalc.engine.rules import Rules, load_rules
from buckshotcalc.engine.solver import SearchCancelled, SearchResult, Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from buckshotcalc.tui import advice as advice_mod
from buckshotcalc.tui import analysis, anim, bridge, panels, theme, timing
from buckshotcalc.tui import settings as settings_mod
from buckshotcalc.tui.panels import SetupFields
from buckshotcalc.tui.session import Session, SessionError
from buckshotcalc.tui.settings_screen import SettingsScreen
from buckshotcalc.tui.watchdriver import WatchDriver

#: The two-choice answer keys. `←`/`→` rather than letters, because letters
#: belong to items and a modal prompt that borrows one is exactly the "this
#: key means two things" problem the redesign removed.
_LEFT_RIGHT_SHELL = {"left": Shell.BLANK, "right": Shell.LIVE}

class _Snap(NamedTuple):
    """What the screen shows until the reveal time passes (the display gate)."""

    state: GameState
    result: SearchResult | None
    solving: bool
    log: tuple[str, ...]
    plan: analysis.PlanNode | None
    forecast: tuple[analysis.ForecastRow, ...]


def _taken_action(e: object) -> Action | None:
    """The player action a bridge event reports, for the round recap."""
    if not isinstance(e, dict) or e.get("actor") != "player":
        return None
    if e.get("ev") == "shot":
        kind = ActionKind.SHOOT_SELF if e.get("target") == "self" else ActionKind.SHOOT_OPPONENT
        return Action(kind)
    if e.get("ev") == "item" and not e.get("stolen"):
        try:
            return Action(ActionKind.USE, Item.from_config_name(str(e["item"]).replace(" ", "_")))
        except (KeyError, ValueError):
            return None
    return None


class _BuildStopped(Exception):
    """Raised from the build's progress callback to stop it (decision 5).

    The engine guarantees that an exception from `progress` propagates, stops
    the worker pool and writes no cache, so this is the whole cancellation
    mechanism. It takes effect at the next progress event, not instantly.
    """


#: How many log lines the (deliberately tall) history box shows.
_LOG_LINES = 12


def _default_state(rules: Rules) -> GameState:
    """A plausible bootstrap state (not a game rule — the operator overwrites
    this immediately from the inline NEW ROUND row). Uses the config's
    minimums for the chamber and its largest declared `max_hp`.
    """
    live, blank = rules.shells.min_live, rules.shells.min_blank
    max_hp = rules.health.possible_max_hp[-1]
    chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
    return GameState.create(
        chamber=chamber,
        hp=(max_hp, max_hp),
        max_hp=max_hp,
        turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
    )


def _actor_word(player: Player) -> str:
    return "you" if player is Player.PLAYER else "the dealer"


def _actor_has(player: Player) -> str:
    """`_actor_word` with the verb agreed — notices are sentences, not fragments."""
    return "you have" if player is Player.PLAYER else "the dealer has"


@dataclass
class _Pending:
    """A partially-entered event awaiting its next keypress.

    Shot entry is **target first** (operator decision, twice): `↑`/`↓` names
    where the gun pointed, then `←`/`→` says what came out. `via_steal` names
    the item being stolen when this chain was opened by Adrenaline (`steal()`
    is called instead of the plain `record_*` method). `prompt` is the exact
    sentence shown on the question line while this is outstanding — phrased
    as a sentence naming the actor, per the M7 brief.
    """

    kind: str
    prompt: str
    at_self: bool | None = None
    phone_index: int | None = None
    via_steal: Item | None = None


class BuckshotApp(App[None]):
    """`buckshotcalc` console-script entry point (SPEC.md §8, §9)."""

    CSS = """
    Screen { align: center top; background: transparent; }
    #board { width: 100%; height: 1fr; background: transparent; }
    #container { background: transparent; }
    """

    def __init__(
        self,
        rules_path: Path | None = None,
        *,
        listen: bool = False,
        record: Path | None = None,
        replay: Path | None = None,
        advice: Path | None = None,
        settings: settings_mod.Settings | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings if settings is not None else settings_mod.load()
        timing.REVEAL_SCALE = self.settings.reveal_scale
        self._listen, self._record, self._replay = listen, record, replay
        self._bridge = bridge.BridgeState()
        self.rules: Rules = load_rules(rules_path)
        self.mode = GameMode.DOUBLE_OR_NOTHING
        self.session = Session(self.rules, self.mode, _default_state(self.rules))
        self.solver = Solver(self.rules, self.mode)
        self._advice = advice_mod.AdviceLog(advice, self.rules, self.mode.name)
        self._item_letters = analysis.build_item_key_map()
        self._letter_to_item = {v: k for k, v in self._item_letters.items()}

        self._result: SearchResult | None = None
        self._solving = False
        # What the screen shows until the reveal time: (state, result, solving).
        self._snap: _Snap | None = None
        self._gated_view = False
        # Watch view (docs/ui-roadmap.md phase 1): shown once the bridge has fed events.
        self._anim = WatchDriver(self.settings)
        self._bridge_seen = False
        self._manual = False  # ctrl+e: force the entry layout
        self._tick_timer = None
        self._last_vm: panels.DashboardViewModel | None = None
        self._last_frames = None
        self._bg_solid: bool | None = None
        self._load_number = 1
        self._plan: analysis.PlanNode | None = None
        self._forecast: tuple[analysis.ForecastRow, ...] = ()
        self._result_state: GameState | None = None
        # (done, total, nodes) from the solver thread; the 25 fps tick reads it.
        self._progress: tuple[int, int, int] | None = None
        self._root_labels: tuple[str, ...] = ()
        self._recap = analysis.RoundRecap()
        self._recap_pending: list[tuple[GameState, Action, float, Action]] = []
        self._solve_cancel: threading.Event | None = None
        self._reload_table_missing = False
        # ctrl+t's background build (decision 5): the tracker exists only while it runs.
        self._build: analysis.ReloadBuildTracker | None = None
        self._build_cancel = threading.Event()
        self._round_number = 1
        self._pending: _Pending | None = None
        self._status: str = ""
        # `space`/`-` prefixes: the next item letter uses / removes instead of
        # adding. A prefix is always visible on the question line and always
        # cancellable with `esc`, so it can never silently change what a
        # letter does.
        self._prefix: str | None = None
        self._setup = SetupFields(
            live=self.rules.shells.min_live,
            blank=self.rules.shells.min_blank,
            max_hp=self.session.state.max_hp,
        )
        self._setup_focus = False

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Vertical(Static(id="board"), id="container")

    def on_mount(self) -> None:
        self.set_focus(None)
        if self._replay is not None:
            done = bridge.replay(self.session, self._replay.read_text().splitlines(), self._bridge)
            self._bridge_seen = bool(done)
        if self._listen:
            self.run_worker(self._run_bridge(), group="bridge")
            self.set_interval(0.1, self._gate_tick)
        self._refresh()
        self._request_solve()

    # -- live bridge (docs/live-bridge.md) -----------------------------------

    async def _run_bridge(self) -> None:
        def handle(event: dict) -> bridge.Result:
            self._bridge_seen = True
            taken = _taken_action(event)
            rec = self._recommendation_for(taken)
            if taken is not None:
                self._advice.decision(
                    event.get("seq"), self.session.state, self._result, taken,
                    self._no_rec_reason(),
                )
            res = bridge.apply_event(self.session, event, self._bridge)
            self._bridge.status = res.status
            if rec is not None and (res.applied or self._bridge.pending is not None):
                self._recap_pending.append(rec)
            if isinstance(event, dict) and event.get("ev") == "end" and not self._bridge.paused:
                self._advice.round_end(event)
            if isinstance(event, dict) and event.get("ev") == "load" and res.applied:
                self._advice.load(event)
                if event.get("kind") == "new_round":
                    self._round_number += 1
                    self._load_number = 1
                    self._recap = analysis.RoundRecap()
                    self._recap_pending.clear()
                else:
                    self._load_number += 1
            return res

        def changed() -> None:
            self._refresh()
            self._request_solve()

        try:
            await bridge.serve(handle, changed, record=self._record)
        except OSError as exc:
            self._bridge.off = f"bridge: cannot listen on {bridge.PORT} ({exc}), manual entry"
            self._refresh()

    def _gate_tick(self) -> None:
        """Show the live state once the reveal time passes."""
        if self._gated_view and bridge.display_released(self._bridge, time.monotonic()):
            self._refresh()

    def _no_rec_reason(self) -> str | None:
        """Why the calc had no valid recommendation for the current position (item 16)."""
        if self.session.degraded is not None:
            return f"degraded: {self.session.degraded}"
        if self._bridge.paused is not None:
            return f"withheld: bridge paused ({self._bridge.paused})"
        if self._reload_table_missing:
            return "no reload table"
        if self._result is None or not self._result.children:
            return "no result yet"
        if self._result_state != self.session.state:
            return "stale: result is for an earlier position"
        return None

    def _recommendation_for(
        self, taken: Action | None
    ) -> tuple[GameState, Action, float, Action] | None:
        """The calc's pick for the current position, paired with what the player did (R1)."""
        res = self._result
        if taken is None or res is None or not res.children:
            return None
        if self._result_state != self.session.state:
            return None
        best = res.children[0]
        if taken.item is Item.ADRENALINE and best.action.item is Item.ADRENALINE:
            taken = best.action  # which item it steals is a later event
        return (self.session.state, best.action, best.value, taken)

    def _accept_game_state(self) -> None:
        self._status = bridge.accept_game_state(self.session, self._bridge)
        self._request_solve()

    # -- rendering (no solving happens here) -------------------------------

    def _watching(self) -> bool:
        """Watch layout while the bridge is feeding events; entry layout otherwise."""
        return self._bridge_seen and not self._bridge.off and not self._manual

    def _view_model(self) -> panels.DashboardViewModel:
        setup = replace(self._setup, focus=self._setup.focus if self._setup_focus else -1)
        self._gated_view = (
            not bridge.display_released(self._bridge, time.monotonic()) and self._snap is not None
        )
        if not self._gated_view:
            self._snap = _Snap(
                self.session.state, self._result, self._solving,
                self.session.recent_log(_LOG_LINES), self._plan, self._forecast,
            )
        snap = self._snap
        assert snap is not None
        return panels.DashboardViewModel(
            state=snap.state,
            result=snap.result,
            solving=snap.solving,
            reload_table_missing=self._reload_table_missing,
            reload_build=self._build.describe() if self._build is not None else None,
            reload_build_stopping=self._build_cancel.is_set(),
            degraded=self.session.degraded,
            bridge_paused=self._bridge.paused,
            bridge_alert=self._bridge.off or self._bridge.warn,
            bridge_line=f"live from the game · last event: {self._bridge.status}"
            if self._bridge.status else "",
            log=snap.log,
            prompt=self._pending.prompt if self._pending is not None else "",
            status=self._status,
            round_number=self._round_number,
            seat=Player.PLAYER,
            setup=setup,
            item_keys=tuple(
                (item, self._item_letters[item]) for item in sorted(Item, key=lambda i: i.value)
            ),
            pending_use=self._prefix == "use",
            pending_remove=self._prefix == "remove",
            plan=snap.plan,
            forecast=snap.forecast,
            recap=self._recap.rows(),
            events=snap.log[-3:],
            load_number=self._load_number,
            # only when the shown result is for the shown position: a held-over
            # result would get another position's death chances
            rules=self.rules
            if snap.result is self._result and self._result_state == snap.state
            else None,
        )

    def _board(self) -> Static:
        return self.screen_stack[0].query_one("#board", Static)  # a modal may be on top

    def _refresh(self) -> None:
        vm = self._view_model()
        self._last_vm = vm
        board = self._board()
        watching = self._watching()
        self._apply_background(watching and self.settings.background == "solid")
        if not watching:
            self._anim.forget()
            board.update(panels.render_dashboard(vm, self.rules))
            return
        now = time.monotonic()
        self._anim.observe(vm, now)
        self._anim.set_progress(self._progress if vm.solving else None, self._root_labels)
        self._last_frames = self._anim.frames(now)
        board.update(panels.render_watch(vm, self.settings, self._last_frames))
        self._ensure_ticking(now)

    # -- the 25 fps tick: runs only while something on screen is moving ----------

    def _ensure_ticking(self, now: float) -> None:
        if self._tick_timer is None and self._anim.busy(now):
            self._tick_timer = self.set_interval(1 / anim.FPS, self._tick)

    def _stop_ticking(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer = None

    def _tick(self) -> None:
        vm = self._last_vm
        if vm is None or not self._watching():
            self._stop_ticking()
            return
        now = time.monotonic()
        self._anim.set_progress(self._progress if vm.solving else None, self._root_labels)
        frames = self._anim.frames(now)
        if frames != self._last_frames:
            self._last_frames = frames
            self._board().update(panels.render_watch(vm, self.settings, frames))
        if not self._anim.busy(now):
            self._stop_ticking()

    def _apply_background(self, solid: bool) -> None:
        """Paint the palette's background on the whole screen only in 'solid' mode."""
        if solid == self._bg_solid:
            return
        self._bg_solid = solid
        colour = theme.palette_for(self.settings).bg_solid if solid else "transparent"
        self.screen_stack[0].styles.background = colour

    # -- settings overlay (ctrl+o) -------------------------------------------------

    def _open_settings(self) -> None:
        self.push_screen(SettingsScreen(self.settings, self._set_settings, self._save_settings))

    def _set_settings(self, new: settings_mod.Settings) -> None:
        self.settings = new
        self._anim.set_settings(new)
        timing.REVEAL_SCALE = new.reveal_scale
        self._bg_solid = None  # palette may have changed under a solid background
        self._refresh()

    def _save_settings(self, new: settings_mod.Settings) -> None:
        settings_mod.save(new)

    # -- background solving -------------------------------------------------

    def _request_solve(self) -> None:
        # Cancelling a worker does not stop its thread; the event does.
        if self._solve_cancel is not None:
            self._solve_cancel.set()
        self._solve_cancel = threading.Event()
        self.run_worker(self._solve(self._solve_cancel), exclusive=True, group="solve")

    async def _solve(self, cancel: threading.Event) -> None:
        state = self.session.state
        if not self.solver.has_cached_reload_table():
            self._reload_table_missing = True
            self._solving = False
            self._refresh()
            return
        self._reload_table_missing = False
        self._solving = True
        self._progress = (0, 1, 0)
        self._root_labels = self._labels_for(state)
        self._refresh()

        def on_progress(done: int, total: int, nodes: int) -> None:
            self._progress = (done, total, nodes)  # read by the 25 fps tick

        def work():
            result = self.solver.evaluate(state, cancel, on_progress)
            plan, forecast = None, ()
            if not cancel.is_set():  # derived panels are computed here, never on the UI thread
                if state.turn is Player.PLAYER and result.children:
                    plan = analysis.plan_tree(self.solver, state)
                elif state.turn is Player.DEALER and state.winner() is None and (
                    not state.chamber.is_empty
                ):
                    forecast = analysis.dealer_forecast(state, self.rules, self.solver)
            return result, plan, forecast

        try:
            result, plan, forecast = await asyncio.to_thread(work)
        except SearchCancelled:
            return
        if state == self.session.state:
            self._result, self._result_state = result, state
            self._plan, self._forecast = plan, forecast
            self._solving = False
            for before, rec, rec_value, taken in self._recap_pending:
                self._recap.record(before, rec, rec_value, taken, result.value)
            self._recap_pending.clear()
            self._refresh()

    def _labels_for(self, state: GameState) -> tuple[str, ...]:
        """Root move names in the solver's order, to name the move being checked (P2)."""
        s = state.canonical()
        if s.winner() is not None or s.chamber.is_empty or s.turn is not Player.PLAYER:
            return ()
        return tuple(
            panels.plain_action(analysis.action_label(a, Player.PLAYER)).lower()
            for a in legal_actions(s, self.rules)
        )

    def _toggle_reload_build(self) -> None:
        """`ctrl+t` — build the reload table in the background, or stop that build."""
        if self._build is not None:
            self._build_cancel.set()
            return
        if self.solver.has_cached_reload_table():
            self._status = "the reload table is already built"
            return
        self._build = analysis.ReloadBuildTracker()
        self._build_cancel.clear()
        self.run_worker(self._build_reload_table(), group="reload-build")

    async def _build_reload_table(self) -> None:
        def progress(event) -> None:
            if self._build_cancel.is_set():
                raise _BuildStopped
            self.call_from_thread(self._on_build_progress, event)

        try:
            await asyncio.to_thread(self.solver.build_reload_table, progress)
        except _BuildStopped:
            self._status = "reload-table build stopped — nothing was saved"
        except Exception as exc:  # a failed build must not take the session down with it
            self._status = f"reload-table build failed: {exc}"
        else:
            self._status = "reload table built and saved"
        finally:
            self._build = None
            self._build_cancel.clear()
        self._refresh()
        self._request_solve()

    def _on_build_progress(self, event) -> None:
        if self._build is not None:
            self._build.update(event)
            self._refresh()

    # -- input dispatch -----------------------------------------------------

    def on_key(self, event) -> None:
        key = event.key
        char = getattr(event, "character", None)
        self._status = ""
        if key == "ctrl+o":
            self._open_settings()
            return
        try:
            self._dispatch(key, char)
        except SessionError as exc:
            self._status = str(exc)
        self._refresh()

    def _dispatch(self, key: str, char: str | None) -> None:
        if key == "escape":
            self._cancel()
            return
        if self._pending is not None:
            self._dispatch_pending(key, char)
            return
        if self._setup_focus:
            self._dispatch_setup(key)
            return
        if self._prefix is not None:
            self._dispatch_prefixed(char)
            return

        if key == "up":
            self._begin_shot(at_self=True)
            return
        if key == "down":
            self._begin_shot(at_self=False)
            return
        if key == "tab":
            self._setup_focus = True
            self._setup = replace(self._setup, focus=0)
            return
        if key == "enter":
            self._load_round()
            return
        if key == "space":
            self._prefix = "use"
            return
        if char == "-":
            self._prefix = "remove"
            return
        if key == "ctrl+z":
            self._undo()
            return
        if key == "ctrl+l":
            self._reload_gun()
            return
        if key == "ctrl+g":
            self._accept_game_state()
            return
        if key == "ctrl+t":
            self._toggle_reload_build()
            return
        if key == "ctrl+e":
            self._manual = not self._manual
            return
        if key == "ctrl+q":
            self._build_cancel.set()  # a running build stops at its next step
            self._stop_ticking()
            self.exit()
            return
        if char is not None and char.isalpha():
            self._add_item(char)
            return

    def _cancel(self) -> None:
        """`esc` — one key backs out of whatever is half-entered."""
        if self._pending is not None:
            self._status = f"cancelled: {self._pending.prompt}"
            self._pending = None
        elif self._prefix is not None:
            self._prefix = None
        elif self._setup_focus:
            self._setup_focus = False

    # -- items --------------------------------------------------------------

    def _item_for(self, char: str | None) -> Item | None:
        if char is None or not char.isalpha():
            return None
        return self._letter_to_item.get(char.lower())

    def _add_item(self, char: str) -> None:
        """Bare letter → you have one; SHIFT+letter → the dealer has one.

        Seat is chosen by the *case of the key*, never by whose turn it is:
        declaring what is on the table is not a move, and making it
        turn-dependent was one of the things that made the old dashboard
        unpredictable.
        """
        item = self._item_for(char)
        if item is None:
            return
        seat = Player.DEALER if char.isupper() else Player.PLAYER
        self.session.give_item(seat, item)
        self._request_solve()

    def _remove_item(self, char: str) -> None:
        item = self._item_for(char)
        if item is None:
            return
        seat = Player.DEALER if char.isupper() else Player.PLAYER
        current = self.session.state.items_of(seat)[item]
        if current == 0:
            self._status = (
                f"{_actor_has(seat)} no {panels.item_display_name(item)} to take away"
            )
            return
        self.session.edit_item_count(seat, item, current - 1)
        self._request_solve()

    def _dispatch_prefixed(self, char: str | None) -> None:
        """The keypress after `space` or `-`."""
        prefix, self._prefix = self._prefix, None
        item = self._item_for(char)
        if item is None or char is None:
            self._status = "that is not an item key"
            return
        if prefix == "remove":
            self._remove_item(char)
            return
        self._use_item(item)

    def _use_item(self, item: Item) -> None:
        """`space`+letter — the item was used, by whoever is moving.

        Case is ignored here on purpose: only the mover can spend an item, so
        there is no seat to choose and `space+M` must not mean something
        different from `space+m`.
        """
        mover = self.session.state.turn
        if self.session.state.items_of(mover)[item] == 0:
            self._status = (
                f"{_actor_has(mover)} no {panels.item_display_name(item)} "
                f"— press [{self._item_letters[item]}] first to say it is held"
            )
            return
        self._begin_item_use(item, via_steal=None)

    # -- shots ---------------------------------------------------------------

    def _begin_shot(self, *, at_self: bool) -> None:
        actor = _actor_word(self.session.state.turn)
        if at_self:
            target = "itself" if actor == "the dealer" else "yourself"
        else:
            target = "you" if actor == "the dealer" else "the dealer"
        self._pending = _Pending(
            kind="shot_result",
            prompt=f"{actor} shot {target} — what came out?   [←] a blank   [→] a live shell",
            at_self=at_self,
        )

    # -- items with an observable outcome -------------------------------------

    def _begin_item_use(self, item: Item, *, via_steal: Item | None) -> None:
        """Start (or immediately finish) using `item`.

        `via_steal` is set when this use is Adrenaline's stolen item — the
        terminal call routes to `session.steal(...)` instead of `use_item`/
        `record_*`.
        """
        actor = _actor_word(self.session.state.turn)
        if item in (Item.CIGARETTES, Item.HANDCUFFS, Item.HANDSAW):
            if via_steal is not None:
                self.session.steal(via_steal)
            else:
                self.session.use_item(item)
            self._request_solve()
            return
        # The Dealer's information items are used PRIVATELY: we watch it look,
        # we never see what it saw. Asking the operator "live or blank?" here
        # and feeding the answer to `record_glass`/`record_phone` would resolve
        # the slot in OUR chamber from information we do not have, and would
        # leave `dealer_knows` empty — exactly backwards, and SPEC.md §0.1 (5)'s
        # named single easiest way to make every recommendation silently wrong.
        # A steal is different: an item Adrenaline took is used by US, so its
        # answer really is ours to see, and it keeps the normal routing.
        dealer_moving = self.session.state.turn is not Player.PLAYER
        if dealer_moving and via_steal is None and item in Session.DEALER_INFO_ITEMS:
            if item is Item.MAGNIFYING_GLASS:
                # The Dealer looked at the shell in the gun: that slot is now
                # known to it, and to it only.
                self._record_dealer_info(0, item)
            else:
                # No question asked: the Phone's *target* is as private as its
                # answer, so asking which shell could only be answered by
                # invention (SPEC.md §15.16(a)). `Session.record_dealer_phone`
                # applies the same rule the search applies to every future
                # Dealer phone use, so the live session and the solver agree.
                try:
                    self.session.record_dealer_phone()
                except SessionError as exc:
                    self._status = str(exc)
                    return
                self._status = (
                    "which shell the dealer's phone named is a guess, not something "
                    "you saw — the number above runs slightly optimistic"
                )
                self._request_solve()
            return
        if item is Item.MAGNIFYING_GLASS:
            self._pending = _Pending(
                kind="glass_shell",
                prompt=f"{actor} looked at the shell in the gun — "
                f"what is it?   [←] a blank   [→] a live shell",
                via_steal=via_steal,
            )
            return
        if item is Item.BURNER_PHONE:
            last = len(self.session.state.chamber.slots)
            self._pending = _Pending(
                kind="phone_index",
                prompt=f"{actor} used the phone — which shell did it name?   [1-{last}]",
                via_steal=via_steal,
            )
            return
        if item is Item.BEER:
            self._pending = _Pending(
                kind="beer_shell",
                prompt=f"{actor} racked the gun — what came out?   "
                f"[←] a blank   [→] a live shell",
                via_steal=via_steal,
            )
            return
        if item is Item.INVERTER:
            # Nothing is asked in either case. A known shell is a certain
            # flip; an unknown one is a coin nobody sees, and asking the
            # operator what it *was* invited a guess that fabricated
            # knowledge (SPEC.md §15.16(b), settled as option (a) of
            # docs/decisions-pending.md item 6). `Session` marks the chamber
            # degraded instead.
            if via_steal is not None:
                self.session.steal(via_steal)
            else:
                self.session.record_inverter()
            self._request_solve()
            return
        if item is Item.EXPIRED_MEDICINE:
            self._pending = _Pending(
                kind="medicine_outcome",
                prompt=f"{actor} took the medicine — what happened?   "
                f"[←] it hurt   [→] it healed",
                via_steal=via_steal,
            )
            return
        if item is Item.ADRENALINE:
            opponent = self.session.state.turn.other
            held = self._held_items(opponent)
            if Action(ActionKind.USE, Item.ADRENALINE) in legal_actions(
                self.session.state, self.rules
            ):
                self.session.waste_adrenaline()  # nothing stealable: spent, turn continues
                self._request_solve()
                return
            names = "   ".join(
                f"[{self._item_letters[i]}] {panels.item_display_name(i)}" for i in held
            )
            self._pending = _Pending(
                kind="adrenaline_target",
                prompt=f"{actor} used adrenaline — stole what from "
                f"{_actor_word(opponent)}?   {names}",
            )
            return
        self._status = f"{panels.item_display_name(item)}: nothing is wired up for this yet"

    def _held_items(self, player: Player) -> tuple[Item, ...]:
        return tuple(item for item, _n in self.session.state.items_of(player).held())

    def _record_dealer_info(self, slot_index: int, item: Item) -> None:
        """Record a Dealer information action without learning its answer.

        Consumes the Dealer's item and adds the slot to `dealer_knows`; our
        own chamber is left untouched, because we did not see what it saw.
        """
        try:
            self.session.record_dealer_info_action(slot_index, item)
        except SessionError as exc:
            self._status = str(exc)
            self._pending = None
            return
        self._request_solve()

    def _dispatch_pending(self, key: str, char: str | None) -> None:
        p = self._pending
        assert p is not None
        shell = _LEFT_RIGHT_SHELL.get(key)

        if p.kind == "shot_result":
            if shell is None:
                return
            self.session.record_shot(at_self=p.at_self, shell=shell)
            self._pending = None
            self._request_solve()
            return

        if p.kind == "glass_shell":
            if shell is None:
                return
            if p.via_steal is not None:
                self.session.steal(p.via_steal, shell=shell)
            else:
                self.session.record_glass(shell)
            self._pending = None
            self._request_solve()
            return

        if p.kind == "phone_index":
            # Shells are numbered from 1 on screen; the engine indexes from 0.
            if char is None or not char.isdigit() or char == "0":
                return
            index = int(char) - 1
            if index >= len(self.session.state.chamber.slots):
                self._status = f"there is no shell {char} in the gun"
                return
            actor = _actor_word(self.session.state.turn)
            self._pending = _Pending(
                kind="phone_shell",
                prompt=f"{actor}'s phone named shell {char} — what is it?   "
                f"[←] a blank   [→] a live shell",
                phone_index=index,
                via_steal=p.via_steal,
            )
            return

        if p.kind == "phone_shell":
            if shell is None:
                return
            if p.via_steal is not None:
                self.session.steal(p.via_steal, shell=shell, phone_slot=p.phone_index)
            else:
                self.session.record_phone(p.phone_index, shell)
            self._pending = None
            self._request_solve()
            return

        if p.kind == "beer_shell":
            if shell is None:
                return
            if p.via_steal is not None:
                self.session.steal(p.via_steal, shell=shell)
            else:
                self.session.record_beer(shell)
            self._pending = None
            self._request_solve()
            return

        if p.kind == "medicine_outcome":
            # No SPEC.md §9 key covers this observation; the ←/→ pair is a
            # documented extension — see SPEC.md §15.15.
            if key not in ("left", "right"):
                return
            healed = key == "right"
            if p.via_steal is not None:
                self.session.steal(p.via_steal, healed=healed)
            else:
                self.session.record_expired_medicine(healed)
            self._pending = None
            self._request_solve()
            return

        if p.kind == "adrenaline_target":
            item = self._item_for(char)
            if item is None or item not in self._held_items(self.session.state.turn.other):
                return
            self._pending = None
            self._begin_item_use(item, via_steal=item)
            return

    # -- the inline NEW ROUND row ---------------------------------------------

    def _dispatch_setup(self, key: str) -> None:
        """←/→ change the focused field, `tab` moves on, `enter` loads.

        While this is focused it owns the arrows; shot entry does not see
        them. `esc` (handled in `_dispatch`) hands them back.
        """
        if key == "tab":
            self._setup = replace(self._setup, focus=(self._setup.focus + 1) % 3)
            return
        if key == "enter":
            self._load_round()
            self._setup_focus = False
            return
        if key not in ("left", "right"):
            return
        step = 1 if key == "right" else -1
        field = self._setup.focus
        if field == 0:
            self._setup = replace(
                self._setup, live=max(self.rules.shells.min_live, self._setup.live + step)
            )
        elif field == 1:
            self._setup = replace(
                self._setup, blank=max(self.rules.shells.min_blank, self._setup.blank + step)
            )
        else:
            options = self.rules.health.possible_max_hp
            current = options.index(self._setup.max_hp) if self._setup.max_hp in options else 0
            self._setup = replace(
                self._setup,
                max_hp=options[max(0, min(len(options) - 1, current + step))],
            )

    def _load_round(self) -> None:
        self.session.new_round(
            self._setup.live, self._setup.blank, max_hp=self._setup.max_hp
        )
        self._round_number += 1
        self._load_number = 1
        self._recap = analysis.RoundRecap()
        self._recap_pending.clear()
        self._status = (
            f"round {self._round_number}: {self._setup.live} live and "
            f"{self._setup.blank} blank, {self._setup.max_hp} health each"
        )
        self._request_solve()

    def _reload_gun(self) -> None:
        """`ctrl+l` — the gun was reloaded mid-round; health and items carry."""
        self.session.new_load(self._setup.live, self._setup.blank)
        self._load_number += 1
        self._status = (
            f"gun reloaded: {self._setup.live} live and {self._setup.blank} blank"
        )
        self._request_solve()

    def _undo(self) -> None:
        self.session.undo()
        self._status = self.session.log[-1]  # undo must say what it undid
        self._request_solve()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", type=Path, help="feed a recorded bridge JSONL through the adapter")
    ap.add_argument("--record", type=Path, help="save the live bridge stream to this JSONL file")
    ap.add_argument("--advice", type=Path, help="advice log file (default: by --record)")
    ap.add_argument("--no-advice", action="store_true", help="do not save the advice log")
    args = ap.parse_args()
    bundled.install()
    advice = None
    if not args.no_advice:
        advice = args.advice or (advice_mod.advice_path_for(args.record) if args.record else None)
    BuckshotApp(
        listen=args.replay is None, record=args.record, replay=args.replay, advice=advice
    ).run()


if __name__ == "__main__":
    main()
