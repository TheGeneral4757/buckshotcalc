"""Colour palette and shared display glyphs for the dashboard (SPEC.md §8).

Textual/rich only (SPEC.md §0.1 (3) reserves this layer for
third-party UI deps). Every colour below is named for the meaning SPEC.md §8
assigns it so `panels.py` never repeats a raw colour string, and every one is
paired with a text label somewhere in `panels.py` — colour is never the sole
carrier of meaning (§8).
"""

from __future__ import annotations

# -- SPEC.md §8's colour table ------------------------------------------------

LIVE = "red"  # live shells, danger, lethal threat to the operator
BLANK = "cyan"  # blanks, safety
RECOMMENDED = "green"  # the recommended action
ALTERNATIVE = "grey62"  # ranked alternatives (dim grey)
WARNING = "yellow"  # warnings, uncertainty, search-cap reached

#: Panel borders. Not one of §8's five meanings — it carries no meaning at
#: all, which is the point: the redesign leans on borders and spacing for
#: separation so the five colours stay meaningful. Never a background: the
#: operator runs a translucent terminal, so nothing here may set one.
BORDER = "grey42"

# -- glyphs (SPEC.md §8's chamber mock) --------------------------------------

LIVE_SLOT_GLYPH = "●"  # ● resolved live
BLANK_SLOT_GLYPH = "○"  # ○ resolved blank
UNKNOWN_SLOT_GLYPH = "?"  # unresolved

WASTED_MARK = "✗"  # ✗ strictly-wasted action marker

BAR_FILLED = "█"  # █
BAR_EMPTY = "░"  # ░

HP_FULL = "♥"  # one point of health remaining
HP_LOST = "♡"  # one point of health already lost

ITEM_HELD = "●"  # one copy of an item held
ITEM_NONE = "·"  # an empty item slot

SAWED_FLAG = "SAWED OFF"
CUFFED_FLAG = "CUFFED"


# -- configurable palettes (settings.py) -------------------------------------

from dataclasses import dataclass, replace  # noqa: E402

from rich.style import Style  # noqa: E402

from buckshotcalc.tui.settings import Settings  # noqa: E402

TOKENS = ("fg", "dim", "acc", "live", "blank", "good", "bad", "warn", "frame", "bg_solid")


@dataclass(frozen=True, slots=True)
class Palette:
    fg: str
    dim: str
    acc: str
    live: str
    blank: str
    good: str
    bad: str
    warn: str
    frame: str
    bg_solid: str


AMBER = Palette("#ffb347", "#8a5a1c", "#ffd98a", "#ff6a3d", "#caa46c", "#ffe9a3",
                "#ff6a3d", "#ffcf5a", "#b8742a", "#0d0905")
GREEN = Palette("#7dff8a", "#2f7a38", "#d2ffd6", "#eaffec", "#4fae5a", "#b9ffbf",
                "#eaffec", "#b8ff6a", "#2f7a38", "#050b06")
# Live is the brightest, filled glyph; blank is a dull rose drawn hollow.
RED = Palette("#ff5a5f", "#7a2a30", "#ffb3b8", "#ffe6e8", "#b0505a", "#ffc2c7",
              "#ff2d3a", "#ffa07a", "#a8323c", "#0a0405")
PRESETS = {"amber": AMBER, "green": GREEN, "red": RED}


def palette_for(s: Settings) -> Palette:
    if s.palette == "custom":
        return replace(AMBER, **{k: v for k, v in s.custom if k in TOKENS})
    return PRESETS.get(s.palette, AMBER)


def style(s: Settings, token: str, *, bold: bool = False) -> Style:
    """Rich style for a palette token; a background only in 'solid' mode."""
    p = palette_for(s)
    bg = p.bg_solid if s.background == "solid" else None
    return Style(color=getattr(p, token), bgcolor=bg, bold=bold)
