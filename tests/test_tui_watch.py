"""Watch-mode screen, animation driver, ctrl+o settings overlay (docs/ui-roadmap.md phase 1)."""

from __future__ import annotations

import asyncio
import dataclasses
import io

import pytest
from rich.console import Console

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.state import (
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from buckshotcalc.tui import analysis, anim, settings, theme, watch
from buckshotcalc.tui.panels import DashboardViewModel, render_watch
from buckshotcalc.tui.watchdriver import WatchDriver
from tests.test_tui import _reload_table, _solver, _state

PALETTES = ("amber", "green", "red")
LOG = ("dealer shot itself: BLANK", "you used the handcuffs", "you used GLASS → LIVE")


def _items():
    return (ItemCounts.of(handsaw=1, beer=1, magnifying_glass=1), ItemCounts.of(handcuffs=1))


def _vm(state=None, *, solving=False, think=None, **kw) -> DashboardViewModel:
    solver = _solver()
    state = state or _state(2, 2, items=_items())
    res = solver.evaluate(state)
    plan = analysis.plan_tree(solver, state) if state.turn is Player.PLAYER else None
    fc = ()
    if state.turn is Player.DEALER:
        fc = analysis.dealer_forecast(state, solver.rules, solver)
    base = dict(
        state=state, result=res, solving=solving, reload_table_missing=False,
        bridge_line="live from the game", plan=plan, forecast=fc, events=LOG, round_number=2,
    )
    base.update(kw)
    return DashboardViewModel(**base)


def _recap_rows(n_followed=3):
    s = _state(2, 2)
    rec = analysis.RoundRecap()
    glass = Action(ActionKind.USE, Item.MAGNIFYING_GLASS)
    shoot = Action(ActionKind.SHOOT_OPPONENT)
    saw = Action(ActionKind.USE, Item.HANDSAW)
    rec.record(s, glass, 0.61, glass, 0.61)
    rec.record(s, saw, 0.67, Action(ActionKind.USE, Item.BEER), 0.64)
    rec.record(s, shoot, 0.72, shoot, 1.0)
    return rec.rows()


def _states():
    """name -> (vm, frames) for every layout the watch view has."""
    dead = _state(1, 1, hp=(2, 0))
    think = watch.ThinkFrame("checking: use the saw", (("██████", "acc"), ("│", "dim"),
                             ("██▌   ", "warn")), "move 2/5 · 420k positions · 1.2s")
    return {
        "player": (_vm(), watch.WatchFrames()),
        "thinking": (_vm(solving=True), watch.WatchFrames(think=think)),
        "dealer": (_vm(_state(2, 1, turn=Player.DEALER)), watch.WatchFrames()),
        "paused": (_vm(bridge_paused="shells left: calc 2, game 3"), watch.WatchFrames()),
        "recap": (_vm(dead, recap=_recap_rows()), watch.WatchFrames()),
        "sawed_cuffed": (_vm(_state(2, 2, items=_items()).with_sawed(True)
                             .with_cuffed(Player.DEALER, 1)), watch.WatchFrames()),
    }


def _segments(renderable, width=160):
    console = Console(width=width, file=io.StringIO())
    return list(console.render(renderable))


def _mono(vm, frames, st=None) -> str:
    console = Console(width=140, no_color=True, force_terminal=False, record=True,
                      file=io.StringIO())
    console.print(render_watch(vm, st or settings.Settings(), frames))
    return console.export_text()


@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("name", list(_states()))
def test_transparent_mode_paints_no_background(palette, name):
    vm, fr = _states()[name]
    st = settings.Settings(palette=palette)
    segs = _segments(render_watch(vm, st, fr))
    assert segs and not [s for s in segs if s.style is not None and s.style.bgcolor is not None]


@pytest.mark.parametrize("palette", PALETTES)
def test_solid_mode_paints_the_palette_background_everywhere(palette):
    vm, fr = _states()["player"]
    st = settings.Settings(palette=palette, background="solid")
    want = theme.palette_for(st).bg_solid
    text = [s for s in _segments(render_watch(vm, st, fr)) if s.text.strip()]
    assert text and all(s.style.bgcolor is not None for s in text)
    assert {s.style.bgcolor.get_truecolor().hex for s in text} == {want}


def test_every_layout_is_exactly_the_mock_width():
    for name, (vm, fr) in _states().items():
        lines = watch.build_lines(vm, settings.Settings(), fr)
        assert {ln.n for ln in lines} == {watch.W}, name


def test_player_turn_has_the_mock_pieces():
    out = _mono(*_states()["player"])
    for piece in ("BUCKSHOTCALC", "watching · bridge live", "round 2 · load 1", "your move",
                  "chance you win this round", "other moves", "this round", "plan",
                  "in the gun", "next shell", "you used GLASS", "ctrl+o"):
        assert piece in out, piece
    assert "┌" in out and "┐" in out  # S1 frame characters
    assert "▸ " in out and "why:" in out
    # I3: glyph plus short name, in the racks
    assert "⌁ saw" in out and "⊔ beer" in out and "⌕ glass" in out and "∞ cuffs" in out


def test_best_action_and_stamps_read_without_colour():
    vm, fr = _states()["sawed_cuffed"]
    out = _mono(vm, fr)
    assert "your move ━ SAWED ━ DEALER CUFFED" in out  # B3
    marked = [ln for ln in out.splitlines() if "▸ " in ln and "█" in ln]
    assert len(marked) == 1, "exactly one alternative carries the best-move marker"


def test_live_and_blank_differ_by_text_not_colour():
    chamber = ChamberState.create((Shell.LIVE, Shell.BLANK, None), 1, 0)
    state = GameState.create(chamber, (3, 3), 4, Player.PLAYER, _items())
    out = _mono(_vm(state), watch.WatchFrames())
    assert "LIVE" in out and "blnk" in out
    assert "▐█▌" in out and "▐░▌" in out  # different glyphs too
    # plan branches are named in words
    branchy = _mono(_vm(_state(2, 2, items=_items())), watch.WatchFrames())
    assert "live" in branchy and "blank" in branchy


def test_thinking_layout_shows_p2():
    out = _mono(*_states()["thinking"])
    assert "thinking checking: use the saw" in out
    assert "move 2/5 · 420k positions · 1.2s" in out and "│" in out


def test_dealer_turn_layout_shows_t1():
    out = _mono(*_states()["dealer"])
    assert "dealer's turn - deciding..." in out and "likely: " in out and "%" in out
    assert "SHOOT" not in out  # no recommendation while the dealer moves


def test_paused_layout_shows_o1_with_the_real_keys():
    out = _mono(*_states()["paused"])
    for piece in ("BRIDGE PAUSED", "shells left: calc 2, game 3", "ctrl+g", "ctrl+z", "ctrl+e",
                  "needs you", "paused · needs you"):
        assert piece in out, piece
    assert "chance you win" not in out.split("BRIDGE PAUSED")[0]  # number withheld


def test_recap_layout_shows_r1():
    out = _mono(*_states()["recap"])
    assert "ROUND 2 LOST" in out or "ROUND 2 WON" in out
    assert "you followed the calc 2 of 3 moves" in out
    assert "= pick" in out and "≠ saw" in out and "recap" in out


# -- driver --------------------------------------------------------------------------------


def _drive(settings_=None):
    return WatchDriver(settings_ or settings.Settings(pulse=False))


def test_driver_number_tween_runs_then_rests():
    d = _drive()
    d.observe(_vm(_state(2, 2)), 0.0)
    d.observe(_vm(_state(3, 1)), 1.0)
    assert d.frames(1.1).num is not None and d.frames(1.1).delta is not None
    assert d.busy(1.1)
    end = 1.0 + 5.0
    d.frames(end)
    assert d.frames(end).num is None and d.frames(end).delta is None
    assert not d.busy(end)


def test_driver_motion_off_has_no_tween():
    d = _drive(settings.Settings(motion="off", pulse=False))
    d.observe(_vm(_state(2, 2)), 0.0)
    d.observe(_vm(_state(3, 1)), 1.0)
    assert d.frames(1.0).num is None
    assert d.frames(1.0).delta is not None  # the badge still shows, steady, for 4 s
    d.frames(6.0)
    assert not d.busy(6.0)


def test_driver_shell_hp_item_and_wipe_effects():
    d = _drive()
    before = _state(2, 2, hp=(3, 3), items=_items())
    after = GameState.create(
        ChamberState.create((None, None, None), 1, 2), (2, 3), 4, Player.DEALER,
        (ItemCounts.of(handsaw=1, magnifying_glass=1), ItemCounts.of(handcuffs=1)),
    )
    d.observe(_vm(before), 0.0)
    d.observe(_vm(after), 1.0)
    fr = d.frames(1.1)
    assert fr.shells is not None and fr.shells.slots[0][0] == "L"  # a live shell left
    assert fr.lost_on[0] in (True, False) and d._pip[0]  # my pip is flashing
    assert [(x.seat, x.item) for x in fr.rack_fx] == [(0, Item.BEER)]
    assert fr.wipe is not None  # turn handed over
    fr2 = d.frames(10.0)
    assert fr2.shells is None and not fr2.rack_fx and fr2.wipe is None


def test_driver_adrenaline_steal_slides_into_the_thief_rack():
    d = _drive()
    held = (ItemCounts.of(adrenaline=1), ItemCounts.of(beer=1))
    after = (ItemCounts.empty(), ItemCounts.empty())
    mk = lambda items: GameState.create(  # noqa: E731
        ChamberState.create((None, None), 1, 1), (3, 3), 4, Player.PLAYER, items)
    d.observe(_vm(mk(held)), 0.0)
    d.observe(_vm(mk(after)), 1.0)
    fx = {(x.seat, x.item): x for x in d.frames(1.05).rack_fx}
    assert fx[(0, Item.BEER)].offset > 0  # stolen beer slides into my rack
    assert (0, Item.ADRENALINE) in fx


def test_driver_stamp_blinks_when_sawed_appears():
    d = _drive()
    s = _state(2, 2, items=_items())
    d.observe(_vm(s), 0.0)
    d.observe(_vm(s.with_sawed(True)), 1.0)
    seen = {d.frames(1.0 + i / 100).stamp_on for i in range(60)}
    assert seen == {True, False}
    assert d.frames(3.0).stamp_on is True


def test_driver_ambient_effects_keep_it_busy_only_while_visible():
    d = WatchDriver(settings.Settings(pulse=True))
    d.observe(_vm(), 0.0)
    assert d.busy(5.0)  # best-move pulse is on screen
    d.set_settings(settings.Settings(pulse=True, motion="reduced"))
    assert not d.busy(5.0)  # reduced motion drops it
    d.set_settings(settings.Settings(pulse=False))
    d.observe(_vm(_state(2, 2, hp=(1, 3), items=_items())), 6.0)
    assert d.busy(7.0)  # heartbeat at 1 HP
    assert d.frames(7.0).static_t is not None


def test_driver_progress_block_waits_then_shows_and_never_goes_backwards():
    d = _drive()
    vm = _vm(solving=True)
    d.observe(vm, 0.0)
    d.set_progress((1, 5, 1000), ("a", "b", "c", "d", "e"))
    assert d.frames(0.1).think is None  # too quick to bother showing
    a = d.frames(0.5).think
    d.set_progress((2, 5, 90_000), ("a", "b", "c", "d", "e"))
    b = d.frames(0.9).think
    assert a.label == "checking: b" and b.label == "checking: c"
    assert "move 3/5" in b.stats and "90k positions" in b.stats
    fill = lambda t: sum(c != " " for txt, _ in t.segments for c in txt if c != "│")  # noqa: E731
    assert fill(b) >= fill(a)
    d.observe(_vm(), 1.0)
    assert d.frames(1.2).think is None and not d.busy(1.2)


# -- app: tick lifecycle and settings overlay -----------------------------------------------


def _watch_app(tmp_path, st=None):
    from buckshotcalc.tui.app import BuckshotApp

    app = BuckshotApp(settings=st or settings.Settings(pulse=False))
    app.solver._reload_table = _reload_table()
    app._bridge_seen = True
    return app


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        await pilot.pause(0.03)
        if cond():
            return True
    return False


def test_tick_interval_runs_only_while_something_moves(tmp_path):
    async def scenario():
        app = _watch_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot, lambda: not app._solving)
            await _wait(pilot, lambda: app._tick_timer is None, 200)
            idle_before = app._tick_timer
            app.session.state = _state(3, 1, items=_items())
            app._request_solve()
            await _wait(pilot, lambda: app._tick_timer is not None, 50)
            started = app._tick_timer is not None
            stopped = await _wait(pilot, lambda: app._tick_timer is None, 200)
            return idle_before, started, stopped

    idle_before, started, stopped = asyncio.run(scenario())
    assert idle_before is None, "an idle screen must not tick"
    assert started and stopped


