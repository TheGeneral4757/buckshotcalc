extends Node

# BuckshotCalc live bridge (mod side). Connects to the calc on 127.0.0.1:47600
# and writes one JSON object per line (docs/live-bridge.md section 6).
# Only what a human at the table can see is sent. No bot or test code lives here.
# Set BR_BRIDGE_DEBUG=1 to also print every line to stdout.

const PORT := 47600
const PROBE_VER := "0.2.0"
const S := "res://scripts/"
const HASHED := ["RoundManager", "ItemInteraction", "DealerIntelligence", "ShotgunShooting",
	"HealthCounter", "ShellEjectManager", "BurnerPhone", "MedicineManager", "HandManager"]
const OUT_CAP := 262144

var _peer := StreamPeerTCP.new()
var _up := false
var _out := PackedByteArray()
var _retry_at := 0
var _seq := 0
var _dropped := 0
var _mid_line := false   # the last partial send ended inside a line
var _hello_sent := false
var _round_seen := false
var _debug := OS.get_environment("BR_BRIDGE_DEBUG") == "1"
var _rm_cache: Object = null
var _im_cache: Object = null
var _kind := "new_round"
var _ended := false
var _shot := {}            # actor/target/damage of the trigger pull in flight
var _phone_wait := false   # SendDialogue is running: next dialogue line is the phone answer

func _init() -> void:
	ModLoaderMod.add_hook(h_start_round, S + "RoundManager.gd", "StartRound")
	ModLoaderMod.add_hook(h_player_turn, S + "RoundManager.gd", "BeginPlayerTurn")
	ModLoaderMod.add_hook(h_out_of_health, S + "RoundManager.gd", "OutOfHealth")
	ModLoaderMod.add_hook(h_dealer_turn, S + "DealerIntelligence.gd", "BeginDealerTurn")
	ModLoaderMod.add_hook(h_dealer_cuff_check, S + "DealerIntelligence.gd", "DealerCheckHandCuffs")
	ModLoaderMod.add_hook(h_spawn, S + "ShellSpawner.gd", "SpawnShells")
	ModLoaderMod.add_hook(h_shoot, S + "ShotgunShooting.gd", "Shoot")
	ModLoaderMod.add_hook(h_dealer_shoot, S + "DealerIntelligence.gd", "Shoot")
	ModLoaderMod.add_hook(h_kill, S + "DeathManager.gd", "Kill")
	ModLoaderMod.add_hook(h_eject_shot, S + "ShellEjectManager.gd", "EjectShell")
	ModLoaderMod.add_hook(h_eject_beer_player, S + "ShellEjectManager.gd", "BeerEjection_player")
	ModLoaderMod.add_hook(h_eject_beer_dealer, S + "ShellEjectManager.gd", "BeerEjection_dealer")
	ModLoaderMod.add_hook(h_player_item, S + "ItemInteraction.gd", "InteractWith")
	ModLoaderMod.add_hook(h_dealer_item, S + "HandManager.gd", "PickupItemFromTable")
	ModLoaderMod.add_hook(h_magnifier, S + "ShellExamine.gd", "SetupShell")
	ModLoaderMod.add_hook(h_phone, S + "BurnerPhone.gd", "SendDialogue")
	ModLoaderMod.add_hook(h_dialogue, S + "DialogueManager.gd", "ShowText_Forever")
	ModLoaderMod.add_hook(h_med_player, S + "HealthCounter.gd", "UpdateDisplayRoutineCigarette_Player")
	ModLoaderMod.add_hook(h_med_dealer, S + "HealthCounter.gd", "UpdateDisplayRoutineCigarette_Enemy")
	ModLoaderMod.add_hook(h_wire_cut, S + "DefibCutter.gd", "CutWire")

# ---------- socket: never blocks the game, never errors when nobody listens
func _process(_delta: float) -> void:
	var now := Time.get_ticks_msec()
	_peer.poll()
	var st := _peer.get_status()
	if st == StreamPeerTCP.STATUS_CONNECTED:
		if not _up:
			_up = true
			_seq = 0
			_hello_sent = false
		_pump(now)
	elif st == StreamPeerTCP.STATUS_NONE or st == StreamPeerTCP.STATUS_ERROR:
		_up = false
		_out.clear()
		_mid_line = false
		if now >= _retry_at:
			_retry_at = now + 2000
			_peer.disconnect_from_host()
			_peer.connect_to_host("127.0.0.1", PORT)

func _pump(now: int) -> void:
	# hello waits for the first StartRound: only then is endless/campaign decided
	if not _hello_sent and _round_seen:
		var rm = _rm()
		if rm != null:
			_hello_sent = true
			_hello(rm)
	if _out.size() > 0:
		var r := _peer.put_partial_data(_out)
		if r[0] == OK and r[1] > 0:
			_mid_line = _out[r[1] - 1] != 10
			_out = _out.slice(r[1])
	if _peer.get_available_bytes() > 0:   # hello_ack etc.: only look for an unknown-hash warning
		for l in _peer.get_utf8_string(_peer.get_available_bytes()).split("\n", false):
			var m = JSON.parse_string(l)
			if m is Dictionary and m.get("ev") == "hello_ack" and m.get("known") == false:
				push_warning("[bridge] calc does not know these game script hashes; continuing")

