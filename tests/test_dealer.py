"""Tests for buckshotcalc.engine.dealer (SPEC.md §3.3, §3.4, M3 acceptance)."""

from __future__ import annotations

import random
import tomllib
from pathlib import Path

import pytest

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.dealer import dealer_step, dealer_view
from buckshotcalc.engine.rules import Rules, default_rules, load_rules
from buckshotcalc.engine.state import (
    ChamberState,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from buckshotcalc.sim.tables import build_solver

RULES = default_rules()
REAL_RULES_PATH = Path(__file__).resolve().parent.parent / "buckshotcalc" / "rules.toml"


def make_state(
    slots,
    unknown_live,
    unknown_blank,
    hp=(3, 3),
    max_hp=3,
    turn=Player.DEALER,
    items=None,
    sawed=False,
    cuffed=(False, False),
    dealer_knows=frozenset(),
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
    )


def dealer_items(**by_config_name: int) -> tuple[ItemCounts, ItemCounts]:
    """(player items empty, dealer items as given)."""
    return (ItemCounts.empty(), ItemCounts.of(**by_config_name))


def with_player_items(dealer_kwargs: dict, player_kwargs: dict) -> tuple[ItemCounts, ItemCounts]:
    return (ItemCounts.of(**player_kwargs), ItemCounts.of(**dealer_kwargs))


def sequence_weights(
    state: GameState, rules: Rules = RULES
) -> dict[tuple[Action, ...], float]:
    """Total weight per distinct action *sequence* across `dealer_step`'s branches.

    Branches carry sequences because the handsaw and its shot are one atomic
    step (see `dealer.py`'s module docstring); every other decision is a
    one-element sequence.
    """
    branches = dealer_step(state, rules)
    assert sum(w for w, _, _ in branches) == pytest.approx(1.0)
    by_seq: dict[tuple[Action, ...], float] = {}
    for w, actions, _ in branches:
        by_seq[actions] = by_seq.get(actions, 0.0) + w
    return by_seq


def only_action(state: GameState, rules: Rules = RULES):
    """dealer_step's branches, asserting every branch is the same single Action."""
    branches = dealer_step(state, rules)
    total = sum(w for w, _, _ in branches)
    assert total == pytest.approx(1.0)
    sequences = {a for _, a, _ in branches}
    assert len(sequences) == 1, f"expected one action across branches, got {sequences}"
    actions = sequences.pop()
    assert len(actions) == 1, f"expected a single action, got the sequence {actions}"
    return actions[0], branches


def action_weights(state: GameState, rules: Rules = RULES) -> dict[Action, float]:
    """Total weight per distinct `Action`, for states with no atomic sequences."""
    by_seq = sequence_weights(state, rules)
    assert all(len(seq) == 1 for seq in by_seq), (
        f"action_weights: state produced multi-action sequences {list(by_seq)}; "
        "use sequence_weights instead"
    )
    return {seq[0]: w for seq, w in by_seq.items()}


# ---------------------------------------------------------------------------
# §7.3 leak test — the important one.
# ---------------------------------------------------------------------------


def test_leak_test_own_glass_does_not_inform_the_dealer():
    """docs/dealer-knowledge.md addendum's worked example.

    Load L1 B2. We glass slot 0: LIVE. Our own `deduce()` then resolves the
    whole chamber for us. The Dealer knows only the multiset {L, B, B} and
    nothing else — its p_live on slot 0 must still be exactly 1/3, and it
    must not "know" the chambered shell. An implementation that reads
    `state.chamber.slots` to decide Dealer knowledge fails this.
    """
    chamber = ChamberState.create((Shell.LIVE, Shell.BLANK, Shell.BLANK), 0, 0)
    state = GameState.create(chamber, (3, 3), 3, Player.DEALER, dealer_knows=frozenset())

    # Sanity: our own knowledge really is total (the glass-then-deduce effect).
    assert all(s is not None for s in state.chamber.slots)

    view = dealer_view(state, RULES)
    assert view.slots[0] is None, "the Dealer must not know the chambered shell"
    assert view.p_live() == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# knows_shell_when_one_remains falls out of closure, no special-casing.
# ---------------------------------------------------------------------------


def test_dealer_counts_the_true_shells_after_an_unseen_inverter():
    """L1 B1 with slot 0 inverted: both shells now match, and the Dealer counts the
    true sequence (FigureOutShell, DealerIntelligence.gd:282-303), so it knows."""
    state = make_state((None, None), 1, 1)
    state = state.with_chamber(ChamberState((None, None), 1, 1, inverted0=True))
    weights = action_weights(state)
    assert weights == {
        Action(ActionKind.SHOOT_OPPONENT): pytest.approx(0.5),
        Action(ActionKind.SHOOT_SELF): pytest.approx(0.5),
    }
    for _, actions, succ in dealer_step(state, RULES):
        fired_live = succ.hp != state.hp
        assert fired_live == (actions[-1].kind is ActionKind.SHOOT_OPPONENT)


def test_one_shell_left_live_closure_gives_certain_shot_at_player():
    state = make_state((None,), unknown_live=1, unknown_blank=0)
    view = dealer_view(state, RULES)
    assert view.slots[0] == Shell.LIVE
    action, _ = only_action(state)
    assert action == Action(ActionKind.SHOOT_OPPONENT)


def test_one_shell_left_blank_closure_gives_certain_self_shot():
    state = make_state((None,), unknown_live=0, unknown_blank=1)
    view = dealer_view(state, RULES)
    assert view.slots[0] == Shell.BLANK
    action, _ = only_action(state)
    assert action == Action(ActionKind.SHOOT_SELF)


# ---------------------------------------------------------------------------
# Shooting decision / exploitable consequences (SPEC.md §3.3)
# ---------------------------------------------------------------------------


def test_more_blanks_than_lives_shoots_self():
    """SPEC.md §3.3 exploit: with more blanks than lives, the Dealer shoots itself."""
    state = make_state((None, None, None), unknown_live=1, unknown_blank=2)
    action, _ = only_action(state)
    assert action == Action(ActionKind.SHOOT_SELF)


def test_more_lives_than_blanks_shoots_player():
    state = make_state((None, None, None), unknown_live=2, unknown_blank=1)
    action, _ = only_action(state)
    assert action == Action(ActionKind.SHOOT_OPPONENT)


# ---------------------------------------------------------------------------
# Mode gating (correctness-patch item 1) — the double_or_nothing path must
# stay byte-for-byte identical; the campaign path must differ.
# ---------------------------------------------------------------------------


def test_double_or_nothing_majority_rule_unchanged(tmp_path: Path):
    """v1's shipped mode: this must be exactly today's behaviour."""
    rules = _rules_with_mode(tmp_path, "double_or_nothing")
    assert rules.is_endless is True
    more_live = make_state((None, None, None), unknown_live=2, unknown_blank=1)
    action, _ = only_action(more_live, rules)
    assert action == Action(ActionKind.SHOOT_OPPONENT)

    more_blank = make_state((None, None, None), unknown_live=1, unknown_blank=2)
    action, _ = only_action(more_blank, rules)
    assert action == Action(ActionKind.SHOOT_SELF)


def test_campaign_mode_ignores_majority_and_is_a_flat_coinflip(tmp_path: Path):
    """The campaign path must differ: no majority rule, flat 50/50 regardless
    of the live/blank split. Fails against code that does not gate
    `_shot_distribution` on `rules.is_endless`."""
    rules = _rules_with_mode(tmp_path, "base")
    assert rules.is_endless is False

    more_live = make_state((None, None, None), unknown_live=2, unknown_blank=1)
    weights = action_weights(more_live, rules)
    p = rules.dealer.unknown_shell_equal_coinflip
    assert weights[Action(ActionKind.SHOOT_OPPONENT)] == pytest.approx(p)
    assert weights[Action(ActionKind.SHOOT_SELF)] == pytest.approx(1.0 - p)

    more_blank = make_state((None, None, None), unknown_live=1, unknown_blank=2)
    weights = action_weights(more_blank, rules)
    assert weights[Action(ActionKind.SHOOT_OPPONENT)] == pytest.approx(p)
    assert weights[Action(ActionKind.SHOOT_SELF)] == pytest.approx(1.0 - p)


def test_campaign_mode_disables_general_count_closure_beyond_one_shell(tmp_path: Path):
    """The general (`n > 1`) count-closure deduction is endless-only. Load
    L2 B0 across 2 slots (pool exhausted of blank at n=2): in
    double_or_nothing the Dealer deduces the chambered shell from the
    exhausted blank pool via public totals alone; in the base campaign it
    does not, and stays genuinely ignorant of a shell it never resolved
    itself, even though the same public totals are available to it."""
    chamber = ChamberState.create((None, None), 2, 0)
    state = GameState.create(chamber, (3, 3), 3, Player.DEALER)

    don_rules = _rules_with_mode(tmp_path, "double_or_nothing")
    view_don = dealer_view(state, don_rules)
    assert view_don.slots[0] == Shell.LIVE  # closed via count-closure

    second_dir = tmp_path / "campaign"
    second_dir.mkdir()
    campaign_rules = _rules_with_mode(second_dir, "base")
    view_campaign = dealer_view(state, campaign_rules)
    assert view_campaign.slots[0] is None  # NOT closed: base campaign is count-blind
    assert view_campaign.p_live() == pytest.approx(1.0)  # still derivable from the pool alone


def test_one_shell_remaining_closure_is_unconditional_across_modes(tmp_path: Path):
    """`knows_shell_when_one_remains` is deliberately NOT endless-gated: a
    single remaining shell is knowable in both modes."""
    chamber = ChamberState.create((None,), 0, 1)
    state = GameState.create(chamber, (3, 3), 3, Player.DEALER)

    don_rules = _rules_with_mode(tmp_path, "double_or_nothing")
    assert dealer_view(state, don_rules).slots[0] == Shell.BLANK

    second_dir = tmp_path / "campaign"
    second_dir.mkdir()
    campaign_rules = _rules_with_mode(second_dir, "base")
    assert dealer_view(state, campaign_rules).slots[0] == Shell.BLANK


def test_equal_unknown_is_a_coinflip():
    state = make_state((None, None), unknown_live=1, unknown_blank=1)
    weights = action_weights(state)
    assert set(weights) == {Action(ActionKind.SHOOT_SELF), Action(ActionKind.SHOOT_OPPONENT)}
    p = RULES.dealer.unknown_shell_equal_coinflip
    assert weights[Action(ActionKind.SHOOT_OPPONENT)] == pytest.approx(p)
    assert weights[Action(ActionKind.SHOOT_SELF)] == pytest.approx(1.0 - p)


# ---------------------------------------------------------------------------
# Per-item IF-AND-ONLY-IF conditions
# ---------------------------------------------------------------------------


def test_handcuffs_always_fires_when_held_and_opponent_uncuffed():
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(handcuffs=1)
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.HANDCUFFS)


