# UI roadmap: a better TUI, then maybe a web UI

Written 2026-10-25, after the live bridge landed. Phase 1 is built (2026-10-25): see "Phase 1 as built" below; the rest is still plans.

## What changed

The M7 TUI was designed for **typing every event at speed** (SPEC.md §8-§9, the settled
calls in the project memory). With the bridge, most rounds need no typing at all. The
screen's job moves from "fast data entry" to "read the answer at a glance while playing,
and step in only when the bridge pauses".

What already makes a second front end cheap:

| Layer | Module | UI-free? |
|---|---|---|
| Game state, undo, belief | `tui/session.py` | yes (stdlib) |
| Live events | `tui/bridge.py` `apply_event` | yes |
| Answers, alternatives, odds | `engine/solver.py`, `tui/analysis.py` | yes |
| Screen snapshot | `panels.DashboardViewModel` | yes (plain dataclass) |
| Drawing | `panels.py`, `theme.py`, `app.py` | Textual/Rich only |

So a new UI is a new drawing layer over the same `DashboardViewModel`, not a rewrite.

## Phase 1: TUI watch mode (small, do first)

| Change | Why |
|---|---|
| Two layouts: **watch** (bridge connected) and **entry** (manual) | In watch mode the keys legend and the new-round editor are noise; give their space to the answer |
| Watch layout: the big number, the recommended action, the "why" line, the top 3 alternatives, the shell odds strip | The things read from across the desk |
| Auto-switch: bridge connects -> watch, bridge off/paused -> entry | The pause is exactly when typing is needed |
| Last event line ("dealer shot itself, blank") with a short highlight | Confirms the calc saw what you saw, so trust is earned per event |
| A narrow layout (about 80 columns) for a terminal beside the game on one monitor | The 27-inch layout assumes a second screen |

Keeps every settled rule: monochrome legibility, no backgrounds, every key on screen
(in entry mode), plain language.

Effort: 1-2 days, mostly `panels.py`. Tests: the existing mono and no-background tests,
plus a render test per layout.

### Phase 1 as built

| Piece | Where |
|---|---|
| Watch layout L2 (hero 58 + side 40, shell strip, event lines), pause box O1, recap R1 | `tui/watch.py` (`panels.render_watch`) |
| Animations (number, shells, delta, pulse, typewriter, pips, items, wipe, stamps, low-HP looks, progress) | `tui/anim.py` (pure) driven by `tui/watchdriver.py` |
| 25 fps tick that runs only while something moves | `app._ensure_ticking` / `_tick` |
| Settings overlay | `ctrl+o`, `tui/settings_screen.py`, saved to `~/.config/buckshotcalc/tui.toml` |
| Layout switch | watch once the bridge has sent an event; `ctrl+e` forces the entry layout, bridge off also returns to it |
| Screenshots | `.venv/bin/python scripts/watch_shots.py OUT_DIR` |

Not built: the narrow (about 80 column) layout, and the session strip (no session tracker exists yet).

## Phase 2: TUI quality of life

- **Round history**: per round, the recommended play vs what you did, and the win chance at each step. Built from the `Session` log plus `--record` files.
- **Replay viewer**: `--replay file.jsonl` steps event by event with left/right, showing what the calc said at each point. Good for checking a lost round.
- **"Why not X?"**: pick any alternative and see the difference in plain words (uses the existing alternatives data).

Effort: 2-3 days.

## Phase 3: web UI (optional, later)

Two ways to build it:

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Local server (recommended)** | Python stdlib `http.server` + one WebSocket or server-sent-events stream; the browser page renders `DashboardViewModel` as JSON | Reuses everything; the solver keeps running on PyPy/CPython speed; the bridge already lives in Python | Needs the calc running locally (it does anyway for the bridge) |
| B. In-browser (Pyodide/WASM) | Run the engine in the browser | No local process | Much slower solver; no bridge (a browser cannot accept the mod's TCP connection); reload table must be shipped |

Option A plan:
1. `buckshotcalc/web/server.py`: serves one static page and streams the view model as JSON after every change. Localhost only, same as the bridge.
2. One HTML page: big number, action, why line, alternatives, shell strip; follows the system light/dark theme; works on a phone on the same network only if explicitly enabled (off by default).
3. Input: a small set of buttons for the pause case (accept game state, undo). Full manual entry stays in the TUI.
4. The TUI and the web page can run at the same time off one `Session`.

Third-party dependencies: none needed (stdlib server); keep it out of `engine/` and `sim/`
per CLAUDE.md.

Effort: 3-5 days for a usable page. Main risk: keeping two front ends in step, which
the shared view model mostly removes.

## Open questions for the operator

1. Watch mode: is the alternatives list wanted while playing, or just the one answer?
2. Web UI: is a phone/tablet beside the screen a goal (needs LAN access), or only a browser window on the same PC?
3. Should round history persist across sessions (a local file per day)?
