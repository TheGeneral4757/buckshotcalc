"""Tests for buckshotcalc.tui.session (SPEC.md §9, M7 acceptance)."""

from __future__ import annotations

import pytest

from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from buckshotcalc.tui.session import (
    InvariantViolation,
    NoMatchingOutcomeError,
    NoUndoHistoryError,
    Session,
    SessionError,
)

RULES = default_rules()


def _state(
    live: int,
    blank: int,
    hp: tuple[int, int] = (4, 4),
    max_hp: int = 4,
    turn: Player = Player.PLAYER,
    items: tuple[ItemCounts, ItemCounts] | None = None,
    sawed: bool = False,
    cuffed: tuple[bool, bool] = (False, False),
    dealer_knows: frozenset[int] = frozenset(),
) -> GameState:
    chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
    return GameState.create(
        chamber=chamber,
        hp=hp,
        max_hp=max_hp,
        turn=turn,
        items=items,
        sawed=sawed,
        cuffed=cuffed,
        dealer_knows=dealer_knows,
    )


def _session(state: GameState) -> Session:
    return Session(RULES, GameMode.BASE, state)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_restores_exact_prior_state_across_mixed_sequence():
    """A long, mixed event sequence, undone one step at a time, must restore
    each exact prior `GameState` — including across a reload (`new_load`).
    """
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    s = _session(_state(1, 1, items=items))
    snapshots = [s.state]

    s.record_glass(Shell.LIVE)
    snapshots.append(s.state)

    s.record_shot(at_self=False, shell=Shell.LIVE)
    snapshots.append(s.state)

    s.new_load(live=2, blank=1)
    snapshots.append(s.state)

    s.record_shot(at_self=True, shell=Shell.BLANK)
    snapshots.append(s.state)

    s.edit_hp(Player.DEALER, 1)
    snapshots.append(s.state)

    # Undo back through every step, including undo-past-a-reload.
    for expected in reversed(snapshots[:-1]):
        restored = s.undo()
        assert restored == expected

    assert not s.can_undo()
    with pytest.raises(NoUndoHistoryError):
        s.undo()


def test_undo_unbounded_history_depth():
    """History has no cap: 50 events, then 50 undos, exactly restores the start."""
    start = _state(1, 1)
    s = _session(start)
    for _ in range(50):
        s.new_load(live=1, blank=1)
    assert len(s._history) == 50
    for _ in range(50):
        s.undo()
    assert not s.can_undo()
    assert s.state == start


# ---------------------------------------------------------------------------
# dealer_knows collapse (SPEC.md §0.1 (5); docs/dealer-knowledge.md)
# ---------------------------------------------------------------------------


def test_glass_result_does_not_enter_dealer_knows():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    s = _session(_state(1, 1, items=items, turn=Player.PLAYER))
    s.record_glass(Shell.LIVE)
    assert s.state.chamber.slots[0] == Shell.LIVE
    assert s.state.dealer_knows == frozenset()


def test_phone_result_does_not_enter_dealer_knows():
    items = (ItemCounts.of(burner_phone=1), ItemCounts.empty())
    s = _session(_state(1, 2, items=items, turn=Player.PLAYER))
    s.record_phone(1, Shell.BLANK)
    assert s.state.chamber.slots[1] == Shell.BLANK
    assert s.state.dealer_knows == frozenset()


def test_dealer_info_action_enters_dealer_knows_without_resolving_for_us():
    s = _session(
        _state(
            1,
            2,
            turn=Player.DEALER,
            items=(ItemCounts.empty(), ItemCounts.of(magnifying_glass=1)),
        )
    )
    s.record_dealer_info_action(1)
    assert s.state.dealer_knows == frozenset({1})
    # Unresolved for us: the Dealer's own private information.
    assert s.state.chamber.slots[1] is None


def test_dealer_info_action_consumes_the_dealer_item():
    """The Dealer spends the glass it just used.

    Leaving the count untouched keeps crediting the Dealer with an item it
    has already spent, and the Dealer's inventory is a real input to its
    policy: a swept sample of 120 random positions found the count changing
    `P(win)` in 52 of them, by as much as 0.33.
    """
    s = _session(
        _state(
            2,
            1,
            turn=Player.DEALER,
            items=(ItemCounts.empty(), ItemCounts.of(magnifying_glass=2)),
        )
    )
    s.record_dealer_info_action(0)
    assert s.state.items_of(Player.DEALER)[Item.MAGNIFYING_GLASS] == 1
    assert s.state.items_used_this_turn == 1
    # And it is refused once the Dealer has none left.
    s.record_dealer_info_action(0)
    assert s.state.items_of(Player.DEALER)[Item.MAGNIFYING_GLASS] == 0
    with pytest.raises(SessionError):
        s.record_dealer_info_action(0)


