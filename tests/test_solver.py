"""Tests for buckshotcalc.engine.solver (SPEC.md §7, M4 acceptance)."""

from __future__ import annotations

import dataclasses
import random
import threading

import pytest

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.dealer import dealer_step
from buckshotcalc.engine.rules import Rules, default_rules, load_rules
from buckshotcalc.engine.solver import SearchCancelled, Solver, _aitken_next, _shell_configs
from buckshotcalc.engine.state import (
    ChamberState,
    GameMode,
    GameState,
    Item,
    ItemCounts,
    Player,
    Shell,
)
from tests.test_rules import _load_real_dict, _write_toml
from tests.test_state import random_chamber_state

RULES = default_rules()
WASTE_EPS = 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_reload_keys(rules: Rules) -> list[tuple[int, int, int]]:
    return [
        (hp_p, hp_d, max_hp)
        for max_hp in rules.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    ]


def _health_ratio_table(rules: Rules) -> dict[tuple[int, int, int], float]:
    """A simple, EXACT closed-form stand-in for the real (slow, sampled)
    fixed point: `hp_player / (hp_player + hp_dealer)`.

    Injected directly into `Solver._reload_table` so the fast suite never
    triggers `_compute_reload_table` (which is slow by construction — see
    the `slow`-marked tests below). Because it's a plain closed-form
    function, hand-computed arithmetic against it is exact, and it exercises
    exactly the same recursion/memo code path a real table would.
    """
    return {key: key[0] / (key[0] + key[1]) for key in _all_reload_keys(rules)}


def _stub_solver(rules: Rules = RULES, mode: GameMode = GameMode.BASE) -> Solver:
    solver = Solver(rules, mode)
    solver._reload_table = _health_ratio_table(rules)
    return solver


def _state(
    live: int,
    blank: int,
    hp: tuple[int, int],
    turn: Player,
    max_hp: int = 3,
    items: tuple[ItemCounts, ItemCounts] | None = None,
    sawed: bool = False,
    cuffed: tuple[bool, bool] = (False, False),
    dealer_knows: frozenset[int] = frozenset(),
) -> GameState:
    chamber = ChamberState.create(tuple([None] * (live + blank)), live, blank)
    return GameState.create(
        chamber=chamber,
        hp=hp,
        max_hp=max_hp,
        turn=turn,
        items=items,
        sawed=sawed,
        cuffed=cuffed,
        dealer_knows=dealer_knows,
    )


def _items(**by_config_name: int) -> ItemCounts:
    return ItemCounts.of(**by_config_name)


def _custom_rules(tmp_path, overrides: dict[str, object]) -> Rules:
    """Load a copy of the real `rules.toml` with dotted-path `overrides` applied.

    e.g. `{"solver.reload_item_draw_samples": 2}`. Reuses M1's TOML
    round-trip helpers from `tests/test_rules.py` rather than duplicating
    them.
    """
    data = _load_real_dict()
    for dotted, value in overrides.items():
        *path, leaf = dotted.split(".")
        table = data
        for key in path:
            table = table[key]
        table[leaf] = value
    path = _write_toml(tmp_path, data)
    return load_rules(path)


TINY_OVERRIDES: dict[str, object] = {
    "shells.max_total": 2,
    "items.dealt_per_round_min": 0,
    "items.dealt_per_round_max": 1,
    "solver.reload_item_draw_samples": 2,
    "solver.reload_max_iterations": 3,
    "solver.reload_table_cache": False,
    "health.possible_max_hp": [2],
}

SMALL_OVERRIDES: dict[str, object] = {
    "shells.max_total": 3,
    "items.dealt_per_round_min": 0,
    "items.dealt_per_round_max": 1,
    "solver.reload_item_draw_samples": 4,
    "solver.reload_max_iterations": 6,
    "solver.reload_fixedpoint_tolerance": 1e-3,
    "solver.reload_table_cache": False,
}


# ---------------------------------------------------------------------------
# Hand-computed exact values (SPEC.md §12.3) — all against the closed-form
# health-ratio reload stub above, so the arithmetic below is exact and
# decoupled from the (separately tested) real fixed point.
# ---------------------------------------------------------------------------


def test_l1_b0_guaranteed_live():
    """One shell, guaranteed live. hp=(3,3), max_hp=3, player to move.

    SHOOT_OPPONENT: live for certain -> dealer hp 3-1=2, chamber empties ->
    reload_value(3, 2, 3) = 3/5 = 0.6.
    SHOOT_SELF: live for certain -> player hp 3-1=2, chamber empties ->
    reload_value(2, 3, 3) = 2/5 = 0.4.
    Best action is SHOOT_OPPONENT at exactly 0.6.
    """
    solver = _stub_solver()
    state = _state(live=1, blank=0, hp=(3, 3), turn=Player.PLAYER)
    result = solver.evaluate(state)
    assert result.value == pytest.approx(0.6)
    by_kind = {c.action.kind: c.value for c in result.children}
    assert by_kind[ActionKind.SHOOT_OPPONENT] == pytest.approx(0.6)
    assert by_kind[ActionKind.SHOOT_SELF] == pytest.approx(0.4)


def test_l0_b1_guaranteed_blank_free_turn():
    """One shell, guaranteed blank. SHOOT_SELF pops it, turn retained, chamber
    empties immediately (no further live shell to resolve) ->
    reload_value(3, 3, 3) = 0.5. SHOOT_OPPONENT: blank ends the turn
    regardless (SPEC.md §6.1) and also empties the chamber ->
    reload_value(3, 3, 3) = 0.5 too (both HPs are unaffected by a blank).
    Both actions tie at exactly 0.5, and neither is `wasted`.
    """
    solver = _stub_solver()
    state = _state(live=0, blank=1, hp=(3, 3), turn=Player.PLAYER)
    result = solver.evaluate(state)
    assert result.value == pytest.approx(0.5)
    for child in result.children:
        assert child.value == pytest.approx(0.5)
        assert child.wasted is False