def test_handcuffs_does_not_fire_when_opponent_already_cuffed():
    state = make_state(
        (None, None),
        unknown_live=1,
        unknown_blank=1,
        items=dealer_items(handcuffs=1),
        cuffed=(True, False),
    )
    weights = action_weights(state)
    assert Action(ActionKind.USE, Item.HANDCUFFS) not in weights


def test_handcuffs_does_not_fire_on_the_last_shell():
    """Regression for correctness-patch item 2: fails against the code before
    the size guard, which fired unconditionally regardless of shells
    remaining. Verbatim source: `!playerCuffed && size != 1`."""
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, items=dealer_items(handcuffs=1)
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.HANDCUFFS)


def test_cigarettes_fire_below_max_hp():
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 2),
        max_hp=3,
        items=dealer_items(cigarettes=1),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.CIGARETTES)


def test_cigarettes_do_not_fire_at_max_hp():
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 3),
        max_hp=3,
        items=dealer_items(cigarettes=1),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.CIGARETTES)


def test_medicine_fires_below_max_hp_and_hp_at_least_two():
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 2),
        max_hp=3,
        items=dealer_items(expired_medicine=1),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.EXPIRED_MEDICINE)


def test_medicine_never_risks_self_kill_at_hp_one():
    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 1),
        max_hp=3,
        items=dealer_items(expired_medicine=1),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.EXPIRED_MEDICINE)