def test_dealer_info_action_never_resolves_a_slot_for_us():
    """The knowledge-leak regression guard (SPEC.md §0.1 (5)).

    Routing a Dealer Glass through `record_glass` would resolve slot 0 in
    *our* chamber from information we never saw, and would leave
    `dealer_knows` empty — both wrong, and in opposite directions.
    """
    st = _state(
        2,
        1,
        turn=Player.DEALER,
        items=(ItemCounts.empty(), ItemCounts.of(magnifying_glass=1)),
    )
    leaked = _session(st)
    leaked.record_glass(Shell.LIVE)
    assert leaked.state.chamber.slots[0] is not None  # the defect, for contrast
    assert leaked.state.dealer_knows == frozenset()

    correct = _session(st)
    correct.record_dealer_info_action(0)
    assert correct.state.chamber.slots == st.chamber.slots  # nothing learned
    assert correct.state.dealer_knows == frozenset({0})


def test_shot_fired_by_dealer_pops_slot_and_renumbers_dealer_knows():
    """A shell the Dealer fired resolves+pops slot 0 (public knowledge) via
    the ordinary `apply()` transition, and any `dealer_knows` indices above
    it renumber down — exercised here starting from a state where the
    Dealer already privately knows a later slot.
    """
    s = _session(_state(1, 2, turn=Player.DEALER, dealer_knows=frozenset({2})))
    s.record_shot(at_self=True, shell=Shell.BLANK)
    # Slot 2 shifts down to slot 1 after the pop.
    assert s.state.dealer_knows == frozenset({1})


# ---------------------------------------------------------------------------
# Desync detection (NoMatchingOutcomeError)
# ---------------------------------------------------------------------------


def test_recording_a_shell_the_chamber_rules_out_raises():
    s = _session(_state(0, 1, turn=Player.PLAYER))  # guaranteed blank
    with pytest.raises(NoMatchingOutcomeError):
        s.record_shot(at_self=True, shell=Shell.LIVE)
    # State must be untouched by the failed attempt.
    assert s.state.chamber.slots == (Shell.BLANK,)
    assert not s.can_undo()


def test_glass_result_contradicting_known_slot_raises():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    state = _state(1, 0, items=items)  # slot 0 already deduced LIVE
    s = _session(state)
    assert s.state.chamber.slots[0] == Shell.LIVE
    with pytest.raises(NoMatchingOutcomeError):
        s.record_glass(Shell.BLANK)


# ---------------------------------------------------------------------------
# Edits: every field type, and invariant-violating edits raise
# ---------------------------------------------------------------------------


def test_edit_hp():
    s = _session(_state(1, 1))
    s.edit_hp(Player.PLAYER, 1)
    assert s.state.hp[Player.PLAYER] == 1
    s.undo()
    assert s.state.hp[Player.PLAYER] == 4


def test_edit_max_hp():
    s = _session(_state(1, 1, max_hp=4))
    s.edit_max_hp(6)
    assert s.state.max_hp == 6


def test_edit_item_count():
    s = _session(_state(1, 1))
    s.edit_item_count(Player.DEALER, Item.BEER, 3)
    assert s.state.items_of(Player.DEALER)[Item.BEER] == 3


def test_edit_sawed_and_cuffed():
    s = _session(_state(1, 1))
    s.edit_sawed(True)
    assert s.state.sawed is True
    s.edit_cuffed(Player.DEALER, True)
    assert s.state.cuffed[Player.DEALER] is True


def test_edit_turn():
    s = _session(_state(1, 1, turn=Player.PLAYER))
    s.edit_turn(Player.DEALER)
    assert s.state.turn == Player.DEALER


def test_edit_dealer_knows():
    s = _session(_state(1, 2))
    s.edit_dealer_knows(frozenset({0, 2}))
    assert s.state.dealer_knows == frozenset({0, 2})