def test_pulse_keeps_ticking_and_motion_off_stops_it(tmp_path):
    async def scenario():
        app = _watch_app(tmp_path, settings.Settings(pulse=True))
        async with app.run_test(size=(200, 50)) as pilot:
            app.session.state = _state(2, 2, items=_items())
            app._request_solve()
            await _wait(pilot, lambda: app._result_state == app.session.state)
            await pilot.pause(1.5)
            pulsing = app._tick_timer is not None
            app._set_settings(dataclasses.replace(app.settings, motion="off"))
            await _wait(pilot, lambda: app._tick_timer is None, 100)
            return pulsing, app._tick_timer

    pulsing, after = asyncio.run(scenario())
    assert pulsing and after is None


def test_watch_view_is_used_only_while_the_bridge_feeds_events(tmp_path):
    async def scenario():
        app = _watch_app(tmp_path)
        app._bridge_seen = False
        async with app.run_test(size=(200, 50)) as pilot:
            entry = app._watching()
            app._bridge_seen = True
            watching = app._watching()
            await pilot.press("ctrl+e")
            manual = app._watching()
            await pilot.press("ctrl+e")
            app._bridge.off = "bridge: campaign not supported, manual entry"
            return entry, watching, manual, app._watching()

    assert asyncio.run(scenario()) == (False, True, False, False)