def test_l1_b1_at_one_hp():
    """Two shells, one live one blank, hp=(1,1). Worked by hand:

    SHOOT_SELF: p_live=0.5.
      - live (0.5): player dies -> value 0.0.
      - blank (0.5): shell resolves+pops; the pool empties to (0,0) so
        `deduce()` forces the remaining slot LIVE. Turn retained. The
        successor is a fresh player node with a KNOWN live chambered shell
        and hp still (1,1): SHOOT_OPPONENT there is certain-live -> dealer
        dies -> 1.0 (beats SHOOT_SELF there, which is certain suicide -> 0.0).
        So this branch's value is 1.0.
      Total: 0.5*0.0 + 0.5*1.0 = 0.5.

    SHOOT_OPPONENT: p_live=0.5, turn always passes here.
      - live (0.5): dealer dies -> value 1.0.
      - blank (0.5): same deduction leaves a KNOWN live shell, turn now
        DEALER's. Public shell counts close the Dealer's own view to the
        same known-live shell (`dealer_view`'s count-closure — the
        `docs/dealer-knowledge.md` addendum's `knows_shell_when_one_remains`
        falling out "for free"), so the Dealer's policy shoots the player
        (deterministic, no coinflip) for 1 damage -> player dies -> 0.0.
      Total: 0.5*1.0 + 0.5*0.0 = 0.5.

    Both root actions tie at exactly 0.5.
    """
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(1, 1), turn=Player.PLAYER)
    result = solver.evaluate(state)
    assert result.value == pytest.approx(0.5)
    for child in result.children:
        assert child.value == pytest.approx(0.5)


def test_terminal_dealer_dead_beats_player_dead():
    """SPEC.md §7.1's terminal order: Dealer HP <= 0 is checked before Player HP <= 0.

    Both reaching 0 in the same state is otherwise unreachable through real
    play, but the ORDER is directly testable by constructing the state.
    """
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(0, 0), turn=Player.PLAYER)
    assert solver.value(state) == 1.0


def test_dealer_dead_alone_is_win():
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(2, 0), turn=Player.PLAYER)
    assert solver.value(state) == 1.0


def test_player_dead_alone_is_loss():
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(0, 2), turn=Player.PLAYER)
    assert solver.value(state) == 0.0


# ---------------------------------------------------------------------------
# Range: values stay in [0, 1] over random legal positions, both seats.
# ---------------------------------------------------------------------------


def test_value_in_range_random_positions():
    solver = _stub_solver()
    rng = random.Random(12345)
    checked = 0
    for _ in range(200):
        chamber = random_chamber_state(rng, max_len=5)
        if chamber.is_empty:
            continue
        hp = (rng.randint(1, 3), rng.randint(1, 3))
        turn = rng.choice([Player.PLAYER, Player.DEALER])
        state = GameState.create(chamber=chamber, hp=hp, max_hp=3, turn=turn)
        value = solver.value(state)
        assert -1e-9 <= value <= 1.0 + 1e-9
        checked += 1
    assert checked > 50  # sanity: the loop actually exercised real positions


def test_value_in_range_with_items_and_flags():
    solver = _stub_solver()
    rng = random.Random(999)
    for _ in range(50):
        chamber = random_chamber_state(rng, max_len=4)
        if chamber.is_empty:
            continue
        hp = (rng.randint(1, 3), rng.randint(1, 3))
        turn = rng.choice([Player.PLAYER, Player.DEALER])
        items = (
            _items(cigarettes=rng.randint(0, 1), handcuffs=rng.randint(0, 1)),
            _items(beer=rng.randint(0, 1), magnifying_glass=rng.randint(0, 1)),
        )
        chambered_known = bool(chamber.slots) and chamber.slots[0] is not None
        sawed = rng.choice([True, False]) if chambered_known else False
        state = GameState.create(
            chamber=chamber, hp=hp, max_hp=3, turn=turn, items=items, sawed=sawed
        )
        value = solver.value(state)
        assert 0.0 - 1e-9 <= value <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# Both seats at the root — M2 shipped a seat bug once; do not repeat it.
# ---------------------------------------------------------------------------


def test_dealer_turn_root_known_live_shoots_player():
    """Dealer to move, chambered shell KNOWN live (via public count-closure):
    deterministic dealer policy shoots the player for 1 damage, ending the
    round if that's lethal.
    """
    solver = _stub_solver()
    state = _state(live=1, blank=0, hp=(1, 3), turn=Player.DEALER)
    assert solver.value(state) == 0.0  # certain lethal shot at the player


def test_dealer_turn_root_matches_reload_after_known_shot():
    solver = _stub_solver()
    state = _state(live=1, blank=0, hp=(3, 1), turn=Player.DEALER)
    # Dealer (hp 1) knows the shell is live -> shoots player (hp 3 -> 2) ->
    # chamber empties -> reload_value(2, 1, 3) = 2/3 under the stub table.
    assert solver.value(state) == pytest.approx(2 / 3)


def test_dealer_turn_root_evaluate_has_no_children():
    """`SearchResult.children` is empty at a Dealer node (pinned contract)."""
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(3, 3), turn=Player.DEALER)
    result = solver.evaluate(state)
    assert result.children == ()


def test_terminal_root_evaluate_has_no_children():
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(0, 3), turn=Player.PLAYER)
    result = solver.evaluate(state)
    assert result.children == ()
    assert result.value == 0.0


# ---------------------------------------------------------------------------
# evaluate() agreement: value matches, children sorted descending, wasted
# set exactly on strictly-worse children.
# ---------------------------------------------------------------------------


def test_evaluate_agrees_with_value_and_is_sorted():
    solver = _stub_solver()
    rng = random.Random(42)
    for _ in range(30):
        chamber = random_chamber_state(rng, max_len=4)
        if chamber.is_empty:
            continue
        hp = (rng.randint(1, 3), rng.randint(1, 3))
        state = GameState.create(chamber=chamber, hp=hp, max_hp=3, turn=Player.PLAYER)
        result = solver.evaluate(state)
        assert result.value == pytest.approx(solver.value(state))
        values = [c.value for c in result.children]
        assert values == sorted(values, reverse=True)
        if result.children:
            best = result.children[0].value
            assert best == pytest.approx(result.value)
            for child in result.children:
                expected_wasted = child.value < best - WASTE_EPS
                assert child.wasted == expected_wasted


def test_evaluate_wasted_marks_strictly_worse_action():
    """An item whose USE strictly underperforms the best line is flagged `✗`.

    L1 B0 at full health, player also holds Beer: the chambered shell is
    guaranteed live, so ejecting it with Beer discards a certain kill
    (Beer never fires a shot — it only reveals+ejects) in favour of a
    reload at unchanged HP, worth exactly `reload_value(3, 3, 3) = 0.5`
    under the stub table — strictly less than SHOOT_OPPONENT's certain-kill
    line at `reload_value(3, 2, 3) = 0.6` (see `test_l1_b0_guaranteed_live`).
    A genuinely free, no-downside item (e.g. Cigarettes at max HP, or the
    Magnifying Glass) can never be strictly `wasted` under this codebase's
    definition — it weakly dominates skipping it — so Beer-on-a-known-kill
    is the right corpus entry for `✗`, not a no-op heal.
    """
    solver = _stub_solver()
    state = _state(
        live=1,
        blank=0,
        hp=(3, 3),
        turn=Player.PLAYER,
        items=(_items(beer=1), ItemCounts.empty()),
    )
    result = solver.evaluate(state)
    by_kind = {(c.action.kind, c.action.item): c for c in result.children}
    beer_child = by_kind[(ActionKind.USE, Item.BEER)]
    shoot_child = by_kind[(ActionKind.SHOOT_OPPONENT, None)]
    assert beer_child.value == pytest.approx(0.5)
    assert shoot_child.value == pytest.approx(0.6)
    assert shoot_child.value > beer_child.value
    assert beer_child.wasted is True
    assert shoot_child.wasted is False