def test_edit_chamber_composition():
    s = _session(_state(1, 1))
    s.edit_chamber((Shell.LIVE, None, None), unknown_live=0, unknown_blank=2)
    assert s.state.chamber.slots == (Shell.LIVE, Shell.BLANK, Shell.BLANK)  # deduced


def test_edit_chamber_invariant_violation_raises_and_does_not_corrupt_state():
    s = _session(_state(1, 1))
    before = s.state
    with pytest.raises(InvariantViolation):
        # slots.count(None) == 1, but pool == 3: invariant violated.
        s.edit_chamber((Shell.LIVE, None), unknown_live=2, unknown_blank=1)
    assert s.state == before
    assert not s.can_undo()  # the failed edit must not have pushed history


def test_edit_negative_item_count_raises_invariant_violation():
    s = _session(_state(1, 1))
    before = s.state
    with pytest.raises(InvariantViolation):
        s.edit_item_count(Player.PLAYER, Item.BEER, -1)
    assert s.state == before


# ---------------------------------------------------------------------------
# New load / new round
# ---------------------------------------------------------------------------


def test_new_load_carries_health_resets_chamber_and_items():
    items = (ItemCounts.of(beer=2), ItemCounts.empty())
    s = _session(_state(1, 1, hp=(2, 3), max_hp=4, items=items, sawed=True))
    s.new_load(live=3, blank=2)
    assert s.state.hp == (2, 3)
    assert s.state.max_hp == 4
    assert len(s.state.chamber.slots) == 5
    assert s.state.chamber.unknown_live == 3
    assert s.state.sawed is False
    assert s.state.items[0].total() == 0


def test_new_round_resets_health_to_max_hp():
    s = _session(_state(1, 1, hp=(1, 4), max_hp=4))
    s.new_round(live=2, blank=2, max_hp=3)
    assert s.state.hp == (3, 3)
    assert s.state.max_hp == 3


# ---------------------------------------------------------------------------
# Adrenaline steal
# ---------------------------------------------------------------------------


def test_steal_deterministic_item():
    items = (ItemCounts.of(adrenaline=1), ItemCounts.of(handsaw=1))
    s = _session(_state(1, 1, items=items, turn=Player.PLAYER))
    s.steal(Item.HANDSAW)
    assert s.state.sawed is True
    assert s.state.items_of(Player.DEALER)[Item.HANDSAW] == 0


def test_waste_adrenaline_keeps_turn_and_spends_it():
    items = (ItemCounts.of(adrenaline=1), ItemCounts.empty())
    s = _session(_state(1, 1, items=items, turn=Player.PLAYER))
    s.waste_adrenaline()
    assert s.state.items_of(Player.PLAYER)[Item.ADRENALINE] == 0
    assert s.state.turn is Player.PLAYER


def test_steal_beer_needs_shell():
    items = (ItemCounts.of(adrenaline=1), ItemCounts.of(beer=1))
    s = _session(_state(1, 1, items=items, turn=Player.PLAYER))
    s.steal(Item.BEER, shell=Shell.LIVE)
    # Slot 0 (LIVE) popped and the transferred Beer is spent; one BLANK left,
    # deduced closed since the live pool is now empty.
    assert s.state.chamber.slots == (Shell.BLANK,)
    assert s.state.items_of(Player.DEALER)[Item.BEER] == 0


# ---------------------------------------------------------------------------
# Event log (operator design decision: plain-language narration)
# ---------------------------------------------------------------------------


def test_log_records_plain_language_events():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    s = _session(_state(1, 1, items=items))
    s.record_glass(Shell.LIVE)
    assert s.log[-1] == "you used GLASS → LIVE"


def test_shot_log_lines_are_plain_english():
    s = _session(_state(2, 2, turn=Player.DEALER))
    s.record_shot(at_self=True, shell=Shell.BLANK)
    assert s.log[-1] == "dealer shot itself: blank (no damage)"
    s = _session(_state(2, 2, turn=Player.PLAYER))
    s.record_shot(at_self=False, shell=Shell.LIVE)
    assert s.log[-1] == "you shot the dealer: live (dealer 4→3)"
    s = _session(_state(2, 2, turn=Player.DEALER))
    s.record_shot(at_self=False, shell=Shell.LIVE)
    assert s.log[-1] == "dealer shot you: live (you 4→3)"


