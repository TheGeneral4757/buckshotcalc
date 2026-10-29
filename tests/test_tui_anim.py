from __future__ import annotations

import pytest

from buckshotcalc.tui import anim


@pytest.mark.parametrize("code", ["N1", "N2", "N3", "N4", "N5"])
@pytest.mark.parametrize("frm,to", [(55.3, 72.4), (72.4, 55.3), (9.9, 100.0)])
def test_tween_start_and_end(code, frm, to):
    s = anim.number_tween(code, frm, to, 0)
    assert s.text == anim.fmt_pct(frm) and not s.done
    e = anim.number_tween(code, frm, to, 3.0)
    assert e.text == anim.fmt_pct(to) and e.done and e.role == "acc"
    # monotone completion: once done, stays done
    assert anim.number_tween(code, frm, to, 10.0) == e


@pytest.mark.parametrize("code", ["N1", "N2", "N3", "N4", "N5"])
def test_tween_durations_in_range(code):
    t = next(i / 100 for i in range(400) if anim.number_tween(code, 40, 60, i / 100).done)
    assert 0.5 <= t <= 1.5


def test_n3_deterministic_and_locks_left_to_right():
    a = anim.number_tween("N3", 55.3, 72.4, 0.2)
    assert a == anim.number_tween("N3", 55.3, 72.4, 0.2)
    late = anim.number_tween("N3", 55.3, 72.4, 0.45)
    assert late.text[0] == "7" and late.text[-1] == "%"


def test_n4_rows():
    fr = anim.number_tween("N4", 55.3, 72.4, 0.1)
    rows = anim.number_rows(fr)
    assert len(rows) == 5 and rows[0][1] == fr.role and rows[-1][1] == "dim"


def test_big_has_five_rows_and_percent():
    rows = anim.big("72.4%")
    assert len(rows) == 5 and len({len(r) for r in rows}) == 1


def test_motion_off_gives_end_frames_at_t0():
    assert anim.number_tween("N3", 1, 2, 0, "off").done
    assert anim.number_tween("N4", 1, 2, 0, "off").text == anim.fmt_pct(2)
    s = anim.shell_strip("L", 4, 2, 2, 1, 2, 0, "off")
    assert s.done and len(s.slots) == 3
    assert anim.typewriter("hello", 0, mode="off") == ("hello", True)
    assert anim.turn_wipe(0, 40, "off") == (40, True)
    assert anim.steal_offset(0, 10, "off") == (0, True)


def test_reduced_drops_flicker():
    assert all(anim.pulse_phase(x / 10, "reduced") == "bright" for x in range(40))
    assert all(anim.heartbeat(x / 10, 1, "reduced") for x in range(40))
    line = "┌" + "─" * 30 + "┐"
    assert all(anim.static(line, x / 10, 1, "reduced") == line for x in range(40))
    assert all(anim.stamp_visible(x / 10, "reduced") for x in range(10))
    assert anim.number_tween("N2", 1, 2, 0.5, "reduced").done


def test_pulse_and_heartbeat_vary_in_full():
    assert {anim.pulse_phase(x / 10) for x in range(32)} == {"bright", "dim"}
    assert {anim.heartbeat(x / 10, 1) for x in range(20)} == {True, False}
    assert all(anim.heartbeat(x / 10, 3) for x in range(20))


def test_shell_strip_reveals_then_drains():
    s0 = anim.shell_strip("L", 4, 2, 2, 1, 2, 0.0)
    assert s0.slots[0] == ("L", "live") and len(s0.slots) == 4 and s0.live_frac == 0.5
    mid = anim.shell_strip("L", 4, 2, 2, 1, 2, 0.7)
    assert 1 / 3 < mid.live_frac < 0.5 and len(mid.slots) == 3
    end = anim.shell_strip("L", 4, 2, 2, 1, 2, 2.0)
    assert end.done and end.live_frac == pytest.approx(1 / 3)
    assert len(end.bar) == 14


def test_smoother_monotone_capped_and_completes():
    sm = anim.ProgressSmoother()
    total, prev = 7, 0.0
    finish = [0.4, 0.7, 2.6, 3.0, 3.3, 3.5, 4.2]
    t = 0.0
    while t < 6:
        t += 0.05
        done = sum(1 for x in finish if x <= t)
        v = sm.step(done, total, 0.05)
        assert v >= prev
        if done < total:
            assert v <= (done + 0.9) / total + 1e-9
        prev = v
    assert v == 1.0


def test_smoother_creeps_while_waiting():
    sm = anim.ProgressSmoother()
    vals = [sm.step(0, 5, 0.05) for _ in range(40)]
    assert vals[-1] > vals[0] and vals[-1] <= 0.9 / 5 + 1e-9


def test_p2_bar():
    runs = anim.p2_bar(0.5, 4)
    text = "".join(r for r, _ in runs)
    assert text.count("│") == 3 and len(text) == 4 * 4 + 3
    assert anim.p2_bar(1.0, 3)[0][1] == "acc"


def test_crack_deterministic_and_rate_rises_with_lower_hp():
    frame = ["┌" + "─" * 400 + "┐", ("│" + " " * 400 + "│")] * 10
    assert anim.crack(frame, 4) == frame
    assert anim.crack(frame, 1) == anim.crack(frame, 1)

    def damage(hp):
        out = anim.crack(frame, hp)
        pairs = ((a, b) for x, y in zip(frame, out, strict=True) for a, b in zip(x, y, strict=True))
        return sum(a != b for a, b in pairs)

    assert 0 < damage(3) < damage(2) < damage(1)


def test_static_only_touches_frame_chars():
    line = "┌─ 72.4% ──────────┐"
    seen = {anim.static(line, x / 10, 3) for x in range(30)}
    assert any(s != line for s in seen)
    for s in seen:
        assert s[0] == "┌" and s[-1] == "┐" and "72.4%" in s


def test_delta_badge_fades_over_4s():
    txt, role, lvl, done = anim.delta_badge(6.1, 0.0)
    assert (txt, role, lvl, done) == ("+6.1", "good", 3, False)
    assert anim.delta_badge(-3.0, 1.5)[1] == "bad"
    assert anim.delta_badge(6.1, 3.9)[2] < 3
    assert anim.delta_badge(6.1, 4.0)[3]


def test_typewriter():
    assert anim.typewriter("abcdef", 0.05, cps=60) == ("abc", False)
    assert anim.typewriter("abc", 1.0) == ("abc", True)


def test_small_effects_end_states():
    assert anim.pip_loss(1.0) == (False, True)
    assert anim.pip_gain(1.0) == (3, True)
    assert anim.item_fade(0.1) == (4, False) and anim.item_fade(2.0) == (0, True)
    assert anim.steal_offset(0.0, 10)[0] == 10 and anim.steal_offset(1.0, 10)[1]
    assert anim.turn_wipe(0.25, 40) == (20, False)
    assert anim.stamp_visible(1.0) and not anim.stamp_visible(0.0)
