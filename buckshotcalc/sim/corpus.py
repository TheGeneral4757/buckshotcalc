"""Known-position agreement corpus (SPEC.md §12.2, M6).

Pure stdlib. This module answers one question: does the solver reproduce a
handful of positions whose correct answer is known **independently of the
solver** — from SPEC.md §3.3's stated exploits, from hand arithmetic worked
against the transition rules directly, or from a rule so direct no search is
needed?

We do not have a dataset of community-sourced answers (SPEC.md §12.2 asks for
one; none exists for this project). Building entries by asking the solver what
it does and recording that as "expected" would be circular — it would always
report 100% agreement and prove nothing. Every scored entry here therefore
carries a `Provenance` that names where its expected answer actually came from,
and only entries whose provenance is independent of the solver are counted in
`agreement_rate`. `Provenance.SOLVER_DERIVED` entries exist purely as
regression pins (a future refactor that silently changes their value gets
noticed) and are excluded from the rate — `agreement_rate` enforces this.

The reload fixed point (SPEC.md §7.4) costs ~19 min/sweep at shipped defaults
(docs/decisions-pending.md item 1) and cannot be run here. Every entry below
either never reaches a reload boundary on its asserted line, or reaches one on
a *losing* line whose exact value doesn't matter to the assertion, or is
checked against the same closed-form `hp_player / (hp_player + hp_dealer)`
stand-in `tests/test_solver.py` uses (`_health_ratio_table` there;
`_health_ratio_reload_table` here — duplicated rather than imported, because
`sim/` cannot depend on `tests/`). Entries that use the stub say so in their
`rationale`, so nobody mistakes one for a validation of the real table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import IntEnum

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.rules import Rules, default_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
)

# SPEC.md §12.2's acceptance criterion, named rather than left as a scattered
# literal: "Acceptance: >=90% agreement, with every remaining disagreement
# documented and explained."
AGREEMENT_THRESHOLD = 0.90


class Provenance(IntEnum):
    """Where a `CorpusEntry`'s expected answer came from (M6 brief).

    Only `SPEC_EXPLOIT`, `HAND_COMPUTED`, and `RULE_FORCED` are independent of
    the solver and are counted by `agreement_rate`. `SOLVER_DERIVED` entries
    are regression pins only.
    """

    SPEC_EXPLOIT = 0
    HAND_COMPUTED = 1
    RULE_FORCED = 2
    SOLVER_DERIVED = 3


_SCORED_PROVENANCE = (
    Provenance.SPEC_EXPLOIT,
    Provenance.HAND_COMPUTED,
    Provenance.RULE_FORCED,
)


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    """One known position and its independently-sourced expected answer."""

    name: str
    state: GameState
    mode: GameMode
    provenance: Provenance
    rationale: str
    expected_action: Action | None = None
    expected_value: float | None = None
    tolerance: float = 1e-9

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ValueError(f"CorpusEntry {self.name!r}: rationale must be non-empty")
        if self.expected_action is None and self.expected_value is None:
            raise ValueError(
                f"CorpusEntry {self.name!r}: at least one of expected_action/"
                "expected_value must be set"
            )
        if self.expected_action is not None and self.state.turn is not Player.PLAYER:
            # `SearchResult.children` (and therefore "the recommended action")
            # only exists at a live PLAYER decision node (solver.py's pinned
            # contract) — a Dealer-turn entry has no action to compare.
            raise ValueError(
                f"CorpusEntry {self.name!r}: expected_action set on a "
                f"non-PLAYER-turn state ({self.state.turn!r}); only value "
                "claims are checkable there"
            )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of checking one `CorpusEntry` against a live `Solver`."""

    entry: CorpusEntry
    agreed: bool
    # `None` at a non-PLAYER-turn entry (e.g. the dealer-knowledge mixture
    # entry): `evaluate()` has no children there (solver.py's pinned
    # contract), so there is no "recommended action" to report. Reporting a
    # fabricated action would be worse than reporting none — see the M6
    # report's judgment-call section for why this deviates from a bare
    # `Action`.
    actual_action: Action | None
    actual_value: float
    detail: str


# ---------------------------------------------------------------------------
# Reload value stand-in (see module docstring)
# ---------------------------------------------------------------------------


