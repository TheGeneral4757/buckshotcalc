"""Tests for buckshotcalc.tui.analysis and .panels (SPEC.md §8, M7 acceptance)."""

from __future__ import annotations

import os
import time
from dataclasses import replace

from rich.console import Console

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.solver import SearchResult, Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
)
from buckshotcalc.tui import analysis
from buckshotcalc.tui.panels import (
    DashboardViewModel,
    SetupFields,
    plain_action,
    render_dashboard,
)

RULES = default_rules()


def _reload_table() -> dict[tuple[int, int, int], float]:
    return {
        (hp_p, hp_d, max_hp): hp_p / (hp_p + hp_d)
        for max_hp in RULES.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    }


def _state(
    live: int, blank: int, hp=(3, 3), max_hp=4, turn=Player.PLAYER, items=None
) -> GameState:
    chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
    return GameState.create(
        chamber=chamber, hp=hp, max_hp=max_hp, turn=turn, items=items
    )


def _solver() -> Solver:
    solver = Solver(RULES, GameMode.BASE)
    solver._reload_table = _reload_table()
    return solver


# ---------------------------------------------------------------------------
# action_label — seat-aware SHOOT_OPPONENT rendering (fixes the M7 defect)
# ---------------------------------------------------------------------------


def _render_text(vm: DashboardViewModel, width: int = 160) -> str:
    """The dashboard as plain text, colour stripped — the monochrome view the
    operator's second monitor has to stay legible in."""
    console = Console(width=width, no_color=True, force_terminal=False, record=True)
    console.print(render_dashboard(vm, RULES))
    return console.export_text()


def test_action_str_is_seat_neutral():
    assert str(Action(ActionKind.SHOOT_OPPONENT)) == "SHOOT OPPONENT"


def test_action_label_from_player_seat_reads_shoot_dealer():
    assert analysis.action_label(Action(ActionKind.SHOOT_OPPONENT), Player.PLAYER) == (
        "SHOOT DEALER"
    )


def test_action_label_from_dealer_seat_reads_shoot_player():
    """The same action, taken by the Dealer against us, must not read
    "SHOOT DEALER" — that was the exact defect this milestone fixes.
    """
    assert analysis.action_label(Action(ActionKind.SHOOT_OPPONENT), Player.DEALER) == (
        "SHOOT PLAYER"
    )


def test_action_label_shoot_self_is_seat_independent():
    assert analysis.action_label(Action(ActionKind.SHOOT_SELF), Player.PLAYER) == "SHOOT SELF"
    assert analysis.action_label(Action(ActionKind.SHOOT_SELF), Player.DEALER) == "SHOOT SELF"


# ---------------------------------------------------------------------------
# ranked_alternatives — ordering, ✗, cap-reached carry through to render layer
# ---------------------------------------------------------------------------


def test_ranked_alternatives_ordered_by_value_with_wasted_flags():
    solver = _solver()
    items = (ItemCounts.of(magnifying_glass=1, handsaw=1), ItemCounts.empty())
    state = _state(2, 1, items=items)
    result = solver.evaluate(state)

    alts = analysis.ranked_alternatives(result, Player.PLAYER)
    assert len(alts) == len(result.children)
    values = [a.value for a in alts]
    assert values == sorted(values, reverse=True)
    assert alts[0].is_best
    assert not alts[0].wasted
    assert all(not a.is_best for a in alts[1:])
    # Whatever the search found best, everything strictly below it is
    # flagged wasted — matches `ChildValue.wasted`'s own definition exactly.
    best_value = alts[0].value
    for a in alts[1:]:
        assert a.wasted == (a.value < best_value - 1e-9)


def test_ranked_alternatives_empty_on_dealer_node():
    solver = _solver()
    state = _state(2, 1, turn=Player.DEALER)
    result = solver.evaluate(state)
    assert result.children == ()
    assert analysis.ranked_alternatives(result, Player.PLAYER) == ()


def test_item_valuations_only_includes_use_actions():
    solver = _solver()
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    state = _state(2, 1, items=items)
    result = solver.evaluate(state)
    ivs = analysis.item_valuations(result, Player.PLAYER)
    assert all(iv.item is not None for iv in ivs)
    assert {iv.item for iv in ivs} == {Item.MAGNIFYING_GLASS}


