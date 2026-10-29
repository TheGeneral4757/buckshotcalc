"""The Dealer's deterministic policy (SPEC.md §3.3, §3.4).

Pure stdlib. This module answers two questions, and only two:

1. `dealer_view` — what does the Dealer *believe* about the chamber right now?
   Never derived from `state.chamber.slots` directly (that is *our* view and
   may know strictly more than the Dealer, e.g. via our own Magnifying Glass —
   see `docs/dealer-knowledge.md`'s addendum). Built instead from public
   knowledge (revealed totals) plus whatever `state.dealer_knows` resolves.
2. `dealer_step` — given that belief, which action (or short atomic sequence
   of actions) does the Dealer's policy take next, and with what
   probability-weighted successor states?

A branch carries a *sequence* of actions rather than a single one because the
handsaw and the shot it was taken for are indivisible: the saw fires IFF the
Dealer intends to shoot the player (SPEC.md §3.3), so letting the caller come
back for the shot separately would re-derive that intent and, in the coinflip
case, contradict it. M4's solver consumes the terminal `GameState`; the TUI's
"projected line of play" panel (SPEC.md §8) renders the sequence.

`docs/dealer-knowledge.md` is binding here: lazy branching (only at slot 0,
only when the Dealer's decision actually consults a value we lack) is the only
place `dealer_step` opens a chance node over *our* knowledge; everything else
routes through `buckshotcalc.engine.actions.apply`, never reimplemented here.
"""

from __future__ import annotations

from buckshotcalc.engine.actions import (
    Action,
    ActionKind,
    _end_turn,
    _retain_turn,
    apply,
    legal_actions,
)
from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.state import ChamberState, GameState, Item, Player, Shell

DealerBranch = tuple[float, tuple[Action, ...], GameState]


# ---------------------------------------------------------------------------
# dealer_view — the Dealer's own belief about the chamber
# ---------------------------------------------------------------------------


def dealer_view(state: GameState, rules: Rules) -> ChamberState:
    """The Dealer's belief about `state.chamber` (docs/dealer-knowledge.md addendum).

    Three tiers of knowledge exist: public (the revealed load totals — every
    shell fired or ejected is observed by both sides), ours privately, and the
    Dealer's privately. `state.chamber.slots` conflates the first two — it is
    *our* view — so reading it here would leak our private knowledge into the
    Dealer's decisions (SPEC.md §7.3's cardinal error; worked example in the
    addendum: our own Magnifying Glass can make our `deduce()` resolve the
    whole chamber while the Dealer still knows only the multiset).

    Built instead as: same length as `state.chamber`; resolved at exactly the
    indices in `state.dealer_knows`, each value taken from
    `state.chamber.slots[i]` and skipped if that index is *itself* unresolved
    for us (SPEC.md §15.9's documented default for the Dealer's own private
    Burner Phone future-slot knowledge — see the residual note there); pool =
    the public remaining totals minus the shells at those resolved indices;
    then `deduce()`, the Dealer counting for itself. This subsumes
    `knows_shell_when_one_remains` for free.
    """
    chamber = state.chamber
    n = len(chamber.slots)
    total_live = chamber.total_live()
    total_blank = chamber.total_blank()

    resolved: dict[int, Shell] = {}
    if rules.dealer.knows_shell_after_own_magnifying_glass:
        for i in state.dealer_knows:
            if i >= n:
                continue
            value = chamber.slots[i]
            if value is None:
                # Unresolved for us too: the Dealer's own private future-slot
                # knowledge (its Burner Phone) that we never observed. Per
                # SPEC.md §15.9 it contributes nothing here — it only enters
                # `dealer_view` once it reaches slot 0, where `dealer_step`'s
                # lazy branch supplies the value for both sides at once.
                continue
            resolved[i] = value

    slots = tuple(resolved.get(i) for i in range(n))
    pool_live = total_live - sum(1 for v in resolved.values() if v is Shell.LIVE)
    pool_blank = total_blank - sum(1 for v in resolved.values() if v is Shell.BLANK)
    view = ChamberState(slots, pool_live, pool_blank)

    # Two distinct rules share the same `deduce()` mechanism but are gated
    # differently (correctness-patch item 1). `n <= 1` is the trivial "only
    # one shell left" case: with at most one unresolved slot, its type is
    # forced from public totals alone and needs no deduction skill, so it is
    # knowable in both modes (`knows_shell_when_one_remains`, unconditional).
    # `n > 1` with the pool already closed to one type is the general
    # count-closure deduction:
    # In endless mode only, a Dealer that does not already know the shell
    # runs its count-closure deduction. Gated on `rules.is_endless`; in the
    # base campaign the
    # Dealer does not perform this deduction at all.
    if rules.dealer.knows_shell_when_one_remains and (n <= 1 or rules.is_endless):
        pre_slot0 = slots[0] if n > 0 else None
        view = view.deduce()
        if n > 0 and pre_slot0 is None and view.slots[0] is not None:
            # Public count-closure alone resolved the chambered shell for the
            # Dealer, without slot 0 being in `resolved` above. The addendum
            # argues this can only happen once *our* knowledge has already
            # resolved slot 0 too: `resolved` is built only from indices that
            # are non-None for us, so the Dealer's pool is always a superset
            # of ours, and a Dealer-side collapse to a single shell type
            # forces the same collapse on our own (already-deduce-closed)
            # chamber. Verified here, not merely assumed — a failure of this
            # assertion is a counterexample to the note and must be reported,
            # not silently worked around.
            assert chamber.slots[0] is not None, (
                "dealer_view: public closure resolved slot 0 for the Dealer "
                "while it is still unresolved for us — counterexample to "
                "docs/dealer-knowledge.md's addendum"
            )
    return view