func _send(ev: String, d: Dictionary = {}) -> void:
	if not _up or not _hello_sent and ev != "hello": return   # nobody listening: drop, never queue forever
	_seq += 1
	d["v"] = 1
	d["ev"] = ev
	d["seq"] = _seq
	d["t"] = Time.get_ticks_msec()
	var line := JSON.stringify(d)
	if _debug: print("[bridge] ", line)
	if _out.size() > OUT_CAP:
		# keep the rest of a line already half sent, or the calc reads a cut-off line
		var nl := _out.find(10) if _mid_line else -1
		_out = _out.slice(0, nl + 1) if nl >= 0 else PackedByteArray()
		_dropped += 1
	_out.append_array((line + "\n").to_utf8_buffer())

func _hello(rm: Object) -> void:
	var scripts := {}
	var cat := ""
	for n in HASHED:
		scripts[n + ".gd"] = FileAccess.get_sha256(S + n + ".gd")
	var keys := scripts.keys()
	keys.sort()
	for k in keys: cat += scripts[k]
	var vi := Engine.get_version_info()
	_send("hello", {"probe": PROBE_VER, "godot": "%d.%d" % [vi.major, vi.minor],
		"game_ver": str(ProjectSettings.get_setting("application/config/version", "0.0.0")),
		"mode": "endless" if rm.get("endless") else "campaign",
		"scripts": scripts, "scripts_sha": cat.sha256_text()})
	if _dropped > 0:
		_send("gap", {"dropped": _dropped})
		_dropped = 0

# ---------- finding the game objects (hooked scripts don't all link to them)
func _find_with(n: Node, props: Array) -> Node:
	var ok := true
	for p in props:
		if n.get(p) == null:
			ok = false
			break
	if ok: return n
	for c in n.get_children():
		var r = _find_with(c, props)
		if r != null: return r
	return null

func _rm() -> Object:
	if _rm_cache != null and is_instance_valid(_rm_cache): return _rm_cache
	_rm_cache = _find_with(Engine.get_main_loop().root, ["health_player", "shellSpawner"])
	return _rm_cache

func _im() -> Object:
	if _im_cache != null and is_instance_valid(_im_cache): return _im_cache
	_im_cache = _find_with(Engine.get_main_loop().root, ["itemSpawnParent", "itemArray_dealer"])
	return _im_cache

# Chamber reads. The only two places sequenceArray is touched in this mod.
# Size only: the player always sees how many shells are left.
func _left() -> int:
	var rm = _rm()
	if rm == null: return 0
	var arr = rm.get("shellSpawner").get("sequenceArray")
	return arr.size() if arr != null else 0

# Slot 0 only, and only called from hooks that run at the moment the game shows
# that shell on screen (ejection, magnifier). Never call this from anywhere else.
func _revealed_shell() -> String:
	var rm = _rm()
	if rm == null: return "?"
	var arr = rm.get("shellSpawner").get("sequenceArray")
	return str(arr[0]) if arr != null and arr.size() > 0 else "?"

# Items on the table per side (what the player sees on the grid), as name -> count.
func _items() -> Dictionary:
	var out := {"player": {}, "dealer": {}}
	var im = _im()
	var parent = im.get("itemSpawnParent") if im != null else null
	if parent == null: return out
	for c in parent.get_children():
		if c.get_child_count() < 2: continue
		var br = c.get_child(1)
		if br == null or br.get("itemName") == null or br.get("itemName") == "": continue
		var side: Dictionary = out["player" if br.get("isPlayerSide") else "dealer"]
		side[br.get("itemName")] = side.get(br.get("itemName"), 0) + 1
	return out

func _max_hp(rm: Object) -> int:
	return rm.get("roundArray")[0].startingHealth   # the game's own heal cap (HealthCounter)

func _sync(who: String) -> void:
	var rm = _rm()
	if rm == null: return
	_send("sync", {"hp": [rm.get("health_player"), rm.get("health_opponent")], "max_hp": _max_hp(rm),
		"cuffed": [rm.get("playerCuffed"), rm.get("dealerCuffed")], "sawed": rm.get("barrelSawedOff"),
		"damage": rm.get("currentShotgunDamage"), "left": _left(), "turn": who, "items": _items(),
		"hp_hidden": [rm.get("wireIsCut_player"), rm.get("wireIsCut_dealer")]})

func _turn(who: String, cuffed_skip: bool) -> void:
	_send("turn", {"who": who, "cuffed_skip": cuffed_skip})
	_sync(who)

# ---------- round / turn flow
func h_start_round(chain: ModLoaderHookChain, getting_next: bool):
	_round_seen = true
	_ended = false
	_kind = "reload" if getting_next else "new_round"
	return await chain.execute_next_async([getting_next])

