"""Benchmark harness for the reload precompute (SPEC.md §7.4, §7.7; M8.5).

Pure stdlib, like the rest of `buckshotcalc/sim/` (SPEC.md §0.1 non-negotiable
#3). Not a library module — a `main()` harness (SPEC.md §0.1's "no `print` in
engine or sim library code; harnesses get a `main()`" rule), so it is the one
place in `sim/` allowed to write to stdout.

Measures exactly the numbers SPEC.md §7.7's baseline table reports, so the
milestone's "before/after" comparison is apples to apples:

- one table key, one sweep, at the configured sample count: wall time and
  memo entries;
- a full sweep across every `(hp_player, hp_dealer, max_hp)` key: wall time,
  peak resident memory (self + terminated worker children), and total nodes
  evaluated (memo entries summed across every key/sweep visited).

Run (defaults match `rules.toml`'s shipped values — §7.7's baseline):

    .venv/bin/python -m buckshotcalc.sim.reload_bench build --game-mode don
    .venv/bin/python -m buckshotcalc.sim.reload_bench single
    .venv/bin/python -m buckshotcalc.sim.reload_bench full --workers 0   # serial
    .venv/bin/python -m buckshotcalc.sim.reload_bench full --workers 16  # parallel
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import platform

try:
    import resource
except ImportError:  # Windows has no `resource`; peak memory is then reported as 0
    resource = None
import time
from collections.abc import Callable

from buckshotcalc.engine.rules import Rules, default_rules
from buckshotcalc.engine.solver import Solver, _cache_path, _shell_configs
from buckshotcalc.engine.state import GameMode

_MODE_BY_NAME = {"base": GameMode.BASE, "don": GameMode.DOUBLE_OR_NOTHING}


def _rules_without_cache(rules: Rules, samples: int | None) -> Rules:
    """A copy of `rules` with the disk cache disabled (so a benchmark run
    always does real work, never reads a stale cached table) and,
    optionally, a different `reload_item_draw_samples` — the accuracy/cost
    dial SPEC.md §7.7 discusses, left at the shipped default unless `--samples`
    overrides it. Built via `dataclasses.replace`, not by touching
    `rules.toml` or `engine/rules.py` — this milestone's isolation note
    reserves both for a concurrent agent.
    """
    solver_cfg = rules.solver
    changes: dict[str, object] = {"reload_table_cache": False}
    if samples is not None:
        changes["reload_item_draw_samples"] = samples
    solver_cfg = dataclasses.replace(solver_cfg, **changes)
    return dataclasses.replace(rules, solver=solver_cfg)


def _fmt_duration(seconds: float) -> str:
    """`3h 07m` / `12m 30s` / `45s` — never a bare float of seconds."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 90:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _fmt_eta(seconds: float) -> str:
    """Remaining time rounded for an ETA: `45s` / `9m` / `2h 05m`."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _hms(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def eta_text(remaining_s: float, now: float) -> str:
    """`~9m (11:47)`: the remaining duration plus the wall-clock finish time."""
    return f"~{_fmt_eta(remaining_s)} ({time.strftime('%H:%M', time.localtime(now + remaining_s))})"


def header_line(title: str, *fields: str) -> str:
    """`reload build  BASE  29 keys  ...  CPython`: two spaces between fields."""
    return "  ".join([title, *fields, platform.python_implementation()])


class _ProgressPrinter:
    """Turns `ReloadProgress` facts into an aligned, scrolling build log.

    One row per finished sweep, plus (for sweeps longer than `every`
    seconds) an in-sweep row at most once per `every` seconds. Timestamps and
    durations come from `clock`, not from `elapsed_s`: a warm-start build may
    restart that counter, and this keeps every row correct either way.

    Two independent unknowns have to be estimated, and they are estimated
    differently because they behave differently:

    1. **Time left in this sweep** — keys within a sweep cost roughly the
       same, so elapsed-per-key extrapolates honestly.
    2. **How many sweeps remain** — this is the one that made the old
       "estimated 30 minutes to several hours" useless. Jacobi iteration on
       a contraction converges linearly: the residual falls by roughly a
       constant factor `r` each sweep. Measuring `r` from consecutive
       residuals gives `sweeps_left = log(tol / delta) / log(r)`. It needs
       two sweeps before it can say anything, and it is an estimate of a
       decay rate, not a promise — so it is labelled as one, and a residual
       that stops falling (`r >= 1`) reports "unknown" rather than a
       comforting number.

    Events carrying a non-"main" `stage` (a preliminary coarse build) get a
    sub-header row when the stage changes and their own sweep statistics.
    """

    def __init__(
        self,
        every: float = 60.0,
        *,
        clock: Callable[[], float] = time.time,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._every = every
        self._clock = clock
        self._emit = emit or (lambda line: print(line, flush=True))
        self._stage = "main"
        self._mark = clock()  # when the current sweep started
        self._last_print = self._mark
        self._durations: list[float] = []
        self._deltas: list[float] = []
        self.sweeps = 0  # sweeps run in the final stage
        self.converged = False

    def _sweeps_left(self, delta: float, tolerance: float) -> float | None:
        if delta < tolerance:
            return 0.0
        if len(self._deltas) < 2:
            return None
        prev, cur = self._deltas[-2], self._deltas[-1]
        if prev <= 0 or cur <= 0 or cur >= prev:
            return None  # not converging (yet): refuse to guess
        return math.log(tolerance / cur) / math.log(cur / prev)

    def _row(self, now: float, sweep: int, middle: str, tail: str) -> None:
        self._emit(f"[{_hms(now)}] sweep {sweep:<3} {middle}  {tail}")

    def __call__(self, p) -> None:
        now = self._clock()
        stage = getattr(p, "stage", "main")
        if stage != self._stage:
            self._stage = stage
            self._durations, self._deltas = [], []
            self._mark = self._last_print = now
            self._emit(f"── {stage} ──")

        if p.phase == "key":
            if now - self._last_print < self._every:
                return
            self._last_print = now
            keys = f"{p.keys_done}/{p.keys_total} keys"
            self._row(now, p.sweep, f"{keys:>10}", f"{_fmt_duration(now - self._mark)} in")
            return

        # phase == "sweep"
        self.sweeps = p.sweep
        self.converged = p.delta < p.tolerance
        self._durations.append(now - self._mark)
        self._deltas.append(p.delta)
        left = self._sweeps_left(p.delta, p.tolerance)
        if left is None:
            eta = "—"
        elif left <= 0:
            eta = "done"
        else:
            capped = min(left, p.sweeps_max - p.sweep)
            eta = eta_text(capped * sum(self._durations) / len(self._durations), now)
            if left > p.sweeps_max - p.sweep:
                eta += f" at the {p.sweeps_max}-sweep cap"
        self._row(
            now,
            p.sweep,
            f"{_fmt_duration(self._durations[-1]):>6}",
            f"residual {p.delta:.2e}  ETA {eta}",
        )
        self._mark = self._last_print = now

    def summary(self, total_s: float, cached: str | None) -> str:
        """The closing line: converged or not, sweeps, total time, cache path."""
        state = "converged" if self.converged else "NOT converged"
        return f"{state}  {self.sweeps} sweeps  {_fmt_duration(total_s)}  cached {cached or 'no'}"


def _build_rules(rules: Rules, samples: int | None) -> Rules:
    """`rules` for the `build` subcommand: cache left ON, fingerprint honest.

    The counterpart to `_rules_without_cache`. `build` exists to leave a
    cached table on disk, so unlike the benchmark subcommands it must not
    disable the cache — which makes the fingerprint load-bearing here in a
    way it is nowhere else in this module.

    `Rules.fingerprint` is the sha256 of the `rules.toml` *bytes*, so a
    `dataclasses.replace` produces an object whose fingerprint names a file
    it no longer matches. With the cache on, an unstamped
    `build --samples 8` would write a low-accuracy table into the *shipped*
    rules file's cache entry, and the TUI would then load it and trust it.
    Re-stamping follows `sim/corpus.py`'s `_corpus_rules`, which found the
    same hazard while the cache happened to be off.
    """
    if samples is None:
        return rules
    return dataclasses.replace(
        rules,
        solver=dataclasses.replace(rules.solver, reload_item_draw_samples=samples),
        fingerprint=f"{rules.fingerprint}+samples{samples}",
    )


def _reload_keys(rules: Rules) -> list[tuple[int, int, int]]:
    return [
        (hp_p, hp_d, max_hp)
        for max_hp in rules.health.possible_max_hp
        for hp_p in range(1, max_hp + 1)
        for hp_d in range(1, max_hp + 1)
    ]


def _peak_rss_mb() -> float:
    """Peak resident set size in MB, self plus every terminated child process.

    `resource.getrusage` is stdlib and available on CPython and PyPy alike
    (no CPython-only construct — see the milestone report's PyPy note).
    `ru_maxrss` is kilobytes on Linux (this machine); it is bytes on macOS,
    which this function does not correct for since the target environment is
    Linux. `RUSAGE_CHILDREN` only reflects processes that have already been
    waited on, which is why every measurement below reads it *after* a
    `multiprocessing.Pool`'s `with` block has exited (workers joined).
    """
    if resource is None:
        return 0.0
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(self_kb, children_kb) / 1024.0


def bench_single_key(
    rules: Rules, mode: GameMode, key: tuple[int, int, int]
) -> tuple[float, int]:
    """Wall time (s) and memo entries for one table key, one sweep, from the `v0` heuristic.

    Mirrors exactly one iteration of one key from
    `Solver._reload_sweeps_serial`'s inner loop — same deterministic sample
    draw, same `_expectimax` call sequence — so this is the "one table key,
    one sweep" row of SPEC.md §7.7's baseline table, measured against the
    current (parallel-capable, integer-keyed-memo) implementation.
    """
    solver = Solver(rules, mode)
    configs = _shell_configs(rules.shells)
    hp_p, hp_d, max_hp = key
    draws_per_config = solver._draw_samples(hp_p, hp_d, max_hp, configs)
    v0 = {k: k[0] / (k[0] + k[1]) for k in _reload_keys(rules)}

    def reload_fn(hpp: int, hpd: int, mhp: int, _v: dict = v0) -> float:
        return _v[(hpp, hpd, mhp)]

    memo: dict[int, tuple[float, bool]] = {}
    t0 = time.perf_counter()
    total = 0.0
    for (w_cfg, live, blank), draws in zip(configs, draws_per_config, strict=True):
        s = 0.0
        for items_p, items_d in draws:
            state = solver._build_reload_state(hp_p, hp_d, max_hp, live, blank, items_p, items_d)
            val, _ = solver._expectimax(state, memo, reload_fn, count_nodes=False)
            s += val
        total += w_cfg * (s / len(draws))
    elapsed = time.perf_counter() - t0
    return elapsed, len(memo)


def bench_full_sweep(
    rules: Rules, mode: GameMode, workers: int
) -> tuple[float, float, dict[tuple[int, int, int], float], int | None]:
    """Wall time (s), peak RSS (MB), the finished table, and total nodes
    (parallel only; `None` for serial, since the serial path never counts
    across keys — its memo size is read directly by the caller instead) for
    a full sweep across every table key.

    `workers <= 0` forces the serial path regardless of the environment
    override `Solver._reload_workers` would otherwise read, so `full
    --workers 0` and `full --workers N` are directly comparable without the
    caller having to fight `BUCKSHOTCALC_RELOAD_WORKERS` in its own shell.
    """
    os.environ["BUCKSHOTCALC_RELOAD_WORKERS"] = str(max(workers, 0))
    solver = Solver(rules, mode)
    t0 = time.perf_counter()
    table = solver._compute_reload_table()
    elapsed = time.perf_counter() - t0
    peak_mb = _peak_rss_mb()
    return elapsed, peak_mb, table, solver._last_parallel_node_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["single", "full", "build"])
    parser.add_argument("--game-mode", choices=sorted(_MODE_BY_NAME), default="base")
    parser.add_argument(
        "--key",
        default=None,
        help="hp_player,hp_dealer,max_hp for `single` (default: the first table key)",
    )
    parser.add_argument("--workers", type=int, default=None, help="for `full`; 0 = serial")
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="override reload_item_draw_samples (default: rules.toml's shipped value)",
    )
    args = parser.parse_args()

    game_mode = _MODE_BY_NAME[args.game_mode]

    if args.mode == "build":
        # The one subcommand that must NOT go through `_rules_without_cache`:
        # `single` and `full` benchmark, so they always do real work, but
        # `build` exists precisely to leave a cached table on disk for the
        # TUI and `sim/montecarlo.py` to find (SPEC.md §7.4). Honours
        # `--samples`, since that changes the fingerprint and so writes a
        # different cache entry.
        rules = _build_rules(default_rules(), args.samples)
        if not rules.solver.reload_table_cache:
            raise SystemExit(
                "build: solver.reload_table_cache is false in rules.toml, so "
                "there is nowhere to build to. Enable it, or use `full` to "
                "benchmark without caching."
            )
        solver = Solver(rules, game_mode)
        if solver.has_cached_reload_table():
            print(f"already cached: mode={game_mode.name} fingerprint={rules.fingerprint}")
            return
        workers = args.workers if args.workers is not None else solver._reload_workers()
        print(
            header_line(
                "reload build",
                game_mode.name,
                f"{len(_reload_keys(rules))} keys",
                f"{rules.solver.reload_item_draw_samples} samples",
                f"{workers} workers",
            ),
            flush=True,
        )
        printer = _ProgressPrinter()
        start = time.perf_counter()
        solver.build_reload_table(progress=printer)
        cached = _cache_path(rules, game_mode)
        where = str(cached) if solver.has_cached_reload_table() else None
        print(printer.summary(time.perf_counter() - start, where))
        print(f"peak_rss_mb={_peak_rss_mb():.1f}")
        return

    rules = _rules_without_cache(default_rules(), args.samples)

    if args.mode == "single":
        keys = _reload_keys(rules)
        if args.key:
            hp_p, hp_d, max_hp = (int(x) for x in args.key.split(","))
            key = (hp_p, hp_d, max_hp)
        else:
            key = keys[0]
        elapsed, memo_size = bench_single_key(rules, game_mode, key)
        print(f"key={key} samples={rules.solver.reload_item_draw_samples}")
        print(f"wall_time_s={elapsed:.3f}")
        print(f"memo_entries={memo_size}")
        print(f"peak_rss_mb={_peak_rss_mb():.1f}")
        return

    if args.workers is not None:
        workers = args.workers
    else:
        workers = Solver(rules, game_mode)._reload_workers()
    elapsed, peak_mb, table, node_count = bench_full_sweep(rules, game_mode, workers)
    print(
        header_line(
            "reload full",
            game_mode.name,
            f"{len(table)} keys",
            f"{rules.solver.reload_item_draw_samples} samples",
            f"{workers} workers",
        )
    )
    print(f"wall_time_s={elapsed:.3f}")
    print(f"peak_rss_mb={peak_mb:.1f}")
    if node_count is not None:
        print(f"total_nodes_evaluated={node_count}")


if __name__ == "__main__":
    main()
