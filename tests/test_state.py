"""Tests for buckshotcalc.engine.state (SPEC.md §5, M1 acceptance)."""

from __future__ import annotations

import random

import pytest

from buckshotcalc.engine.state import (
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)


def random_chamber_state(rng: random.Random, max_len: int = 8) -> ChamberState:
    """Generate a random, invariant-respecting `ChamberState`.

    Test-only generator for the SPEC.md §5 invariant property test. Pick a
    length, pick which slots are resolved, then split the pool over exactly
    the unresolved count — which is the invariant, satisfied by construction.
    Deliberately does *not* call `deduce()`: an all-one-type pool is a valid
    input that `deduce` is supposed to close, so generating it is the point.
    """
    length = rng.randint(0, max_len)
    slots: list[Shell | None] = [
        rng.choice([Shell.LIVE, Shell.BLANK, None]) for _ in range(length)
    ]
    unresolved = slots.count(None)
    unknown_live = rng.randint(0, unresolved)
    return ChamberState(tuple(slots), unknown_live, unresolved - unknown_live)

# ---------------------------------------------------------------------------
# Item / ItemCounts
# ---------------------------------------------------------------------------


def test_item_config_name_round_trips():
    for item in Item:
        assert Item.from_config_name(item.config_name) is item
    assert Item.MAGNIFYING_GLASS.config_name == "magnifying_glass"


def test_item_from_config_name_unknown_raises():
    with pytest.raises(KeyError):
        Item.from_config_name("not_an_item")


def test_item_counts_empty_and_of():
    empty = ItemCounts.empty()
    assert empty.total() == 0
    assert list(empty.held()) == []

    counts = ItemCounts.of(cigarettes=2, beer=1)
    assert counts[Item.CIGARETTES] == 2
    assert counts[Item.BEER] == 1
    assert counts[Item.HANDCUFFS] == 0
    assert counts.total() == 3
    assert set(counts.held()) == {(Item.CIGARETTES, 2), (Item.BEER, 1)}


def test_item_counts_order_insensitive():
    a = ItemCounts.of(cigarettes=1, beer=2)
    b = ItemCounts.of(beer=2, cigarettes=1)
    assert a == b
    assert hash(a) == hash(b)


def test_item_counts_rejects_negative():
    with pytest.raises(ValueError):
        ItemCounts.from_mapping({Item.BEER: -1})


def test_item_counts_with_delta():
    counts = ItemCounts.of(beer=1)
    increased = counts.with_delta(Item.BEER, 2)
    assert increased[Item.BEER] == 3
    decreased = increased.with_delta(Item.BEER, -3)
    assert decreased[Item.BEER] == 0
    with pytest.raises(ValueError):
        counts.with_delta(Item.BEER, -5)


def test_item_counts_is_full():
    counts = ItemCounts.of(cigarettes=8)
    assert counts.is_full(8)
    assert not counts.is_full(9)


# ---------------------------------------------------------------------------
# ChamberState: hand-computed exact values
# ---------------------------------------------------------------------------


def test_p_live_unresolved_matches_hand_computation():
    c = ChamberState.create((None,), 1, 0)
    assert c.p_live() == 1.0  # L1 B0: guaranteed live


def test_p_live_l0_b1_is_zero():
    c = ChamberState.create((None,), 0, 1)
    assert c.p_live() == 0.0  # L0 B1: guaranteed blank


def test_p_live_l1_b1_is_half():
    c = ChamberState.create((None, None), 1, 1)
    assert c.p_live() == 0.5


def test_p_live_resolved_slot_zero_returns_exact_bounds():
    live = ChamberState.create((Shell.LIVE, None), 0, 1)
    assert live.p_live() == 1.0
    blank = ChamberState.create((Shell.BLANK, None), 1, 0)
    assert blank.p_live() == 0.0


def test_total_live_and_blank():
    c = ChamberState.create((Shell.LIVE, None, None), 1, 1)
    assert c.total_live() == 2
    assert c.total_blank() == 1


def test_resolve_decrements_matching_pool():
    # Pool (2, 2) over 4 unresolved slots: resolving one shell still leaves a
    # mixed pool either way, so the result isn't further closed by deduce().
    c = ChamberState.create((None, None, None, None), 2, 2)
    resolved_live = c.resolve(0, Shell.LIVE)
    assert resolved_live.slots[0] == Shell.LIVE
    assert resolved_live.unknown_live == 1
    assert resolved_live.unknown_blank == 2

    resolved_blank = c.resolve(0, Shell.BLANK)
    assert resolved_blank.slots[0] == Shell.BLANK
    assert resolved_blank.unknown_live == 2
    assert resolved_blank.unknown_blank == 1