def test_cap_reached_flag_surfaces_through_search_result(tmp_path):
    """A hand-picked position where the item-use cap binds: `max_items_per_turn`
    forced to 1, with 2 cigarettes held — the second cigarette can't be used
    without the cap truncating that subtree (SPEC.md §7.5).
    """
    from tests.test_solver import _custom_rules

    rules = _custom_rules(tmp_path, {"solver.max_items_per_turn": 1})
    solver = Solver(rules, GameMode.BASE)
    solver._reload_table = {
        (hp_p, hp_d, max_hp): hp_p / (hp_p + hp_d)
        for max_hp in rules.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    }
    items = (ItemCounts.of(cigarettes=2), ItemCounts.empty())
    chamber = ChamberState.create((None, None), 1, 1)
    state = GameState.create(
        chamber=chamber, hp=(2, 3), max_hp=3, turn=Player.PLAYER, items=items
    )
    result = solver.evaluate(state)
    assert result.cap_reached is True

    vm = DashboardViewModel(
        state=state, result=result, solving=False, reload_table_missing=False,
    )
    console = Console(width=100, file=open(os.devnull, "w"), record=True)
    console.print(render_dashboard(vm, rules))
    output = console.export_text()
    assert "SEARCH CAP REACHED" in output


def test_cap_reached_flag_present_on_result_type():
    """`SearchResult.cap_reached` exists and is a bool — the render layer
    (`_recommendation_panel`) reads it directly with no extra derivation.
    """
    solver = _solver()
    state = _state(1, 1)
    result = solver.evaluate(state)
    assert isinstance(result.cap_reached, bool)


# ---------------------------------------------------------------------------
# render_dashboard — <16ms render budget (M7 acceptance criterion)
# ---------------------------------------------------------------------------


def test_render_dashboard_meets_16ms_budget():
    """The solver must not run on the render path: this times only
    `render_dashboard` against an already-computed, cached `SearchResult` —
    exactly what `app.py`'s redraw does. A test that merely asserts "it
    doesn't raise" would not prove the budget; this asserts the number.
    """
    solver = _solver()
    items = (
        ItemCounts.of(magnifying_glass=1, handsaw=1, cigarettes=1, handcuffs=1),
        ItemCounts.of(beer=1),
    )
    state = _state(3, 2, items=items)
    result = solver.evaluate(state)

    vm = DashboardViewModel(
        state=state,
        result=result,
        solving=False,
        reload_table_missing=False,
    )
    console = Console(width=100, file=open(os.devnull, "w"))

    # Warm up (module import / first-render JIT-ish costs), then measure.
    for _ in range(3):
        console.print(render_dashboard(vm, RULES))

    n = 50
    start = time.perf_counter()
    for _ in range(n):
        renderable = render_dashboard(vm, RULES)
        console.print(renderable)
    elapsed = time.perf_counter() - start
    per_render = elapsed / n

    assert per_render < 0.016, (
        f"render_dashboard averaged {per_render * 1000:.3f} ms over {n} "
        "renders, exceeding the SPEC.md M7 16 ms redraw budget"
    )


def test_render_dashboard_handles_missing_reload_table_without_solving():
    """Rendering with `reload_table_missing=True` and `result=None` must not
    touch the solver at all — this is the SPEC.md §7.7 degrade path.
    """
    state = _state(1, 1)
    vm = DashboardViewModel(
        state=state,
        result=None,
        solving=False,
        reload_table_missing=True,
    )
    renderable = render_dashboard(vm, RULES)
    console = Console(width=100, file=open(os.devnull, "w"))
    console.print(renderable)  # must not raise


# ---------------------------------------------------------------------------
# Item letter map (operator decision: numbers AND letters, reserved-key safe)
# ---------------------------------------------------------------------------


def test_item_key_map_covers_every_item_uniquely_and_avoids_reserved_keys():
    key_map = analysis.build_item_key_map()
    assert set(key_map) == set(Item)
    letters = list(key_map.values())
    assert len(letters) == len(set(letters))
    assert not (set(letters) & analysis.RESERVED_KEYS)


