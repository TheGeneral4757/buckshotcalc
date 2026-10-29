"""Pure, time-injected animation frames for the dashboard.

No Textual or Rich imports: every function takes explicit inputs plus `t`
(seconds since the animation started) and returns plain strings / tuples.
Ported from the operator-approved showcase page (target 25 fps). Colour is a
*role* name ('acc', 'good', 'bad', ...) the caller maps through the palette.

Motion modes (settings.motion): pass `mode` to any function that takes it.
'full' plays everything; 'reduced' runs tweens ~3x faster and drops every
flicker (pulse, static, heartbeat, blink, flashes); 'off' returns the end
frame at any `t`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

FPS = 25
_END = 1e9

BLK = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": (" █ ", "██ ", " █ ", " █ ", "███"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    "%": ("█ █", "  █", " █ ", "█  ", "█ █"),
    ".": ("   ", "   ", "   ", "   ", " █ "),
    " ": ("   ", "   ", "   ", "   ", "   "),
    "?": ("███", "  █", " ██", "   ", " █ "),  # no answer yet (watch view)
    "-": ("   ", "   ", "███", "   ", "   "),
}


def motion_t(mode: str, t: float) -> float:
    """Effective time for a motion mode: 'off' jumps to the end frame."""
    if mode == "off":
        return _END
    return t * 3 if mode == "reduced" else t


def big(text: str) -> list[str]:
    """Block-font rows (5) for a string of digits, '.', '%' and spaces."""
    return [" ".join(BLK.get(ch, BLK[" "])[r] for ch in text) for r in range(5)]


def fmt_pct(v: float) -> str:
    return f"{v:.1f}".rjust(4) + "%"


def eighth_bar(frac: float, w: int) -> str:
    e = " ▏▎▍▌▋▊▉█"
    tot = max(0.0, min(1.0, frac)) * w
    full = int(tot)
    rem = round((tot - full) * 8)
    s = "█" * full
    if full < w and rem:
        s += e[rem]
    return s.ljust(w)


# -- number tweens (N1-N5) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumFrame:
    text: str  # the number string to draw with big()
    role: str
    done: bool
    old: str | None = None  # N4 only: the outgoing number
    wipe: int = 0  # N4 only: rows already showing `text`


def number_tween(code: str, frm: float, to: float, t: float, mode: str = "full") -> NumFrame:
    """Frame of a win-chance change from `frm` to `to` (percent). (N1-N5)"""
    t = motion_t(mode, t)
    f = int(t * FPS)
    a, b = fmt_pct(frm), fmt_pct(to)
    role = "good" if to > frm else "bad"
    end = NumFrame(b, "acc", True)
    if f <= 0 and t < _END:
        return NumFrame(a, role, False)
    if code == "N1":  # odometer: each digit ticks up to its target
        if f > 35:
            return end
        s = ""
        for i, (ca, cb) in enumerate(zip(a, b, strict=True)):
            if not (ca.isdigit() and cb.isdigit()):
                s += cb
                continue
            steps = (int(cb) - int(ca)) % 10
            s += str((int(ca) + max(0, min(steps, f - i * 3))) % 10)
        return NumFrame(s, "acc" if f > 22 else role, False)
    if code == "N2":  # count + flash
        if f > 30:
            return end
        p = min(1.0, f / 14)
        e = 1 - (1 - p) ** 3
        return NumFrame(fmt_pct(frm + (to - frm) * e), role, False)
    if code == "N3":  # decode: left to right, scrambled digits lock in
        lock = f // 3
        if lock >= len(b) + 2:
            return end
        rng = random.Random(f"{frm}|{to}|{f}")
        s = "".join(
            cb if (i < lock or not cb.isdigit()) else str(rng.randrange(10))
            for i, cb in enumerate(b)
        )
        return NumFrame(s, role, False)
    if code == "N4":  # row wipe from the old numeral to the new
        k = f // 2
        if k > 5 + 3:
            return end
        return NumFrame(b, role, False, old=a, wipe=k)
    if code == "N5":  # overshoot then settle
        if f > 30:
            return end
        p = min(1.0, f / 22)
        ov = 0.12
        e = (p / 0.6) * (1 + ov) if p < 0.6 else 1 + ov * (1 - (p - 0.6) / 0.4)
        return NumFrame(fmt_pct(frm + (to - frm) * e), role, False)
    raise ValueError(f"unknown number animation {code!r}")


def number_rows(fr: NumFrame) -> list[tuple[str, str]]:
    """Five (row, role) pairs for a NumFrame; only N4 mixes two numerals."""
    new = big(fr.text)
    if fr.old is None:
        return [(r, fr.role) for r in new]
    old = big(fr.old)
    return [(new[i], fr.role) if i < fr.wipe else (old[i], "dim") for i in range(5)]


# -- shell reveal + counts-bar drain (H4) -------------------------------------

_BAR_W = 14


@dataclass(frozen=True, slots=True)
class ShellFrame:
    slots: tuple[tuple[str, str], ...]  # (kind 'L'/'B'/'U', role)
    bar: str  # eighth-block live share, width 14
    live_frac: float
    done: bool


def shell_strip(
    revealed: str, n_before: int, live_before: int, blank_before: int,
    live_after: int, blank_after: int, t: float, mode: str = "full",
) -> ShellFrame:
    """The fired/ejected shell shows what it was, then vanishes while the
    counts bar drains to the new live:blank split (~2 s). (H4)"""
    t = motion_t(mode, t)
    rest = tuple(("U", "dim") for _ in range(max(0, n_before - 1)))
    f0 = live_before / max(1, live_before + blank_before)
    f1 = live_after / max(1, live_after + blank_after)
    done = t >= 2.0
    if t < 0.36:
        slots, frac = ((revealed, "live" if revealed == "L" else "blank"),) + rest, f0
    else:
        frac = f0 + (f1 - f0) * min(1.0, (t - 0.36) / 0.72)
        slots = ((revealed, "dim"),) + rest if t < 0.6 else rest
    if done:
        frac = f1
    return ShellFrame(slots, eighth_bar(frac, _BAR_W), frac, done)


# -- search progress (P2) -----------------------------------------------------


class ProgressSmoother:
    """Display fraction that eases toward the next real step, creeps slowly,
    never passes the ceiling (done+0.9)/total and never goes backwards. (P2)"""

    def __init__(self) -> None:
        self.disp = 0.0

    def reset(self) -> None:
        self.disp = 0.0

    def step(self, real_done: int, total: int, dt: float) -> float:
        if total <= 0:
            return self.disp
        if real_done >= total:
            self.disp = 1.0
            return 1.0
        k = dt / 0.05
        real = real_done / total
        ceil = min(1.0, (real_done + 0.9) / total)
        ease = 1 - (1 - 0.18) ** k
        nxt = self.disp + (real - self.disp) * ease + 0.0025 * k
        self.disp = max(self.disp, min(ceil, nxt))
        return self.disp


def p2_bar(fraction: float, total: int, width_per_seg: int = 4) -> list[tuple[str, str]]:
    """(text, role) runs: one segment per root move, '|' dividers."""
    out: list[tuple[str, str]] = []
    for i in range(total):
        fr = max(0.0, min(1.0, fraction * total - i))
        out.append((eighth_bar(fr, width_per_seg), "acc" if fr >= 1 else "warn"))
        if i < total - 1:
            out.append(("│", "dim"))
    return out


# -- small effects ------------------------------------------------------------


def delta_badge(delta: float, t: float, mode: str = "full") -> tuple[str, str, int, bool]:
    """(text, role, level 3..0, done) for the +6.1 / -3.0 badge; fades over 4 s.
    'off' shows it steady for 4 s with no fade. (X1)"""
    role = "good" if delta >= 0 else "bad"
    if t >= 4.0:
        return "", role, 0, True
    level = 3 if mode == "off" else 3 - int(t / 4.0 * 4)
    return f"{delta:+.1f}", role, max(1, level), False


def pulse_phase(t: float, mode: str = "full") -> str:
    """'bright' or 'dim' on a 1.6 s breath for the best action. (X2)"""
    if mode != "full":
        return "bright"
    return "dim" if 0.4 <= t % 1.6 < 1.2 else "bright"


def heartbeat(t: float, hp: int, mode: str = "full") -> bool:
    """True = pips bright. Beats at 2 hp and below (faster at 1). (D4)"""
    if mode != "full" or hp > 2:
        return True
    ph = (t % (0.8 if hp <= 1 else 1.2)) / (0.8 if hp <= 1 else 1.2)
    return not (0.15 <= ph < 0.3 or 0.45 <= ph < 0.6)


def _lcg(seed: int):
    s = seed

    def rnd() -> float:
        nonlocal s
        s = (s * 9301 + 49297) % 233280
        return s / 233280

    return rnd


def crack(lines: list[str], hp: int, seed: int = 7) -> list[str]:
    """Break frame lines up as health falls: none at 4+, 5% / 14% / 30%. (D2)"""
    if hp >= 4:
        return list(lines)
    rate = 0.05 if hp == 3 else 0.14 if hp == 2 else 0.3
    rnd = _lcg(seed)
    gaps = "╌┄ ·"

    def one(ch: str) -> str:
        if ch not in "─━│┃═║" or rnd() >= rate:
            return ch
        if ch in "│┃║":
            return "╎" if rnd() < 0.5 else " "
        return gaps[int(rnd() * len(gaps))]

    return ["".join(one(c) for c in ln) for ln in lines]


def static(line: str, t: float, seed: int = 0, mode: str = "full") -> str:
    """Briefly corrupt horizontal frame chars of a header line. (D5)
    Pass only frame lines; numbers and the action are never routed here."""
    if mode != "full" or int(t / 0.3) % 6 >= 2:
        return line
    rng = random.Random(f"{seed}|{int(t / 0.1)}")
    return "".join(
        rng.choice("▚▞░▒") if c in "─━" and rng.random() < 0.25 else c for c in line
    )


def typewriter(text: str, t: float, cps: float = 60, mode: str = "full") -> tuple[str, bool]:
    n = len(text) if mode == "off" else int(t * cps * (3 if mode == "reduced" else 1))
    return text[: max(0, n)], n >= len(text)


def pip_loss(t: float, mode: str = "full") -> tuple[bool, bool]:
    """(flash_on, done): a lost HP pip flashes for 0.4 s."""
    if mode != "full" or t >= 0.4:
        return False, True
    return int(t / 0.1) % 2 == 0, False


def pip_gain(t: float, mode: str = "full") -> tuple[int, bool]:
    """(level 1..3, done): a gained HP pip fades in over 0.45 s."""
    if mode != "full" or t >= 0.45:
        return 3, True
    return 1 + int(t / 0.15), False


def item_fade(t: float, mode: str = "full") -> tuple[int, bool]:
    """(level, done): a used item flashes (4) for 0.3 s then fades 3..0 by 1 s."""
    if mode != "full" or t >= 1.0:
        return 0, True
    if t < 0.3:
        return 4, False
    return 3 - int((t - 0.3) / 0.7 * 3.99), False


def steal_offset(t: float, width: int, mode: str = "full") -> tuple[int, bool]:
    """Columns still to slide for a stolen item, easing to 0 over 0.5 s."""
    if mode == "off" or t >= 0.5:
        return 0, True
    return round(width * (1 - t / 0.5) ** 2), False


def turn_wipe(t: float, width: int, mode: str = "full") -> tuple[int, bool]:
    """Column the turn-handoff wipe has reached across the hero (0.5 s)."""
    if mode == "off" or t >= 0.5:
        return width, True
    return int(width * t / 0.5), False


def stamp_visible(t: float, mode: str = "full") -> bool:
    """SAWED / CUFFED stamp blinks in over 0.6 s, then holds."""
    if mode != "full" or t >= 0.6:
        return True
    return int(t / 0.1) % 2 == 1