# ---------------------------------------------------------------------------
# The shooting decision (SPEC.md §3.3) — reused verbatim by the handsaw.
# ---------------------------------------------------------------------------


def _shot_distribution(view: ChamberState, rules: Rules) -> tuple[tuple[float, Player], ...]:
    """Who the Dealer's shot targets, and with what probability, given `view`.

    `Player.PLAYER` means "shoot the operator"; `Player.DEALER` means "shoot
    itself". Pure function of `view` alone — reused unchanged for the handsaw
    condition (SPEC.md §3.4: it "depends on the shooting decision").

    `rules.dealer.unknown_shell_equal_coinflip` is read as P(shoot player);
    the value is symmetric (0.5) today so this reading is unverified but
    inconsequential — flagged in the milestone report.

    The "majority rule" branches below are endless-mode gated
    (correctness-patch item 1, `VERIFIED` by observation): in the base
    campaign the Dealer is count-blind and always falls through to the plain
    coinflip below, regardless of the live/blank split. `rules.is_endless`
    reads v1's static `[mode]` declaration (`rules.toml`) — v1 ships
    double_or_nothing, so this must stay unconditionally on for us today.
    """
    d = rules.dealer
    slot0 = view.slots[0] if len(view) > 0 else None
    if slot0 is not None:
        return ((1.0, Player.PLAYER if slot0 == Shell.LIVE else Player.DEALER),)

    live = view.total_live()
    blank = view.total_blank()
    if live > blank and d.unknown_shell_more_live_shoots_player and rules.is_endless:
        return ((1.0, Player.PLAYER),)
    if blank > live and d.unknown_shell_more_blank_shoots_self and rules.is_endless:
        return ((1.0, Player.DEALER),)

    p = d.unknown_shell_equal_coinflip
    branches: list[tuple[float, Player]] = []
    if p > 0.0:
        branches.append((p, Player.PLAYER))
    if p < 1.0:
        branches.append((1.0 - p, Player.DEALER))
    return tuple(branches)


# ---------------------------------------------------------------------------
# Per-item IF-AND-ONLY-IF conditions (SPEC.md §3.3)
# ---------------------------------------------------------------------------