# ---------------------------------------------------------------------------
# SPEC.md §3.3's first exploit: more blanks than lives -> the Dealer shoots
# itself, so spending handcuffs on it buys nothing.
# ---------------------------------------------------------------------------


def test_dont_waste_handcuffs_exploit_direction():
    """L1 B3 (more blanks than lives), player holds only handcuffs.

    The Dealer's majority-rule policy is deterministic here (3 blank > 1
    live -> shoots itself), so cuffing it away denies it nothing it
    wouldn't already have denied itself. The solver must not need the
    handcuffs to reach its own best value: some non-cuffing action ties or
    beats USE(HANDCUFFS), i.e. handcuffs are never *required* for the
    optimum. (Measured: in this exact position they tie exactly, which is
    the strongest form of "wasted" — spending the item buys literally
    nothing — but the direction asserted here, "no worse to skip it", is
    what SPEC.md §3.3's exploit actually claims and is the assertion this
    test relies on.)
    """
    solver = _stub_solver()
    state = _state(
        live=1,
        blank=3,
        hp=(3, 1),
        turn=Player.PLAYER,
        items=(_items(handcuffs=1), ItemCounts.empty()),
    )
    result = solver.evaluate(state)
    by_kind = {(c.action.kind, c.action.item): c for c in result.children}
    cuffs_child = by_kind[(ActionKind.USE, Item.HANDCUFFS)]
    non_cuffing = [c for c in result.children if c.action.item is not Item.HANDCUFFS]
    assert non_cuffing, "expected at least one non-cuffing action"
    assert max(c.value for c in non_cuffing) >= cuffs_child.value - WASTE_EPS


# ---------------------------------------------------------------------------
# item cap (SPEC.md §7.5)
# ---------------------------------------------------------------------------


def test_cap_reached_flag_true_when_cap_binds(tmp_path):
    """A rules variant with `max_items_per_turn=1` lets exactly one item use
    through; a state holding two more (still-legal) items past that point
    must report `cap_reached=True` at the node where the cap bites.
    """
    rules = _custom_rules(tmp_path, {"solver.max_items_per_turn": 1})
    solver = _stub_solver(rules)
    state = _state(
        live=1,
        blank=0,
        hp=(3, 3),
        turn=Player.PLAYER,
        items=(_items(cigarettes=1, handcuffs=1), ItemCounts.empty()),
        # Charge the counter to the cap directly (a plain field value, not a
        # reimplemented transition) so the state under test is itself the
        # node where the cap has just bound, rather than depending on a
        # chosen action sequence reaching it.
    )
    state = state.with_items_used_this_turn(1)
    result = solver.evaluate(state)
    assert result.cap_reached is True


def test_cap_reached_flag_false_without_binding():
    solver = _stub_solver()
    state = _state(live=1, blank=0, hp=(3, 3), turn=Player.PLAYER)
    result = solver.evaluate(state)
    assert result.cap_reached is False


# ---------------------------------------------------------------------------
# Memo hygiene
# ---------------------------------------------------------------------------


def test_value_idempotent_and_memo_only_grows_on_new_states():
    solver = _stub_solver()
    state = _state(live=1, blank=1, hp=(2, 2), turn=Player.PLAYER)
    v1 = solver.value(state)
    size_after_first = len(solver._memo)
    v2 = solver.value(state)
    assert v1 == v2
    assert len(solver._memo) == size_after_first  # second call is all memo hits


def test_memo_hygiene_across_independently_tabled_solver_instances():
    """Two `Solver` instances, same rules/mode, deliberately different reload
    tables: each instance's memo must reflect only its own table, never leak
    into (or be poisoned by) another instance's.

    This is the closest testable proxy for "solving a position, then again
    after a reload-table rebuild" that the pinned API supports: a `Solver`
    is documented to be valid for exactly one `(rules, mode)` lifetime, so a
    genuine table rebuild is modeled as a fresh instance, not in-place
    mutation of a live one.
    """
    state = _state(live=1, blank=0, hp=(2, 2), turn=Player.PLAYER)
    keys = _all_reload_keys(RULES)

    s1 = Solver(RULES, GameMode.BASE)
    s1._reload_table = {k: 0.1 for k in keys}
    v1 = s1.value(state)

    s2 = Solver(RULES, GameMode.BASE)
    s2._reload_table = {k: 0.9 for k in keys}
    v2 = s2.value(state)

    assert v1 == pytest.approx(0.1)
    assert v2 == pytest.approx(0.9)
    assert v1 != v2


# ---------------------------------------------------------------------------
# reload_value: out-of-table lookups, no-legal-actions assertion sanity
# ---------------------------------------------------------------------------


def test_reload_value_outside_table_raises():
    solver = _stub_solver()
    with pytest.raises(KeyError):
        solver.reload_value(999, 1, 3)


# ---------------------------------------------------------------------------
# Determinism (fast: uses a tiny rules variant, not the shipped defaults)
# ---------------------------------------------------------------------------


def test_reload_table_deterministic_across_solver_instances(tmp_path):
    rules = _custom_rules(tmp_path, TINY_OVERRIDES)
    s1 = Solver(rules, GameMode.BASE)
    s1._ensure_reload_table()
    s2 = Solver(rules, GameMode.BASE)
    s2._ensure_reload_table()
    assert s1._reload_table == s2._reload_table
    assert s1._reload_table  # non-empty


def test_reload_table_deterministic_across_mode(tmp_path):
    """BASE and DOUBLE_OR_NOTHING draw from different item lists and are
    cached under different keys (`_cache_path` includes `mode.name`) — each
    is independently deterministic, and the two need not agree.
    """
    rules = _custom_rules(tmp_path, TINY_OVERRIDES)
    base1 = Solver(rules, GameMode.BASE)
    base1._ensure_reload_table()
    base2 = Solver(rules, GameMode.BASE)
    base2._ensure_reload_table()
    assert base1._reload_table == base2._reload_table

    don1 = Solver(rules, GameMode.DOUBLE_OR_NOTHING)
    don1._ensure_reload_table()
    don2 = Solver(rules, GameMode.DOUBLE_OR_NOTHING)
    don2._ensure_reload_table()
    assert don1._reload_table == don2._reload_table