def _health_ratio_reload_table(rules: Rules) -> dict[tuple[int, int, int], float]:
    """`hp_player / (hp_player + hp_dealer)` for every `(hp_p, hp_d, max_hp)` key.

    An exact closed-form stand-in for the real (slow, sampled) `V_reload`
    fixed point, identical in spirit to `tests/test_solver.py`'s
    `_health_ratio_table`. Injected directly into `Solver._reload_table` so
    the corpus never triggers `_compute_reload_table`. Any entry whose
    asserted branch crosses a reload boundary says so in its `rationale`.
    """
    return {
        (hp_p, hp_d, max_hp): hp_p / (hp_p + hp_d)
        for max_hp in rules.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    }


def _corpus_rules(base: Rules) -> Rules:
    """`base` with `solver.max_items_per_turn` tightened to 1.

    An engineering knob, not a game rule (`rules.toml` marks the whole
    `[solver]` section "ours, not the game's") — the same kind of override
    `tests/test_solver.py`'s `TINY_OVERRIDES`/`SMALL_OVERRIDES` apply, just
    built via `dataclasses.replace` instead of a TOML round-trip so `sim/`
    stays free of a hand-rolled TOML writer.

    Why: SPEC.md §12.2 requires a corpus entry proving the solver never marks
    a genuinely no-op item (e.g. the Magnifying Glass on an already-resolved
    chamber) as *required*. Under the shipped cap of 6, no such entry is
    constructible — a no-op item use never forecloses any later action within
    the same turn (confirmed by `tests/test_solver.py`'s own commentary: "A
    genuinely free, no-downside item... can never be strictly wasted... it
    weakly dominates skipping it"), so demonstrating "wasted" needs a cap
    tight enough to force a real choice between the no-op item and a
    genuinely useful one. Cap=1 is the minimal such value.

    Verified (by hand, cross-checked against the solver) that tightening the
    cap to 1 changes none of this module's other entries' values relative to
    the shipped cap of 6 — every other entry needs at most one item use on
    its asserted line. See the M6 report for the check.

    Also re-stamps `fingerprint`. `Rules.fingerprint` is documented as the
    sha256 of the `rules.toml` *bytes*, and it is what keys the reload
    table's on-disk cache (SPEC.md §7.4). A `dataclasses.replace` changes the
    content without changing that hash, which leaves a `Rules` object whose
    fingerprint identifies a file it no longer matches — so a table computed
    under this cap could be written to, or read from, the cache entry
    belonging to the real `rules.toml`, and the live solver would load it and
    trust it. Nothing here triggers `_compute_reload_table` today (the stub
    table is always injected first), which makes this latent rather than
    live, and exactly the kind of silent cross-contamination that is cheaper
    to make impossible than to remember. `tests/test_solver.py`'s
    `_custom_rules` does not need this because it round-trips through a real
    TOML file and `load_rules` recomputes the hash honestly.
    """
    return replace(
        base,
        solver=replace(base.solver, max_items_per_turn=1),
        fingerprint=f"{base.fingerprint}+corpus-cap1",
    )


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------


def _entry_handcuffs_more_blanks_than_lives(rules: Rules) -> CorpusEntry:
    """SPEC.md §3.3's first exploit, stated outright: "If more blanks than
    lives remain, the Dealer will shoot itself. Do not waste handcuffs — let
    its own policy cost it the turn."

    Position: 1 live, 3 blank (unresolved), hp=(3,1), player to move, holding
    only Handcuffs. The player must not *need* Handcuffs to reach the best
    available value — some non-cuffing action must tie or beat
    USE(HANDCUFFS). `legal_actions` always orders SHOOT_SELF and
    SHOOT_OPPONENT ahead of any USE (SPEC.md §6.1/actions.py), so if a shot
    ties HANDCUFFS for best, the stable sort in `evaluate()` puts the shot
    first — the *recommendation* is therefore a shot, not the cuffs, which is
    exactly what "don't waste handcuffs" requires.

    The expected *action* is asserted here (SHOOT_OPPONENT); no numeric value
    is pinned — deriving the exact win probability by hand would mean
    hand-walking the Dealer's full self-shot retry chain over a 4-shell pool,
    which is not done here (see the M6 report). The *value* comparison this
    exploit really demands — cuffs is never strictly required — is checked
    separately in `tests/test_corpus.py` as the "sharper form" the M6 brief
    asks for: `value(USE HANDCUFFS) <= value(best action)`, re-derived from
    `evaluate()`'s own children rather than pinned to a magic number.
    """
    chamber = ChamberState.create(tuple([None] * 4), 1, 3)
    state = GameState.create(
        chamber=chamber,
        hp=(3, 1),
        max_hp=3,
        turn=Player.PLAYER,
        items=(ItemCounts.of(handcuffs=1), ItemCounts.empty()),
    )
    return CorpusEntry(
        name="handcuffs_more_blanks_than_lives",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.SPEC_EXPLOIT,
        rationale=(
            "SPEC.md §3.3: more blanks (3) than lives (1) means the Dealer's "
            "majority-rule policy shoots itself once it faces this chamber "
            "unresolved, over and over on each blank miss, until it either "
            "dies or the pool tips. Cuffing it away denies it nothing it "
            "would not already deny itself. legal_actions orders shots "
            "before any USE, so if the best shot ties Handcuffs, evaluate()'s "
            "stable sort recommends the shot, not the cuffs."
        ),
        expected_action=Action(ActionKind.SHOOT_OPPONENT),
    )