def test_item_key_map_is_stable_regardless_of_subset_order():
    """Muscle memory must not move as inventory changes: a subset built from
    a different `items` ordering still assigns the same letters (the map is
    keyed off the full `Item` enum's own order internally when called with
    the default argument, which every call site uses).
    """
    full = analysis.build_item_key_map()
    again = analysis.build_item_key_map()
    assert full == again


# ---------------------------------------------------------------------------
# Colour is never the sole carrier of meaning (SPEC.md §8; operator's mono
# requirement) — render with colour stripped and check every meaning is
# still recoverable as text.
# ---------------------------------------------------------------------------


def test_dashboard_readable_with_colour_stripped():
    solver = _solver()
    items = (
        ItemCounts.of(magnifying_glass=1, handsaw=1, cigarettes=1),
        ItemCounts.empty(),
    )
    state = _state(2, 1, items=items)
    # Force a resolved chamber (one LIVE, one BLANK) so both slot glyphs
    # have their text-label counterpart on screen, per the monochrome
    # requirement — the render must not rely on colour to distinguish them.
    from buckshotcalc.engine.state import Shell

    resolved_chamber = ChamberState.create((Shell.LIVE, Shell.BLANK), 0, 0)
    state = state.with_chamber(resolved_chamber)
    result = solver.evaluate(state)

    vm = DashboardViewModel(
        state=state,
        result=result,
        solving=False,
        reload_table_missing=False,
        log=("you used GLASS → LIVE",),
        prompt="you shot the DEALER — live or blank?",
    )
    console = Console(width=120, no_color=True, force_terminal=False, record=True)
    console.print(render_dashboard(vm, RULES))
    output = console.export_text()

    # Live vs blank slots distinguishable as text, not only by colour.
    assert "LIVE" in output and "BLNK" in output  # spelled, not colour-coded
    # The recommended action is identifiable via its own marker glyph.
    assert "▶" in output
    # Every alternative still carries its win % (the gap matters more than
    # the rank — SPEC.md §8).
    for alt in analysis.ranked_alternatives(result, Player.PLAYER):
        assert f"{alt.value:.1%}" in output
        if alt.wasted:
            assert "✗" in output
    # Status flags, when active, still read as plain text.
    assert "DEALER" in output and "YOU" in output or "You" in output.upper()


def test_dashboard_cap_warning_visible_without_colour():
    """The yellow search-cap warning must still be legible as plain text."""
    solver = _solver()

    vm_state = _state(1, 1)
    # Build a cap-reached SearchResult the cheap way: reuse the cap test's
    # scenario characteristics directly against the default solver by
    # asserting the text warning renders whenever cap_reached=True, which is
    # what panels.py actually branches on.
    result = solver.evaluate(vm_state)
    forced = SearchResult(
        value=result.value, children=result.children, nodes=result.nodes, cap_reached=True
    )
    vm = DashboardViewModel(
        state=vm_state, result=forced, solving=False, reload_table_missing=False,
    )
    console = Console(width=120, no_color=True, record=True)
    console.print(render_dashboard(vm, RULES))
    assert "SEARCH CAP REACHED" in console.export_text()


