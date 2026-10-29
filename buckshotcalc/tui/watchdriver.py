"""Animation driver for the watch view: Textual-free, time injected.

`observe(vm, now)` is called with the *released* snapshot (the display gate in
`bridge.py` / `app.py` has already held back anything the player has not yet
seen on screen), diffs it against the previous one and starts tweens.
`frames(now)` turns the running tweens into a `WatchFrames` for `render_watch`.
`busy(now)` says whether anything still needs a tick, so the app can stop its
25 fps timer when the screen is at rest.

Ambient effects (best-move pulse, heartbeat, static at 1 HP) keep `busy` true
for as long as they are on screen; `frames` is quantised so the app can skip
repaints when nothing visibly changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from buckshotcalc.engine.state import Item, Player
from buckshotcalc.tui import anim
from buckshotcalc.tui.settings import Settings
from buckshotcalc.tui.watch import HERO_W, RackFx, ThinkFrame, WatchFrames, _win

THINK_DELAY = 0.3  # seconds a solve must take before the progress block replaces the action
_HIST = 24


@dataclass(slots=True)
class _Prev:
    state: object
    win: float | None
    round: int
    events: tuple[str, ...]


class WatchDriver:
    def __init__(self, settings: Settings) -> None:
        self.st = settings
        self._epoch = 0.0
        self._prev: _Prev | None = None
        self._hist: list[float] = []
        self._num = None  # (t0, from, to)
        self._delta = None  # (t0, delta)
        self._shell = None  # (t0, args)
        self._type = None  # (t0, text)
        self._pip: list[dict] = [{}, {}]  # per seat: {"loss": t0} / {"gain": t0}
        self._rack: list[tuple[float, int, Item, str]] = []  # (t0, seat, item, "use"|"steal")
        self._wipe = None
        self._stamp = None
        self._think_since: float | None = None
        self._progress: tuple[int, int, int] | None = None
        self._labels: tuple[str, ...] = ()
        self._smooth = anim.ProgressSmoother()
        self._last_think = 0.0
        self._hp = 4
        self._best_shown = False

    def forget(self) -> None:
        """Left the watch view: drop the diff baseline so re-entry starts clean."""
        self._prev = None

    def set_settings(self, st: Settings) -> None:
        self.st = st

    def set_progress(self, progress: tuple[int, int, int] | None, labels: tuple[str, ...]) -> None:
        self._progress, self._labels = progress, labels

    # -- diffing a released snapshot -------------------------------------------

    def observe(self, vm, now: float) -> None:
        state = vm.state
        win = _win(vm)
        events = tuple(vm.events[-3:])
        prev = self._prev
        if prev is None or vm.round_number != prev.round:
            self._hist = []
        self._hp = state.hp[vm.seat.value]
        mine = state.turn is vm.seat and not vm.solving
        self._best_shown = win is not None and bool(vm.result.children) and mine
        if vm.solving and self._think_since is None:
            self._think_since = now
            self._smooth.reset()
            self._last_think = now
        elif not vm.solving:
            self._think_since = None
        if win is not None and (not self._hist or abs(win - self._hist[-1]) >= 0.05):
            self._hist = (self._hist + [win])[-_HIST:]
        if prev is not None:
            self._diff(prev, vm, state, win, events, now)
        self._prev = _Prev(state, win if win is not None else (prev.win if prev else None),
                           vm.round_number, events)

    def _diff(self, prev: _Prev, vm, state, win, events, now: float) -> None:
        ps = prev.state
        if win is not None and prev.win is not None and abs(win - prev.win) >= 0.05:
            self._num = (now, prev.win, win)
            self._delta = (now, win - prev.win)
        if events and events != prev.events:
            self._type = (now, events[-1])
        same_load = (
            len(state.chamber) <= len(ps.chamber)
            and state.max_hp == ps.max_hp
            and vm.round_number == prev.round
        )
        if not same_load:
            return
        if len(state.chamber) < len(ps.chamber):
            gone_live = state.chamber.total_live() < ps.chamber.total_live()
            self._shell = (now, ("L" if gone_live else "B", len(ps.chamber),
                                 ps.chamber.total_live(), ps.chamber.total_blank(),
                                 state.chamber.total_live(), state.chamber.total_blank()))
        for seat in (0, 1):
            if state.hp[seat] < ps.hp[seat]:
                self._pip[seat] = {"loss": now}
            elif state.hp[seat] > ps.hp[seat]:
                self._pip[seat] = {"gain": now}
        self._diff_items(ps, state, now)
        if state.turn != ps.turn:
            self._wipe = now
        if (state.sawed and not ps.sawed) or any(
            c and not p for c, p in zip(state.cuffed, ps.cuffed, strict=True)
        ):
            self._stamp = now

    def _diff_items(self, ps, state, now: float) -> None:
        dec = {
            (seat, item)
            for seat in (0, 1)
            for item in Item
            if ps.items_of(Player(seat))[item] > state.items_of(Player(seat))[item]
        }
        for seat, item in dec:
            thief = 1 - seat
            if (thief, Item.ADRENALINE) in dec and item is not Item.ADRENALINE:
                self._rack.append((now, thief, item, "steal"))  # slides into the thief's rack
            else:
                self._rack.append((now, seat, item, "use"))

    # -- frames --------------------------------------------------------------------

    def frames(self, now: float) -> WatchFrames:
        st, mode = self.st, self.st.motion
        num = delta = shells = event_text = wipe = think = None
        if self._num is not None:
            t0, a, b = self._num
            nf = anim.number_tween(st.number_anim, a, b, now - t0, mode)
            if nf.done:
                self._num = None
            else:
                num = nf
        if self._delta is not None:
            t0, d = self._delta
            text, role, level, done = anim.delta_badge(d, now - t0, mode)
            if done:
                self._delta = None
            else:
                delta = (text, role, level)
        if self._shell is not None:
            t0, args = self._shell
            sf = anim.shell_strip(*args, now - t0, mode)
            if sf.done:
                self._shell = None
            else:
                shells = sf
        if self._type is not None:
            t0, text = self._type
            event_text, done = anim.typewriter(text, now - t0, 60, mode)
            if done:
                self._type = None
                event_text = None
        lost = [False, False]
        gain = [0, 0]
        for seat in (0, 1):
            fx = self._pip[seat]
            if "loss" in fx:
                on, done = anim.pip_loss(now - fx["loss"], mode)
                lost[seat] = on
                if done:
                    self._pip[seat] = {}
            elif "gain" in fx:
                lvl, done = anim.pip_gain(now - fx["gain"], mode)
                gain[seat] = 0 if done else lvl
                if done:
                    self._pip[seat] = {}
        racks = []
        keep = []
        for t0, seat, item, kind in self._rack:
            t = now - t0
            off = 0
            if kind == "steal":
                off, slid = anim.steal_offset(t, 16, mode)
                t = t - 0.5 if slid else 0.0
            level, done = anim.item_fade(max(0.0, t), mode)
            if done:
                continue
            keep.append((t0, seat, item, kind))
            racks.append(RackFx(seat, item, max(level, 1), off))
        self._rack = keep
        if self._wipe is not None:
            col, done = anim.turn_wipe(now - self._wipe, HERO_W, mode)
            if done:
                self._wipe = None
            else:
                wipe = col
        stamp_on = True
        if self._stamp is not None:
            stamp_on = anim.stamp_visible(now - self._stamp, mode)
            if now - self._stamp >= 0.6 or mode != "full":
                self._stamp = None
        clock = now - self._epoch
        pulse = anim.pulse_phase(clock, mode) == "bright" if st.pulse else True
        beat = anim.heartbeat(clock, self._hp, mode) if st.d4 else True
        static_t = None
        if st.d5 and self._hp == 1 and mode == "full":
            static_t = int(clock / 0.1) / 10 + 0.001
        if self._think_since is not None and self._progress is not None:
            elapsed = now - self._think_since
            if elapsed >= THINK_DELAY:
                think = self._think_frame(now, elapsed)
        return WatchFrames(
            num=num, delta=delta, shells=shells, event_text=event_text,
            lost_on=(lost[0], lost[1]), gain_level=(gain[0], gain[1]), rack_fx=tuple(racks),
            wipe=wipe, stamp_on=stamp_on, pulse_bright=pulse, beat_bright=beat,
            static_t=static_t, think=think, history=tuple(self._hist),
        )

    def _think_frame(self, now: float, elapsed: float) -> ThinkFrame:
        done, total, nodes = self._progress
        frac = self._smooth.step(done, total, min(0.25, now - self._last_think))
        self._last_think = now
        label = self._labels[done] if done < len(self._labels) else "the dealer's replies"
        seg_w = max(2, min(6, 40 // max(1, total) - 1))
        pos = f"{nodes}" if nodes < 1000 else f"{nodes / 1000:.0f}k"
        return ThinkFrame(
            "checking: " + label,
            tuple(anim.p2_bar(frac, total, seg_w)),
            f"move {min(total, done + 1)}/{total} · {pos} positions · {elapsed:.1f}s",
        )

    # -- scheduling ------------------------------------------------------------------

    def busy(self, now: float) -> bool:
        """Does anything on screen still change with time?"""
        st = self.st
        if any((self._num, self._delta, self._shell, self._type, self._wipe, self._stamp,
                self._rack, self._think_since is not None, *self._pip)):
            return True
        if st.motion != "full":
            return False
        return (
            (st.pulse and self._best_shown)
            or (st.d4 and self._hp <= 2)
            or (st.d5 and self._hp == 1)
        )