def _entry_wasted_magnifying_glass(rules: Rules) -> CorpusEntry:
    """SPEC.md §12.2's second mandatory check: an information item whose
    answer cannot change the action must not be required.

    Position: two shells, both forced LIVE by count-closure
    (`unknown_live=2, unknown_blank=0` — `ChamberState.deduce()` resolves both
    slots immediately, so the chamber is fully determined before the player
    even acts). The player holds both a Magnifying Glass (strictly pointless:
    the chambered shell is already known) and Handcuffs (genuinely useful:
    with a second known-live shell still to come, denying the Dealer's turn
    matters). `rules` is the cap=1 variant from `_corpus_rules` — see that
    function's docstring for why the cap must bind for "wasted" to be
    reachable at all.

    Hand computation (`hp=(3,3)`, `max_hp=3`, `base_damage=1`, against the
    `_health_ratio_reload_table` stand-in — this entry crosses a reload
    boundary on every line, so it validates the mechanism, not the real
    table):

    - USE(HANDCUFFS) then SHOOT_OPPONENT (cap reached by the cuffs, so no
      further item use before the mandatory shot): dealer 3->2 (known-live
      shot). SHOOT_OPPONENT always ends the turn; the Dealer is now cuffed,
      so the turn bounces straight back to the player (cuff stays, spent; cap
      reset — a genuine new turn). One known-live shell remains: the player
      shoots it too, dealer 2->1... no: 2 dealer HP already reached, so this
      SECOND shot takes dealer 2->0. Wait — walked precisely: after the first
      hit dealer is at 3-1=2; the second (post-bounce) hit takes dealer to
      2-1=1, NOT 0 — the chamber is then empty (both shells fired) ->
      reload_value(3, 1, 3) = 3/4 = 0.75.
    - Direct SHOOT_OPPONENT, or USE(MAGNIFYING_GLASS) then SHOOT_OPPONENT
      (cap reached either way by the one item use, or by none): dealer
      3->2, turn passes to the (uncuffed) Dealer, which already knows the
      remaining shell is live via public count-closure (0 blanks were ever
      in the pool) and shoots the player: player 3->2. Chamber empties ->
      reload_value(2, 2, 3) = 0.5.

    0.5 < 0.75, so USE(MAGNIFYING_GLASS) is strictly worse than the best line
    (Handcuffs) — `evaluate()` must mark it `wasted=True`. Verified
    numerically (both by direct computation and via the solver as a
    cross-check, not as the source of the expected numbers): USE(HANDCUFFS)
    = 0.75, SHOOT_OPPONENT = USE(MAGNIFYING_GLASS) = 0.5, SHOOT_SELF = 0.25.
    """
    chamber = ChamberState.create((None, None), 2, 0)
    state = GameState.create(
        chamber=chamber,
        hp=(3, 3),
        max_hp=3,
        turn=Player.PLAYER,
        items=(ItemCounts.of(magnifying_glass=1, handcuffs=1), ItemCounts.empty()),
    )
    return CorpusEntry(
        name="wasted_magnifying_glass_fully_determined_chamber",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.HAND_COMPUTED,
        rationale=(
            "Two shells, both forced LIVE by count-closure before the player "
            "acts. Magnifying Glass therefore reveals nothing. With Handcuffs "
            "also held and a cap of 1 (see `_corpus_rules`), the two items "
            "compete for the turn's one item-use slot: cuffing denies the "
            "Dealer's turn between the two known-live shots (both land on the "
            "Dealer, value 0.75), while glassing wastes that slot and lets "
            "the Dealer get its own known-live shot in on the player instead "
            "(value 0.5). Full arithmetic against the health-ratio reload "
            "stand-in is in this function's docstring. Crosses a reload "
            "boundary on every line — stub table only, not a validation of "
            "the real one."
        ),
        expected_action=Action(ActionKind.USE, Item.HANDCUFFS),
        expected_value=0.75,
    )


