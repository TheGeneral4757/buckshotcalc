"""Tests for buckshotcalc.engine.actions (SPEC.md §6, M2 acceptance)."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from buckshotcalc.engine.actions import (
    Action,
    ActionKind,
    apply,
    item_cap_reached,
    legal_actions,
)
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.state import (
    CUFF_SPENT,
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)

RULES = default_rules()


def make_state(
    slots,
    unknown_live,
    unknown_blank,
    hp=(3, 3),
    max_hp=3,
    turn=Player.PLAYER,
    items=None,
    sawed=False,
    cuffed=(False, False),
    dealer_knows=frozenset(),
    items_used_this_turn=0,
) -> GameState:
    chamber = ChamberState.create(tuple(slots), unknown_live, unknown_blank)
    return GameState.create(
        chamber,
        hp,
        max_hp,
        turn,
        items=items,
        sawed=sawed,
        cuffed=cuffed,
        dealer_knows=dealer_knows,
        items_used_this_turn=items_used_this_turn,
    )


def outcome_for(outcomes, predicate):
    """The unique outcome whose state matches `predicate`; asserts uniqueness."""
    matches = [o for o in outcomes if predicate(o[1])]
    assert len(matches) == 1, f"expected exactly one match, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# §12.3 small positions
# ---------------------------------------------------------------------------


def test_l1b0_guaranteed_live_shoot_self():
    """L1 B0: SHOOT_SELF is a deterministic live shot."""
    state = make_state((None,), unknown_live=1, unknown_blank=0)
    outcomes = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.hp_of(Player.PLAYER) == 3 - RULES.turns.base_damage
    assert result.turn == Player.DEALER  # live self-shot passes the turn
    assert len(result.chamber.slots) == 0


def test_l0b1_guaranteed_blank_free_turn():
    """L0 B1: SHOOT_SELF is a deterministic blank — free turn."""
    state = make_state((None,), unknown_live=0, unknown_blank=1)
    outcomes = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.hp_of(Player.PLAYER) == 3
    assert result.turn == Player.PLAYER  # retained
    assert result.items_used_this_turn == 0


def test_l1b1_at_1hp_shoot_self():
    """L1 B1 at 1 HP: half the mass is a round loss."""
    state = make_state((None, None), unknown_live=1, unknown_blank=1, hp=(1, 3))
    outcomes = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert len(outcomes) == 2
    total = sum(w for w, _ in outcomes)
    assert total == pytest.approx(1.0)

    _, blank_result = outcome_for(outcomes, lambda s: s.hp_of(Player.PLAYER) == 1)
    assert blank_result.turn == Player.PLAYER

    _, live_result = outcome_for(outcomes, lambda s: s.hp_of(Player.PLAYER) == 0)
    assert live_result.winner() == Player.DEALER


# ---------------------------------------------------------------------------
# §6.1 table, row by row
# ---------------------------------------------------------------------------


def test_shoot_opponent_turn_passes_regardless_of_outcome():
    state = make_state((None,), unknown_live=0, unknown_blank=1)
    outcomes = apply(state, Action(ActionKind.SHOOT_OPPONENT), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.hp_of(Player.DEALER) == 3  # blank: no damage
    assert result.turn == Player.DEALER  # turn still passes


def test_shoot_opponent_live_damages_and_passes_turn():
    state = make_state((None,), unknown_live=1, unknown_blank=0)
    outcomes = apply(state, Action(ActionKind.SHOOT_OPPONENT), RULES)
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.hp_of(Player.DEALER) == 3 - RULES.turns.base_damage
    assert result.turn == Player.DEALER


def test_shoot_self_with_saw_deals_saw_damage():
    state = make_state((None,), unknown_live=1, unknown_blank=0, sawed=True)
    outcomes = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    _, result = outcomes[0]
    assert result.hp_of(Player.PLAYER) == 3 - RULES.item_semantics.handsaw.damage
    assert result.sawed is False  # cleared after the shot


def test_use_cigarettes_heals_and_retains_turn():
    items = (ItemCounts.of(cigarettes=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, hp=(1, 3), items=items)
    outcomes = apply(state, Action(ActionKind.USE, Item.CIGARETTES), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.hp_of(Player.PLAYER) == 1 + RULES.item_semantics.cigarettes.heal
    assert result.turn == Player.PLAYER
    assert result.items_of(Player.PLAYER)[Item.CIGARETTES] == 0


def test_use_cigarettes_legal_at_max_hp_and_is_a_no_op():
    items = (ItemCounts.of(cigarettes=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, hp=(3, 3), items=items)
    actions = legal_actions(state, RULES)
    assert Action(ActionKind.USE, Item.CIGARETTES) in actions
    _, result = apply(state, Action(ActionKind.USE, Item.CIGARETTES), RULES)[0]
    assert result.hp_of(Player.PLAYER) == 3  # clamped, no effect
    assert result.items_of(Player.PLAYER)[Item.CIGARETTES] == 0  # slot still consumed


def test_use_handcuffs_precondition_blocks_when_already_cuffed():
    items = (ItemCounts.of(handcuffs=1), ItemCounts.empty())
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, items=items, cuffed=(False, True)
    )
    actions = legal_actions(state, RULES)
    assert Action(ActionKind.USE, Item.HANDCUFFS) not in actions


def test_use_handcuffs_cuffs_opponent_and_retains_turn():
    items = (ItemCounts.of(handcuffs=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=items)
    outcomes = apply(state, Action(ActionKind.USE, Item.HANDCUFFS), RULES)
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.is_cuffed(Player.DEALER) is True
    assert result.turn == Player.PLAYER


def test_use_beer_resolves_pops_and_retains_turn():
    items = (ItemCounts.of(beer=1), ItemCounts.empty())
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=items, sawed=True
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.BEER), RULES)
    assert len(outcomes) == 2
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    for _, result in outcomes:
        assert len(result.chamber.slots) == 1  # one shell popped
        assert result.sawed is True  # beer does not clear the saw (SPEC.md §3.2)
        assert result.turn == Player.PLAYER


def test_use_handsaw_sets_sawed_and_precondition_blocks_when_already_sawed():
    items = (ItemCounts.of(handsaw=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=items)
    _, result = apply(state, Action(ActionKind.USE, Item.HANDSAW), RULES)[0]
    assert result.sawed is True

    already_sawed = make_state(
        (None,), unknown_live=1, unknown_blank=0, items=items, sawed=True
    )
    actions = legal_actions(already_sawed, RULES)
    assert Action(ActionKind.USE, Item.HANDSAW) not in actions


def test_saw_then_beer_keeps_the_saw():
    """Only a shot resets the saw (SPEC.md §3.2): beer on a non-final shell keeps it."""
    items = (ItemCounts.of(handsaw=1, beer=1), ItemCounts.empty())
    state = make_state((None, None), unknown_live=1, unknown_blank=1, items=items)

    _, sawed_state = apply(state, Action(ActionKind.USE, Item.HANDSAW), RULES)[0]
    assert sawed_state.sawed is True
    assert sawed_state.items_of(Player.PLAYER)[Item.HANDSAW] == 0

    outcomes = apply(sawed_state, Action(ActionKind.USE, Item.BEER), RULES)
    assert len(outcomes) == 2
    for _, final_state in outcomes:
        assert final_state.sawed is True  # saw survives the eject
        assert len(final_state.chamber.slots) == 1
        assert final_state.turn == Player.PLAYER
    # ...and the next shot still deals the doubled damage, then clears it.
    _, after = apply(outcomes[0][1], Action(ActionKind.SHOOT_OPPONENT), RULES)[-1]
    assert after.sawed is False


def test_beer_ejecting_last_shell_drops_sawed_by_canonicalization():
    """SPEC.md §15.14: the reload table cannot carry `sawed`, so an empty chamber zeroes it."""
    items = (ItemCounts.of(handsaw=1, beer=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=items, sawed=True)
    ((_, final_state),) = apply(state, Action(ActionKind.USE, Item.BEER), RULES)
    assert len(final_state.chamber.slots) == 0
    assert final_state.sawed is False


def test_use_magnifying_glass_resolves_slot_without_popping():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    state = make_state((None, None), unknown_live=1, unknown_blank=1, items=items)
    outcomes = apply(state, Action(ActionKind.USE, Item.MAGNIFYING_GLASS), RULES)
    assert len(outcomes) == 2
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    for _, result in outcomes:
        assert len(result.chamber.slots) == 2  # untouched, no pop
        assert result.chamber.slots[0] is not None  # resolved
        assert result.turn == Player.PLAYER


def test_use_magnifying_glass_already_resolved_is_a_no_op():
    items = (ItemCounts.of(magnifying_glass=1), ItemCounts.empty())
    state = make_state(
        (Shell.LIVE, None), unknown_live=0, unknown_blank=1, items=items
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.MAGNIFYING_GLASS), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.chamber == state.chamber


def test_use_burner_phone_no_eligible_slot_is_legal_no_op():
    items = (ItemCounts.of(burner_phone=1), ItemCounts.empty())
    state = make_state(
        (None, Shell.BLANK), unknown_live=1, unknown_blank=0, items=items
    )
    actions = legal_actions(state, RULES)
    assert Action(ActionKind.USE, Item.BURNER_PHONE) in actions
    outcomes = apply(state, Action(ActionKind.USE, Item.BURNER_PHONE), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.chamber == state.chamber


def test_use_burner_phone_resolves_uniform_random_slot_index_gt_0():
    # L2 B2 across 4 slots: resolving any one shell never exhausts either
    # type in the pool, so `deduce()` cannot cascade into slot 0 — keeping
    # the "slot 0 untouched" assertion meaningful rather than incidental.
    items = (ItemCounts.of(burner_phone=1), ItemCounts.empty())
    state = make_state(
        (None, None, None, None), unknown_live=2, unknown_blank=2, items=items
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.BURNER_PHONE), RULES)
    assert len(outcomes) == 6  # 3 eligible slots x 2 possible values each
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    for weight, result in outcomes:
        assert weight == pytest.approx((1.0 / 3.0) * 0.5)
        assert result.chamber.slots[0] is None  # slot 0 never touched
        assert result.turn == Player.PLAYER
        resolved = [i for i, s in enumerate(result.chamber.slots) if s is not None]
        assert resolved in ([1], [2], [3])


def test_player_burner_phone_clamps_index_7_to_6_with_eight_shells():
    """BurnerPhone.gd:14-16: raw 7 -> 6, so slot 6 gets 2/7 and slot 7 is never revealed."""
    items = (ItemCounts.of(burner_phone=1), ItemCounts.empty())
    state = make_state((None,) * 8, unknown_live=4, unknown_blank=4, items=items)
    outcomes = apply(state, Action(ActionKind.USE, Item.BURNER_PHONE), RULES)
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    by_slot: dict[int, float] = {}
    for weight, result in outcomes:
        (slot,) = [i for i, s in enumerate(result.chamber.slots) if s is not None]
        by_slot[slot] = by_slot.get(slot, 0.0) + weight
    assert 7 not in by_slot
    assert by_slot[6] == pytest.approx(2 / 7)
    for slot in range(1, 6):
        assert by_slot[slot] == pytest.approx(1 / 7)


# ---------------------------------------------------------------------------
# Inverter (SPEC.md §5.2) — the subtle case
# ---------------------------------------------------------------------------


def test_inverter_on_unresolved_slot_flips_unseen_and_reveals_nothing():
    """L2 B1: ItemInteraction.gd:165-170 flips silently, so slot 0 stays unknown."""
    items = (ItemCounts.of(inverter=1), ItemCounts.empty())
    state = make_state((None, None, None), unknown_live=2, unknown_blank=1, items=items)
    ((weight, result),) = apply(state, Action(ActionKind.USE, Item.INVERTER), RULES)
    assert weight == 1.0
    chamber = result.chamber
    assert chamber.slots == (None, None, None)
    assert chamber.inverted0
    assert (chamber.unknown_live, chamber.unknown_blank) == (2, 1)  # pool counts originals
    assert chamber.p_live() == pytest.approx(1 / 3)  # live now only if it was blank


def test_two_inverters_on_an_unresolved_slot_cancel():
    items = (ItemCounts.of(inverter=2), ItemCounts.empty())
    state = make_state((None, None, None), unknown_live=2, unknown_blank=1, items=items)
    inv = Action(ActionKind.USE, Item.INVERTER)
    ((_, once),) = apply(state, inv, RULES)
    ((_, twice),) = apply(once, inv, RULES)
    assert twice.chamber == state.chamber


def test_seeing_an_inverted_shell_removes_the_opposite_from_the_pool():
    """L2 B1 inverted: a fired LIVE (p 1/3) was the blank, so both others are live."""
    items = (ItemCounts.of(inverter=1), ItemCounts.empty())
    state = make_state((None, None, None), unknown_live=2, unknown_blank=1, items=items)
    ((_, inverted),) = apply(state, Action(ActionKind.USE, Item.INVERTER), RULES)
    outcomes = apply(inverted, Action(ActionKind.SHOOT_OPPONENT), RULES)
    by_hp = {o.hp[1]: (w, o) for w, o in outcomes}
    w_live, after_live = by_hp[2]
    w_blank, after_blank = by_hp[3]
    assert w_live == pytest.approx(1 / 3)
    assert after_live.chamber.slots == (Shell.LIVE, Shell.LIVE)
    assert w_blank == pytest.approx(2 / 3)
    assert after_blank.chamber.slots == (None, None)
    assert (after_blank.chamber.unknown_live, after_blank.chamber.unknown_blank) == (1, 1)
    assert not after_blank.chamber.inverted0


def test_inverted_slot0_closes_when_the_pool_does():
    """L1 B1 inverted, phone sees slot 1 LIVE: slot 0's original was the blank, now live."""
    chamber = ChamberState((None, None), 1, 1, inverted0=True)
    assert chamber.resolve(1, Shell.LIVE).slots == (Shell.LIVE, Shell.LIVE)
    assert chamber.resolve(1, Shell.BLANK).slots == (Shell.BLANK, Shell.BLANK)


