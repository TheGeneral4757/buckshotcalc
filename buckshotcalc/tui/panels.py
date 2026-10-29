"""Dashboard rendering (SPEC.md §8, as amended by the 2026-09-05 redesign).

The layout the operator settled on is a **banner over three columns**:

    ┌ banner ─────────────────────────────────────────────────────────────┐
    │ whose turn it is · the recommendation, in a large numeral · why     │
    └─────────────────────────────────────────────────────────────────────┘
      DEALER          ITEMS  (both seats,      WHAT ELSE YOU COULD DO
      THE GUN          every key on screen)    WHAT HAS HAPPENED
      YOU             NEW ROUND                KEYS

Three things about it are acceptance criteria, not preferences:

1. **Nothing lives behind a help key.** Every key an operator can press is
   printed on screen, in a grid rather than a run-on line, because the old
   single-line footer was unreadable at a glance. There is no help overlay
   any more; `?` has nothing left to show.
2. **Nothing paints a background.** The operator runs a translucent terminal,
   so every style here is a foreground colour or an attribute — no `on <x>`
   anywhere, and emphasis is carried by weight, glyphs and borders.
3. **Colour is never the only signal** (SPEC.md §8). It stays the five
   meanings `theme.py` names, and every coloured element also carries a word:
   the dashboard is fully legible in monochrome, and `tests/test_tui.py`
   asserts it.

Wording is deliberately plain: "the shell in the gun", not "the chambered
round"; "chance you win this round", not "P(win)". The spec's vocabulary
stays in the docstrings, where a reviewer wants it, and off the screen, where
the operator does not.

Textual/rich only — the third-party layer SPEC.md §0.1 reserves for the TUI.
Every number rendered here is already computed: `analysis.py` derives them and
this module never touches the solver.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from buckshotcalc.engine.rules import Rules
from buckshotcalc.engine.solver import SearchResult
from buckshotcalc.engine.state import CUFF_SPENT, GameState, Item, Player, Shell
from buckshotcalc.tui import analysis, theme

# ---------------------------------------------------------------------------
# View model — everything render_dashboard needs, already computed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetupFields:
    """The inline new-round row (operator decision: no command-bar syntax).

    `focus` is the index of the field ←/→ currently edits, or -1 when the
    setup row is not focused — which is also what makes the arrow keys safe
    to share with shot entry: exactly one of the two owns them at a time,
    and the focused field is drawn with a marker so which one is never in
    doubt.
    """

    live: int
    blank: int
    max_hp: int
    focus: int = -1

    FIELDS = ("LIVE", "BLANK", "MAX HP")

    def values(self) -> tuple[int, int, int]:
        return (self.live, self.blank, self.max_hp)


@dataclass(frozen=True, slots=True)
class DashboardViewModel:
    """A snapshot ready to render — no solving happens downstream of this."""

    state: GameState
    result: SearchResult | None  # None while no solve has completed yet
    solving: bool  # a background solve is currently outstanding
    reload_table_missing: bool  # SPEC.md §7.7 hazard — see app.py
    reload_build: str | None = None  # progress sentence while ctrl+t's build runs (decision 5)
    reload_build_stopping: bool = False  # ctrl+t pressed again; waiting on the step in flight
    degraded: str | None = None  # SPEC.md §15.16(b): belief is a mixture; P(win) suppressed
    bridge_paused: str | None = None  # the game and the calc disagree; P(win) suppressed
    bridge_alert: str = ""  # bridge is off or the game's scripts are unknown
    bridge_line: str = ""  # bridge is feeding events; empty when it never connected
    log: tuple[str, ...] = ()  # `Session.log` tail, oldest first
    prompt: str = ""  # the outstanding question, phrased as a sentence
    status: str = ""  # a transient notice (undo confirmation, a raised SessionError)
    round_number: int = 1
    seat: Player = Player.PLAYER  # the operator's seat; fixed in v1
    setup: SetupFields | None = None  # the inline new-round row
    item_keys: tuple[tuple[Item, str], ...] = ()  # display order, with each item's key
    pending_use: bool = False  # `space` pressed: the next letter uses instead of adds
    pending_remove: bool = False  # `-` pressed: the next letter removes instead of adds
    # -- watch view only (watch.render_watch); the entry dashboard ignores these --
    plan: analysis.PlanNode | None = None  # best line for this turn
    forecast: tuple[analysis.ForecastRow, ...] = ()  # what the dealer will probably do
    recap: tuple[analysis.RecapRow, ...] = ()  # this round's moves, for the end-of-round card
    events: tuple[str, ...] = ()  # last few released log lines, oldest first
    load_number: int = 1  # which load of the round (1 = the first)
    rules: Rules | None = None  # for the per-move death chance (item 15(a)); None hides it


# ---------------------------------------------------------------------------
# Small presentation helpers
# ---------------------------------------------------------------------------

#: A three-row numeral, for the one number the operator reads from across the
#: room. Box-drawing rather than a font: it renders identically everywhere and
#: needs no third-party dependency beyond rich.
_BIG_GLYPHS: dict[str, tuple[str, str, str]] = {
    "0": ("┏━┓", "┃ ┃", "┗━┛"),
    "1": ("  ┃", "  ┃", "  ┃"),
    "2": ("┏━┓", "┏━┛", "┗━━"),
    "3": ("┏━┓", " ━┫", "┗━┛"),
    "4": ("┃ ┃", "┗━┫", "  ┃"),
    "5": ("┏━━", "┗━┓", "┗━┛"),
    "6": ("┏━━", "┣━┓", "┗━┛"),
    "7": ("┏━┓", "  ┃", "  ┃"),
    "8": ("┏━┓", "┣━┫", "┗━┛"),
    "9": ("┏━┓", "┗━┫", "┗━┛"),
    ".": ("  ", "  ", "▪ "),
    "?": ("┏━┓", " ━┛", " ▪ "),
    "-": ("   ", "━━━", "   "),
}


def big_number(text: str) -> tuple[str, str, str]:
    """`text` as three rows of box-drawing glyphs. Unknown chars render blank."""
    rows = ["", "", ""]
    for ch in text:
        glyph = _BIG_GLYPHS.get(ch, ("   ", "   ", "   "))
        for i in range(3):
            rows[i] += glyph[i] + " "
    return (rows[0], rows[1], rows[2])


def _bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return theme.BAR_FILLED * filled + theme.BAR_EMPTY * (width - filled)


def _hp_pips(hp: int, max_hp: int) -> str:
    """HP as pips rather than a bar: countable at a glance, and it survives
    monochrome without needing a legend."""
    return " ".join([theme.HP_FULL] * hp + [theme.HP_LOST] * max(max_hp - hp, 0))


def _count_pips(count: int, slots: int) -> str:
    """An inventory count as pips.

    Padded out to `rules.items.max_slots` empty dots was unreadable at the
    shipped cap of eight — the row became a field of dots with one pip in it.
    So: pips for what is actually held, a single dot for nothing, and a
    numeric tail past four, which is where counting by eye stops working.
    """
    if count <= 0:
        return theme.ITEM_NONE.ljust(5)
    shown = min(count, 4)
    tail = f"+{count - shown}" if count > shown else ""
    return (theme.ITEM_HELD * shown + tail).ljust(5)


def item_display_name(item: Item) -> str:
    """The short, plain name shown in the item grid (`magnifying_glass` → `magnifier`).

    Delegates to `analysis` so the grid, the questions and the history box all
    use one vocabulary.
    """
    return analysis.item_display_name(item)


#: Plain-language rewrites of the solver's action labels. Anything not listed
#: falls through unchanged, so a new action is never silently mislabelled.
_PLAIN_ACTIONS = {
    "SHOOT SELF": "SHOOT YOURSELF",
    "SHOOT DEALER": "SHOOT THE DEALER",
    "SHOOT PLAYER": "SHOOT YOU",
}


def plain_action(label: str) -> str:
    """`SHOOT SELF` → `SHOOT YOURSELF`; `USE HANDSAW` → `USE THE SAW`;
    `ADRENALINE -> MAGNIFYING_GLASS` → `ADRENALINE, TAKE THE MAGNIFIER`."""
    if label in _PLAIN_ACTIONS:
        return _PLAIN_ACTIONS[label]
    if label.startswith("ADRENALINE -> "):
        taken = label[len("ADRENALINE -> "):].lower()
        plain = analysis.ITEM_DISPLAY_NAMES.get(taken, taken.replace("_", " "))
        return f"ADRENALINE, TAKE THE {plain.upper()}"
    if label == "ADRENALINE (nothing to take)":
        return "ADRENALINE, NOTHING TO TAKE"
    if label.startswith("USE "):
        rest = label[4:].lower()
        for config_name, plain in analysis.ITEM_DISPLAY_NAMES.items():
            rest = rest.replace(config_name, plain).replace(config_name.replace("_", " "), plain)
        return "USE THE " + rest.upper()
    return label


def item_is_pointless(state: GameState, rules: Rules, item: Item, player: Player) -> bool:
    """Whether using `item` right now could not possibly change anything.

    Operator decision: items that cannot help are *dimmed rather than hidden*,
    so the grid never moves under muscle memory but a wasted keystroke is
    visibly a wasted keystroke. This is a display hint only — every one of
    these actions stays legal and stays reachable (CLAUDE.md's "saw + beer
    wastes the saw … it must be reachable"); the solver, not this function,
    decides what is *losing*.

    Every threshold read here comes from `rules` (SPEC.md §10), never a
    literal.
    """
    semantics = rules.item_semantics
    shells_left = len(state.chamber.slots)
    if item is Item.HANDSAW:
        return state.sawed and not semantics.handsaw.stackable
    if item is Item.HANDCUFFS:
        return state.cuffed[state.turn.other.value] and not (
            semantics.handcuffs.stackable_on_cuffed_target
        )
    if item is Item.CIGARETTES:
        return state.hp[player.value] >= state.max_hp
    if item in (Item.BEER, Item.MAGNIFYING_GLASS, Item.INVERTER, Item.BURNER_PHONE):
        return shells_left == 0
    if item is Item.REMOTE:
        # 1v1 only in v1: a turn-order reversal needs more seats than exist.
        return semantics.remote.min_players > 2
    if item is Item.ADRENALINE:
        return not any(n for _i, n in state.items_of(player.other).held())
    return False


#: Operator decision: the operator's own HP colours every border, whoever is
#: moving. These mirror the watch view's thresholds (watch.py `hp <= 2`,
#: `hp == 1`); they are display thresholds, not game rules, so not rules.toml.
_LOW_HP = 2
_CRITICAL_HP = 1


def _my_hp(vm: DashboardViewModel) -> int:
    return vm.state.hp[vm.seat.value]


def _border(vm: DashboardViewModel, accent: bool = False) -> str:
    """Border colour for every panel: danger colours win over the accent.

    Colour is never the only signal: the banner title also says LOW HEALTH /
    CRITICAL, and the moving seat's title says MOVING.
    """
    hp = _my_hp(vm)
    if hp <= _CRITICAL_HP:
        return theme.LIVE
    if hp <= _LOW_HP:
        return theme.WARNING
    return theme.RECOMMENDED if accent else theme.BORDER


# ---------------------------------------------------------------------------
# The banner: whose turn it is, and the one number that matters
# ---------------------------------------------------------------------------


def _turn_line(vm: DashboardViewModel) -> Text:
    """The turn, stated so loudly it cannot be misread.

    The operator's single biggest complaint about the old dashboard was that
    whose turn it was took reading to work out; it is now a full-width line
    with a direction, a word, and a sentence.
    """
    yours = vm.state.turn is vm.seat
    if yours:
        body = "◀◀◀   YOUR TURN — the gun is in your hands   ▶▶▶"
        style = f"bold {theme.RECOMMENDED}"
    else:
        body = "▶▶▶   THE DEALER'S TURN — enter what it does   ◀◀◀"
        style = f"bold {theme.WARNING}"
    return Text(body, style=style, justify="center")


def _headline_number(vm: DashboardViewModel) -> tuple[str, str]:
    """`(text-for-the-big-numeral, plain-language caption)`."""
    if vm.bridge_paused is not None:
        return ("?", "paused: the game and the calc disagree — see the notice below")
    if vm.degraded is not None:
        return ("?", "the shell count is uncertain — see the notice below")
    if vm.reload_table_missing and vm.reload_build is not None:
        return ("?", "no answer yet: the reload table is being built")
    if vm.reload_table_missing:
        return ("?", "no answer yet: the reload table has not been built")
    if vm.result is None:
        return ("?", "working it out…")
    return (f"{vm.result.value * 100:.1f}", "chance you win this round")


def _withheld(vm: DashboardViewModel) -> bool:
    """A number on screen now could be wrong without looking wrong, so none is shown."""
    return vm.degraded is not None or vm.bridge_paused is not None


def _why_line(vm: DashboardViewModel) -> Text:
    """One plain sentence saying *why*, under every recommendation.

    Deliberately about the position, not the search: it restates what is known
    about the shell in the gun, which is the fact that moves the number most.
    """
    chamber = vm.state.chamber
    if not chamber.slots:
        return Text("the gun is empty — load the next round.", style="dim")
    first = chamber.slots[0]
    if first is Shell.LIVE:
        return Text(
            "why: the shell in the gun is LIVE — it will fire.", style=theme.LIVE
        )
    if first is Shell.BLANK:
        return Text(
            "why: the shell in the gun is a BLANK — shooting yourself costs nothing "
            "and you keep the gun.",
            style=theme.BLANK,
        )
    live, blank = chamber.unknown_live, chamber.unknown_blank
    pct = chamber.p_live() * 100
    live_word = "shell" if live == 1 else "shells"
    blank_word = "shell" if blank == 1 else "shells"
    return Text(
        f"why: {live} live {live_word} and {blank} blank {blank_word} unaccounted for — "
        f"the shell in the gun fires {pct:.0f}% of the time.",
        style="dim",
    )


def _health_tag(vm: DashboardViewModel) -> str:
    hp = _my_hp(vm)
    if hp <= _CRITICAL_HP:
        return f" · CRITICAL - {hp} HEALTH LEFT"
    return " · LOW HEALTH" if hp <= _LOW_HP else ""


def _banner(vm: DashboardViewModel) -> Panel:
    number, caption = _headline_number(vm)
    rows = big_number(number)
    best = ""
    if vm.result is not None and vm.result.children and not _withheld(vm):
        best = plain_action(analysis.action_label(vm.result.children[0].action, vm.seat))

    # The per-cent sign is part of the number, so it appears only when there
    # *is* a number: a bare "?" followed by "%" would read as a percentage
    # that failed to print rather than as "no answer" (SPEC.md §15.16(b)
    # wants the absence to be unmistakable).
    unit = "  %" if number != "?" else ""
    left = Text()
    left.append(rows[0] + "\n", style=f"bold {theme.RECOMMENDED}")
    left.append(rows[1] + unit + "\n", style=f"bold {theme.RECOMMENDED}")
    left.append(rows[2], style=f"bold {theme.RECOMMENDED}")

    right = Text()
    right.append(caption.upper() + "\n", style="dim")
    if best:
        right.append(f"▶▶  {best}\n", style=f"bold {theme.RECOMMENDED}")
    elif vm.solving:
        right.append("working…\n", style=theme.WARNING)
    elif vm.reload_table_missing and vm.reload_build is not None:
        right.append("no recommendation until the table is built\n", style=theme.WARNING)
    else:
        right.append("no recommendation yet\n", style=theme.WARNING)
    right.append_text(_why_line(vm))

    grid = Table.grid(padding=(0, 3))
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_row(left, right)

    body: list[RenderableType] = [_turn_line(vm), Text(""), grid]
    for note in _notices(vm):
        body.append(note)
    return Panel(
        Group(*body),
        title=f"ROUND {vm.round_number}" + _health_tag(vm),
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


def _notices(vm: DashboardViewModel) -> list[Text]:
    """Warnings that must never be silently dropped (SPEC.md §7.7, §15.16(b))."""
    out: list[Text] = []
    if vm.bridge_paused is not None:
        out.append(
            Text(f"⚠ the game and the calc disagree: {vm.bridge_paused}", style=theme.WARNING)
        )
        out.append(Text("  ctrl+g takes the game's numbers · or fix it by hand and it resumes",
                        style="dim"))
    elif vm.bridge_alert:
        out.append(Text(f"⚠ {vm.bridge_alert}", style=theme.WARNING))
    elif vm.bridge_line:
        out.append(Text(f"⇄ {vm.bridge_line}", style="dim"))
    if vm.degraded is not None:
        out.append(Text(f"⚠ {vm.degraded}", style=theme.WARNING))
    if vm.reload_table_missing and vm.reload_build is not None:
        # Words carry the state ("building", "stopping"); the colour only repeats it.
        if vm.reload_build_stopping:
            tail = "stopping after the step in flight…"
        else:
            tail = "ctrl+t stops it"
        out.append(Text(f"⟳ building the reload table · {vm.reload_build}", style=theme.WARNING))
        out.append(Text(f"  answers appear when it finishes · {tail}", style="dim"))
    elif vm.reload_table_missing:
        out.append(
            Text(
                "⚠ the reload table has not been built for this rules and engine version, so no "
                "number can be shown for positions that reach a reload.",
                style=theme.WARNING,
            )
        )
        out.append(
            Text(
                "  press ctrl+t to build it in the background — it uses every core "
                "and can take an hour or more; it is saved, so this happens once.",
                style="dim",
            )
        )
    if vm.result is not None and vm.result.cap_reached:
        # SPEC.md §7.5: a truncated subtree makes the value provisional. It
        # was a footer line before; it belongs on the banner, beside the
        # number it qualifies.
        out.append(
            Text(
                "⚠ SEARCH CAP REACHED — the search stopped early somewhere below "
                "this position, so the number is provisional.",
                style=theme.WARNING,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Column one: the table, seen from above
# ---------------------------------------------------------------------------


def _seat_panel(vm: DashboardViewModel, player: Player, rules: Rules) -> Panel:
    state = vm.state
    is_you = player is vm.seat
    moving = state.turn is player
    title = "YOU" if is_you else "DEALER"
    # A label/value grid rather than one run-on Text: a long value wraps under
    # itself (hanging indent) instead of under the label.
    fields = Table.grid(padding=(0, 1))
    fields.add_column(no_wrap=True)
    fields.add_column()

    hp = Text()
    hp.append(_hp_pips(state.hp[player.value], state.max_hp), style=theme.LIVE)
    hp.append(f"   {state.hp[player.value]}/{state.max_hp}")
    fields.add_row(Text("HEALTH", style="dim"), hp)

    parts: list[tuple[str, str]] = []
    if state.cuffed[player.value]:
        spent = state.cuffed[player.value] == CUFF_SPENT
        note = "breaks free next turn" if spent else "skips a turn"
        # ∞, not ⛓: the operator's font has no chain glyph and the fallback
        # overdraws the next cell.
        parts.append((f"∞ {theme.CUFFED_FLAG} — {note}", theme.WARNING))
    if moving and state.sawed:
        parts.append((f"▰ {theme.SAWED_FLAG} — the next hit is doubled", theme.LIVE))
    if not parts:
        fields.add_row(Text("STATUS", style="dim"), Text("nothing"))
    for i, (word, style) in enumerate(parts):
        fields.add_row(Text("STATUS" if i == 0 else "", style="dim"), Text(word, style=style))

    if not is_you and state.dealer_knows:
        known = ", ".join(str(i + 1) for i in sorted(state.dealer_knows))
        fields.add_row(
            Text("KNOWS", style="dim"),
            Text(f"shells {known} — it has seen these", style=theme.WARNING),
        )

    return Panel(
        fields,
        title=title + ("   ◀ MOVING" if moving else ""),
        title_align="left",
        border_style=_border(vm, moving),
        box=box.ROUNDED,
    )


class _ShellCards:
    """The shell cards, six cells each, wrapped onto further rows when the
    panel is too narrow for all of them (eight shells need 48 columns)."""

    def __init__(self, slots) -> None:
        self._slots = slots

    def __rich_console__(self, console, options):
        per_row = max(1, options.max_width // 6)
        for start in range(0, len(self._slots), per_row):
            chunk = self._slots[start:start + per_row]
            cards = Table.grid(padding=(0, 0))
            for _ in chunk:
                cards.add_column(justify="center")
            top, ids, bars, labels, bottom = [], [], [], [], []
            for i, slot in enumerate(chunk, start):
                marker = "▸" if i == 0 else " "
                if slot.resolved is Shell.LIVE:
                    label, style = "LIVE", theme.LIVE
                elif slot.resolved is Shell.BLANK:
                    label, style = "BLNK", theme.BLANK
                else:
                    label, style = f"{slot.p_live * 100:.0f}%", "default"
                top.append(Text("┌────┐", style="dim"))
                ids.append(Text(f"│{marker}{i + 1:<3}│", style="dim"))
                bars.append(Text("│", style="dim") + Text(_bar(slot.p_live, 4), style=style)
                            + Text("│", style="dim"))
                labels.append(Text("│", style="dim") + Text(f"{label:^4}", style=style)
                              + Text("│", style="dim"))
                bottom.append(Text("└────┘", style="dim"))
            for row in (top, ids, bars, labels, bottom):
                cards.add_row(*row)
            yield cards


def _gun_panel(vm: DashboardViewModel) -> Panel:
    """The shells as cards, each carrying its own chance of firing.

    Replaces the old `? ? L ?` strip: a per-slot bar is the thing the operator
    actually reads, and a resolved slot says LIVE/BLANK in words so the panel
    survives monochrome.
    """
    state = vm.state
    slots = analysis.slot_probabilities(state)
    summary = analysis.chamber_summary(state)

    header = Text()
    header.append(f"{summary.total_live} LIVE", style=theme.LIVE)
    header.append(" · ")
    header.append(f"{summary.total_blank} BLANK", style=theme.BLANK)
    header.append(f" · {len(state.chamber.slots)} in the gun", style="dim")

    if not slots:
        return Panel(
            Group(header, Text("")),
            title="THE GUN",
            title_align="left",
            border_style=_border(vm),
            box=box.ROUNDED,
        )

    chambered = Text("▸ shell 1 is in the gun now", style="dim")
    return Panel(
        Group(header, Text(""), _ShellCards(slots), Text(""), chambered),
        title="THE GUN",
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Column two: items (the panel the whole redesign is built around)
# ---------------------------------------------------------------------------


def _items_panel(vm: DashboardViewModel, rules: Rules) -> Panel:
    """Every item, every key, both inventories — always on screen.

    The operator's brief: choosing what you and the Dealer hold is the single
    most common thing done with this tool, so it gets a permanent panel where
    the key is printed beside the name and nothing has to be remembered. A key
    means exactly one thing here: `c` adds cigarettes to *your* rack, `C` adds
    them to the Dealer's. Using is a separate gesture (`space` first), so no
    key ever changes meaning based on what happens to be held.
    """
    slots = rules.items.max_slots
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left", no_wrap=True)  # key
    grid.add_column(justify="left", no_wrap=True)  # name
    grid.add_column(justify="left", no_wrap=True)  # yours
    grid.add_column(justify="left", no_wrap=True)  # dealer's

    grid.add_row(Text(""), Text(""), Text("YOU", style="dim"), Text("DEALER", style="dim"))
    for item, key in vm.item_keys:
        mine = vm.state.items_of(vm.seat)[item]
        theirs = vm.state.items_of(vm.seat.other)[item]
        pointless = item_is_pointless(vm.state, rules, item, vm.state.turn)
        name_style = "dim" if pointless else "default"
        grid.add_row(
            Text(f"[{key}]", style="dim" if pointless else theme.RECOMMENDED),
            Text(item_display_name(item), style=name_style),
            Text(_count_pips(mine, slots), style="dim" if not mine else name_style),
            Text(_count_pips(theirs, slots), style="dim" if not theirs else name_style),
        )

    legend = Table.grid(padding=(0, 2))
    legend.add_column(justify="right", no_wrap=True)
    legend.add_column(justify="left", no_wrap=True)
    for gesture, meaning in (
        ("key", "you have one"),
        ("SHIFT+key", "the dealer has one"),
        ("space key", "it was used"),
        ("- key", "take one away"),
    ):
        legend.add_row(Text(gesture, style=theme.RECOMMENDED), Text(meaning))

    note = Text(
        "\ndimmed = held, but using it now would change nothing", style="dim"
    )

    return Panel(
        Group(grid, Text(""), legend, note),
        title="ITEMS",
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


def _setup_panel(vm: DashboardViewModel) -> Panel:
    """The inline new-round row — steppers, not a command-bar grammar.

    Focused (`tab`) it owns ←/→; unfocused it is a read-only summary and the
    arrows belong to shot entry.
    """
    setup = vm.setup
    focused = setup is not None and setup.focus >= 0
    if setup is None:
        setup = SetupFields(live=0, blank=0, max_hp=vm.state.max_hp)

    # no_wrap + ellipsis: a wrapped "MAX HP" / "4" is worse than a clipped tail.
    row = Text(no_wrap=True, overflow="ellipsis")
    for i, (name, value) in enumerate(zip(SetupFields.FIELDS, setup.values(), strict=True)):
        on = focused and setup.focus == i
        row.append("  " if i else "")
        row.append(f"{name} ", style=f"bold {theme.RECOMMENDED}" if on else "dim")
        row.append("◂" if on else " ", style=theme.RECOMMENDED)
        row.append(str(value), style="bold" if on else "default")
        row.append("▸" if on else " ", style=theme.RECOMMENDED)

    hint = Text()
    if focused:
        hint.append("←→ change · tab next · enter load · esc leave", style=theme.RECOMMENDED)
    else:
        hint.append("tab", style=theme.RECOMMENDED)
        hint.append(" edits these · ")
        hint.append("enter", style=theme.RECOMMENDED)
        hint.append(" loads a fresh round", style="default")

    return Panel(
        Group(row, Text(""), hint),
        title="NEW ROUND" + ("   ◀ EDITING" if focused else ""),
        title_align="left",
        border_style=_border(vm, focused),
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Column three: alternatives, history, keys
# ---------------------------------------------------------------------------


class _AlternativeRows:
    """The ranked alternatives, sized to the width they are given.

    Rich renderables do not know their width up front, so this reads
    `options.max_width`: the label column is as wide as the longest label (so
    the % column always lines up) but is truncated before it could push the %
    out, and the bar is dropped rather than wrapped when it does not fit.
    """

    def __init__(self, alternatives: tuple[analysis.RankedAlternative, ...]) -> None:
        self._alts = alternatives

    def __rich_console__(self, console, options):
        labels = [
            plain_action(a.label)
            + (f" {theme.WASTED_MARK} wastes it" if a.wasted else "")
            + (f" - {analysis.dies_text(a.dies)}" if a.dies > 0 else "")
            for a in self._alts
        ]
        width = options.max_width
        value_w = 6  # "100.0%"
        label_w = min(max(len(x) for x in labels), max(width - 2 - 2 - value_w, 4))
        bar_w = min(18, width - 2 - label_w - 2 - value_w - 2)
        if bar_w < 4:
            bar_w = 0
        for alt, label in zip(self._alts, labels, strict=True):
            style = theme.RECOMMENDED if alt.is_best else theme.ALTERNATIVE
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append("▶ " if alt.is_best else "  ",
                        style=f"bold {theme.RECOMMENDED}" if alt.is_best else "dim")
            name = Text(label, style=style)
            name.truncate(label_w, overflow="ellipsis", pad=True)
            line.append_text(name)
            line.append(f"  {alt.value * 100:5.1f}%")
            if bar_w:
                line.append("  " + _bar(alt.value, bar_w), style=style)
            yield line


def _alternatives_panel(vm: DashboardViewModel) -> Panel:
    rows: list[RenderableType] = []
    alternatives = (
        analysis.ranked_alternatives(vm.result, vm.seat, vm.state, vm.rules)
        if vm.result is not None and not _withheld(vm)
        else ()
    )
    if not alternatives:
        rows.append(Text("nothing to choose here yet", style="dim"))
    else:
        rows.append(_AlternativeRows(alternatives))
    if vm.result is not None:
        # The `[a]` analysis board's search diagnostics (SPEC.md §8), folded
        # into the panel they qualify rather than hidden behind a toggle —
        # §8's own opening requirement is "everything visible simultaneously,
        # no drill-down".
        rows.append(
            Text(
                f"\nsearched {vm.result.nodes:,} positions"
                + ("  ·  stopped early at the search cap" if vm.result.cap_reached else ""),
                style="dim",
            )
        )
    return Panel(
        Group(*rows),
        title="WHAT ELSE YOU COULD DO",
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


def _log_panel(vm: DashboardViewModel) -> Panel:
    """The history box — deliberately tall (operator decision: "longer log")."""
    if not vm.log:
        rows = [Text("nothing has happened yet", style="dim")]
    else:
        rows = []
        for i, line in enumerate(vm.log):
            newest = i == len(vm.log) - 1
            rows.append(
                Text(("▸ " if newest else "· ") + line,
                     style="default" if newest else "dim")
            )
    return Panel(
        Group(*rows),
        title="WHAT HAS HAPPENED",
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


#: The key legend, as a grid of (heading, key, meaning) rows. On screen at all
#: times, in columns — the old run-on footer line was the thing the operator
#: could not read at a glance. The item gestures (key / SHIFT+key / space / -)
#: and each item's own key are printed by the ITEMS panel, so not repeated here.
_KEY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("SHOTS", "↑", "the shot was at yourself…"),
    ("", "↓", "the shot was at the other one…"),
    ("", "then ← →", "…and it was a blank / a live shell"),
    ("ROUND", "tab", "edit the new-round numbers"),
    ("", "enter", "start that round"),
    ("", "ctrl+l", "the gun was reloaded mid-round"),
    ("FIXING", "ctrl+z", "undo — it says what it undid"),
    ("", "esc", "back out of the question on screen"),
    ("", "ctrl+t", "build the reload table (again to stop)"),
    ("", "ctrl+g", "take the game's numbers when paused"),
    ("", "ctrl+e", "switch between the watch view and this one"),
    ("", "ctrl+o", "look and motion settings"),
    ("", "ctrl+q", "quit"),
)


def _keys_panel(vm: DashboardViewModel) -> Panel:
    # Two columns, with each group's heading on its own row: a third column
    # for the heading is the first thing to be truncated when the rail is
    # narrow, and a legend that reads "S…" is no legend at all.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(justify="left")
    for heading, key, meaning in _KEY_ROWS:
        if heading:
            grid.add_row(Text(""), Text(heading, style="dim"))
        grid.add_row(Text(key, style=theme.RECOMMENDED), Text(meaning))
    return Panel(
        grid,
        title="KEYS",
        title_align="left",
        border_style=_border(vm),
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# The question / notice line
# ---------------------------------------------------------------------------


def _status_line(vm: DashboardViewModel) -> Text:
    if vm.prompt:
        return Text("▸ " + vm.prompt, style=f"bold {theme.WARNING}")
    if vm.pending_use:
        return Text("▸ space — now press an item's key to say it was used  (esc cancels)",
                    style=f"bold {theme.RECOMMENDED}")
    if vm.pending_remove:
        return Text("▸ minus — now press an item's key to take one away  (esc cancels)",
                    style=f"bold {theme.RECOMMENDED}")
    if vm.status:
        return Text("▸ " + vm.status, style=theme.WARNING)
    if vm.solving:
        return Text("▸ working out the answer…", style="dim")
    return Text("▸ ready", style="dim")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


#: Below this width three columns truncate item names ("cigare…"), so they stack.
_STACK_BELOW = 118


class _Columns:
    """Three columns at ratio 34/30/36, or one stack when the terminal is narrow."""

    def __init__(self, *columns: RenderableType) -> None:
        self._columns = columns

    def __rich_console__(self, console, options):
        if options.max_width < _STACK_BELOW:
            yield Group(*self._columns)
            return
        grid = Table.grid(padding=(0, 1), expand=True)
        for ratio in (34, 30, 36):
            grid.add_column(ratio=ratio)
        grid.add_row(*self._columns)
        yield grid


def render_dashboard(vm: DashboardViewModel, rules: Rules) -> Group:
    """The banner over three columns (see the module docstring)."""
    left = Group(
        _seat_panel(vm, vm.seat.other, rules),
        _gun_panel(vm),
        _seat_panel(vm, vm.seat, rules),
    )
    middle = Group(_items_panel(vm, rules), _setup_panel(vm))
    right = Group(_alternatives_panel(vm), _log_panel(vm), _keys_panel(vm))

    body = _Columns(left, middle, right)

    # The question sits right under the banner, never last: on a short terminal
    # the bottom is what gets cropped, and this line is the one that must not be.
    return Group(_banner(vm), _status_line(vm), body)


def render_watch(vm: DashboardViewModel, settings, frames=None):
    """The watch layout (see `watch.py`); imported late so panels stays the base module."""
    from buckshotcalc.tui import watch

    return watch.render_watch(vm, settings, frames)