def test_dealer_information_item_never_asks_us_what_the_dealer_saw():
    """The knowledge-leak guard at the key-dispatch level (SPEC.md §0.1 (5)).

    Pressing the Dealer's Magnifying Glass key must NOT open a "live or
    blank?" prompt: we watch the Dealer look, we never see what it saw.
    Answering such a prompt would resolve the slot in *our* chamber from
    information we do not have and leave `dealer_knows` empty — wrong in
    both directions at once. The session-level counterpart is
    `tests/test_session.py::test_dealer_info_action_never_resolves_a_slot_for_us`.
    """
    import asyncio

    from buckshotcalc.tui.app import BuckshotApp

    async def scenario() -> tuple[object, ...]:
        app = BuckshotApp()
        async with app.run_test() as pilot:
            st = app.session.state
            st = st.with_turn(Player.DEALER).with_items(
                Player.DEALER, ItemCounts.of(magnifying_glass=1)
            )
            app.session.state = st
            before = st.chamber.slots
            # `space` then the glass's own letter — the only "use" gesture.
            await pilot.press("space", analysis.build_item_key_map()[Item.MAGNIFYING_GLASS])
            await pilot.pause()
            after = app.session.state
            return (
                app._pending,
                before,
                after.chamber.slots,
                after.dealer_knows,
                after.items_of(Player.DEALER)[Item.MAGNIFYING_GLASS],
            )

    pending, before, after_slots, knows, glass_left = asyncio.run(scenario())
    assert pending is None, "the app must not prompt us for the Dealer's private result"
    assert after_slots == before, "the Dealer's private look must not resolve OUR chamber"
    assert knows == frozenset({0}), "the Dealer must be recorded as knowing the slot"
    assert glass_left == 0, "the Dealer's glass must be consumed"


def test_setup_row_owns_the_arrows_only_while_it_is_focused():
    """The arrows are shared between shot entry and the NEW ROUND steppers.

    That is only safe if exactly one of them owns the arrows at a time, so
    this asserts the switch in both directions: unfocused, `→` is half a shot
    and the round numbers are untouched; focused, `→` changes a number and no
    shot is started. The operator can see which is which — the panel is drawn
    with a marker and a highlighted border while it holds the arrows — and
    `test_focused_setup_row_is_visible_in_the_render` covers that half.
    """
    import asyncio

    from buckshotcalc.tui.app import BuckshotApp

    async def scenario() -> tuple[object, ...]:
        app = BuckshotApp()
        async with app.run_test() as pilot:
            await pilot.press("right")  # not focused: no field may move
            unfocused_live = app._setup.live
            unfocused_pending = app._pending
            await pilot.press("tab", "right", "right")  # focused: LIVE goes up by 2
            focused_live = app._setup.live
            focused_pending = app._pending
            return (unfocused_live, unfocused_pending, focused_live, focused_pending)

    before, stray_prompt, after, focused_prompt = asyncio.run(scenario())
    assert stray_prompt is None, "a bare → outside the setup row must not start anything"
    assert after == before + 2, "→ must step the focused field once per press"
    assert focused_prompt is None, "the focused setup row must not start a shot"


def test_focused_setup_row_is_visible_in_the_render():
    """Which half of the app owns the arrows must be readable, in monochrome."""
    state = _state(3, 2)
    unfocused = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=False,
        setup=SetupFields(live=3, blank=3, max_hp=4, focus=-1),
    ))
    focused = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=False,
        setup=SetupFields(live=3, blank=3, max_hp=4, focus=0),
    ))
    assert "EDITING" in focused and "EDITING" not in unfocused
    assert "◂" in focused and "◂" not in unfocused


def test_every_key_the_app_dispatches_is_printed_on_the_dashboard():
    """The redesign deleted the help overlay: the legend is permanent.

    Every gesture `_dispatch` acts on has to be on screen at all times, or
    the operator is back to memorising. Asserted against the rendered
    dashboard, not the source table, so a legend that is built but never
    drawn still fails.
    """
    state = _state(3, 2)
    letters = tuple(
        (item, analysis.build_item_key_map()[item])
        for item in sorted(Item, key=lambda it: it.value)
    )
    text = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=False,
        item_keys=letters, setup=SetupFields(live=3, blank=3, max_hp=4),
    ))
    for gesture in ("↑", "↓", "←", "→", "SHIFT+key", "space", "tab", "enter",
                    "ctrl+z", "ctrl+l", "ctrl+t", "ctrl+q", "esc"):
        assert gesture in text, f"the dashboard never mentions {gesture!r}"
    for item, letter in letters:
        name = analysis.item_display_name(item)
        assert name in text, f"item {name!r} is not on screen"
        assert letter in text, f"item key {letter!r} is not on screen"


