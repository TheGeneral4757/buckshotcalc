"""Bit-packed integer memo keys for the reload fixed point (SPEC.md §7.7 item 2).

Pure stdlib. `Solver`'s reload-table sweep memoizes `GameState -> (value,
cap_reached)`. Retaining whole frozen `GameState` objects as `dict` keys is
costly to hash and costly to retain at the ~869 K-entry-per-key scale §7.7
measures; this module turns the key into a single Python `int` instead.

`encode`/`decode` never change what a state *is* — §5.3's canonicalization
stays the definition of state identity. `encode` canonicalizes its input
before packing (idempotent, so this is a no-op on an already-canonical
state) precisely so that identity is never redefined by an oversight at a
call site; `decode` exists even though the hot path never calls it, because
it is what makes injectivity testable and a wrong encoding debuggable
(milestone brief).

Bit widths are derived from `rules.toml` bounds, never hardcoded (SPEC.md §0.1
non-negotiable #2):

- chamber length, and the two unresolved shell pools, from `shells.max_total`
- both HPs and `max_hp` from `max(health.possible_max_hp)`
- a single item's count (there are ten, times two players) from
  `items.max_slots`
- `items_used_this_turn` from `solver.max_items_per_turn` — the field that
  actually bounds it (SPEC.md §7.5's Adrenaline-chain guard), which is a
  `rules.toml` value like the other three, not a literal

A field that does not fit its derived width raises rather than truncating:
a silent collision between two distinct states is the one failure mode here
that produces no exception and quietly returns a value computed for a
different position (milestone brief).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.state import ChamberState, GameState, Item, ItemCounts, Player, Shell

#: A chamber slot is unresolved, blank, or live; slot 0 may also be unresolved
#: and inverted (`ChamberState.inverted0`). Fixed
#: by the `Shell`/`None` domain itself, not a `rules.toml` bound.
_SLOT_UNRESOLVED = 0
_SLOT_BLANK = 1
_SLOT_LIVE = 2
_SLOT_INVERTED = 3  # slot 0 unresolved with `ChamberState.inverted0` set
_SLOT_WIDTH = 2  # bits(0..3)


@dataclass(frozen=True, slots=True)
class _Widths:
    """Per-field bit widths for one `Rules` instance's memo-key encoding."""

    max_total: int  # shells.max_total
    w_length: int  # chamber length: 0..max_total
    w_pool: int  # unknown_live / unknown_blank: 0..max_total
    w_hp: int  # hp_player, hp_dealer, max_hp: 0..max(possible_max_hp)
    w_mask: int  # dealer_knows bitmask: max_total bits, one per slot index
    w_cap: int  # items_used_this_turn: 0..solver.max_items_per_turn + 1 (see `_widths`)
    w_item: int  # one item's count: 0..items.max_slots


def _bits_for(max_value: int) -> int:
    """Bits needed to represent every integer in `0..max_value` inclusive.

    `bit_length()` alone under-counts when `max_value` is itself a power of
    two minus something small has already been handled correctly by
    `bit_length`; the only edge case is `max_value == 0`, which needs one
    bit (to hold the single value 0), not zero.
    """
    if max_value < 0:
        raise ValueError(f"memo_key: bit width requested for a negative bound {max_value}")
    return max(1, max_value.bit_length())


@lru_cache(maxsize=8)
def _widths(rules: Rules) -> _Widths:
    """Cached: `encode`/`encode_canonical` call this on every node the search
    visits (its hot path, at the reload sweep's ~1 M-entry per-key scale),
    and every field it needs is fixed for the lifetime of one `Rules`
    object, so recomputing four `bit_length()` calls and rebuilding this
    dataclass per node is pure waste.

    Keyed by `rules` itself (`lru_cache`'s normal equality-based lookup), not
    `id(rules)` — an `id`-keyed cache was tried and reverted: two distinct
    `Rules` objects can share an `id()` once the first is garbage collected
    and CPython reuses the address, which silently returns the *wrong*
    (too-narrow) widths for the second object. That collided in exactly the
    way §7.7 item 2's "the failure mode is silent" warning describes, and a
    test caught it (`test_montecarlo.py`'s reload-bias check, which builds
    many short-lived `Rules` instances). `Rules` is a frozen dataclass of
    frozen dataclasses/tuples/strings, so it hashes correctly for
    `lru_cache`'s purposes — structurally, which costs more per call than the
    `id`-keyed version did (see the milestone report's numbers), but is
    actually correct on every Python implementation, PyPy's non-refcounting
    GC included, where an `id`-reuse race would have been even harder to rule
    out. `maxsize=8` is generous headroom for "more than one `Rules` object
    alive in a process" without growing unbounded.
    """
    max_total = rules.shells.max_total
    max_hp = max(rules.health.possible_max_hp)
    max_slots = rules.items.max_slots
    # `items_used_this_turn` can EXCEED the cap. `actions._apply_use` charges
    # `count_delta` after the legality check, and Adrenaline charges 2 (itself
    # plus the item it steals and uses), so a turn entering at `cap - 1` leaves
    # at `cap + 1`. Sizing this field from the cap alone fits only by accident
    # at the shipped `max_items_per_turn = 6` (3 bits hold 0..7); it raises at
    # cap 1 and would raise again at cap 7. Found by integration, not by either
    # milestone's own tests.
    max_items_used = rules.solver.max_items_per_turn + 1
    return _Widths(
        max_total=max_total,
        w_length=_bits_for(max_total),
        w_pool=_bits_for(max_total),
        w_hp=_bits_for(max_hp),
        w_mask=max_total,
        w_cap=_bits_for(max_items_used),
        w_item=_bits_for(max_slots),
    )