def test_settings_overlay_changes_palette_and_saves(tmp_path, monkeypatch):
    target = tmp_path / "tui.toml"
    monkeypatch.setattr(settings, "default_path", lambda: target)

    async def scenario():
        app = _watch_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            await pilot.press("right")  # palette: amber -> green (live preview)
            await pilot.pause(0.05)
            live = app.settings.palette
            await pilot.press("down", "down", "right")  # number animation: N3 -> N4
            await pilot.press("escape")
            await pilot.pause(0.1)
            return live, app.settings

    live, final = asyncio.run(scenario())
    assert live == "green" and final.palette == "green" and final.number_anim == "N4"
    assert settings.load(target) == final


def test_settings_overlay_exposes_reveal_scale(tmp_path, monkeypatch):
    from buckshotcalc.tui import timing
    from buckshotcalc.tui.settings_screen import ROWS, step

    monkeypatch.setattr(settings, "default_path", lambda: tmp_path / "t.toml")
    monkeypatch.setattr(timing, "REVEAL_SCALE", 1.0)
    assert [f for f, _, _ in ROWS] == [
        "palette", "background", "number_anim", "d1", "d2", "d4", "d5", "motion", "pulse",
        "reveal_scale",
    ]
    assert step(settings.Settings(), "reveal_scale", 1).reveal_scale == 1.5
    assert step(settings.Settings(), "reveal_scale", -2).reveal_scale == 0.0

    async def scenario():
        app = _watch_app(tmp_path)
        async with app.run_test(size=(200, 50)):
            app._set_settings(dataclasses.replace(app.settings, reveal_scale=0.5))
            return timing.REVEAL_SCALE

    assert asyncio.run(scenario()) == 0.5