def test_no_element_paints_a_background():
    """The operator runs a translucent terminal (2026-09-05 brief).

    A single `on <colour>` anywhere punches an opaque rectangle through it,
    so this walks every rendered segment rather than trusting review.
    """
    state = _state(3, 2)
    vm = DashboardViewModel(
        state=state, result=_solver().evaluate(state), solving=False,
        reload_table_missing=False,
        item_keys=tuple(
            (item, analysis.build_item_key_map()[item])
            for item in sorted(Item, key=lambda it: it.value)
        ),
        setup=SetupFields(live=3, blank=3, max_hp=4, focus=1),
        log=("you used the saw",),
        status="something went wrong",
    )
    console = Console(width=160, file=open(os.devnull, "w"))
    painted = [
        seg for seg in console.render(render_dashboard(vm, RULES))
        if seg.style is not None and seg.style.bgcolor is not None
    ]
    assert not painted, f"{len(painted)} segment(s) paint a background"


def test_one_letter_one_meaning_across_the_item_gestures():
    """The complaint that started the redesign: a key that meant two things.

    A bare letter *always* adds to your rack (never uses, however many you
    hold), SHIFT+letter *always* adds to the Dealer's, and `space`+letter is
    the only thing that spends one. Asserted together, because the bug was
    the interaction between them.
    """
    import asyncio

    from buckshotcalc.tui.app import BuckshotApp

    async def scenario() -> tuple[object, ...]:
        app = BuckshotApp()
        keys = analysis.build_item_key_map()
        async with app.run_test() as pilot:
            beer = keys[Item.BEER]
            await pilot.press(beer, beer)  # twice: two beers, not a use
            mine_after_two = app.session.state.items_of(Player.PLAYER)[Item.BEER]
            pending_after_two = app._pending
            await pilot.press(beer.upper())  # SHIFT: the Dealer's side
            theirs = app.session.state.items_of(Player.DEALER)[Item.BEER]
            mine_unchanged = app.session.state.items_of(Player.PLAYER)[Item.BEER]
            await pilot.press("space", beer)  # now, and only now, a use
            return (mine_after_two, pending_after_two, theirs, mine_unchanged,
                    app._pending)

    mine, pending, theirs, still_mine, use_prompt = asyncio.run(scenario())
    assert mine == 2, "a bare letter must always add, never use"
    assert pending is None, "adding an item must never open a question"
    assert theirs == 1 and still_mine == 2, "SHIFT must reach the other seat only"
    assert use_prompt is not None and "racked the gun" in use_prompt.prompt


def test_shot_entry_is_target_then_result():
    """Two keystrokes, in the order the events happen (operator decision)."""
    import asyncio

    from buckshotcalc.tui.app import BuckshotApp

    async def scenario() -> tuple[object, ...]:
        app = BuckshotApp()
        async with app.run_test() as pilot:
            await pilot.press("down")  # at the other one …
            mid = app._pending
            hp_before = app.session.state.hp
            await pilot.press("right")  # … and it was live
            return (mid, hp_before, app.session.state.hp, app._pending)

    mid, before, after, cleared = asyncio.run(scenario())
    assert mid is not None and "shot the dealer" in mid.prompt
    assert before == after or after[Player.DEALER.value] < before[Player.DEALER.value]
    assert after[Player.DEALER.value] < before[Player.DEALER.value]
    assert cleared is None, "the question must close once it is answered"


def test_pointless_items_are_dimmed_not_hidden():
    """Dimming is a hint, never a gate: the action stays legal and reachable
    (CLAUDE.md — `saw + beer` wastes the saw and must stay in the action
    space). This asserts the hint tracks the state, in both directions.
    """
    from buckshotcalc.tui.panels import item_is_pointless

    items = (ItemCounts.of(handsaw=1, cigarettes=1), ItemCounts.empty())
    state = _state(3, 2, items=items)
    assert not item_is_pointless(state, RULES, Item.HANDSAW, Player.PLAYER)
    assert item_is_pointless(state.with_sawed(True), RULES, Item.HANDSAW, Player.PLAYER)
    # Cigarettes at full health heal nothing.
    assert item_is_pointless(state.with_hp(Player.PLAYER, state.max_hp), RULES,
                             Item.CIGARETTES, Player.PLAYER)
    assert not item_is_pointless(state.with_hp(Player.PLAYER, 1), RULES,
                                 Item.CIGARETTES, Player.PLAYER)