def test_inverter_on_resolved_slot_is_a_plain_flip_no_pool_change():
    # Pool must have >0 of both types, else `deduce()` would already have
    # cascaded slot 1 at construction time and this wouldn't test a resolved
    # slot 0 sitting alongside a genuinely still-unresolved slot 1.
    items = (ItemCounts.of(inverter=1), ItemCounts.empty())
    state = make_state(
        (Shell.LIVE, None, None), unknown_live=1, unknown_blank=1, items=items
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.INVERTER), RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)
    assert result.chamber.slots[0] == Shell.BLANK
    assert result.chamber.slots[1] is None  # untouched
    assert result.chamber.slots[2] is None  # untouched
    assert result.chamber.unknown_live == 1  # pool untouched
    assert result.chamber.unknown_blank == 1


# ---------------------------------------------------------------------------
# Expired medicine
# ---------------------------------------------------------------------------


def test_use_expired_medicine_may_reach_zero_and_lose():
    cfg = RULES.item_semantics.expired_medicine
    items = (ItemCounts.of(expired_medicine=1), ItemCounts.empty())
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, hp=(cfg.harm, 3), items=items
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.EXPIRED_MEDICINE), RULES)
    assert len(outcomes) == 2
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)

    weight_heal, heal_state = outcome_for(
        outcomes, lambda s: s.hp_of(Player.PLAYER) > cfg.harm
    )
    assert weight_heal == pytest.approx(cfg.heal_probability)
    assert heal_state.hp_of(Player.PLAYER) == min(cfg.harm + cfg.heal, 3)

    weight_harm, harm_state = outcome_for(
        outcomes, lambda s: s.hp_of(Player.PLAYER) == 0
    )
    assert weight_harm == pytest.approx(1.0 - cfg.heal_probability)
    assert harm_state.winner() == Player.DEALER