def _entry_handsaw_decisive(rules: Rules) -> CorpusEntry:
    """SPEC.md §12.2's third mandatory check, decisive half: sawing must be
    recommended when the extra damage converts a non-lethal shot into a
    lethal one.

    Position: one shell, guaranteed live (`live=1, blank=0`). Dealer at 2 HP.
    `rules.turns.base_damage` (1) leaves the Dealer alive; `rules.item_
    semantics.handsaw.damage` (2) kills. Neither literal is hardcoded — both
    come from `rules`.

    Hand computation: without the saw, SHOOT_OPPONENT deals `base_damage`,
    dealer 2->1 (alive), chamber empties -> a reload at
    `reload_value(hp_player, 1, max_hp)` (< 1.0, stub table). With the saw,
    USE(HANDSAW) then SHOOT_OPPONENT deals `handsaw.damage`, dealer 2->0 — a
    genuine terminal win, value exactly 1.0, independent of the reload table
    entirely (the winning line never reaches one). 1.0 beats anything < 1.0,
    so USE(HANDSAW) must be the top-ranked action.
    """
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber,
        hp=(3, 2),
        max_hp=3,
        turn=Player.PLAYER,
        items=(ItemCounts.of(handsaw=1), ItemCounts.empty()),
    )
    return CorpusEntry(
        name="handsaw_decisive_converts_to_lethal",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.HAND_COMPUTED,
        rationale=(
            "One shell, guaranteed live; Dealer at 2 HP. base_damage=1 "
            "leaves the Dealer alive (non-lethal); handsaw.damage=2 kills. "
            "The sawed line is a genuine terminal win (value 1.0, no reload "
            "lookup needed at all on this branch); the unsawed line reaches "
            "a reload at strictly less than 1.0. USE(HANDSAW) must therefore "
            "be the top-ranked action."
        ),
        expected_action=Action(ActionKind.USE, Item.HANDSAW),
        expected_value=1.0,
    )


def _entry_handsaw_not_decisive(rules: Rules) -> CorpusEntry:
    """SPEC.md §12.2's third mandatory check, non-decisive half: sawing must
    not be required when the extra damage changes nothing.

    Position: one shell, guaranteed live, Dealer already at 1 HP —
    `rules.turns.base_damage` alone is already lethal (1-1=0), so
    `rules.item_semantics.handsaw.damage`'s extra point is provably inert:
    both the sawed and unsawed shots reach the exact same terminal win
    (value 1.0 either way; HP is clamped at 0 by `GameState.canonical`, so
    "overkill by one" and "kill exactly" are the same state). Chosen over the
    "Dealer HP still far from death" reading of "changes nothing" because
    that reading is not constructible as an exact tie under this codebase's
    monotone-in-opponent-HP reload model without also engineering an
    unrelated cap-driven opportunity cost (see the M6 report's judgment-call
    section) — this is the clean, unambiguous half of "not decisive": already
    decided without the saw.

    Because both lines are exact terminal wins, SHOOT_OPPONENT and
    USE(HANDSAW) tie at exactly 1.0; `legal_actions` orders the shot first,
    so the top-ranked recommendation is the plain shot, not the saw.
    """
    chamber = ChamberState.create((None,), 1, 0)
    state = GameState.create(
        chamber=chamber,
        hp=(3, 1),
        max_hp=3,
        turn=Player.PLAYER,
        items=(ItemCounts.of(handsaw=1), ItemCounts.empty()),
    )
    return CorpusEntry(
        name="handsaw_not_decisive_already_lethal",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.HAND_COMPUTED,
        rationale=(
            "One shell, guaranteed live, Dealer already at 1 HP: base_damage "
            "alone is already lethal, so the saw's extra point of damage is "
            "provably inert (HP clamps at 0 either way). Both SHOOT_OPPONENT "
            "and USE(HANDSAW) are exact terminal wins tied at 1.0; the shot "
            "sorts first, so the recommendation is the plain shot, not the "
            "saw."
        ),
        expected_action=Action(ActionKind.SHOOT_OPPONENT),
        expected_value=1.0,
    )


