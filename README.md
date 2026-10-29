# BuckshotCalc

An exact expectimax solver for Buckshot Roulette, with an optional live TUI.
MIT licensed.

> **Unmaintained, AI-written, use at your own risk.** All of this code was written
> by AI (Claude Code) under the direction of James Thiessen, and the repo is
> published as-is. It may not be maintained. See [Notices](#notices).

A real-time decision assistant for **Buckshot Roulette**. Feed it what happens at
the table; it returns the play with the highest probability of winning the round,
ranked against every alternative, with the reasoning behind it.

Not a shell-odds calculator — an exact game-tree solver. Anyone can compute
`live / (live + blank)`. The value is in the decisions that ratio doesn't answer:
whether an information item is worth spending, whether a marginal gamble is
correct at your current health, and when the Dealer's own policy will beat it for
you.

## Install

Python >= 3.11. The solver itself has **no dependencies**; the TUI is optional.

| You want | Install | Run |
| --- | --- | --- |
| Just the calculator (no TUI, stdlib only) | `pip install .` | `buckshotcalc --live 3 --blank 2 --hp 4 4 --max-hp 4 --items beer=1` |
| Calculator as a library | `pip install .` | `from buckshotcalc.engine.solver import Solver` |
| Calculator and TUI | `pip install ".[tui]"` | `buckshotcalc-tui` |

```bash
git clone https://github.com/TheGeneral4757/buckshotcalc
cd buckshotcalc
python -m venv .venv && .venv/bin/pip install ".[tui]"   # or "." for calc-only
.venv/bin/buckshotcalc-tui                                 # the TUI
.venv/bin/buckshotcalc --help                              # the headless calc
```

Calc-only works by simply not installing the `[tui]` extra: `buckshotcalc/engine/`
and `buckshotcalc/sim/` import only the standard library. If you already have the
TUI installed, you can delete `buckshotcalc/tui/` and nothing else breaks.
Contributors use `pip install -e ".[dev]"`.

**First run needs a reload table.** The solver's `V_reload` fixed point is a
one-time precompute; until it exists the TUI shows a yellow
`RELOAD TABLE NOT BUILT` and withholds recommendations rather than guessing.
See below.

## Status

v1 is feature-complete: solver, Dealer policy, TUI, Monte Carlo harness,
agreement corpus, item-value tables, and a live bridge from the game. The
full-scale acceptance run (100 positions vs Monte Carlo) passes in both modes (worst error
0.0031 against a 0.01 gate, 100 positions x 100k trials). Rules were
re-audited against the game's code on 2026-10-25. Open engineering
decisions are tracked in
[`docs/decisions-pending.md`](docs/decisions-pending.md).

- [`rules.toml`](buckshotcalc/rules.toml) — authoritative game constants, each
  with a confidence level. The solver is only as correct as this file.

## Live with the game (optional)

Instead of typing every event, a game mod can feed the TUI as you play
(Double or Nothing only). It sends only what you can see on screen.

```bash
./start.sh            # the calc + the game (test copy), live
./start.sh --steam    # the same with your Steam install
./start.sh --calc-only   # just the calc, type events by hand
./start.sh --help     # everything else (--build, --record, --uninstall)
```

The first run sets up `.venv` and installs the
[BRMLNeo](https://github.com/MSLaFaver/BRMLNeo) mod loader and the bridge mod
into the game folder (`--uninstall` takes them out again). Each game's events
are saved under `temp/games/`. The TUI shows "live from the game"; if the game
and the calc ever disagree it pauses and says what differs, and `ctrl+g` takes
the game's numbers.

Details: [`bridge/README.md`](bridge/README.md), contract: [`docs/live-bridge.md`](docs/live-bridge.md).

## Building the reload table, and running a simulation

The reload fixed point is the expensive part. Build it once —
it is cached to disk under `~/.cache/buckshotcalc/`, keyed by the rules
fingerprint, so this is paid per `rules.toml`, not per run:

```bash
# about 1 h under CPython (estimate)
.venv/bin/python -m buckshotcalc.sim.reload_bench build --game-mode don

# about 26 min under PyPy on 16 cores (measured; identical results)
PYTHONPATH=$PWD pypy3 -m buckshotcalc.sim.reload_bench build --game-mode don
```

Either way it prints a per-sweep progress line with a live ETA — the residual
falls by a roughly constant factor each sweep, so after the second sweep the
estimate is real rather than a guess. The cache is plain JSON and portable
between interpreters: build it under PyPy, use it from the CPython TUI.

It prints where it got to and exits immediately if the table is already
cached. (`full` and `single` are the *benchmark* subcommands — they
deliberately bypass the cache so they always do real work, and leave nothing
behind.)

Then run the §12.1 validation harness against Double or Nothing:

```bash
.venv/bin/python -m buckshotcalc.sim.montecarlo --mode don --positions 100 --trials 100000
```

It prints the worst positions by statistical significance and by raw error,
then the §12.1 acceptance line: how many positions have
`|predicted − empirical| >= 0.01` (acceptance requires zero). Start smaller
(`--positions 20 --trials 5000`) for a quick signal; the full run is long.

Useful flags: `--seed` (position selection and rollouts — note it does *not*
vary the reload table's own sampling error, which is fixed per rules file),
`--rules` to point at an alternate `rules.toml`, `--top` for how many
positions to list.

Worker count for the precompute is `solver.reload_workers` in `rules.toml`
(`-1` = every visible core, the default; `0`/`1` = serial), overridable per
run with `BUCKSHOTCALC_RELOAD_WORKERS`.

**Editing `rules.toml` invalidates the cached table** and re-rolls its
item-draw sample — the next run rebuilds from scratch. See
[`docs/decisions-pending.md`](docs/decisions-pending.md) item 1. **So does a code
change to the engine** (`state`, `rules`, `actions`, `dealer`, `memo_key`, `solver`):
the cache name carries a hash of that code, ignoring comments and docstrings. A stale
table is never loaded; `./start.sh` says the table is missing and `./start.sh --build`
(or `ctrl+t` in the calc) remakes it.

## Design in one paragraph

Shooting yourself with a blank costs nothing *and* returns the turn to you, so
the naive baseline is "shoot yourself when `P(blank) >= 50%`". Every interesting
decision is a deviation from that baseline driven by lethality, turn economy,
item leverage, and the value of information. The solver derives all four from
the game tree rather than encoding them as rules, and maximizes exactly one
quantity: `P(win the current round)`.

## Roadmap

| Version | Scope |
|---|---|
| **v1** | Dealer mode. Exact solver against the Dealer's deterministic policy. All base + Double or Nothing items. TUI, item-value tables, Monte Carlo verification |
| **v2** | Multiplayer heads-up. CFR engine with mixed strategies and information-leakage warnings |
| **v3** | Adaptive opponent profiling; 3–4 player support |

## Stack

Python ≥3.11 · [Textual](https://textual.textualize.io/) TUI · stdlib `tomllib` ·
`pytest`. The engine is pure stdlib and remains importable without the TUI.

## Contributing

Short version: **issues and pull requests may go unanswered.** Forks are welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

- **Mike Klubnika**, creator of [Buckshot Roulette](https://store.steampowered.com/app/2835570/Buckshot_Roulette/).
  The game, its rules and its Dealer are his work; this is an unofficial fan tool.
- **[thecatontheceiling/buckshotroulette](https://github.com/thecatontheceiling/buckshotroulette)**,
  a community decompilation of the game's GDScript. The rules in `rules.toml` and
  the Dealer policy were checked against it.
- **[Bytestorm5/Buckshot-Roulette-Python](https://github.com/Bytestorm5/Buckshot-Roulette-Python)**,
  a Python implementation of the game, used as a reference.
- **[MSLaFaver/BRMLNeo](https://github.com/MSLaFaver/BRMLNeo)**, the mod loader the
  optional live bridge relies on.

Thanks to all of them. Any mistakes in this repo are ours, not theirs.

## Notices

- **AI authorship** (details in [AI-DISCLOSURE.md](AI-DISCLOSURE.md)). All code, tests, docs and the game-rules analysis in this
  repo were produced by AI (Anthropic's Claude, via Claude Code), prompted,
  directed and reviewed at a high level by James Thiessen. Nothing was
  written line by line by a human. Treat it like any unaudited code: read it
  before you trust it. The test suite and Monte Carlo checks (see `sim/montecarlo.py`)
  are the evidence of correctness, not human review.
- **No warranty, no support.** MIT licensed, provided "as is". The author may not
  maintain this repo.
- **Not affiliated.** Buckshot Roulette is by Mike Klubnika. This is an
  unofficial fan tool with no connection to the game's developer or publisher.
  Rules in `rules.toml` come from observing the game and may be wrong or go stale
  after a game update.
- **Game bridge.** `bridge/` and `start.sh` install a mod loader and a mod into
  your game folder. Optional, use at your own risk, and read `bridge/README.md`
  first. Don't use it in a way that breaks the game's terms of service.

---

*MIT licensed, (c) 2026 James Thiessen. See [LICENSE](LICENSE).*