def _condition_fires(item: Item, state: GameState, view: ChamberState, rules: Rules) -> bool:
    """Whether `item`'s SPEC.md §3.3 usage condition holds right now.

    Does not check holding count, per-item precondition (e.g. handcuffs vs. an
    already-cuffed opponent), or the item-use cap — the caller filters those
    via `legal_actions`, the single source of truth for legality (never
    reimplemented here). ADRENALINE is handled separately (`_adrenaline_want`)
    because its condition is "wants an item", not a fixed predicate.
    """
    d = rules.dealer
    n = len(view)
    slot0 = view.slots[0] if n > 0 else None

    if item is Item.HANDCUFFS:
        # The condition is: the player is not already cuffed, and more than
        # one shell remains. The not-already-cuffed half
        # half is the per-item precondition, checked by `legal_actions`
        # (docstring above), not here — only the size guard is a condition.
        return d.always_uses_handcuffs and n >= d.handcuffs_requires_min_shells_remaining
    if item is Item.CIGARETTES:
        return d.uses_cigarettes_below_max_hp and state.hp_of(Player.DEALER) < state.max_hp
    if item is Item.EXPIRED_MEDICINE:
        hp = state.hp_of(Player.DEALER)
        dealer_cigarettes = state.items_of(Player.DEALER)[Item.CIGARETTES]
        # The condition also requires that it holds no cigarettes and is
        # not already using medicine.
        # `!usingMedicine` is a re-entrancy guard against the game's own
        # animation, not a game rule — deliberately not modelled; there is no
        # analogous re-entrancy hazard in a pure functional transition. The
        # "holds no cigarettes" clause IS a real, previously unmodelled game rule: the
        # Dealer will not use medicine while holding any cigarettes at all.
        return (
            d.medicine_requires_below_max_hp
            and hp < state.max_hp
            and hp >= d.medicine_requires_hp_at_least
            and (not d.medicine_requires_no_cigarettes_held or dealer_cigarettes == 0)
        )
    if item is Item.MAGNIFYING_GLASS:
        # The condition is: the Dealer does not already know the shell, and
        # more than one shell remains. The size guard is
        # arguably moot on its own — at size == 1 `knows_shell_when_one_remains`
        # already makes `slot0 is not None`, so "does not know the shell" is already
        # false — but implemented anyway so the two conditions are not
        # coupled by accident (SPEC.md item 2 of the correctness patch).
        return (
            d.uses_glass_when_chambered_shell_unknown
            and slot0 is None
            and n >= d.glass_requires_min_shells_remaining
        )
    if item is Item.BURNER_PHONE:
        # VERIFIED by observation, superseding the previous ASSUMED
        # "future shell unknown" condition (SPEC.md §15.8): the real
        # condition is purely `size > 2`, with no dependency on whether any
        # future slot is already resolved — a phone drawn onto an
        # already-resolved slot is legal and simply wastes it (item 4 of the
        # correctness patch; see `_use_burner_phone` in `actions.py`).
        return n >= d.burner_phone_min_shells_remaining
    if item is Item.BEER:
        # The condition is: the Dealer does not know the shell is live, and
        # more than one shell remains.
        return (
            d.uses_beer_when_knows_blank_or_not_knows_live
            and slot0 != Shell.LIVE
            and n >= d.beer_requires_min_shells_remaining
        )
    if item is Item.INVERTER:
        return d.uses_inverter_only_when_knows_blank and slot0 == Shell.BLANK
    if item is Item.HANDSAW:
        # "Wants" reading only (used by `_adrenaline_want`); the real firing
        # decision lives in `_evaluate_from`, which must branch on the
        # coinflip case rather than collapse it to a bool. Here — deciding
        # whether the Dealer *wants* a stolen handsaw via Adrenaline — a
        # judgment call: only an unambiguous "definitely shoots the player"
        # counts as wanting it; an ambiguous (coinflip) shot intent does not.
        # Undocumented by SPEC.md; flagged in the milestone report.
        dist = _shot_distribution(view, rules)
        return len(dist) == 1 and dist[0][1] is Player.PLAYER
    raise AssertionError(f"_condition_fires: unhandled item {item!r}")