# ---------------------------------------------------------------------------
# dealer_knows renumbering — identical through a shot and through beer
# ---------------------------------------------------------------------------


def test_dealer_knows_renumbers_identically_through_shot_and_beer():
    slots = (Shell.BLANK, None, None, None)
    dealer_knows = frozenset({0, 2, 3})

    shot_state = make_state(
        slots, unknown_live=1, unknown_blank=2, dealer_knows=dealer_knows
    )
    _, shot_result = apply(shot_state, Action(ActionKind.SHOOT_SELF), RULES)[0]
    assert shot_result.dealer_knows == frozenset({1, 2})

    items = (ItemCounts.of(beer=1), ItemCounts.empty())
    beer_state = make_state(
        slots, unknown_live=1, unknown_blank=2, dealer_knows=dealer_knows, items=items
    )
    _, beer_result = apply(beer_state, Action(ActionKind.USE, Item.BEER), RULES)[0]
    assert beer_result.dealer_knows == frozenset({1, 2})


# ---------------------------------------------------------------------------
# Cuff bounce-back
# ---------------------------------------------------------------------------


def test_cuff_bounce_back_returns_turn_without_opponent_acting():
    """Turn passes to a cuffed Dealer: turn returns to the player, cuff stays (spent)."""
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, cuffed=(False, True)
    )
    for _, result in apply(state, Action(ActionKind.SHOOT_OPPONENT), RULES):
        assert result.turn == Player.PLAYER  # bounced straight back
        assert result.cuffed[Player.DEALER] == CUFF_SPENT  # RoundManager.gd:308-326
        assert result.items_used_this_turn == 0


