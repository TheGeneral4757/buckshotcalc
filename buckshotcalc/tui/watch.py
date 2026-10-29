"""Watch-mode screen (layout L2 of the operator-approved showcase mock).

Hero frame on the left (58 columns), moves / history / plan / racks on the right (40),
shell strip and counts bar along the bottom, an event line under that. Every
function here is a transcription of a mock function (`heroLines`, `altBars`,
`frame`, `shellRows`, `countsBar`, ...), kept close enough that the two can be
compared side by side.

The mock is characters plus foreground colours only, so this module draws on a
grid of `(char, role)` cells and turns it into one Rich `Text` at the end. A role
is a palette token (`fg dim acc live blank good bad warn fr`), with a trailing
`+` for bold. A background is painted only when `settings.background == "solid"`
(via `theme.style`).

Nothing here solves or animates: `WatchFrames` carries whatever the animation
driver (`watchdriver.py`) computed for this instant, and the defaults are the
settled end frame, so `render_watch(vm, settings)` alone draws a still screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.align import Align
from rich.console import RenderableType
from rich.style import Style
from rich.text import Text

from buckshotcalc.engine.actions import Action, ActionKind
from buckshotcalc.engine.state import Item, Player, Shell
from buckshotcalc.tui import analysis, anim, theme
from buckshotcalc.tui.settings import Settings

W = 100  # the whole view
NARROW_W = 80  # below W the side column stacks under the hero and the view is this wide
HERO_W = 58
SIDE_W = 40
SIDE_H = 21  # fixed top block: other moves 5 + this round 3 + plan 7 + gap 1 + racks 4 + gaps
HERO_H = 12  # the narrow layout's fixed hero block (the recap card, 15, grows it once at round end)
BIG_EXTRA = 5  # rows the 2x win-% numeral adds to the hero
_SPARK = "▁▂▃▄▅▆▇█"

ITEM_TAG: dict[Item, tuple[str, str]] = {
    Item.CIGARETTES: ("≋", "cigs"),
    Item.HANDCUFFS: ("∞", "cuffs"),
    Item.BEER: ("⊔", "beer"),
    Item.HANDSAW: ("⌁", "saw"),
    Item.MAGNIFYING_GLASS: ("⌕", "glass"),
    Item.ADRENALINE: ("↯", "adren"),
    Item.EXPIRED_MEDICINE: ("✚", "meds"),
    Item.BURNER_PHONE: ("☏", "phone"),
    Item.INVERTER: ("⇅", "invert"),
    Item.REMOTE: ("⇄", "remote"),
}


def item_tag(item: Item) -> str:
    """I3: glyph plus short name."""
    g, n = ITEM_TAG[item]
    return f"{g} {n}"


# -- animation inputs ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThinkFrame:
    """P2 progress block, already formatted."""

    label: str  # "checking: shoot dealer"
    segments: tuple[tuple[str, str], ...]  # (text, role) runs of the segmented bar
    stats: str  # "move 3/7 · 140k positions · 1.2s"


@dataclass(frozen=True, slots=True)
class RackFx:
    """A used or stolen item still visible in a rack: `level` 4..1 fades, `offset` slides in."""

    seat: int
    item: Item
    level: int
    offset: int = 0


@dataclass(frozen=True, slots=True)
class WatchFrames:
    """What is animating right now. The defaults are the settled picture."""

    num: anim.NumFrame | None = None  # N1-N5 frame; None = the plain number
    delta: tuple[str, str, int] | None = None  # X1 (text, role, level 3..1)
    shells: anim.ShellFrame | None = None  # H4
    event_text: str | None = None  # typed prefix of the newest event; None = all of it
    lost_on: tuple[bool, bool] = (False, False)  # per seat: flash the pip just lost
    gain_level: tuple[int, int] = (0, 0)  # per seat: fade-in level of the pip just gained
    rack_fx: tuple[RackFx, ...] = ()
    wipe: int | None = None  # turn handoff: columns of the hero already drawn
    stamp_on: bool = True  # SAWED / CUFFED blink
    pulse_bright: bool = True  # X2
    beat_bright: bool = True  # D4 heartbeat
    static_t: float | None = None  # D5 clock (quantised); None = off
    think: ThinkFrame | None = None  # P2
    history: tuple[float, ...] = ()  # win % per released change this round


# -- cell grid -----------------------------------------------------------------


class Ln:
    """One screen row as `(char, role)` cells."""

    __slots__ = ("c",)

    def __init__(self, cells: list[tuple[str, str]] | None = None) -> None:
        self.c: list[tuple[str, str]] = list(cells or [])

    @property
    def n(self) -> int:
        return len(self.c)

    def t(self, s: str, role: str = "fg") -> Ln:
        self.c.extend((ch, role) for ch in s)
        return self

    def sp(self, k: int) -> Ln:
        # role "sp" is styled like "fg" but kept as its own run: SVG exports drop the offset
        # of a run that starts with spaces, so padding must not merge into the text after it
        return self.t(" " * k, "sp") if k > 0 else self

    def pad(self, w: int) -> Ln:
        return self.sp(w - self.n)

    def lead(self, k: int) -> Ln:
        if k > 0:
            self.c[:0] = [(" ", "sp")] * k
        return self

    def ctr(self, w: int) -> Ln:
        self.lead((w - self.n) // 2)
        return self.pad(w)

    def clip(self, w: int) -> Ln:
        del self.c[w:]
        return self


def L(s: str, role: str = "fg") -> Ln:
    return Ln().t(s, role)


def hcat(blocks: list[tuple[int, list[Ln]]], gap: int = 2) -> list[Ln]:
    rows = max(len(b) for _, b in blocks)
    out = []
    for r in range(rows):
        line = Ln()
        for i, (w, b) in enumerate(blocks):
            src = b[r] if r < len(b) else Ln()
            line.c.extend(src.c[:w])
            line.pad(sum(x[0] for x in blocks[: i + 1]) + gap * i)
            if i < len(blocks) - 1:
                line.sp(gap)
        out.append(line)
    return out


def frame(
    inner: list[Ln], w: int, title: Ln | None, role: str, hp: int | None = None,
    st: Settings | None = None, static_t: float | None = None,
) -> list[Ln]:
    """S1 frame characters. `hp` cracks the border (D2), `static_t` corrupts the top (D5)."""
    tl, tr, bl, br, h, v = "┌┐└┘─│"
    top = Ln().t(tl + h, role)
    if title is not None:
        top.t(" ", "dim")
        top.c.extend(title.c)
        top.t(" ", "dim")
    top.t(h * max(0, w - 1 - top.n), role).t(tr, role)
    out = [top]
    for ln in inner:
        row = Ln().t(v + " ", role)
        row.c.extend(ln.clip(w - 3).c)
        row.pad(w - 1).t(v, role)
        out.append(row)
    out.append(Ln().t(bl + h * (w - 2) + br, role))
    mode = st.motion if st else "full"
    if hp is not None and st is not None and st.d2:
        _edit_border(out, role, lambda ss: anim.crack(ss, hp))
    if static_t is not None and st is not None and st.d5:
        _edit_border(out[:1], role, lambda ss: [anim.static(ss[0], static_t, 0, mode)])
    return out


def _edit_border(lines: list[Ln], role: str, fn) -> None:
    """Run a string-level effect over the border cells only; other cells are left alone."""
    strs = ["".join(ch if r == role else "\0" for ch, r in ln.c) for ln in lines]
    for ln, old, new in zip(lines, strs, fn(strs), strict=True):
        if old != new:
            ln.c = [(new[i], r) if r == role and new[i] != "\0" else (ch, r)
                    for i, (ch, r) in enumerate(ln.c)]


def overlay(base: list[Ln], box: list[Ln], row: int, col: int) -> None:
    for i, b in enumerate(box):
        if 0 <= row + i < len(base):
            line = base[row + i]
            line.pad(col + b.n)
            line.c[col : col + b.n] = b.c


def to_text(lines: list[Ln], st: Settings) -> Text:
    """The grid as one Rich Text; same-role runs merged."""
    cache: dict[str, Style] = {}

    def style_of(role: str) -> Style:
        s = cache.get(role)
        if s is None:
            bold = role.endswith("+")
            base = role.rstrip("+")
            token = {"fr": "frame", "sp": "fg", "dfr": "dim"}.get(base, base)
            s = cache[role] = theme.style(st, token, bold=bold)
        return s

    out = Text(no_wrap=True, overflow="crop")
    for i, ln in enumerate(lines):
        if i:
            out.append("\n")
        run, cur = [], None
        for ch, role in ln.c:
            if role != cur and run:
                out.append("".join(run), style_of(cur))
                run = []
            cur = role
            run.append(ch)
        if run:
            out.append("".join(run), style_of(cur))
    return out


def plain_text(lines: list[Ln]) -> str:
    return "\n".join("".join(ch for ch, _ in ln.c).rstrip() for ln in lines)


# -- small pieces --------------------------------------------------------------


def spark(vals) -> str:
    return "".join(_SPARK[max(0, min(7, int(v / 100 * 8)))] for v in vals)


def short_action(action: Action) -> str:
    """Alternatives column label: icon plus short name for items."""
    if action.kind is ActionKind.SHOOT_OPPONENT:
        return "shoot dealer"
    if action.kind is ActionKind.SHOOT_SELF:
        return "shoot yourself"
    assert action.item is not None
    tag = item_tag(action.item)
    if action.item is Item.ADRENALINE and action.steal is not None:
        return f"{tag} > {ITEM_TAG[action.steal][1]}"
    return tag


def short_label(label: str) -> str:
    """Plan / recap label (an `analysis.action_label` string) in plain words."""
    if label in ("SHOOT DEALER", "SHOOT PLAYER", "SHOOT OPPONENT"):
        return "shoot dealer" if label != "SHOOT PLAYER" else "shoot you"
    if label == "SHOOT SELF":
        return "shoot yourself"
    if label.startswith("USE "):
        try:
            return ITEM_TAG[Item[label[4:]]][1]
        except KeyError:
            return label[4:].lower()
    if label.startswith("ADRENALINE ->"):
        try:
            return "adren > " + ITEM_TAG[Item[label[14:]]][1]
        except KeyError:
            pass
    return label.lower()


def _live_role(word: str) -> str:
    w = word.lower()
    if "blank" in w:
        return "blank"
    if "live" in w:
        return "live"
    return "fg"


def _alt_bars(vm, w: int = SIDE_W) -> list[Ln]:
    res = vm.result
    if res is None or not res.children or _withheld(vm):
        return []
    best = res.children[0].value * 100
    out = []
    for i, c in enumerate(res.children[:4]):
        v = c.value * 100
        top = i == 0
        dies = analysis.dies_for(vm.state, c.action, vm.rules, vm.seat)
        ln = Ln().t(("▸ " if top else "  ") + short_action(c.action).ljust(14)[:14] + " ",
                    "acc" if top else "fg")
        # the bar gives way to the death chance: the row has no room for both at 40 columns
        ln.t(analysis.dies_text(dies).ljust(8) if dies > 0 else anim.eighth_bar(c.value, 8),
             "acc" if top else "dim")
        ln.t(" " + f"{v:.1f}".rjust(5), "acc" if top else "fg")
        if not top:
            gap = v - best
            ln.t("  " + ("=" if abs(gap) < 0.05 else f"{gap:.1f}").rjust(5), "dim")
        out.append(ln.clip(w))
    return out


def _withheld(vm) -> bool:
    return vm.degraded is not None or vm.bridge_paused is not None


def _win(vm) -> float | None:
    if vm.result is None or _withheld(vm):
        return None
    return vm.result.value * 100


# -- plan tree (A2) ------------------------------------------------------------


def _chain(node) -> tuple[list[str], object]:
    parts = [short_label(node.action)]
    while len(node.children) == 1 and node.children[0].label == "":
        node = node.children[0]
        parts.append(short_label(node.action) if node.action else node.end)
        if node.action is None:
            break
    return parts, node


def _chain_ln(parts: list[str], ended: bool) -> Ln:
    ln = Ln()
    for i, p in enumerate(parts):
        if i:
            ln.t(" → ", "dim")
        last = i == len(parts) - 1
        ln.t(p, ("dim" if ended else "acc") if last else "fg")
    return ln


def plan_lines(plan, limit: int = 7) -> list[Ln]:
    if plan is None or plan.action is None:
        return []
    out = [L("plan", "dim")]

    def emit(node, indent: str, row: Ln | None) -> None:
        parts, last = _chain(node)
        row = row if row is not None else Ln()
        row.c.extend(_chain_ln(parts, last.action is None).c)
        out.append(row)
        # a branch cut off at the depth limit ("...") is not a step, so it gets no row
        kids = [c for c in last.children if c.action is not None or c.end != "..."]
        words = [("then" if len(kids) == 1 else "either way") if "/" in c.label else c.label
                 for c in kids]
        for i, c in enumerate(kids):
            if len(out) >= limit:
                return
            conn = "└" if i == len(kids) - 1 else "├"
            word = words[i]
            lab = Ln().t(indent + conn + " ", "dim").t(
                word.ljust(max(map(len, words))) + " ", _live_role(word))
            if c.action is None:
                out.append(lab.t(c.end or "", "dim"))
            else:
                emit(c, indent + ("  " if conn == "└" else "│ "), lab)

    emit(plan, "", None)
    return [ln.clip(SIDE_W) for ln in out[:limit]]


# -- racks ---------------------------------------------------------------------


def _pips(hp: int, max_hp: int, st: Settings, fr: WatchFrames, seat: int) -> Ln:
    ln = Ln()
    lost_on, gain = fr.lost_on[seat], fr.gain_level[seat]
    beat = fr.beat_bright or not (st.d4 and seat == 0 and hp <= 2)
    for i in range(max_hp):
        if i < hp:
            role = "bad" if beat else "dim"
            if gain and i == hp - 1:
                role = ("dim", "fg", "bad")[min(gain, 3) - 1]
            ln.t("♥", role)
        elif lost_on and i == hp:
            ln.t("♥", "bad+")
        else:
            ln.t("·", "dim")
    return ln


def _rack(vm, st, fr, seat: Player, label: str) -> list[Ln]:
    state = vm.state
    hp = state.hp[seat.value]
    head = Ln().t(label + " ", "dim")
    head.c.extend(_pips(hp, state.max_hp, st, fr, seat.value).c)
    tokens: list[Ln] = []
    for item, n in state.items_of(seat).held():
        tokens.append(L(item_tag(item) + (f"×{n}" if n > 1 else ""), "fg"))
    for fx in fr.rack_fx:
        if fx.seat == seat.value and (fx.level or fx.offset):
            role = "acc+" if fx.level >= 4 else "fg" if fx.level >= 3 else "dim"
            tokens.append(L(" " * fx.offset + item_tag(fx.item), role))
    if not tokens:
        tokens = [L("-", "dim")]
    lines = [head]
    cur = head
    for tok in tokens:
        sep = 2
        if cur.n + sep + tok.n > SIDE_W:
            cur = Ln().sp(4)
            lines.append(cur)
            sep = 0
        cur.sp(sep)
        cur.c.extend(tok.c)
    return [ln.clip(SIDE_W) for ln in lines]


def _right(vm, st, fr) -> list[Ln]:
    out: list[Ln] = []
    if vm.state.winner() is None:  # the recap card already says what a finished round was
        alts = _alt_bars(vm)
        out.append(L("other moves" if alts or vm.state.turn is vm.seat else "dealer's turn", "dim"))
        if alts:
            out.extend(alts)
        else:
            dealer = vm.state.turn is not vm.seat
            out.append(L("waiting on the dealer" if dealer else "nothing to choose yet", "dim"))
        out.append(Ln())
        out.append(L("this round", "dim"))
        h = fr.history
        if h:
            out.append(Ln().t(spark(h[-16:]), "good").t(f"  {h[0]:.0f}% → {h[-1]:.0f}%", "fg"))
        else:
            out.append(L("-", "dim"))
        plan = plan_lines(vm.plan) if vm.state.turn is vm.seat and not _withheld(vm) else []
        if plan:
            out.append(Ln())
            out.extend(plan)
        out.append(Ln())
    out.extend(_rack(vm, st, fr, vm.seat, "you"))
    out.extend(_rack(vm, st, fr, vm.seat.other, "dlr"))
    return out


# -- hero ----------------------------------------------------------------------


def _why(vm) -> str:
    ch = vm.state.chamber
    if not ch.slots:
        return "the gun is empty - waiting for the next load"
    if ch.slots[0] is Shell.LIVE:
        return "why: the shell in the gun is live"
    if ch.slots[0] is Shell.BLANK:
        return "why: the shell in the gun is blank"
    return (f"why: {ch.unknown_live} live, {ch.unknown_blank} blank unseen - "
            f"next shell live {ch.p_live() * 100:.0f}%")


def _caption(vm) -> str:
    if vm.bridge_paused is not None:
        return "paused - see the box"
    if vm.degraded is not None:
        return "shell count uncertain - no number"
    if vm.reload_table_missing:
        return "no answer yet: reload table not built"
    if vm.result is None:
        return "working it out..."
    return "chance you win this round"


def _stamps(vm) -> list[str]:
    s = vm.state
    out = []
    if s.sawed:
        out.append("SAWED")
    if s.cuffed[vm.seat.other.value]:
        out.append("DEALER CUFFED")
    if s.cuffed[vm.seat.value]:
        out.append("YOU CUFFED")
    return out


def _tint_role(vm, st: Settings, fr: WatchFrames) -> str | None:
    """D1 + D4: the role the dim / frame cells of the whole view shift to, or None.

    warn at 2 HP, bad at 1 or less. With the heartbeat on, the dark half of the beat drops
    the tint, so the view pulses; the pips and the CRITICAL word carry it without colour."""
    hp = vm.state.hp[vm.seat.value]
    if not st.d1 or hp > 2 or (st.d4 and not fr.beat_bright):
        return None
    return "bad" if hp <= 1 else "warn"


def _tint(lines: list[Ln], role: str) -> None:
    for ln in lines:
        ln.c = [(ch, role if r in ("dim", "fr", "dfr") else r) for ch, r in ln.c]


def _double(rows: list[tuple[str, str]], iw: int) -> list[tuple[str, str]]:
    """2x numeral: every cell twice across and every row twice down (falls back if too wide)."""
    wide = [("".join(c * 2 for c in r), role) for r, role in rows]
    if any(len(r) > iw for r, _ in wide):
        return rows
    return [x for x in wide for _ in (0, 1)]


def _hero(vm, st: Settings, fr: WatchFrames, big: bool = False) -> list[Ln]:
    w = HERO_W
    iw = w - 4
    state = vm.state
    hp = state.hp[vm.seat.value]
    dealer_turn = state.turn is not vm.seat
    win = _win(vm)
    thinking = fr.think is not None
    withheld = _withheld(vm)

    if fr.num is not None:
        rows = anim.number_rows(fr.num)
    else:
        text = anim.fmt_pct(win) if win is not None else "?"
        role = "dim" if thinking else "fg" if dealer_turn else "acc"
        rows = [(r, role) for r in anim.big(text)]
    tint = _tint_role(vm, st, fr)
    if tint is not None:  # D1; a stale (thinking) number stays dim
        rows = [(r, tint if role in ("acc", "fg") else role) for r, role in rows]
    if big:
        rows = _double(rows, iw)

    inner = [Ln().ctr(iw)]
    for r, role in rows:
        inner.append(L(r, role).ctr(iw))
    sub = L(_caption(vm), "dim")
    if fr.delta is not None and win is not None:
        text, role, level = fr.delta
        sub.t("   ").t(text, role + "+" if level >= 3 else role if level == 2 else "dim")
    inner.append(sub.ctr(iw))

    if thinking:
        t = fr.think
        inner.append(Ln().t("thinking ", "warn").t(t.label, "dim").ctr(iw))
        bar = Ln()
        for txt, role in t.segments:
            bar.t(txt, role)
        inner.append(bar.ctr(iw))
        inner.append(L(t.stats, "dim").ctr(iw))
    elif dealer_turn and not withheld:
        inner.append(Ln().ctr(iw))
        inner.append(L("dealer's turn - deciding...", "dim").ctr(iw))
        inner.append(L(_forecast_text(vm), "dim").ctr(iw))
    else:
        inner.append(Ln().ctr(iw))
        best = ""
        if vm.result is not None and vm.result.children and not withheld:
            from buckshotcalc.tui.panels import plain_action

            best = plain_action(analysis.action_label(vm.result.children[0].action, vm.seat))
        pulse = st.pulse and st.motion == "full"
        inner.append(L(best, "acc" if (fr.pulse_bright or not pulse) else "fg").ctr(iw))
        inner.append(L("" if withheld or vm.result is None else _why(vm), "dim").ctr(iw))

    # "dfr" is "dim" for the dealer's frame; a role of its own so crack / static (which edit
    # border cells by role) never touch the dim title
    frame_role = "dfr" if dealer_turn else "fr"
    critical = st.d4 and hp == 1  # D4: the tag beats with the pips
    title = L(("CRITICAL - dealer's turn" if critical else "dealer") if dealer_turn
              else "CRITICAL - your move" if critical else "your move",
              ("bad+" if fr.beat_bright else "bad") if critical else "dim")
    if fr.stamp_on:
        for s in _stamps(vm):
            title.t(" ━ ", "dim").t(s, "warn")
    lines = frame(inner, w, title, frame_role, hp=hp, st=st,
                  static_t=fr.static_t if hp == 1 else None)
    if fr.wipe is not None:
        for ln in lines:
            for i in range(fr.wipe, ln.n):
                ln.c[i] = (" ", "sp")
    return lines


def _forecast_text(vm) -> str:
    if not vm.forecast:
        return "likely: not worked out yet"
    return "likely: " + " · ".join(f"{r.label} {r.probability * 100:.0f}%" for r in vm.forecast[:3])


def _recap(vm, st: Settings, fr: WatchFrames) -> list[Ln]:
    won = vm.state.winner() is vm.seat
    rows = vm.recap
    followed = sum(r.followed for r in rows)
    head = Ln().t(f"ROUND {vm.round_number} {'WON' if won else 'LOST'}", "good+" if won else "bad+")
    head.t(f"   you followed the calc {followed} of {len(rows)} moves" if rows
           else "   no moves recorded", "dim")
    inner = [head, Ln()]
    for i, r in enumerate(rows[-9:], max(1, len(rows) - 8)):
        ln = Ln().t(f"{i:2d} ", "dim").t(short_label(r.taken).ljust(16)[:16] + " ", "fg")
        if r.followed:
            ln.t("= pick ", "good")
        else:
            ln.t("≠ " + short_label(r.recommended).ljust(5)[:5], "warn")
        ln.t(f"{r.value_after * 100:4.0f}%", "fg")
        if not r.followed:
            ln.t(f"  {(r.value_after - r.recommended_value) * 100:.1f}", "bad")
        inner.append(ln)
    if rows:
        vals = [rows[0].recommended_value * 100] + [r.value_after * 100 for r in rows]
        vals.append(100.0 if won else 0.0)
        inner += [Ln(), Ln().t(spark(vals), "good").t(
            f"  {vals[0]:.0f}% → {'won' if won else 'lost'}", "dim")]
    return frame(inner, HERO_W, L("recap", "dim"), "fr", hp=vm.state.hp[vm.seat.value], st=st,
                 static_t=fr.static_t if vm.state.hp[vm.seat.value] == 1 else None)


# -- bottom of the screen ------------------------------------------------------


def _shell_rows(vm, st: Settings, fr: WatchFrames) -> list[Ln]:
    slots = list(analysis.slot_probabilities(vm.state))
    cells: list[tuple[str, str, str]] = []  # (glyph, label, role)
    blank_glyph = "▐·▌" if st.palette == "green" else "▐░▌"
    lead = fr.shells.slots[0] if fr.shells and len(fr.shells.slots) > len(slots) else None
    if lead is not None:
        kind, role = lead
        live_kind = kind == "L"
        cells.append(("▐█▌" if live_kind else blank_glyph, "LIVE" if live_kind else "blnk", role))
    for s in slots:
        if s.resolved is Shell.LIVE:
            cells.append(("▐█▌", "LIVE", "live"))
        elif s.resolved is Shell.BLANK:
            cells.append((blank_glyph, "blnk", "blank"))
        else:
            cells.append(("▐?▌", f"{s.p_live * 100:.0f}%", "dim"))
    a, b = Ln(), Ln()
    for glyph, label, role in cells:
        a.t(glyph, role).sp(3)
        b.t(label.ljust(6)[:6], role)
    out = [a.lead(2), b.lead(2)]
    if cells:
        out.append(L("▲ in the gun", "dim").lead(2 + (6 if lead is not None else 0)))
    out.append(_counts_bar(vm, st, fr).lead(2))
    return out


def _counts_bar(vm, st: Settings, fr: WatchFrames) -> Ln:
    summ = analysis.chamber_summary(vm.state)
    live, blank = summ.total_live, summ.total_blank
    bw = 14
    frac = fr.shells.live_frac if fr.shells is not None else live / max(1, live + blank)
    lw = round(frac * bw)
    ln = Ln().t("live ", "dim").t("█" * lw, "live").t("░" * (bw - lw), "blank")
    ln.t(" blank   ", "dim")
    ln.t(f"{live} live · {blank} blank", "fg")
    ch = vm.state.chamber
    if ch.slots:
        p = ch.p_live() * 100
        ln.t(" · next shell ", "dim")
        if p >= 50:
            ln.t(f"{p:.0f}% live", "live")
        else:
            ln.t(f"{100 - p:.0f}% blank", "blank")
    return ln


_WORDS = re.compile(r"(live|blank)", re.I)


def _event_line(text: str, newest: bool) -> Ln:
    ln = Ln().t("▸ " if newest else "  ", "dim")
    for part in _WORDS.split(text):
        role = _live_role(part) if _WORDS.fullmatch(part) else "fg"
        ln.t(part, role if newest else "dim")
    return ln


def _header(vm, st: Settings, fr: WatchFrames, w: int) -> Ln:
    if vm.bridge_paused is not None:
        mid = Ln().t("  paused · needs you", "warn")
    else:
        mid = Ln().t("  watching · bridge live", "dim")
    left = Ln().t("BUCKSHOTCALC", "acc")
    left.c.extend(mid.c)
    right = f"round {vm.round_number} · load {vm.load_number}"
    gap = w - len(right) - left.n
    if fr.static_t is not None and st.d5 and vm.state.hp[vm.seat.value] == 1:
        # D5: the header line has no rule to corrupt, so the static fills the gap in it
        noise = anim.static("─" * gap, fr.static_t, 1, st.motion)
        left.t("".join(" " if c == "─" else c for c in noise), "dim")
    return left.pad(w - len(right)).t(right, "dim")


_FOOTER = (
    "ctrl+z undo · ctrl+g take the game's state · ctrl+e enter by hand · "
    "ctrl+o settings · ctrl+q quit"
)


def _pause_box(vm, st: Settings, fr: WatchFrames) -> list[Ln]:
    """O1: a yellow box that replaces the hero until the pause is resolved."""
    iw = HERO_W - 4
    inner = [
        Ln(),
        L("BRIDGE PAUSED", "warn+").ctr(iw),
        L(vm.bridge_paused or "", "fg").clip(iw).ctr(iw),
        Ln(),
        Ln().t("ctrl+g", "acc").t(" take the game's state", "dim").ctr(iw),
        Ln().t("ctrl+z", "acc").t(" undo   ", "dim").t("ctrl+e", "acc").t(" enter by hand", "dim")
        .ctr(iw),
    ]
    inner += [Ln()] * (10 - len(inner))
    hp = vm.state.hp[vm.seat.value]
    return frame(inner, HERO_W, L("needs you", "dim"), "warn", hp=hp, st=st,
                 static_t=fr.static_t if hp == 1 else None)


def _fit(rows: list[Ln], h: int) -> list[Ln]:
    """Pad a block to `h` rows so what is below it never moves (it only grows if it must)."""
    return rows + [Ln() for _ in range(h - len(rows))]


def _footer(w: int) -> list[Ln]:
    """The key hints on one row, wrapped between hints when `w` is too narrow."""
    rows, cur = [], ""
    for hint in _FOOTER.split(" · "):
        if cur and len(cur) + 3 + len(hint) > w:
            rows.append(cur)
            cur = ""
        cur += (" · " if cur else "") + hint
    return [L(r, "dim") for r in rows + [cur]]


def build_lines(vm, st: Settings, fr: WatchFrames, w: int = W, big: bool = False) -> list[Ln]:
    """The whole view on a grid `w` wide: side by side at W, stacked below that."""
    done = vm.state.winner() is not None
    out = [_header(vm, st, fr, w)]
    left = _recap(vm, st, fr) if done else _hero(vm, st, fr, big)
    side = _right(vm, st, fr)
    if w < W:
        out.extend(_fit(left, HERO_H + (BIG_EXTRA if big else 0)))
        out.append(Ln())
        out.extend(_fit(side, SIDE_H))
    else:
        if done:  # centre the recap over the top block plus the empty gun rows below it
            left = [Ln() for _ in range(max(0, (SIDE_H + 5 - len(left)) // 2))] + left
        out.extend(_fit(hcat([(HERO_W, left), (SIDE_W, side)], 2), SIDE_H))
    out.append(Ln())
    # a finished round has no gun to show; the rows stay so the footer keeps its place
    out.extend(_fit([] if done else _shell_rows(vm, st, fr), 4))
    out.append(Ln())
    events = list(vm.events[-3:]) or ["waiting for the first event"]
    for i, text in enumerate(events):
        newest = i == len(events) - 1
        if newest and fr.event_text is not None:
            text = fr.event_text
        out.append(_event_line(text, newest))
    out.extend(Ln() for _ in range(3 - len(events)))
    out.append(Ln())
    out.extend(_footer(w))
    if vm.bridge_paused is not None:
        overlay(out, _pause_box(vm, st, fr), 1, 0)
    tint = _tint_role(vm, st, fr)
    if tint is not None:
        _tint(out, tint)
    return [ln.pad(w).clip(w) for ln in out]


class _WatchView:
    """Rich renderable: lays the view out for the width it is given and, when it is also told a
    height, centres it vertically and draws the win-% numeral at 2x if the whole view still fits."""

    def __init__(self, vm, st: Settings, fr: WatchFrames) -> None:
        self.vm, self.st, self.fr = vm, st, fr

    def __rich_console__(self, console, options):
        w = W if options.max_width >= W else NARROW_W
        h = options.height  # None outside the app: no centring, 1x numeral
        lines = build_lines(self.vm, self.st, self.fr, w)
        if h is not None and self.vm.state.winner() is None and self.vm.bridge_paused is None:
            bigger = build_lines(self.vm, self.st, self.fr, w, big=True)
            if len(bigger) <= h:
                lines = bigger
        if h is not None and len(lines) < h:
            lines = [Ln() for _ in range((h - len(lines)) // 2)] + lines
        yield Align(to_text(lines, self.st), "center", width=w)


def render_watch(vm, settings: Settings, frames: WatchFrames | None = None) -> RenderableType:
    """The watch view as a Rich renderable: 100 wide (centred) or stacked in 80 when narrower."""
    return _WatchView(vm, settings, frames or WatchFrames())