# ---------------------------------------------------------------------------
# Disk cache (SPEC.md §7.4)
# ---------------------------------------------------------------------------


def test_disk_cache_round_trips(tmp_path, monkeypatch):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    overrides = dict(TINY_OVERRIDES)
    overrides["solver.reload_table_cache"] = True
    rules = _custom_rules(rules_dir, overrides)

    s1 = Solver(rules, GameMode.BASE)
    s1._ensure_reload_table()

    cache_files = list((cache_home / "buckshotcalc").glob("*.json"))
    assert len(cache_files) == 1
    assert list((cache_home / "buckshotcalc").glob("*.tmp")) == []  # write was renamed into place

    # Overwrite the cached table with a distinctive sentinel value so a
    # second Solver can only produce it by READING the cache, never by
    # recomputing (the real fixed point cannot land on an exact constant
    # for every entry).
    import json

    payload = json.loads(cache_files[0].read_text())
    for k in payload["table"]:
        payload["table"][k] = 0.123456
    cache_files[0].write_text(json.dumps(payload))

    s2 = Solver(rules, GameMode.BASE)
    s2._ensure_reload_table()
    assert all(v == pytest.approx(0.123456) for v in s2._reload_table.values())


def test_disk_cache_fingerprint_mismatch_ignored(tmp_path, monkeypatch):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    overrides = dict(TINY_OVERRIDES)
    overrides["solver.reload_table_cache"] = True
    rules = _custom_rules(rules_dir, overrides)

    s1 = Solver(rules, GameMode.BASE)
    s1._ensure_reload_table()
    fresh_table = dict(s1._reload_table)

    cache_files = list((cache_home / "buckshotcalc").glob("*.json"))
    assert len(cache_files) == 1
    import json

    payload = json.loads(cache_files[0].read_text())
    payload["fingerprint"] = "not-the-real-fingerprint"
    for k in payload["table"]:
        payload["table"][k] = 0.123456
    cache_files[0].write_text(json.dumps(payload))

    s2 = Solver(rules, GameMode.BASE)
    s2._ensure_reload_table()
    # A fingerprint mismatch must be ignored (recomputed), not trusted —
    # the sentinel value must NOT appear, and the recomputed table must
    # match a fresh from-scratch computation exactly (determinism).
    assert s2._reload_table == fresh_table


def test_disk_cache_from_other_engine_code_is_stale(tmp_path, monkeypatch):
    """A table built by different engine code is never loaded: the operator must rebuild."""
    from buckshotcalc.engine import solver as solver_mod

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    solver_rules = dataclasses.replace(RULES.solver, reload_table_cache=True)
    rules = dataclasses.replace(RULES, solver=solver_rules)
    solver_mod._write_cache(rules, GameMode.BASE, {(1, 1, 3): 0.5})
    assert Solver(rules, GameMode.BASE).has_cached_reload_table()

    monkeypatch.setattr(solver_mod, "_engine_fingerprint", lambda: "other-engine")
    assert not Solver(rules, GameMode.BASE).has_cached_reload_table()


def test_engine_fingerprint_ignores_docstrings_and_comments(tmp_path, monkeypatch):
    from buckshotcalc.engine import solver as solver_mod

    def fp(src: str) -> str:
        (tmp_path / "state.py").write_text(src)
        monkeypatch.setattr(solver_mod, "_ENGINE_MODULES", ("state",))
        monkeypatch.setattr(solver_mod, "__file__", str(tmp_path / "solver.py"))
        solver_mod._engine_fingerprint.cache_clear()
        return solver_mod._engine_fingerprint()

    base = fp("def f():\n    return 1\n")
    assert fp('def f():\n    """doc"""\n    # note\n    return 1\n') == base
    assert fp("def f():\n    return 2\n") != base
    solver_mod._engine_fingerprint.cache_clear()


def test_disk_cache_corrupt_file_degrades_to_recompute(tmp_path, monkeypatch):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    overrides = dict(TINY_OVERRIDES)
    overrides["solver.reload_table_cache"] = True
    rules = _custom_rules(rules_dir, overrides)

    cache_dir = cache_home / "buckshotcalc"
    cache_dir.mkdir(parents=True)
    from buckshotcalc.engine.solver import _cache_path

    bad_path = _cache_path(rules, GameMode.BASE)
    bad_path.write_text("{ this is not valid json ")

    solver = Solver(rules, GameMode.BASE)
    # Must not raise.
    solver._ensure_reload_table()
    assert solver._reload_table


def test_disk_cache_disabled_writes_nothing(tmp_path, monkeypatch):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules = _custom_rules(rules_dir, TINY_OVERRIDES)  # reload_table_cache: False
    solver = Solver(rules, GameMode.BASE)
    solver._ensure_reload_table()
    assert not (cache_home / "buckshotcalc").exists()


# ---------------------------------------------------------------------------
# Slow: the real fixed point (SPEC.md §7.4) over a small-but-nontrivial table.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reload_fixed_point_converges_and_is_monotone(tmp_path):
    """Monotone in HP, at 4 item-draw samples.

    The draw is seeded from `rules.fingerprint` (decisions-pending item 1), so
    any unrelated `rules.toml` edit re-rolls it, and a 4-sample table is noisy
    enough to break monotonicity on some draws. Pinned so this tests the
    iteration, not the luck of the rules file's hash (it flipped on the 13c/13d
    keys, 2026-10-25, with Aitken on and off alike).
    """
    rules = dataclasses.replace(_custom_rules(tmp_path, SMALL_OVERRIDES), fingerprint="pinned")
    solver = Solver(rules, GameMode.BASE)
    solver._ensure_reload_table()
    table = solver._reload_table
    assert table

    for v in table.values():
        assert 0.0 <= v <= 1.0

    # Monotone in the obvious direction: more player HP at fixed dealer HP
    # and max_hp -> higher value; more dealer HP at fixed player HP -> lower
    # value.
    for max_hp in rules.health.possible_max_hp:
        for hp_d in range(1, max_hp + 1):
            series = [table[(hp_p, hp_d, max_hp)] for hp_p in range(1, max_hp + 1)]
            assert series == sorted(series)
        for hp_p in range(1, max_hp + 1):
            series = [table[(hp_p, hp_d, max_hp)] for hp_d in range(1, max_hp + 1)]
            assert series == sorted(series, reverse=True)