def test_ctrl_o_and_ctrl_e_are_on_screen_in_both_layouts():
    from buckshotcalc.tui.panels import render_dashboard
    from tests.test_tui import RULES

    vm, fr = _states()["player"]
    console = Console(width=160, no_color=True, record=True, file=io.StringIO())
    console.print(render_dashboard(vm, RULES))
    entry = console.export_text()
    for text in (entry, _mono(vm, fr)):
        assert "ctrl+o" in text and "ctrl+e" in text


def test_recap_is_fed_from_the_calcs_pick_and_the_value_after(tmp_path):
    from buckshotcalc.tui.app import _taken_action

    async def scenario():
        app = _watch_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            app.session.state = _state(2, 2, items=_items())
            app._request_solve()
            await _wait(pilot, lambda: app._result_state == app.session.state)
            taken = _taken_action({"ev": "item", "actor": "player", "item": "handsaw"})
            rec = app._recommendation_for(taken)
            stale = app._result_state
            app._result_state = None
            none_when_stale = app._recommendation_for(taken)
            app._result_state = stale
            app._recap_pending.append(rec)
            app.session.use_item(Item.HANDSAW)
            app._request_solve()
            await _wait(pilot, lambda: not app._recap_pending and not app._solving)
            return rec, none_when_stale, app._recap.rows(), app._result.value

    rec, stale, rows, value = asyncio.run(scenario())
    assert stale is None
    assert len(rows) == 1 and rows[0].value_after == value
    assert rows[0].followed == (rec[1] == rec[3]) and rows[0].recommended_value == rec[2]


