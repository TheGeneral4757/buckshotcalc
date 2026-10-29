"""Live-bridge adapter (docs/live-bridge.md): events -> Session actions."""

from __future__ import annotations

from pathlib import Path

import pytest

from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.state import GameMode, Item, Player, Shell
from buckshotcalc.tui import bridge
from buckshotcalc.tui.app import _default_state
from buckshotcalc.tui.bridge import BridgeState, apply_event
from buckshotcalc.tui.session import Session

FIXTURE = Path(__file__).parent / "fixtures" / "bridge_bot_run.jsonl"
_n = [0]


def ev(name: str, **kw) -> dict:
    _n[0] += 1
    return {"v": 1, "ev": name, "seq": _n[0], "t": 0, **kw}


def setup(live=3, blank=3, hp=4, player=None, dealer=None):
    _n[0] = 0
    r = default_rules()
    s = Session(r, GameMode.DOUBLE_OR_NOTHING, _default_state(r))
    bs = BridgeState()
    apply_event(s, ev("hello", mode="endless", scripts_sha=bridge.known_scripts_sha()), bs)
    res = apply_event(
        s,
        ev("load", kind="new_round", live=live, blank=blank, hp=[hp, hp], max_hp=hp,
           items={"player": player or {}, "dealer": dealer or {}}, first_turn="player"),
        bs,
    )  # fmt: skip
    assert res.applied
    return s, bs


def self_blank():
    return ev("shot", actor="player", target="self", shell="blank", damage=1)


def sync_for(s: Session, **over) -> dict:
    st = s.state
    items = {
        w: {i.config_name.replace("_", " "): st.items_of(p)[i] for i in Item if st.items_of(p)[i]}
        for w, p in (("player", Player.PLAYER), ("dealer", Player.DEALER))
    }
    d = dict(hp=list(st.hp), max_hp=st.max_hp, cuffed=[bool(c) for c in st.cuffed],
             sawed=st.sawed, left=len(st.chamber), turn=st.turn.name.lower(),
             items=items)  # fmt: skip
    d.update(over)
    return ev("sync", **d)


def test_hello_ack_known_and_unknown_hash():
    s, bs = setup()
    r = apply_event(s, ev("hello", mode="endless", scripts_sha="x"), bs)
    assert r.reply == {"v": 1, "ev": "hello_ack", "ok": True, "known": False}
    assert "unknown game scripts" in bs.banner()
    r = apply_event(s, ev("hello", mode="endless", scripts_sha=bridge.known_scripts_sha()), bs)
    assert r.reply["known"] is True and bs.banner() == ""


def test_campaign_refused_until_next_hello():
    s, bs = setup()
    apply_event(s, ev("hello", mode="campaign"), bs)
    assert "campaign not supported" in bs.banner()
    before = s.state
    assert not apply_event(s, self_blank(), bs).applied
    assert s.state == before
    apply_event(s, ev("hello", mode="endless"), bs)
    assert apply_event(s, self_blank(), bs).applied


def test_load_new_round_and_reload_carry_hp():
    s, bs = setup(live=2, blank=2, hp=4, player={"cigarettes": 1})
    assert s.state.items_of(Player.PLAYER)[Item.CIGARETTES] == 1
    apply_event(s, ev("shot", actor="player", target="opponent", shell="live", damage=1), bs)
    apply_event(s, ev("load", kind="reload", live=3, blank=1, hp=[4, 3], max_hp=4,
                      items={"player": {}, "dealer": {}}, first_turn="player"), bs)  # fmt: skip
    assert s.state.hp == (4, 3) and len(s.state.chamber) == 4 and s.state.turn is Player.PLAYER


def test_shot_blank_self_keeps_turn_live_passes_it():
    s, bs = setup()
    apply_event(s, self_blank(), bs)
    assert s.state.turn is Player.PLAYER
    apply_event(s, ev("shot", actor="player", target="opponent", shell="live", damage=1), bs)
    assert s.state.turn is Player.DEALER and s.state.hp == (4, 3)


def test_plain_items():
    s, bs = setup(player={"handcuffs": 1, "handsaw": 1, "cigarettes": 1})
    for name in ("handcuffs", "handsaw", "cigarettes"):
        assert apply_event(s, ev("item", actor="player", item=name, stolen=False), bs).applied
    assert s.state.cuffed[Player.DEALER] and s.state.sawed


def test_beer_waits_for_eject():
    s, bs = setup(player={"beer": 1})
    r = apply_event(s, ev("item", actor="player", item="beer", stolen=False), bs)
    assert not r.applied and s.state.items_of(Player.PLAYER)[Item.BEER] == 1
    assert apply_event(s, ev("eject", actor="player", cause="beer", shell="live"), bs).applied
    assert len(s.state.chamber) == 5 and s.state.items_of(Player.PLAYER)[Item.BEER] == 0


