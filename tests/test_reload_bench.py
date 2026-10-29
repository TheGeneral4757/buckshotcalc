"""Tests for buckshotcalc.sim.reload_bench (SPEC.md §7.7; M8.5).

The harness itself shipped without tests. These cover the one part of it
that can silently corrupt something outside the benchmark run: the `build`
subcommand writes to the reload table's on-disk cache, and that cache is
keyed by `Rules.fingerprint`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from buckshotcalc.engine.rules import default_rules
from buckshotcalc.engine.solver import ReloadProgress
from buckshotcalc.sim.reload_bench import (
    _build_rules,
    _fmt_eta,
    _ProgressPrinter,
    _rules_without_cache,
    header_line,
)


def test_build_rules_is_a_no_op_without_a_samples_override():
    rules = default_rules()
    assert _build_rules(rules, None) is rules


def test_build_rules_leaves_the_cache_enabled():
    """`build` exists to populate the cache — unlike the benchmark subcommands."""
    rules = default_rules()
    assert _build_rules(rules, 8).solver.reload_table_cache is True
    assert _rules_without_cache(rules, 8).solver.reload_table_cache is False


def test_build_rules_restamps_the_fingerprint_when_samples_change():
    """A sample-count override must not inherit the shipped file's cache entry.

    `Rules.fingerprint` is the sha256 of the `rules.toml` bytes, so
    `dataclasses.replace` alone would leave a low-accuracy table claiming to
    be the shipped one — and with the cache on, the TUI would load it and
    trust it. Same hazard `sim/corpus.py`'s `_corpus_rules` documents.
    """
    rules = default_rules()
    built = _build_rules(rules, 8)

    assert built.solver.reload_item_draw_samples == 8
    assert built.fingerprint != rules.fingerprint, (
        "a different sample count must not share the shipped rules file's "
        "cache entry"
    )
    assert _build_rules(rules, 16).fingerprint != built.fingerprint


def test_build_rules_does_not_mutate_the_original():
    rules = default_rules()
    shipped_samples = rules.solver.reload_item_draw_samples
    _build_rules(rules, 8)
    assert rules.solver.reload_item_draw_samples == shipped_samples


def _ev(phase, sweep, delta=None, done=29, stage=None, tol=1e-3):
    kw = dict(
        phase=phase, sweep=sweep, sweeps_max=12, keys_done=done, keys_total=29,
        elapsed_s=0.0, delta=delta, tolerance=tol,
    )  # fmt: skip
    if stage is None:
        return ReloadProgress(**kw)
    return SimpleNamespace(stage=stage, **kw)


def _printer(t, every=60.0):
    lines: list[str] = []
    return _ProgressPrinter(every, clock=lambda: t[0], emit=lines.append), lines


def test_sweep_rows_are_aligned_with_eta_from_the_second_sweep():
    t = [time.mktime((2026, 10, 24, 11, 36, 27, 0, 0, -1))]
    p, lines = _printer(t)
    t[0] += 95
    p(_ev("sweep", 1, 0.384))
    t[0] += 90
    p(_ev("sweep", 2, 0.169))
    assert lines[0] == "[11:38:02] sweep 1   1m 35s  residual 3.84e-01  ETA —"
    assert lines[1].startswith("[11:39:32] sweep 2   1m 30s  residual 1.69e-01  ETA ~")
    assert lines[1].endswith(")") and "ETA ~" in lines[1]


def test_in_sweep_rows_only_for_long_sweeps_and_throttled():
    t = [1_000_000.0]
    p, lines = _printer(t)
    t[0] += 30
    p(_ev("key", 1, done=5))
    assert lines == []  # short: silent
    t[0] += 35
    p(_ev("key", 1, done=15))
    t[0] += 10
    p(_ev("key", 1, done=16))  # throttled
    assert len(lines) == 1 and "sweep 1   15/29 keys  65s in" in lines[0]


def test_stage_subheader_and_per_stage_statistics():
    t = [1_000_000.0]
    p, lines = _printer(t)
    t[0] += 5
    p(_ev("sweep", 1, 0.5, stage="warm start (4 samples)"))
    t[0] += 5
    p(_ev("sweep", 2, 0.4, stage="warm start (4 samples)"))
    p(_ev("sweep", 1, 0.3))  # no stage attribute: "main"
    assert lines[0] == "── warm start (4 samples) ──"
    assert sum(ln.startswith("──") for ln in lines) == 2 and lines[-2] == "── main ──"
    assert "ETA —" in lines[-1]  # main restarted its decay history


def test_converged_summary_and_formatting_helpers():
    t = [1_000_000.0]
    p, lines = _printer(t)
    t[0] += 10
    p(_ev("sweep", 1, 1e-4))
    assert lines[0].endswith("ETA done")
    assert p.summary(125, "/x/y.json") == "converged  1 sweeps  2m 05s  cached /x/y.json"
    assert _fmt_eta(540) == "9m" and _fmt_eta(40) == "40s" and _fmt_eta(7500) == "2h 05m"
    assert header_line("reload build", "BASE", "29 keys").split("  ")[:3] == [
        "reload build", "BASE", "29 keys",
    ]  # fmt: skip
