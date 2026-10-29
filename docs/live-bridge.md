# Live bridge: Calc-Probe mod to Session

Status: both sides built (2026-10-25). Mod `Calc-Probe` 0.2.0 and scripts in `bridge/` (see `bridge/README.md`); calc side in `buckshotcalc/tui/bridge.py`. Sources read: `Calc-Probe/mod_main.gd` (probe v4), `tui/session.py`, SPEC.md sections 3, 9, 15, the decompile in `_ref/buckshotroulette/`, the live log `probe_events.log` and the bot logs in `_ref/br-test/*.log`.

Note on the logs: `_ref/probe_run1.log` is probe **v3**. The live log and the bot logs are **v4**. Two of the known issues (beer "?", `<null>` HP in "after:" lines) are v3 bugs that v4 already fixed (eject hooks, `_rm()` lookup). The rest of this doc is written against v4.

## 1. Purpose

The operator types every table event into the TUI. The mod sees the same events in the real game. A localhost socket carries them to the calc, and an adapter turns each one into the same `Session` call the operator would make (`record_shot`, `use_item`, `steal`, `new_load`, ...). The adapter never talks to the engine directly. The operator keeps `undo` and every `edit_*`, so a wrong event is always recoverable.

Design rules:

- The bridge sends only what a human sitting at the table can see. Nothing else crosses the socket.
- Events describe what happened, not what the calc should do. The adapter owns the mapping.
- The game is the clock. The adapter applies events in `seq` order and checks a `sync` snapshot at every turn start. A mismatch is shown to the operator, not silently fixed.

## 2. Event table

What probe v4 emits today, and what is still needed. "Probe line" is the current log text.