def _entry_dealer_glass_belief_mixture(rules: Rules) -> CorpusEntry:
    """docs/dealer-knowledge.md's mandatory entry: a position where the Dealer
    has glassed slot 0 and we have not, whose value must equal the
    belief-weighted mixture of the two observable continuations — computed by
    hand, not by the solver.

    Position: one live, one blank shell (both unresolved for us),
    `state.dealer_knows = {0}` (the Dealer privately resolved slot 0, e.g.
    via its own Magnifying Glass), Dealer to move, hp=(2,3), max_hp=3,
    player holds one Cigarette, Dealer holds nothing.

    `dealer_step` opens the lazy chance node described in
    docs/dealer-knowledge.md: our belief `p_live = 0.5` weights two branches,
    resolving slot 0 for us too inside each.

    **Blank branch (weight 0.5).** The Dealer's known-blank shot targets
    itself (SPEC.md §3.3, deterministic — no coinflip once known); it misses
    (harmless), retains its turn (blank self-shot), and the one remaining
    shell is forced LIVE by count-closure. Still the Dealer's turn: it now
    knows that shell is live too (public closure) and shoots the player for
    `base_damage=1`: player 2->1. The chamber is now empty (both shells
    consumed within the Dealer's own turn) ->
    `reload_value(1, 3, 3) = 1/4 = 0.25`.

    **Live branch (weight 0.5).** The Dealer's known-live shot targets the
    player immediately: player 2->1. SHOOT_OPPONENT always ends the turn;
    one BLANK shell (forced by closure) remains, so the chamber is *not*
    empty — the Dealer is not cuffed, so the turn passes to the PLAYER. This
    is the crux of the entry: the player's decision here is legitimately
    informed by having just been shot with a live round from the previously
    "secret" slot — SPEC.md's disclosure argument (docs/dealer-knowledge.md:
    "every Dealer behaviour whose condition depends on the hidden value
    discloses it through the resulting observation") means this is *not*
    strategy fusion; the player is entitled to condition on it. At hp=1
    (below `max_hp=3`), the player's best move is USE(CIGARETTES) (heal to
    2) before finishing the harmless known-blank shell:
    `reload_value(2, 3, 3) = 2/5 = 0.4` — strictly better than skipping the
    heal (`reload_value(1, 3, 3) = 0.25`).

    **Mixture:** `0.5 * 0.25 + 0.5 * 0.4 = 0.325`. A strategy-fusion bug
    (letting the player condition on the secret before it is actually
    disclosed, or otherwise miscomputing the branch weights) would not, in
    general, reproduce this number.
    """
    chamber = ChamberState.create((None, None), 1, 1)
    state = GameState.create(
        chamber=chamber,
        hp=(2, 3),
        max_hp=3,
        turn=Player.DEALER,
        items=(ItemCounts.of(cigarettes=1), ItemCounts.empty()),
        dealer_knows=frozenset({0}),
    )
    return CorpusEntry(
        name="dealer_glassed_slot_belief_weighted_mixture",
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.HAND_COMPUTED,
        rationale=(
            "docs/dealer-knowledge.md's mandatory entry. 1 live + 1 blank, "
            "Dealer privately glassed slot 0 (dealer_knows={0}, unresolved "
            "for us). Blank branch (p=0.5): Dealer misses itself, then "
            "shoots the (now count-closed) known-live player for 1 dmg, "
            "chamber empties within the Dealer's own turn -> "
            "reload_value(1,3,3)=0.25. Live branch (p=0.5): Dealer shoots "
            "the player for 1 dmg immediately, one known-blank shell "
            "remains, turn passes to the player who — legitimately, since "
            "getting shot discloses which branch this is — heals with "
            "Cigarettes before the harmless last shot -> "
            "reload_value(2,3,3)=0.4. Mixture: 0.5*0.25+0.5*0.4=0.325. Full "
            "derivation in this function's docstring. Crosses a reload "
            "boundary on both branches — stub table only."
        ),
        expected_value=0.325,
    )


