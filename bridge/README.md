# bridge

Live link from the game to the calc (contract: `docs/live-bridge.md`).

| Path | What |
|---|---|
| `probe-src/` | Calc-Probe, the bridge mod: sends game events as JSON lines to `127.0.0.1:47600` |
| `bot-src/` | Calc-Bot, headless test bot (reads hidden shells; never install it for real play) |
| `play.sh [game_dir]` | install only the bridge mod and launch the game windowed |
| `run_bot.sh` | install both mods and run the headless bot (testing) |
| `listen.py` | minimal stdlib listener for debugging the mod without the TUI |

Game folder defaults to `~/GitHub/_ref/br-test/game`; override with `BR_GAME` or `play.sh <dir>`.
Needs the [BRMLNeo](https://github.com/MSLaFaver/BRMLNeo) mod loader installed in that game folder.

Play: `./start.sh` at the repo root (`--steam` for the Steam install) does all of it.
`play.sh` rebuilds the loader's hook pack only when the mod or the game binary changed.