@pytest.mark.slow
def test_reload_fixed_point_deterministic_full_scale(tmp_path):
    """Same as the fast determinism test, but at a size closer to production
    (still far short of the shipped defaults — see the milestone report for
    why the shipped defaults are impractical to run in a test)."""
    rules = _custom_rules(tmp_path, SMALL_OVERRIDES)
    s1 = Solver(rules, GameMode.BASE)
    s1._ensure_reload_table()
    s2 = Solver(rules, GameMode.BASE)
    s2._ensure_reload_table()
    assert s1._reload_table == s2._reload_table


# ---------------------------------------------------------------------------
# Config keys with no implementation behind them must fail loudly
# (SPEC.md §0.1 (2)). A key that is read and discarded is worse
# than one that is unsupported: the run looks exact and is not.
# ---------------------------------------------------------------------------


def test_cuffs_surviving_reload_is_refused_not_ignored(tmp_path):
    """`round.cuffs_clear_on_reload = false` has no 3-D representation (SPEC.md §15.12).

    The reload table is keyed on health alone, so a cuff surviving into the
    fresh load is state it cannot carry. The only honest options are a fourth
    dimension or a refusal; silently building the table as if cuffs cleared
    would misprice every reload while reporting a normal-looking float.
    """
    overrides = dict(SMALL_OVERRIDES)
    overrides["round.cuffs_clear_on_reload"] = False
    rules = _custom_rules(tmp_path, overrides)
    solver = Solver(rules, GameMode.BASE)
    with pytest.raises(NotImplementedError, match="cuffs_clear_on_reload"):
        solver.reload_value(1, 1, 2)


@pytest.mark.parametrize(
    "key", ["shells.even_total_splits_equally", "shells.odd_total_differs_by_one"]
)
def test_unsupported_shell_split_rule_is_refused(tmp_path, key):
    """SPEC.md §3.1 step 3 defines no alternative split rule for either flag."""
    overrides = dict(SMALL_OVERRIDES)
    overrides[key] = False
    rules = _custom_rules(tmp_path, overrides)
    solver = Solver(rules, GameMode.BASE)
    with pytest.raises(NotImplementedError):
        solver.reload_value(1, 1, 2)


# ---------------------------------------------------------------------------
# Structural monotonicity. These are the strongest cheap defect detectors the
# suite has: each is a property the true value function must satisfy for
# reasons that have nothing to do with how the search is written, so a seat
# mix-up, a sign error, or a Dealer node evaluated as a min node breaks one of
# them immediately. Run against the closed-form reload stub, which is itself
# monotone in the same directions, so the property is exact.
# ---------------------------------------------------------------------------


def _monotonicity_sweep_states(seed: int, mode: GameMode, count: int) -> list[GameState]:
    rng = random.Random(seed)
    pool = list(RULES.items.base_items)
    if mode is GameMode.DOUBLE_OR_NOTHING:
        pool += list(RULES.items.double_or_nothing_items)
    states = []
    for _ in range(count):
        total = rng.randint(2, 4)
        live = rng.randint(1, total - 1)

        def rack() -> ItemCounts:
            counts = ItemCounts.empty()
            for _ in range(rng.randint(0, 2)):
                counts = counts.with_delta(rng.choice(pool), 1)
            return counts

        states.append(
            _state(
                live=live,
                blank=total - live,
                hp=(rng.randint(1, 3), rng.randint(1, 3)),
                turn=rng.choice([Player.PLAYER, Player.DEALER]),
                max_hp=3,
                items=(rack(), rack()),
                cuffed=(rng.random() < 0.25, rng.random() < 0.25),
            )
        )
    return states


@pytest.mark.parametrize("mode", [GameMode.BASE, GameMode.DOUBLE_OR_NOTHING])
def test_value_is_monotone_in_health(mode):
    """More player HP never hurts us; more Dealer HP never helps us.

    True of the game regardless of policy: HP is only ever spent, and neither
    side's legal actions depend on the *amount* of health either holds.
    """
    solver = _stub_solver(mode=mode)
    for state in _monotonicity_sweep_states(11, mode, 120):
        base = solver.value(state)
        if state.hp[Player.PLAYER] < state.max_hp:
            better = solver.value(
                state.with_hp(Player.PLAYER, state.hp[Player.PLAYER] + 1)
            )
            assert better >= base - 1e-9, f"more player HP lowered the value: {base} -> {better}"
        if state.hp[Player.DEALER] < state.max_hp:
            worse = solver.value(
                state.with_hp(Player.DEALER, state.hp[Player.DEALER] + 1)
            )
            assert worse <= base + 1e-9, f"more Dealer HP raised the value: {base} -> {worse}"


def test_extra_player_item_never_lowers_the_value_in_base_mode():
    """In the base game an item in our rack is a strictly added option.

    `legal_actions` can only grow, and nothing forces us to spend it — an
    unwanted Cigarette at max HP is declinable. Restricted to `GameMode.BASE`
    on purpose: in Double or Nothing the Dealer's Adrenaline can steal what
    we hold, so an extra item genuinely can hurt us and the property is false.
    """
    solver = _stub_solver(mode=GameMode.BASE)
    for state in _monotonicity_sweep_states(12, GameMode.BASE, 60):
        base = solver.value(state)
        for item in RULES.items.base_items:
            richer = state.with_items(
                Player.PLAYER, state.items_of(Player.PLAYER).with_delta(item, 1)
            )
            assert solver.value(richer) >= base - 1e-9, (
                f"an extra {item.name} lowered the player's value: "
                f"{base} -> {solver.value(richer)}"
            )


def test_dealer_node_is_a_weighted_average_not_a_minimum():
    """SPEC.md §7.2: Dealer nodes are policy-evaluation nodes, not min nodes.

    The Dealer's action is *determined* by its policy, so its node takes the
    probability-weighted average over `dealer_step`'s branches. Evaluating it
    as a minimum instead is the specific error §7.2 warns about, and it is
    invisible to every value-level assertion in this file — a min node still
    returns a float in `[0, 1]`, still respects the health monotonicity above,
    and still gets the hand-computed single-branch positions right, because
    those have nothing to minimize over. It is only detectable at a Dealer
    node whose branches genuinely disagree, which is what this asserts:
    the node equals the weighted mean, and is strictly above the minimum.
    """
    solver = _stub_solver(mode=GameMode.DOUBLE_OR_NOTHING)
    discriminating = 0
    for state in _monotonicity_sweep_states(13, GameMode.DOUBLE_OR_NOTHING, 120):
        if state.turn is not Player.DEALER or state.winner() is not None:
            continue
        branches = dealer_step(state, RULES)
        child_values = [solver.value(successor) for _w, _a, successor in branches]
        weighted = sum(w * v for (w, _a, _s), v in zip(branches, child_values, strict=True))
        assert sum(w for w, _a, _s in branches) == pytest.approx(1.0)
        assert solver.value(state) == pytest.approx(weighted)
        if max(child_values) - min(child_values) > 1e-6:
            discriminating += 1
            assert solver.value(state) > min(child_values) + 1e-9, (
                "Dealer node collapsed to its worst branch — evaluated as a min "
                "node rather than under the Dealer's policy (SPEC.md §7.2)"
            )
    assert discriminating >= 10, (
        f"only {discriminating} Dealer nodes had disagreeing branches; this test "
        "cannot distinguish a min node from a policy node without them"
    )