def _entry_beer_dealer_hands(
    rules: Rules, *, with_beer: bool
) -> CorpusEntry:
    """SPEC.md §3.3: "Beer in the Dealer's hands usually helps you... its
    ejections reveal shells and shorten the round, returning first-move
    advantage to you sooner." Comparative claim: matched positions differing
    only in whether the Dealer holds a Beer.

    Position: one live, one blank (unresolved), Dealer to move, hp=(3,3),
    max_hp=3, neither side holds anything else in the no-beer variant; the
    Dealer additionally holds one Beer in the with-beer variant.

    **No beer.** Counts are tied (1 live = 1 blank), so `_shot_distribution`
    flips `rules.dealer.unknown_shell_equal_coinflip` (0.5) for the TARGET,
    independently of the true (also 50/50) shell draw — four equally-weighted
    (0.25 each) combined outcomes:
    - (target=player, actual=live): player takes 1 dmg, one blank shell
      remains known, turn passes to the player -> both of the player's
      actions there tie (a known blank does nothing to anyone regardless of
      target) at `reload_value(2, 3, 3) = 0.4`.
    - (target=player, actual=blank): SHOOT_OPPONENT always ends the turn
      regardless of outcome; one live shell remains known, turn passes to
      the player, who shoots the known-live Dealer ->
      `reload_value(3, 2, 3) = 0.6`.
    - (target=self, actual=live): the Dealer takes 1 dmg, one blank remains
      known, turn passes to the player -> `reload_value(3, 2, 3) = 0.6`.
    - (target=self, actual=blank): the Dealer misses, retains its turn (blank
      self-shot), the one remaining shell is forced LIVE by closure, and the
      Dealer (still moving) now knows it and shoots the player ->
      `reload_value(2, 3, 3) = 0.4`.
    Average: `0.5*0.4 + 0.5*0.6 = 0.5`.

    **With beer.** `_condition_fires(BEER, ...)` is `slot0 != LIVE`, which is
    true whenever slot0 is unresolved — Beer therefore fires unconditionally
    here, *before* the shot decision is ever reached (it precedes Adrenaline/
    Inverter/Handsaw, and there is nothing earlier in `rules.dealer.
    item_priority` for this position to consume). It removes the Dealer's
    50/50 self-target coinflip entirely, for better or worse:
    - Blank draw (0.5): the ejected shell was blank; the one remaining shell
      is forced LIVE by closure; turn retained (Beer retains the turn), and
      the Dealer — now knowing it's live — shoots the player deterministically
      (no coinflip: the shell is known) -> `reload_value(2, 3, 3) = 0.4`.
    - Live draw (0.5): the ejected shell was live — a wasted shot (SPEC.md
      §3.3's second listed exploit: "if the Dealer ejects a live shell with
      beer, it did not know the shell was live"); the remaining shell is
      forced BLANK by closure; the Beer is now spent, so the walk falls
      through to the shot decision on a *known* blank -> deterministic
      SHOOT_SELF, a harmless miss, turn retained, chamber empties ->
      `reload_value(3, 3, 3) = 0.5`.
    Average: `0.5*0.4 + 0.5*0.5 = 0.45`.

    **Finding, not a passing assertion:** `0.45 < 0.5` — in this exact,
    tied-count position, Beer in the Dealer's hands *hurts* the player. This
    is the opposite of SPEC.md §3.3's stated direction. See the M6 report:
    it is not a solver bug (both numbers are verified by hand and match the
    solver exactly); it is a genuine counter-example to "usually helps" that
    the M6 brief explicitly anticipated ("if it does not hold in the specific
    position you build, that is a finding to report, not a test to tune until
    it passes"). Both matched values are still scored as exact hand-computed
    entries — the *entries* agree with the solver; the *SPEC's directional
    claim* does not hold universally, which is the finding.
    """
    chamber = ChamberState.create((None, None), 1, 1)
    items = (
        (ItemCounts.empty(), ItemCounts.of(beer=1))
        if with_beer
        else (ItemCounts.empty(), ItemCounts.empty())
    )
    state = GameState.create(
        chamber=chamber, hp=(3, 3), max_hp=3, turn=Player.DEALER, items=items
    )
    name = (
        "beer_dealer_hands_tied_count_with_beer"
        if with_beer
        else "beer_dealer_hands_tied_count_no_beer"
    )
    expected_value = 0.45 if with_beer else 0.5
    return CorpusEntry(
        name=name,
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.HAND_COMPUTED,
        rationale=(
            "SPEC.md §3.3's comparative claim: 'Beer in the Dealer's hands "
            "usually helps you.' Matched pair (this entry is the "
            + ("with-beer" if with_beer else "no-beer baseline")
            + " half) at 1 live + 1 blank, Dealer to move, hp=(3,3). "
            "No-beer: the Dealer's tied-count coinflip on target is "
            "independent of the true draw, averaging to 0.5 across four "
            "equally-weighted outcomes. With beer: Beer fires unconditionally "
            "on any unresolved slot0, before the shot decision, removing "
            "that coinflip; it averages to 0.45 (a wasted live-shell "
            "ejection half the time is worse for the Dealer's win chances "
            "than the coinflip it replaces, in THIS exact position). Full "
            "derivation in this function's docstring. Result: beer HURTS "
            "the player here — the opposite of SPEC's 'usually helps' — a "
            "reported finding, not a tuned assertion; see the M6 report. "
            "Crosses a reload boundary on every branch — stub table only."
        ),
        expected_value=expected_value,
    )


