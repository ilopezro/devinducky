"""The devinducky daemon: owns the pad, polls Devin, reacts to keys."""

import json
import logging
import queue
import subprocess
import threading
import time
import urllib.request

from . import auth, browser, desktop, keys, state
from .bindings import Bindings
from .config import BINDINGS_FILE
from .devin import Devin, DevinError
from .browser import BrowserError
from .input import PadInput
from .render import Frame, Renderer, WHITE

log = logging.getLogger("devinducky")

GREEN, RED, DIM = (0, 255, 60), (255, 0, 0), (40, 40, 40)
CURSOR_TIMEOUT_S = 10
GOTO_SETTLE_S = 0.35            # switch tabs once the wheel stops turning
API_OK, API_AUTH, API_NET = 0, 1, 2


def notify(title, message):
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def open_url(url):
    subprocess.run(["open", url], capture_output=True)


class Daemon:
    def __init__(self, cfg, dry_pad=False):
        self.cfg = cfg
        self.dry_pad = dry_pad
        self.lock = threading.RLock()
        self.bindings = Bindings(BINDINGS_FILE, keys.NUM_SLOTS).load()
        self.sessions = {}            # session_id -> latest session dict
        self.states = {}              # session_id -> display state
        self.finished_since = {}      # session_id -> unix ts first seen finished
        self.cursor = None            # slot index
        self.cursor_moved = 0.0
        self.goto_due = None          # monotonic time to switch the browser to the cursor session
        self.nav_lock = threading.Lock()   # one tab switch at a time, in order
        self.armed = {}               # key ID -> monotonic deadline
        self.api_health = API_OK
        self.failures = 0
        self.fast_until = 0.0
        self.poll_now = threading.Event()
        self.frame = Frame()
        self.renderer = Renderer(cfg["resync_s"], cfg["pad_profile"])
        self.devin = None
        self.input = PadInput(cfg["hold_ms"],
                              on_raw=lambda ev, _t: self.renderer.invalidate(keys.led_of_key(ev.key)))

    # ---------- Devin polling ----------

    def connect(self):
        while True:
            try:
                self.devin = Devin(auth.api_key())
                me = self.devin.whoami()
                log.info("authenticated as %s (%s) in %s", me.get("user_name") or me.get("service_user_name"),
                         me.get("principal_type"), self.devin.org_id)
                self.api_health = API_OK
                return
            except auth.NoApiKey as e:
                log.error("%s", e)
                self.api_health = API_AUTH
            except DevinError as e:
                log.error("Devin auth check failed: %s", e)
                self.api_health = API_AUTH if e.status in (401, 403) else API_NET
            time.sleep(30)

    def poll_once(self):
        ids = self.bindings.ids()
        if not ids:
            return
        found = {s["session_id"]: s for s in self.devin.list_sessions(ids)}
        with self.lock:
            prev = dict(self.states)
            for sid in ids:
                if sid in found:
                    self._update_session(found[sid])
            changes = state.transitions(prev, {sid: self.states[sid] for sid in ids if sid in self.states})
        for sid, _old, new in changes:
            self._notify_transition(sid, new)

    def _update_session(self, s):
        sid = s["session_id"]
        st = state.display_state(s)
        self.sessions[sid] = s
        if st == "finished" and self.states.get(sid) != "finished":
            # First sighting: if we only just started, trust updated_at instead of "now".
            seen_before = sid in self.states
            self.finished_since[sid] = state.now_ts() if seen_before else (
                state.parse_ts(s.get("updated_at")) or state.now_ts())
        self.states[sid] = st

    def _notify_transition(self, sid, new):
        kind = {"waiting": "waiting", "approval": "waiting", "finished": "finished", "error": "error"}[new]
        if not self.cfg["notify"].get(kind):
            return
        s = self.sessions.get(sid, {})
        slot = self.bindings.slot_of(sid)
        label = f"S{slot + 1} · " if slot is not None else ""
        title = s.get("title") or sid[:8]
        if new == "finished":
            prs = len(s.get("pull_requests") or [])
            msg = f"{label}finished: {title}" + (f" ({prs} PR{'s' if prs != 1 else ''})" if prs else "")
        elif new == "error":
            msg = f"{label}error: {title} ({s.get('status_detail') or s.get('status')})"
        else:
            msg = f"{label}Devin needs you: {title}" + (" (approval)" if new == "approval" else "")
        log.info("notify: %s", msg)
        notify("Devin", msg)
        if self.cfg["ntfy_url"]:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    self.cfg["ntfy_url"], data=msg.encode(), method="POST"), timeout=5)
            except OSError as e:
                log.warning("ntfy failed: %s", e)

    def poll_loop(self):
        self.connect()
        backoff = 0
        last_health = 0.0
        while True:
            fast = time.monotonic() < self.fast_until
            wait = backoff or (self.cfg["fast_poll_s"] if fast else self.cfg["poll_s"])
            self.poll_now.wait(wait)
            self.poll_now.clear()
            try:
                if self.bindings.ids():
                    self.poll_once()
                elif time.monotonic() - last_health > 300:
                    self.devin.whoami()
                    last_health = time.monotonic()
                self.failures, backoff, self.api_health = 0, 0, API_OK
            except DevinError as e:
                self.failures += 1
                if e.status == 429:
                    backoff = min(60, max(5, backoff * 2))
                    log.warning("rate limited; backing off %ss", backoff)
                elif e.status in (401, 403):
                    self.api_health = API_AUTH
                    log.error("auth error: %s", e)
                else:
                    log.warning("poll failed (%d): %s", self.failures, e)
                    if self.failures >= 3:
                        self.api_health = API_NET

    # ---------- key handling ----------

    def handle(self, kind, key):
        log.debug("event %s %s", kind, key)
        slot = keys.slot_of(key)
        if slot is not None:
            if kind == "ready":          # held long enough; let go to bind
                self.frame.set_effect(keys.led_of_slot(slot), "solid", WHITE, 5)
            elif kind == "hold":
                self.frame.set_effect(keys.led_of_slot(slot), "solid", WHITE, 5)
                threading.Thread(target=self.bind_from_tab, args=(slot,), daemon=True).start()
            elif kind == "tap":
                self.open_slot(slot)
            return
        launch = keys.launch_of(key)
        if launch is not None:
            self.tap_launch(launch)
        elif key == keys.KEY_RESET:
            self.tap_reset()
        elif key == keys.KEY_OPEN:
            self.frame.set_effect(keys.led_of_key(keys.KEY_OPEN), "fade", WHITE, 0.6)
            open_url(self.cfg["new_session_url"])
        elif key == keys.ENC1_PUSH:
            target = self.cursor if self.cursor is not None else self.most_urgent_slot()
            if target is not None:
                self.open_slot(target)
        elif key == keys.KEY_DASH:
            open_url(self.cfg["dashboard_url"])
        elif key in (keys.KEY_REFRESH, keys.ENC2_PUSH):
            self.poll_now.set()
            self.frame.set_effect(keys.led_of_key(keys.KEY_REFRESH), "fade", WHITE, 0.6)
        elif key in (keys.ENC1_CW, keys.ENC1_CCW):
            self.move_cursor(1 if key == keys.ENC1_CW else -1)

    def _front_is_desktop(self):
        if not self.cfg["desktop_app"]:
            return False
        try:
            return browser.frontmost_app() == desktop.APP
        except BrowserError:
            return False

    def bind_from_tab(self, slot):
        led = keys.led_of_slot(slot)
        if self._front_is_desktop():
            self.bind_from_desktop(slot)
            return
        try:
            app, url = browser.active_tab_url(self.cfg["browsers"])
        except browser.BrowserError as e:
            log.warning("could not read browser tab: %s", e)
            notify("devinducky", "Couldn't read the browser tab. Allow Automation for devinducky in "
                   "System Settings › Privacy & Security › Automation.")
            self.frame.set_effect(led, "flash", RED, 1.0)
            return
        sid = browser.session_id_from_url(url, self.cfg["session_url_regex"])
        if not sid:
            with self.lock:
                old = self.bindings.unbind(slot)
                if old:
                    self.bindings.save()
            if old:
                log.info("S%d unbound (active %s tab is not a Devin session)", slot + 1, app)
                self.frame.set_effect(led, "fade", WHITE, 0.8)
            else:
                log.info("S%d: active %s tab is not a Devin session", slot + 1, app)
                self.frame.set_effect(led, "flash", RED, 1.0)
            return
        self.bind(slot, sid)

    def bind_from_desktop(self, slot):
        """Like bind_from_tab, for the desktop app. Never unbinds: a miss may just be an unreadable title."""
        led = keys.led_of_slot(slot)
        try:
            title, sid = desktop.active_session_id(self.devin)
        except (BrowserError, DevinError, subprocess.TimeoutExpired) as e:
            log.warning("could not read the Devin app's session: %s", e)
            self.frame.set_effect(led, "flash", RED, 1.0)
            return
        if not sid:
            log.info("S%d: no session titled %r for the Devin app's front window", slot + 1, title)
            self.frame.set_effect(led, "flash", RED, 1.0)
            return
        self.bind(slot, sid)

    def bind(self, slot, sid):
        led = keys.led_of_slot(slot)
        try:
            s = self.devin.get_session(sid)
        except DevinError as e:
            log.warning("bind S%d to %s failed: %s", slot + 1, sid, e)
            notify("devinducky", f"Couldn't load that session ({e.status}).")
            self.frame.set_effect(led, "flash", RED, 1.0)
            return
        with self.lock:
            moved = self.bindings.bind(slot, s["session_id"])
            self.bindings.save()
            self._update_session(s)
            self.fast_until = time.monotonic() + self.cfg["fast_poll_window_s"]
        log.info("S%d bound to %s %r%s", slot + 1, s["session_id"], s.get("title"),
                 f" (moved from S{moved + 1})" if moved is not None else "")
        self.frame.set_effect(led, "fade", WHITE, 0.6)

    def open_slot(self, slot):
        sid = self.bindings.get(slot)
        if not sid:
            self.frame.set_effect(keys.led_of_slot(slot), "fade", DIM, 0.3)
            return
        s = self.sessions.get(sid)
        url = s["url"] if s and s.get("url") else f"https://app.devin.ai/sessions/{sid}"
        threading.Thread(target=self._focus_or_open, args=(slot, sid, url), daemon=True).start()

    def _focus_or_open(self, slot, sid, url):
        """Switch to the session in the desktop app if that's in front, else to its open browser
        tab, else to the desktop app if it's running; failing all that, open a new tab."""
        with self.nav_lock:
            t = time.monotonic()
            if self._front_is_desktop() and desktop.focus_session(sid):
                log.info("S%d: shown in the Devin app (%.2fs)", slot + 1, time.monotonic() - t)
                return
            app = browser.focus_tab(self.cfg["browsers"], sid)
            if app:
                log.info("S%d: focused its %s tab (%.2fs)", slot + 1, app, time.monotonic() - t)
            elif self.cfg["desktop_app"] and desktop.focus_session(sid):
                log.info("S%d: no open tab, shown in the Devin app (%.2fs)", slot + 1, time.monotonic() - t)
            else:
                open_url(url)
                log.info("S%d: no open tab, opened %s (%.2fs)", slot + 1, url, time.monotonic() - t)

    def most_urgent_slot(self):
        with self.lock:
            ranked = sorted(self.bindings.ordered(),
                            key=lambda sl: state.URGENCY.index(self.states.get(self.bindings.get(sl), "ended")))
        return ranked[0] if ranked else None

    def move_cursor(self, step):
        bound = self.bindings.ordered()
        if not bound:
            return
        if self.cursor not in bound:
            self.cursor = bound[0] if step > 0 else bound[-1]
        else:
            self.cursor = bound[(bound.index(self.cursor) + step) % len(bound)]
        self.cursor_moved = time.monotonic()
        self.goto_due = self.cursor_moved + GOTO_SETTLE_S

    def confirmed(self, key):
        """True on the second tap within arm_s; the first tap arms the key and blinks it."""
        now = time.monotonic()
        if self.armed.get(key, 0) > now:
            del self.armed[key]
            return True
        self.armed[key] = now + self.cfg["arm_s"]
        self.frame.set_effect(keys.led_of_key(key), "blink", WHITE, self.cfg["arm_s"], period=0.2)
        return False

    def tap_launch(self, idx):
        key = keys.LAUNCH_KEYS[idx]
        led = keys.led_of_key(key)
        presets = self.cfg["launch"]
        if idx >= len(presets):
            self.frame.set_effect(led, "fade", DIM, 0.3)
            return
        if self.confirmed(key):
            self.frame.set_effect(led, "solid", WHITE, 30)
            threading.Thread(target=self.launch, args=(idx,), daemon=True).start()
        else:
            log.info("L%d armed: %s (tap again to launch)", idx + 1, presets[idx].get("name"))

    def tap_reset(self):
        led = keys.led_of_key(keys.KEY_RESET)
        if not self.bindings.ids():
            self.frame.set_effect(led, "fade", DIM, 0.3)
            return
        if not self.confirmed(keys.KEY_RESET):
            log.info("reset armed (tap again to unbind every slot)")
            return
        with self.lock:
            n = self.bindings.clear()
            self.bindings.save()
            self.cursor = self.goto_due = None
        log.info("reset: unbound %d slot(s); previous bindings in %s.bak", n, self.bindings.path)
        self.frame.set_effect(led, "flash", GREEN, 1.0)

    def launch(self, idx):
        preset = self.cfg["launch"][idx]
        led = keys.led_of_key(keys.LAUNCH_KEYS[idx])
        try:
            s = self.devin.create_session(preset)
        except DevinError as e:
            log.error("launch L%d failed: %s", idx + 1, e)
            notify("devinducky", f"Launch '{preset.get('name')}' failed: {e}")
            self.frame.set_effect(led, "flash", RED, 1.5)
            return
        sid = s.get("session_id")
        log.info("L%d launched %s: %s", idx + 1, sid, s.get("url"))
        self.frame.set_effect(led, "flash", GREEN, 1.5)
        with self.lock:
            self.fast_until = time.monotonic() + self.cfg["fast_poll_window_s"]
            if self.cfg["auto_bind_launched"] and sid:
                free = self.bindings.first_free()
                if free is not None:
                    self.bindings.bind(free, sid)
                    self.bindings.save()
        if s.get("url"):
            open_url(s["url"])
        self.poll_now.set()

    # ---------- rendering ----------

    def compose(self, t):
        cfg_c = self.cfg["colors"]
        f = self.frame
        f.pulses = {}
        now_wall = state.now_ts()
        bound_states = []
        with self.lock:
            for slot in range(keys.NUM_SLOTS):
                led = keys.led_of_slot(slot)
                sid = self.bindings.get(slot)
                st = self.states.get(sid, "starting") if sid else "unbound"
                if sid:
                    bound_states.append(st)
                color = cfg_c[st]
                if st == "finished" and now_wall - self.finished_since.get(sid, now_wall) > self.cfg["finished_dim_s"]:
                    color = cfg_c["finished_dim"]
                f.base[led] = tuple(color)
                if st in state.PULSE:
                    f.pulses[led] = state.PULSE[st]
            for i, k in enumerate(keys.LAUNCH_KEYS):
                preset = self.cfg["launch"][i] if i < len(self.cfg["launch"]) else None
                f.base[keys.led_of_key(k)] = tuple(preset.get("color", cfg_c["launch"])) if preset else (0, 0, 0)
            f.base[keys.led_of_key(keys.KEY_RESET)] = tuple(cfg_c["reset"])
            for k in (keys.KEY_OPEN, keys.KEY_DASH, keys.KEY_REFRESH, keys.KEY_INFO):
                f.base[keys.led_of_key(k)] = tuple(cfg_c["nav"])
            info = keys.led_of_key(keys.KEY_INFO)
            if self.api_health != API_OK:
                f.base[info] = RED
                f.pulses[info] = 0.8
            if self.cursor is not None and (t - self.cursor_moved > CURSOR_TIMEOUT_S
                                            or self.bindings.get(self.cursor) is None):
                self.cursor = None
            f.cursor = keys.led_of_slot(self.cursor) if self.cursor is not None else None
            cur_sid = self.bindings.get(self.cursor) if self.cursor is not None else None
            gvs = state.summary_gvs(bound_states, self.cursor, self.states.get(cur_sid), self.api_health)
        return f.evaluate(t), gvs

    def render_loop(self):
        # Each LED write is a ~30 ms USB round trip, so a full repaint takes ~0.6 s;
        # it runs here so key handling never waits on the pad.
        last_dry = None
        while True:
            t = time.monotonic()
            leds, gvs = self.compose(t)
            if self.dry_pad:
                snap = (tuple(leds), tuple(sorted(gvs.items())))
                if snap != last_dry:
                    log.debug("frame %s gvs %s", leds[:12], gvs)
                    last_dry = snap
            else:
                self.renderer.push(leds, gvs, t)
            time.sleep(0.08 if self.frame.animating(t) else 0.25)

    def run(self):
        logging.getLogger("devinducky").info("starting; %d slot(s) bound", len(self.bindings.ids()))
        if len(self.cfg["launch"]) > keys.NUM_LAUNCH:
            log.warning("config has %d launch presets; only the first %d have a key",
                        len(self.cfg["launch"]), keys.NUM_LAUNCH)
        self.input.start()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.render_loop, daemon=True).start()
        while True:
            try:
                kind, key = self.input.events.get(timeout=0.1)
                self.handle(kind, key)
            except queue.Empty:
                pass
            t = time.monotonic()
            if self.goto_due and t >= self.goto_due:
                self.goto_due = None
                if self.cursor is not None:
                    self.open_slot(self.cursor)
            for idx, deadline in list(self.armed.items()):
                if deadline <= t:
                    del self.armed[idx]