def test_glass_and_phone_player_resolve_chamber():
    s, bs = setup(player={"magnifying glass": 1, "burner phone": 1})
    apply_event(s, ev("item", actor="player", item="magnifying glass", stolen=False), bs)
    apply_event(s, ev("glass", shell="blank"), bs)
    assert s.state.chamber.slots[0] is Shell.BLANK
    apply_event(s, ev("item", actor="player", item="burner phone", stolen=False), bs)
    apply_event(s, ev("phone", slot=2, shell="live"), bs)
    assert s.state.chamber.slots[2] is Shell.LIVE and not s.state.dealer_knows


def test_phone_one_shell_left_is_spent_without_info():
    s, bs = setup(live=1, blank=0, player={"burner phone": 1})
    apply_event(s, ev("item", actor="player", item="burner phone", stolen=False), bs)
    assert apply_event(s, ev("phone", slot=None, shell=None), bs).applied
    assert s.state.items_of(Player.PLAYER)[Item.BURNER_PHONE] == 0


def test_medicine_and_inverter():
    s, bs = setup(hp=4, player={"expired medicine": 1, "inverter": 1})
    s.edit_hp(Player.PLAYER, 2)
    apply_event(s, ev("item", actor="player", item="expired medicine", stolen=False), bs)
    apply_event(s, ev("medicine", actor="player", healed=True), bs)
    assert s.state.hp[Player.PLAYER] == 4
    apply_event(s, ev("item", actor="player", item="inverter", stolen=False), bs)
    assert s.state.chamber.inverted0 and s.state.items_of(Player.PLAYER)[Item.INVERTER] == 0


def test_dealer_own_glass_and_phone_are_private():
    s, bs = setup(dealer={"magnifying glass": 1, "burner phone": 1})
    s.edit_turn(Player.DEALER)
    apply_event(s, ev("item", actor="dealer", item="magnifying glass", stolen=False), bs)
    assert 0 in s.state.dealer_knows and s.state.chamber.slots[0] is None
    apply_event(s, ev("item", actor="dealer", item="burner phone", stolen=False), bs)
    assert len(s.state.dealer_knows) == 2 and all(x is None for x in s.state.chamber.slots)


@pytest.mark.parametrize("item", ["magnifying glass", "burner phone"])
def test_dealer_steal_of_our_info_item_routes_to_dealer_knowledge(item):
    s, bs = setup(player={item: 1}, dealer={"adrenaline": 1})
    s.edit_turn(Player.DEALER)
    apply_event(s, ev("item", actor="dealer", item="adrenaline", stolen=False), bs)
    r = apply_event(s, ev("item", actor="dealer", item=item, stolen=True), bs)
    assert r.applied and not bs.paused
    assert s.state.dealer_knows and all(x is None for x in s.state.chamber.slots)
    assert s.state.items_of(Player.PLAYER)[Item(Item.from_config_name(item.replace(" ", "_")))] == 0
    assert s.state.items_of(Player.DEALER)[Item.ADRENALINE] == 0
    assert not apply_event(s, ev("glass", shell="live"), bs).applied  # no outcome is ever ours
    assert all(x is None for x in s.state.chamber.slots)


def test_player_steal_beer_waits_then_applies():
    s, bs = setup(player={"adrenaline": 1}, dealer={"beer": 1})
    apply_event(s, ev("item", actor="player", item="adrenaline", stolen=False), bs)
    apply_event(s, ev("item", actor="player", item="beer", stolen=True), bs)
    apply_event(s, ev("eject", actor="player", cause="beer", shell="blank"), bs)
    assert s.state.items_of(Player.DEALER)[Item.BEER] == 0 and len(s.state.chamber) == 5


def test_mismatch_pauses_and_accept_resumes():
    s, bs = setup()
    r = apply_event(s, sync_for(s, hp=[3, 4]), bs)
    assert bs.paused and "player hp: calc 4, game 3" in bs.paused and not r.applied
    before = s.state
    r = apply_event(s, self_blank(), bs)
    assert s.state == before  # ignored while paused
    bridge.accept_game_state(s, bs)
    assert s.state.hp == (3, 4) and not bs.paused
    assert apply_event(s, self_blank(), bs).applied


def test_matching_sync_is_quiet_and_repeat_sync_is_idempotent():
    s, bs = setup()
    n = len(s.log)
    for _ in range(2):
        e = sync_for(s)
        apply_event(s, e, bs)
        apply_event(s, dict(e), bs)  # same seq again
    assert not bs.paused and len(s.log) == n


def test_gap_pauses_until_accept():
    s, bs = setup()
    apply_event(s, ev("gap", dropped=3), bs)
    assert "3 events were dropped" in bs.paused
    apply_event(s, sync_for(s), bs)  # agreeing sync resumes
    assert not bs.paused


def test_seq_jump_pauses():
    s, bs = setup()
    _n[0] += 5
    apply_event(s, ev("turn", who="player", cuffed_skip=False), bs)
    assert "missing" in bs.paused


def test_seq_gap_of_one_pauses():
    s, bs = setup()
    _n[0] += 1
    apply_event(s, ev("turn", who="player", cuffed_skip=False), bs)
    assert "missing" in bs.paused