# ---------------------------------------------------------------------------
# Correctness patch (decompiled-source items 2-4) — solver-level P(win)
# evidence, against the exact closed-form reload stub so the numbers below
# are exact, not sampled.
# ---------------------------------------------------------------------------


def test_last_shell_size_guard_does_not_move_p_win_handcuffs():
    """Item 2 (handcuffs `size != 1` guard): n=1, guaranteed BLANK, Dealer
    holds only handcuffs.

    Hand-computed: whatever the Dealer does with its now-guard-blocked
    handcuffs, the shell is known BLANK (closure always applies at n<=1), so
    the self-shot is deterministic and harmless either way, the chamber
    empties, and `round.cuffs_clear_on_reload` (SPEC.md §15.12) wipes any
    cuff the old, unguarded code would have applied before the reload
    boundary is ever consulted — which is keyed on `(hp_player, hp_dealer,
    max_hp)` alone (SPEC.md §7.4) and never looks at `cuffed` or `turn`. So
    `P(win)` is provably identical before and after this fix at hp=(2, 2):
    `reload_value(2, 2, 3) == 2/(2+2) == 0.5` either way — the guard is a
    real, verified correction to the Dealer's action *sequence* (see
    `test_dealer.py`), not to `P(win)` in this engine's architecture. This
    is worth asserting exactly rather than assuming: it is the negative
    finding reported alongside items 2/3/4.
    """
    chamber = ChamberState.create((None,), 0, 1)  # n=1, guaranteed BLANK
    items = (ItemCounts.empty(), ItemCounts.of(handcuffs=1))
    state = GameState.create(chamber, (2, 2), 3, Player.DEALER, items=items)
    solver = _stub_solver(RULES, GameMode.DOUBLE_OR_NOTHING)
    assert solver.value(state) == pytest.approx(0.5)


def test_medicine_never_fires_while_dealer_holds_cigarettes(tmp_path):
    """Item 3 (`!hasCigs` conjunct): fails against the code before the fix.

    Under the *shipped* `dealer.item_priority` (cigarettes strictly before
    expired_medicine), this guard is structurally unreachable — cigarettes'
    own condition (`hp < max_hp`) is a strict subset of medicine's
    (`hp < max_hp and hp >= 2`), so whenever the Dealer holds both and
    medicine could fire, cigarettes already fired first via the priority
    walk. Reported as a finding, not papered over: see the milestone report.

    To exercise the guard at all, this reorders the priority so
    expired_medicine is checked first — item_priority is config-driven
    precisely so this is a legitimate rules.toml, not a code change
    (SPEC.md §3.4). Position: Dealer holds both cigarettes and
    expired_medicine, hp=(3, 2), max_hp=3 (hp=2 satisfies both
    `< max_hp` and `>= medicine_requires_hp_at_least`).

    Hand-computed against the closed-form reload stub: with the guard,
    cigarettes fires deterministically (+1 heal, dealer hp 2->3), which
    never reaches a reload boundary this call (chamber still holds both
    shells) — `dealer_step` returns exactly one branch, `USE(CIGARETTES)`.
    Before the fix, expired_medicine fires as a chance node instead (0.5
    heal-to-max, 0.5 harm to hp=1), which is strictly worse in expectation
    here since cigarettes already gets the Dealer to a safe, deterministic
    heal — this test pins the post-fix action and its resulting value.
    """
    overrides = {
        "dealer.item_priority": [
            "expired_medicine",
            "handcuffs",
            "cigarettes",
            "magnifying_glass",
            "burner_phone",
            "beer",
            "adrenaline",
            "inverter",
            "handsaw",
        ]
    }
    rules = _custom_rules(tmp_path, overrides)
    chamber = ChamberState.create((None, None), 1, 1)
    items = (ItemCounts.empty(), ItemCounts.of(cigarettes=1, expired_medicine=1))
    state = GameState.create(chamber, (3, 2), 3, Player.DEALER, items=items)

    branches = dealer_step(state, rules)
    assert len(branches) == 1
    weight, actions, _successor = branches[0]
    assert weight == pytest.approx(1.0)
    assert actions == (Action(ActionKind.USE, Item.CIGARETTES),)

    solver = _stub_solver(rules, GameMode.DOUBLE_OR_NOTHING)
    assert solver.value(state) == pytest.approx(0.5)


