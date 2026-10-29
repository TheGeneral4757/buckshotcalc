"""Live bridge, calc side (docs/live-bridge.md): game-mod JSON events -> Session actions.

`apply_event` is a pure adapter (no I/O, no Textual): it turns one event into the same
`Session` call the operator would type, so undo and the manual keys keep working. It only
ever writes what a human at the table can see; it never touches `edit_chamber` or
`edit_dealer_knows`. `serve` is the small asyncio TCP listener that feeds it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from buckshotcalc.engine.state import Item, ItemCounts, Player, Shell
from buckshotcalc.tui import timing
from buckshotcalc.tui.session import Session, SessionError

PORT = 47600
PROTOCOL_V = 1
_KNOWN_FILE = Path(__file__).with_name("bridge_known.json")

_SEAT = {"player": Player.PLAYER, "dealer": Player.DEALER}
_SHELL = {"live": Shell.LIVE, "blank": Shell.BLANK}
_PLAIN = {"cigarettes": Item.CIGARETTES, "handcuffs": Item.HANDCUFFS, "handsaw": Item.HANDSAW}
_INFO = (Item.MAGNIFYING_GLASS, Item.BURNER_PHONE)
# Items whose outcome arrives in a later event (beer eject, glass, phone, medicine).
_WAITS = {Item.BEER, Item.MAGNIFYING_GLASS, Item.BURNER_PHONE, Item.EXPIRED_MEDICINE}
_OUTCOME_EV = {
    "eject": Item.BEER,
    "glass": Item.MAGNIFYING_GLASS,
    "phone": Item.BURNER_PHONE,
    "medicine": Item.EXPIRED_MEDICINE,
}
_KNOWN_EV = {
    "hello", "load", "turn", "sync", "shot", "item", "eject", "glass", "phone",
    "medicine", "wire_cut", "end", "gap",
}  # fmt: skip


def known_scripts_sha() -> str | None:
    try:
        return json.loads(_KNOWN_FILE.read_text())["scripts_sha"]
    except (OSError, ValueError, KeyError):
        return None


@dataclass
class Result:
    status: str = ""
    applied: bool = False  # a Session action ran
    reply: dict | None = None  # line to send back to the mod
    close: bool = False  # drop the connection after replying


@dataclass
class BridgeState:
    """Everything the adapter remembers between events (the Session holds the game)."""

    off: str | None = None  # why events are ignored until the next hello
    warn: str = ""  # unknown game scripts
    paused: str | None = None  # why recommendations are paused
    last_seq: int | None = None
    last_sync: dict | None = None
    pending: tuple[Item, Player, bool] | None = None  # item waiting for its outcome event
    adrenaline: bool = False  # adrenaline seen, stolen item still to come
    ended: bool = False
    skip_sync: bool = False  # the next sync is mid cuff-skip (game turn lags the engine's)
    # The game breaks a skipped seat's cuffs ~1.4s into that seat's NEXT turn, after the `turn`
    # event and its sync: that one sync still reports the seat cuffed, the engine cleared it.
    cuff_seat: str | None = None  # seat that skipped on cuffs, until its next turn begins
    break_seat: str | None = None  # seat whose cuffed flag the next sync may still show stale
    status: str = ""
    # Display gate: events apply at once, the screen waits until the player would see them.
    release_at: float = 0.0  # monotonic time of the latest pending reveal (0 = never gated)

    def banner(self) -> str:
        """One line for the status/notice area; empty when everything is fine."""
        if self.paused:
            return f"BRIDGE PAUSED: {self.paused} (ctrl+g accepts the game's state)"
        return self.off or self.warn or ""


def _items(mapping: dict) -> ItemCounts:
    return ItemCounts.from_mapping(
        {Item.from_config_name(k.replace(" ", "_")): int(n) for k, n in mapping.items()}
    )


def _diffs(session: Session, sync: dict) -> list[tuple[str, Callable[[], object] | None]]:
    """Differences between the tracked state and a `sync`, each with the edit that fixes it."""
    st = session.state
    out: list[tuple[str, Callable[[], object] | None]] = []

    def add(label: str, ours, theirs, fix) -> None:
        if ours != theirs:
            out.append((f"{label}: calc {ours}, game {theirs}", fix))

    add("max hp", st.max_hp, sync["max_hp"], lambda: session.edit_max_hp(sync["max_hp"]))
    for i, seat in enumerate(Player):
        w = seat.name.lower()
        add(
            f"{w} hp", st.hp[i], sync["hp"][i],
            lambda i=i, seat=seat: session.edit_hp(seat, sync["hp"][i]),
        )  # fmt: skip
        add(
            f"{w} cuffed", bool(st.cuffed[i]), bool(sync["cuffed"][i]),
            lambda i=i, seat=seat: session.edit_cuffed(seat, bool(sync["cuffed"][i])),
        )  # fmt: skip
    add("sawed", st.sawed, bool(sync["sawed"]), lambda: session.edit_sawed(bool(sync["sawed"])))
    add("turn", st.turn.name.lower(), sync["turn"], lambda: session.edit_turn(_SEAT[sync["turn"]]))
    add("shells left", len(st.chamber), sync["left"], None)  # chamber contents are never guessed
    for seat in Player:
        w = seat.name.lower()
        theirs = _items(sync["items"][w])
        for item in Item:
            add(
                f"{w} {item.config_name}", st.items_of(seat)[item], theirs[item],
                lambda seat=seat, item=item, n=theirs[item]: session.edit_item_count(seat, item, n),
            )  # fmt: skip
    return out


def accept_game_state(session: Session, bs: BridgeState) -> str:
    """The one operator action: make the Session match the game's last `sync`, then resume."""
    if bs.last_sync is None:
        return "no game state received yet"
    skipped = []
    for label, fix in _diffs(session, bs.last_sync):
        if fix is None:
            skipped.append(label)
        else:
            fix()
    bs.paused = None
    bs.pending = None
    bs.adrenaline = False
    note = f" (not editable: {'; '.join(skipped)})" if skipped else ""
    return "accepted the game's state" + note