def test_taken_action_maps_only_the_players_own_moves():
    from buckshotcalc.tui.app import _taken_action

    assert _taken_action({"ev": "shot", "actor": "player", "target": "self"}) == Action(
        ActionKind.SHOOT_SELF)
    assert _taken_action({"ev": "shot", "actor": "dealer", "target": "self"}) is None
    assert _taken_action({"ev": "item", "actor": "player", "item": "magnifying glass"}) == Action(
        ActionKind.USE, Item.MAGNIFYING_GLASS)
    assert _taken_action({"ev": "item", "actor": "player", "item": "beer", "stolen": True}) is None


# -- layout: settings box, narrow stack, fixed rows, 2x number, plan, recap, low health ----------


def _plain(vm, fr, st=None, **kw) -> list[str]:
    return [
        "".join(ch for ch, _ in ln.c)
        for ln in watch.build_lines(vm, st or settings.Settings(), fr, **kw)
    ]


def test_settings_box_fits_its_help_line():
    from buckshotcalc.tui.settings_screen import _W, SettingsScreen, settings_lines

    lines = settings_lines(settings.Settings(), 0)
    text = ["".join(ch for ch, _ in ln.c) for ln in lines]
    assert any("esc save and close" in t for t in text)
    assert {ln.n for ln in lines} == {_W}
    assert f"width: {_W};" in SettingsScreen.DEFAULT_CSS


def test_narrow_stack_at_80_columns_loses_nothing():
    for name, (vm, fr) in _states().items():
        wide = "\n".join(_plain(vm, fr))
        out = _mono(vm, fr)  # at 140 columns: side by side
        console = Console(width=80, no_color=True, force_terminal=False, record=True,
                          file=io.StringIO())
        console.print(render_watch(vm, settings.Settings(), fr))
        narrow = console.export_text()
        lines = _plain(vm, fr, w=watch.NARROW_W)
        assert {len(x) for x in lines} == {watch.NARROW_W}, name
        # every word of the wide layout is still on screen
        for tok in set(wide.split()) - set("│┌┐└┘─┄╌╎·"):
            if tok.strip("─┄╌╎┌┐└┘│·"):
                assert tok in narrow or tok in out and name == "paused", (name, tok)
        assert "ctrl+q quit" in narrow and "ctrl+o settings" in narrow


def test_narrow_layout_stacks_the_side_column_under_the_hero():
    vm, fr = _states()["player"]
    rows = _plain(vm, fr, w=watch.NARROW_W)
    hero_end = next(i for i, r in enumerate(rows) if r.startswith("└"))
    assert hero_end < next(i for i, r in enumerate(rows) if r.startswith("other moves"))
    assert all(len(r) <= 80 for r in rows)


def test_rows_below_the_top_block_never_move():
    where = {}
    for name, (vm, fr) in _states().items():
        rows = _plain(vm, fr)
        where[name] = (len(rows), next(i for i, r in enumerate(rows) if "ctrl+q" in r))
        if name != "recap":
            where[name] += (next(i for i, r in enumerate(rows) if "in the gun" in r),)
    assert len({v[:2] for v in where.values()}) == 1, where
    gun = {v[2] for k, v in where.items() if k != "recap"}
    assert len(gun) == 1, where


def test_a_wrapped_rack_is_never_cropped():
    many = (ItemCounts.of(handsaw=1, beer=2, magnifying_glass=1, cigarettes=2, handcuffs=1,
                          adrenaline=1, inverter=1),) * 2
    vm = _vm(_state(2, 2, items=many))
    rows = _plain(vm, watch.WatchFrames())
    assert sum("⇅ invert" in r for r in rows) == 2  # the last token of each rack
    assert "ctrl+q quit" in rows[-1]


def test_plan_has_no_dangling_ellipsis_row():
    N = analysis.PlanNode
    cut = N("", 1.0, "USE CIGARETTES", None, (
        N("", 1.0, "SHOOT OPPONENT", None, (
            N("live", 0.5, None, "..."),
            N("blank", 0.5, "SHOOT OPPONENT", None, (N("", 1.0, None, "you win"),)),
        )),
    ))
    text = "\n".join("".join(ch for ch, _ in ln.c) for ln in watch.plan_lines(cut))
    assert "..." not in text.split("blank")[0] and "live" not in text
    assert "then   " not in text


def test_recap_card_replaces_other_moves_and_this_round():
    rows = _plain(*_states()["recap"])
    text = "\n".join(rows)
    assert "other moves" not in text and "nothing to choose" not in text
    assert "this round" not in text and "recap" in text
    assert "you ♥" in text  # the racks stay


