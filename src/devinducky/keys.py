"""Pad actions and the hotkeys the pad types to signal them.

This table is the single source of truth: `gen-profile` renders the duckyScript
for each key from it, and the input listener decodes hotkeys with it.

duckyPad Pro key IDs: 1-20 are keys (top-left = 1, row-major, 4 per row),
21-23 upper encoder CW/CCW/push, 24-26 lower encoder CW/CCW/push.
The upper encoder sends nothing to the daemon: its push holds modifiers for
another app's push-to-talk (see `wheel_hold` in config.py).
LED index = key ID - 1.
"""

from dataclasses import dataclass

NUM_SLOTS = 12
NUM_LAUNCH = 3

SLOT_KEYS = list(range(1, 13))          # rows 0-2
LAUNCH_KEYS = list(range(13, 16))       # row 3
KEY_RESET = 16
KEY_OPEN, KEY_DASH, KEY_REFRESH, KEY_INFO = 17, 18, 19, 20
ENC1_CW, ENC1_CCW, ENC1_PUSH = 21, 22, 23
ENC2_CW, ENC2_CCW, ENC2_PUSH = 24, 25, 26

# Modifier sets x F13-F20. Every combo includes at least two modifiers so it
# never collides with brightness/media handling of bare F-keys on macOS.
MODIFIER_SETS = [
    ("ctrl", "alt"),
    ("ctrl", "shift"),
    ("alt", "shift"),
    ("ctrl", "alt", "shift"),
    ("ctrl", "alt", "cmd"),
]
FKEYS = [f"f{n}" for n in range(13, 21)]

DUCKY_MOD = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "cmd": "COMMAND"}


@dataclass(frozen=True)
class Event:
    kind: str          # "down" | "up" | "press"
    key: int           # duckyPad key ID


@dataclass(frozen=True)
class Hotkey:
    mods: tuple
    fkey: str

    def ducky(self):
        return " ".join([DUCKY_MOD[m] for m in self.mods] + [self.fkey.upper()])

    def __str__(self):
        return "+".join(self.mods + (self.fkey,))


def _events():
    """Every event the pad can emit, in a fixed order (hotkeys are assigned in this order)."""
    evs = []
    for k in SLOT_KEYS:                 # slots need down AND up to time a hold
        evs += [Event("down", k), Event("up", k)]
    for k in LAUNCH_KEYS + [KEY_RESET, KEY_OPEN, KEY_DASH, KEY_REFRESH]:
        evs.append(Event("press", k))
    evs.append(Event("press", ENC2_PUSH))
    return evs


def _combos():
    for mods in MODIFIER_SETS:
        for f in FKEYS:
            yield Hotkey(mods, f)


EVENTS = _events()
HOTKEY_FOR = dict(zip(EVENTS, _combos(), strict=False))
EVENT_FOR = {hk: ev for ev, hk in HOTKEY_FOR.items()}
assert len(HOTKEY_FOR) == len(EVENTS), "not enough hotkey combos for all events"


def slot_of(key):
    """Key ID -> slot index 0-11, or None."""
    return key - 1 if key in SLOT_KEYS else None


def launch_of(key):
    return key - LAUNCH_KEYS[0] if key in LAUNCH_KEYS else None


def led_of_slot(slot):
    return slot  # key ID slot+1 -> LED index slot


def led_of_key(key):
    return key - 1
