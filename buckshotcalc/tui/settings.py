"""User-tunable TUI look and motion, persisted as a small TOML file.

Stdlib only (tomllib to read, a tiny writer below). Anything unknown or
invalid in the file falls back to the default for that field.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

PALETTES = ("amber", "green", "red", "custom")
BACKGROUNDS = ("transparent", "solid")
NUMBER_ANIMS = ("N1", "N2", "N3", "N4", "N5")
MOTIONS = ("full", "reduced", "off")
_BOOLS = ("d1", "d2", "d4", "d5", "pulse")
REVEAL_SCALES = (0.0, 0.5, 1.0, 1.5)  # timing.REVEAL_SCALE; 0 shows events at once


@dataclass(frozen=True, slots=True)
class Settings:
    palette: str = "amber"
    #: (token, "#rrggbb") pairs used when palette == "custom"; hashable.
    custom: tuple[tuple[str, str], ...] = ()
    background: str = "transparent"
    number_anim: str = "N3"
    d1: bool = True  # colour shift
    d2: bool = True  # cracked frame
    d4: bool = True  # heartbeat pips + CRITICAL
    d5: bool = True  # header static
    motion: str = "full"
    pulse: bool = True
    reveal_scale: float = 1.0


_CHOICES = {
    "palette": PALETTES,
    "background": BACKGROUNDS,
    "number_anim": NUMBER_ANIMS,
    "motion": MOTIONS,
}


def _is_hex(v: object) -> bool:
    return (
        isinstance(v, str) and len(v) == 7 and v[0] == "#"
        and all(c in "0123456789abcdefABCDEF" for c in v[1:])
    )


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "buckshotcalc" / "tui.toml"


def from_mapping(data: dict) -> Settings:
    kw: dict = {}
    for name, choices in _CHOICES.items():
        if data.get(name) in choices:
            kw[name] = data[name]
    for name in _BOOLS:
        if isinstance(data.get(name), bool):
            kw[name] = data[name]
    rs = data.get("reveal_scale")
    if isinstance(rs, (int, float)) and not isinstance(rs, bool) and rs in REVEAL_SCALES:
        kw["reveal_scale"] = float(rs)
    cust = data.get("custom")
    if isinstance(cust, dict):
        kw["custom"] = tuple(
            sorted((k, v) for k, v in cust.items() if isinstance(k, str) and _is_hex(v))
        )
    return Settings(**kw)


def load(path: Path | str | None = None) -> Settings:
    try:
        with open(path or default_path(), "rb") as f:
            return from_mapping(tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError):
        return Settings()


def dumps(s: Settings) -> str:
    out = []
    for f in fields(s):
        v = getattr(s, f.name)
        if f.name == "custom":
            continue
        if isinstance(v, bool | float):
            out.append(f"{f.name} = {str(v).lower() if isinstance(v, bool) else v}")
        else:
            out.append(f'{f.name} = "{v}"')
    if s.custom:
        out.append("\n[custom]")
        out += [f'{json.dumps(k)} = "{v}"' for k, v in s.custom]
    return "\n".join(out) + "\n"


def save(s: Settings, path: Path | str | None = None) -> None:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(s), encoding="utf-8")