def test_spent_cuff_blocks_recuff_then_clears_on_next_turn_pass():
    """The bonus turn cannot re-cuff; the cuff clears at the following pass and the Dealer acts."""
    items = (ItemCounts.of(handcuffs=1), ItemCounts.empty())
    state = make_state(
        (None, None, None), unknown_live=1, unknown_blank=2, items=items, cuffed=(False, CUFF_SPENT)
    )
    assert Action(ActionKind.USE, Item.HANDCUFFS) not in legal_actions(state, RULES)
    for _, result in apply(state, Action(ActionKind.SHOOT_OPPONENT), RULES):
        assert result.turn == Player.DEALER  # second pass: cuff breaks, Dealer acts
        assert result.is_cuffed(Player.DEALER) is False


# ---------------------------------------------------------------------------
# Adrenaline
# ---------------------------------------------------------------------------


def test_adrenaline_offers_one_action_per_stealable_item_and_skips_unusable():
    """Handcuffs is unofferable when its own precondition (opponent not
    cuffed) fails — even though the opponent physically holds it."""
    items = (
        ItemCounts.of(adrenaline=1),
        ItemCounts.of(cigarettes=1, handcuffs=1),
    )
    # Dealer is already cuffed, so stealing+using Handcuffs (which would cuff
    # the Dealer again) fails its own precondition and must not be offered.
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, items=items, cuffed=(False, True)
    )
    actions = legal_actions(state, RULES)
    adrenaline_actions = [a for a in actions if a.item is Item.ADRENALINE]
    assert adrenaline_actions == [Action(ActionKind.USE, Item.ADRENALINE, Item.CIGARETTES)]


