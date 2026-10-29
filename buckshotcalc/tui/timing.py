"""When the player SEES an event, relative to when the mod sent it (docs/live-bridge.md).

Offsets are seconds after the event's arrival. Source: the game decompile in
`_ref/buckshotroulette/` (timers summed from `create_timer` calls; scene clip lengths are
packed in the game binary and were not read, hence the ASSUMED values).
ASSUMED = a guess, to be calibrated from a `--record` file using the mods `t` field.
"""

from __future__ import annotations

REVEAL_SCALE = 1.0  # 0 turns display gating off; settings will expose it later

# Player item use, by item name. Anything not listed is 0.0: beer, burner phone, medicine,
# glass and adrenaline are shown by their own outcome event (eject / phone / medicine / glass).
PLAYER_ITEM = {
    "handcuffs": 1.0,  # ItemInteraction.gd:118-129; visible at 1.0 s, returns 4.2 s
    "cigarettes": 3.5,  # ASSUMED: ItemInteraction.gd:147-152, heal mid-clip, returns 5.0 s
    "handsaw": 1.5,  # ASSUMED: ItemInteraction.gd:153-158, returns 4.48 s
    "inverter": 1.5,  # ASSUMED: ItemInteraction.gd:165-171, returns 3.2 s
}
DEALER_ITEM = 1.5  # ASSUMED: HandManager.gd:52-127, lerpDuration and clip length unknown
PHONE = 1.5  # ASSUMED: DialogueManager.gd:59-69 types it out, hint at the end
MEDICINE = 0.8  # HealthCounter.gd:158-166
LIVE_SHOT = 3.0  # DeathManager.gd:35-112: HP display 3.0 s (dealer) / 3.13 s (player) after Kill
WIRE_CUT = 4.0  # DefibCutter.gd:52-74, blade closes at 4.0 s


def reveal_offset(event: dict) -> float:
    """Seconds after arrival until the screen shows this event's effect (before scaling)."""
    ev = event.get("ev")
    if ev == "shot":
        return LIVE_SHOT if event.get("shell") == "live" else 0.0
    if ev == "item":
        if event.get("actor") == "dealer":
            return DEALER_ITEM
        return PLAYER_ITEM.get(event.get("item"), 0.0)
    return {"phone": PHONE, "medicine": MEDICINE, "wire_cut": WIRE_CUT}.get(ev, 0.0)
