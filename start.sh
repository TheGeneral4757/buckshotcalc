#!/usr/bin/env bash
# One command for BuckshotCalc: first-time setup, the calc, and the game with the live bridge.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOADER="${BR_LOADER:-$ROOT/temp/BRMLNeo}"   # BRMLNeo mod loader checkout
TEST_GAME="${BR_GAME:-$HOME/GitHub/_ref/br-test/game}"   # the author's test copy; absent elsewhere
LOADER_FILES=(addons/JSON_Schema_Validator addons/mod_loader godot/global_script_class_cache.cfg
              godot/uid_cache.bin override.cfg)

usage() {
  cat <<'E'
Usage: ./start.sh [options]

  (no options)     the calc + the test copy of the game, live (Steam if there is none)
  --steam          use the Steam install instead of the test copy
  --game DIR       use this game folder
  --calc-only      just the calc, type events by hand (no game)
  --build          build the missing reload table first (~35 min, uses every core)
  --record FILE    save the game's events here (default: temp/games/<time>.jsonl)
  --no-record      do not save events
  --advice FILE    save the calc's advice log here (default: temp/advice/<same name as the game log>)
  --no-advice      do not save the advice log
  --uninstall      remove the mod and the mod loader from the game folder, then exit
  -h, --help       this help

First run sets up .venv and installs the mod loader into the game folder by itself.
E
}

game="$TEST_GAME"; calc_only=0; build=0; uninstall=0
record="$ROOT/temp/games/$(date +%Y%m%d-%H%M%S).jsonl"
advice=""; no_advice=0
while [ $# -gt 0 ]; do
  case "$1" in
    --steam) game="" ;;
    --game) game="${2:?--game needs a folder}"; shift ;;
    --calc-only) calc_only=1 ;;
    --build) build=1 ;;
    --record) record="${2:?--record needs a file}"; shift ;;
    --no-record) record="" ;;
    --advice) advice="${2:?--advice needs a file}"; shift ;;
    --no-advice) no_advice=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

[ "$game" = "$TEST_GAME" ] && [ ! -d "$TEST_GAME" ] && game=""   # no test copy here: use Steam
if [ -z "$game" ]; then
  # Steam library folders, then the Linux build inside the game's folder
  while IFS= read -r lib; do
    d="$lib/steamapps/common/Buckshot Roulette/Buckshot Roulette_linux"
    [ -x "$d/Buckshot Roulette.x86_64" ] && { game="$d"; break; }
  done < <(grep -ho '"path"[[:space:]]*"[^"]*"' ~/.local/share/Steam/steamapps/libraryfolders.vdf \
           ~/.steam/steam/steamapps/libraryfolders.vdf \
           ~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/libraryfolders.vdf 2>/dev/null | cut -d'"' -f4 | sort -u)
  [ -n "$game" ] || { echo "Buckshot Roulette is not installed in any Steam library"; exit 1; }
fi

if [ $uninstall = 1 ]; then
  cd "$game" || exit 1
  pkill -x 'Buckshot Roulet' 2>/dev/null
  rm -rf "${LOADER_FILES[@]}" mods/Calc-Probe.zip mods/Calc-Bot.zip mods/.calc-hooks-stamp mod-hooks.zip
  rmdir addons godot mods 2>/dev/null
  echo "removed the mod and the mod loader from $game"
  exit 0
fi

# first run: the venv
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "first run: setting up .venv ..."
  if command -v uv >/dev/null; then (cd "$ROOT" && uv venv -q && uv pip install -q -e ".[tui]")
  else python3 -m venv "$ROOT/.venv" && "$ROOT/.venv/bin/pip" install -q -e "$ROOT[tui]"; fi || exit 1
fi
PY="$ROOT/.venv/bin/python"

tables_ok() {
  "$PY" -c "
import sys
from buckshotcalc.engine.rules import load_rules
from buckshotcalc.engine.solver import Solver
from buckshotcalc.engine.state import GameMode
r = load_rules()
from buckshotcalc.bundled import install
install()
sys.exit(0 if Solver(r, GameMode.DOUBLE_OR_NOTHING).has_cached_reload_table() else 1)"
}
if ! tables_ok; then
  if [ $build = 1 ]; then
    fast="$(command -v pypy3 || echo "$PY")"
    for m in don; do
      echo "building the $m reload table with $(basename "$fast") ..."
      (cd "$ROOT" && PYTHONPATH="$ROOT" "$fast" -m buckshotcalc.sim.reload_bench build --game-mode $m) || exit 1
    done
  else
    echo "note: no reload table for the current rules and engine code (never built, or rules.toml"
    echo "      or the engine changed since), so some answers are withheld."
    echo "      run ./start.sh --build once (~35 min), or press ctrl+t in the calc."
  fi
fi

args=()
if [ $calc_only = 0 ]; then
  [ -x "$game/Buckshot Roulette.x86_64" ] || { echo "no game found in $game"; exit 1; }
  if [ ! -f "$game/override.cfg" ]; then
    [ -d "$LOADER" ] || git clone -q https://github.com/MSLaFaver/BRMLNeo "$LOADER" || exit 1
    echo "installing the BRMLNeo mod loader into $game"
    (cd "$LOADER" && cp -r addons godot override.cfg "$game/") || exit 1
  fi
  mkdir -p "$ROOT/temp"
  echo "starting the game (log: temp/game.log)"
  "$ROOT/bridge/play.sh" "$game" > "$ROOT/temp/game.log" 2>&1 &
  if [ -n "$record" ]; then mkdir -p "$(dirname "$record")"; args=(--record "$record"); fi
  if [ $no_advice = 1 ]; then args+=(--no-advice)
  elif [ -n "$advice" ]; then mkdir -p "$(dirname "$advice")"; args+=(--advice "$advice"); fi
fi
cd "$ROOT" && exec "$PY" -m buckshotcalc.tui.app "${args[@]}"
