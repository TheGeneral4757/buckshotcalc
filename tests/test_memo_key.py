"""Tests for buckshotcalc.engine.memo_key (SPEC.md §7.7 item 2, M8.5).

The correctness bar for `encode`/`decode` is injectivity: two distinct
`GameState`s must never collide onto the same packed `int`, because a
collision means the memo silently returns a value computed for a different
position, with nothing raising (milestone brief). This file's backbone is
therefore an exhaustive enumeration of a bounded-but-non-trivial state space,
checked two ways: `len(codes) == len(states)` (injectivity) and
`decode(encode(s)) == s` for every one of them (round-trip).
"""

from __future__ import annotations

import itertools

import pytest

from buckshotcalc.engine.memo_key import decode, encode
from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.state import (
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from tests.test_solver import SMALL_OVERRIDES, _custom_rules

RULES = default_rules()


# ---------------------------------------------------------------------------
# Bounded state-space enumeration
# ---------------------------------------------------------------------------


def _chamber_variants(length: int) -> list[ChamberState]:
    """A handful of distinct, valid `ChamberState`s at a given `length`.

    Covers three shapes: fully resolved (every possible live/blank pattern),
    fully unresolved (every legal live/blank pool split), and mixed (one
    resolved slot ahead of an unresolved pool) — so the packed slot field is
    exercised in both its "resolved" and "unresolved" codes side by side
    within one state, not only in isolation.
    """
    if length == 0:
        return [ChamberState((), 0, 0)]

    variants: list[ChamberState] = []
    for combo in itertools.product((Shell.LIVE, Shell.BLANK), repeat=length):
        variants.append(ChamberState(combo, 0, 0))
    for unknown_live in range(length + 1):
        variants.append(ChamberState((None,) * length, unknown_live, length - unknown_live))
    for unknown_live in range(1, length):
        variants.append(ChamberState((None,) * length, unknown_live, length - unknown_live, True))
    if length >= 2:
        rest = length - 1
        for first in (Shell.LIVE, Shell.BLANK):
            for unknown_live in range(rest + 1):
                slots = (first,) + (None,) * rest
                variants.append(ChamberState(slots, unknown_live, rest - unknown_live))
    return variants


def _enumerate_states() -> list[GameState]:
    """A bounded but non-trivial set of distinct, canonical `GameState`s.

    Small shell counts (chamber length 0..3) and small HP (1..2, `max_hp`
    fixed at 2), varying every field `encode` packs: chamber shape (via
    `_chamber_variants`), both HPs, turn, sawed, both cuff states (0/1/2), a
    representative sample of `dealer_knows` subsets, one item per side, and
    `items_used_this_turn`. Deduplicated by `GameState` equality (its
    `__eq__`/`__hash__` are the canonical-state identity §5.3 defines)
    before returning, so what's counted is genuinely distinct positions, not
    duplicate constructions of the same one.
    """
    max_hp = 2
    seen: dict[GameState, None] = {}
    for length in range(4):
        for chamber in _chamber_variants(length):
            dealer_knows_options: list[frozenset[int]] = [frozenset(), frozenset(range(length))]
            if length >= 1:
                dealer_knows_options.append(frozenset({0}))
            for hp_p, hp_d in itertools.product((1, 2), repeat=2):
                for turn in (Player.PLAYER, Player.DEALER):
                    for sawed in (False, True):
                        for cuffed in itertools.product((0, 1, 2), repeat=2):
                            for dealer_knows in dealer_knows_options:
                                for cig, saw in itertools.product((0, 1), repeat=2):
                                    for items_used in (0, 1):
                                        items = (
                                            ItemCounts.of(cigarettes=cig),
                                            ItemCounts.of(handsaw=saw),
                                        )
                                        state = GameState.create(
                                            chamber=chamber,
                                            hp=(hp_p, hp_d),
                                            max_hp=max_hp,
                                            turn=turn,
                                            items=items,
                                            sawed=sawed,
                                            cuffed=cuffed,
                                            dealer_knows=dealer_knows,
                                            items_used_this_turn=items_used,
                                        )
                                        seen[state] = None
    return list(seen)


_STATES = _enumerate_states()


def test_enumeration_is_non_trivial() -> None:
    """A sanity floor on the fixture itself: this must be a real state space,
    not an accidentally-empty or single-element one that would make the
    injectivity test below pass vacuously."""
    assert len(_STATES) > 1000, (
        f"only {len(_STATES)} distinct states enumerated — too small to call "
        "this an exhaustive-enough check of injectivity"
    )


@pytest.mark.slow
def test_encoding_is_injective() -> None:
    """No two distinct states among `_STATES` may encode to the same `int`."""
    codes = [encode(s, RULES) for s in _STATES]
    assert len(set(codes)) == len(_STATES), (
        "memo_key.encode collision: fewer distinct codes than distinct states "
        "— two different GameStates packed to the same int"
    )


@pytest.mark.slow
def test_decode_round_trips_every_enumerated_state() -> None:
    for state in _STATES:
        code = encode(state, RULES)
        assert decode(code, RULES) == state


# ---------------------------------------------------------------------------
# Targeted unit tests
# ---------------------------------------------------------------------------


def test_round_trip_single_state() -> None:
    chamber = ChamberState.create((None, None, None), 2, 1)
    state = GameState.create(
        chamber=chamber,
        hp=(3, 4),
        max_hp=4,
        turn=Player.DEALER,
        items=(ItemCounts.of(cigarettes=1, beer=2), ItemCounts.of(handcuffs=1)),
        sawed=False,
        cuffed=(True, False),
        dealer_knows=frozenset({1}),
        items_used_this_turn=2,
    )
    code = encode(state, RULES)
    assert isinstance(code, int)
    assert code >= 0
    assert decode(code, RULES) == state


def test_round_trip_all_items_at_max_slots() -> None:
    """Every item field at its widest legal value (SPEC.md §10's `items.max_slots`),
    both players, so every one of the 20 item fields is exercised at its
    upper bound in one state, not just at 0/1."""
    max_slots = RULES.items.max_slots
    per_item = max_slots // len(Item)  # keep each player's total <= max_slots
    counts = tuple(per_item for _ in Item)
    total = RULES.shells.max_total
    chamber = ChamberState.create(tuple([None] * total), 4, total - 4)
    state = GameState.create(
        chamber=chamber,
        hp=(1, 1),
        max_hp=max(RULES.health.possible_max_hp),
        turn=Player.PLAYER,
        items=(ItemCounts(counts), ItemCounts(counts)),
        dealer_knows=frozenset(range(RULES.shells.max_total)),
    )
    code = encode(state, RULES)
    assert decode(code, RULES) == state


def test_encode_canonicalizes_before_packing() -> None:
    """An uncanonicalized state and its canonical form must encode identically
    — encode() canonicalizes internally so identity is never redefined by an
    oversight at a call site (SPEC.md §5.3)."""
    chamber = ChamberState((None, None), 0, 2)  # pool is all-blank: deduce() should close it
    uncanonical = GameState(
        chamber=chamber,
        hp=(2, 2),
        max_hp=2,
        items=(ItemCounts.empty(), ItemCounts.empty()),
        sawed=False,
        cuffed=(False, False),
        turn=Player.PLAYER,
        dealer_knows=frozenset(),
        items_used_this_turn=0,
    )
    canonical = uncanonical.canonical()
    assert uncanonical != canonical  # the pool-closing actually changed something
    assert encode(uncanonical, RULES) == encode(canonical, RULES)


# ---------------------------------------------------------------------------
# Overflow must raise, never silently truncate
# ---------------------------------------------------------------------------


def test_item_count_exceeding_max_slots_raises() -> None:
    """`ItemCounts` itself does not cap a single item's count at
    `items.max_slots` (only the rack *total* is capped, by `actions.py`, not
    by the dataclass), so a value wider than its derived bit field is
    reachable and must raise rather than truncate.

    The bit field's width is rounded up to the next power of two, so an
    overflow test must clear that rounding, not just `max_slots + 1` — hence
    a value far beyond any plausible `items.max_slots`.
    """
    overflow = 10_000
    chamber = ChamberState.create((), 0, 0)
    state = GameState.create(
        chamber=chamber,
        hp=(1, 1),
        max_hp=2,
        turn=Player.PLAYER,
        items=(ItemCounts.of(cigarettes=overflow), ItemCounts.empty()),
    )
    with pytest.raises(ValueError, match="does not fit"):
        encode(state, RULES)


def test_hp_exceeding_table_bound_raises() -> None:
    """Same rounding note as the item-count overflow test above: the bound
    must clear the field's power-of-two width, not just the rules bound."""
    overflow_hp = 10_000
    chamber = ChamberState.create((), 0, 0)
    state = GameState.create(
        chamber=chamber,
        hp=(overflow_hp, 1),
        max_hp=overflow_hp,
        turn=Player.PLAYER,
    )
    with pytest.raises(ValueError, match="does not fit"):
        encode(state, RULES)


def test_decode_rejects_negative_code() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        decode(-1, RULES)


def test_widths_cache_survives_rules_garbage_collection(tmp_path) -> None:
    """Regression test for a real bug this milestone hit: an `id(rules)`-keyed
    widths cache silently returned another `Rules` object's (too-narrow)
    widths once the first was garbage collected and its `id()` reused,
    because CPython is free to reuse an address the moment refcount hits
    zero. `_widths` must key on `rules` itself (equality), not `id(rules)`,
    so this never happens regardless of GC timing.
    """
    import gc

    from buckshotcalc.engine.rules import load_rules
    from tests.test_rules import _load_real_dict, _write_toml

    data = _load_real_dict()
    data["shells"]["max_total"] = 2  # a narrow chamber-length/pool bound
    small_dir = tmp_path / "small"
    small_dir.mkdir()
    small_path = _write_toml(small_dir, data)

    small_rules = load_rules(small_path)
    small_code = encode(
        GameState.create(
            chamber=ChamberState.create((), 0, 0),
            hp=(1, 1),
            max_hp=2,
            turn=Player.PLAYER,
        ),
        small_rules,
    )
    del small_rules
    gc.collect()  # force the id CPython might now reuse to actually free up

    # RULES has a much wider max_total (8) and max HP (4) — if the cache
    # were id-keyed and collided with the freed `small_rules`'s id, a value
    # that only fits the wide rules' bit width would wrongly be checked
    # against the narrow one's and (depending on luck) raise or truncate.
    wide_state = GameState.create(
        chamber=ChamberState.create((), 0, 0),
        hp=(4, 4),
        max_hp=4,
        turn=Player.PLAYER,
    )
    wide_code = encode(wide_state, RULES)  # must not raise
    assert decode(wide_code, RULES) == wide_state
    assert wide_code != small_code


def test_decode_rejects_leftover_high_bits() -> None:
    """A code with more high-order bits than the field layout accounts for
    was not produced by `encode()` under this `Rules` — must raise, not
    silently drop bits."""
    valid = encode(
        GameState.create(
            chamber=ChamberState.create((), 0, 0),
            hp=(1, 1),
            max_hp=2,
            turn=Player.PLAYER,
        ),
        RULES,
    )
    too_big = valid | (1 << 500)
    with pytest.raises(ValueError, match="leftover"):
        decode(too_big, RULES)


@pytest.mark.parametrize("cap", [1, 6, 7])
def test_cap_field_holds_a_counter_that_exceeded_the_cap(tmp_path, cap):
    """`items_used_this_turn` may exceed `max_items_per_turn`, and must still pack.

    `actions._apply_use` charges `count_delta` *after* the legality check, and
    Adrenaline charges 2 (itself plus the item it steals and uses), so a turn
    entering at `cap - 1` leaves at `cap + 1`. Sizing the field from the cap
    alone happens to fit at the shipped cap of 6 — three bits hold 0..7 — and
    raises at 1 and at 7. Parametrized across all three so the accidental pass
    at 6 cannot stand in for the general case.
    """
    rules = _custom_rules(tmp_path, {**SMALL_OVERRIDES, "solver.max_items_per_turn": cap})
    state = GameState.create(
        chamber=ChamberState.create((None, None), 1, 1),
        hp=(2, 2),
        max_hp=3,
        turn=Player.PLAYER,
        items=(ItemCounts.empty(), ItemCounts.empty()),
        sawed=False,
        cuffed=(False, False),
        dealer_knows=frozenset(),
        items_used_this_turn=cap + 1,
    )
    assert decode(encode(state, rules), rules) == state