def test_big_number_only_when_the_terminal_has_room():
    vm, fr = _states()["player"]

    def shown(width, height):
        console = Console(width=width, height=height, no_color=True, force_terminal=False,
                          record=True, file=io.StringIO())
        console.print(render_watch(vm, settings.Settings(), fr), height=height)
        return console.export_text()

    roomy, tight = shown(200, 50).splitlines(), shown(200, 30).splitlines()
    digits = lambda rows: sum("██████" in r for r in rows[:22])  # noqa: E731
    assert digits(roomy) >= 4 and digits(tight) == 0
    top = next(i for i, r in enumerate(roomy) if "BUCKSHOTCALC" in r)
    assert top == (50 - len(_plain(vm, fr, big=True))) // 2  # centred
    assert next(i for i, r in enumerate(tight) if "BUCKSHOTCALC" in r) == 0  # no spare rows


def test_widest_number_fits_at_2x():
    rows = [(r, "fg") for r in anim.big("100.0%")]
    assert max(len(r) for r, _ in watch._double(rows, 54)) <= 54
    assert len(watch._double(rows, 54)) == 10
    assert watch._double(rows, 40) == rows  # too wide: stays 1x


def _low(hp, **kw):
    st = settings.Settings(pulse=False, **kw)
    s = _state(2, 1, hp=(hp, 3), turn=Player.DEALER, items=_items())
    vm = _vm(s)
    fr = watch.WatchFrames(static_t=0.001)
    return vm, st, fr, watch.build_lines(vm, st, fr)


def _roles(lines, row):
    return {r for _, r in lines[row].c if r != "sp"}


def test_low_health_looks_show_on_the_dealers_turn_and_outside_the_hero():
    vm, st, fr, lines = _low(1)
    text = ["".join(c for c, _ in ln.c) for ln in lines]
    assert "CRITICAL - dealer's turn" in "\n".join(text)  # D4, in words
    heading = next(i for i, t in enumerate(text) if "dealer's turn" in t[60:])
    assert "bad" in {r for c, r in lines[heading].c[60:] if c != " "}  # D1: the side heading
    assert "bad" in _roles(lines, 0) and _roles(lines, 0) - {"bad", "acc"} == set()  # header
    assert {r for _, r in lines[-1].c if r != "sp"} == {"bad"}  # footer
    border = [t[0] for t in text[2:12]] + [t[watch.HERO_W - 1] for t in text[2:12]] + list(text[12])
    assert any(c in "╎╌┄· " for c in border)  # D2: the dealer's frame is cracked too
    assert any(c in "▚▞░▒" for c in text[0])  # D5 static reaches the header
    # warn at 2 HP, plain at 3 and above
    assert "warn" in _roles(_low(2)[3], -1)
    assert {r for _, r in _low(3)[3][-1].c if r != "sp"} == {"dim"}


def test_low_health_looks_follow_their_toggles_and_the_heartbeat():
    off = _low(1, d1=False, d2=False, d4=False, d5=False)[3]
    text = ["".join(c for c, _ in ln.c) for ln in off]
    assert {r for _, r in off[-1].c if r != "sp"} == {"dim"}
    assert "CRITICAL" not in "\n".join(text) and not any(c in "▚▞░▒" for c in text[0])
    vm, st, _, _ = _low(1)
    dark = watch.build_lines(vm, st, watch.WatchFrames(beat_bright=False))
    assert {r for _, r in dark[-1].c if r != "sp"} == {"dim"}  # the beat's dark half


def test_low_health_reaches_the_recap_card_and_the_pause_box():
    st = settings.Settings(pulse=False)
    for vm in (_vm(_state(2, 2, hp=(0, 1)), recap=_recap_rows()),
               _vm(_state(2, 2, hp=(1, 3), items=_items()), bridge_paused="x")):
        lines = watch.build_lines(vm, st, watch.WatchFrames())
        card = ["".join(c for c, _ in ln.c)[:watch.HERO_W] for ln in lines[1:16]]
        assert any(c in "╎╌┄" for row in card for c in row)  # cracked


def test_watch_alternatives_show_death_chance():
    from tests.test_tui import RULES, _solver, _state

    s = _state(2, 3, hp=(1, 3))
    vm = DashboardViewModel(
        state=s, result=_solver().evaluate(s), solving=False, reload_table_missing=False,
        rules=RULES,
    )
    console = Console(width=200, no_color=True, force_terminal=False, record=True)
    console.print(render_watch(vm, settings.Settings(pulse=False)))
    assert "dies 40%" in console.export_text()
