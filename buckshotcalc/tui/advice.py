"""The advice log: one JSON line per player decision (decisions-pending item 16).

Pairs what the calc recommended with what the operator did, so
`scripts/advice_report.py` can check calibration against the real game and fit a
safety preference (item 15(b)). Stdlib only. Nothing here solves: it records the
result the app already holds, and an I/O failure is logged and never raised, so a
full disk cannot take the TUI down.

Row schema (`v` = 1), one per line:

- ``decision``: ``t`` (unix seconds), ``game`` (bridge log file name), ``seq``,
  ``rules_fp``, ``engine_fp``, ``mode``, ``state`` (`memo_key.encode`), ``summary``,
  ``children`` [{``action``, ``value``, ``wasted``, ``dies``}], ``rec`` and ``rec_value``,
  ``taken``, ``taken_value`` (null when not among the children), ``taken_approx``,
  ``no_rec`` (a reason string, or null).
- ``load``: ``kind``, ``live``, ``blank``, ``hp``, ``max_hp``.
- ``round_end``: ``winner`` ("player" or "dealer").
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from buckshotcalc.engine import memo_key
from buckshotcalc.engine.actions import Action
from buckshotcalc.engine.rules import _DEFAULT_RULES_PATH, Rules, rules_fingerprint
from buckshotcalc.engine.solver import SearchResult, _engine_fingerprint
from buckshotcalc.engine.state import GameState, Item, Player, Shell
from buckshotcalc.tui import analysis

log = logging.getLogger(__name__)
SCHEMA_V = 1


def advice_path_for(record: Path) -> Path:
    """`temp/games/X.jsonl` -> `temp/advice/X.jsonl` (the same stamp, the sibling folder)."""
    return record.parent.parent / "advice" / record.name


def summarize(state: GameState) -> dict:
    """A readable view of the state for humans reading the log."""
    ch = state.chamber
    live = ch.unknown_live + sum(1 for s in ch.slots if s == Shell.LIVE)
    blank = ch.unknown_blank + sum(1 for s in ch.slots if s == Shell.BLANK)
    return {
        "live": live,
        "blank": blank,
        "p_live": ch.p_live() if ch.slots else None,
        "slots": ["L" if s == Shell.LIVE else "B" if s == Shell.BLANK else "?" for s in ch.slots],
        "hp": list(state.hp),
        "max_hp": state.max_hp,
        "items": [{i.config_name: n for i, n in c.held()} for c in state.items],
        "sawed": state.sawed,
        "cuffed": list(state.cuffed),
        "items_used_this_turn": state.items_used_this_turn,
    }


def _label(action: Action) -> str:
    return analysis.action_label(action, Player.PLAYER)  # the operator's seat is fixed (v1)


def decision_row(
    rules: Rules,
    state: GameState,
    result: SearchResult | None,
    taken: Action,
    no_rec: str | None,
) -> dict:
    """The decision payload (without the envelope fields `AdviceLog` adds)."""
    children, rec, rec_value, taken_value, approx = [], None, None, None, False
    if no_rec is None and result is not None:
        for c in result.children:
            children.append({
                "action": _label(c.action),
                "value": c.value,
                "wasted": c.wasted,
                "dies": analysis.death_chance(state, c.action, rules),
            })
            if taken_value is None and c.action == taken:
                taken_value = c.value
        if result.children:
            rec, rec_value = _label(result.children[0].action), result.children[0].value
        if taken_value is None and taken.item is Item.ADRENALINE:
            # which item it steals is a later event: score it as the best Adrenaline line
            adr = [c.value for c in result.children if c.action.item is Item.ADRENALINE]
            if adr:
                taken_value, approx = max(adr), True
    return {
        "state": memo_key.encode(state, rules),
        "summary": summarize(state),
        "children": children,
        "rec": rec,
        "rec_value": rec_value,
        "taken": _label(taken),
        "taken_value": taken_value,
        "taken_approx": approx,
        "no_rec": no_rec,
    }


class AdviceLog:
    """Appends rows to one JSONL file; `path=None` makes every call a no-op."""

    def __init__(self, path: Path | None, rules: Rules, mode: str, game: str = "") -> None:
        self._path, self._rules, self._mode, self._game = path, rules, mode, game
        self._fh = None
        self._fps: tuple[str, str] | None = None

    def decision(
        self,
        seq: object,
        state: GameState,
        result: SearchResult | None,
        taken: Action,
        no_rec: str | None,
    ) -> None:
        if self._path is None:
            return
        try:
            row = decision_row(self._rules, state, result, taken, no_rec)
        except Exception:  # noqa: BLE001 - the log must never take the TUI down
            log.exception("advice log: could not build a decision row")
            return
        self._write({"ev": "decision", "seq": seq, **self._envelope(), **row})

    def load(self, event: dict) -> None:
        self._write({
            "ev": "load", "seq": event.get("seq"), "kind": event.get("kind"),
            "live": event.get("live"), "blank": event.get("blank"),
            "hp": event.get("hp"), "max_hp": event.get("max_hp"),
        })

    def round_end(self, event: dict) -> None:
        down = event.get("who")  # the bridge reports who went down
        winner = {"player": "dealer", "dealer": "player"}.get(str(down))
        self._write({"ev": "round_end", "seq": event.get("seq"), "winner": winner})

    def _envelope(self) -> dict:
        if self._fps is None:
            self._fps = (rules_fingerprint(_DEFAULT_RULES_PATH), _engine_fingerprint())
        return {"rules_fp": self._fps[0], "engine_fp": self._fps[1], "mode": self._mode}

    def _write(self, row: dict) -> None:
        if self._path is None:
            return
        row = {"v": SCHEMA_V, "t": time.time(), "game": self._game or self._path.name, **row}
        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._fh.flush()
        except OSError as exc:
            log.warning("advice log: cannot write %s (%s)", self._path, exc)
            self._fh = None