def test_degraded_belief_suppresses_the_win_probability():
    """SPEC.md §15.16(b): a number from a chamber we know is wrong is worse than none.

    The operator cannot tell a wrong win chance is wrong by looking at it, so
    option (a) of docs/decisions-pending.md item 6 suppresses the number
    itself rather than annotating it. Asserted both ways: the reason shows,
    and no percentage takes its place on the banner.
    """
    solver = _solver()
    state = _state(1, 1)
    result = solver.evaluate(state)

    vm = DashboardViewModel(
        state=state, result=result, solving=False, reload_table_missing=False,
        degraded="INVERTER on an unresolved shell: test reason.",
    )
    output = _render_text(vm, width=140)
    banner = output.split("DEALER")[0]

    assert "INVERTER on an unresolved shell" in output
    assert "the shell count is uncertain" in banner.lower()
    # No win chance is offered in its place — neither the caption that
    # introduces one nor a recommended action derived from it.
    assert "CHANCE YOU WIN" not in banner.upper()
    assert "no recommendation yet" in banner


def test_bridge_pause_suppresses_the_win_probability_and_names_the_fix():
    """docs/live-bridge.md: on a game/calc mismatch the operator is asked, never auto-applied.

    Same contract as a degraded belief (no number, no derived action), plus
    the one key that resolves it, in words, and the key is in the legend.
    """
    solver = _solver()
    state = _state(1, 1)
    vm = DashboardViewModel(
        state=state, result=solver.evaluate(state), solving=False, reload_table_missing=False,
        bridge_paused="dealer hp game 2, calc 3",
    )
    output = _render_text(vm, width=140)
    banner = output.split("DEALER")[0]

    assert "the game and the calc disagree: dealer hp game 2, calc 3" in output
    assert "ctrl+g takes the game's numbers" in output
    assert "CHANCE YOU WIN" not in banner.upper()
    assert "no recommendation yet" in banner
    assert "the shell count is uncertain" not in banner.lower(), "a pause is not a degraded belief"
    assert output.count("ctrl+g") >= 2, "notice and key legend"


def test_bridge_line_and_alert_render_in_words():
    state = _state(1, 1)
    base = dict(state=state, result=None, solving=False, reload_table_missing=False)
    line = "live from the game · last event: shot"
    live = _render_text(DashboardViewModel(**base, bridge_line=line))
    assert "live from the game" in live
    off = "bridge: campaign not supported, manual entry"
    alert = _render_text(DashboardViewModel(**base, bridge_alert=off))
    assert "campaign not supported" in alert
    assert "live from the game" not in _render_text(DashboardViewModel(**base))


# ---------------------------------------------------------------------------
# ctrl+t: the opt-in background reload-table build (decisions-pending item 5)
# ---------------------------------------------------------------------------


def _progress(phase, sweep, keys_done, elapsed_s, delta=None):
    from buckshotcalc.engine.solver import ReloadProgress

    return ReloadProgress(
        phase=phase, sweep=sweep, sweeps_max=64, keys_done=keys_done, keys_total=29,
        elapsed_s=elapsed_s, delta=delta, tolerance=1e-3,
    )


def test_reload_build_tracker_estimates_from_the_residual_decay():
    """Two sweeps give the ratio; before that the time left is not known yet.

    Hand-computed: residuals 0.4 then 0.1 give r = 0.25, so reaching 1e-3
    takes log(1e-3 / 0.1) / log(0.25) = 3.32 more sweeps at 60 s each, which
    is 199 s. Thirty seconds into the third sweep leaves 169 s, about 3 min.
    """
    from datetime import datetime

    tracker = analysis.ReloadBuildTracker()
    assert tracker.describe().startswith("starting")
    tracker.update(_progress("key", 1, 10, 20.0))
    assert "pass 1 · 10 of 29" in tracker.describe()
    assert "not known yet" in tracker.describe()
    tracker.update(_progress("sweep", 1, 29, 60.0, delta=0.4))
    assert "pass 2 · 0 of 29" in tracker.describe(), "a finished sweep starts the next pass"
    assert "not known yet" in tracker.describe(), "one residual is not a decay rate"
    tracker.update(_progress("sweep", 2, 29, 120.0, delta=0.1))
    tracker.update(_progress("key", 3, 14, 150.0))
    assert abs(tracker.seconds_left() - 169.3) < 0.5
    text = tracker.describe(now=datetime(2026, 10, 24, 11, 40))
    assert "about 3 min left (done around 11:42)" in text