def test_undo_appends_a_reversal_narration_not_just_pops_log():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    s = _session(_state(1, 1, items=items))
    s.record_glass(Shell.LIVE)
    before_len = len(s.log)
    s.undo()
    assert len(s.log) == before_len + 1
    assert "reverted" in s.log[-1]
    assert "GLASS" in s.log[-1]


def test_recent_log_returns_last_n_entries():
    s = _session(_state(1, 1))
    for _ in range(8):
        s.new_load(live=1, blank=1)
    recent = s.recent_log(5)
    assert len(recent) == 5
    assert recent == tuple(s.log[-5:])


# ---------------------------------------------------------------------------
# SPEC.md §5.2 / §15.16(b) — the Inverter on an unresolved shell flips unseen:
# the engine tracks it exactly (`ChamberState.inverted0`), nothing degrades.
# ---------------------------------------------------------------------------


def test_inverter_on_unresolved_shell_is_tracked_as_flipped_not_degraded():
    session = _session(_state(2, 2, items=(ItemCounts.of(inverter=1), ItemCounts.empty())))
    before = session.state.chamber

    session.record_inverter()

    chamber = session.state.chamber
    assert session.degraded is None
    assert chamber.inverted0 and chamber.slots == before.slots
    assert session.state.items_of(Player.PLAYER)[Item.INVERTER] == 0, "the item is spent"
    assert "unseen" in session.log[-1]
    session.undo()
    assert session.state.chamber == before


def test_inverter_on_resolved_shell_is_an_exact_flip_and_does_not_degrade():
    """A resolved slot 0 is deterministic — nothing is unobserved, so nothing degrades."""
    session = _session(
        _state(2, 2, items=(ItemCounts.of(inverter=1, magnifying_glass=1), ItemCounts.empty()))
    )
    session.record_glass(Shell.LIVE)
    assert session.state.chamber.slots[0] is Shell.LIVE

    session.record_inverter()

    assert session.degraded is None
    assert session.state.chamber.slots[0] is Shell.BLANK


def test_stolen_inverter_on_unresolved_shell_is_tracked_the_same_way():
    session = _session(
        _state(
            2,
            2,
            items=(ItemCounts.of(adrenaline=1), ItemCounts.of(inverter=1)),
        )
    )
    session.steal(Item.INVERTER)
    assert session.degraded is None and session.state.chamber.inverted0


# ---------------------------------------------------------------------------
# SPEC.md §15.16(a) — the Dealer's Burner Phone target is not observable, so
# the operator is no longer asked to name it.
# ---------------------------------------------------------------------------


def test_dealer_phone_slot_comes_from_the_engine_not_the_operator():
    """The live session and the search must assume the same slot.

    `dealer_phone_target` is the one rule; before this, the TUI took the
    operator's guess while the solver assumed index 1 for every future phone
    use it searched — so a session could contradict its own recommendation.
    """
    from buckshotcalc.engine.dealer import dealer_phone_target

    state = _state(
        2, 2, turn=Player.DEALER, items=(ItemCounts.empty(), ItemCounts.of(burner_phone=1))
    )
    session = _session(state)

    session.record_dealer_phone()

    expected = dealer_phone_target(state, RULES)
    assert session.state.dealer_knows == frozenset({expected})


def test_dealer_phone_does_not_resolve_our_chamber():
    """A Dealer information action never tells *us* a shell (docs/dealer-knowledge.md)."""
    state = _state(
        2, 2, turn=Player.DEALER, items=(ItemCounts.empty(), ItemCounts.of(burner_phone=1))
    )
    session = _session(state)
    before = session.state.chamber

    session.record_dealer_phone()

    assert session.state.chamber == before
    assert session.state.items_of(Player.DEALER)[Item.BURNER_PHONE] == 0
    assert session.degraded is None, "our belief about the shells is untouched"


def test_dealer_phone_log_line_says_the_slot_was_assumed():
    """The Glass's target is observable; the Phone's is not. The log must not blur them."""
    session = _session(
        _state(
            2, 2, turn=Player.DEALER,
            items=(ItemCounts.empty(), ItemCounts.of(burner_phone=1, magnifying_glass=1)),
        )
    )
    session.record_dealer_phone()
    assert "assumed" in session.log[-1]

    session.record_dealer_info_action(0, Item.MAGNIFYING_GLASS)
    assert "assumed" not in session.log[-1]


# ---------------------------------------------------------------------------
# Unseen Inverter: the observed shell is the FLIPPED one, so the pool loses
# the opposite (original) shell. Saw carry across a last-shell Beer (§15.18).
# ---------------------------------------------------------------------------