def _adrenaline_want(
    state: GameState,
    view: ChamberState,
    rules: Rules,
    avail_actions: tuple[Action, ...],
) -> Item | None:
    """The item the Dealer's Adrenaline steals, or `None` if it wants nothing.

    SPEC.md §15.10: wants item X iff X is stealable (already filtered by
    `legal_actions`, which also excludes ADRENALINE-on-ADRENALINE), the Dealer
    does not already hold X, and X's own §3.3 condition currently fires — i.e.
    it steals what it would immediately use. Multiple wanted items: first in
    `Item` enum order (the same order `legal_actions` offers them in) — SPEC.md
    does not specify a tie-break, so this is a documented choice, not a rule.
    """
    d = rules.dealer
    dealer_items = state.items_of(Player.DEALER)
    stealable = [
        a.steal for a in avail_actions if a.item is Item.ADRENALINE and a.steal is not None
    ]
    for x in stealable:
        if d.adrenaline_requires_wanted_item_not_held and dealer_items[x] > 0:
            continue
        if d.adrenaline_wants_item_with_satisfied_condition and not _condition_fires(
            x, state, view, rules
        ):
            continue
        return x
    return None


# ---------------------------------------------------------------------------
# The Dealer's own information items (Glass, Phone) — docs/dealer-knowledge.md
# step 1: adds an index to `dealer_knows`, never resolves `state.chamber`.
# ---------------------------------------------------------------------------


def _use_dealer_info_item(
    state: GameState, item: Item, target: int, rules: Rules
) -> GameState:
    """Consume `item` for the Dealer's own reveal without touching `state.chamber`.

    `docs/dealer-knowledge.md` step 1 is explicit: a Dealer information action
    "adds the index to `dealer_knows` and does not resolve the slot for us. No
    chance node. The chamber's pool is untouched, because our belief is
    untouched." Routing through `actions.apply`/`_use_magnifying_glass`/
    `_use_burner_phone` would do exactly the opposite — resolve *our* chamber
    from the Dealer's private draw — which is the shipped-since-M3 bug this
    function exists to fix.

    Charges the item count and the per-turn item-use cap identically to
    `actions._apply_use`, and routes the turn through the very same
    `_end_turn`/`_retain_turn` helpers (imported, not reimplemented) so a
    future `rules.toml` change to `retains_turn` for either item is honoured
    here exactly as it would be for any other item use — SPEC.md §0.1
    non-negotiable #2, "no hardcoded rule values."

    Parallel to (but a separate implementation from) `tui.session.Session
    .record_dealer_info_action`, which exists for the identical reason on the
    TUI's manual-tracking path over `Session`, not `GameState` — the two
    types don't share a base, and `tui/` is allowed third-party imports the
    engine must never take on (SPEC.md §0.1 (3)). The *rule* the
    brief warns must not fork — charge the item, charge the cap, union
    `target` into `dealer_knows`, leave the chamber alone — is the same in
    both; only the plumbing differs.
    """
    dealer_items = state.items_of(Player.DEALER).with_delta(item, -1)
    successor = (
        state.with_items(Player.DEALER, dealer_items)
        .with_items_used_this_turn(state.items_used_this_turn + 1)
        .with_dealer_knows(state.dealer_knows | {target})
    )
    if rules.item_semantics.for_item(item).retains_turn:
        return _retain_turn(successor)
    if successor.winner() is None:
        return _end_turn(successor)
    return successor


def dealer_phone_target(state: GameState, rules: Rules) -> int:
    """The slot index recorded in `dealer_knows` for the Dealer's own Burner Phone.

    VERIFIED source draws uniformly over every slot `1..n-1` (correctness-
    patch item 4; SPEC.md §15.9's addendum), including already-resolved ones.
    We never observe which slot was drawn or what it held — not even the
    index — so branching `dealer_step` over that draw would let the search's
    own downstream play diverge between branches we cannot actually tell
    apart, which is exactly the strategy-fusion failure mode
    `docs/dealer-knowledge.md`'s "branch lazily... never at the moment the
    Dealer acquires it" rule exists to forbid: acquiring the phone's answer
    does not disclose it (the doc's own table lists both Glass and Phone as
    "Depends on value? no" / "What we observe? —"), unlike every behaviour
    that legitimately earns a branch.

    So v1 records a single, deterministic index rather than modelling the
    true distribution: index 1, the slot nearest the chamber (unconditional —
    `rules.dealer.burner_phone_min_shells_remaining` already guarantees at
    least two non-chambered slots exist whenever this fires). This is an
    ASSUMED engineering simplification, not a rule fact, and per SPEC.md
    §15.9 it understates the Dealer (a real draw landing on a farther slot
    would delay the Dealer's `dealer_view` gain past what this models, and a
    real draw landing on an already-resolved slot wastes the phone entirely,
    neither of which this single fixed choice can express). Left open, with
    the reasoning and the alternative (branch and accept the fusion; or model
    a belief instead of a point) recorded in `docs/decisions-pending.md`
    rather than settled silently here — do not change this to a branch
    without updating that entry.

    Public (not `_`-prefixed) because `tui/session.py` must record a Dealer
    phone use the *same* way: the operator cannot see which slot the Dealer
    checked any more than the search can, so both seats of the problem have
    to resolve it through this one function or the live session and the
    search would silently assume different slots.
    """
    return 1


