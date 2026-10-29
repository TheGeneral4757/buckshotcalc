#!/usr/bin/env bash
# Rebuild the Calc-Probe (bridge) and Calc-Bot mods, regenerate the hook pack, then run the headless bot.
# Usage: ./run_bot.sh [games=1] [speed=8] [policy=oracle|random] [timeout_s=600]
set -u
GAMES=${1:-1}; SPEED=${2:-8}; POLICY=${3:-oracle}; TMO=${4:-600}
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GAME="${BR_GAME:-$HOME/GitHub/_ref/br-test/game}"   # the test copy; Steam later
LOGDIR="${BR_LOGDIR:-$(dirname "$GAME")}"   # logs stay next to the game, out of the repo
SRC="${PROBE_SRC:-$HERE/probe-src}"
EXE="./Buckshot Roulette.x86_64"

pkill -x 'Buckshot Roulet' 2>/dev/null
# package the mods: bridge (Calc-Probe) and test bot (Calc-Bot) are separate zips
BOT_SRC="${BOT_SRC:-$HERE/bot-src}"
mkdir -p "$GAME/mods"; rm -f "$GAME/mods/Calc-Probe.zip" "$GAME/mods/Calc-Bot.zip"
zipmod() { (cd "$1" && python3 -I -c "
import zipfile,os,sys
z=zipfile.ZipFile(sys.argv[1],'w')
for r,_,fs in os.walk('mods-unpacked'):
    for f in fs: z.write(os.path.join(r,f))
z.close()" "$2"); }
zipmod "$SRC" "$GAME/mods/Calc-Probe.zip"
zipmod "$BOT_SRC" "$GAME/mods/Calc-Bot.zip"
if [ "${SKIP_GEN:-0}" != "1" ]; then
# a stale hook pack blocks new hooks: always regenerate
rm -f "$GAME/mod-hooks.zip"
(cd "$GAME" && timeout 240 "$EXE" --headless --quit-after 400 >/dev/null 2>&1 &)
# wait for the hook pack to appear and stop growing (generation then triggers a self-restart)
prev=-1; stable=0
for _ in $(seq 1 120); do
  sleep 2
  sz=$(stat -c %s "$GAME/mod-hooks.zip" 2>/dev/null || echo 0)
  if [ "$sz" -gt 0 ] && [ "$sz" = "$prev" ]; then stable=$((stable+1)); else stable=0; fi
  prev=$sz
  [ $stable -ge 5 ] && break
done
pkill -x 'Buckshot Roulet' 2>/dev/null; sleep 1
fi
[ -f "$GAME/mod-hooks.zip" ] || { echo "hook pack was not generated"; exit 1; }
# real run
(cd "$GAME" && BR_BRIDGE_DEBUG=1 BR_BOT=1 BR_BOT_GAMES="$GAMES" BR_BOT_SPEED="$SPEED" BR_BOT_POLICY="$POLICY" \
  timeout "$TMO" "$EXE" --headless >"$LOGDIR/bot_run.txt" 2>&1)
echo "exit=$? (124 = hit the ${TMO}s cap)"
pkill -x 'Buckshot Roulet' 2>/dev/null
grep -aE "^\[(bridge|bot)\]" "$LOGDIR/bot_run.txt" > "$LOGDIR/last_run.log"; echo "log: $LOGDIR/last_run.log ($(wc -l < "$LOGDIR/last_run.log") lines)"