def test_burner_phone_min_shells_denies_a_same_turn_heal(tmp_path):
    """Item 4 (`size > 2` condition, and the uniform-over-all-slots
    distribution): fails against the code before the fix.

    Before the fix, the Dealer's burner_phone condition read "any future
    slot unresolved"; here every slot is already resolved
    (`dealer_knows={0,1,2}`), so the old condition is false and the Dealer's
    priority walk falls through to Adrenaline, stealing and using the
    player's cigarettes for a deterministic +1 heal before the chamber's
    known LIVE round ends its turn. After the fix, the condition is purely
    `size > 2` (here size == 3), so the phone fires anyway — on an all-
    resolved chamber this uniformly wastes the Dealer's only item use this
    turn (`solver.max_items_per_turn` forced to 1) and the Dealer takes the
    known LIVE shot at the player *without* healing first.

    Position: chamber (LIVE, BLANK, BLANK) fully known to the Dealer, Dealer
    holds burner_phone + adrenaline (no cigarettes of its own), player holds
    cigarettes, hp=(3, 2), max_hp=3.
    """
    rules = _custom_rules(tmp_path, {"solver.max_items_per_turn": 1})
    chamber = ChamberState.create((Shell.LIVE, Shell.BLANK, Shell.BLANK), 0, 0)
    items = (ItemCounts.of(cigarettes=1), ItemCounts.of(burner_phone=1, adrenaline=1))
    state = GameState.create(
        chamber, (3, 2), 3, Player.DEALER, items=items, dealer_knows=frozenset({0, 1, 2})
    )

    branches = dealer_step(state, rules)
    assert len(branches) == 1
    _weight, actions, _successor = branches[0]
    assert actions[0].item is Item.BURNER_PHONE

    solver = _stub_solver(rules, GameMode.DOUBLE_OR_NOTHING)
    assert solver.value(state) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# SPEC.md §7.7 item 1 — the parallel sweep's correctness claim.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reload_table_parallel_matches_serial(tmp_path):
    """The parallel sweep is a cost optimization and nothing else (SPEC.md §7.7 item 1).

    Jacobi iteration reads a frozen snapshot of the previous sweep's `V`, so
    no key's result within a sweep feeds another's and the `for key in keys`
    loop is a safe parallel unit. That argument is the *whole* justification
    for `_reload_sweeps_parallel`, and `_compute_reload_table`'s docstring
    cites this test by name as its proof — so the table built across worker
    processes must be bit-for-bit identical to the serial one, not merely
    close. `==` on the floats, deliberately: `pytest.approx` here would pass
    on a table that had genuinely drifted.

    Runs against `SMALL_OVERRIDES` (29 keys, small chambers) because the
    property is structural — it does not depend on the table being large.
    """
    rules = _custom_rules(tmp_path, SMALL_OVERRIDES)

    serial = Solver(rules, GameMode.BASE)
    serial._reload_workers = lambda: 0  # type: ignore[method-assign]
    serial._ensure_reload_table()

    parallel = Solver(rules, GameMode.BASE)
    parallel._reload_workers = lambda: 4  # type: ignore[method-assign]
    parallel._ensure_reload_table()

    assert serial._reload_table is not None
    assert parallel._reload_table is not None
    assert serial._reload_table.keys() == parallel._reload_table.keys()
    for key in serial._reload_table:
        assert serial._reload_table[key] == parallel._reload_table[key], (
            f"reload table entry {key} differs between the serial and parallel "
            "sweeps: the keys within a sweep are not independent after all"
        )


def test_parallel_progress_counts_tasks_not_whole_keys(tmp_path):
    """Heaviest-first tasks finish each key's lightest config last, so counting
    completed keys sat at ~1/29 until the sweep's end. One event per finished
    task now, scaled to keys: non-decreasing, ending at keys_total."""
    rules = _custom_rules(tmp_path, SMALL_OVERRIDES)
    solver = Solver(rules, GameMode.BASE)
    solver._reload_workers = lambda: 2  # type: ignore[method-assign]
    events = []
    solver.build_reload_table(events.append)
    first = [e for e in events if e.sweep == 1 and e.phase == "key"]
    n_tasks = len(_all_reload_keys(rules)) * len(_shell_configs(rules.shells))
    assert len(first) == n_tasks
    done = [e.keys_done for e in first]
    assert done == sorted(done) and done[-1] == first[-1].keys_total


def _build_variant(rules: Rules, **solver_kw):
    """Build a table from `rules` with solver keys replaced in place.

    `replace` keeps the fingerprint, so every variant draws the same samples
    and differs only in how it iterates."""
    r = dataclasses.replace(rules, solver=dataclasses.replace(rules.solver, **solver_kw))
    solver = Solver(r, GameMode.BASE)
    solver._reload_workers = lambda: 0  # type: ignore[method-assign]
    sweeps = []
    solver.build_reload_table(lambda p: sweeps.append(p) if p.phase == "sweep" else None)
    assert solver._reload_table is not None
    return solver._reload_table, sum(1 for p in sweeps if p.stage == "main")


@pytest.mark.slow
def test_aitken_and_warm_start_land_within_tolerance_of_the_plain_fixed_point(tmp_path):
    """SPEC.md §7.7 / decisions-pending 13c, 13d: both speed-ups move the answer
    by less than reload_fixedpoint_tolerance, and never cost sweeps."""
    rules = _custom_rules(
        tmp_path, {**SMALL_OVERRIDES, "solver.reload_max_iterations": 60}
    )
    tol = rules.solver.reload_fixedpoint_tolerance
    off = {"reload_aitken": False, "reload_warm_start_samples": 0}
    exact, _ = _build_variant(rules, **off, reload_fixedpoint_tolerance=1e-9)
    plain, plain_sweeps = _build_variant(rules, **off)
    for kw in (
        {"reload_aitken": True, "reload_warm_start_samples": 0},
        {"reload_aitken": False, "reload_warm_start_samples": 2},
        {"reload_aitken": True, "reload_warm_start_samples": 2},
    ):
        table, sweeps = _build_variant(rules, **kw)
        assert max(abs(table[k] - exact[k]) for k in exact) < tol, kw
        assert sweeps <= plain_sweeps, kw
    assert max(abs(plain[k] - exact[k]) for k in exact) < tol


def test_aitken_jumps_a_geometric_sequence_to_its_limit():
    key = (1, 1, 2)
    seq = [{key: 0.4 + 0.5 * 0.3**n} for n in range(4)]
    v, hist = _aitken_next([], seq[0])
    for x in seq[1:]:
        v, hist = _aitken_next(hist, x)
    assert v[key] == pytest.approx(0.4, abs=1e-12) and hist == [v]
    # Not yet linear (ratios disagree): no jump.
    wild = [{key: x} for x in (0.0, 0.5, 0.6, 0.9)]
    v, hist = _aitken_next([], wild[0])
    for x in wild[1:]:
        v, hist = _aitken_next(hist, x)
    assert v == wild[-1]


def test_warm_start_table_is_cached_separately_by_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules = _custom_rules(
        rules_dir,
        {**SMALL_OVERRIDES, "solver.reload_table_cache": True,
         "solver.reload_item_draw_samples": 8, "solver.reload_warm_start_samples": 2},
    )  # fmt: skip
    Solver(rules, GameMode.BASE)._ensure_reload_table()
    names = sorted(f.name for f in (tmp_path / "xdg" / "buckshotcalc").glob("*.json"))
    assert sorted(n.endswith("+samples2.json") for n in names) == [False, True]


