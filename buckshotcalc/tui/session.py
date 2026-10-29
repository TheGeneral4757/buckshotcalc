"""Session: the undo/edit/event/log model behind the TUI (SPEC.md §9).

Pure stdlib — this module must **never** import `textual` or `rich` (SPEC.md §0.1,
M7 brief). It owns the live `GameState`, the loaded `Rules`, the `GameMode`,
an unbounded history stack, and the plain-language event log the operator
asked for (rendered in the right rail's LOG panel — the log is presentation
of history this module already tracks, not a second parallel history). It is
the only place §9's input events are translated into engine transitions.
`app.py`/`panels.py` call into this module and render its results; they
never touch `GameState` directly.

Every event pushes the state that was current before it onto `_history`,
paired with the plain-language description that also lands in `log`, and
installs the successor as `state`. `undo()` pops that stack and restores the
exact prior snapshot — because `GameState` is frozen and immutable there is
no transition to invert, only a prior snapshot to restore — and appends a
narration of *what it undid* to `log`, per the operator's explicit
desync-recovery requirement: popping the log is not enough, the reversal
must be visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from buckshotcalc.engine.actions import Action, ActionKind, apply
from buckshotcalc.engine.dealer import dealer_phone_target
from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from buckshotcalc.tui.analysis import item_display_name

Outcome = tuple[float, GameState]



class SessionError(Exception):
    """Base class for every session-level error the UI can catch and render.

    Per SPEC.md §9, a mis-keyed event must never silently poison the state —
    it must be visible and recoverable. Every method below that can fail
    raises a typed subclass of this rather than corrupting `self.state`.
    """


class NoUndoHistoryError(SessionError):
    """Raised by `undo()` when the history stack is empty."""


class NoMatchingOutcomeError(SessionError):
    """An observed event does not match any real branch of the engine transition.

    This doubles as desync detection: claiming a LIVE shell was fired when
    the chamber is already known-BLANK (or any other observation that
    contradicts tracked state) raises here rather than silently accepting
    the operator's claim and corrupting the chamber pool.
    """


class InvariantViolation(SessionError):
    """A direct edit (§9 `[e]`) would violate a `GameState`/`ChamberState` invariant.

    Wraps the `ValueError` the engine's frozen dataclasses raise from
    `__post_init__` so the UI has one exception type to catch for "this edit
    is not legal," without ever letting a half-applied edit reach `self.state`.
    """


# ---------------------------------------------------------------------------
# Outcome selection — matching an *observed* event to the engine's chance-node
# branches, without reimplementing any transition (SPEC.md §0.1 (3)
# / the M7 brief: "select that branch — do not re-implement the transition").
# ---------------------------------------------------------------------------


def _select_by_pop(
    pre: GameState, outcomes: tuple[Outcome, ...], shell: Shell
) -> GameState:
    """Pick the outcome where the *popped* slot-0 shell was `shell`.

    Used for events that consume slot 0 (a shot, Beer): the shell itself is
    no longer present in the successor to compare directly, so the branch is
    identified by which pool count dropped by exactly one — `total_live()`/
    `total_blank()` sum resolved and pooled shells for the *remaining*
    chamber, so popping a live shell (and nothing else) drops `total_live()`
    by exactly 1 and leaves `total_blank()` untouched, and vice versa.
    """
    pre_live = pre.chamber.total_live()
    pre_blank = pre.chamber.total_blank()
    # An inverted slot 0 is counted by its pre-flip shell, so its pop moves
    # the other type's total.
    counted = shell
    if pre.chamber.inverted0:
        counted = Shell.BLANK if shell == Shell.LIVE else Shell.LIVE
    for _weight, succ in outcomes:
        live_delta = succ.chamber.total_live() - pre_live
        blank_delta = succ.chamber.total_blank() - pre_blank
        if counted == Shell.LIVE and live_delta == -1 and blank_delta == 0:
            return succ
        if counted == Shell.BLANK and blank_delta == -1 and live_delta == 0:
            return succ
    raise NoMatchingOutcomeError(
        f"no branch matches an observed {shell.name} shell here — tracked "
        "state already rules this out (desync?)"
    )


def _select_by_slot(
    pre: GameState, outcomes: tuple[Outcome, ...], index: int, shell: Shell
) -> GameState:
    """Pick the outcome where `chamber.slots[index]` resolved to `shell`.

    Used for events that resolve a slot *without popping it* (Glass at index
    0, Phone at an index > 0): the resolved value is still directly readable
    in the successor's chamber.
    """
    for _weight, succ in outcomes:
        slots = succ.chamber.slots
        if 0 <= index < len(slots) and slots[index] == shell:
            return succ
    raise NoMatchingOutcomeError(
        f"no branch resolves slot {index} to {shell.name} — tracked state "
        "already rules this out (desync?)"
    )


def _select_medicine_outcome(
    pre: GameState, outcomes: tuple[Outcome, ...], healed: bool
) -> GameState:
    """Pick the Expired Medicine branch matching the observed HP direction."""
    mover = pre.turn
    pre_hp = pre.hp_of(mover)
    for _weight, succ in outcomes:
        post_hp = succ.hp_of(mover)
        if healed and post_hp >= pre_hp:
            return succ
        if not healed and post_hp < pre_hp:
            return succ
    raise NoMatchingOutcomeError(
        f"no branch matches healed={healed} for expired medicine here"
    )


def _seat_word(player: Player) -> str:
    """Plain-language actor name for the LOG/prompt narration ("you"/"dealer")."""
    return "you" if player is Player.PLAYER else "dealer"


def _seat_has(player: Player) -> str:
    """`_seat_word` with the verb agreed, so log lines read as English.

    "you now has BURNER_PHONE x1" was the old inventory line; the redesign
    treats the history box as something read at speed, so it says "you have a
    phone" instead.
    """
    return "you have" if player is Player.PLAYER else "the dealer has"


def _item_tally(player: Player, item: Item, count: int) -> str:
    """One inventory line, in words: "you have a beer" / "the dealer has no saw"."""
    name = item_display_name(item)
    if count == 0:
        return f"{_seat_has(player)} no {name}"
    if count == 1:
        # "a cigarettes" reads wrong: the plural-named items take no article.
        article = "" if name.endswith("s") else "a "
        return f"{_seat_has(player)} {article}{name}"
    return f"{_seat_has(player)} {count} × {name}"


class Session:
    """Owns the live `GameState`, its history, its log, and §9 event application.

    One `Session` per running TUI. `rules` and `mode` are fixed for its
    lifetime (the same pair a `Solver` is built against).
    """

    def __init__(self, rules: Rules, mode: GameMode, state: GameState) -> None:
        self.rules = rules
        self.mode = mode
        self.state = state
        # Each entry pairs the state *before* the event with a plain-language
        # description of the event, so `undo()` can both restore the state
        # and narrate exactly what it reversed.
        self._history: list[tuple[GameState, str]] = []
        self.log: list[str] = []
        # Set when an unobservable chance node has left our belief about slot
        # 0 unrepresentable (SPEC.md §15.16(b)); `None` when the chamber is
        # trustworthy. A `GameState` holds one chamber, so it cannot express
        # the mixture such a node produces -- rather than commit to a branch
        # the operator cannot actually observe, the session says so and the
        # TUI suppresses `P(win)` until an observation collapses it.
        self.degraded: str | None = None
        # True when the load just ended on a Beer that ejected the last shell
        # while sawed: the game skips EndTurn, so the saw carries (SPEC.md §15.18).
        self.saw_carry = False
        self._degraded_history: list[tuple[str | None, bool]] = []

    # -- history / log ----------------------------------------------------

    def _push(
        self,
        new_state: GameState,
        description: str,
        degraded: str | None = None,
        saw_carry: bool = False,
    ) -> GameState:
        """Advance the session, carrying `degraded` (SPEC.md §15.16(b)) with the state.

        `degraded` is a *reason* to start (or keep) suppressing `P(win)`.
        When it is `None` the flag is cleared automatically as soon as the
        event resolved or consumed the slot our belief had gone bad on:
        either slot 0 is now a known value (a Glass or Beer observation) or
        the chamber got shorter (a shot popped it, and the operator reported
        the fired shell). Both are real observations that put the pool back
        on a footing the engine can represent, which is precisely what "until
        the next observation collapses it" means. Anything that leaves the
        chamber alone -- an item use, an HP edit -- leaves the flag standing.
        """
        prior = self.state
        self._history.append((prior, description))
        self._degraded_history.append((self.degraded, self.saw_carry))
        self.state = new_state
        self.saw_carry = saw_carry
        if degraded is not None:
            self.degraded = degraded
        elif self.degraded is not None:
            collapsed = (
                new_state.chamber.slots[0] is not None
                if len(new_state.chamber) > 0
                else True
            ) or len(new_state.chamber) < len(prior.chamber)
            if collapsed:
                self.degraded = None
        self.log.append(description)
        return self.state

    def can_undo(self) -> bool:
        return bool(self._history)

    def undo(self) -> GameState:
        """Restore the exact prior `GameState` (SPEC.md §9 `[u]`, unbounded).

        Appends `'undo: reverted "<description>"'` to `log` — undo must say
        what it undid, not merely disappear the last log line, so a mis-key
        is visible immediately rather than after it has poisoned several
        recommendations (the operator's explicit desync-recovery requirement).
        """
        if not self._history:
            raise NoUndoHistoryError("undo: history is empty")
        prior_state, description = self._history.pop()
        self.state = prior_state
        self.degraded, self.saw_carry = self._degraded_history.pop()
        self.log.append(f'undo: reverted "{description}"')
        return self.state

    def recent_log(self, n: int = 5) -> tuple[str, ...]:
        """The last `n` log lines, oldest first — what the LOG panel renders."""
        return tuple(self.log[-n:])

    # -- observed shell events (SPEC.md §9) ------------------------------

    def record_shot(self, at_self: bool, shell: Shell) -> GameState:
        """A live/blank shell was fired at self/dealer (§9 `s`/`d` then `l`/`b`).

        `at_self` names the *target* of the shot the mover just took, which
        is always `state.turn` — SHOOT_SELF/SHOOT_OPPONENT fire from
        whichever seat is currently moving (the Dealer shoots itself too).
        """
        pre = self.state
        actor = _seat_word(pre.turn)
        target = actor if at_self else _seat_word(pre.turn.other)
        kind = ActionKind.SHOOT_SELF if at_self else ActionKind.SHOOT_OPPONENT
        outcomes = apply(pre, Action(kind), self.rules)
        post = _select_by_pop(pre, outcomes, shell)
        hurt = pre.turn if at_self else pre.turn.other
        if post.hp[hurt] < pre.hp[hurt]:
            consequence = f"{target} {pre.hp[hurt]}→{post.hp[hurt]}"
        else:
            consequence = "no damage"
        # Plain English for the history box; "live"/"blank" stay as words
        # because the watch view colours them by regex.
        if at_self:
            victim = "yourself" if pre.turn is Player.PLAYER else "itself"
        else:
            victim = "you" if pre.turn.other is Player.PLAYER else "the dealer"
        description = f"{actor} shot {victim}: {shell.name.lower()} ({consequence})"
        return self._push(post, description)

    def record_glass(self, shell: Shell) -> GameState:
        """Our own Magnifying Glass result (§9 `g` then `l`/`b`).

        Resolves `chamber.slots[0]` for **us only** — it must never touch
        `dealer_knows` (docs/dealer-knowledge.md; the M7 brief is explicit
        about this exact case).
        """
        pre = self.state
        outcomes = apply(pre, Action(ActionKind.USE, Item.MAGNIFYING_GLASS), self.rules)
        post = _select_by_slot(pre, outcomes, 0, shell)
        actor = _seat_word(self.state.turn)
        return self._push(post, f"{actor} used GLASS → {shell.name}")

    def record_phone(self, slot_index: int | None, shell: Shell | None) -> GameState:
        """Our own Burner Phone result: a named future slot's value (§9 `p`).

        `slot_index=None` is the game's "one shell left" line: the phone is
        spent and tells us nothing.

        Resolves `chamber.slots[slot_index]` for **us only**, same rationale
        as `record_glass`.
        """
        pre = self.state
        outcomes = apply(pre, Action(ActionKind.USE, Item.BURNER_PHONE), self.rules)
        actor = _seat_word(self.state.turn)
        if slot_index is None or shell is None:
            return self._push(outcomes[0][1], f"{actor} used PHONE → no information")
        post = _select_by_slot(pre, outcomes, slot_index, shell)
        return self._push(
            post, f"{actor} used PHONE slot {slot_index} → {shell.name}"
        )

    def record_beer(self, shell: Shell) -> GameState:
        """Our own Beer's revealed shell (structurally a slot-0 pop, like a shot).

        SPEC.md §9's key table has no dedicated key for this; we treat it
        like Glass — press the Beer item key, then `l`/`b` — since Beer
        resolves slot 0 exactly as Glass does (SPEC.md §5.1). This is an
        input-model detail SPEC.md §9 does not settle; see SPEC.md §15.15.
        """
        pre = self.state
        outcomes = apply(pre, Action(ActionKind.USE, Item.BEER), self.rules)
        post = _select_by_pop(pre, outcomes, shell)
        actor = _seat_word(self.state.turn)
        return self._push(
            post, f"{actor} used BEER → {shell.name}", saw_carry=self._beer_carries_saw(pre)
        )

    def _beer_carries_saw(self, pre: GameState) -> bool:
        # The last-shell Beer skips EndTurn, so the saw survives into the reload.
        return (
            pre.sawed
            and len(pre.chamber) == 1
            and not self.rules.item_semantics.beer.clears_sawed
        )

    def record_inverter(self) -> GameState:
        """An Inverter was used (SPEC.md §5.2), by either seat.

        A resolved slot 0 flips exactly. An unresolved one flips unseen: the
        engine tracks it as `ChamberState.inverted0`, so the belief stays exact
        and the next observation of slot 0 resolves it.
        """
        actor = _seat_word(self.state.turn)
        unseen = self.state.chamber.slots[0] is None
        (_, post), = apply(self.state, Action(ActionKind.USE, Item.INVERTER), self.rules)
        note = " (unseen shell, tracked as flipped)" if unseen else " (flip)"
        return self._push(post, f"{actor} used INVERTER{note}")

    def record_expired_medicine(self, healed: bool) -> GameState:
        """Our own Expired Medicine's outcome (SPEC.md §6.1's 50/50 chance node).

        Matched on whether the mover's HP went up (`healed`) or down.
        """
        actor = _seat_word(self.state.turn)
        outcomes = apply(
            self.state, Action(ActionKind.USE, Item.EXPIRED_MEDICINE), self.rules
        )
        post = _select_medicine_outcome(self.state, outcomes, healed)
        verb = "healed" if healed else "harmed"
        return self._push(post, f"{actor} used MEDICINE: {verb}")

    #: The Dealer's information items — used privately, so we see the *use*
    #: but never the answer. Routing one of these through `record_glass` /
    #: `record_phone` would leak the Dealer's private information into our
    #: own belief state, which SPEC.md §0.1 (5) names as the single easiest way to
    #: make every recommendation silently wrong.
    DEALER_INFO_ITEMS = frozenset({Item.MAGNIFYING_GLASS, Item.BURNER_PHONE})

    def record_dealer_info_action(
        self, slot_index: int, item: Item = Item.MAGNIFYING_GLASS
    ) -> GameState:
        """The Dealer used its own Glass/Phone and privately resolved a slot.

        Per docs/dealer-knowledge.md step 1: a Dealer information action adds
        `slot_index` to `dealer_knows` and does **not** resolve the slot for
        us — the opposite direction from `record_glass`/`record_phone`,
        which resolve a slot for us and must never touch `dealer_knows`.

        The Dealer's item is consumed here rather than by routing through
        `engine.actions.apply`, because `apply` would also resolve the slot
        *in our chamber* — that is the whole point of not using it. Only the
        publicly observable consequences are applied: the item leaves the
        Dealer's inventory, the per-turn item counter advances, and
        `dealer_knows` gains the slot. Our chamber is deliberately untouched.
        """
        n = len(self.state.chamber.slots)
        if not (0 <= slot_index < n):
            raise SessionError(
                f"record_dealer_info_action: slot {slot_index} out of range [0, {n})"
            )
        if item not in self.DEALER_INFO_ITEMS:
            raise SessionError(
                f"record_dealer_info_action: {item.name} is not an information item"
            )
        held = self.state.items_of(Player.DEALER)
        if held[item] <= 0:
            raise SessionError(f"the dealer holds no {item.name} to use")
        post = (
            self.state.with_items(Player.DEALER, held.with_delta(item, -1))
            .with_items_used_this_turn(self.state.items_used_this_turn + 1)
            .with_dealer_knows(self.state.dealer_knows | {slot_index})
        )
        name = item.config_name.replace("_", " ")
        # The Glass's target really is observable (it is always the chambered
        # round); the Phone's is not, so the log must not word them alike.
        how = "private" if item is Item.MAGNIFYING_GLASS else "slot assumed, never seen"
        return self._push(
            post, f"dealer used {name} on slot {slot_index} ({how} — we do not see it)"
        )

    def record_dealer_phone(self) -> GameState:
        """The Dealer used its own Burner Phone (SPEC.md §15.16(a)).

        M7 asked the operator which slot the Dealer checked. Nothing on
        screen says — the phone's answer is private and so is its *target* —
        so that prompt could only ever be answered with an invention, which
        is the same defect §15.16(b) had for the Inverter. It is removed:
        the slot comes from `engine.dealer.dealer_phone_target`, the single
        rule the search itself already applies to every future Dealer phone
        use.

        That is still an assumption (index 1 — see
        `docs/decisions-pending.md` item 10, which remains open, and SPEC.md
        §15.9 for the direction of its bias). The change is *whose*
        assumption it is: a documented modelling choice the search shares,
        rather than a number the operator made up at the keyboard. Crucially
        the live session and the search now agree; before, the operator's
        answer could contradict what the solver assumed the Dealer knew in
        every line it searched from that position.
        """
        slot = dealer_phone_target(self.state, self.rules)
        return self.record_dealer_info_action(slot, Item.BURNER_PHONE)

    def record_dealer_steal_info(self, item: Item) -> GameState:
        """The Dealer used Adrenaline to take our Glass/Phone (live bridge).

        Same information rule as `record_dealer_info_action` (the answer is
        the Dealer's alone; our chamber is untouched), but the item comes off
        OUR rack and the Dealer's Adrenaline is spent. The charge is 2, like
        `_apply_adrenaline`.
        """
        if item not in self.DEALER_INFO_ITEMS:
            raise SessionError(f"record_dealer_steal_info: {item.name} is not an information item")
        st = self.state
        dealer, ours = st.items_of(Player.DEALER), st.items_of(Player.PLAYER)
        if dealer[Item.ADRENALINE] <= 0 or ours[item] <= 0:
            raise SessionError(f"the dealer cannot steal our {item.name} here")
        slot = 0 if item is Item.MAGNIFYING_GLASS else dealer_phone_target(st, self.rules)
        post = (
            st.with_items(Player.DEALER, dealer.with_delta(Item.ADRENALINE, -1))
            .with_items(Player.PLAYER, ours.with_delta(item, -1))
            .with_items_used_this_turn(st.items_used_this_turn + 2)
            .with_dealer_knows(st.dealer_knows | {slot})
        )
        name = item.config_name.replace("_", " ")
        return self._push(
            post, f"dealer used adrenaline → took our {name} (private — we do not see it)"
        )

    # -- deterministic item use / adrenaline ------------------------------

    _OBSERVATION_ITEMS = frozenset(
        {
            Item.BEER,
            Item.INVERTER,
            Item.MAGNIFYING_GLASS,
            Item.BURNER_PHONE,
            Item.EXPIRED_MEDICINE,
            Item.ADRENALINE,
        }
    )

    def use_item(self, item: Item) -> GameState:
        """Use a deterministic item: cigarettes, handcuffs, or the handsaw.

        The other item types have a chance-dependent or slot-dependent
        outcome the operator must report; use the dedicated `record_*` (or
        `steal`, for Adrenaline) method for those instead.
        """
        if item in self._OBSERVATION_ITEMS:
            raise SessionError(
                f"use_item: {item.name} needs a dedicated record_*/steal method"
            )
        actor = _seat_word(self.state.turn)
        outcomes = apply(self.state, Action(ActionKind.USE, item), self.rules)
        assert len(outcomes) == 1, f"use_item: {item.name} was not deterministic"
        return self._push(outcomes[0][1], f"{actor} used the {item_display_name(item)}")

    def steal(
        self,
        item: Item,
        *,
        shell: Shell | None = None,
        healed: bool | None = None,
        phone_slot: int | None = None,
    ) -> GameState:
        """Adrenaline: steal `item` from the opponent and apply its USE (SPEC.md §6.1).

        Deterministic stolen items (cigarettes, handcuffs, handsaw) need no
        extra argument. A stolen Beer needs `shell`; a stolen Expired
        Medicine needs `healed`; a stolen
        Magnifying Glass or Burner Phone needs `shell` (and `phone_slot` for
        the phone). Stealing a second Adrenaline is impossible by
        construction (`_adrenaline_stealable_items` excludes it).
        """
        actor = _seat_word(self.state.turn)
        outcomes = apply(
            self.state, Action(ActionKind.USE, Item.ADRENALINE, item), self.rules
        )
        prefix = f"{actor} used adrenaline → took the {item_display_name(item)}"
        if len(outcomes) == 1:
            return self._push(outcomes[0][1], prefix)
        if item is Item.EXPIRED_MEDICINE:
            if healed is None:
                raise SessionError("steal: stolen EXPIRED_MEDICINE needs healed=")
            post = _select_medicine_outcome(self.state, outcomes, healed)
            verb = "healed" if healed else "harmed"
            return self._push(post, f"{prefix}: {verb}")
        if item is Item.BEER:
            if shell is None:
                raise SessionError("steal: stolen BEER needs shell=")
            pre = self.state
            outcomes = apply(
                pre, Action(ActionKind.USE, Item.ADRENALINE, item), self.rules
            )
            post = _select_by_pop(pre, outcomes, shell)
            return self._push(
                post, f"{prefix} → {shell.name}", saw_carry=self._beer_carries_saw(pre)
            )
        if item is Item.MAGNIFYING_GLASS:
            if shell is None:
                raise SessionError("steal: stolen MAGNIFYING_GLASS needs shell=")
            pre = self.state
            outcomes = apply(
                pre, Action(ActionKind.USE, Item.ADRENALINE, item), self.rules
            )
            post = _select_by_slot(pre, outcomes, 0, shell)
            return self._push(post, f"{prefix} → {shell.name}")
        if item is Item.BURNER_PHONE:
            if shell is None or phone_slot is None:
                raise SessionError("steal: stolen BURNER_PHONE needs shell= and phone_slot=")
            pre = self.state
            outcomes = apply(
                pre, Action(ActionKind.USE, Item.ADRENALINE, item), self.rules
            )
            post = _select_by_slot(pre, outcomes, phone_slot, shell)
            return self._push(post, f"{prefix} slot {phone_slot} → {shell.name}")
        raise SessionError(f"steal: unsupported multi-outcome stolen item {item.name}")

    def waste_adrenaline(self) -> GameState:
        """Adrenaline spent with nothing taken: the game's steal timer ran out (rules.toml).

        Only the Adrenaline leaves the rack; the turn continues.
        """
        actor = _seat_word(self.state.turn)
        outcomes = apply(self.state, Action(ActionKind.USE, Item.ADRENALINE), self.rules)
        return self._push(outcomes[0][1], f"{actor} used adrenaline, nothing taken")

    # -- load / round boundaries (SPEC.md §9 `[n]`/`[r]`) ------------------

    def new_load(
        self,
        live: int,
        blank: int,
        *,
        items: tuple[ItemCounts, ItemCounts] | None = None,
        first_turn: Player = Player.PLAYER,
    ) -> GameState:
        """New load, same round — health carries over (SPEC.md §9 `[n]`)."""
        chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
        new_state = GameState.create(
            chamber=chamber,
            hp=self.state.hp,
            max_hp=self.state.max_hp,
            turn=first_turn,
            items=items,
            sawed=self.saw_carry,
            cuffed=(False, False),
            dealer_knows=frozenset(),
            items_used_this_turn=0,
        )
        return self._push(new_state, f"new load: {live} live / {blank} blank")

    def new_round(
        self,
        live: int,
        blank: int,
        *,
        max_hp: int | None = None,
        items: tuple[ItemCounts, ItemCounts] | None = None,
        first_turn: Player = Player.PLAYER,
    ) -> GameState:
        """New round — health resets to `max_hp` (SPEC.md §9 `[r]`)."""
        resolved_max_hp = max_hp if max_hp is not None else self.state.max_hp
        chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
        new_state = GameState.create(
            chamber=chamber,
            hp=(resolved_max_hp, resolved_max_hp),
            max_hp=resolved_max_hp,
            turn=first_turn,
            items=items,
            sawed=False,
            cuffed=(False, False),
            dealer_knows=frozenset(),
            items_used_this_turn=0,
        )
        return self._push(
            new_state,
            f"new round: {live} live / {blank} blank, HP reset to "
            f"{resolved_max_hp}/{resolved_max_hp}",
        )

    # -- direct field edits (SPEC.md §9 `[e]`, desync recovery) ------------

    def _edit(self, build: Callable[[GameState], GameState], description: str) -> GameState:
        try:
            new_state = build(self.state)
        except ValueError as exc:
            raise InvariantViolation(str(exc)) from exc
        return self._push(new_state, description)

    def edit_hp(self, player: Player, value: int) -> GameState:
        return self._edit(
            lambda s: s.with_hp(player, value), f"edit: hp {_seat_word(player)} → {value}"
        )

    def edit_max_hp(self, value: int) -> GameState:
        return self._edit(
            lambda s: replace(s, max_hp=value).canonical(), f"edit: max_hp → {value}"
        )

    def give_item(self, player: Player, item: Item) -> GameState:
        """Declare that `player` now holds one more `item` (quick-entry, TUI M9 fix).

        The `[e] item ...` grammar already lets the operator set a seat's
        item count directly; this is the same effect for the common single-
        item case — an item just seen on the table — without typing a
        command. Not turn-restricted: the operator declares what is on the
        table for either seat regardless of whose turn it is, since only
        *using* an item is turn-gated (SPEC.md §3.3), not seeing it.
        """
        held = self.state.items_of(player)
        new_counts = held.with_delta(item, 1)
        post = self.state.with_items(player, new_counts)
        n = new_counts.counts[item.value]
        return self._push(post, _item_tally(player, item, n))

    def edit_item_count(self, player: Player, item: Item, count: int) -> GameState:
        def build(s: GameState) -> GameState:
            counts = list(s.items_of(player).counts)
            counts[item.value] = count
            return s.with_items(player, ItemCounts(tuple(counts)))

        return self._edit(build, _item_tally(player, item, count))

    def edit_sawed(self, value: bool) -> GameState:
        return self._edit(lambda s: s.with_sawed(value), f"edit: sawed → {value}")

    def edit_cuffed(self, player: Player, value: bool) -> GameState:
        return self._edit(
            lambda s: s.with_cuffed(player, value),
            f"edit: {_seat_word(player)} cuffed → {value}",
        )

    def edit_turn(self, player: Player) -> GameState:
        return self._edit(
            lambda s: s.with_turn(player), f"edit: turn → {_seat_word(player)}"
        )

    def edit_dealer_knows(self, dealer_knows: frozenset[int]) -> GameState:
        return self._edit(
            lambda s: s.with_dealer_knows(dealer_knows),
            f"edit: dealer_knows → {sorted(dealer_knows)}",
        )

    def edit_chamber(
        self, slots: tuple[Shell | None, ...], unknown_live: int, unknown_blank: int
    ) -> GameState:
        """Direct chamber-composition edit. Raises `InvariantViolation` on a
        `ChamberState`/`slots.count(None) == unknown_live + unknown_blank`
        mismatch rather than corrupting `self.state` (SPEC.md §5's invariant).
        """

        def build(s: GameState) -> GameState:
            chamber = ChamberState.create(slots, unknown_live, unknown_blank)
            return s.with_chamber(chamber)

        return self._edit(build, "edit: chamber composition")
