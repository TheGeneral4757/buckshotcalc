extends Node

# TEST BOT (off unless BR_BOT=1). Plays endless runs by calling the same
# functions a mouse click reaches, gated on the same interactionAllowed
# flags a human is gated by. Env: BR_BOT_GAMES (default 1), BR_BOT_SPEED
# (Engine.time_scale, default 8), BR_BOT_POLICY (oracle|random).
# The oracle policy PEEKS AT THE HIDDEN CHAMBER. That is a test-harness choice;
# this mod must never be shipped with the bridge (Calc-Probe).

const S := "res://scripts/"
const MAIN_SCENE := "res://scenes/main.tscn"
var _rm_cache: Object = null
var _bot := false
var _bot_started := false
var _bot_games := 0
var _bot_max_games := 1
var _bot_oracle := true
var _bot_last := 0
var _last_log_ms := 0
var _bot_dead_at := 0
var _bot_speed := 8.0
var _bot_fps := 0
var _bot_sleep_usec := 0
var _pf_bot := 0
var _pf_frames := 0
var _pf_last := 0

func _init() -> void:
	ModLoaderMod.add_hook(h_out_of_health, S + "RoundManager.gd", "OutOfHealth")
	ModLoaderMod.add_hook(h_dealer_turn, S + "DealerIntelligence.gd", "BeginDealerTurn")
	ModLoaderMod.add_hook(h_dealer_turn, S + "DealerIntelligence.gd", "DealerCheckHandCuffs")
	ModLoaderMod.add_hook(h_player_turn, S + "RoundManager.gd", "BeginPlayerTurn")
	ModLoaderMod.add_hook(h_spawn, S + "ShellSpawner.gd", "SpawnShells")

# The game leaves the player's item flags set while the dealer plays, so a click-bot would use items
# out of turn (a human cannot: no cursor). Track whose turn it is the way the probe does.
var _dealer_turn := false
func h_dealer_turn(chain: ModLoaderHookChain):
	_dealer_turn = true
	return await chain.execute_next_async([])

func h_player_turn(chain: ModLoaderHookChain):
	_dealer_turn = false
	return await chain.execute_next_async([])

func h_spawn(chain: ModLoaderHookChain, n: int, lives: int, blanks: int, shuffling: bool):
	_dealer_turn = false
	return await chain.execute_next_async([n, lives, blanks, shuffling])

func h_out_of_health(chain: ModLoaderHookChain, who: String):
	if _bot and who == "player": _bot_dead_at = Time.get_ticks_msec()
	return await chain.execute_next_async([who])

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

func _shell() -> String:
	var rm = _rm()
	if rm == null: return "?"
	var a = rm.get("shellSpawner").get("sequenceArray")
	return str(a[0]) if a != null and a.size() > 0 else "?"

func _process(_delta: float) -> void:
	if not _bot: return
	var t0 := Time.get_ticks_usec()
	_bot_tick()
	_pf_bot += Time.get_ticks_usec() - t0
	_pf_frames += 1
	var now := Time.get_ticks_msec()
	if now - _pf_last > 5000:
		if _pf_last > 0 and _pf_frames > 0:
			_log("PERF fps=%d frames=%d nodes=%d scale=%s | per-frame ms: bot=%.2f" % [
				Performance.get_monitor(Performance.TIME_FPS), _pf_frames,
				Performance.get_monitor(Performance.OBJECT_NODE_COUNT), Engine.time_scale,
				_pf_bot / 1000.0 / _pf_frames])
		_pf_last = now
		_pf_bot = 0
		_pf_frames = 0

func _ready() -> void:
	if OS.get_environment("BR_BOT") != "1": return
	_bot = true
	var g := OS.get_environment("BR_BOT_GAMES")
	_bot_max_games = int(g) if g != "" else 1
	var sp := OS.get_environment("BR_BOT_SPEED")
	_bot_speed = float(sp) if sp != "" else 8.0
	var ff := OS.get_environment("BR_BOT_FPS")
	_bot_fps = int(ff) if ff != "" else 0
	var su := OS.get_environment("BR_BOT_SLEEP_USEC")
	_bot_sleep_usec = int(su) if su != "" else 0
	Engine.time_scale = _bot_speed
	_bot_oracle = OS.get_environment("BR_BOT_POLICY") != "random"
	_last_log_ms = Time.get_ticks_msec()
	_log("BOT enabled games=%d speed=%s oracle=%s" % [_bot_max_games, Engine.time_scale, _bot_oracle])

func _branches(n: Node, out: Array) -> void:
	if n.get("interactionAlias") != null: out.append(n)
	for c in n.get_children(): _branches(c, out)