def test_stale_engine_table_seeds_the_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules = _custom_rules(
        rules_dir,
        {**SMALL_OVERRIDES, "solver.reload_table_cache": True,
         "solver.reload_item_draw_samples": 8, "solver.reload_warm_start_samples": 0,
         "solver.reload_max_iterations": 64},
    )  # fmt: skip
    from buckshotcalc.engine import solver as solver_mod

    sweeps: list[int] = []

    def build():
        solver = Solver(rules, GameMode.BASE)
        solver._compute_reload_table(
            lambda p: sweeps.append(p.sweep) if p.phase == "sweep" else None
        )

    build()
    first = max(sweeps)
    cache = tmp_path / "xdg" / "buckshotcalc"
    (path,) = cache.glob("*.json")
    truth = solver_mod._read_cache(rules, GameMode.BASE)
    path.rename(path.with_name(path.name.replace("_e", "_eold", 1)))  # an older engine's file
    assert solver_mod._read_cache(rules, GameMode.BASE) is None
    assert solver_mod._read_stale_cache(rules, GameMode.BASE) == truth

    sweeps.clear()
    build()
    assert max(sweeps) < first
    assert not list(cache.glob("*+samples*.json"))


def test_odd_total_extra_shell_is_blank_by_default():
    """RoundManager.gd:148-150: live = max(1, total // 2), blank = the rest."""
    configs = _shell_configs(RULES.shells)
    assert sum(w for w, _, _ in configs) == pytest.approx(1.0)
    for _, live, blank in configs:
        assert live == max(1, (live + blank) // 2)
    three = [(live, blank, w) for w, live, blank in configs if live + blank == 3]
    assert three == [(1, 2, pytest.approx(1 / 7))]


@pytest.mark.parametrize("extra", ["live", "either"])
def test_odd_total_extra_shell_alternatives(extra):
    configs = _shell_configs(dataclasses.replace(RULES.shells, odd_total_extra_shell=extra))
    assert sum(w for w, _, _ in configs) == pytest.approx(1.0)
    three = {(live, blank): w for w, live, blank in configs if live + blank == 3}
    if extra == "live":
        assert three == {(2, 1): pytest.approx(1 / 7)}
    else:
        assert three == {(2, 1): pytest.approx(1 / 14), (1, 2): pytest.approx(1 / 14)}


def test_wasted_adrenaline_never_tops_an_exact_tie():
    """One live shell, Dealer on 1 HP: shooting wins outright, and so does wasting first.

    The two tie at 1.0 exactly; the recommendation must be the shot, never the waste.
    """
    state = _state(1, 0, hp=(2, 1), turn=Player.PLAYER,
                   items=(_items(adrenaline=1), ItemCounts.empty()))
    children = _stub_solver().evaluate(state).children
    waste = [c for c in children if c.action.item is Item.ADRENALINE]
    assert waste and waste[0].value == children[0].value == 1.0
    assert children[0].action.kind is ActionKind.SHOOT_OPPONENT


# ---------------------------------------------------------------------------
# Tie-break: on an exact tie prefer the move that risks nothing.
# ---------------------------------------------------------------------------


def test_exact_tie_prefers_shoot_opponent_over_shoot_self():
    """Dealer cuffed, 1 live + 2 blank unknown, hp (2,1): both shots are worth
    2/3 + R/3 (R = reload value at hp (1,1)), so they tie exactly."""
    solver = _stub_solver()
    state = _state(1, 2, hp=(2, 1), turn=Player.PLAYER, max_hp=3, cuffed=(False, True))
    result = solver.evaluate(state)
    by_kind = {c.action.kind: c.value for c in result.children}
    self_v, opp_v = by_kind[ActionKind.SHOOT_SELF], by_kind[ActionKind.SHOOT_OPPONENT]
    assert self_v == pytest.approx(opp_v, abs=1e-12)
    assert result.children[0].action.kind is ActionKind.SHOOT_OPPONENT


def test_noop_cigarettes_at_max_hp_is_not_the_top_pick():
    solver = _stub_solver()
    state = _state(
        1, 1, hp=(3, 3), turn=Player.PLAYER,
        items=(_items(cigarettes=1), ItemCounts.empty()),
    )
    top = solver.evaluate(state).children[0].action
    assert not (top.kind is ActionKind.USE and top.item is Item.CIGARETTES)


def test_two_inverters_are_not_a_free_magnifying_glass():
    """1L1B, hp (1,1), two Inverters (SPEC.md §5.2). Inverting unseen makes both
    shells match, so shooting the Dealer wins 1/2 and a blank leaves the Dealer a
    known blank, which it fires at itself into a reload: 1/2 + 1/2 * R(1,1) = 0.75.
    The old reveal-on-invert model said 1.0 (invert, look, invert back if blank)."""
    solver = _stub_solver(mode=GameMode.DOUBLE_OR_NOTHING)
    two_inv = _state(1, 1, hp=(1, 1), turn=Player.PLAYER, max_hp=2,
                     items=(_items(inverter=2), ItemCounts.empty()))
    glass = _state(1, 1, hp=(1, 1), turn=Player.PLAYER, max_hp=2,
                   items=(_items(magnifying_glass=1), ItemCounts.empty()))
    assert solver.value(two_inv) == pytest.approx(0.75)
    assert solver.value(glass) == pytest.approx(1.0)


def test_noop_use_loses_a_tie_with_a_real_item_use():
    """1L2B at full HP with cigarettes, glass and beer: smoking ties the best item
    line exactly (the cigarette is worth nothing more) and used to win on `Item`
    order, so the calc said "use cigarettes"."""
    state = _state(
        1, 2, hp=(2, 2), turn=Player.PLAYER, max_hp=2,
        items=(_items(cigarettes=1, magnifying_glass=1, beer=1), ItemCounts.empty()),
    )
    children = _stub_solver(mode=GameMode.DOUBLE_OR_NOTHING).evaluate(state).children
    smoke = next(c for c in children if c.action.item is Item.CIGARETTES)
    assert smoke.value == pytest.approx(children[0].value, abs=1e-12)
    assert children[0].action.item is not Item.CIGARETTES


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def _cancel_state() -> GameState:
    return _state(2, 2, hp=(3, 3), turn=Player.PLAYER, items=(_items(beer=1), ItemCounts.empty()))


def test_preset_cancel_event_raises():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SearchCancelled):
        _stub_solver().evaluate(_cancel_state(), cancel)


def test_cancelled_search_leaves_a_valid_memo():
    state = _cancel_state()
    solver = _stub_solver()
    cancel = threading.Event()
    real = solver.reload_value
    calls = 0

    def trip(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 3:
            cancel.set()
        return real(*args, **kw)

    solver.reload_value = trip
    with pytest.raises(SearchCancelled):
        solver.evaluate(state, cancel)
    assert calls >= 3 and solver._memo  # cancelled partway, not before or after
    solver.reload_value = real
    fresh = solver.evaluate(state)
    expected = _stub_solver().evaluate(state)
    assert fresh.value == expected.value
    assert [(c.action, c.value) for c in fresh.children] == [
        (c.action, c.value) for c in expected.children
    ]