def test_medicine_does_not_fire_while_dealer_holds_cigarettes():
    """Regression for correctness-patch item 3: fails against the code before
    the `!hasCigs` conjunct, which fired medicine here regardless of whether
    the Dealer held cigarettes. Verbatim source: `... && !hasCigs && ...`.

    Calls `_condition_fires` directly rather than going through
    `dealer_step`'s priority walk: under the *shipped* `item_priority`
    (cigarettes strictly before expired_medicine), holding cigarettes would
    always make the priority walk fire cigarettes first regardless of this
    guard, since cigarettes' own condition (`hp < max_hp`) is a strict subset
    of medicine's — the guard would never be reached from the top. Calling
    the predicate directly isolates the guard itself from that (separate,
    also-real) reachability finding. See
    `tests/test_solver.py::test_medicine_never_fires_while_dealer_holds_cigarettes`
    for a `dealer_step`-level demonstration using a non-default priority
    order, and the milestone report for why the default order matters here.
    """
    from buckshotcalc.engine.dealer import _condition_fires

    state = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 2),
        max_hp=3,
        items=dealer_items(expired_medicine=1, cigarettes=1),
    )
    view = dealer_view(state, RULES)
    assert _condition_fires(Item.EXPIRED_MEDICINE, state, view, RULES) is False

    # Sanity: without the cigarettes held, the same HP position DOES want it.
    state_no_cigs = make_state(
        (None,),
        unknown_live=1,
        unknown_blank=0,
        hp=(3, 2),
        max_hp=3,
        items=dealer_items(expired_medicine=1),
    )
    view_no_cigs = dealer_view(state_no_cigs, RULES)
    assert _condition_fires(Item.EXPIRED_MEDICINE, state_no_cigs, view_no_cigs, RULES) is True


def test_glass_fires_when_chambered_shell_unknown():
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(magnifying_glass=1)
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.MAGNIFYING_GLASS)


def test_glass_does_not_fire_once_dealer_already_knows():
    """Dealer_knows={0} with the value resolved for us too (simulating a prior
    Dealer glass use whose answer we then observed)."""
    state = make_state(
        (Shell.LIVE, None),
        unknown_live=0,
        unknown_blank=1,
        items=dealer_items(magnifying_glass=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.MAGNIFYING_GLASS)


def test_burner_phone_fires_when_at_least_three_shells_remain():
    """Verbatim source condition: `size > 2` (correctness-patch item 4)."""
    state = make_state(
        (None, None, None),
        unknown_live=1,
        unknown_blank=2,
        items=dealer_items(burner_phone=1),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.BURNER_PHONE)


def test_burner_phone_does_not_fire_with_no_future_slot():
    state = make_state((None,), unknown_live=1, unknown_blank=0, items=dealer_items(burner_phone=1))
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.BURNER_PHONE)


def test_burner_phone_does_not_fire_at_exactly_two_shells():
    """Regression for the old (wrong) condition: this fails against the code
    before correctness-patch item 4, which fired here because a future slot
    (index 1) is unresolved — the real, source-verified condition is `size >
    2`, so two shells remaining must NOT fire the phone regardless of what
    that one future slot's state is."""
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(burner_phone=1)
    )
    weights = action_weights(state)
    assert Action(ActionKind.USE, Item.BURNER_PHONE) not in weights