def test_resolve_already_resolved_raises():
    c = ChamberState.create((Shell.LIVE,), 0, 0)
    with pytest.raises(ValueError):
        c.resolve(0, Shell.LIVE)


def test_resolve_empty_pool_raises():
    c = ChamberState.create((None,), 1, 0)
    with pytest.raises(ValueError):
        c.resolve(0, Shell.BLANK)


def test_pop_renumbers_remaining_slots():
    # Mixed pool over the two unresolved slots so neither closes via deduce().
    c = ChamberState.create((Shell.LIVE, None, None), 1, 1)
    popped = c.pop()
    assert popped.slots == (None, None)
    assert len(popped) == 2
    assert popped.unknown_live == 1
    assert popped.unknown_blank == 1


def test_pop_unresolved_slot_zero_raises():
    c = ChamberState.create((None, None), 1, 1)
    with pytest.raises(ValueError):
        c.pop()


def test_deduce_closes_all_one_type_pool():
    # 3 unresolved slots, only live shells left in the pool -> all resolve LIVE
    c = ChamberState.create((None, None, None), 3, 0)
    assert c.slots == (Shell.LIVE, Shell.LIVE, Shell.LIVE)
    assert c.unknown_live == 0 and c.unknown_blank == 0

    c2 = ChamberState.create((Shell.BLANK, None, None), 0, 2)
    assert c2.slots == (Shell.BLANK, Shell.BLANK, Shell.BLANK)


def test_deduce_does_not_close_mixed_pool():
    c = ChamberState.create((None, None), 1, 1)
    assert c.slots == (None, None)


def test_chamber_len_and_is_empty():
    c = ChamberState.create((), 0, 0)
    assert len(c) == 0
    assert c.is_empty
    c2 = ChamberState.create((None,), 1, 0)
    assert len(c2) == 1
    assert not c2.is_empty


# ---------------------------------------------------------------------------
# ChamberState invariant: property test over many random chambers
# ---------------------------------------------------------------------------


def test_chamber_invariant_holds_over_random_chambers():
    rng = random.Random(1234)
    for _ in range(2000):
        c = random_chamber_state(rng)
        assert c.slots.count(None) == c.unknown_live + c.unknown_blank
        assert c.unknown_live >= 0
        assert c.unknown_blank >= 0
        # p_live is only meaningful when there is a chambered slot to ask about
        # (SPEC.md §5's p_live snippet assumes a nonempty chamber).
        if not c.is_empty:
            assert 0.0 <= c.p_live() <= 1.0


def test_constructing_invalid_chamber_raises():
    with pytest.raises(ValueError):
        ChamberState((None, None), 1, 0)  # 2 unresolved slots, pool of 1
    with pytest.raises(ValueError):
        ChamberState((Shell.LIVE,), -1, 1)


# ---------------------------------------------------------------------------
# GameState: canonicalization
# ---------------------------------------------------------------------------


def _base_game_state(**overrides) -> GameState:
    kwargs = dict(
        chamber=ChamberState.create((None, None), 1, 1),
        hp=(3, 3),
        max_hp=3,
        items=(ItemCounts.empty(), ItemCounts.empty()),
        sawed=False,
        cuffed=(False, False),
        turn=Player.PLAYER,
        dealer_knows=frozenset(),
        items_used_this_turn=0,
    )
    kwargs.update(overrides)
    return GameState.create(**kwargs)


def test_canonical_is_idempotent():
    s = _base_game_state(dealer_knows=frozenset({0}))
    once = s.canonical()
    twice = once.canonical()
    assert once == twice
    assert hash(once) == hash(twice)


def test_canonical_drops_stale_dealer_knows_index():
    # chamber has 2 slots (indices 0, 1); index 5 is stale and must be dropped.
    with_stale = _base_game_state(dealer_knows=frozenset({0, 5}))
    without_stale = _base_game_state(dealer_knows=frozenset({0}))
    assert with_stale.canonical() == without_stale.canonical()
    assert hash(with_stale.canonical()) == hash(without_stale.canonical())
    assert 5 not in with_stale.dealer_knows  # canonicalized at construction via create()