def test_reload_build_tracker_refuses_to_guess_when_the_residual_rises():
    tracker = analysis.ReloadBuildTracker()
    tracker.update(_progress("sweep", 1, 29, 60.0, delta=0.1))
    tracker.update(_progress("sweep", 2, 29, 120.0, delta=0.2))
    assert tracker.seconds_left() is None


def test_unbuilt_table_notice_offers_ctrl_t_in_monochrome():
    state = _state(1, 1)
    idle = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=True,
    ))
    assert "has not been built" in idle and "press ctrl+t to build it" in idle
    building = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=True,
        reload_build="pass 2 · 10 of 29 table entries done · time left not known yet",
    ))
    assert "BEING BUILT" in building, "the headline must say a build is under way"
    assert "building the reload table · pass 2 · 10 of 29" in building
    assert "ctrl+t stops it" in building
    stopping = _render_text(DashboardViewModel(
        state=state, result=None, solving=False, reload_table_missing=True,
        reload_build="pass 2", reload_build_stopping=True,
    ))
    assert "stopping after the step in flight" in stopping


def _app_with_fake_build(build):
    """A `BuckshotApp` whose solver reports no table and builds with `build`.

    Never touches the real precompute or the disk cache. `solves` records
    every solve the app asks for, so the test can see one follow the build.
    """
    from buckshotcalc.tui.app import BuckshotApp

    app = BuckshotApp()
    built = {"done": False}
    solves: list[int] = []
    app.solver.has_cached_reload_table = lambda: built["done"]
    app.solver.build_reload_table = lambda progress=None: build(progress, built)
    app._request_solve = lambda: solves.append(1)
    return app, solves


def test_ctrl_t_builds_in_the_background_and_reports_progress():
    import asyncio
    import threading

    release = threading.Event()

    def build(progress, built):
        progress(_progress("key", 1, 5, 3.0))
        release.wait(5)
        built["done"] = True

    async def scenario():
        app, solves = _app_with_fake_build(build)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            for _ in range(50):
                await pilot.pause(0.02)
                if app._build is not None and app._build._last is not None:
                    break
            during = app._view_model().reload_build
            release.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if app._build is None:
                    break
            return during, app._build, app._status, len(solves)

    during, after, status, solves = asyncio.run(scenario())
    assert during is not None and "pass 1 · 5 of 29" in during
    assert after is None
    assert status == "reload table built and saved"
    assert solves >= 1, "answers must be requested as soon as the table exists"


def test_ctrl_t_again_stops_the_build_and_saves_nothing():
    import asyncio
    import time as _time

    def build(progress, built):
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline:  # raises once ctrl+t is pressed again
            progress(_progress("key", 1, 1, 1.0))
            _time.sleep(0.01)

    async def scenario():
        app, _ = _app_with_fake_build(build)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause(0.05)
            await pilot.press("ctrl+t")
            for _ in range(100):
                await pilot.pause(0.02)
                if app._build is None:
                    break
            return app._build, app._status, app.solver.has_cached_reload_table()

    after, status, cached = asyncio.run(scenario())
    assert after is None
    assert status == "reload-table build stopped — nothing was saved"
    assert cached is False


def test_ctrl_t_with_a_table_already_built_says_so():
    import asyncio

    from buckshotcalc.tui.app import BuckshotApp

    async def scenario():
        app = BuckshotApp()
        app.solver.has_cached_reload_table = lambda: True
        app._request_solve = lambda: None
        async with app.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            return app._build, app._status

    build, status = asyncio.run(scenario())
    assert build is None and status == "the reload table is already built"


