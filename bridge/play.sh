#!/usr/bin/env bash
# Install only the bridge mod (no test bot) into the game, rebuild the hook pack if needed, launch windowed.
# The usual way in is ../start.sh, which also starts the calc.
# Usage: ./play.sh [game_dir]   (default: BR_GAME, else the _ref test copy)
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GAME="$(cd "${1:-${BR_GAME:-$HOME/GitHub/_ref/br-test/game}}" && pwd)"
SRC="${PROBE_SRC:-$HERE/probe-src}"
EXE="./Buckshot Roulette.x86_64"
STAMP="$GAME/mods/.calc-hooks-stamp"

pkill -x 'Buckshot Roulet' 2>/dev/null
mkdir -p "$GAME/mods"; rm -f "$GAME/mods/Calc-Probe.zip" "$GAME/mods/Calc-Bot.zip"
(cd "$SRC" && python3 -I -c "
import zipfile,os,sys
z=zipfile.ZipFile(sys.argv[1],'w')
for r,_,fs in os.walk('mods-unpacked'):
    for f in fs: z.write(os.path.join(r,f))
z.close()" "$GAME/mods/Calc-Probe.zip")

# The hook pack must match the mod set and the game binary (a stale one blocks hooks). It takes
# minutes to build, so only rebuild when the mod, the game (a Steam update) or the pack itself changed.
stamp() { (cd "$GAME" && { sha256sum mods/Calc-Probe.zip mod-hooks.zip 2>/dev/null; stat -c '%s %Y' "$EXE"; }); }
if [ "$(stamp)" != "$(cat "$STAMP" 2>/dev/null)" ]; then
  echo "building the mod hook pack (a few minutes, once per mod or game update)..."
  rm -f "$GAME/mod-hooks.zip"
  (cd "$GAME" && timeout 240 "$EXE" --headless --quit-after 400 >/dev/null 2>&1 &)
  prev=-1; stable=0
  for _ in $(seq 1 120); do
    sleep 2
    sz=$(stat -c %s "$GAME/mod-hooks.zip" 2>/dev/null || echo 0)
    if [ "$sz" -gt 0 ] && [ "$sz" = "$prev" ]; then stable=$((stable+1)); else stable=0; fi
    prev=$sz
    [ $stable -ge 5 ] && break
  done
  pkill -x 'Buckshot Roulet' 2>/dev/null; sleep 1
  [ -f "$GAME/mod-hooks.zip" ] || { echo "hook pack was not generated"; exit 1; }
  stamp > "$STAMP"
fi
cd "$GAME" && exec "$EXE"