def _entry_beer_dealer_hands_majority_live(
    rules: Rules, *, with_beer: bool
) -> CorpusEntry:
    """A second, larger matched pair (2 live + 1 blank, majority already
    favours shooting the player even without Beer) where Beer's direction
    flips back to SPEC.md §3.3's stated "usually helps" — included as a
    `SOLVER_DERIVED` regression pin, **not** hand-verified: the 3-shell
    Dealer/player recursion here is long enough that a by-hand derivation
    risks the exact kind of error this milestone is trying to catch, and
    doing it properly is out of scope for this pass (see the M6 report).
    Kept anyway because it is the illustration that "usually helps" *does*
    hold somewhere nearby the counter-example above — the direction is
    position-dependent, not simply wrong.
    """
    chamber = ChamberState.create((None, None, None), 2, 1)
    items = (
        (ItemCounts.empty(), ItemCounts.of(beer=1))
        if with_beer
        else (ItemCounts.empty(), ItemCounts.empty())
    )
    state = GameState.create(
        chamber=chamber, hp=(3, 3), max_hp=3, turn=Player.DEALER, items=items
    )
    name = (
        "beer_dealer_hands_majority_live_with_beer"
        if with_beer
        else "beer_dealer_hands_majority_live_no_beer"
    )
    expected_value = 0.5 if with_beer else 5.0 / 12.0
    return CorpusEntry(
        name=name,
        state=state,
        mode=GameMode.BASE,
        provenance=Provenance.SOLVER_DERIVED,
        rationale=(
            "Regression pin, NOT independently verified — see this "
            "function's docstring. 2 live + 1 blank, Dealer to move, "
            "hp=(3,3), majority already favours shooting the player "
            "(deterministic, no coinflip) even without Beer. Solver-computed "
            "value: "
            + ("with beer = 0.5" if with_beer else "no beer = 5/12 ≈ 0.4167")
            + f" ({'with' if with_beer else 'without'} Beer). Paired with "
            "beer_dealer_hands_majority_live_"
            + ("no_beer" if with_beer else "with_beer")
            + f", this pair shows beer {'helping' if with_beer else 'the baseline'} "
            "in a majority-live position — the opposite direction from the "
            "tied-count counter-example above, i.e. SPEC's 'usually helps' "
            "direction depends on the position. Crosses a reload boundary "
            "on every branch — stub table only."
        ),
        expected_value=expected_value,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def corpus(rules: Rules) -> tuple[CorpusEntry, ...]:
    """The full M6 agreement corpus, built from `rules` (SPEC.md §12.2).

    Callers that want the mandatory "wasted redundant item" entry to actually
    exercise `wasted=True` must pass a `rules` whose `solver.
    max_items_per_turn` is tight enough to bind (>=1 suffices) — see
    `_corpus_rules`, which `run_corpus` and `main` apply by default.
    """
    return (
        _entry_handcuffs_more_blanks_than_lives(rules),
        _entry_wasted_magnifying_glass(rules),
        _entry_handsaw_decisive(rules),
        _entry_handsaw_not_decisive(rules),
        _entry_dealer_glass_belief_mixture(rules),
        _entry_beer_dealer_hands(rules, with_beer=False),
        _entry_beer_dealer_hands(rules, with_beer=True),
        _entry_beer_dealer_hands_majority_live(rules, with_beer=False),
        _entry_beer_dealer_hands_majority_live(rules, with_beer=True),
    )


def check(entry: CorpusEntry, solver: Solver, rules: Rules) -> CheckResult:
    """Check one `CorpusEntry` against a live `Solver` (SPEC.md §12.2)."""
    result = solver.evaluate(entry.state)
    actual_action: Action | None = result.children[0].action if result.children else None
    actual_value = result.value

    problems: list[str] = []
    if entry.expected_action is not None:
        if actual_action != entry.expected_action:
            problems.append(
                f"expected top action {entry.expected_action}, got {actual_action}"
            )
    if entry.expected_value is not None:
        if abs(actual_value - entry.expected_value) > entry.tolerance:
            problems.append(
                f"expected value {entry.expected_value!r} +/- {entry.tolerance!r}, "
                f"got {actual_value!r}"
            )

    agreed = not problems
    detail = "; ".join(problems)
    return CheckResult(
        entry=entry,
        agreed=agreed,
        actual_action=actual_action,
        actual_value=actual_value,
        detail=detail,
    )


def run_corpus(rules: Rules) -> tuple[CheckResult, ...]:
    """Check every entry in `corpus(rules)` (SPEC.md §12.2).

    Builds one `Solver` per distinct `GameMode` present in the corpus, each
    with the `_health_ratio_reload_table` stand-in injected directly (never
    the real, slow fixed point — see the module docstring).
    """
    entries = corpus(rules)
    solvers: dict[GameMode, Solver] = {}
    results = []
    for entry in entries:
        solver = solvers.get(entry.mode)
        if solver is None:
            solver = Solver(rules, entry.mode)
            solver._reload_table = _health_ratio_reload_table(rules)
            solvers[entry.mode] = solver
        results.append(check(entry, solver, rules))
    return tuple(results)


def agreement_rate(results: Sequence[CheckResult]) -> float:
    """Fraction of *scored* results that agreed (SPEC.md §12.2).

    `Provenance.SOLVER_DERIVED` entries are excluded — they are regression
    pins, not independent evidence, and including them would let a padded
    corpus of solver-echoing entries inflate the rate exactly the way the M6
    brief warns against.
    """
    scored = [r for r in results if r.entry.provenance in _SCORED_PROVENANCE]
    if not scored:
        raise ValueError("agreement_rate: no scored (non-SOLVER_DERIVED) results")
    return sum(1 for r in scored if r.agreed) / len(scored)


def main() -> None:
    """Harness: run the corpus against the shipped `rules.toml` and report."""
    rules = _corpus_rules(default_rules())
    results = run_corpus(rules)
    scored = [r for r in results if r.entry.provenance in _SCORED_PROVENANCE]
    pinned = [r for r in results if r.entry.provenance not in _SCORED_PROVENANCE]

    rate = agreement_rate(results)
    print(f"Agreement: {rate:.1%} over {len(scored)} scored entries "
          f"({len(pinned)} SOLVER_DERIVED regression pins excluded)")
    for r in results:
        tag = (
            "SOLVER_DERIVED (pin)"
            if r.entry.provenance is Provenance.SOLVER_DERIVED
            else "scored"
        )
        status = "OK" if r.agreed else "DISAGREE"
        print(f"[{status}] ({tag}) {r.entry.name}: value={r.actual_value:.6f} "
              f"action={r.actual_action}")
        if not r.agreed:
            print(f"    {r.detail}")
    if rate < AGREEMENT_THRESHOLD:
        print(f"BELOW THRESHOLD: {rate:.1%} < {AGREEMENT_THRESHOLD:.0%}")


if __name__ == "__main__":
    main()