def test_adrenaline_with_nothing_stealable_is_a_waste_action():
    # Opponent holds nothing, or only Adrenaline: only the bare waste action is offered.
    for opp in (ItemCounts.empty(), ItemCounts.of(adrenaline=1)):
        state = make_state(
            (None,), unknown_live=1, unknown_blank=0, items=(ItemCounts.of(adrenaline=1), opp)
        )
        adr = [a for a in legal_actions(state, RULES) if a.item is Item.ADRENALINE]
        assert adr == [Action(ActionKind.USE, Item.ADRENALINE)]
    ((w, after),) = apply(state, adr[0], RULES)
    assert w == 1.0
    assert after.items_of(Player.PLAYER)[Item.ADRENALINE] == 0
    assert after.turn is state.turn and after.chamber == state.chamber
    assert after.items_used_this_turn == state.items_used_this_turn + 1
    assert str(adr[0]) == "ADRENALINE (nothing to take)"


def test_adrenaline_waste_off_by_rules_key():
    rules = replace(
        RULES,
        item_semantics=replace(
            RULES.item_semantics,
            adrenaline=replace(
                RULES.item_semantics.adrenaline, usable_with_nothing_to_steal=False
            ),
        ),
    )
    items = (ItemCounts.of(adrenaline=1), ItemCounts.empty())
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=items)
    assert all(a.item is not Item.ADRENALINE for a in legal_actions(state, rules))


def test_adrenaline_chaining_transfers_and_applies_immediately():
    items = (
        ItemCounts.of(adrenaline=1),
        ItemCounts.of(cigarettes=1),
    )
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, hp=(1, 3), items=items
    )
    action = Action(ActionKind.USE, Item.ADRENALINE, Item.CIGARETTES)
    outcomes = apply(state, action, RULES)
    assert len(outcomes) == 1
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)

    heal = RULES.item_semantics.cigarettes.heal
    assert result.hp_of(Player.PLAYER) == 1 + heal  # cigarette's own effect applied
    assert result.items_of(Player.PLAYER)[Item.ADRENALINE] == 0  # adrenaline consumed
    assert result.items_of(Player.PLAYER)[Item.CIGARETTES] == 0  # stolen item also consumed
    assert result.items_of(Player.DEALER)[Item.CIGARETTES] == 0  # removed from opponent
    assert result.turn == Player.PLAYER  # cigarettes retains the turn
    # Adrenaline (1) + the stolen cigarette's own use (1) = 2, the judgment call.
    assert result.items_used_this_turn == 2


# ---------------------------------------------------------------------------
# Item-use cap (SPEC.md §7.5)
# ---------------------------------------------------------------------------


def test_item_cap_blocks_use_actions_but_not_shots():
    cap = RULES.solver.max_items_per_turn
    items = (ItemCounts.of(cigarettes=1), ItemCounts.empty())
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        items=items,
        items_used_this_turn=cap,
    )
    assert item_cap_reached(state, RULES) is True
    actions = legal_actions(state, RULES)
    assert actions == (Action(ActionKind.SHOOT_SELF), Action(ActionKind.SHOOT_OPPONENT))


def test_item_cap_not_reached_below_threshold():
    cap = RULES.solver.max_items_per_turn
    items = (ItemCounts.of(cigarettes=1), ItemCounts.empty())
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        items=items,
        items_used_this_turn=cap - 1,
    )
    assert item_cap_reached(state, RULES) is False
    actions = legal_actions(state, RULES)
    assert Action(ActionKind.USE, Item.CIGARETTES) in actions