def display_released(bs: BridgeState, now: float) -> bool:
    """True when the screen may show the live state (every event's reveal time has passed)."""
    return now >= bs.release_at


def _pause(bs: BridgeState, why: str) -> Result:
    bs.paused = why
    bs.pending = None
    bs.adrenaline = False
    return Result(f"bridge paused: {why}")


def apply_event(
    session: Session, event: dict, bs: BridgeState, now: float | None = None
) -> Result:
    """Apply one mod event. Never raises on a bad event: it pauses or switches to manual entry."""
    ev = event.get("ev") if isinstance(event, dict) else None
    if not isinstance(event, dict) or event.get("v") != PROTOCOL_V:
        bs.off = "bridge: unknown protocol version, manual entry"
        return Result(bs.off, reply={"v": PROTOCOL_V, "ev": "hello_ack", "ok": False}, close=True)
    if ev == "hello":
        return _hello(bs, event)
    if ev not in _KNOWN_EV:
        bs.off = f"bridge: unknown event {ev!r}, manual entry"
        return Result(bs.off)
    if bs.off:
        return Result(bs.off)
    seq = event.get("seq")
    if isinstance(seq, int):
        if bs.last_seq is not None:
            if seq <= bs.last_seq:
                return Result("duplicate event ignored")
            if seq > bs.last_seq + 1 and ev != "gap" and not bs.paused:
                bs.last_seq = seq
                return _pause(bs, f"events missing before seq {seq}")
        bs.last_seq = seq
    try:
        res = _dispatch(session, event, bs, ev)
    except (
        SessionError, KeyError, ValueError, TypeError, AssertionError, IndexError, AttributeError
    ) as exc:
        return _pause(bs, f"seq {event.get('seq')} {ev}: {exc}")
    if isinstance(seq, int) and not bs.paused and not bs.off and timing.REVEAL_SCALE > 0:
        t = time.monotonic() if now is None else now
        bs.release_at = max(bs.release_at, t + timing.reveal_offset(event) * timing.REVEAL_SCALE)
    return res


