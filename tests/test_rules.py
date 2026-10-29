"""Tests for buckshotcalc.engine.rules (SPEC.md §10, M1 acceptance)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from buckshotcalc.engine.rules import (
    RulesError,
    load_rules,
    rules_fingerprint,
)
from buckshotcalc.engine.state import Item

REAL_RULES_PATH = Path(__file__).resolve().parent.parent / "buckshotcalc" / "rules.toml"


def _load_real_dict() -> dict:
    with REAL_RULES_PATH.open("rb") as f:
        return tomllib.load(f)


def _write_toml(tmp_path: Path, data: dict) -> Path:
    """Serialize `data` back to a .toml file under tmp_path. Minimal writer,
    sufficient for the scalar/list/table shapes rules.toml actually uses."""
    path = tmp_path / "rules.toml"
    path.write_text(_dump_toml(data))
    return path


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


# ---------------------------------------------------------------------------
# Loading the real, checked-in file
# ---------------------------------------------------------------------------


def test_loads_real_rules_toml():
    rules = load_rules()
    assert rules.schema_version == 1


def test_shells_fields_match_file():
    rules = load_rules()
    assert rules.shells.min_total == 2
    assert rules.shells.max_total == 8
    assert rules.shells.min_live == 1
    assert rules.shells.min_blank == 1
    assert rules.shells.even_total_splits_equally is True
    assert rules.shells.odd_total_differs_by_one is True
    assert rules.shells.odd_total_extra_shell == "blank"


def test_health_fields_match_file():
    rules = load_rules()
    assert rules.health.possible_max_hp == (2, 3, 4)
    assert rules.health.equal_for_both_players is True
    assert rules.health.resets_on_new_round is True
    assert rules.health.carries_through_reload is True


def test_items_global_fields_match_file():
    rules = load_rules()
    assert rules.items.max_slots == 8
    assert rules.items.dealt_per_round_min == 2
    assert rules.items.dealt_per_round_max == 5
    assert rules.items.base_items == (
        Item.CIGARETTES,
        Item.HANDCUFFS,
        Item.BEER,
        Item.HANDSAW,
        Item.MAGNIFYING_GLASS,
    )
    assert rules.items.double_or_nothing_items == (
        Item.ADRENALINE,
        Item.EXPIRED_MEDICINE,
        Item.BURNER_PHONE,
        Item.INVERTER,
    )
    assert rules.items.multiplayer_items == (
        Item.ADRENALINE,
        Item.BURNER_PHONE,
        Item.INVERTER,
        Item.REMOTE,
    )


def test_item_semantics_match_file():
    rules = load_rules()
    assert rules.item_semantics.cigarettes.heal == 1
    assert rules.item_semantics.handcuffs.skips_opponent_turns == 1
    assert rules.item_semantics.handcuffs.stackable_on_cuffed_target is False
    assert rules.item_semantics.beer.ejects_chambered_shell is True
    assert rules.item_semantics.handsaw.damage == 2
    assert rules.item_semantics.handsaw.consumed_on == ("shot_fired",)
    assert rules.item_semantics.magnifying_glass.reveals_slot == 0
    assert rules.item_semantics.adrenaline.can_steal_adrenaline is False
    assert rules.item_semantics.expired_medicine.heal == 2
    assert rules.item_semantics.expired_medicine.harm == 1
    assert rules.item_semantics.expired_medicine.heal_probability == 0.5
    assert rules.item_semantics.burner_phone.excludes_slot == 0
    assert rules.item_semantics.burner_phone.player_index_decrement_from == 7
    assert rules.item_semantics.inverter.resolves_slot_on_use is True
    assert rules.item_semantics.remote.min_players == 3

    # for_item routes to the correct sub-config
    assert rules.item_semantics.for_item(Item.HANDSAW) is rules.item_semantics.handsaw


def test_dealer_fields_match_file():
    rules = load_rules()
    assert rules.dealer.knows_shell_when_one_remains is True
    assert rules.dealer.unknown_shell_equal_coinflip == 0.5
    assert rules.dealer.medicine_requires_hp_at_least == 2
    assert rules.dealer.medicine_requires_no_cigarettes_held is True
    assert rules.dealer.handcuffs_requires_min_shells_remaining == 2
    assert rules.dealer.beer_requires_min_shells_remaining == 2
    assert rules.dealer.glass_requires_min_shells_remaining == 2
    assert rules.dealer.burner_phone_min_shells_remaining == 3
    assert rules.dealer.item_priority == (
        Item.HANDCUFFS,
        Item.CIGARETTES,
        Item.EXPIRED_MEDICINE,
        Item.MAGNIFYING_GLASS,
        Item.BURNER_PHONE,
        Item.BEER,
        Item.ADRENALINE,
        Item.INVERTER,
        Item.HANDSAW,
    )


# ---------------------------------------------------------------------------
# [mode] — correctness-patch item 1: an explicit, loaded mode declaration.
# ---------------------------------------------------------------------------


def test_mode_field_matches_file():
    from buckshotcalc.engine.state import GameMode

    rules = load_rules()
    assert rules.mode.mode is GameMode.DOUBLE_OR_NOTHING
    assert rules.is_endless is True


def test_mode_missing_table_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["mode"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="mode"):
        load_rules(path)


def test_mode_unknown_value_raises(tmp_path: Path):
    data = _load_real_dict()
    data["mode"]["mode"] = "not_a_mode"
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="mode"):
        load_rules(path)


def test_mode_campaign_value_loads_and_is_not_endless(tmp_path: Path):
    from buckshotcalc.engine.state import GameMode

    data = _load_real_dict()
    data["mode"]["mode"] = "base"
    path = _write_toml(tmp_path, data)
    rules = load_rules(path)
    assert rules.mode.mode is GameMode.BASE
    assert rules.is_endless is False


# ---------------------------------------------------------------------------
# Dealer size-guard keys (correctness-patch item 2) must be >= 1.
# ---------------------------------------------------------------------------


def test_glass_requires_min_shells_remaining_below_one_raises(tmp_path: Path):
    data = _load_real_dict()
    data["dealer"]["glass_requires_min_shells_remaining"] = 0
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="glass_requires_min_shells_remaining"):
        load_rules(path)


def test_turns_round_solver_fields_match_file():
    rules = load_rules()
    assert rules.turns.player_moves_first is True
    assert rules.turns.base_damage == 1
    assert rules.round_.campaign_rounds_to_win == 3
    assert rules.round_.double_or_nothing_zero_hp_ends_run is True
    assert rules.round_.player_moves_first_after_reload is True
    assert rules.round_.cuffs_clear_on_reload is True
    assert rules.solver.max_items_per_turn == 6
    # 1e-3, not 1e-6: set against the table's own +/-0.0067 sampling error
    # rather than below it (docs/decisions-pending.md item 11).
    assert rules.solver.reload_fixedpoint_tolerance == 1e-3
    assert rules.solver.reload_table_cache is True
    assert rules.solver.redraw_budget_ms == 16
    assert rules.solver.reload_item_draw_samples == 64
    assert rules.solver.reload_max_iterations == 64
    assert rules.solver.reload_workers == -1
    assert rules.solver.reload_aitken is True
    assert rules.solver.reload_warm_start_samples == 4


# ---------------------------------------------------------------------------
# M4 additions (SPEC.md §15.11, §15.12): round.player_moves_first_after_reload,
# round.cuffs_clear_on_reload, solver.reload_item_draw_samples,
# solver.reload_max_iterations. Loader must reject the file in BOTH
# directions: missing (old files without these keys) and unknown (typos).
# ---------------------------------------------------------------------------


def test_round_missing_player_moves_first_after_reload_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["round"]["player_moves_first_after_reload"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="player_moves_first_after_reload"):
        load_rules(path)


def test_round_missing_cuffs_clear_on_reload_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["round"]["cuffs_clear_on_reload"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="cuffs_clear_on_reload"):
        load_rules(path)


def test_round_unknown_key_raises(tmp_path: Path):
    data = _load_real_dict()
    data["round"]["some_typo_key"] = True
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="some_typo_key"):
        load_rules(path)


def test_solver_missing_reload_item_draw_samples_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["solver"]["reload_item_draw_samples"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_item_draw_samples"):
        load_rules(path)


def test_solver_missing_reload_max_iterations_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["solver"]["reload_max_iterations"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_max_iterations"):
        load_rules(path)


def test_solver_missing_reload_workers_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["solver"]["reload_workers"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_workers"):
        load_rules(path)


def test_solver_reload_workers_accepts_auto_serial_and_explicit(tmp_path: Path):
    """`-1` is the auto sentinel, `0`/`1` are serial, `n` is n workers (SPEC.md §7.7 item 1).

    The sentinel exists because TOML has no null and a literal core count in
    a committed `rules.toml` would be a machine-specific constant (SPEC.md §0.1
    non-negotiable #2). Only values below `-1` are meaningless.
    """
    for value in (-1, 0, 1, 8):
        data = _load_real_dict()
        data["solver"]["reload_workers"] = value
        rules = load_rules(_write_toml(tmp_path, data))
        assert rules.solver.reload_workers == value


def test_solver_reload_workers_below_sentinel_raises(tmp_path: Path):
    data = _load_real_dict()
    data["solver"]["reload_workers"] = -2
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_workers"):
        load_rules(path)


def test_solver_unknown_key_raises(tmp_path: Path):
    data = _load_real_dict()
    data["solver"]["some_typo_key"] = 1
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="some_typo_key"):
        load_rules(path)


def test_reload_item_draw_samples_below_one_raises(tmp_path: Path):
    data = _load_real_dict()
    data["solver"]["reload_item_draw_samples"] = 0
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_item_draw_samples"):
        load_rules(path)


def test_reload_max_iterations_below_one_raises(tmp_path: Path):
    data = _load_real_dict()
    data["solver"]["reload_max_iterations"] = 0
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="reload_max_iterations"):
        load_rules(path)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_and_sensitive_to_bytes(tmp_path: Path):
    data = _load_real_dict()
    path_a = _write_toml(tmp_path, data)
    fp_a1 = rules_fingerprint(path_a)
    fp_a2 = rules_fingerprint(path_a)
    assert fp_a1 == fp_a2

    data["schema_version"] = 1  # unchanged, but rewrite to a second file
    path_b = tmp_path / "rules_copy.toml"
    path_b.write_text(path_a.read_text())
    assert rules_fingerprint(path_b) == fp_a1

    path_a.write_text(path_a.read_text() + "\n# trailing comment\n")
    assert rules_fingerprint(path_a) != fp_a1


def test_load_rules_exposes_fingerprint_matching_helper():
    rules = load_rules()
    assert rules.fingerprint == rules_fingerprint(REAL_RULES_PATH)


# ---------------------------------------------------------------------------
# Schema violations, each built from a mutated copy of the real file
# ---------------------------------------------------------------------------


def test_missing_required_key_raises(tmp_path: Path):
    data = _load_real_dict()
    del data["shells"]["min_total"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="min_total"):
        load_rules(path)


def test_unknown_key_raises(tmp_path: Path):
    data = _load_real_dict()
    data["shells"]["bogus_key"] = 42
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="bogus_key"):
        load_rules(path)


def test_bad_schema_version_raises(tmp_path: Path):
    data = _load_real_dict()
    data["schema_version"] = 2
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="schema_version"):
        load_rules(path)


def test_out_of_range_probability_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["expired_medicine"]["heal_probability"] = 1.5
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="heal_probability"):
        load_rules(path)


def test_unknown_item_name_in_item_priority_raises(tmp_path: Path):
    data = _load_real_dict()
    data["dealer"]["item_priority"] = list(data["dealer"]["item_priority"]) + ["not_an_item"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="item_priority"):
        load_rules(path)


def test_min_total_greater_than_max_total_raises(tmp_path: Path):
    data = _load_real_dict()
    data["shells"]["min_total"] = 9
    data["shells"]["max_total"] = 8
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="min_total"):
        load_rules(path)


def test_empty_possible_max_hp_raises(tmp_path: Path):
    data = _load_real_dict()
    data["health"]["possible_max_hp"] = []
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="possible_max_hp"):
        load_rules(path)


def test_item_priority_missing_conditioned_item_raises(tmp_path: Path):
    data = _load_real_dict()
    data["dealer"]["item_priority"] = [
        name for name in data["dealer"]["item_priority"] if name != "handcuffs"
    ]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="item_priority"):
        load_rules(path)


def test_duplicate_item_priority_raises(tmp_path: Path):
    data = _load_real_dict()
    data["dealer"]["item_priority"] = list(data["dealer"]["item_priority"]) + ["handcuffs"]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="item_priority"):
        load_rules(path)


def test_max_items_per_turn_below_one_raises(tmp_path: Path):
    data = _load_real_dict()
    data["solver"]["max_items_per_turn"] = 0
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="max_items_per_turn"):
        load_rules(path)


# ---------------------------------------------------------------------------
# Per-item semantics are validated against a declared schema, not the file's
# own types. A type-swapped value must be rejected, not silently accepted.
# ---------------------------------------------------------------------------


def test_type_swapped_item_semantic_value_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["cigarettes"]["heal"] = True  # bool where int is declared
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="heal"):
        load_rules(path)


def test_type_swapped_int_for_bool_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["cigarettes"]["usable_at_max_hp"] = 1  # int where bool is declared
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="usable_at_max_hp"):
        load_rules(path)


def test_type_swapped_consumed_on_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["handsaw"]["consumed_on"] = 3  # scalar where a string list is declared
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="consumed_on"):
        load_rules(path)


# ---------------------------------------------------------------------------
# Dealer burner-phone condition (SPEC.md §15.8, VERIFIED from decompiled source)
# ---------------------------------------------------------------------------


def test_dealer_burner_phone_condition_loaded():
    rules = load_rules()
    assert rules.dealer.burner_phone_min_shells_remaining == 3


def test_item_priority_missing_burner_phone_raises(tmp_path: Path):
    data = _load_real_dict()
    data["dealer"]["item_priority"] = [
        name for name in data["dealer"]["item_priority"] if name != "burner_phone"
    ]
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="burner_phone"):
        load_rules(path)


# ---------------------------------------------------------------------------
# Cross-section consistency
# ---------------------------------------------------------------------------


def test_min_live_plus_min_blank_exceeding_min_total_raises(tmp_path: Path):
    data = _load_real_dict()
    data["shells"]["min_total"] = 1  # cannot hold the required one live + one blank
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="min_total"):
        load_rules(path)


def test_dealt_per_round_min_above_max_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["dealt_per_round_min"] = 6
    data["items"]["dealt_per_round_max"] = 5
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="dealt_per_round_min"):
        load_rules(path)


def test_dealt_per_round_max_above_max_slots_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["dealt_per_round_max"] = 9  # max_slots is 8
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="dealt_per_round_max"):
        load_rules(path)


def test_non_positive_handsaw_damage_raises(tmp_path: Path):
    data = _load_real_dict()
    data["items"]["handsaw"]["damage"] = 0
    path = _write_toml(tmp_path, data)
    with pytest.raises(RulesError, match="handsaw.damage"):
        load_rules(path)