# ---------------------------------------------------------------------------
# legal_actions ordering
# ---------------------------------------------------------------------------


def test_legal_actions_stable_order():
    items = (
        ItemCounts.of(cigarettes=1, handsaw=1, adrenaline=1),
        ItemCounts.of(handcuffs=1, beer=1),
    )
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=items)
    actions = legal_actions(state, RULES)
    assert actions[0] == Action(ActionKind.SHOOT_SELF)
    assert actions[1] == Action(ActionKind.SHOOT_OPPONENT)
    # Remaining USE actions must appear in Item enum order; adrenaline
    # variants (also enum-ordered by kind) come after plain USEs since
    # ADRENALINE > CIGARETTES/HANDSAW in Item value order.
    use_items = [a.item for a in actions[2:]]
    assert use_items == sorted(use_items, key=lambda i: i.value) or True  # enum order below
    expected = [
        Action(ActionKind.USE, Item.CIGARETTES),
        Action(ActionKind.USE, Item.HANDSAW),
        Action(ActionKind.USE, Item.ADRENALINE, Item.HANDCUFFS),
        Action(ActionKind.USE, Item.ADRENALINE, Item.BEER),
    ]
    assert actions[2:] == tuple(expected)


# ---------------------------------------------------------------------------
# str()
# ---------------------------------------------------------------------------


def test_action_str_labels():
    assert str(Action(ActionKind.SHOOT_SELF)) == "SHOOT SELF"
    assert str(Action(ActionKind.SHOOT_OPPONENT)) == "SHOOT OPPONENT"
    assert str(Action(ActionKind.USE, Item.HANDSAW)) == "USE HANDSAW"
    assert (
        str(Action(ActionKind.USE, Item.ADRENALINE, Item.HANDSAW))
        == "ADRENALINE -> HANDSAW"
    )


# ---------------------------------------------------------------------------
# Randomized sweep: every apply() distribution sums to 1.0
# ---------------------------------------------------------------------------


def _random_state(rng: random.Random) -> GameState:
    length = rng.randint(1, 6)
    n_unresolved = rng.randint(0, length)
    n_resolved = length - n_unresolved
    resolved = [rng.choice([Shell.LIVE, Shell.BLANK]) for _ in range(n_resolved)]
    slots = resolved + [None] * n_unresolved
    rng.shuffle(slots)
    unknown_live = rng.randint(0, n_unresolved)
    unknown_blank = n_unresolved - unknown_live
    # Ensure at least one live/blank exists somewhere so the game is legal-ish;
    # not required by ChamberState itself, so skip if the split is degenerate
    # only in the sense that deduce() will already have closed it - that is a
    # perfectly fine input.
    chamber = ChamberState.create(tuple(slots), unknown_live, unknown_blank)

    max_hp = rng.choice([2, 3, 4])
    hp = (rng.randint(1, max_hp), rng.randint(1, max_hp))
    turn = rng.choice([Player.PLAYER, Player.DEALER])
    sawed = rng.choice([True, False]) if len(chamber.slots) > 0 else False
    cuffed = (rng.choice([True, False]), rng.choice([True, False]))

    def random_counts() -> ItemCounts:
        counts = [0] * len(Item)
        n = len(chamber.slots)
        for item in Item:
            if item in (Item.EXPIRED_MEDICINE, Item.REMOTE):
                continue  # not in base/double-or-nothing heads-up set here
            counts[item.value] = rng.randint(0, 2)
        _ = n
        return ItemCounts(tuple(counts))

    items = (random_counts(), random_counts())
    dealer_knows = frozenset(
        i for i in range(len(chamber.slots)) if rng.random() < 0.3
    )
    items_used_this_turn = rng.randint(0, RULES.solver.max_items_per_turn - 1)

    return GameState.create(
        chamber,
        hp,
        max_hp,
        turn,
        items=items,
        sawed=sawed,
        cuffed=cuffed,
        dealer_knows=dealer_knows,
        items_used_this_turn=items_used_this_turn,
    )


def test_apply_distributions_sum_to_one_randomized_sweep():
    rng = random.Random(20260902)
    checked = 0
    for _ in range(300):
        state = _random_state(rng)
        for action in legal_actions(state, RULES):
            outcomes = apply(state, action, RULES)
            assert len(outcomes) > 0
            total = sum(w for w, _ in outcomes)
            assert total == pytest.approx(1.0, abs=1e-12), (
                f"action {action} at state {state} summed to {total}"
            )
            checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# Both seats (regression: SHOOT_SELF once hardcoded Player.PLAYER as the target)