# ---------------------------------------------------------------------------
# Priority walk (SPEC.md §3.4) — config-driven order, never hardcoded.
# ---------------------------------------------------------------------------


def _evaluate_from(
    state: GameState,
    view: ChamberState,
    rules: Rules,
    start: int,
    weight: float,
    committed_target: Player | None,
    stale_self: bool = False,
) -> list[DealerBranch]:
    """Walk `rules.dealer.item_priority` from index `start`, honouring config order.

    `committed_target` carries a shot-intent already resolved by an earlier
    handsaw evaluation in *this same decision* (its "doesn't fire" branch),
    so a later fallthrough to the shoot decision does not re-derive — and
    potentially contradict — the coinflip that decided the saw wasn't used.

    Confirmed by observation (correctness-patch item 8): when the
    Dealer holds an unused handsaw and does not know the shell, a single
    `CoinFlip()` decides the target and whether to saw *simultaneously* — this
    is exactly what threading `committed_target` through models. Left
    unchanged.

    `stale_self` is the game's leftover `dealerTarget = "self"` after a Beer on
    a shell the Dealer knew was blank (SPEC.md §15.19). It is hidden Dealer
    state that outlives the recursive `DealerChoice`, so the rest of that
    Dealer turn is walked atomically (`_continue_stale`) rather than handed
    back to a caller who could not carry it. It only matters at the final
    shot, and only while slot 0 is still unknown to the Dealer.
    """
    priority = rules.dealer.item_priority
    stale_on = rules.dealer.beer_keeps_known_blank_target and rules.is_endless
    avail_actions = legal_actions(state, rules)
    available_use_items = {a.item for a in avail_actions if a.kind is ActionKind.USE}

    for i in range(start, len(priority)):
        item = priority[i]

        if item is Item.HANDSAW:
            available = (
                item in available_use_items and rules.dealer.uses_handsaw_only_when_shooting_player
            )
            if not available:
                continue
            branches: list[DealerBranch] = []
            for w, target in _shot_distribution(view, rules):
                if target is Player.PLAYER:
                    saw = Action(ActionKind.USE, Item.HANDSAW)
                    for w2, s2 in apply(state, saw, rules):
                        # The saw and the shot it was taken for are ONE atomic
                        # step. Handing control back here would let the next
                        # `dealer_step` re-derive the intent from scratch and,
                        # in the coinflip case, shoot *itself* with the saw
                        # still active — double self-damage the policy never
                        # chose. Thread the resolved target through instead.
                        tail = _evaluate_from(s2, view, rules, 0, 1.0, Player.PLAYER)
                        if i == len(priority) - 1:
                            # SPEC.md §3.4's documented order puts the handsaw
                            # last precisely because it "depends on the
                            # shooting decision that all prior steps inform".
                            # Sawing mutates only `sawed`, which can never
                            # *enable* a condition — so re-walking fires
                            # nothing new and the tail is exactly the shot.
                            # Asserted rather than assumed. Skipped when the
                            # (config-driven, §15.2) order moves the saw off
                            # the end, where the argument no longer applies —
                            # `committed_target` keeps the result correct
                            # either way.
                            assert all(
                                len(acts) == 1
                                and acts[0].kind is ActionKind.SHOOT_OPPONENT
                                for _, acts, _ in tail
                            ), (
                                "post-saw invariant violated: an item condition "
                                f"fired after the handsaw — tail was {tail!r}"
                            )
                        for w3, tail_actions, s3 in tail:
                            branches.append(
                                (weight * w * w2 * w3, (saw, *tail_actions), s3)
                            )
                else:
                    # Intent resolved to "self" for this branch's weight: the
                    # saw does not fire (IFF), and the eventual shoot below
                    # must stay consistent with that resolved intent rather
                    # than re-flipping.
                    branches.extend(
                        _evaluate_from(state, view, rules, i + 1, weight * w, Player.DEALER)
                    )
            return branches

        if item is Item.ADRENALINE:
            if item not in available_use_items:
                continue
            steal = _adrenaline_want(state, view, rules, avail_actions)
            if steal is None:
                continue
            action = Action(ActionKind.USE, Item.ADRENALINE, steal)
            adr = [
                (weight * w2, (action,), _knows_inverted(s2, steal))
                for w2, s2 in apply(state, action, rules)
            ]
            stale = stale_self or (steal is Item.BEER and view.slots[0] is Shell.BLANK)
            return _continue_stale(adr, rules) if stale and stale_on else adr

        if item not in available_use_items:
            continue
        if not _condition_fires(item, state, view, rules):
            continue
        action = Action(ActionKind.USE, item)

        if item is Item.MAGNIFYING_GLASS or item is Item.BURNER_PHONE:
            # The Dealer's own information items never route through
            # `apply()`: that would resolve the shell in *our* chamber, the
            # M3-era bug docs/dealer-knowledge.md exists to prevent (step 1).
            # Deterministic, single-branch — no chance node, matching the
            # doc's "the chamber's pool is untouched, because our belief is
            # untouched." The player's own use of either item is unaffected:
            # it still routes through `apply` -> `_use_magnifying_glass` /
            # `_use_burner_phone` below, which resolve *our* chamber as they
            # always have. The seat distinction is made here, by whose turn
            # this is (`_evaluate_from` only ever runs for `Player.DEALER`
            # per `dealer_step`'s guard) — never by a guess about the action.
            target = 0 if item is Item.MAGNIFYING_GLASS else dealer_phone_target(state, rules)
            successor = _use_dealer_info_item(state, item, target, rules)
            info = [(weight, (action,), successor)]
            return _continue_stale(info, rules) if stale_self and stale_on else info

        used = [
            (weight * w2, (action,), _knows_inverted(s2, item))
            for w2, s2 in apply(state, action, rules)
        ]
        stale = stale_self or (item is Item.BEER and view.slots[0] is Shell.BLANK)
        return _continue_stale(used, rules) if stale and stale_on else used

    # No item fired: shoot. Use the intent already committed by an earlier
    # handsaw branch if there is one; otherwise derive it fresh.
    dist: tuple[tuple[float, Player], ...]
    if committed_target is not None:
        dist = ((1.0, committed_target),)
    elif stale_self and stale_on and view.slots[0] is None:
        # Shoot(dealerTarget) with the target left over from the known blank
        # (DealerIntelligence.gd:275-276), not the majority rule.
        dist = ((1.0, Player.DEALER),)
    else:
        dist = _shot_distribution(view, rules)

    branches = []
    for w, target in dist:
        if target is Player.PLAYER:
            action = Action(ActionKind.SHOOT_OPPONENT)
        else:
            action = Action(ActionKind.SHOOT_SELF)
        for w2, s2 in apply(state, action, rules):
            branches.append((weight * w * w2, (action,), s2))
    return branches


