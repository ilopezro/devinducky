"""Build LED frames and push only what changed to the pad."""

import logging
import math
import threading
import time
from dataclasses import dataclass

from . import hid

log = logging.getLogger(__name__)

WHITE = (255, 255, 255)
OFF = (0, 0, 0)


def scale(rgb, k):
    return tuple(int(round(c * k)) for c in rgb)


def mix(a, b, k):
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))


def pulse(rgb, period_s, t, floor=0.15):
    """Smooth breathe between floor and full brightness."""
    k = floor + (1 - floor) * (0.5 + 0.5 * math.cos(2 * math.pi * t / period_s))
    return scale(rgb, k)


@dataclass
class Effect:
    kind: str            # "flash" | "solid" | "blink" | "fade"
    color: tuple
    start: float
    until: float
    period: float = 0.25

    def apply(self, base, t):
        if self.kind == "solid":
            return self.color
        if self.kind == "flash":       # on/off at `period`, ending on the base colour
            on = int((t - self.start) / self.period) % 2 == 0
            return self.color if on else OFF
        if self.kind == "blink":       # armed: alternate with the base colour
            on = int((t - self.start) / self.period) % 2 == 0
            return self.color if on else base
        if self.kind == "fade":        # from color to base over the effect's life
            k = (t - self.start) / max(1e-6, self.until - self.start)
            return mix(self.color, base, min(1, k))
        return base


class Frame:
    """Desired look for every LED; animated parts are evaluated at time t."""

    def __init__(self):
        self.base = [OFF] * hid.NUM_LEDS
        self.pulses = {}         # led -> period_s
        self.effects = {}        # led -> Effect
        self._lock = threading.Lock()

    def set_effect(self, led, kind, color, duration, period=0.25):
        now = time.monotonic()
        with self._lock:
            self.effects[led] = Effect(kind, tuple(color), now, now + duration, period)

    def clear_effect(self, led):
        with self._lock:
            self.effects.pop(led, None)

    def animating(self, t):
        return bool(self.pulses) or any(e.until > t for e in list(self.effects.values()))

    def evaluate(self, t):
        with self._lock:
            self.effects = {k: e for k, e in self.effects.items() if e.until > t}
            effects = dict(self.effects)
        out = []
        for i, base in enumerate(self.base):
            c = pulse(base, self.pulses[i], t) if i in self.pulses else base
            if i in effects:
                c = effects[i].apply(c, t)
            out.append(tuple(c))
        return out


class Renderer:
    """Owns pad I/O: diffs frames, batches writes, repaints everything periodically."""

    def __init__(self, resync_s=10, pad_profile=0):
        self.sent = [None] * hid.NUM_LEDS
        self.sent_gvs = {}
        self.resync_s = resync_s
        self.pad_profile = pad_profile
        self.last_full = 0.0
        self.connected = False

    def invalidate(self, led):
        """The pad repaints a key itself on press/release; resend our colour for it."""
        if 0 <= led < hid.NUM_LEDS:
            self.sent[led] = None

    def push(self, leds, gvs, now):
        if now - self.last_full > self.resync_s:
            self.sent = [None] * hid.NUM_LEDS       # profile switches repaint LEDs on the pad
            self.sent_gvs = {}
            self.last_full = now
        changed = [(i, c) for i, c in enumerate(leds) if self.sent[i] != c]
        gv_changed = {k: v for k, v in gvs.items() if self.sent_gvs.get(k) != v}
        if not changed and not gv_changed:
            return

        def write(pad):
            if self.pad_profile:
                info = pad.info()
                if info.profile != self.pad_profile:
                    return False
            for i, c in changed:
                pad.set_led(i, c)
                self.sent[i] = c
            if gv_changed:
                pad.write_gv(gv_changed)
                self.sent_gvs.update(gv_changed)
            return True

        try:
            hid.with_retry(write, attempts=2, delay_s=0.05)
            if not self.connected:
                log.info("pad connected")
            self.connected = True
        except hid.PadBusy:
            pass                                      # try again next frame
        except (hid.PadNotFound, hid.PadError, OSError) as e:
            if self.connected:
                log.warning("pad lost: %s", e)
            self.connected = False
            self.sent = [None] * hid.NUM_LEDS
            self.sent_gvs = {}
