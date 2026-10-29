# The reload table and the long builds

## What it is

When the chamber empties the game reloads with a fresh random set of shells and
items. The solver's value for "the round continues after a reload" depends on
itself, so it is computed once as a fixed point (`V_reload`) by repeated sweeps
over all health combinations, rather than during a query. That precompute is the
long job: about 35 min to 1 h on CPython using every core, or about 26 min under
PyPy on 16 cores. The result is a small JSON file (about 1 KB).

Without it, the calculator exits with "no reload table yet", and the TUI shows
`RELOAD TABLE NOT BUILT` and withholds answers that reach a reload.

## It is shipped, so most people never build it

`buckshotcalc/tables/` holds the Double or Nothing table for the current
`rules.toml` and engine. It is copied into your cache directory
(`~/.cache/buckshotcalc/`, or `$XDG_CACHE_HOME/buckshotcalc/`) automatically the
first time you run `buckshotcalc`, `buckshotcalc-tui` or `./start.sh`. Nothing to
do. To check: `buckshotcalc --live 3 --blank 2 --hp 4 4 --max-hp 4` answers
immediately.

Only Double or Nothing is shipped (v1's supported mode).

## What makes a table invalid

The file name carries two hashes, and a table is loaded only if both match:

| Change | Result |
| --- | --- |
| Edit `buckshotcalc/rules.toml` (any value) | Table ignored, rebuild needed |
| Change the code of `state`, `rules`, `actions`, `dealer`, `memo_key` or `solver` in `engine/` | Table ignored, rebuild needed |
| Edit only comments, docstrings or formatting in those files | Still valid |
| Change the TUI, `sim/`, the CLI, docs or tests | Still valid |
| A different `--mode` | A different table |

A stale table is never loaded, so a mismatch costs time, not correctness. An
older table for the same rules and mode is used as a starting guess to speed up
the new build.

## Rebuilding

```bash
buckshotcalc --build            # CPython, about 1 h, prints a sweep counter
./start.sh --build              # same, uses PyPy if installed, then starts the TUI
# fastest, if you have PyPy and many cores:
PYTHONPATH=$PWD pypy3 -m buckshotcalc.sim.reload_bench build --game-mode don
```

The result is cached and portable between interpreters and operating systems:
build under PyPy, use under CPython. Worker count is `solver.reload_workers` in
`rules.toml` (`-1` = all cores), overridable with `BUCKSHOTCALC_RELOAD_WORKERS`.

## For contributors: refreshing the shipped table

`tests/test_bundled.py` fails when `rules.toml` or the engine changed without
the shipped table being refreshed. To fix it:

1. `buckshotcalc --build`
2. Copy the new `reload_double_or_nothing_*.json` from the cache directory into
   `buckshotcalc/tables/`, and delete the old file there.
3. Re-run the tests.

## The validation run (Monte Carlo)

`python -m buckshotcalc.sim.montecarlo --mode don --positions 100 --trials 100000`
plays 100 random positions 100,000 times each and compares the simulated win rate
to the solver's prediction. It takes a long time and produces a report, not a
cache. It passes when no position differs by 0.01 or more.

The last full runs are saved in [`docs/acceptance/`](acceptance/) (worst error
0.0031 in both modes). **They were run on 2026-10-25 and the engine has changed
since**, so treat them as historical evidence, not proof about today's code. To
check current code, re-run the command above (needs the table).