def _knows_inverted(state: GameState, item: Item) -> GameState:
    """After its own Inverter the Dealer knows slot 0 is live (SPEC.md §3.3, §5.2).

    It only inverts a shell it knows is blank. That knowledge may have come
    from public counting rather than `dealer_knows`, and the flip breaks the
    count, so it is recorded here or `dealer_view` forgets it.
    """
    if item is not Item.INVERTER:
        return state
    return state.with_dealer_knows(state.dealer_knows | {0})


def _continue_stale(branches: list[DealerBranch], rules: Rules) -> list[DealerBranch]:
    """Extend each item-use branch with the rest of the Dealer's turn, stale target carried.

    Stops at the shot (which ends the action) and at anything that ends the
    Dealer's turn, a win, or an emptied chamber. A branch whose slot 0 the
    Dealer already knows is left alone: its target is then view-determined, so
    the stale one can never matter and the step stays a single action.
    """
    out: list[DealerBranch] = []
    for w, acts, s in branches:
        if (
            acts[-1].kind is ActionKind.USE
            and s.turn is Player.DEALER
            and s.winner() is None
            and not s.chamber.is_empty
            and dealer_view(s, rules).slots[0] is None
        ):
            for w2, acts2, s2 in dealer_step(s, rules, stale_self=True):
                out.append((w * w2, acts + acts2, s2))
        else:
            out.append((w, acts, s))
    return out