def _field_order(w: _Widths) -> list[tuple[str, int]]:
    """`(field name, bit width)` in packing order, used by `decode`.

    `encode` (the hot path) packs the same fields in this same order by hand
    rather than by iterating this list — see its docstring — so this order
    is authoritative for `decode` and must be kept in sync with `encode`'s
    unrolled version by inspection; `test_decode_round_trips_every_enumerated_state`
    is what actually enforces they agree.
    """
    order: list[tuple[str, int]] = [("chamber_length", w.w_length)]
    order += [(f"slot{i}", _SLOT_WIDTH) for i in range(w.max_total)]
    order += [
        ("unknown_live", w.w_pool),
        ("unknown_blank", w.w_pool),
        ("hp_player", w.w_hp),
        ("hp_dealer", w.w_hp),
        ("max_hp", w.w_hp),
        ("turn", 1),
        ("sawed", 1),
        ("cuffed_player", 2),
        ("cuffed_dealer", 2),
        ("dealer_knows_mask", w.w_mask),
        ("items_used_this_turn", w.w_cap),
    ]
    for side in ("player", "dealer"):
        order += [(f"item_{side}_{item.name}", w.w_item) for item in Item]
    return order


def _pack(code: int, value: int, width: int, name: str) -> int:
    if value < 0 or value >= (1 << width):
        raise ValueError(
            f"memo_key.encode: field {name!r} value {value} does not fit its "
            f"{width}-bit width — this must raise, never silently truncate "
            "(SPEC.md §7.7 item 2)"
        )
    return (code << width) | value


def _unpack(code: int, width: int) -> tuple[int, int]:
    mask = (1 << width) - 1
    return code & mask, code >> width


def widths_for(rules: Rules) -> _Widths:
    """Public accessor for `_widths` — a `Solver` fetches this once (`__init__`)
    and reuses it for every node the search visits, rather than letting the
    hot path re-derive it from `rules` on every call.

    This matters because `_widths` is `lru_cache`d *by equality*, not
    identity (see its docstring for why an identity-keyed version was wrong):
    every cache lookup still hashes the full `rules` object to find the
    bucket, and `Rules` is a large nested frozen dataclass, so that hash is
    not free. Computing it once per `Solver` and threading the result through
    (`Solver._key_widths`, `_pack_state`) turns an O(nodes) cost into an O(1)
    one without reintroducing the identity-cache bug — see the milestone
    report's before/after numbers for how much this was worth.
    """
    return _widths(rules)


def encode(state: GameState, rules: Rules) -> int:
    """Pack `state` into a single non-negative `int`, injective over `rules`'s bounds.

    Canonicalizes first (§5.3 stays the definition of state identity — this
    changes only how the memo is *keyed*), then packs every field reachable
    from `GameState`: the chamber's slot tuple and its unresolved pool
    counts, both HPs, `max_hp`, `turn`, `sawed`, `cuffed`, `dealer_knows`,
    `items_used_this_turn`, and both players' item counts.

    This is the safe, general entry point for callers with a `Rules` but no
    already-canonicalized `state` and no reason to avoid `_widths`'s
    per-call cache lookup (tests, one-off tooling). `Solver`'s hot path uses
    `widths_for` once plus `_pack_state` directly instead — see their
    docstrings.
    """
    return _pack_state(state.canonical(), _widths(rules))


def encode_canonical(state: GameState, rules: Rules) -> int:
    """`encode`, minus the (redundant, for an already-canonical `state`) canonicalize call.

    Precondition: `state == state.canonical()`. Passing an uncanonical state
    here silently uses its as-given field values — the whole point of this
    variant is skipping the check, so it does not re-verify the precondition
    at runtime. Getting this wrong is exactly "encoding an uncanonicalized
    state," which the milestone brief calls a bug; `encode`'s round-trip
    tests (which always canonicalize) are what cover this function's packing
    logic (via `_pack_state`, which both funnel through).
    """
    return _pack_state(state, _widths(rules))