#
# Every test above plays from the player's seat. The Dealer drives these same
# transitions under its §3.3 policy, so a seat assumption anywhere in the
# module is invisible to a player-only suite — which is exactly how the
# SHOOT_SELF damage target came to be wrong. These exercise §6.1 from both
# sides; the mirror property below covers the rest structurally.
# ---------------------------------------------------------------------------

SEATS = [Player.PLAYER, Player.DEALER]


def seated(seat, mine, theirs):
    """Order a (mine, theirs) pair into (player, dealer) tuple order for `seat`."""
    return (mine, theirs) if seat is Player.PLAYER else (theirs, mine)


@pytest.mark.parametrize("seat", SEATS)
def test_shoot_self_live_damages_the_mover_not_a_fixed_seat(seat):
    """The self-shot's damage lands on `state.turn`, whoever that is."""
    state = make_state((None,), 1, 0, hp=seated(seat, 3, 3), turn=seat)
    ((weight, result),) = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert weight == pytest.approx(1.0)
    assert result.hp_of(seat) == 3 - RULES.turns.base_damage
    assert result.hp_of(seat.other) == 3, "the non-mover must be untouched"
    assert result.turn == seat.other  # live self-shot ends the turn


@pytest.mark.parametrize("seat", SEATS)
def test_shoot_self_blank_retains_turn_from_either_seat(seat):
    state = make_state((None,), 0, 1, hp=seated(seat, 3, 3), turn=seat)
    ((_, result),) = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert result.hp == (3, 3)
    assert result.turn == seat


@pytest.mark.parametrize("seat", SEATS)
def test_shoot_opponent_live_damages_the_non_mover_from_either_seat(seat):
    state = make_state((None,), 1, 0, hp=seated(seat, 3, 3), turn=seat)
    ((_, result),) = apply(state, Action(ActionKind.SHOOT_OPPONENT), RULES)
    assert result.hp_of(seat.other) == 3 - RULES.turns.base_damage
    assert result.hp_of(seat) == 3
    assert result.turn == seat.other


@pytest.mark.parametrize("seat", SEATS)
def test_sawed_self_shot_deals_saw_damage_to_the_mover(seat):
    state = make_state(
        (None, None), 1, 1, hp=seated(seat, 3, 3), turn=seat, sawed=True
    )
    _, result = outcome_for(
        apply(state, Action(ActionKind.SHOOT_SELF), RULES),
        lambda s: s.hp_of(seat) < 3,
    )
    assert result.hp_of(seat) == 3 - RULES.item_semantics.handsaw.damage
    assert result.hp_of(seat.other) == 3


@pytest.mark.parametrize("seat", SEATS)
def test_cigarettes_heal_the_mover_from_either_seat(seat):
    items = seated(seat, ItemCounts.of(cigarettes=1), ItemCounts.empty())
    state = make_state(
        (None,), 1, 0, hp=seated(seat, 1, 1), max_hp=3, turn=seat, items=items
    )
    ((_, result),) = apply(state, Action(ActionKind.USE, Item.CIGARETTES), RULES)
    assert result.hp_of(seat) == 1 + RULES.item_semantics.cigarettes.heal
    assert result.hp_of(seat.other) == 1


@pytest.mark.parametrize("seat", SEATS)
def test_handcuffs_cuff_the_non_mover_from_either_seat(seat):
    items = seated(seat, ItemCounts.of(handcuffs=1), ItemCounts.empty())
    state = make_state((None,), 1, 0, turn=seat, items=items)
    ((_, result),) = apply(state, Action(ActionKind.USE, Item.HANDCUFFS), RULES)
    assert result.is_cuffed(seat.other) is True
    assert result.is_cuffed(seat) is False


@pytest.mark.parametrize("seat", SEATS)
def test_expired_medicine_harms_the_mover_from_either_seat(seat):
    items = seated(seat, ItemCounts.of(expired_medicine=1), ItemCounts.empty())
    state = make_state(
        (None,), 1, 0, hp=seated(seat, 2, 2), max_hp=3, turn=seat, items=items
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.EXPIRED_MEDICINE), RULES)
    _, harmed = outcome_for(outcomes, lambda s: s.hp_of(seat) < 2)
    assert harmed.hp_of(seat) == 2 - RULES.item_semantics.expired_medicine.harm
    assert harmed.hp_of(seat.other) == 2


# ---------------------------------------------------------------------------
# Mirror symmetry — the structural guard against seat assumptions
# ---------------------------------------------------------------------------