| # | Event (probe line) | Fields today | Status | What is wrong / fix |
|---|---|---|---|---|
| E1 | (none) hello/handshake | - | MISSING | Add `hello` with game script hashes (section 6). Loader reports game version 0.0.0. |
| E2 | Round or load start: `ROUND START gettingNext=`, `ITEMS DEALT`, `SHELLS LOADED` | gettingNext, endless, both item racks, total/live/blank | PARTIAL | Three separate lines; HP only arrives later via the poller. Merge into one `load` event emitted from the `ShellSpawner.SpawnShells` hook (runs after items). `gettingNext=false` is a new round (`MainBatchSetup` -> `StartRound(false)`, HP reset), `true` is a reload (HP carries). Read HP from `RoundManager.health_player/health_opponent` at that point. `max_hp` = `roundManager.roundArray[0].startingHealth` (the game's own heal cap, `HealthCounter.gd:127`). |
| E3 | Turn flow: `PLAYER TURN BEGIN`, `DEALER TURN BEGIN`, `DEALER CUFF CHECK`, `TURN HANDOFF -> x` | state string (hp, cuffs, saw, left) | PARTIAL | Used only as a sync check, since the engine already handles cuffs. `BeginPlayerTurn` also fires on a cuffed player's skipped turn (it then hands to the dealer), so `PLAYER TURN BEGIN` is not always a real player turn. Emit `turn` with `cuffed_skip=true` in that case (read `playerCuffed` at hook entry). State is a free-text string; send JSON. |
| E4 | Shot: `PLAYER/DEALER AIMS at x`, `SHELL EJECTED (after shot) = t`, `KILL who=`, `PLAYER/DEALER SHOT x done` | target, shell type (on eject) | PARTIAL | Four lines for one fact, and they interleave (the "done" line is logged after the hook returns, often after the next turn's lines). A fatal shot has no eject line (17 of 221 shots in the logs; always live). Fix: remember actor and target at the AIMS hook, and emit one `shot` the first time the shell is revealed: `ShellEjectManager.EjectShell` / `DeathEjection` (blank or live), or `DeathManager.Kill` (live, fatal). Add `damage` from `currentShotgunDamage` read at AIMS. |
| E5 | `PLAYER USES x` / `DEALER USES x` for cigarettes, handcuffs, handsaw | item name | COVERED | Maps straight to `use_item`. Actor is the line prefix. |
| E6 | `... USES inverter` | item name | COVERED | Nobody sees the flip. `record_inverter` takes no argument. |
| E7 | Beer: `SHELL EJECTED (player beer)` / `(dealer beer)` = t | shell type | COVERED | v3 logged "?"; v4 hooks `ShellEjectManager.BeerEjection_player/_dealer` and reads slot 0 before the game removes it. Both seats work. |
| E8 | Own glass: `MAGNIFIER shows chamber = t` | shell type | COVERED | Hook is `ShellExamine.SetupShell`, which only runs for the player's own glass (including a stolen one). The dealer's glass does not call it. |
| E9 | Dealer private info: `DEALER USES magnifying glass` / `burner phone` | item name | COVERED | Use event only, no answer. Goes to `record_dealer_info_action(0)` / `record_dealer_phone()`. |
| E10 | Own burner phone result: `DIALOGUE SECOND SHELL ... / ... LIVE ROUND.` | raw text | PARTIAL | Unparsed, and the dialogue hook fires for every line of dialogue. Source: `BurnerPhone.SendDialogue` picks `randindex` (local, not readable) and shows `tr(tr("SEQUENCE<n>"))` + live/blank text. Fix: hook `SendDialogue`, then take the next `ShowText_Forever` text and decode it with the game's own translations (build the lookup at startup from `tr(tr("SEQUENCE2..7"))` -> slot n-1, and `LIVEROUND`/`BLANKROUND`), so it works in any language. `UNFORTUNATE` means one shell left: no information. Emit `phone{slot, shell}`. |
| E11 | Expired medicine: `PLAYER MEDICINE done | hp`, `DEALER MEDICINE done | hp` | HP only | PARTIAL | The heal/harm outcome must be guessed from the HP delta, which breaks at max HP and for a wire-cut dealer (see section 5). The coin is a local (`dying` in `MedicineManager.UseMedicine`). Authoritative source: hook `HealthCounter.UpdateDisplayRoutineCigarette_Player/_Enemy` and read `overriding_medicine` and `overriding_medicine_adding` on the reference object at entry (set just before the call by both seats' medicine code). Emit `medicine{actor, healed}`. Same hook with `overriding_medicine=false` is cigarettes. |
| E12 | Adrenaline: `PLAYER USES adrenaline` then `PLAYER USES x` (same for dealer) | item names | PARTIAL | The second line looks like an ordinary use. Needs a `stolen` flag: `ItemInteraction.stealing` (player) and `HandManager.stealing` (dealer) are set before the stolen item's `InteractWith`/`PickupItemFromTable`. Read it at hook entry. The adapter then calls `steal(item, ...)` and completes it with the glass/phone/beer/medicine outcome that follows. Adrenaline can expire unused (SPEC 3.2); there is no event for that (E19). |
| E13 | State stream: `CHANGE hp/cuffed/sawed/damage/chamber count/items` | field, old, new | PARTIAL | Polled every 0.1 s in `_process`, so it has no causal order against hook events, and HP lags the shot. Keep it for `sync` only (section 6), never as an action source. `sawed` clears at turn end, not at the shot (see section 5). |
| E14 | Item racks: `ITEMS DEALT` | counts per seat (sorted list) | COVERED | Public. Feeds `new_load/new_round(items=...)`. Optional: dealer rack in deal order (E18). |
| E15 | Round end: `OUT OF HEALTH who=`, `BATCH END`, `DOUBLE-OR-NOTHING RESPONSE` | who / bool | COVERED | Used to assert `state.winner()` and prompt for a new round. |
| E16 | Wire cut / forced HP=1 / broken HP display (campaign final batch) | - | MISSING | Hook `DefibCutter.CutWire` (and read `wireIsCut_player/dealer`). Emit `wire_cut{who}` so the adapter can `edit_hp(who, 1)` and flag "no healing". Campaign only; see section 5. |
| E17 | Test harness: `BOT ...`, `PERF ...` | - | drop | Must not exist in the shipped bridge build (see section 4). |
| E18 | Dealer rack in deal order | - | MISSING (optional) | SPEC 3.4 says the real Dealer walks its rack in deal order; `GameState` cannot hold it yet. Public on the table, so cheap to send for later. Source: `ItemManager.itemArray_dealer`. |
| E19 | Adrenaline expired unused | - | MISSING (rare) | No hook found yet. Handle by reconciling at the next `sync`. |

Totals for this table: COVERED 7 (E5-E9, E14, E15), PARTIAL 7 (E2, E3, E4, E10, E11, E12, E13), MISSING 4 (E1, E16, E18, E19). E17 is dropped.

## 3. Session-action mapping

| Session action | Driven by | Status | Notes |
|---|---|---|---|
| `record_shot(at_self, shell)` | `shot` (E4) | PARTIAL | Needs the merged `shot` event. `at_self` = target equals actor. Shell from eject/kill. |
| `record_glass(shell)` | E8 | COVERED | Only for the player's own glass. |
| `record_phone(slot, shell)` | `phone` (E10) | PARTIAL | Slot is 1-based position from the chamber in the game's words ("second shell" = slot 1); session slot index is 0-based, chamber = 0. |
| `record_beer(shell)` | E7 | COVERED | Actor is `state.turn`, so it works for the dealer too. |
| `record_inverter()` | E6 | COVERED | Sets `degraded` on an unresolved slot 0, by design. |
| `record_expired_medicine(healed)` | `medicine` (E11) | PARTIAL | Needs the explicit `healed` flag. |
| `record_dealer_info_action(0)` / `record_dealer_phone()` | E9 | COVERED | Uses the dealer-glass/phone event only. |
| `use_item(cigarettes / handcuffs / handsaw)` | E5 | COVERED | |
| `steal(item, shell=, healed=, phone_slot=)` | E12 + the outcome event | PARTIAL | Needs `stolen`. The adapter buffers the steal until the outcome event arrives (beer eject, glass, phone, medicine). |
| `new_load(live, blank, items)` | `load` kind=reload | PARTIAL | Needs merged `load` (E2). First turn is always the player after a reload (SPEC 15.11). |
| `new_round(live, blank, max_hp, items)` | `load` kind=new_round | PARTIAL | Same. |
| `give_item` / `edit_item_count` | `load.items`, `sync.items` | COVERED | Use the dealt snapshot, not the poller. |
| `edit_hp / edit_max_hp / edit_cuffed / edit_sawed / edit_turn` | `sync` | PARTIAL | Auto-apply only after the operator confirms a shown mismatch. |
| `edit_dealer_knows`, `edit_chamber` | none | NOT DRIVEN | Both are hidden information. The bridge never writes them. |
| `undo` | operator | NOT DRIVEN | Stays manual. An undo desyncs the calc from the game; the next `sync` shows it. |
| Campaign wire cut (forced HP 1, no healing) | `wire_cut` (E16) | MISSING | No Session action exists. Use `edit_hp`, plus a new "healing disabled" flag if campaign mode is ever supported. |

Totals: COVERED 6, PARTIAL 7, MISSING 1 (plus 2 NOT DRIVEN by design).

## 4. Hidden-info rules

The calc may receive only what the human player can see or is told.

| Item | Verdict |
|---|---|
| `sequenceArray` (the full shell order) | Never serialise. The probe's `_chamber()` / `_shell()` helpers expose it. v4 uses `_shell()` only inside eject and magnifier hooks, which fire at the moment the game shows the shell. Keep it that way: no other code path may read slot 0. |
| Reading slot 0 at `Shoot` entry (needed to build `shot`) | Allowed only if kept private in the mod and sent when the eject/kill hook fires, not at AIMS (the shot lands about 2 s later). |
| `BOT` oracle (`_bot_pick_target` reads `_shell()`) | LEAK by design (test harness). Strip all bot code from the bridge build, or gate it so the socket is never opened when `BR_BOT=1`. |
| Dealer-private variables: `DealerIntelligence.knownShell`, `dealerKnowsShell`, `dealerTarget`, `sequenceArray_knownShell` | Never read. The calc models dealer knowledge itself via `dealer_knows`. |
| `MedicineManager.dealerDying` | Set by the dealer's decision well before the animation shows it. Read only after the hook's chain finishes. |
| Dealer's glass/phone result | Not sent. Only "dealer used X". |
| `FigureOutShell` result | Dealer-private. Do not read. |
| Chamber count, live/blank totals at load, both item racks, HP, cuffs, saw | Public. Fine. |
| Own glass/phone results, ejected shells, fired shells | Public to the player. Fine. |
| Burner phone dialogue | Private to the player but the player knows it. Fine, and the dealer's phone shows no dialogue. |

How `session.py` keeps them apart: `record_glass` / `record_phone` resolve our chamber and never touch `dealer_knows`. `record_dealer_info_action` adds to `dealer_knows` and never resolves our chamber. `Session.DEALER_INFO_ITEMS` guards the dealer routes. The adapter must route by actor: player glass/phone -> `record_glass/phone`; dealer glass/phone -> `record_dealer_*`. Do not route a dealer event to a `record_*` that has no actor guard. One caveat to check in the engine: `steal()` with a dealer-stolen glass or phone still requires `shell=`, and the dealer's result is never visible, so that path cannot be fed from the bridge. Treat "dealer steals our glass/phone" as an open item (section 8).

## 5. Forced HP=1 finding

Found in the decompile. It applies only to the **campaign, final batch**, never to Double or Nothing.

`HealthCounter.gd` lines 91-105, inside `UpdateDisplayRoutine` (called from `DeathManager.gd:68, 103` after a hit):

```
91  if ((roundManager.health_opponent <= 2 or roundManager.health_player <= 2) && roundManager.playerData.currentBatchIndex == 2 && !roundManager.endless):
...
96      if (roundManager.health_opponent <= 2 && !roundManager.wireIsCut_dealer):
97          roundManager.wireToCut = "dealer"
98          roundManager.requestedWireCut = true
99          roundManager.wireIsCut_dealer = true
100         roundManager.health_opponent = 1
101     if (roundManager.health_player <= 2 && !roundManager.wireIsCut_player):
...
105         roundManager.health_player = 1
```

`ShotgunShooting.gd:187` repeats the condition only to add delays. The cutter (`DefibCutter.CutWire`) then runs the "cut the wire" scene and `BreakDisplay` hides that side's HP display for good.

Consequences:

- Trigger: first time a side's HP drops to 2 or less (a hit that leaves 3 or 4 HP at 2 or less, or reaches 2 at max HP 2). That side is set to exactly 1 HP, once. A max-HP-4 side hit by a sawed shot from 4 to 2 goes to 1. A side at 3 HP shot for 1 goes to 2, then to 1.
- After the cut: `wireIsCut_*` is never reset in the scripts. `HealthCounter.gd:130,145` skip all healing for that side (cigarettes and the medicine heal). The dealer's medicine harm (`:131-134`) is also skipped, so a cut dealer is immune to its own bad pill; the player's harm still applies (`MedicineManager.gd:27`).
- The player can no longer see that side's HP (display broken). The calc must keep tracking it itself.
- Gate: `currentBatchIndex == 2 && !endless`. `defibCutterReady` is also only set when `!endless` (`RoundManager.gd:202`).

Effect on the solver: `rules.toml` ships `mode = "double_or_nothing"`, so nothing changes today. SPEC section 3 and 15 do not mention this. If the campaign final batch is ever a target it needs a rules entry: forced HP 1 at `hp <= 2` once per side, then no healing for that side, dealer immune to medicine harm. Add as a new SPEC 15 question when that happens.

### Related divergence found while reading (not asked, but it contradicts a VERIFIED rule)

SPEC 3.2 and `rules.toml:76-80` say the handsaw is consumed when the shell is fired **or ejected**, so saw plus beer wastes the saw. The decompile and the logs say otherwise:

- The only place that clears `barrelSawedOff` and resets `currentShotgunDamage` to 1 is `SegmentManager.GrowBarrel` (`SegmentManager.gd:16-17`), called from `RoundManager.EndTurn` (`RoundManager.gd:269-273`) and at batch end. The beer branch of `ItemInteraction.InteractWith` (`:130-141`) and the dealer's beer never touch the saw.
- `sweep_100.log` shows `CHANGE sawed false -> true`, then `PLAYER USES beer` / `SHELL EJECTED (player beer)`, then the next shot is taken with `sawed=true` and the flag clears only after `TURN HANDOFF`.

So in the real game the saw survives a beer and applies to the next shell, and it clears at the end of the turn's shot (including a blank self-shot). This needs an operator decision (section 8).

Smaller note: the player's burner phone clamps its index (`BurnerPhone.gd`: `if randindex == 8 or 7: randindex -= 1`), so with 8 shells slot 7 is never named and slot 6 is twice as likely. The dealer's phone only clamps 8 (never reached), so it is uniform. This affects only how the player's phone result is weighted; the bridge reports what was shown.

## 6. Wire format

Transport: TCP on 127.0.0.1, port 47600. The calc listens; the mod connects (`StreamPeerTCP`, non-blocking) and retries every 2 s while nothing listens, so either side can restart and the game never stalls or errors. One JSON object per line, UTF-8, `\n` terminated. The mod only writes, except that it reads `hello_ack` to warn on `known=false`. While disconnected the mod drops events (no queue); the first connection after drops sends `gap`. Unsent bytes are capped at 256 KB (cleared and counted as dropped if the calc stops reading).

Every message has `v` (protocol version, integer, now 1) and `ev`. `seq` (integer, starts at 1 on each connection) and `t` (ms since the game process started) are on every message. Seats are `"player"` or `"dealer"`. Items use the game's names: `cigarettes, handcuffs, beer, handsaw, magnifying glass, adrenaline, expired medicine, burner phone, inverter`. Shells are `"live"` or `"blank"`. Key order within a line is not fixed.

| ev | Fields | Notes |
|---|---|---|
| `hello` | `probe, godot` (`"4.1"`), `game_ver`, `mode` (`"endless"` or `"campaign"`), `scripts` (file -> sha256), `scripts_sha` (sha256 of the hashes concatenated in file-name order) | Sent once per connection, at the first `StartRound` after connecting (the mode is only known then, not in the menu). Hashes are `FileAccess.get_sha256("res://scripts/<f>.gd")` for `RoundManager, ItemInteraction, DealerIntelligence, ShotgunShooting, HealthCounter, ShellEjectManager, BurnerPhone, MedicineManager, HandManager` (the files as loaded, i.e. after the mod loader's hook rewrite; stable for a given install). |
| `hello_ack` (calc to mod) | `v, ok, known` | `known=false`: the mod prints a warning and keeps going. |
| `load` | `kind` (`"new_round"` = `StartRound(false)`, HP reset; `"reload"` = `StartRound(true)`, HP carries), `live, blank, hp:[player,dealer], max_hp, items:{player:{name:n}, dealer:{name:n}}, first_turn` | Emitted after `SpawnShells` returns, i.e. after items are dealt. `first_turn` is always `"player"`. |
| `turn` | `who, cuffed_skip` | `cuffed_skip=true` when the seat's turn is skipped because it is cuffed (player: `BeginPlayerTurn` with `playerCuffed` and not about to break free; dealer: `DealerCheckHandCuffs`). A turn can be announced twice in a row (the game calls `BeginDealerTurn` twice after a player cuff skip); the `sync` that follows is identical, so the adapter must be idempotent. Always followed by a `sync`. |
| `shot` | `actor, target, shell, damage` | One per trigger pull, sent when the shell is first revealed (`DeathManager.Kill` for live, `ShellEjectManager.EjectShell` for blank). `target` is `"self"` or `"opponent"`. `damage` is `currentShotgunDamage` read at the aim (2 when sawed). |
| `item` | `actor, item, stolen` | Every item use. Adrenaline sends the adrenaline `item` first, then the stolen item with `stolen=true`. |
| `eject` | `actor, cause:"beer", shell` | Beer reveal. |
| `glass` | `shell` | Own glass only (including a stolen one). The dealer's glass sends only its `item`. |
| `phone` | `slot, shell` | Own phone, decoded with the game's own translations. `slot` 1..6 = shells after the chamber (1 = next shell, the "second shell" line). `slot:null, shell:null` for the one-shell-left line. |
| `medicine` | `actor, healed` | From `HealthCounter.UpdateDisplayRoutineCigarette_Player/_Enemy` when `overriding_medicine` is set; `healed` = `overriding_medicine_adding`. Cigarettes send only their `item`. |
| `wire_cut` | `who` | `DefibCutter.CutWire` entry. Campaign only; not exercised in testing (endless never cuts). |
| `sync` | `hp:[p,d], max_hp, cuffed:[p,d], sawed, damage, left, turn, items:{player:{...},dealer:{...}}, hp_hidden:[p,d]` | After every `turn`. `left` = shells remaining (count only). `hp_hidden` = `wireIsCut_*`. The adapter compares it to `Session.state`. |
| `end` | `who` (the side that reached 0 HP) | Once per round (the game calls `OutOfHealth` twice for a dealer death; the mod dedupes). |
| `gap` | `dropped` | Events were dropped while the calc was not connected or not reading. |

Dropped from the earlier draft: `bye`; the dealer rack order (E18); adrenaline-expired (E19). `ev` is the discriminator (the test listener checks `v` and `ev`).

Example (from a real headless run):

```
{"v":1,"ev":"hello","seq":1,"t":3100,"probe":"0.2.0","godot":"4.1","game_ver":"0.0.0","mode":"endless","scripts_sha":"7482...","scripts":{"RoundManager.gd":"4b7a...","ItemInteraction.gd":"1f02..."}}
{"v":1,"ev":"load","seq":2,"t":7911,"kind":"new_round","live":4,"blank":4,"hp":[3,3],"max_hp":3,"items":{"player":{"burner phone":1,"cigarettes":1},"dealer":{"handcuffs":1,"handsaw":1}},"first_turn":"player"}
{"v":1,"ev":"shot","seq":3,"t":8258,"actor":"player","target":"self","shell":"blank","damage":1}
{"v":1,"ev":"phone","seq":7,"t":8583,"slot":6,"shell":"blank"}
```

Version rule: the calc rejects a `v` it does not know with `{"v":1,"ev":"hello_ack","ok":false}` and closes. Adding optional fields does not bump `v`.

Adapter ordering rules (short):

1. Apply events in `seq` order, one at a time.
2. `item` for beer, glass, phone, medicine, or a stolen version of those waits for its outcome event (`eject`, `glass`, `phone`, `medicine`) before calling `Session`.
3. A `sync` that disagrees with `Session.state` on hp, cuffs, saw, turn or items is shown as a banner, with a one-key "apply" that calls the matching `edit_*`.
4. The adapter never calls `edit_chamber` or `edit_dealer_knows`.
5. Display gating (calc only, no extra messages): events apply and the solver starts at once, but the screen keeps the last released state until each event would be visible: arrival time + `timing.reveal_offset(event)` * `timing.REVEAL_SCALE` (offset table and ASSUMED entries in `buckshotcalc/tui/timing.py`; scale 0 turns gating off). The gate is the latest pending release time, so a later event extends it. Events without `seq` never gate. The mod`s `t` (ms) on every message is for calibrating the offsets from a `--record` file.

## 7. Probe fixes (all done in `Calc-Probe` 0.2.0, 2026-10-25)

1-9 and 11 of the earlier list are implemented as described in section 6 (hello with hashes and mode, merged `load`, one `shot` at reveal, `stolen`, phone decode, medicine via `overriding_medicine_adding`, `wire_cut`, `sync` at every turn replacing the 0.1 s poller, `cuffed_skip`). Item 10: the bot and perf code moved to a separate mod, `bridge/bot-src/.../Calc-Bot`; the bridge mod has no `_bot_*`, no `BR_BOT`, and reads `sequenceArray` in two commented helpers only. Item 11: the file log is gone; `BR_BRIDGE_DEBUG=1` prints each line to stdout.

## 8. Open questions

### Settled by the operator, 2026-10-25

- **Port and who listens (8):** the calc listens on TCP 127.0.0.1:47600; the mod connects and retries quietly (the game never stalls or errors when the calc is not running).
- **Unknown game hash (5):** warn and continue. The calc shows a banner; the mod just sends the hashes in `hello`.
- **Campaign support (2):** the mod still sends `hello` with the mode; the calc refuses `campaign`. Support is a future milestone.
- **Desync policy (4):** on a mismatch the calc asks the operator (calc side). The mod sends a `sync` snapshot of player-visible state at each turn start.
- **Dealer steals our glass or phone (3):** counts as a dealer info action (calc side). The mod only marks the actor and the `stolen` flag.
- **Target game:** the test copy in `_ref/br-test/game` for now; Steam later.

### Still open

1. **Saw and beer.** The game keeps the saw through a beer and clears it at turn end. SPEC 3.2 / `rules.toml` say saw plus beer wastes it. Do you want this treated as a rules correction? It changes the solver and many tests.
6. **Dealer rack order (E18).** Send it now for the later work on SPEC 3.4, or leave it out?
7. **Player phone clamp.** The player's phone never names shell 8 and favours shell 7 in a full chamber. Does the engine's phone branch weighting need to match, or is it ignorable?
