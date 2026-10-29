# BuckshotCalc

An exact expectimax solver for Buckshot Roulette, with an optional live TUI.
MIT licensed.

> **Notice:** this software was written entirely by AI (Claude Code) under the
> direction of James Thiessen. It is provided as-is, without warranty, and is not
> actively maintained. See [Notices](#notices).

## What is this?

A decision assistant for **Buckshot Roulette** (the Dealer, Double or Nothing
mode). Tell it the position: shells left, both players' health, and who holds
which items. It returns the play with the highest chance of winning the round,
ranked against every alternative.

It is an exact game-tree solver, not a shell-odds calculator. Anyone can compute
`live / (live + blank)`. The value is in the calls that ratio can't make: whether
an item is worth spending, whether a gamble is right at your current health, and
when the Dealer's own behaviour will do the work for you.

There are two ways to use it:

- **The calculator**: a command (or Python library) with no dependencies. Give it a
  position, get ranked moves.
- **The TUI**: a terminal app that tracks a whole round as you type in events,
  or follows the game live through an optional mod.

## Quick start

Needs Python 3.11+ and git. Pick one.

Prefer a download? Grab the wheel from the [Releases page](https://github.com/TheGeneral4757/buckshotcalc/releases) and `pip install` it (add `[tui]` for the TUI, e.g. `pip install "buckshotcalc-1.0.0-py3-none-any.whl[tui]"`).

**Calculator only** (no TUI, no dependencies):

```bash
git clone https://github.com/TheGeneral4757/buckshotcalc
cd buckshotcalc
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/buckshotcalc --build        # one-time precompute, about 1 hour, then cached
.venv/bin/buckshotcalc --live 3 --blank 2 --hp 4 4 --max-hp 4 --items beer=1,handsaw=1
```

**Calculator and TUI:**

```bash
git clone https://github.com/TheGeneral4757/buckshotcalc
cd buckshotcalc
./start.sh --build                    # one-time precompute, about 35 min, then cached
./start.sh --calc-only                # launch the TUI; type events by hand
```

**Windows** (PowerShell; `start.sh` is bash-only, so use pip directly):

```powershell
git clone https://github.com/TheGeneral4757/buckshotcalc
cd buckshotcalc
py -m venv .venv
.venv\Scripts\pip install ".[tui]"
.venv\Scripts\buckshotcalc --build          # one-time precompute, then cached
.venv\Scripts\buckshotcalc-tui              # the TUI (use Windows Terminal)
```

### Platform support

| Platform | Calculator and TUI | Live game bridge (`start.sh`) |
| --- | --- | --- |
| Arch / CachyOS (developed here) | Yes | Yes |
| Other Linux (Debian, Ubuntu, Fedora, ...) | Yes | Should work, needs bash 4.4+ and a Steam Linux build |
| Windows 10/11 | Yes, via pip as above (CI-tested) | No: `start.sh` and the mod install are Linux-only |
| macOS | Calculator and TUI via pip (CI-tested) | No |

Distro prerequisites: Arch `sudo pacman -S python git`; Debian/Ubuntu
`sudo apt install python3 python3-venv git`; Fedora `sudo dnf install python3 git`.

That is all most people need. The rest of this file is detail.

## The calculator in detail

```bash
buckshotcalc --live 3 --blank 2 --hp 4 4 --max-hp 4 \
             --items beer=1,handsaw=1 --dealer-items cigarettes=1
```

| Flag | Meaning |
| --- | --- |
| `--live N`, `--blank N` | Shells left in the chamber (all unseen) |
| `--hp YOU DEALER` | Current health, and `--max-hp N` for the round |
| `--items`, `--dealer-items` | `name=count,name=count`. Names: `cigarettes handcuffs beer handsaw magnifying_glass adrenaline expired_medicine burner_phone inverter remote` |
| `--mode` | `double_or_nothing` (default) or `base` |
| `--build` | Build the reload table, then exit |

It prints your win probability and every legal move ranked. A move marked
`(wasted)` is strictly worse than the best one. It always assumes it is your turn,
with the chamber fully unseen. For known shells, cuffs, a sawed barrel and so on,
use the TUI or the Python API below.

As a library (standard library only):

```python
from buckshotcalc.engine.rules import load_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import ChamberState, GameMode, GameState, Player

solver = Solver(load_rules(), GameMode.DOUBLE_OR_NOTHING)
state = GameState.create(ChamberState.create((None,) * 5, 3, 2), hp=(4, 4), max_hp=4,
                         turn=Player.PLAYER)
print(solver.evaluate(state).value)
```

## The TUI in detail

Install with `pip install ".[tui]"` (or let `./start.sh` do it), then run
`buckshotcalc-tui` or `./start.sh --calc-only`. In the calc, `ctrl+t` builds the
reload table if it is missing.

### Reload table

The solver needs a one-time precompute (the `V_reload` fixed point). Until it
exists, the calculator exits with a message and the TUI shows a yellow
`RELOAD TABLE NOT BUILT` and withholds answers rather than guessing. It is cached in
`~/.cache/buckshotcalc/`, so you pay once per `rules.toml` and engine version.
Full build and timing details are in
[Building the reload table](#building-the-reload-table-and-running-a-simulation).

### Calculator without the TUI

The `engine/` and `sim/` packages import only the standard library. Not installing
the `[tui]` extra is enough. Deleting `buckshotcalc/tui/` also works.
Contributors use `pip install -e ".[dev]"`.

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

Then run the validation harness against Double or Nothing:

```bash
.venv/bin/python -m buckshotcalc.sim.montecarlo --mode don --positions 100 --trials 100000
```

It prints the worst positions by statistical significance and by raw error,
then the acceptance line: how many positions have
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

This repository is not actively maintained, and issues and pull requests may not
receive a response. Forks are encouraged. Guidelines for reporting bugs and
proposing changes are in [CONTRIBUTING.md](CONTRIBUTING.md).

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

The authors and maintainers listed above are not responsible for any errors in this repository.

## Notices

- **AI authorship.** All code, tests, documentation, and rules analysis in this
  repository were produced by AI (Anthropic's Claude, via Claude Code), directed
  and approved by James Thiessen, and have not had independent human review. See
  [AI-DISCLOSURE.md](AI-DISCLOSURE.md).
- **No warranty or support.** The software is provided "as is" under the MIT
  License. The author may not maintain it.
- **No affiliation.** Buckshot Roulette is created by Mike Klubnika. This is an
  unofficial fan project with no connection to the game's developer or publisher.
  The rules in `rules.toml` are derived from observation of the game and may be
  incorrect or become outdated after a game update.
- **Game bridge.** The optional `bridge/` directory and `start.sh` install a mod
  loader and a mod into your game folder. Use them at your own risk, review
  `bridge/README.md` first, and ensure your use complies with the game's terms of
  service.

---

*MIT licensed, (c) 2026 James Thiessen. See [LICENSE](LICENSE).*
