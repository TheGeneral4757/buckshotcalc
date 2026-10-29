"""`rules.toml` loader with schema validation (SPEC.md §10, §11).

Pure stdlib. The engine must never hardcode a value that lives in
`rules.toml` — this module is the single point where the file is read and
validated, and every other module takes a loaded `Rules` object rather than
reaching for a global.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from buckshotcalc.engine.state import GameMode, Item

_DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules.toml"


class RulesError(ValueError):
    """Raised when `rules.toml` fails schema validation, naming the offending key."""


# ---------------------------------------------------------------------------
# Typed sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShellsConfig:
    min_total: int
    max_total: int
    min_live: int
    min_blank: int
    even_total_splits_equally: bool
    odd_total_differs_by_one: bool
    odd_total_extra_shell: str  # "blank" | "live" | "either" (50/50)


@dataclass(frozen=True, slots=True)
class HealthConfig:
    possible_max_hp: tuple[int, ...]
    equal_for_both_players: bool
    resets_on_new_round: bool
    carries_through_reload: bool


@dataclass(frozen=True, slots=True)
class ItemsConfig:
    max_slots: int
    dealt_per_round_min: int
    dealt_per_round_max: int
    base_items: tuple[Item, ...]
    double_or_nothing_items: tuple[Item, ...]
    multiplayer_items: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class CigarettesConfig:
    heal: int
    usable_at_max_hp: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class HandcuffsConfig:
    skips_opponent_turns: int
    stackable_on_cuffed_target: bool
    clears_at_end_of_round: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class BeerConfig:
    ejects_chambered_shell: bool
    reveals_ejected_shell: bool
    clears_sawed: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class HandsawConfig:
    damage: int
    consumed_on: tuple[str, ...]
    persists_across_turns: bool
    stackable: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class MagnifyingGlassConfig:
    reveals_slot: int
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class AdrenalineConfig:
    steals_opponent_item: bool
    uses_stolen_item_immediately: bool
    can_steal_adrenaline: bool
    usable_with_nothing_to_steal: bool
    expiry_seconds: int
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class ExpiredMedicineConfig:
    heal: int
    harm: int
    heal_probability: float
    can_be_lethal: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class BurnerPhoneConfig:
    reveals_future_shell: bool
    excludes_slot: int
    player_index_decrement_from: int
    stacks: bool
    index_is_relative: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class InverterConfig:
    inverts_chambered_shell: bool
    resolves_slot_on_use: bool
    retains_turn: bool


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    reverses_turn_order: bool
    min_players: int
    retains_turn: bool


ItemConfig = (
    CigarettesConfig
    | HandcuffsConfig
    | BeerConfig
    | HandsawConfig
    | MagnifyingGlassConfig
    | AdrenalineConfig
    | ExpiredMedicineConfig
    | BurnerPhoneConfig
    | InverterConfig
    | RemoteConfig
)


@dataclass(frozen=True, slots=True)
class ItemSemantics:
    """Per-item configuration, keyed by `Item` via `for_item`.

    A plain `Mapping[Item, ...]` would be unhashable and the per-item
    payloads have different shapes, so each item gets its own named field
    and its own small frozen dataclass type.
    """

    cigarettes: CigarettesConfig
    handcuffs: HandcuffsConfig
    beer: BeerConfig
    handsaw: HandsawConfig
    magnifying_glass: MagnifyingGlassConfig
    adrenaline: AdrenalineConfig
    expired_medicine: ExpiredMedicineConfig
    burner_phone: BurnerPhoneConfig
    inverter: InverterConfig
    remote: RemoteConfig

    def for_item(self, item: Item) -> ItemConfig:
        """The config dataclass for `item`, looked up by its `rules.toml` key."""
        return getattr(self, item.config_name)


@dataclass(frozen=True, slots=True)
class ModeConfig:
    """Which mode's Dealer AI this `rules.toml` targets (correctness-patch item 1).

    Distinct from the `GameMode` a `Solver` is constructed with (which
    selects an *item list* for reload draws, SPEC.md §3.2/§7.4, and is
    supplied per call): this is a static declaration of what the operator
    actually plays, read only to gate the two Dealer AI branches in
    `engine/dealer.py` that observation shows are conditioned on
    the endless-mode flag (majority-rule shot targeting, and the Dealer's
    private count-closure deduction). v1 targets Double or Nothing, so this
    is `GameMode.DOUBLE_OR_NOTHING` today and the two gated behaviours must
    stay byte-for-byte what they are now — only their conditionality is new.
    """

    mode: GameMode


@dataclass(frozen=True, slots=True)
class DealerConfig:
    knows_shell_after_own_magnifying_glass: bool
    knows_shell_when_one_remains: bool
    learns_from_player_item_use: bool
    unknown_shell_more_live_shoots_player: bool
    unknown_shell_more_blank_shoots_self: bool
    unknown_shell_equal_coinflip: float
    always_uses_handcuffs: bool
    handcuffs_requires_min_shells_remaining: int
    uses_cigarettes_below_max_hp: bool
    medicine_requires_below_max_hp: bool
    medicine_requires_hp_at_least: int
    medicine_requires_no_cigarettes_held: bool
    uses_handsaw_only_when_shooting_player: bool
    uses_beer_when_knows_blank_or_not_knows_live: bool
    beer_requires_min_shells_remaining: int
    beer_keeps_known_blank_target: bool
    uses_inverter_only_when_knows_blank: bool
    adrenaline_requires_wanted_item_not_held: bool
    adrenaline_wants_item_with_satisfied_condition: bool
    uses_glass_when_chambered_shell_unknown: bool
    glass_requires_min_shells_remaining: int
    burner_phone_min_shells_remaining: int
    item_priority: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class TurnsConfig:
    player_moves_first: bool
    blank_self_shot_retains_turn: bool
    live_self_shot_ends_turn: bool
    shooting_opponent_ends_turn: bool
    base_damage: int


@dataclass(frozen=True, slots=True)
class RoundConfig:
    campaign_rounds_to_win: int
    double_or_nothing_zero_hp_ends_run: bool
    player_moves_first_after_reload: bool
    cuffs_clear_on_reload: bool


@dataclass(frozen=True, slots=True)
class SolverConfig:
    max_items_per_turn: int
    reload_fixedpoint_tolerance: float
    reload_table_cache: bool
    redraw_budget_ms: int
    reload_item_draw_samples: int
    reload_max_iterations: int
    reload_workers: int
    reload_aitken: bool
    reload_warm_start_samples: int


@dataclass(frozen=True, slots=True)
class Rules:
    schema_version: int
    shells: ShellsConfig
    health: HealthConfig
    items: ItemsConfig
    item_semantics: ItemSemantics
    dealer: DealerConfig
    mode: ModeConfig
    turns: TurnsConfig
    round_: RoundConfig
    solver: SolverConfig
    fingerprint: str

    @property
    def is_endless(self) -> bool:
        """Whether the two endless-mode gated Dealer AI branches are active.

        Gates SPEC.md §3.3's majority-rule shot targeting and the Dealer's
        private count-closure deduction (its count-closure deduction) in
        `engine/dealer.py` — both `VERIFIED` by observation as
        Double-or-Nothing-only; the base campaign's Dealer is count-blind and
        flips a fair coin instead. `knows_shell_when_one_remains` is
        deliberately NOT gated by this — a single remaining shell is knowable
        in both modes.
        """
        return self.mode.mode is GameMode.DOUBLE_OR_NOTHING


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

# Items with a dealer usage condition in rules.toml; item_priority must cover
# (be a superset of) these. Eight come from SPEC.md §3.3's IF-AND-ONLY-IF table;
# burner_phone's is now `VERIFIED` by observation too (SPEC.md §15.8):
# `size > 2`, unrelated to §3.3's silence on it.
_ITEMS_WITH_DEALER_CONDITIONS: dict[str, str] = {
    "always_uses_handcuffs": "handcuffs",
    "uses_cigarettes_below_max_hp": "cigarettes",
    "medicine_requires_below_max_hp": "expired_medicine",
    "uses_handsaw_only_when_shooting_player": "handsaw",
    "uses_beer_when_knows_blank_or_not_knows_live": "beer",
    "uses_inverter_only_when_knows_blank": "inverter",
    "adrenaline_requires_wanted_item_not_held": "adrenaline",
    "uses_glass_when_chambered_shell_unknown": "magnifying_glass",
    "burner_phone_min_shells_remaining": "burner_phone",
}


def _require_keys(table: object, expected: set[str], path: str) -> dict:
    if not isinstance(table, dict):
        raise RulesError(f"{path}: expected a table, got {type(table).__name__}")
    actual = set(table.keys())
    missing = expected - actual
    if missing:
        raise RulesError(f"{path}: missing required key(s): {sorted(missing)}")
    unknown = actual - expected
    if unknown:
        raise RulesError(f"{path}: unknown key(s): {sorted(unknown)}")
    return table


def _get(table: dict, key: str, expected_type: type, path: str) -> object:
    value = table[key]
    # bool is a subclass of int; only accept int where int (not bool) is asked for.
    if expected_type is int and isinstance(value, bool):
        raise RulesError(f"{path}.{key}: expected int, got bool")
    if expected_type is float and isinstance(value, (int,)) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, expected_type):
        raise RulesError(
            f"{path}.{key}: expected {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


def _item(table: dict, key: str, path: str) -> Item:
    name = _get(table, key, str, path)
    try:
        return Item.from_config_name(name)
    except KeyError:
        raise RulesError(f"{path}.{key}: unknown item name {name!r}") from None


def _item_tuple(table: dict, key: str, path: str) -> tuple[Item, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RulesError(f"{path}.{key}: expected a list of item name strings")
    items = []
    for name in value:
        try:
            items.append(Item.from_config_name(name))
        except KeyError:
            raise RulesError(f"{path}.{key}: unknown item name {name!r}") from None
    return tuple(items)


def _str_tuple(table: dict, key: str, path: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RulesError(f"{path}.{key}: expected a list of strings")
    return tuple(value)


def _int_tuple(table: dict, key: str, path: str) -> tuple[int, ...]:
    value = table[key]
    if not isinstance(value, list) or not value or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        raise RulesError(f"{path}.{key}: expected a non-empty list of ints")
    return tuple(value)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def rules_fingerprint(rules_path: Path) -> str:
    """sha256 hex digest of the raw file bytes at `rules_path`."""
    return hashlib.sha256(rules_path.read_bytes()).hexdigest()


def load_rules(path: Path | None = None) -> Rules:
    """Load and validate `rules.toml` into a `Rules` object.

    Raises `RulesError` (a `ValueError`) naming the offending key on any
    schema violation: missing key, unknown key, wrong type, or an
    out-of-range / inconsistent value.
    """
    resolved_path = path if path is not None else _DEFAULT_RULES_PATH
    with resolved_path.open("rb") as f:
        raw = tomllib.load(f)

    fingerprint = rules_fingerprint(resolved_path)

    top_level_expected = {
        "schema_version",
        "shells",
        "health",
        "items",
        "dealer",
        "mode",
        "turns",
        "round",
        "solver",
    }
    _require_keys(raw, top_level_expected, "<root>")

    schema_version = raw["schema_version"]
    if schema_version != 1:
        raise RulesError(f"schema_version: expected 1, got {schema_version!r}")

    shells = _load_shells(raw["shells"])
    health = _load_health(raw["health"])
    items, item_semantics = _load_items(raw["items"])
    dealer = _load_dealer(raw["dealer"])
    mode = _load_mode(raw["mode"])
    turns = _load_turns(raw["turns"])
    round_ = _load_round(raw["round"])
    solver = _load_solver(raw["solver"])

    _cross_validate(shells, items, item_semantics, dealer)

    return Rules(
        schema_version=schema_version,
        shells=shells,
        health=health,
        items=items,
        item_semantics=item_semantics,
        dealer=dealer,
        mode=mode,
        turns=turns,
        round_=round_,
        solver=solver,
        fingerprint=fingerprint,
    )


def _load_shells(table: dict) -> ShellsConfig:
    path = "shells"
    _require_keys(
        table,
        {
            "min_total",
            "max_total",
            "min_live",
            "min_blank",
            "even_total_splits_equally",
            "odd_total_differs_by_one",
            "odd_total_extra_shell",
        },
        path,
    )
    min_total = _get(table, "min_total", int, path)
    max_total = _get(table, "max_total", int, path)
    if min_total > max_total:
        raise RulesError(f"{path}: min_total ({min_total}) > max_total ({max_total})")
    extra = _get(table, "odd_total_extra_shell", str, path)
    if extra not in ("blank", "live", "either"):
        raise RulesError(
            f"{path}.odd_total_extra_shell: must be 'blank', 'live' or 'either', got {extra!r}"
        )
    return ShellsConfig(
        min_total=min_total,
        max_total=max_total,
        min_live=_get(table, "min_live", int, path),
        min_blank=_get(table, "min_blank", int, path),
        even_total_splits_equally=_get(table, "even_total_splits_equally", bool, path),
        odd_total_differs_by_one=_get(table, "odd_total_differs_by_one", bool, path),
        odd_total_extra_shell=extra,
    )


def _load_health(table: dict) -> HealthConfig:
    path = "health"
    _require_keys(
        table,
        {
            "possible_max_hp",
            "equal_for_both_players",
            "resets_on_new_round",
            "carries_through_reload",
        },
        path,
    )
    possible_max_hp = _int_tuple(table, "possible_max_hp", path)
    if not possible_max_hp or any(v <= 0 for v in possible_max_hp):
        raise RulesError(f"{path}.possible_max_hp: must be non-empty and all positive")
    return HealthConfig(
        possible_max_hp=possible_max_hp,
        equal_for_both_players=_get(table, "equal_for_both_players", bool, path),
        resets_on_new_round=_get(table, "resets_on_new_round", bool, path),
        carries_through_reload=_get(table, "carries_through_reload", bool, path),
    )


# Per-item schema: key name -> declared type, plus the config class to build.
# The declared type is the authority; the file is validated against it. Reading
# the type back out of the file instead would let `heal = true` under
# [items.cigarettes] land a bool in an int field, which is precisely the class
# of typo this module exists to catch. `list` means "list of strings".
_ITEM_SEMANTIC_SCHEMAS: dict[str, tuple[dict[str, type], type]] = {
    "cigarettes": (
        {"heal": int, "usable_at_max_hp": bool, "retains_turn": bool},
        CigarettesConfig,
    ),
    "handcuffs": (
        {
            "skips_opponent_turns": int,
            "stackable_on_cuffed_target": bool,
            "clears_at_end_of_round": bool,
            "retains_turn": bool,
        },
        HandcuffsConfig,
    ),
    "beer": (
        {
            "ejects_chambered_shell": bool,
            "reveals_ejected_shell": bool,
            "clears_sawed": bool,
            "retains_turn": bool,
        },
        BeerConfig,
    ),
    "handsaw": (
        {
            "damage": int,
            "consumed_on": list,
            "persists_across_turns": bool,
            "stackable": bool,
            "retains_turn": bool,
        },
        HandsawConfig,
    ),
    "magnifying_glass": ({"reveals_slot": int, "retains_turn": bool}, MagnifyingGlassConfig),
    "adrenaline": (
        {
            "steals_opponent_item": bool,
            "uses_stolen_item_immediately": bool,
            "can_steal_adrenaline": bool,
            "usable_with_nothing_to_steal": bool,
            "expiry_seconds": int,
            "retains_turn": bool,
        },
        AdrenalineConfig,
    ),
    "expired_medicine": (
        {
            "heal": int,
            "harm": int,
            "heal_probability": float,
            "can_be_lethal": bool,
            "retains_turn": bool,
        },
        ExpiredMedicineConfig,
    ),
    "burner_phone": (
        {
            "reveals_future_shell": bool,
            "excludes_slot": int,
            "player_index_decrement_from": int,
            "stacks": bool,
            "index_is_relative": bool,
            "retains_turn": bool,
        },
        BurnerPhoneConfig,
    ),
    "inverter": (
        {
            "inverts_chambered_shell": bool,
            "resolves_slot_on_use": bool,
            "retains_turn": bool,
        },
        InverterConfig,
    ),
    "remote": (
        {"reverses_turn_order": bool, "min_players": int, "retains_turn": bool},
        RemoteConfig,
    ),
}

_ITEMS_GLOBAL_KEYS = {
    "max_slots",
    "dealt_per_round_min",
    "dealt_per_round_max",
    "base_items",
    "double_or_nothing_items",
    "multiplayer_items",
}


def _load_items(table: dict) -> tuple[ItemsConfig, ItemSemantics]:
    path = "items"
    expected = _ITEMS_GLOBAL_KEYS | set(_ITEM_SEMANTIC_SCHEMAS.keys())
    _require_keys(table, expected, path)

    max_slots = _get(table, "max_slots", int, path)
    if max_slots < 1:
        raise RulesError(f"{path}.max_slots: must be >= 1, got {max_slots}")

    items_config = ItemsConfig(
        max_slots=max_slots,
        dealt_per_round_min=_get(table, "dealt_per_round_min", int, path),
        dealt_per_round_max=_get(table, "dealt_per_round_max", int, path),
        base_items=_item_tuple(table, "base_items", path),
        double_or_nothing_items=_item_tuple(table, "double_or_nothing_items", path),
        multiplayer_items=_item_tuple(table, "multiplayer_items", path),
    )

    semantics_kwargs: dict[str, object] = {}
    for config_name, (key_types, config_cls) in _ITEM_SEMANTIC_SCHEMAS.items():
        sub_path = f"{path}.{config_name}"
        sub_table = _require_keys(table[config_name], set(key_types), sub_path)
        kwargs: dict[str, object] = {}
        for key, expected_type in key_types.items():
            if expected_type is list:
                kwargs[key] = _str_tuple(sub_table, key, sub_path)
            else:
                kwargs[key] = _get(sub_table, key, expected_type, sub_path)
        semantics_kwargs[config_name] = config_cls(**kwargs)

    heal_probability = semantics_kwargs["expired_medicine"].heal_probability
    if not (0.0 <= heal_probability <= 1.0):
        raise RulesError(
            f"{path}.expired_medicine.heal_probability: must be in [0, 1], "
            f"got {heal_probability}"
        )

    return items_config, ItemSemantics(**semantics_kwargs)


def _load_dealer(table: dict) -> DealerConfig:
    path = "dealer"
    expected = {
        "knows_shell_after_own_magnifying_glass",
        "knows_shell_when_one_remains",
        "learns_from_player_item_use",
        "unknown_shell_more_live_shoots_player",
        "unknown_shell_more_blank_shoots_self",
        "unknown_shell_equal_coinflip",
        "always_uses_handcuffs",
        "handcuffs_requires_min_shells_remaining",
        "uses_cigarettes_below_max_hp",
        "medicine_requires_below_max_hp",
        "medicine_requires_hp_at_least",
        "medicine_requires_no_cigarettes_held",
        "uses_handsaw_only_when_shooting_player",
        "uses_beer_when_knows_blank_or_not_knows_live",
        "beer_requires_min_shells_remaining",
        "beer_keeps_known_blank_target",
        "uses_inverter_only_when_knows_blank",
        "adrenaline_requires_wanted_item_not_held",
        "adrenaline_wants_item_with_satisfied_condition",
        "uses_glass_when_chambered_shell_unknown",
        "glass_requires_min_shells_remaining",
        "burner_phone_min_shells_remaining",
        "item_priority",
    }
    _require_keys(table, expected, path)

    coinflip = _get(table, "unknown_shell_equal_coinflip", float, path)
    if not (0.0 <= coinflip <= 1.0):
        raise RulesError(
            f"{path}.unknown_shell_equal_coinflip: must be in [0, 1], got {coinflip}"
        )

    item_priority = _item_tuple(table, "item_priority", path)
    if len(set(item_priority)) != len(item_priority):
        raise RulesError(f"{path}.item_priority: contains duplicate items")

    for min_shells_key in (
        "handcuffs_requires_min_shells_remaining",
        "beer_requires_min_shells_remaining",
        "glass_requires_min_shells_remaining",
        "burner_phone_min_shells_remaining",
    ):
        value = _get(table, min_shells_key, int, path)
        if value < 1:
            raise RulesError(f"{path}.{min_shells_key}: must be >= 1, got {value}")

    return DealerConfig(
        knows_shell_after_own_magnifying_glass=_get(
            table, "knows_shell_after_own_magnifying_glass", bool, path
        ),
        knows_shell_when_one_remains=_get(table, "knows_shell_when_one_remains", bool, path),
        learns_from_player_item_use=_get(table, "learns_from_player_item_use", bool, path),
        unknown_shell_more_live_shoots_player=_get(
            table, "unknown_shell_more_live_shoots_player", bool, path
        ),
        unknown_shell_more_blank_shoots_self=_get(
            table, "unknown_shell_more_blank_shoots_self", bool, path
        ),
        unknown_shell_equal_coinflip=coinflip,
        always_uses_handcuffs=_get(table, "always_uses_handcuffs", bool, path),
        handcuffs_requires_min_shells_remaining=_get(
            table, "handcuffs_requires_min_shells_remaining", int, path
        ),
        uses_cigarettes_below_max_hp=_get(table, "uses_cigarettes_below_max_hp", bool, path),
        medicine_requires_below_max_hp=_get(
            table, "medicine_requires_below_max_hp", bool, path
        ),
        medicine_requires_hp_at_least=_get(table, "medicine_requires_hp_at_least", int, path),
        medicine_requires_no_cigarettes_held=_get(
            table, "medicine_requires_no_cigarettes_held", bool, path
        ),
        uses_handsaw_only_when_shooting_player=_get(
            table, "uses_handsaw_only_when_shooting_player", bool, path
        ),
        uses_beer_when_knows_blank_or_not_knows_live=_get(
            table, "uses_beer_when_knows_blank_or_not_knows_live", bool, path
        ),
        beer_requires_min_shells_remaining=_get(
            table, "beer_requires_min_shells_remaining", int, path
        ),
        beer_keeps_known_blank_target=_get(table, "beer_keeps_known_blank_target", bool, path),
        uses_inverter_only_when_knows_blank=_get(
            table, "uses_inverter_only_when_knows_blank", bool, path
        ),
        adrenaline_requires_wanted_item_not_held=_get(
            table, "adrenaline_requires_wanted_item_not_held", bool, path
        ),
        adrenaline_wants_item_with_satisfied_condition=_get(
            table, "adrenaline_wants_item_with_satisfied_condition", bool, path
        ),
        uses_glass_when_chambered_shell_unknown=_get(
            table, "uses_glass_when_chambered_shell_unknown", bool, path
        ),
        glass_requires_min_shells_remaining=_get(
            table, "glass_requires_min_shells_remaining", int, path
        ),
        burner_phone_min_shells_remaining=_get(
            table, "burner_phone_min_shells_remaining", int, path
        ),
        item_priority=item_priority,
    )


def _load_mode(table: dict) -> ModeConfig:
    path = "mode"
    _require_keys(table, {"mode"}, path)
    name = _get(table, "mode", str, path)
    try:
        mode = GameMode.from_config_name(name)
    except KeyError:
        raise RulesError(f"{path}.mode: unknown mode name {name!r}") from None
    return ModeConfig(mode=mode)


def _load_turns(table: dict) -> TurnsConfig:
    path = "turns"
    expected = {
        "player_moves_first",
        "blank_self_shot_retains_turn",
        "live_self_shot_ends_turn",
        "shooting_opponent_ends_turn",
        "base_damage",
    }
    _require_keys(table, expected, path)
    return TurnsConfig(
        player_moves_first=_get(table, "player_moves_first", bool, path),
        blank_self_shot_retains_turn=_get(table, "blank_self_shot_retains_turn", bool, path),
        live_self_shot_ends_turn=_get(table, "live_self_shot_ends_turn", bool, path),
        shooting_opponent_ends_turn=_get(table, "shooting_opponent_ends_turn", bool, path),
        base_damage=_get(table, "base_damage", int, path),
    )


def _load_round(table: dict) -> RoundConfig:
    path = "round"
    expected = {
        "campaign_rounds_to_win",
        "double_or_nothing_zero_hp_ends_run",
        "player_moves_first_after_reload",
        "cuffs_clear_on_reload",
    }
    _require_keys(table, expected, path)
    return RoundConfig(
        campaign_rounds_to_win=_get(table, "campaign_rounds_to_win", int, path),
        double_or_nothing_zero_hp_ends_run=_get(
            table, "double_or_nothing_zero_hp_ends_run", bool, path
        ),
        player_moves_first_after_reload=_get(
            table, "player_moves_first_after_reload", bool, path
        ),
        cuffs_clear_on_reload=_get(table, "cuffs_clear_on_reload", bool, path),
    )


def _load_solver(table: dict) -> SolverConfig:
    path = "solver"
    expected = {
        "max_items_per_turn",
        "reload_fixedpoint_tolerance",
        "reload_table_cache",
        "redraw_budget_ms",
        "reload_item_draw_samples",
        "reload_max_iterations",
        "reload_workers",
        "reload_aitken",
        "reload_warm_start_samples",
    }
    _require_keys(table, expected, path)
    max_items_per_turn = _get(table, "max_items_per_turn", int, path)
    if max_items_per_turn < 1:
        raise RulesError(f"{path}.max_items_per_turn: must be >= 1, got {max_items_per_turn}")
    reload_item_draw_samples = _get(table, "reload_item_draw_samples", int, path)
    if reload_item_draw_samples < 1:
        raise RulesError(
            f"{path}.reload_item_draw_samples: must be >= 1, got {reload_item_draw_samples}"
        )
    reload_max_iterations = _get(table, "reload_max_iterations", int, path)
    if reload_max_iterations < 1:
        raise RulesError(
            f"{path}.reload_max_iterations: must be >= 1, got {reload_max_iterations}"
        )
    # -1 is the "auto" sentinel, not a count: TOML has no null, and a literal
    # worker count in rules.toml would be a machine-specific constant baked
    # into a shared config (SPEC.md §0.1 (2)). 0 and 1 both mean
    # serial, per SPEC.md §7.7 item 1's contract.
    reload_workers = _get(table, "reload_workers", int, path)
    if reload_workers < -1:
        raise RulesError(
            f"{path}.reload_workers: must be >= -1 (-1 = auto), got {reload_workers}"
        )
    reload_warm_start_samples = _get(table, "reload_warm_start_samples", int, path)
    if reload_warm_start_samples < 0:
        raise RulesError(
            f"{path}.reload_warm_start_samples: must be >= 0 (0 = off), "
            f"got {reload_warm_start_samples}"
        )
    return SolverConfig(
        max_items_per_turn=max_items_per_turn,
        reload_fixedpoint_tolerance=_get(table, "reload_fixedpoint_tolerance", float, path),
        reload_table_cache=_get(table, "reload_table_cache", bool, path),
        redraw_budget_ms=_get(table, "redraw_budget_ms", int, path),
        reload_item_draw_samples=reload_item_draw_samples,
        reload_max_iterations=reload_max_iterations,
        reload_workers=reload_workers,
        reload_aitken=_get(table, "reload_aitken", bool, path),
        reload_warm_start_samples=reload_warm_start_samples,
    )


def _cross_validate(
    shells: ShellsConfig,
    items: ItemsConfig,
    item_semantics: ItemSemantics,
    dealer: DealerConfig,
) -> None:
    priority_set = set(dealer.item_priority)
    for condition_key, item_name in _ITEMS_WITH_DEALER_CONDITIONS.items():
        item = Item.from_config_name(item_name)
        if item not in priority_set:
            raise RulesError(
                f"dealer.item_priority: missing {item_name!r}, required because "
                f"dealer.{condition_key} defines a usage condition for it"
            )

    # A load that cannot satisfy its own minimums is unusable: the smallest
    # legal total must have room for the required live and blank (SPEC.md §3.1
    # guarantees at least one of each).
    if shells.min_live + shells.min_blank > shells.min_total:
        raise RulesError(
            f"shells: min_live + min_blank ({shells.min_live} + {shells.min_blank}) "
            f"exceeds min_total ({shells.min_total})"
        )

    if items.dealt_per_round_min > items.dealt_per_round_max:
        raise RulesError(
            f"items: dealt_per_round_min ({items.dealt_per_round_min}) > "
            f"dealt_per_round_max ({items.dealt_per_round_max})"
        )
    # Dealing more than the slot limit could accept is a contradiction, not a
    # cap to silently apply (SPEC.md §3.1: at 8, no further items are accepted).
    if items.dealt_per_round_max > items.max_slots:
        raise RulesError(
            f"items: dealt_per_round_max ({items.dealt_per_round_max}) exceeds "
            f"max_slots ({items.max_slots})"
        )

    # A non-positive saw damage would make the saw a no-op or a heal. Whether
    # it exceeds turns.base_damage is checked at the call site, where both are
    # in scope; here we only reject values that are nonsense on their own.
    if item_semantics.handsaw.damage <= 0:
        raise RulesError(
            f"items.handsaw.damage: must be positive, got {item_semantics.handsaw.damage}"
        )

    # base_items / double_or_nothing_items / multiplayer_items are already
    # validated as known Item names by _item_tuple (unknown names raise there).


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def default_rules() -> Rules:
    """Cached `load_rules()` at the default path.

    Convenience only — every consumer should take a `Rules` object as an
    argument rather than reaching for this global.
    """
    return load_rules()
