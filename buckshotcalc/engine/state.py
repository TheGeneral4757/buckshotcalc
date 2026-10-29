"""GameState, ChamberState, canonicalization, hashing (SPEC.md §5).

Pure stdlib. Every object here is a frozen, slotted dataclass (or IntEnum) so
that a `GameState` is directly usable as an `lru_cache` key: no `list`,
`dict`, or `set` is reachable from it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import IntEnum

CUFF_SPENT = 2  # GameState.cuffed value: the skip is taken, the cuff clears at the next turn pass


class Shell(IntEnum):
    """One shotgun shell (SPEC.md §5)."""

    BLANK = 0
    LIVE = 1


class Player(IntEnum):
    """The two heads-up seats. v1 only; multiplayer (v2+) extends this."""

    PLAYER = 0
    DEALER = 1

    @property
    def other(self) -> Player:
        """The opposing seat."""
        return Player.DEALER if self is Player.PLAYER else Player.PLAYER


class GameMode(IntEnum):
    """Which item set governs a reload draw (SPEC.md §3.2, §7.4).

    Not itself a `GameState` field — the mode is fixed for the lifetime of a
    `Solver` instance (one `(rules, mode)` pair per solver), not a per-state
    property, so it never needs to be part of the memo key.
    """

    BASE = 0
    DOUBLE_OR_NOTHING = 1

    @property
    def config_name(self) -> str:
        """The `rules.toml` key for this mode, e.g. `"double_or_nothing"`."""
        return self.name.lower()

    @classmethod
    def from_config_name(cls, name: str) -> GameMode:
        """Look up a `GameMode` by its `rules.toml` key.

        Raises `KeyError` if `name` is not a known mode's `config_name`.
        """
        try:
            return _CONFIG_NAME_TO_MODE[name]
        except KeyError:
            raise KeyError(f"unknown mode name: {name!r}") from None


_CONFIG_NAME_TO_MODE: dict[str, GameMode] = {mode.config_name: mode for mode in GameMode}


class Item(IntEnum):
    """All ten items, in `rules.toml` declaration order (SPEC.md §3.2)."""

    CIGARETTES = 0
    HANDCUFFS = 1
    BEER = 2
    HANDSAW = 3
    MAGNIFYING_GLASS = 4
    ADRENALINE = 5
    EXPIRED_MEDICINE = 6
    BURNER_PHONE = 7
    INVERTER = 8
    REMOTE = 9

    @property
    def config_name(self) -> str:
        """The `rules.toml` key for this item, e.g. `"magnifying_glass"`."""
        return self.name.lower()

    @classmethod
    def from_config_name(cls, name: str) -> Item:
        """Look up an `Item` by its `rules.toml` key.

        Raises `KeyError` if `name` is not a known item's `config_name`.
        """
        try:
            return _CONFIG_NAME_TO_ITEM[name]
        except KeyError:
            raise KeyError(f"unknown item name: {name!r}") from None


_CONFIG_NAME_TO_ITEM: dict[str, Item] = {item.config_name: item for item in Item}

_ITEM_COUNT = len(Item)


@dataclass(frozen=True, slots=True)
class ItemCounts:
    """A count vector over `Item`, indexed by `Item.value`.

    Deliberately **not** a list of held items — item order is irrelevant to
    the game, and treating it as ordered would multiply the state space for
    no informational gain (SPEC.md §5.3).
    """

    counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.counts) != _ITEM_COUNT:
            raise ValueError(
                f"ItemCounts.counts must have length {_ITEM_COUNT} (one per Item), "
                f"got {len(self.counts)}"
            )
        for item, n in zip(Item, self.counts, strict=True):
            if n < 0:
                raise ValueError(f"ItemCounts: negative count for {item.name}: {n}")

    @classmethod
    def empty(cls) -> ItemCounts:
        return cls(tuple(0 for _ in Item))

    @classmethod
    def of(cls, **by_config_name: int) -> ItemCounts:
        """Build from keyword args named by `Item.config_name`, e.g. `cigarettes=1`."""
        return cls.from_mapping(
            {Item.from_config_name(name): n for name, n in by_config_name.items()}
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[Item, int]) -> ItemCounts:
        counts = [0] * _ITEM_COUNT
        for item, n in mapping.items():
            counts[item.value] = n
        return cls(tuple(counts))

    def __getitem__(self, item: Item) -> int:
        return self.counts[item.value]

    def with_delta(self, item: Item, delta: int) -> ItemCounts:
        """Return a copy with `item`'s count adjusted by `delta`.

        Raises `ValueError` if the result would be negative.
        """
        new = self.counts[item.value] + delta
        if new < 0:
            raise ValueError(
                f"ItemCounts.with_delta: {item.name} count would go negative "
                f"({self.counts[item.value]} + {delta} = {new})"
            )
        counts = list(self.counts)
        counts[item.value] = new
        return ItemCounts(tuple(counts))

    def total(self) -> int:
        return sum(self.counts)

    def held(self) -> Iterator[tuple[Item, int]]:
        """Yield `(item, count)` for every item held in nonzero quantity."""
        for item, n in zip(Item, self.counts, strict=True):
            if n > 0:
                yield item, n

    def is_full(self, max_slots: int) -> bool:
        return self.total() >= max_slots


def _other(shell: Shell) -> Shell:
    return Shell.BLANK if shell == Shell.LIVE else Shell.LIVE


@dataclass(frozen=True, slots=True)
class ChamberState:
    """Partial knowledge of the remaining shell sequence (SPEC.md §5).

    `slots[0]` is the chambered shell. `None` means unresolved; every
    unresolved slot is equally likely to hold any unresolved shell drawn
    uniformly from the pool (`unknown_live`, `unknown_blank`).

    Invariant: `slots.count(None) == unknown_live + unknown_blank`.

    `inverted0` (SPEC.md §5.2): slot 0 is unresolved and was flipped by an odd
    number of Inverters nobody saw the result of. The pool still counts its
    *original* shell, so the chambered shell is the opposite of a pool draw:
    `p_live` reads the blank share, and seeing X there removes a not-X from the
    pool. The game shows nothing on an Inverter, so this is never a reveal.
    """

    slots: tuple[Shell | None, ...]
    unknown_live: int
    unknown_blank: int
    inverted0: bool = False

    def __post_init__(self) -> None:
        if self.unknown_live < 0 or self.unknown_blank < 0:
            raise ValueError(
                "ChamberState: pool counts must be non-negative, got "
                f"unknown_live={self.unknown_live}, unknown_blank={self.unknown_blank}"
            )
        none_count = self.slots.count(None)
        pool = self.unknown_live + self.unknown_blank
        if none_count != pool:
            raise ValueError(
                "ChamberState invariant violated: "
                f"slots.count(None) == {none_count} but "
                f"unknown_live + unknown_blank == {pool}"
            )
        if self.inverted0 and (not self.slots or self.slots[0] is not None):
            raise ValueError("ChamberState: inverted0 requires an unresolved slot 0")

    @classmethod
    def create(
        cls, slots: tuple[Shell | None, ...], unknown_live: int, unknown_blank: int
    ) -> ChamberState:
        """Build and immediately close (`deduce`) — the normal entry point."""
        return cls(slots, unknown_live, unknown_blank).deduce()

    def p_live(self) -> float:
        """P(chambered shell is live) (SPEC.md §5).

        Precondition: the chamber is non-empty. An empty chamber raises
        `IndexError`; SPEC.md §5's snippet indexes `slots[0]` unconditionally
        and leaves the empty case undefined, and the solver checks
        `is_empty` (a reload node, SPEC.md §7.4) before it ever asks.
        """
        if self.slots[0] is not None:
            return float(self.slots[0] == Shell.LIVE)
        total = self.unknown_live + self.unknown_blank
        return (self.unknown_blank if self.inverted0 else self.unknown_live) / total

    def total_live(self) -> int:
        """Resolved live shells plus the live shells still in the pool.

        With `inverted0` this counts slot 0's pre-flip shell, so it is not the
        true remaining total; `dealer_step` resolves slot 0 before the Dealer
        counts.
        """
        return sum(1 for s in self.slots if s == Shell.LIVE) + self.unknown_live

    def total_blank(self) -> int:
        """Resolved blank shells plus the blank shells still in the pool."""
        return sum(1 for s in self.slots if s == Shell.BLANK) + self.unknown_blank

    def __len__(self) -> int:
        return len(self.slots)

    @property
    def is_empty(self) -> bool:
        return len(self.slots) == 0

    def resolve(self, index: int, shell: Shell) -> ChamberState:
        """Mark `slots[index]` as `shell`, decrementing the matching pool.

        Raises `ValueError` if that slot is already resolved or the pool has
        no shells of that type left.
        """
        if self.slots[index] is not None:
            raise ValueError(f"ChamberState.resolve: slot {index} is already resolved")
        # Seeing `shell` on an inverted slot 0 means its pre-flip original, the
        # shell the pool still counts, was the other type.
        drawn = _other(shell) if index == 0 and self.inverted0 else shell
        if drawn == Shell.LIVE:
            if self.unknown_live <= 0:
                raise ValueError("ChamberState.resolve: no live shells left in the pool")
            unknown_live, unknown_blank = self.unknown_live - 1, self.unknown_blank
        else:
            if self.unknown_blank <= 0:
                raise ValueError("ChamberState.resolve: no blank shells left in the pool")
            unknown_live, unknown_blank = self.unknown_live, self.unknown_blank - 1
        new_slots = tuple(shell if i == index else s for i, s in enumerate(self.slots))
        inverted0 = self.inverted0 and index != 0
        return ChamberState(new_slots, unknown_live, unknown_blank, inverted0).deduce()

    def pop(self) -> ChamberState:
        """Drop slot 0. It must already be resolved — callers resolve first.

        Later slots renumber automatically since `slots` is a sequence; this
        is exactly what makes Burner Phone's relative indexing (SPEC.md §3.2)
        fall out for free.
        """
        if self.slots[0] is None:
            raise ValueError("ChamberState.pop: slot 0 is unresolved; resolve it first")
        return ChamberState(self.slots[1:], self.unknown_live, self.unknown_blank).deduce()

    def deduce(self) -> ChamberState:
        """Close the state: if the pool is all one type, resolve every remaining slot.

        This is a genuine information source — once the last live (or blank)
        shell is known spent, every other unresolved slot is known too
        (SPEC.md §5 commentary).
        """
        if self.unknown_live == 0 and self.unknown_blank == 0:
            return self  # nothing left to deduce
        if self.inverted0 and (self.unknown_live == 0 or self.unknown_blank == 0):
            # The pool is one type, so slot 0's original is too: it now holds the other.
            pool_type = Shell.BLANK if self.unknown_live == 0 else Shell.LIVE
            rest = tuple(pool_type if s is None else s for s in self.slots[1:])
            return ChamberState((_other(pool_type),) + rest, 0, 0)
        if self.unknown_live == 0:
            new_slots = tuple(Shell.BLANK if s is None else s for s in self.slots)
            return ChamberState(new_slots, 0, 0)
        if self.unknown_blank == 0:
            new_slots = tuple(Shell.LIVE if s is None else s for s in self.slots)
            return ChamberState(new_slots, 0, 0)
        return self


@dataclass(frozen=True, slots=True)
class GameState:
    """Full game state (SPEC.md §5.3) — its own memoization key.

    `dealer_knows` is the set of slot indices the **Dealer** has
    independently resolved (e.g. via its own Magnifying Glass, or the
    "only one shell remains" rule). It is tracked completely separately
    from `chamber` — which reflects *our* knowledge — and the two must
    never be conflated: a slot can be in `dealer_knows` while
    `chamber.slots[i]` is still `None` for us. Leaking our knowledge into
    the Dealer's decisions, or vice versa, silently makes every downstream
    recommendation wrong (SPEC.md §7.3).
    """

    chamber: ChamberState
    hp: tuple[int, int]  # (player, dealer)
    max_hp: int
    items: tuple[ItemCounts, ItemCounts]
    sawed: bool  # chambered shell deals doubled damage
    # Per side: 0/False = free, 1/True = cuffed (skips its next turn), 2 = cuffed and the skip
    # is spent: still cuffed through the opponent's bonus turn, cleared at the next turn pass
    # (SPEC.md §3.2, RoundManager.gd:308-326).
    cuffed: tuple[int, int]
    turn: Player
    dealer_knows: frozenset[int]
    items_used_this_turn: int  # guards the Adrenaline chain (SPEC.md §7.5)

    def __post_init__(self) -> None:
        if len(self.hp) != 2:
            raise ValueError(f"GameState.hp must have length 2, got {len(self.hp)}")
        if len(self.items) != 2:
            raise ValueError(f"GameState.items must have length 2, got {len(self.items)}")
        if len(self.cuffed) != 2:
            raise ValueError(f"GameState.cuffed must have length 2, got {len(self.cuffed)}")
        if self.items_used_this_turn < 0:
            raise ValueError(
                "GameState.items_used_this_turn must be non-negative, "
                f"got {self.items_used_this_turn}"
            )

    @classmethod
    def create(
        cls,
        chamber: ChamberState,
        hp: tuple[int, int],
        max_hp: int,
        turn: Player,
        items: tuple[ItemCounts, ItemCounts] | None = None,
        sawed: bool = False,
        cuffed: tuple[int, int] = (0, 0),
        # A bare `frozenset()` default is safe precisely because it is
        # immutable — the shared-mutable-default trap needs a mutable object.
        dealer_knows: frozenset[int] = frozenset(),
        items_used_this_turn: int = 0,
    ) -> GameState:
        """The normal entry point — builds, then canonicalizes (SPEC.md §5.3).

        Only the four fields with no sensible default are required. The rest
        default to the start of a fresh load: no items, no saw, no cuffs, the
        Dealer knowing nothing, and no items spent this turn.
        """
        if items is None:
            items = (ItemCounts.empty(), ItemCounts.empty())
        raw = cls(
            chamber, hp, max_hp, items, sawed, cuffed, turn, dealer_knows, items_used_this_turn
        )
        return raw.canonical()

    # -- read-only helpers ---------------------------------------------------

    def hp_of(self, player: Player) -> int:
        return self.hp[player]

    def items_of(self, player: Player) -> ItemCounts:
        return self.items[player]

    def is_cuffed(self, player: Player) -> bool:
        return self.cuffed[player] != 0

    def winner(self) -> Player | None:
        """The round's winner, or `None` if both players are still alive.

        Checked Dealer-first per SPEC.md §7.1's `value()` sketch. A single
        action damages exactly one player, so both HPs reaching 0
        simultaneously is unreachable — the ordering therefore never
        actually matters, but the Dealer-first check is kept to match the
        reference algorithm exactly.
        """
        if self.hp[Player.DEALER] <= 0:
            return Player.PLAYER
        if self.hp[Player.PLAYER] <= 0:
            return Player.DEALER
        return None

    # -- replace helpers (dataclasses.replace, re-canonicalized) ------------

    def _replace(self, **changes: object) -> GameState:
        return replace(self, **changes).canonical()

    def with_chamber(self, chamber: ChamberState) -> GameState:
        return self._replace(chamber=chamber)

    def with_hp(self, player: Player, value: int) -> GameState:
        hp = (value, self.hp[1]) if player == Player.PLAYER else (self.hp[0], value)
        return self._replace(hp=hp)

    def with_items(self, player: Player, counts: ItemCounts) -> GameState:
        items = (
            (counts, self.items[1]) if player == Player.PLAYER else (self.items[0], counts)
        )
        return self._replace(items=items)

    def with_sawed(self, sawed: bool) -> GameState:
        return self._replace(sawed=sawed)

    def with_cuffed(self, player: Player, value: bool | int) -> GameState:
        cuffed = (
            (value, self.cuffed[1]) if player == Player.PLAYER else (self.cuffed[0], value)
        )
        return self._replace(cuffed=cuffed)

    def with_turn(self, turn: Player) -> GameState:
        return self._replace(turn=turn)

    def with_dealer_knows(self, dealer_knows: frozenset[int]) -> GameState:
        return self._replace(dealer_knows=dealer_knows)

    def with_items_used_this_turn(self, value: int) -> GameState:
        return self._replace(items_used_this_turn=value)

    # -- canonicalization (SPEC.md §5.3, mandatory) --------------------------

    def canonical(self) -> GameState:
        """Reduce to canonical form so equal positions share a memo key.

        Implemented as a factory rather than mutation in `__post_init__`
        because the dataclass is frozen.
        """
        # Rule: if no slot is resolved, `slots` carries no information beyond
        # its length — every unresolved slot is already `None` by
        # construction, so there is nothing to transform here; this is
        # exercised directly by the property test in tests/test_state.py.
        chamber = self.chamber.deduce()  # close any pool that has gone all-one-type

        n = len(chamber.slots)
        # Stale dealer-knowledge indices (from before a pop shrank the
        # chamber) refer to nothing and must be dropped so they don't
        # fragment the memo table.
        dealer_knows = frozenset(i for i in self.dealer_knows if i < n)

        # An empty chamber has nothing chambered, so "sawed" is meaningless —
        # force it False so it can't split states that are otherwise equal.
        sawed = self.sawed if n > 0 else False

        # Clamp HP at 0 so a transient -1 (mid-transition, before the
        # terminal check runs) and 0 share the same memo key.
        hp = (max(self.hp[0], 0), max(self.hp[1], 0))

        if (
            chamber == self.chamber
            and dealer_knows == self.dealer_knows
            and sawed == self.sawed
            and hp == self.hp
        ):
            return self
        return GameState(
            chamber,
            hp,
            self.max_hp,
            self.items,
            sawed,
            self.cuffed,
            self.turn,
            dealer_knows,
            self.items_used_this_turn,
        )
