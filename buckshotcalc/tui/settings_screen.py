"""ctrl+o: look and motion settings, previewed live on the watch view behind it.

up/down pick a row, left/right change it, esc closes (the app saves on close).
The panel is drawn with the same cell grid and palette as the watch view and
paints no background of its own.
"""

from __future__ import annotations

from dataclasses import replace

from textual.screen import ModalScreen
from textual.widgets import Static

from buckshotcalc.tui import settings as S
from buckshotcalc.tui.watch import L, Ln, frame, to_text

#: (field, label, choices); booleans are on/off.
ROWS: tuple[tuple[str, str, tuple], ...] = (
    ("palette", "palette", S.PALETTES),
    ("background", "background", S.BACKGROUNDS),
    ("number_anim", "number animation", S.NUMBER_ANIMS),
    ("d1", "low health: colour shift", (True, False)),
    ("d2", "low health: cracked frame", (True, False)),
    ("d4", "low health: heartbeat", (True, False)),
    ("d5", "1 health: static", (True, False)),
    ("motion", "motion", S.MOTIONS),
    ("pulse", "best move pulse", (True, False)),
    ("reveal_scale", "reveal timing", S.REVEAL_SCALES),
)
_W = 58


def step(s: S.Settings, field: str, d: int) -> S.Settings:
    """`s` with `field` moved `d` choices along (wrapping)."""
    choices = next(c for f, _, c in ROWS if f == field)
    cur = getattr(s, field)
    return replace(s, **{field: choices[(choices.index(cur) + d) % len(choices)]})


def _show(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return {0.0: "off (at once)", 0.5: "half", 1.0: "normal", 1.5: "slow"}[v]
    return str(v)


def settings_lines(s: S.Settings, cursor: int) -> list[Ln]:
    inner = [Ln()]
    for i, (field, label, _) in enumerate(ROWS):
        on = i == cursor
        ln = Ln().t("▸ " if on else "  ", "acc").t(label.ljust(26), "acc" if on else "fg")
        ln.t("◂ " if on else "  ", "dim")
        ln.t(_show(getattr(s, field)).ljust(14), "acc+" if on else "fg")
        ln.t("▸" if on else " ", "dim")
        inner.append(ln)
    inner += [Ln(), L("up/down pick · left/right change · esc save and close", "dim")]
    return frame(inner, _W, L("settings", "dim"), "fr")


class SettingsScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: transparent; }
    #settings-box { width: 58; height: auto; background: transparent; }
    """

    def __init__(self, current: S.Settings, on_change, on_close) -> None:
        super().__init__()
        self.current, self.cursor = current, 0
        self._on_change, self._on_close = on_change, on_close

    def compose(self):
        yield Static(id="settings-box")

    def on_mount(self) -> None:
        self._draw()

    def _draw(self) -> None:
        self.query_one("#settings-box", Static).update(
            to_text(settings_lines(self.current, self.cursor), self.current)
        )

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()
        key = event.key
        if key == "escape":
            self._on_close(self.current)
            self.dismiss()
            return
        if key in ("up", "down"):
            self.cursor = (self.cursor + (1 if key == "down" else -1)) % len(ROWS)
        elif key in ("left", "right"):
            self.current = step(self.current, ROWS[self.cursor][0], 1 if key == "right" else -1)
            self._on_change(self.current)
        self._draw()