def test_burner_phone_fires_even_when_the_only_future_slot_is_already_resolved():
    """`size > 2` has nothing to do with whether a future slot is resolved —
    verbatim source, unlike our old ASSUMED analogy with the glass."""
    state = make_state(
        (None, Shell.BLANK, None),
        unknown_live=1,
        unknown_blank=1,
        items=dealer_items(burner_phone=1),
        dealer_knows=frozenset({1}),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.BURNER_PHONE)


def test_beer_fires_when_unknown():
    state = make_state((None, None), unknown_live=1, unknown_blank=1, items=dealer_items(beer=1))
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.BEER)


def test_beer_fires_when_knows_blank():
    state = make_state(
        (Shell.BLANK, None),
        unknown_live=1,
        unknown_blank=0,
        items=dealer_items(beer=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.BEER)


def test_beer_does_not_fire_when_knows_live():
    state = make_state(
        (Shell.LIVE, None),
        unknown_live=0,
        unknown_blank=1,
        items=dealer_items(beer=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.BEER)


def test_beer_does_not_fire_on_the_last_shell_even_when_known_blank():
    """Regression for correctness-patch item 2: fails against the code before
    the size guard, which fired unconditionally on `slot0 != LIVE` regardless
    of shells remaining. Verbatim source: `knownShell != "live" && size != 1`
    — beer never fires on the last shell."""
    state = make_state(
        (Shell.BLANK,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(beer=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.BEER)


def test_inverter_fires_only_when_knows_blank():
    state = make_state(
        (Shell.BLANK,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(inverter=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.INVERTER)


def test_inverter_does_not_fire_when_unknown():
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(inverter=1)
    )
    weights = action_weights(state)
    assert Action(ActionKind.USE, Item.INVERTER) not in weights


def test_inverter_does_not_fire_when_knows_live():
    state = make_state(
        (Shell.LIVE,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(inverter=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.INVERTER)


def test_inverter_known_by_counting_leaves_dealer_knowing_live():
    """All shells blank: the Dealer knows slot 0 by counting, not `dealer_knows`.
    After its Inverter it must still know slot 0 (now live) and shoot us, not
    fall back to the majority rule on the broken count and shoot itself."""
    state = make_state(
        (Shell.BLANK, Shell.BLANK, Shell.BLANK),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(inverter=1),
    )
    action, branches = only_action(state)
    assert action == Action(ActionKind.USE, Item.INVERTER)
    (_, _, successor), = branches
    assert dealer_view(successor, RULES).slots[0] is Shell.LIVE
    next_action, _ = only_action(successor)
    assert next_action == Action(ActionKind.SHOOT_OPPONENT)


def test_handsaw_fires_when_deterministically_shooting_player():
    state = make_state(
        (Shell.LIVE,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(handsaw=1),
        dealer_knows=frozenset({0}),
    )
    weights = sequence_weights(state)
    # The saw and the shot it was taken for are emitted as one atomic step.
    assert weights == {
        (Action(ActionKind.USE, Item.HANDSAW), Action(ActionKind.SHOOT_OPPONENT)): 1.0
    }


def test_handsaw_does_not_fire_when_shooting_self():
    state = make_state(
        (Shell.BLANK,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(handsaw=1),
        dealer_knows=frozenset({0}),
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.SHOOT_SELF)


SAW = Action(ActionKind.USE, Item.HANDSAW)


def test_handsaw_coinflip_case_splits_consistently():
    """Ambiguous (equal unknown) case: the coinflip is resolved ONCE.

    With weight p the Dealer commits to shooting the player, so it saws and
    shoots the player in one atomic step. With weight 1-p it commits to
    shooting itself, so the saw never fires at all (SPEC.md §3.3: the saw is
    used IFF it intends to shoot the player).
    """
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(handsaw=1)
    )
    weights = sequence_weights(state)
    p = RULES.dealer.unknown_shell_equal_coinflip
    assert weights.get((SAW, Action(ActionKind.SHOOT_OPPONENT)), 0.0) == pytest.approx(p)
    assert weights.get((Action(ActionKind.SHOOT_SELF),), 0.0) == pytest.approx(1.0 - p)


def test_handsaw_is_never_followed_by_a_self_shot():
    """Regression: the Dealer must never saw and then shoot itself.

    The saw fires only because the intent resolved to "shoot the player". An
    earlier implementation returned the saw and let the next `dealer_step`
    re-derive that intent from scratch, so in the coinflip case the Dealer
    shot *itself* half the time with the saw still active — taking doubled
    self-damage the policy never chose, and inflating P(win) exactly where
    even loads make coinflips common.

    Checked two ways: no emitted sequence pairs a saw with a self-shot, and
    no successor of a sawing branch is left mid-turn holding a live saw.
    """
    state = make_state(
        (None, None), unknown_live=1, unknown_blank=1, items=dealer_items(handsaw=1)
    )
    for _, actions, successor in dealer_step(state, RULES):
        if SAW in actions:
            assert Action(ActionKind.SHOOT_SELF) not in actions, (
                f"Dealer sawed and then shot itself: {actions}"
            )
            assert actions[-1] == Action(ActionKind.SHOOT_OPPONENT)
            # The step is atomic: the saw is spent, never handed back live.
            assert not successor.sawed


def test_post_saw_invariant_no_item_fires_after_the_saw():
    """SPEC.md §3.4 puts the handsaw last because it depends on the shot
    decision every prior step informs. Sawing mutates only `sawed`, which can
    never *enable* a condition — so once the saw fires, the next action is the
    shot, with nothing in between. Asserted directly over a Dealer holding a
    full rack, so every earlier condition has a chance to fire late.
    """
    state = make_state(
        (Shell.LIVE,),
        unknown_live=0,
        unknown_blank=0,
        items=dealer_items(
            handsaw=1, cigarettes=1, handcuffs=1, beer=1, magnifying_glass=1, inverter=1
        ),
        hp=(3, 3),
        max_hp=3,
        dealer_knows=frozenset({0}),
    )
    for _, actions, _ in dealer_step(state, RULES):
        if SAW in actions:
            assert actions[actions.index(SAW) + 1 :] == (
                Action(ActionKind.SHOOT_OPPONENT),
            ), f"an item fired between the saw and the shot: {actions}"


def test_adrenaline_wants_item_whose_condition_fires():
    items = with_player_items({"adrenaline": 1}, {"cigarettes": 1})
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, hp=(3, 2), max_hp=3, items=items
    )
    action, _ = only_action(state)
    assert action == Action(ActionKind.USE, Item.ADRENALINE, Item.CIGARETTES)


def test_adrenaline_does_not_want_item_whose_condition_is_false():
    """Player holds cigarettes, but the Dealer is at max HP: it would not use
    cigarettes itself, so it does not want them via Adrenaline either."""
    items = with_player_items({"adrenaline": 1}, {"cigarettes": 1})
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, hp=(3, 3), max_hp=3, items=items
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.ADRENALINE)


def test_adrenaline_does_not_want_item_already_held():
    """Dealer already holds cigarettes (hp at max so its own condition is
    moot); it must not steal a second copy via Adrenaline."""
    items = with_player_items({"adrenaline": 1}, {"cigarettes": 1, "adrenaline": 1})
    state = make_state(
        (None,), unknown_live=1, unknown_blank=0, hp=(3, 3), max_hp=3, items=items
    )
    action, _ = only_action(state)
    assert action != Action(ActionKind.USE, Item.ADRENALINE)


# ---------------------------------------------------------------------------
# Beer resets the Dealer's knowledge on eject (correctness-patch item 5).
# ---------------------------------------------------------------------------


def test_beer_eject_drops_the_dealers_stale_slot0_knowledge():
    """Item 5: in endless, the Dealer's beer sets `dealerKnowsShell = false`
    after ejecting, because the *new* chambered shell is a different one.

    Verifies we already get this for free by construction, per
    `actions.py::_renumber_dealer_knows` (`frozenset(i - 1 for i in
    dealer_knows if i > 0)`) — index 0 (the ejected slot) is unconditionally
    dropped, never carried forward as index -1 or reinterpreted. This is a
    regression test, not a fix: it would already pass before this patch, and
    is added here because item 5 asked us to confirm it rather than add a
    redundant config key.

    L1 (known, dealer_knows={0}) then B?L? (ambiguous, 1 live + 1 blank in
    the pool, unresolved for anyone) at slots 1-2. After beer ejects slot 0
    (revealing LIVE, as it always does), `dealer_knows` must renumber to the
    empty set — NOT `{-1}` and NOT stay `{0}` pointing at the new (different)
    chambered shell — so the Dealer's view of the new slot 0 is exactly as
    ambiguous as ours: p_live == 0.5, not resolved.
    """
    from buckshotcalc.engine.actions import apply

    chamber = ChamberState.create((Shell.LIVE, None, None), 1, 1)
    state = GameState.create(
        chamber,
        (3, 3),
        3,
        Player.PLAYER,
        items=(ItemCounts.of(beer=1), ItemCounts.empty()),
        dealer_knows=frozenset({0}),
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.BEER), RULES)
    assert len(outcomes) == 1  # slot 0 was already resolved: deterministic pop
    weight, result = outcomes[0]
    assert weight == pytest.approx(1.0)

    assert result.dealer_knows == frozenset()
    view = dealer_view(result, RULES)
    assert view.slots[0] is None
    assert view.p_live() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Lazy branching — hand-computed.
# ---------------------------------------------------------------------------


def test_lazy_branching_hand_computed():
    """0 in dealer_knows, slot 0 unresolved for us: the returned distribution
    must be the belief-weighted mixture of the two observable continuations.

    Chamber L1 B1 (slot 0 unresolved for both). Dealer resolved slot 0 itself
    (e.g. its own Magnifying Glass) but we never observed the answer, so
    `dealer_knows = {0}` while `state.chamber.slots[0] is None`.

    By hand: p_live = 1/2.
      - BLANK branch (weight 0.5): Dealer knows blank -> shoots itself, no
        damage, turn retained (a fresh turn for the same mover).
      - LIVE branch (weight 0.5): Dealer knows live -> shoots the player,
        1 damage, turn passes.
    """
    state = make_state((None, None), unknown_live=1, unknown_blank=1, dealer_knows=frozenset({0}))
    branches = dealer_step(state, RULES)
    assert sum(w for w, _, _ in branches) == pytest.approx(1.0)

    by_action: dict[Action, list] = {}
    for w, actions, s in branches:
        assert len(actions) == 1, f"expected single actions here, got {actions}"
        by_action.setdefault(actions[0], []).append((w, s))

    assert set(by_action) == {Action(ActionKind.SHOOT_SELF), Action(ActionKind.SHOOT_OPPONENT)}

    (w_self, s_self) = by_action[Action(ActionKind.SHOOT_SELF)][0]
    assert w_self == pytest.approx(0.5)
    assert s_self.hp == (3, 3)  # blank: no damage
    assert s_self.turn == Player.DEALER  # retained (fresh turn, same mover)

    (w_opp, s_opp) = by_action[Action(ActionKind.SHOOT_OPPONENT)][0]
    assert w_opp == pytest.approx(0.5)
    assert s_opp.hp == (3 - RULES.turns.base_damage, 3)  # live: player damaged
    assert s_opp.turn == Player.PLAYER  # turn passes


# ---------------------------------------------------------------------------
# Priority order is config-driven.
# ---------------------------------------------------------------------------


def _dump_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(_dump_value(v) for v in value) + "]"
    raise TypeError(f"cannot dump {value!r}")


def _dump_toml(data: dict, prefix: str = "") -> str:
    lines = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_dump_value(value)}")
    for key, value in tables.items():
        full_key = f"{prefix}.{key}" if prefix else key
        lines.append(f"\n[{full_key}]")
        lines.append(_dump_toml(value, full_key))
    return "\n".join(lines)


def _rules_with_priority(tmp_path: Path, priority: list[str]):
    with REAL_RULES_PATH.open("rb") as f:
        data = tomllib.load(f)
    data["dealer"]["item_priority"] = priority
    path = tmp_path / "rules.toml"
    path.write_text(_dump_toml(data))
    return load_rules(path)


def _rules_with_mode(tmp_path: Path, mode: str) -> Rules:
    """Load a copy of the real `rules.toml` with `[mode].mode` overridden.

    Used only to prove the campaign path differs (correctness-patch item 1)
    — v1 ships `double_or_nothing`, and this must stay unconditionally what
    it is today; only the *conditionality* is new.
    """
    with REAL_RULES_PATH.open("rb") as f:
        data = tomllib.load(f)
    data["mode"]["mode"] = mode
    path = tmp_path / "rules.toml"
    path.write_text(_dump_toml(data))
    return load_rules(path)


def test_priority_order_is_config_driven(tmp_path: Path):
    # Both handcuffs and cigarettes conditions fire. Two shells remaining so
    # the handcuffs size guard (item 2 of the correctness patch) does not
    # itself suppress handcuffs and collapse this into a single-choice test.
    state = make_state(
        (None, None),
        unknown_live=1,
        unknown_blank=1,
        hp=(3, 2),
        max_hp=3,
        items=dealer_items(handcuffs=1, cigarettes=1),
    )

    handcuffs_first = _rules_with_priority(
        tmp_path,
        [
            "handcuffs",
            "cigarettes",
            "expired_medicine",
            "magnifying_glass",
            "burner_phone",
            "beer",
            "adrenaline",
            "inverter",
            "handsaw",
        ],
    )
    action, _ = only_action(state, handcuffs_first)
    assert action == Action(ActionKind.USE, Item.HANDCUFFS)

    second_dir = tmp_path / "b"
    second_dir.mkdir()
    cigarettes_first = _rules_with_priority(
        second_dir,
        [
            "cigarettes",
            "handcuffs",
            "expired_medicine",
            "magnifying_glass",
            "burner_phone",
            "beer",
            "adrenaline",
            "inverter",
            "handsaw",
        ],
    )
    action2, _ = only_action(state, cigarettes_first)
    assert action2 == Action(ActionKind.USE, Item.CIGARETTES)
    assert action != action2


# ---------------------------------------------------------------------------
# Weights sum to 1.0 across a randomized sweep.
# ---------------------------------------------------------------------------


def _random_state(rng: random.Random) -> GameState:
    n = rng.randint(1, 5)
    n_resolved = rng.randint(0, n)
    resolved_idx = set(rng.sample(range(n), n_resolved))
    slots = []
    live_used = blank_used = 0
    for i in range(n):
        if i in resolved_idx:
            shell = rng.choice([Shell.LIVE, Shell.BLANK])
            slots.append(shell)
            if shell == Shell.LIVE:
                live_used += 1
            else:
                blank_used += 1
        else:
            slots.append(None)
    unknown_live = rng.randint(0, 3)
    unknown_blank = rng.randint(0 if unknown_live > 0 else 1, 3)
    pool = unknown_live + unknown_blank
    unresolved_count = n - len(resolved_idx)
    if pool != unresolved_count:
        # Force consistency with the invariant cheaply: rebuild pool to match.
        if unresolved_count == 0:
            unknown_live = unknown_blank = 0
        else:
            unknown_live = rng.randint(0, unresolved_count)
            unknown_blank = unresolved_count - unknown_live

    max_hp = rng.choice([2, 3, 4])
    hp = (rng.randint(1, max_hp), rng.randint(1, max_hp))
    dealer_counts = {
        name: rng.randint(0, 1)
        for name in (
            "handcuffs",
            "cigarettes",
            "expired_medicine",
            "magnifying_glass",
            "burner_phone",
            "beer",
            "adrenaline",
            "inverter",
            "handsaw",
        )
    }
    dealer_knows = frozenset(i for i in resolved_idx if rng.random() < 0.5) | (
        {0} if n > 0 and rng.random() < 0.3 else set()
    )
    dealer_knows = frozenset(i for i in dealer_knows if i < n)

    return make_state(
        tuple(slots),
        unknown_live,
        unknown_blank,
        hp=hp,
        max_hp=max_hp,
        items=dealer_items(**dealer_counts),
        cuffed=(rng.random() < 0.2, False),
        dealer_knows=dealer_knows,
    )


def test_dealer_step_weights_sum_to_one_randomized():
    rng = random.Random(1234)
    for _ in range(200):
        state = _random_state(rng)
        branches = dealer_step(state, RULES)
        total = sum(w for w, _, _ in branches)
        assert total == pytest.approx(1.0, abs=1e-9)
        for w, _, _ in branches:
            assert 0.0 <= w <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# Termination: a full rack must still end in a shot.
# ---------------------------------------------------------------------------


def test_full_rack_terminates_in_a_shot():
    items = with_player_items(
        {
            "cigarettes": 1,
            "handcuffs": 1,
            "beer": 1,
            "handsaw": 1,
            "magnifying_glass": 1,
            "adrenaline": 1,
            "expired_medicine": 1,
            "burner_phone": 1,
            "inverter": 1,
        },
        {"beer": 1},
    )
    state = make_state(
        (None, None, None, None), unknown_live=2, unknown_blank=2, hp=(3, 3), max_hp=4, items=items
    )

    for _step in range(40):
        branches = dealer_step(state, RULES)
        total = sum(w for w, _, _ in branches)
        assert total == pytest.approx(1.0)
        weight, actions, successor = max(branches, key=lambda b: b[0])
        if any(
            a.kind in (ActionKind.SHOOT_SELF, ActionKind.SHOOT_OPPONENT) for a in actions
        ):
            return
        if successor.chamber.is_empty or successor.winner() is not None:
            return
        state = successor
        if state.turn is not Player.DEALER:
            # Turn passed without a shot only via the cuff bounce-back, which
            # returns the turn to the same mover with a fresh budget -- but a
            # shot is what ends the loop for this test, so this should not
            # happen while the chamber still holds shells.
            return
    pytest.fail("dealer did not terminate in a shot within 40 steps")


# ---------------------------------------------------------------------------
# The Dealer's own information items (Glass, Phone) — docs/dealer-knowledge.md
# step 1. Regression for the shipped-since-M3 bug: the Dealer's own reveal
# must never resolve `state.chamber` (our belief), only `dealer_knows`.
# ---------------------------------------------------------------------------


def test_dealer_own_glass_marks_dealer_knows_and_leaves_our_chamber_untouched():
    """3-shell 2L/1B (the priced position from the bug report): before the
    fix, `apply()`'s `_use_magnifying_glass` resolved slot 0 for us and
    `deduce()` then collapsed the whole chamber via count-closure, while
    `dealer_knows` came back empty. Both must be false after the fix."""
    state = make_state(
        (None, None, None),
        unknown_live=2,
        unknown_blank=1,
        items=dealer_items(magnifying_glass=1),
    )
    branches = dealer_step(state, RULES)
    assert sum(w for w, _, _ in branches) == pytest.approx(1.0)
    for _weight, actions, successor in branches:
        assert actions == (Action(ActionKind.USE, Item.MAGNIFYING_GLASS),)
        assert 0 in successor.dealer_knows
        assert successor.chamber.slots == state.chamber.slots
        assert successor.chamber.unknown_live == state.chamber.unknown_live
        assert successor.chamber.unknown_blank == state.chamber.unknown_blank


def test_dealer_own_phone_marks_dealer_knows_and_leaves_our_chamber_untouched():
    """Same regression, for the Burner Phone (`size > 2`, so at least 3
    shells). Also the branch-weight-sums-to-1.0 requirement from the brief —
    trivially one branch here because docs/dealer-knowledge.md forbids
    branching over the phone's unobserved index draw at acquisition time
    (see `dealer.dealer_phone_target`)."""
    state = make_state(
        (None, None, None, None),
        unknown_live=2,
        unknown_blank=2,
        items=dealer_items(burner_phone=1),
    )
    branches = dealer_step(state, RULES)
    assert sum(w for w, _, _ in branches) == pytest.approx(1.0)
    for _weight, actions, successor in branches:
        assert actions == (Action(ActionKind.USE, Item.BURNER_PHONE),)
        assert successor.dealer_knows - state.dealer_knows != frozenset()
        assert successor.chamber.slots == state.chamber.slots
        assert successor.chamber.unknown_live == state.chamber.unknown_live
        assert successor.chamber.unknown_blank == state.chamber.unknown_blank


def test_dealer_own_glass_charges_item_count_and_turn_cap():
    """The bookkeeping the brief requires stay identical to before: the item
    leaves the Dealer's rack and the per-turn item-use counter advances,
    exactly as `actions._apply_use` would have charged it."""
    state = make_state(
        (None, None, None),
        unknown_live=2,
        unknown_blank=1,
        items=dealer_items(magnifying_glass=1),
    )
    _, _, successor = dealer_step(state, RULES)[0]
    assert successor.items_of(Player.DEALER)[Item.MAGNIFYING_GLASS] == 0
    assert successor.items_used_this_turn == state.items_used_this_turn + 1
    # retains_turn=True in shipped rules.toml: the Dealer keeps acting.
    assert successor.turn is Player.DEALER


def test_player_own_glass_still_resolves_our_chamber():
    """The fix must not overreach: the player's own Glass/Phone use is
    untouched, and still routes through `actions.apply`, which resolves our
    chamber exactly as before."""
    from buckshotcalc.engine.actions import apply

    state = make_state(
        (None, None, None),
        unknown_live=2,
        unknown_blank=1,
        turn=Player.PLAYER,
        items=with_player_items({}, {"magnifying_glass": 1}),
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.MAGNIFYING_GLASS), RULES)
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    for _weight, successor in outcomes:
        assert successor.chamber.slots[0] is not None
        assert successor.dealer_knows == state.dealer_knows


def test_player_own_phone_still_resolves_our_chamber():
    from buckshotcalc.engine.actions import apply

    state = make_state(
        (None, None, None, None),
        unknown_live=2,
        unknown_blank=2,
        turn=Player.PLAYER,
        items=with_player_items({}, {"burner_phone": 1}),
    )
    outcomes = apply(state, Action(ActionKind.USE, Item.BURNER_PHONE), RULES)
    assert sum(w for w, _ in outcomes) == pytest.approx(1.0)
    assert any(
        any(s is not None for s in successor.chamber.slots[1:]) for _w, successor in outcomes
    )
    for _weight, successor in outcomes:
        assert successor.dealer_knows == state.dealer_knows


@pytest.mark.parametrize(
    ("name", "n_live", "n_blank", "expected"),
    [
        ("3 shells 1L/2B", 1, 2, 4.0 / 9.0),
        ("4 shells 2L/2B", 2, 2, 1.0 / 3.0),
        ("2 shells 1L/1B", 1, 1, 1.0 / 3.0),
    ],
)
def test_corrected_win_probability_dealer_holds_glass(name, n_live, n_blank, expected):
    """Exact values from the bug report's priced table, hp=(2,2), max_hp=3,
    computed against the closed-form health-ratio reload stand-in so this
    test never triggers a full reload-table build (`sim.tables.build_solver`,
    per the M8 test convention)."""
    rules = default_rules()
    solver = build_solver(rules)
    slots = (None,) * (n_live + n_blank)
    chamber = ChamberState.create(slots, n_live, n_blank)
    items = (ItemCounts.empty(), ItemCounts.of(magnifying_glass=1))
    state = GameState.create(chamber, (2, 2), 3, Player.DEALER, items=items)
    assert solver.value(state) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Stale "self" target after a Beer on a known blank (SPEC.md §15.19)
# ---------------------------------------------------------------------------


def _beer_on_known_blank_state(**extra) -> GameState:
    # Dealer knows slot 0 is blank; after the beer 3 shells remain (2L 1B), so the
    # majority rule alone would shoot the player.
    return make_state(
        (Shell.BLANK, None, None, None),
        unknown_live=2,
        unknown_blank=1,
        items=dealer_items(beer=1, **extra),
        dealer_knows=frozenset({0}),
    )


def test_beer_on_known_blank_then_shoots_self_at_the_next_unknown_shell():
    """DealerIntelligence.gd:170-175 + 275-276: dealerTarget stays "self" after the beer."""
    by_seq = sequence_weights(_beer_on_known_blank_state())
    beer = Action(ActionKind.USE, Item.BEER)
    assert by_seq == {(beer, Action(ActionKind.SHOOT_SELF)): pytest.approx(1.0)}


def test_stale_target_is_overwritten_by_the_handsaw_coinflip():
    """:206-216: holding a handsaw, the post-beer shot is the majority-rule coinflip again."""
    by_seq = sequence_weights(_beer_on_known_blank_state(handsaw=1))
    beer = Action(ActionKind.USE, Item.BEER)
    saw = Action(ActionKind.USE, Item.HANDSAW)
    assert by_seq == {(beer, saw, Action(ActionKind.SHOOT_OPPONENT)): pytest.approx(1.0)}


def test_stale_target_is_gated_by_rules_key(tmp_path):
    with REAL_RULES_PATH.open("rb") as f:
        data = tomllib.load(f)
    data["dealer"]["beer_keeps_known_blank_target"] = False
    path = tmp_path / "rules.toml"
    path.write_text(_dump_toml(data))
    by_seq = sequence_weights(_beer_on_known_blank_state(), load_rules(path))
    assert by_seq == {(Action(ActionKind.USE, Item.BEER),): pytest.approx(1.0)}