def test_canonical_clamps_negative_hp_to_zero():
    negative = _base_game_state(hp=(-1, 3))
    zero = _base_game_state(hp=(0, 3))
    assert negative == zero
    assert hash(negative) == hash(zero)
    assert negative.hp == (0, 3)


def test_canonical_forces_sawed_false_on_empty_chamber():
    s = GameState.create(
        chamber=ChamberState.create((), 0, 0),
        hp=(3, 3),
        max_hp=3,
        items=(ItemCounts.empty(), ItemCounts.empty()),
        sawed=True,
        cuffed=(False, False),
        turn=Player.PLAYER,
        dealer_knows=frozenset(),
        items_used_this_turn=0,
    )
    assert s.sawed is False


def test_canonical_closes_chamber_pool():
    # All-live pool over 2 unresolved slots must close on canonicalization.
    s = GameState.create(
        chamber=ChamberState((None, None), 2, 0),
        hp=(3, 3),
        max_hp=3,
        items=(ItemCounts.empty(), ItemCounts.empty()),
        sawed=False,
        cuffed=(False, False),
        turn=Player.PLAYER,
        dealer_knows=frozenset(),
        items_used_this_turn=0,
    )
    assert s.chamber.slots == (Shell.LIVE, Shell.LIVE)


# ---------------------------------------------------------------------------
# GameState: read-only helpers
# ---------------------------------------------------------------------------


def test_hp_of_items_of_is_cuffed():
    s = _base_game_state(
        hp=(2, 3),
        items=(ItemCounts.of(beer=1), ItemCounts.empty()),
        cuffed=(True, False),
    )
    assert s.hp_of(Player.PLAYER) == 2
    assert s.hp_of(Player.DEALER) == 3
    assert s.items_of(Player.PLAYER)[Item.BEER] == 1
    assert s.is_cuffed(Player.PLAYER) is True
    assert s.is_cuffed(Player.DEALER) is False


def test_winner_none_when_both_alive():
    s = _base_game_state(hp=(1, 1))
    assert s.winner() is None


def test_winner_player_when_dealer_at_zero():
    s = _base_game_state(hp=(1, 0))
    assert s.winner() is Player.PLAYER


def test_winner_dealer_when_player_at_zero():
    s = _base_game_state(hp=(0, 1))
    assert s.winner() is Player.DEALER


def test_player_other():
    assert Player.PLAYER.other is Player.DEALER
    assert Player.DEALER.other is Player.PLAYER


def test_with_helpers_return_canonicalized_copies():
    s = _base_game_state()
    s2 = s.with_hp(Player.PLAYER, 1)
    assert s2.hp == (1, 3)
    assert s.hp == (3, 3)  # original untouched

    s3 = s.with_turn(Player.DEALER)
    assert s3.turn is Player.DEALER

    s4 = s.with_sawed(True)
    assert s4.sawed is True

    s5 = s.with_cuffed(Player.DEALER, True)
    assert s5.cuffed == (False, True)

    s6 = s.with_items(Player.DEALER, ItemCounts.of(handsaw=1))
    assert s6.items[1][Item.HANDSAW] == 1

    s7 = s.with_items_used_this_turn(3)
    assert s7.items_used_this_turn == 3


# ---------------------------------------------------------------------------
# GameState.create defaults (keeps M2's transition code readable)
# ---------------------------------------------------------------------------


def test_create_defaults_to_a_fresh_load():
    s = GameState.create(
        chamber=ChamberState.create((None, None), 1, 1),
        hp=(3, 3),
        max_hp=3,
        turn=Player.PLAYER,
    )
    assert s.items == (ItemCounts.empty(), ItemCounts.empty())
    assert s.sawed is False
    assert s.cuffed == (False, False)
    assert s.dealer_knows == frozenset()
    assert s.items_used_this_turn == 0


def test_create_defaults_are_not_shared_between_states():
    a = GameState.create(
        chamber=ChamberState.create((None, None), 1, 1),
        hp=(3, 3),
        max_hp=3,
        turn=Player.PLAYER,
    )
    b = a.with_items(Player.PLAYER, ItemCounts.of(handsaw=1))
    c = GameState.create(
        chamber=ChamberState.create((None, None), 1, 1),
        hp=(3, 3),
        max_hp=3,
        turn=Player.PLAYER,
    )
    assert b.items_of(Player.PLAYER)[Item.HANDSAW] == 1
    assert c.items_of(Player.PLAYER)[Item.HANDSAW] == 0
    assert a == c and hash(a) == hash(c)
