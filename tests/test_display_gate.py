"""Display gating (docs/live-bridge.md section 6, rule 5): reveal offsets hold the screen."""

from __future__ import annotations

import pytest

from buckshotcalc.tui import timing
from buckshotcalc.tui.bridge import BridgeState, apply_event, display_released
from tests.test_bridge import ev, setup

CUFFS = {"actor": "player", "item": "handcuffs", "stolen": False}


def session():
    s, bs = setup(player={"handsaw": 1, "handcuffs": 1})
    bs.release_at = 0.0  # setup() applied its events at real time
    return s, bs


def test_handcuffs_gate_one_second():
    s, bs = session()
    apply_event(s, ev("item", **CUFFS), bs, now=10.0)
    assert not display_released(bs, 10.9)
    assert display_released(bs, 11.0)


def test_later_event_extends_gate():
    s, bs = session()
    apply_event(s, ev("item", **CUFFS), bs, now=10.0)
    apply_event(s, ev("item", actor="player", item="handsaw", stolen=False), bs, now=10.5)
    assert not display_released(bs, 11.9)
    assert display_released(bs, 12.0)


def test_live_shot_waits_blank_does_not():
    s, bs = session()
    apply_event(s, ev("shot", actor="player", target="self", shell="blank", damage=1), bs, now=1.0)
    assert display_released(bs, 1.0)
    live = ev("shot", actor="player", target="opponent", shell="live", damage=1)
    apply_event(s, live, bs, now=2.0)
    assert not display_released(bs, 4.9)
    assert display_released(bs, 5.0)


def test_never_gated_without_seq():
    s, bs = session()
    e = ev("item", **CUFFS)
    del e["seq"]
    apply_event(s, e, bs, now=10.0)
    assert display_released(bs, 10.0)
    assert display_released(BridgeState(), 0.0)


def test_scale_zero_disables(monkeypatch):
    monkeypatch.setattr(timing, "REVEAL_SCALE", 0.0)
    s, bs = session()
    apply_event(s, ev("item", **CUFFS), bs, now=10.0)
    assert display_released(bs, 10.0)


@pytest.mark.parametrize("scale", [0.5, 2.0])
def test_scale_stretches(monkeypatch, scale):
    monkeypatch.setattr(timing, "REVEAL_SCALE", scale)
    s, bs = session()
    apply_event(s, ev("item", **CUFFS), bs, now=10.0)
    assert display_released(bs, 10.0 + scale)
    assert not display_released(bs, 10.0 + scale - 0.01)