_INV = lambda: (ItemCounts.of(inverter=2, magnifying_glass=1, beer=1), ItemCounts.empty())  # noqa: E731


@pytest.mark.parametrize("seen", [Shell.BLANK, Shell.LIVE])
def test_shot_after_unseen_inverter_removes_the_opposite_shell(seen):
    s = _session(_state(2, 2, items=_INV()))
    s.record_inverter()
    s.record_shot(at_self=True, shell=seen)
    c = s.state.chamber
    assert s.degraded is None
    # the original shell was the opposite of `seen`: that one leaves the pool
    assert (c.total_live(), c.total_blank()) == ((1, 2) if seen is Shell.BLANK else (2, 1))


def test_glass_after_unseen_inverter_sets_slot_and_removes_opposite():
    s = _session(_state(2, 2, items=_INV()))
    s.record_inverter()
    s.record_glass(Shell.LIVE)
    c = s.state.chamber
    assert s.degraded is None and c.slots[0] is Shell.LIVE
    # original blank flipped to live: the chamber now holds 3 live / 1 blank
    assert (c.total_live(), c.total_blank()) == (3, 1)
    s.undo()
    assert s.state.chamber.inverted0 and s.state.chamber.slots[0] is None


def test_beer_after_unseen_inverter_removes_opposite_shell():
    s = _session(_state(2, 2, items=_INV()))
    s.record_inverter()
    s.record_beer(Shell.BLANK)
    c = s.state.chamber
    assert (c.total_live(), c.total_blank()) == (1, 2)


def test_two_inverters_cancel_for_the_observation():
    s = _session(_state(2, 2, items=_INV()))
    s.record_inverter()
    s.record_inverter()
    s.record_shot(at_self=True, shell=Shell.BLANK)
    c = s.state.chamber
    assert (c.total_live(), c.total_blank()) == (2, 1) and s.degraded is None
    s.undo()
    s.undo()  # back to one flip
    s.record_shot(at_self=True, shell=Shell.BLANK)
    assert (s.state.chamber.total_live(), s.state.chamber.total_blank()) == (1, 2)


def test_dealer_inverter_collapses_the_same_way():
    items = (ItemCounts.empty(), ItemCounts.of(inverter=1))
    s = _session(_state(2, 2, turn=Player.DEALER, items=items))
    s.record_inverter()
    s.record_shot(at_self=True, shell=Shell.LIVE)
    assert (s.state.chamber.total_live(), s.state.chamber.total_blank()) == (2, 1)


def test_inverter_on_known_slot_flips_without_degrading_then_shot_is_exact():
    s = _session(_state(2, 2, items=_INV()))
    s.record_glass(Shell.LIVE)
    s.record_inverter()
    s.record_shot(at_self=True, shell=Shell.BLANK)
    assert s.degraded is None
    assert (s.state.chamber.total_live(), s.state.chamber.total_blank()) == (1, 2)


def test_beer_on_last_shell_while_sawed_carries_the_saw_into_the_reload():
    s = _session(_state(1, 0, items=_INV(), sawed=True))
    s.record_beer(Shell.LIVE)
    assert s.state.chamber.is_empty and s.saw_carry
    s.new_load(2, 2)
    assert s.state.sawed
    s.record_shot(at_self=False, shell=Shell.LIVE)
    assert s.state.hp[Player.DEALER] == 2, "sawed shot deals 2"
    assert not s.state.sawed


def test_saw_does_not_carry_after_a_shot_or_unsawed_beer_or_other_event():
    s = _session(_state(1, 0, sawed=True))
    s.record_shot(at_self=False, shell=Shell.LIVE)
    s.new_load(1, 1)
    assert not s.state.sawed
    t = _session(_state(1, 0, items=_INV()))
    t.record_beer(Shell.LIVE)
    t.new_load(1, 1)
    assert not t.state.sawed
    u = _session(_state(1, 0, items=_INV(), sawed=True))
    u.record_beer(Shell.LIVE)
    u.edit_hp(Player.PLAYER, 3)
    u.new_load(1, 1)
    assert not u.state.sawed


def test_undo_restores_saw_carry():
    s = _session(_state(1, 0, items=_INV(), sawed=True))
    s.record_beer(Shell.LIVE)
    s.undo()
    assert not s.saw_carry