# Runs after the items are dealt, so this is the one place the whole load is known.
func h_spawn(chain: ModLoaderHookChain, n: int, lives: int, blanks: int, shuffling: bool):
	var r = await chain.execute_next_async([n, lives, blanks, shuffling])
	var rm = _rm()
	if rm != null:
		_send("load", {"kind": _kind, "live": lives, "blank": blanks,
			"hp": [rm.get("health_player"), rm.get("health_opponent")], "max_hp": _max_hp(rm),
			"items": _items(), "first_turn": "player"})
	return r

func h_player_turn(chain: ModLoaderHookChain):
	var rm = chain.reference_object
	_turn("player", rm.get("playerCuffed") and not rm.get("playerAboutToBreakFree"))
	return await chain.execute_next_async([])

func h_dealer_turn(chain: ModLoaderHookChain):
	_turn("dealer", false)
	return await chain.execute_next_async([])

func h_dealer_cuff_check(chain: ModLoaderHookChain):
	_turn("dealer", true)
	return await chain.execute_next_async([])

func h_out_of_health(chain: ModLoaderHookChain, who: String):
	if not _ended:   # the game calls OutOfHealth twice for a dealer death
		_ended = true
		_send("end", {"who": who})
	return await chain.execute_next_async([who])

# ---------- shots: remember the pull at AIMS, emit once when the shell is revealed
func _aim(actor: String, at_self: bool) -> void:
	var rm = _rm()
	_shot = {"actor": actor, "target": "self" if at_self else "opponent",
		"damage": rm.get("currentShotgunDamage") if rm != null else 1}

func _reveal(shell: String) -> void:
	if _shot.is_empty(): return
	_shot["shell"] = shell
	var d := _shot
	_shot = {}
	_send("shot", d)

func h_shoot(chain: ModLoaderHookChain, who: String):
	_aim("player", who == "self")
	return await chain.execute_next_async([who])

func h_dealer_shoot(chain: ModLoaderHookChain, who: String):
	_aim("dealer", who == "self")
	return await chain.execute_next_async([who])

# Kill is only ever called for a live shell (blanks never reach it), fatal or not.
func h_kill(chain: ModLoaderHookChain, who: String, true_death: bool, returning: bool):
	if not true_death: _reveal("live")
	return await chain.execute_next_async([who, true_death, returning])

# Blank shots end here. Live ones were already sent at Kill, so _shot is empty.
func h_eject_shot(chain: ModLoaderHookChain):
	if not _shot.is_empty(): _reveal(_revealed_shell())   # ejection is on screen now
	return await chain.execute_next_async([])

func h_eject_beer_player(chain: ModLoaderHookChain):
	_send("eject", {"actor": "player", "cause": "beer", "shell": _revealed_shell()})   # beer shows the shell
	return await chain.execute_next_async([])

func h_eject_beer_dealer(chain: ModLoaderHookChain):
	_send("eject", {"actor": "dealer", "cause": "beer", "shell": _revealed_shell()})   # beer shows the shell
	return await chain.execute_next_async([])

# ---------- items
func h_player_item(chain: ModLoaderHookChain, item_name: String):
	_send("item", {"actor": "player", "item": item_name, "stolen": bool(chain.reference_object.get("stealing"))})
	return await chain.execute_next_async([item_name])

func h_dealer_item(chain: ModLoaderHookChain, item_name: String):
	_send("item", {"actor": "dealer", "item": item_name, "stolen": bool(chain.reference_object.get("stealing"))})
	return await chain.execute_next_async([item_name])

# Only runs for the player's own glass (including a stolen one).
func h_magnifier(chain: ModLoaderHookChain):
	_send("glass", {"shell": _revealed_shell()})   # the glass shows this shell to the player
	return await chain.execute_next_async([])

func h_phone(chain: ModLoaderHookChain):
	_phone_wait = true
	var r = await chain.execute_next_async([])
	_phone_wait = false
	return r

# Decode the phone line with the game's own translations (any language). No chamber read.
func h_dialogue(chain: ModLoaderHookChain, text: String):
	if _phone_wait:
		_phone_wait = false
		if text == tr("UNFORTUNATE"):
			_send("phone", {"slot": null, "shell": null})
		else:
			var hit := false
			for i in range(1, 7):
				for s in ["live", "blank"]:
					var tail := tr(tr("LIVEROUND" if s == "live" else "BLANKROUND") % "")
					if text == tr(tr("SEQUENCE%d" % (i + 1))) + "\n" + "... " + tail:
						_send("phone", {"slot": i, "shell": s})
						hit = true
			if not hit: push_warning("[bridge] could not decode phone line")
	return await chain.execute_next_async([text])

# Both seats set overriding_medicine(_adding) just before this call; plain cigarettes leave it false.
func h_med_player(chain: ModLoaderHookChain):
	_medicine("player", chain.reference_object)
	return await chain.execute_next_async([])

func h_med_dealer(chain: ModLoaderHookChain):
	_medicine("dealer", chain.reference_object)
	return await chain.execute_next_async([])

func _medicine(actor: String, counter: Object) -> void:
	if counter.get("overriding_medicine"):
		_send("medicine", {"actor": actor, "healed": bool(counter.get("overriding_medicine_adding"))})

func h_wire_cut(chain: ModLoaderHookChain, who: String):
	_send("wire_cut", {"who": who})
	return await chain.execute_next_async([who])