def _hello(bs: BridgeState, event: dict) -> Result:
    last_sync = bs.last_sync
    bs.__init__()  # type: ignore[misc]  # a new connection starts clean
    bs.last_sync = last_sync
    bs.last_seq = event.get("seq") if isinstance(event.get("seq"), int) else None
    sha = event.get("scripts_sha")
    known = sha is not None and sha == known_scripts_sha()
    reply = {"v": PROTOCOL_V, "ev": "hello_ack", "ok": True, "known": known}
    if event.get("mode") != "endless":
        bs.off = "bridge: campaign not supported, manual entry"
        return Result(bs.off, reply=reply)
    if not known:
        bs.warn = "bridge: unknown game scripts (hash differs), continuing"
    return Result(bs.warn or "bridge connected", reply=reply)


def _dispatch(session: Session, e: dict, bs: BridgeState, ev: str) -> Result:
    if ev == "gap":
        return _pause(bs, f"{e.get('dropped', '?')} events were dropped")
    if ev == "sync":
        bs.last_sync = e
        if bs.skip_sync:
            bs.skip_sync = False
            return Result("sync during cuff skip, checked at the next turn")
        diffs = _diffs(session, e)
        if bs.break_seat:
            diffs = [d for d in diffs if not d[0].startswith(f"{bs.break_seat} cuffed")]
            bs.break_seat = None
        if not diffs:
            bs.paused = None
            return Result("in sync")
        return _pause(bs, "; ".join(label for label, _ in diffs))
    if ev == "load":
        return _load(session, e, bs)
    if bs.paused:
        return Result(f"ignored while paused: {ev}")
    if ev == "turn":
        # The engine already passed the turn when the shot/item resolved; the `sync` that
        # follows is the check. A repeated announcement is therefore a no-op.
        bs.skip_sync = bool(e.get("cuffed_skip"))
        if bs.skip_sync:
            bs.cuff_seat = e.get("who")
        elif e.get("who") == bs.cuff_seat:
            bs.break_seat, bs.cuff_seat = bs.cuff_seat, None
        return Result("turn")
    if ev == "end":
        return _end(session, e, bs)
    if ev == "wire_cut":
        bs.off = "bridge: wire cut (campaign), manual entry"
        return Result(bs.off)
    if ev != "item" and not (ev in _OUTCOME_EV and bs.pending):
        _flush_adrenaline(session, bs)
    if ev in _OUTCOME_EV:
        return _outcome(session, e, bs, ev)
    if bs.pending:
        return _pause(bs, f"{bs.pending[0].config_name} had no result before {ev}")
    if ev == "shot":
        _actor(session, e["actor"])
        session.record_shot(e["target"] == "self", _SHELL[e["shell"]])
        return Result("shot", applied=True)
    return _item(session, e, bs)


def _actor(session: Session, who: str) -> Player:
    seat = _SEAT[who]
    if session.state.turn != seat:
        mine = session.state.turn.name.lower()
        raise SessionError(f"{who} acted but the calc has it as {mine}'s turn")
    return seat


def _flush_adrenaline(session: Session, bs: BridgeState) -> None:
    """An Adrenaline with no stolen item before the next action timed out: it was wasted."""
    if bs.adrenaline:
        bs.adrenaline = False
        session.waste_adrenaline()


def _item(session: Session, e: dict, bs: BridgeState) -> Result:
    seat = _actor(session, e["actor"])
    item = Item.from_config_name(e["item"].replace(" ", "_"))
    stolen = bool(e.get("stolen"))
    if item is Item.ADRENALINE and not stolen:
        _flush_adrenaline(session, bs)
        bs.adrenaline = True
        return Result("adrenaline")
    if stolen and not bs.adrenaline:
        raise SessionError("stolen item without adrenaline")
    if not stolen:
        _flush_adrenaline(session, bs)
    bs.adrenaline = False
    if item in _WAITS and not (stolen and seat is Player.DEALER and item in _INFO):
        if seat is Player.DEALER and item in _INFO:  # its own glass/phone: answer is private
            if item is Item.MAGNIFYING_GLASS:
                session.record_dealer_info_action(0)
            else:
                session.record_dealer_phone()
            return Result("dealer info", applied=True)
        bs.pending = (item, seat, stolen)
        return Result(f"waiting for {item.config_name} result")
    if stolen and item in _INFO:  # dealer took our glass/phone: private, never our chamber
        session.record_dealer_steal_info(item)
    elif stolen:
        session.steal(item)
    elif item is Item.INVERTER:
        session.record_inverter()
    elif item in _PLAIN.values():
        session.use_item(item)
    else:
        raise SessionError(f"unhandled item {item.name}")
    return Result(item.config_name, applied=True)