def test_end_with_hp_mismatch_pauses():
    s, bs = setup(hp=3)
    apply_event(s, ev("end", who="dealer"), bs)
    assert "game says dealer is down" in bs.paused


def test_duplicate_end_is_reported_ignored():
    s, bs = setup(hp=1)
    apply_event(s, ev("shot", actor="player", target="opponent", shell="live", damage=1), bs)
    apply_event(s, ev("end", who="dealer"), bs)
    res = apply_event(s, ev("end", who="dealer"), bs)
    assert res.status == "duplicate end ignored"


def test_malformed_item_map_pauses_instead_of_raising():
    s, bs = setup()
    res = apply_event(
        s,
        ev("load", kind="reload", live=1, blank=1, hp=[3, 3], max_hp=3,
           items={"player": [], "dealer": []}),
        bs,
    )  # fmt: skip
    assert not res.applied and bs.paused


def test_duplicate_turn_and_end_are_noops():
    s, bs = setup(hp=1)
    apply_event(s, ev("shot", actor="player", target="opponent", shell="live", damage=1), bs)
    for _ in range(2):
        apply_event(s, ev("turn", who="dealer", cuffed_skip=False), bs)
    log = len(s.log)
    apply_event(s, ev("end", who="dealer"), bs)
    apply_event(s, ev("end", who="dealer"), bs)
    assert len(s.log) == log and not bs.paused


def test_unknown_event_and_bad_version_drop_to_manual():
    s, bs = setup()
    apply_event(s, ev("mystery"), bs)
    assert "manual entry" in bs.banner()
    apply_event(s, ev("hello", mode="endless"), bs)
    r = apply_event(s, {"v": 2, "ev": "shot"}, bs)
    assert r.close and r.reply["ok"] is False and bs.off


def test_undo_of_bridge_event():
    s, bs = setup()
    before = s.state
    apply_event(s, ev("shot", actor="player", target="opponent", shell="live", damage=1), bs)
    s.undo()
    assert s.state == before


def test_adrenaline_with_nothing_to_steal_is_wasted_on_the_next_action():
    s, bs = setup(player={"adrenaline": 1, "beer": 1})
    apply_event(s, ev("item", actor="player", item="adrenaline", stolen=False), bs)
    res = apply_event(s, ev("item", actor="player", item="beer", stolen=False), bs)
    assert not bs.paused and bs.pending is not None
    assert s.state.items_of(Player.PLAYER)[Item.ADRENALINE] == 0
    assert "waiting" in res.status


def _replay(lines):
    r = default_rules()
    s = Session(r, GameMode.DOUBLE_OR_NOTHING, _default_state(r))
    res = bridge.replay(s, lines)
    seqs = [__import__("json").loads(ln)["seq"] for ln in lines]
    firsts, prev = [], False
    for q, x in zip(seqs, res, strict=True):
        now = x.status.startswith("bridge paused")
        if now and not prev:
            firsts.append(q)
        prev = now or (prev and x.status.startswith("ignored while paused"))
    return res, firsts


def test_replay_recorded_game():
    lines = FIXTURE.read_text().splitlines()
    res, firsts = _replay(lines)
    assert len(res) == len(lines) and sum(x.applied for x in res) > 40
    # Seq 43 is an Adrenaline on an EMPTY dealer table: the game spends it for nothing, and the
    # adapter records that as a waste when the next action arrives, so nothing pauses.
    assert firsts == []


@pytest.mark.parametrize("name", ["bridge_bot_run2", "bridge_bot_run3"])
def test_replay_more_recorded_games(name):
    # Recorded after the bot stopped using items out of turn: whole rounds, with steals and phones.
    lines = (FIXTURE.parent / f"{name}.jsonl").read_text().splitlines()
    res, firsts = _replay(lines)
    assert len(res) == len(lines) and firsts == []


def test_replay_from_the_last_clean_load_never_pauses():
    lines = FIXTURE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if '"seq":81,' in ln.replace(" ", ""))
    _, firsts = _replay(lines[start:])
    assert firsts == []


def test_serve_acks_hello_and_records(tmp_path):
    import asyncio
    import json
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    s, bs = setup()
    rec = tmp_path / "rec.jsonl"
    line = json.dumps({"v": 1, "ev": "hello", "seq": 1, "mode": "endless"}) + "\n"

    async def run():
        task = asyncio.create_task(
            bridge.serve(lambda e: apply_event(s, e, bs), lambda: None, port, rec)
        )
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(line.encode())
        ack = json.loads(await asyncio.wait_for(r.readline(), 2))
        w.close()
        task.cancel()
        return ack

    ack = asyncio.run(run())
    assert ack["ev"] == "hello_ack" and ack["ok"] and rec.read_text() == line


def test_shot_on_an_empty_chamber_pauses_instead_of_raising():
    s, bs = setup(live=0, blank=1)
    apply_event(s, ev("shot", actor="player", target="self", shell="blank", damage=1), bs)
    res = apply_event(s, ev("shot", actor="player", target="self", shell="blank", damage=1), bs)
    assert bs.paused and res.status.startswith("bridge paused")