def _pack_state(state: GameState, w: _Widths) -> int:
    """The actual bit-packing, parameterized on an already-resolved `_Widths`.

    Hand-unrolled (no per-field name lookup, no intermediate `dict`) rather
    than driven by `_field_order` the way `decode` is: this is the search's
    hot path — the whole point of this module is being cheaper per node than
    hashing a `GameState` was — and building a ~30-entry dict of f-string
    keys per node measurably was not (see the milestone report). `decode` is
    off the hot path (only tests call it) and stays table-driven, since
    matching this order by construction there is worth more than the speed.
    `_pack`'s bounds check still runs on every field either way, so the
    "raise, never truncate" guarantee is identical regardless of which
    public function got here.

    Taking `w` directly (not `rules`) is what lets `Solver` amortize
    `_widths`'s cache lookup to once per solver instance instead of once per
    node — see `widths_for`.
    """
    chamber = state.chamber
    slots = chamber.slots
    length = len(slots)
    max_total = w.max_total

    code = _pack(0, length, w.w_length, "chamber_length")
    for i in range(max_total):
        if i < length:
            slot = slots[i]
            if slot is None:
                slot_code = _SLOT_INVERTED if i == 0 and chamber.inverted0 else _SLOT_UNRESOLVED
            elif slot == Shell.LIVE:
                slot_code = _SLOT_LIVE
            else:
                slot_code = _SLOT_BLANK
        else:
            slot_code = _SLOT_UNRESOLVED  # padding beyond the current length
        code = _pack(code, slot_code, _SLOT_WIDTH, "slot")
    code = _pack(code, chamber.unknown_live, w.w_pool, "unknown_live")
    code = _pack(code, chamber.unknown_blank, w.w_pool, "unknown_blank")
    code = _pack(code, state.hp[0], w.w_hp, "hp_player")
    code = _pack(code, state.hp[1], w.w_hp, "hp_dealer")
    code = _pack(code, state.max_hp, w.w_hp, "max_hp")
    code = _pack(code, int(state.turn), 1, "turn")
    code = _pack(code, int(state.sawed), 1, "sawed")
    code = _pack(code, int(state.cuffed[0]), 2, "cuffed_player")
    code = _pack(code, int(state.cuffed[1]), 2, "cuffed_dealer")
    mask = 0
    for i in state.dealer_knows:
        mask |= 1 << i
    code = _pack(code, mask, w.w_mask, "dealer_knows_mask")
    code = _pack(code, state.items_used_this_turn, w.w_cap, "items_used_this_turn")
    for side_items in state.items:
        for item in Item:
            code = _pack(code, side_items[item], w.w_item, "item")
    return code


def decode(code: int, rules: Rules) -> GameState:
    """Inverse of `encode`: reconstructs the exact `GameState` that produced `code`.

    Kept even though the hot path never calls it — it is what makes
    injectivity testable (`decode(encode(s)) == s`) and a wrong encoding
    debuggable rather than merely suspected.
    """
    if code < 0:
        raise ValueError(f"memo_key.decode: code must be non-negative, got {code}")
    w = _widths(rules)
    order = _field_order(w)

    values: dict[str, int] = {}
    remaining = code
    for name, width in reversed(order):
        raw, remaining = _unpack(remaining, width)
        values[name] = raw
    if remaining != 0:
        raise ValueError(
            f"memo_key.decode: {remaining} leftover high bits after unpacking every "
            "field — code was not produced by encode() under this Rules"
        )

    length = values["chamber_length"]
    slots: list[Shell | None] = []
    inverted0 = length > 0 and values["slot0"] == _SLOT_INVERTED
    for i in range(length):
        slot_code = values[f"slot{i}"]
        if slot_code == _SLOT_UNRESOLVED or (i == 0 and inverted0):
            slots.append(None)
        elif slot_code == _SLOT_LIVE:
            slots.append(Shell.LIVE)
        elif slot_code == _SLOT_BLANK:
            slots.append(Shell.BLANK)
        else:
            raise ValueError(f"memo_key.decode: slot {i} has invalid code {slot_code}")
    chamber = ChamberState(
        tuple(slots), values["unknown_live"], values["unknown_blank"], inverted0
    )

    mask = values["dealer_knows_mask"]
    dealer_knows = frozenset(i for i in range(w.max_total) if mask & (1 << i))

    items: list[ItemCounts] = []
    for side in ("player", "dealer"):
        counts = tuple(values[f"item_{side}_{item.name}"] for item in Item)
        items.append(ItemCounts(counts))

    return GameState(
        chamber=chamber,
        hp=(values["hp_player"], values["hp_dealer"]),
        max_hp=values["max_hp"],
        items=(items[0], items[1]),
        sawed=bool(values["sawed"]),
        cuffed=(values["cuffed_player"], values["cuffed_dealer"]),
        turn=Player(values["turn"]),
        dealer_knows=dealer_knows,
        items_used_this_turn=values["items_used_this_turn"],
    ).canonical()