def _outcome(session: Session, e: dict, bs: BridgeState, ev: str) -> Result:
    if bs.pending is None or bs.pending[0] is not _OUTCOME_EV[ev]:
        raise SessionError(f"{ev} result with no matching item use")
    item, seat, stolen = bs.pending
    bs.pending = None
    if seat is Player.DEALER and item in _INFO:
        raise SessionError(f"{ev} result for the dealer would leak its private information")
    shell = _SHELL[e["shell"]] if e.get("shell") else None
    slot = e.get("slot")
    if item is Item.EXPIRED_MEDICINE:
        healed = bool(e["healed"])
        session.steal(item, healed=healed) if stolen else session.record_expired_medicine(healed)
    elif stolen:
        session.steal(item, shell=shell, phone_slot=slot)
    elif item is Item.BEER:
        session.record_beer(shell)
    elif item is Item.MAGNIFYING_GLASS:
        session.record_glass(shell)
    else:
        session.record_phone(slot, shell)
    return Result(ev, applied=True)


def _load(session: Session, e: dict, bs: BridgeState) -> Result:
    items = (_items(e["items"]["player"]), _items(e["items"]["dealer"]))
    first = _SEAT[e.get("first_turn", "player")]
    if e["kind"] == "new_round":
        session.new_round(e["live"], e["blank"], max_hp=e["max_hp"], items=items, first_turn=first)
    elif e["kind"] == "reload":
        session.new_load(e["live"], e["blank"], items=items, first_turn=first)
    else:
        raise ValueError(f"load kind {e['kind']!r}")
    if session.state.max_hp != e["max_hp"]:
        session.edit_max_hp(e["max_hp"])
    for seat, hp in zip(Player, e["hp"], strict=True):  # a reload carries the game's HP
        if session.state.hp[seat] != hp:
            session.edit_hp(seat, hp)
    bs.paused = None
    bs.pending = None
    bs.adrenaline = False
    bs.ended = False
    return Result("load", applied=True)


def _end(session: Session, e: dict, bs: BridgeState) -> Result:
    if bs.ended:
        return Result("duplicate end ignored")
    bs.ended = True
    seat = _SEAT[e["who"]]
    if session.state.hp[seat] != 0:
        return _pause(bs, f"game says {e['who']} is down, calc has hp {session.state.hp[seat]}")
    return Result(f"round over: {e['who']} down")


def replay(session: Session, lines: Iterable[str], bs: BridgeState | None = None) -> list[Result]:
    """Feed recorded JSONL lines through the adapter (the `--replay` path)."""
    bs = bs or BridgeState()
    out = []
    for line in lines:
        if line.strip() and not line.startswith("#"):
            out.append(apply_event(session, _parse(line), bs))
    return out


def _parse(line: str | bytes) -> dict:
    try:
        return json.loads(line)
    except ValueError:
        return {}  # fails the version check: manual entry


async def serve(
    handle: Callable[[dict], Result],
    on_change: Callable[[], None],
    port: int = PORT,
    record: Path | None = None,
) -> None:
    """Listen on localhost only; every line goes to `handle`, replies and closes as it says."""
    rec = record.open("a", encoding="utf-8") if record else None

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async for raw in reader:
                if rec:
                    rec.write(raw.decode(errors="replace").rstrip("\n") + "\n")
                    rec.flush()
                res = handle(_parse(raw))
                if res.reply:
                    writer.write((json.dumps(res.reply) + "\n").encode())
                    await writer.drain()
                on_change()
                if res.close:
                    break
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(client, "127.0.0.1", port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        if rec:
            rec.close()