func _bot_tick() -> void:
	Engine.time_scale = _bot_speed   # the game resets it to 1 on every main-scene load
	Engine.max_fps = _bot_fps
	OS.low_processor_usage_mode = false
	OS.low_processor_usage_mode_sleep_usec = _bot_sleep_usec
	AudioServer.playback_speed_scale = _bot_speed
	var tree := Engine.get_main_loop() as SceneTree
	var now := Time.get_ticks_msec()
	if now - _last_log_ms > 90000:
		_log("BOT STUCK (no events for 90s) scene=%s" % (tree.current_scene.scene_file_path if tree.current_scene else "none"))
		var bs := []
		_branches(tree.root, bs)
		for b in bs:
			if b.get("interactionAllowed"): _log("  allowed branch alias='%s' item='%s' grid=%s" % [b.get("interactionAlias"), b.get("itemName"), b.get("isGrid")])
		tree.quit(2)
		return
	if now - _bot_last < 350: return
	_bot_last = now
	var scene := tree.current_scene
	if scene == null: return
	var dead_for := now - _bot_dead_at if _bot_dead_at > 0 else 0
	if _bot_started and (scene.scene_file_path != MAIN_SCENE or dead_for > 6000):
		_bot_started = false
		_bot_dead_at = 0
		_bot_games += 1
		_log("BOT GAME OVER (scene=%s) games=%d/%d" % [scene.scene_file_path, _bot_games, _bot_max_games])
		if _bot_games >= _bot_max_games:
			tree.quit(0)
			return
		_bot_last = now + 1500
		tree.change_scene_to_file(MAIN_SCENE)
		return
	if _bot_started and _bot_dead_at > 0: return   # player is dead: do nothing, like a human
	if scene.scene_file_path != MAIN_SCENE:
		_bot_last = now + 1500
		tree.change_scene_to_file(MAIN_SCENE)
		return
	if not _bot_started:
		_bot_boot(tree)
		return
	_bot_play(tree)

func _bot_boot(tree: SceneTree) -> void:
	_rm_cache = null
	var rm = _rm()
	var intro = _find_with(tree.root, ["endlessmode"])
	if rm == null or intro == null: return
	var pd = rm.get("playerData")
	var em = intro.get("endlessmode")
	if pd == null or em == null: return
	pd.set("hasSignedWaiver", true)
	em.call("SetupEndless")
	_bot_started = true
	_bot_last = Time.get_ticks_msec() + 1500
	_log("BOT RUN START game %d" % (_bot_games + 1))
	intro.call("BeginGame")

func _bot_play(tree: SceneTree) -> void:
	var im = _find_with(tree.root, ["itemInteraction", "fs_dec"])
	if im == null: return
	var bs := []
	_branches(tree.root, bs)
	var ii = im.get("itemInteraction")
	# 1) double or nothing: always double so the run continues
	for b in bs:
		if b.get("interactionAlias") == "double yes" and b.get("interactionAllowed"):
			_log("BOT double yes")
			im.call("InteractWith", "double yes")
			return
	# 2) steal phase after adrenaline: take any dealer item
	if ii != null and ii.get("stealing"):
		var steal := []
		for b in bs:
			if b.get("interactionAlias") == "item" and b.get("interactionAllowed") and not b.get("isPlayerSide") and b.get("itemName") != "":
				steal.append(b)
		if steal.size() > 0:
			var sb = steal[randi() % steal.size()]
			_log("BOT steals %s" % sb.get("itemName"))
			ii.call("PickupItemFromTable", sb.get_parent(), sb.get("itemName"))
			return
	# 3) item grabbing from the briefcase, then place it on a free grid slot
	for b in bs:
		if b.get("interactionAlias") == "briefcase intake" and b.get("interactionAllowed"):
			im.call("InteractWith", "briefcase intake")
			return
	for b in bs:
		if b.get("isGrid") and b.get("interactionAllowed") and not b.get("interactionInvalid"):
			im.call("InteractWithGrid", b.get("gridIndex"))
			return
	# 4) shooting choice (after the shotgun was picked up)
	for b in bs:
		var a = b.get("interactionAlias")
		if (a == "text dealer" or a == "text you") and b.get("interactionAllowed") and not im.get("fs_dec"):
			var target := _bot_pick_target()
			_log("BOT shoots %s" % target)
			im.call("InteractWith", "text dealer" if target == "dealer" else "text you")
			return
	# 5) player's turn: maybe use an item, else grab the shotgun
	if _dealer_turn: return
	for b in bs:
		if b.get("interactionAlias") == "shotgun" and b.get("interactionAllowed"):
			var usable := []
			for c in bs:
				if c.get("interactionAlias") == "item" and c.get("isPlayerSide") and c.get("interactionAllowed") and not c.get("interactionInvalid") and c.get("itemName") != "":
					usable.append(c)
			if usable.size() > 0 and randf() < 0.55 and ii != null:
				var ub = usable[randi() % usable.size()]
				_log("BOT uses item %s" % ub.get("itemName"))
				ii.call("PickupItemFromTable", ub.get_parent(), ub.get("itemName"))
				return
			_log("BOT grabs shotgun")
			im.call("InteractWith", "shotgun")
			return

func _bot_pick_target() -> String:
	if _bot_oracle and randf() > 0.15:
		return "self" if _shell() == "blank" else "dealer"
	return "dealer" if randf() < 0.5 else "self"

func _log(s: String) -> void:
	_last_log_ms = Time.get_ticks_msec()
	print("[bot] ", s)