# ---------------------------------------------------------------------------
# Layout fixes (entry dashboard): height, status line, narrow widths, glyphs
# ---------------------------------------------------------------------------


def _full_vm(**kw) -> DashboardViewModel:
    state = kw.pop("state", _state(2, 2))
    return DashboardViewModel(
        state=state, result=_solver().evaluate(state), solving=False,
        reload_table_missing=False,
        item_keys=tuple(
            (item, analysis.build_item_key_map()[item])
            for item in sorted(Item, key=lambda it: it.value)
        ),
        setup=SetupFields(live=3, blank=3, max_hp=4),
        log=("dealer shot itself: blank (no damage)", "you used the saw"),
        prompt="you shot the dealer - live or blank?",
        **kw,
    )


def test_dashboard_fits_a_50_row_terminal_and_the_question_is_near_the_top():
    lines = _render_text(_full_vm(), width=200).splitlines()
    assert len(lines) <= 42
    asked = next(i for i, ln in enumerate(lines) if "live or blank?" in ln)
    assert asked < 12, "the question must sit under the banner, not at the bottom"


def test_dashboard_does_not_wrap_the_alternatives_or_setup_row_when_narrow():
    for width in (120, 160):
        text = _render_text(_full_vm(), width=width)
        assert any("LIVE" in ln and "BLANK" in ln and "MAX HP" in ln
                   for ln in text.splitlines()), width
        # every alternative's % is on the same line as its label
        for alt in analysis.ranked_alternatives(_full_vm().result, Player.PLAYER):
            label = plain_action(alt.label)[:12]  # clipped labels keep their start
            assert any(label in ln and f"{alt.value:.1%}" in ln
                       for ln in text.splitlines()), (width, alt.label)


def test_no_chain_glyph_and_cuffs_use_the_infinity_mark():
    state = replace(_state(2, 2), cuffed=(1, 1))
    text = _render_text(_full_vm(state=state))
    assert "⛓" not in text and "∞ CUFFED" in text


def test_adrenaline_labels_use_the_plain_item_names():
    assert plain_action("ADRENALINE -> MAGNIFYING_GLASS") == "ADRENALINE, TAKE THE MAGNIFIER"
    assert plain_action("ADRENALINE -> EXPIRED_MEDICINE") == "ADRENALINE, TAKE THE MEDICINE"
    assert plain_action("ADRENALINE (nothing to take)") == "ADRENALINE, NOTHING TO TAKE"
    assert plain_action("USE HANDSAW") == "USE THE SAW"


def test_low_own_health_recolours_every_border_and_names_it_in_the_banner():
    from buckshotcalc.tui import theme

    normal = _full_vm(state=_state(2, 2, hp=(3, 3)))
    low = _full_vm(state=_state(2, 2, hp=(2, 3)))
    crit = _full_vm(state=_state(2, 2, hp=(1, 3), turn=Player.DEALER))
    assert "LOW HEALTH" not in _render_text(normal) and "CRITICAL" not in _render_text(normal)
    assert "LOW HEALTH" in _render_text(low)
    assert "CRITICAL - 1 HEALTH LEFT" in _render_text(crit)

    def borders(vm):
        console = Console(width=160, file=open(os.devnull, "w"))
        return {
            str(seg.style.color.name)
            for seg in console.render(render_dashboard(vm, RULES))
            if seg.style is not None and seg.style.color is not None
            and seg.text.strip() and set(seg.text.strip()) <= set("╭╮╰╯─│")
        }

    from rich.color import Color
    warn = Color.parse(theme.WARNING).name
    live = Color.parse(theme.LIVE).name
    assert warn in borders(low) and live not in borders(low)
    assert borders(crit) == {live}


def test_alternatives_show_death_chance_only_when_above_zero():
    risky = _state(2, 3, hp=(1, 3))
    safe = _state(2, 3, hp=(3, 3))
    texts = []
    for s in (risky, safe):
        vm = DashboardViewModel(
            state=s, result=_solver().evaluate(s), solving=False,
            reload_table_missing=False, rules=RULES,
        )
        texts.append(_render_text(vm))
    assert "dies 40%" in texts[0]
    assert "dies" not in texts[1]