def _merge(branches: list[DealerBranch]) -> tuple[DealerBranch, ...]:
    """Sum the weights of branches that agree on both actions and successor.

    Two chance outcomes can land on the same `GameState` — canonicalization
    (SPEC.md §5.3) makes that more common than it looks. Collapsing them keeps
    M4's search from walking an identical subtree twice and keeps a
    distribution small enough for a human to check by hand.

    Keyed on `(actions, state)` rather than the state alone: distinct lines
    that happen to converge stay distinct, because the TUI renders the actions
    and merging them away would lose which line was taken. Order is
    first-appearance, so the result stays deterministic.
    """
    merged: dict[tuple[tuple[Action, ...], GameState], float] = {}
    for weight, actions, successor in branches:
        key = (actions, successor)
        merged[key] = merged.get(key, 0.0) + weight
    return tuple((w, a, s) for (a, s), w in merged.items())


# ---------------------------------------------------------------------------
# dealer_step — public entry point
# ---------------------------------------------------------------------------


def dealer_step(
    state: GameState, rules: Rules, *, stale_self: bool = False
) -> tuple[DealerBranch, ...]:
    """The probability-weighted distribution over the Dealer's single next action.

    Weights sum to 1.0. Callers (M4's solver) loop this until the turn passes
    — every `Action` here retains or ends the turn exactly as
    `buckshotcalc.engine.actions.apply` says it does, since that is the only
    place transitions are computed.

    Lazy branching (docs/dealer-knowledge.md): if the Dealer resolved slot 0
    itself (`0 in state.dealer_knows`) but we have not, the policy is about to
    consult a value we lack. Branch there, and only there, as a chance node
    weighted by our belief (`chamber.p_live()`), resolving slot 0 for us too
    inside each branch — sound because every Dealer behaviour whose condition
    depends on that value discloses it through the resulting observation (see
    the note's table). Elsewhere, `dealer_view` alone determines the Dealer's
    behaviour; our own resolved/unresolved slots never do.

    `stale_self` is internal (set by `_continue_stale`): see `_evaluate_from`.
    """
    if state.turn is not Player.DEALER:
        raise ValueError("dealer_step: state.turn is not Player.DEALER")
    if state.chamber.is_empty:
        raise ValueError("dealer_step: chamber is empty; caller must handle reload (SPEC.md §7.4)")

    # An unseen Inverter flip (`inverted0`) branches here too: the Dealer counts
    # the true shells (FigureOutShell, DealerIntelligence.gd:282-303), and its
    # turn always ends by firing or ejecting slot 0 in front of us.
    if (0 in state.dealer_knows or state.chamber.inverted0) and state.chamber.slots[0] is None:
        chamber = state.chamber
        p_live = chamber.p_live()
        branches: list[DealerBranch] = []
        if p_live < 1.0:
            blank_state = state.with_chamber(chamber.resolve(0, Shell.BLANK))
            sub = dealer_step(blank_state, rules, stale_self=stale_self)
            branches.extend((w * (1.0 - p_live), a, s) for w, a, s in sub)
        if p_live > 0.0:
            live_state = state.with_chamber(chamber.resolve(0, Shell.LIVE))
            sub = dealer_step(live_state, rules, stale_self=stale_self)
            branches.extend((w * p_live, a, s) for w, a, s in sub)
        return _merge(branches)

    view = dealer_view(state, rules)
    return _merge(_evaluate_from(state, view, rules, 0, 1.0, None, stale_self))