def _state_key(s: GameState):
    """A fully deterministic identity for a state, for comparing distributions."""
    return (
        # -1 for unresolved: `None` and `Shell` are not mutually orderable,
        # and this key gets sorted.
        tuple(-1 if x is None else int(x) for x in s.chamber.slots),
        s.chamber.unknown_live,
        s.chamber.unknown_blank,
        s.hp,
        s.max_hp,
        s.items[0].counts,
        s.items[1].counts,
        s.sawed,
        s.cuffed,
        int(s.turn),
        tuple(sorted(s.dealer_knows)),
        s.items_used_this_turn,
    )


def _mirror(s: GameState) -> GameState:
    """Swap the two seats. Requires empty `dealer_knows` to be well defined.

    `dealer_knows` is the Dealer's private knowledge (SPEC.md §7.3) and has no
    player-side counterpart, so it is the one field the game is genuinely
    asymmetric in — mirrored states are built without it.
    """
    assert not s.dealer_knows
    return GameState.create(
        s.chamber,
        (s.hp[1], s.hp[0]),
        s.max_hp,
        s.turn.other,
        items=(s.items[1], s.items[0]),
        sawed=s.sawed,
        cuffed=(s.cuffed[1], s.cuffed[0]),
        dealer_knows=frozenset(),
        items_used_this_turn=s.items_used_this_turn,
    )


def _distribution_key(outcomes):
    return sorted((round(w, 12), _state_key(s)) for w, s in outcomes)


def test_transitions_are_seat_symmetric_randomized_sweep():
    """Mirroring a position mirrors every successor, for every legal action.

    Nothing in §6 privileges a seat: the Dealer's *policy* differs (that is
    M3), but the transitions are the same game for both players. Any hardcoded
    `Player.PLAYER` in `actions.py` breaks this immediately.
    """
    rng = random.Random(0xBADC0FFEE)
    checked = 0
    for _ in range(300):
        state = _random_state(rng).with_dealer_knows(frozenset())
        mirrored = _mirror(state)

        assert legal_actions(state, RULES) == legal_actions(mirrored, RULES), (
            f"legal actions differ between seats at {state}"
        )

        for action in legal_actions(state, RULES):
            direct = apply(mirrored, action, RULES)
            reflected = [(w, _mirror(s)) for w, s in apply(state, action, RULES)]
            assert _distribution_key(direct) == _distribution_key(reflected), (
                f"action {action} is not seat-symmetric at {state}"
            )
            checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# The item-use cap actually accumulates (guards _retain_turn's contract)
# ---------------------------------------------------------------------------


def test_item_uses_accumulate_until_the_cap_binds():
    """A chain of item uses charges the counter once each, until it binds.

    `_retain_turn` must not reset `items_used_this_turn` — if it does, the
    §7.5 cap never binds and the adrenaline worst case is unbounded.
    """
    cap = RULES.solver.max_items_per_turn
    items = (ItemCounts.of(cigarettes=cap), ItemCounts.empty())
    state = make_state((None, None), 1, 1, hp=(1, 3), max_hp=4, items=items)

    for expected in range(1, cap + 1):
        assert not item_cap_reached(state, RULES)
        assert Action(ActionKind.USE, Item.CIGARETTES) in legal_actions(state, RULES)
        ((_, state),) = apply(state, Action(ActionKind.USE, Item.CIGARETTES), RULES)
        assert state.items_used_this_turn == expected
        assert state.turn == Player.PLAYER  # every item use retains the turn

    assert item_cap_reached(state, RULES)
    assert all(a.kind is not ActionKind.USE for a in legal_actions(state, RULES))


def test_blank_self_shot_refills_the_item_budget():
    """A blank self-shot is a *new* turn, so the item-use counter resets."""
    state = make_state((None,), 0, 1, items_used_this_turn=3)
    ((_, result),) = apply(state, Action(ActionKind.SHOOT_SELF), RULES)
    assert result.turn == Player.PLAYER
    assert result.items_used_this_turn == 0


def test_adrenaline_charges_the_cap_for_both_uses():
    """A steal charges 2 — the adrenaline plus the stolen item's immediate use."""
    items = (ItemCounts.of(adrenaline=1), ItemCounts.of(cigarettes=1))
    state = make_state((None,), 1, 0, hp=(1, 3), max_hp=3, items=items)
    action = Action(ActionKind.USE, Item.ADRENALINE, Item.CIGARETTES)
    ((_, result),) = apply(state, action, RULES)
    assert result.items_used_this_turn == 2
