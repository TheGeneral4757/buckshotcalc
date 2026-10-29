"""Action generation and transitions (SPEC.md §6).

Pure stdlib. This module turns a `GameState` into the set of legal `Action`s
and, for each, the probability-weighted distribution of successor states.
Every value that the game defines (damage, heal amounts, the item-use cap)
comes from `Rules` — never a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.state import CUFF_SPENT, ChamberState, GameState, Item, Player, Shell


class ActionKind(IntEnum):
    SHOOT_SELF = 0
    SHOOT_OPPONENT = 1
    USE = 2


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    item: Item | None = None  # set iff kind is USE
    steal: Item | None = None  # ADRENALINE only; None = spent with nothing to take

    def __str__(self) -> str:
        """Seat-neutral rendering.

        `SHOOT_OPPONENT` is deliberately rendered as "SHOOT OPPONENT" here,
        not "SHOOT DEALER" — this action fires from *either* seat (the
        Dealer takes it against us too), and hardcoding the player's
        opponent's name here was a defect (see the M7 milestone brief). A
        seat-aware label for the operator's own display lives in
        `buckshotcalc.tui.analysis.action_label`, not here.
        """
        if self.kind is ActionKind.SHOOT_SELF:
            return "SHOOT SELF"
        if self.kind is ActionKind.SHOOT_OPPONENT:
            return "SHOOT OPPONENT"
        assert self.item is not None
        if self.item is Item.ADRENALINE:
            if self.steal is None:
                return "ADRENALINE (nothing to take)"
            return f"ADRENALINE -> {self.steal.name}"
        return f"USE {self.item.name}"


Outcome = tuple[float, GameState]


# ---------------------------------------------------------------------------
# Shared helpers — the single place every slot-0 pop and every turn-pass runs.
# ---------------------------------------------------------------------------


def _resolve_and_pop(chamber: ChamberState, shell: Shell) -> ChamberState:
    """Resolve slot 0 to `shell` (no-op if already resolved to it) and pop it.

    The one and only place that pops a shell. Renumbering `dealer_knows` after
    a pop is the caller's job (`_pop_and_renumber_dealer_knows`) — this helper
    only knows about the chamber, not the full `GameState`.
    """
    if chamber.slots[0] is None:
        chamber = chamber.resolve(0, shell)
    return chamber.pop()


def _renumber_dealer_knows(dealer_knows: frozenset[int]) -> frozenset[int]:
    """Slot 0 leaves with the popped shell; everything above shifts down.

    `frozenset(i - 1 for i in dealer_knows if i > 0)` per the milestone spec —
    also matches `GameState.canonical`'s stale-index cleanup, which subsumes
    this for indices that fall outside the new (shorter) chamber.
    """
    return frozenset(i - 1 for i in dealer_knows if i > 0)


def _pop_slot0(state: GameState, shell: Shell) -> GameState:
    """Resolve+pop slot 0 to `shell`, renumber `dealer_knows`, clear `sawed`.

    Routed through by every action that consumes the chambered shell: both
    shots, and Beer (which then restores `sawed`, see `_use_beer`). This is
    the single helper SPEC.md's milestone brief warns must never be duplicated.
    """
    chamber = _resolve_and_pop(state.chamber, shell)
    dealer_knows = _renumber_dealer_knows(state.dealer_knows)
    return state.with_chamber(chamber).with_dealer_knows(dealer_knows).with_sawed(False)


def _end_turn(state: GameState) -> GameState:
    """Pass the turn to the mover's opponent, handling the cuff bounce-back.

    VERIFIED (RoundManager.gd:308-326, DealerIntelligence.gd:39-56): the cuffed
    side's first turn is skipped and the cuff persists through the mover's
    bonus turn; it clears only on the following turn pass (SPEC.md §3.2).

    A genuine turn boundary, so `items_used_this_turn` resets to 0 — including
    on the cuff bounce-back, where the mover gets a fresh turn rather than a
    continuation of the one they just ended (SPEC.md §6.1's turn-end rule).
    """
    next_player = state.turn.other
    cuff = state.cuffed[next_player]
    if cuff == 1:
        # First pass: the cuffed player skips, the mover takes a bonus turn, and
        # the cuff STAYS (spent) so it cannot be re-applied during that turn.
        state = state.with_cuffed(next_player, CUFF_SPENT)
        return state.with_turn(state.turn).with_items_used_this_turn(0)
    if cuff == CUFF_SPENT:
        # Second pass (the game's "break free"): clear it and the player acts.
        state = state.with_cuffed(next_player, False)
    return state.with_turn(next_player).with_items_used_this_turn(0)


def _retain_turn(state: GameState) -> GameState:
    """The mover continues *within the same turn* — an item use (SPEC.md §6.1).

    Deliberately does **not** touch `items_used_this_turn`: the whole point of
    that counter is to bound a chain of item uses inside one turn, so resetting
    it here would stop the §7.5 cap from ever binding. The counter is charged
    by `_apply_use` and reset only at genuine turn boundaries — `_end_turn`,
    and `_new_turn_same_mover` after a blank self-shot.

    Identity today, and kept anyway so every transition names the turn rule it
    is applying instead of leaving it implicit.
    """
    return state


def _new_turn_same_mover(state: GameState) -> GameState:
    """A blank self-shot: the mover shoots again, but as a *fresh* turn.

    The shot ended the action, so the item-use budget refills — distinct from
    `_retain_turn`, which continues the turn already in progress.
    """
    return state.with_items_used_this_turn(0)


# ---------------------------------------------------------------------------
# Legality
# ---------------------------------------------------------------------------


def item_cap_reached(state: GameState, rules: Rules) -> bool:
    """True once `items_used_this_turn` has hit `rules.solver.max_items_per_turn`."""
    return state.items_used_this_turn >= rules.solver.max_items_per_turn


def _item_precondition(state: GameState, player: Player, item: Item) -> bool:
    """Whether `item`'s USE precondition holds for `player` right now.

    Does not check holding-count or the item-use cap — callers check those
    separately (holding count in `legal_actions`/adrenaline-stealability,
    the cap in `legal_actions` only, since a stolen item's own USE bypasses
    the cap check by design: the cap already charged 2 for the whole steal).
    None of these preconditions depend on `rules`, so it is not a parameter.
    """
    if item is Item.HANDCUFFS:
        return not state.is_cuffed(player.other)
    if item is Item.HANDSAW:
        return not state.sawed
    if item is Item.ADRENALINE:
        return _adrenaline_stealable_items(state, player) != ()
    # cigarettes, beer, magnifying_glass, burner_phone, inverter,
    # expired_medicine: no precondition beyond holding the item.
    return True


def _adrenaline_stealable_items(state: GameState, player: Player) -> tuple[Item, ...]:
    """Distinct item types adrenaline may steal, in `Item` enum order.

    Precondition (SPEC.md §6.1, and the milestone brief's tightening of it):
    the opponent holds >=1 item that is not Adrenaline itself, AND whose own
    USE precondition also holds — the stolen item is used immediately, so an
    unusable steal is not offered.
    """
    opponent = player.other
    out = []
    for item, count in state.items_of(opponent).held():
        if item is Item.ADRENALINE:
            continue
        if count <= 0:
            continue
        if _item_precondition(state, player, item):
            out.append(item)
    return tuple(out)


def legal_actions(state: GameState, rules: Rules) -> tuple[Action, ...]:
    """Legal actions at `state` for the player to move, in stable order.

    Order (documented, depended on by the TUI's ranked display): SHOOT_SELF,
    SHOOT_OPPONENT, then USE in `Item` enum order, then adrenaline-steal
    variants in `Item` order of the stolen item.
    """
    actions: list[Action] = [
        Action(ActionKind.SHOOT_SELF),
        Action(ActionKind.SHOOT_OPPONENT),
    ]

    if item_cap_reached(state, rules):
        return tuple(actions)

    player = state.turn
    held = state.items_of(player)

    for item in Item:
        if held[item] <= 0:
            continue
        if item is Item.ADRENALINE:
            stealable = _adrenaline_stealable_items(state, player)
            for stolen in stealable:
                actions.append(Action(ActionKind.USE, Item.ADRENALINE, stolen))
            if not stealable and rules.item_semantics.adrenaline.usable_with_nothing_to_steal:
                actions.append(Action(ActionKind.USE, Item.ADRENALINE))
            continue
        if not _item_precondition(state, player, item):
            continue
        actions.append(Action(ActionKind.USE, item))

    return tuple(actions)


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def apply(state: GameState, action: Action, rules: Rules) -> tuple[Outcome, ...]:
    """The probability-weighted distribution of successors of `action` at `state`.

    Never returns an empty tuple: a deterministic action is a one-element
    distribution with weight 1.0.
    """
    if action.kind is ActionKind.SHOOT_SELF:
        return _apply_shoot_self(state, rules)
    if action.kind is ActionKind.SHOOT_OPPONENT:
        return _apply_shoot_opponent(state, rules)
    assert action.item is not None
    if action.item is Item.ADRENALINE:
        return _apply_adrenaline(state, action, rules)
    return _apply_use(state, action.item, rules, count_delta=1)


def _damage(state: GameState, rules: Rules) -> int:
    return rules.item_semantics.handsaw.damage if state.sawed else rules.turns.base_damage


def _apply_shoot_self(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    """SHOOT_SELF (SPEC.md §6.1): chance node on slot 0.

    The target is `state.turn` — whoever is moving. Never a fixed seat: the
    Dealer shoots itself constantly under its own §3.3 policy, and hardcoding
    the player here would bill the operator for the Dealer's self-inflicted
    damage.
    """
    shooter = state.turn
    chamber = state.chamber
    p_live = chamber.p_live()
    outcomes: list[Outcome] = []

    if p_live < 1.0:
        blank_state = _pop_slot0(state, Shell.BLANK)
        blank_state = _new_turn_same_mover(blank_state)
        outcomes.append((1.0 - p_live, blank_state))

    if p_live > 0.0:
        dmg = _damage(state, rules)
        live_state = _pop_slot0(state, Shell.LIVE)
        hp = max(live_state.hp_of(shooter) - dmg, 0)
        live_state = live_state.with_hp(shooter, hp)
        if live_state.winner() is None:
            live_state = _end_turn(live_state)
        outcomes.append((p_live, live_state))

    return tuple(outcomes)


def _apply_shoot_opponent(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    """SHOOT_OPPONENT (SPEC.md §6.1): chance node on slot 0; turn always passes."""
    chamber = state.chamber
    p_live = chamber.p_live()
    dmg = _damage(state, rules)
    outcomes: list[Outcome] = []
    target = state.turn.other

    if p_live < 1.0:
        blank_state = _pop_slot0(state, Shell.BLANK)
        blank_state = _end_turn(blank_state)
        outcomes.append((1.0 - p_live, blank_state))

    if p_live > 0.0:
        live_state = _pop_slot0(state, Shell.LIVE)
        hp = max(live_state.hp_of(target) - dmg, 0)
        live_state = live_state.with_hp(target, hp)
        if live_state.winner() is None:
            live_state = _end_turn(live_state)
        outcomes.append((p_live, live_state))

    return tuple(outcomes)


def _apply_use(
    state: GameState, item: Item, rules: Rules, count_delta: int
) -> tuple[Outcome, ...]:
    """Dispatch a `USE(item)` to its transition, charge the cap, route the turn.

    The per-item `_use_*` helpers below return the *board* effect only. Turn
    routing and the item-use counter are applied here, in one place, so the two
    concerns cannot drift apart per item.

    `count_delta` lets adrenaline's stolen-item application charge the shared
    counter for the whole steal (see `_apply_adrenaline`).
    """
    player = state.turn
    items = state.items_of(player).with_delta(item, -1)
    state = state.with_items(player, items)

    if item is Item.CIGARETTES:
        outcomes = _use_cigarettes(state, rules)
    elif item is Item.HANDCUFFS:
        outcomes = _use_handcuffs(state)
    elif item is Item.BEER:
        outcomes = _use_beer(state, rules)
    elif item is Item.HANDSAW:
        outcomes = _use_handsaw(state)
    elif item is Item.MAGNIFYING_GLASS:
        outcomes = _use_magnifying_glass(state)
    elif item is Item.BURNER_PHONE:
        outcomes = _use_burner_phone(state, rules)
    elif item is Item.INVERTER:
        outcomes = _use_inverter(state)
    elif item is Item.EXPIRED_MEDICINE:
        outcomes = _use_expired_medicine(state, rules)
    elif item is Item.ADRENALINE:
        outcomes = ((1.0, state),)  # the wasted Adrenaline; see _apply_adrenaline
    else:
        raise AssertionError(f"_apply_use: unhandled item {item!r}")

    # SPEC.md §6.1 states every item use retains the turn, and `rules.toml`
    # carries that per item as `retains_turn`. Read it rather than hardcode it
    # (SPEC.md §0.1 (2)): the engine must still be correct if the
    # file is corrected. Charge the counter first, so an item configured to end
    # the turn resets it afterwards rather than carrying a stale charge over.
    retains_turn = rules.item_semantics.for_item(item).retains_turn

    routed: list[Outcome] = []
    charged = state.items_used_this_turn + count_delta
    for weight, successor in outcomes:
        successor = successor.with_items_used_this_turn(charged)
        if retains_turn:
            successor = _retain_turn(successor)
        elif successor.winner() is None:
            successor = _end_turn(successor)
        routed.append((weight, successor))
    return tuple(routed)


def _use_cigarettes(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    player = state.turn
    cfg = rules.item_semantics.cigarettes
    hp = min(state.hp_of(player) + cfg.heal, state.max_hp)
    return ((1.0, state.with_hp(player, hp)),)


def _use_handcuffs(state: GameState) -> tuple[Outcome, ...]:
    opponent = state.turn.other
    new_state = state.with_cuffed(opponent, True)
    return ((1.0, new_state),)


def _use_beer(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    """Resolve slot 0, pop it — chance node. The saw survives unless `beer.clears_sawed`.

    VERIFIED (SPEC.md §3.2): only a shot resets the saw, so with the shipped
    `clears_sawed = false` saw then beer keeps the saw. If the pop empties the
    chamber, `GameState.canonical` still zeroes `sawed` (SPEC.md §15.14).
    Turn routing is applied by `_apply_use`, not here.
    """
    chamber = state.chamber
    p_live = chamber.p_live()
    keep = not rules.item_semantics.beer.clears_sawed
    outcomes: list[Outcome] = []
    for weight, shell in ((1.0 - p_live, Shell.BLANK), (p_live, Shell.LIVE)):
        if weight > 0.0:
            popped = _pop_slot0(state, shell)
            outcomes.append((weight, popped.with_sawed(state.sawed) if keep else popped))
    return tuple(outcomes)


def _use_handsaw(state: GameState) -> tuple[Outcome, ...]:
    return ((1.0, state.with_sawed(True)),)


def _use_magnifying_glass(state: GameState) -> tuple[Outcome, ...]:
    """Resolve slot 0 (chance node); chamber otherwise untouched.

    Turn routing is applied by `_apply_use`, not here.
    """
    chamber = state.chamber
    if chamber.slots[0] is not None:
        return ((1.0, state),)
    p_live = chamber.p_live()
    outcomes: list[Outcome] = []
    if p_live < 1.0:
        new_chamber = chamber.resolve(0, Shell.BLANK)
        outcomes.append((1.0 - p_live, state.with_chamber(new_chamber)))
    if p_live > 0.0:
        new_chamber = chamber.resolve(0, Shell.LIVE)
        outcomes.append((p_live, state.with_chamber(new_chamber)))
    return tuple(outcomes)


def _use_burner_phone(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    """The player's phone: a draw over `1 .. len - 1`, then the game's index clamp, then reveal.

    VERIFIED (BurnerPhone.gd:14-16): `randi_range(1, len - 1)`, and an index
    `>= burner_phone.player_index_decrement_from` is shifted down by one, so
    the slot distribution is NOT uniform when the chamber is long enough for
    the clamp to bite (SPEC.md §3.2). A draw may land on an already-resolved
    slot: that is a no-op branch (it wastes the phone), not an excluded one.
    The Dealer's own phone is resolved in `dealer.py` and is untouched here.

    Legal (and a no-op) even with no non-chambered slot at all.
    """
    chamber = state.chamber
    n = len(chamber.slots)
    if n < 2:
        return ((1.0, state),)

    cut = rules.item_semantics.burner_phone.player_index_decrement_from
    p_raw = 1.0 / (n - 1)
    slot_weight: dict[int, float] = {}
    for raw in range(1, n):
        idx = raw - 1 if raw >= cut else raw
        slot_weight[idx] = slot_weight.get(idx, 0.0) + p_raw

    outcomes: list[Outcome] = []
    for idx, p_slot in slot_weight.items():
        if chamber.slots[idx] is not None:
            outcomes.append((p_slot, state))
            continue
        total = chamber.unknown_live + chamber.unknown_blank
        if chamber.unknown_live > 0:
            new_chamber = chamber.resolve(idx, Shell.LIVE)
            weight = p_slot * (chamber.unknown_live / total)
            outcomes.append((weight, state.with_chamber(new_chamber)))
        if chamber.unknown_blank > 0:
            new_chamber = chamber.resolve(idx, Shell.BLANK)
            weight = p_slot * (chamber.unknown_blank / total)
            outcomes.append((weight, state.with_chamber(new_chamber)))
    return tuple(outcomes)


def _set_slot0(
    chamber: ChamberState, shell: Shell, unknown_live: int, unknown_blank: int
) -> ChamberState:
    """Build a chamber with slot 0 forced to `shell` and the given pool counts.

    Used only by the Inverter, whose resolution is not "reveal the true
    value" (what `ChamberState.resolve` models) but "resolve to the
    *opposite* of the drawn type" — so we construct the result directly
    rather than routing through `resolve`.
    """
    new_slots = tuple(shell if i == 0 else s for i, s in enumerate(chamber.slots))
    return ChamberState(new_slots, unknown_live, unknown_blank).deduce()


def _use_inverter(state: GameState) -> tuple[Outcome, ...]:
    """SPEC.md §5.2: a plain flip of a resolved slot 0, an unseen flip of an unresolved one.

    VERIFIED (ItemInteraction.gd:165-170): the game flips `sequenceArray[0]` and
    shows nothing, so inverting an unresolved shell reveals nothing either. It
    toggles `inverted0` rather than branching: branching would hand the
    inverting seat the shell's value for free, two Inverters in a row would be
    a Magnifying Glass, and every line through it would read too high.
    """
    chamber = state.chamber
    if chamber.slots[0] is not None:
        # Already resolved: plain flip, no pool change.
        flipped = Shell.BLANK if chamber.slots[0] == Shell.LIVE else Shell.LIVE
        new_chamber = _set_slot0(chamber, flipped, chamber.unknown_live, chamber.unknown_blank)
        return ((1.0, state.with_chamber(new_chamber)),)
    new_chamber = ChamberState(
        chamber.slots, chamber.unknown_live, chamber.unknown_blank, not chamber.inverted0
    )
    return ((1.0, state.with_chamber(new_chamber)),)


def _use_expired_medicine(state: GameState, rules: Rules) -> tuple[Outcome, ...]:
    """Chance node on `heal_probability`. May reduce HP to 0 — not clamped away."""
    player = state.turn
    cfg = rules.item_semantics.expired_medicine
    outcomes: list[Outcome] = []
    if cfg.heal_probability > 0.0:
        hp = min(state.hp_of(player) + cfg.heal, state.max_hp)
        outcomes.append((cfg.heal_probability, state.with_hp(player, hp)))
    if cfg.heal_probability < 1.0:
        hp = state.hp_of(player) - cfg.harm  # canonicalization clamps at 0 (terminal)
        outcomes.append(
            (1.0 - cfg.heal_probability, state.with_hp(player, hp))
        )
    return tuple(outcomes)


def _apply_adrenaline(state: GameState, action: Action, rules: Rules) -> tuple[Outcome, ...]:
    """Steal `action.steal` from the opponent and immediately apply its USE.

    One `Action` per distinct stealable item type — the choice of what to
    steal is the acting player's decision. Applying it transfers the item,
    then charges the item-use cap by 2 (judgment call — see the comment
    below) and applies the stolen item's own USE distribution.
    """
    if action.steal is None:
        # Spent with nothing to take (rules.toml usable_with_nothing_to_steal):
        # only the Adrenaline leaves the rack and the turn is kept.
        return _apply_use(state, Item.ADRENALINE, rules, count_delta=1)
    player = state.turn
    opponent = player.other
    stolen = action.steal

    # The Adrenaline itself is consumed (this call bypasses `_apply_use`, so
    # nothing else removes it), then the stolen item is transferred onto the
    # player's rack so `_apply_use` below can consume it exactly as if the
    # player had held and used it directly.
    own_items = state.items_of(player).with_delta(Item.ADRENALINE, -1).with_delta(stolen, 1)
    opp_items = state.items_of(opponent).with_delta(stolen, -1)
    transferred = state.with_items(player, own_items).with_items(opponent, opp_items)

    # Judgment call (architect-specified, not in SPEC.md §7.5): an adrenaline
    # steal increments items_used_this_turn by 2 — the adrenaline itself plus
    # the stolen item's immediate use — rather than 1. Flagged in the report.
    sub_outcomes = _apply_use(transferred, stolen, rules, count_delta=2)
    return sub_outcomes
