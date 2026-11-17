import struct

import pytest

from devinducky import browser, hid, keys, state
from devinducky.bindings import Bindings
from devinducky.config import DEFAULTS
from devinducky.input import HoldClassifier, decode


# ---------- state ----------

@pytest.mark.parametrize("status,detail,expected", [
    ("new", None, "starting"), ("claimed", None, "starting"), ("resuming", None, "starting"),
    ("running", "working", "working"), ("running", None, "working"),
    ("running", "waiting_for_user", "waiting"), ("running", "waiting_for_approval", "approval"),
    ("running", "finished", "finished"),
    ("suspended", "inactivity", "idle"), ("suspended", "user_request", "idle"),
    ("suspended", "out_of_credits", "error"), ("suspended", "usage_limit_exceeded", "error"),
    ("suspended", "error", "error"), ("error", None, "error"), ("exit", None, "ended"),
    ("running", "something_new", "working"), ("brand_new_status", None, "working"),
])
def test_display_state(status, detail, expected):
    assert state.display_state({"status": status, "status_detail": detail}) == expected


def test_archived_is_ended_and_none_is_unbound():
    assert state.display_state({"status": "running", "status_detail": "working", "is_archived": True}) == "ended"
    assert state.display_state(None) == "unbound"


def test_transitions_only_notable_changes():
    prev = {"a": "working", "b": "working", "c": "waiting", "d": "working"}
    cur = {"a": "waiting", "b": "working", "c": "waiting", "d": "idle", "e": "finished"}
    assert state.transitions(prev, cur) == [("a", "working", "waiting")]


def test_summary_gvs():
    g = state.summary_gvs(["working", "waiting", "finished", "approval"], 0)
    assert g == {20: state.CODE["approval"], 21: 2, 22: 1, 23: 1, 26: 0, 27: 4}
    assert state.summary_gvs([], 1)[20] == 0


def test_parse_ts():
    assert state.parse_ts("1970-01-01T00:01:00Z") == 60
    assert state.parse_ts(5) == 5.0 and state.parse_ts("junk") is None


# ---------- bindings ----------

def test_bind_move_unbind_roundtrip(tmp_path):
    b = Bindings(str(tmp_path / "b.json"), 12)
    assert b.bind(0, "s1") is None
    assert b.bind(3, "s2") is None
    assert b.bind(5, "s1") == 0                   # moves from slot 0
    assert b.get(0) is None and b.get(5) == "s1"
    assert b.bind(3, "s3") is None                # rebind replaces s2
    assert b.slot_of("s2") is None
    b.save()
    b2 = Bindings(str(tmp_path / "b.json"), 12).load()
    assert b2.slots == {3: "s3", 5: "s1"}
    assert b2.unbind(3) == "s3" and b2.first_free() == 0


def test_clear_keeps_a_backup(tmp_path):
    path = tmp_path / "b.json"
    b = Bindings(str(path), 12)
    b.bind(0, "s1")
    b.bind(4, "s2")
    b.save()
    assert b.clear() == 2 and b.slots == {}
    b.save()
    assert Bindings(str(path), 12).load().slots == {}
    assert Bindings(str(path) + ".bak", 12).load().slots == {0: "s1", 4: "s2"}
    assert b.clear() == 0                         # nothing bound: the backup is left alone
    assert Bindings(str(path) + ".bak", 12).load().slots == {0: "s1", 4: "s2"}


def test_bindings_ignore_bad_file(tmp_path):
    p = tmp_path / "b.json"
    p.write_text('{"0": "a", "40": "out-of-range", "1": ""}')
    assert Bindings(str(p), 12).load().slots == {0: "a"}
    p.write_text("not json")
    assert Bindings(str(p), 12).load().slots == {}


# ---------- browser URL parsing ----------

@pytest.mark.parametrize("url,expected", [
    ("https://app.devin.ai/sessions/7f29616947aa4d3fa98317c42e008493", "7f29616947aa4d3fa98317c42e008493"),
    ("https://app.devin.ai/sessions/7f29616947aa4d3fa98317c42e008493?tab=shell#x", "7f29616947aa4d3fa98317c42e008493"),
    ("https://app.devin.ai/sessions/devin-abc123", "abc123"),
    ("https://app.devin.ai/sessions", None),
    ("https://github.com/foo", None), ("", None), (None, None),
])
def test_session_id_from_url(url, expected):
    assert browser.session_id_from_url(url, DEFAULTS["session_url_regex"]) == expected


# ---------- HID packets ----------

def test_set_led_packet():
    p = hid.build_set_led(7, (300, 128, -5))
    assert len(p) == 64 and p[:7] == bytes([0x05, 0, 0x04, 7, 255, 128, 0]) and not any(p[7:])
    with pytest.raises(ValueError):
        hid.build_set_led(20, (0, 0, 0))


def test_write_gv_packets_split():
    pkts = hid.build_write_gv({i: i * 1000 for i in range(14)})
    assert len(pkts) == 2
    p = pkts[0]
    assert p[2] == 0x19 and p[3] == 0x80 and struct.unpack_from("<I", p, 4)[0] == 0
    assert p[8] == 0x81 and struct.unpack_from("<I", p, 9)[0] == 1000
    assert pkts[1][3] == 0x80 | 12


def test_parse_info():
    r = bytearray(64)
    r[3:7] = bytes([2, 1, 0, 24])
    r[7:11] = struct.pack("<I", 1234)
    r[11] = 3
    info = hid.parse_info(r)
    assert (info.firmware, info.hw_rev, info.serial, info.profile, info.sleeping) == ("2.1.0", 24, 1234, 3, False)


# ---------- hotkeys ----------

def test_hotkey_table_unique_and_roundtrips():
    assert len(set(keys.HOTKEY_FOR.values())) == len(keys.EVENTS)
    for ev, hk in keys.HOTKEY_FOR.items():
        assert decode(set(hk.mods), hk.fkey) == ev
    assert decode({"ctrl"}, "f13") is None


def test_every_slot_has_down_and_up():
    for k in keys.SLOT_KEYS:
        assert keys.Event("down", k) in keys.HOTKEY_FOR and keys.Event("up", k) in keys.HOTKEY_FOR


# ---------- tap vs hold ----------

def test_tap_vs_hold():
    c = HoldClassifier(700)
    c.down(1, 0.0)
    assert c.due(0.5) == [] and c.up(1, 0.6) == "tap"
    c.down(2, 1.0)
    assert c.due(1.69) == [] and c.due(1.71) == [2] and c.due(2.0) == []   # "ready" once
    assert c.up(2, 2.5) == "hold"                 # the hold itself fires on release
    assert c.up(9, 3.0) == "tap"                  # release without a down: pad woke up on the press


def test_lost_release_never_becomes_a_hold():
    c = HoldClassifier(700)
    c.down(3, 0.0)                                # tap whose release never arrives (Mac waking)
    assert c.due(1.0) == [3]                      # only the visual cue
    assert c.due(6.0) == [] and 3 not in c.pressed   # given up on
    c.down(4, 10.0)
    assert c.up(4, 16.0) is None                  # release far too late: ignore, don't rebind


def test_title_gvs_pack_three_oled_lines():
    title = "Add Slack submission notifications for the Allied Health rater café"
    lines = state.title_lines(title)
    assert lines == ["Add Slack", "submission", "notifications..."]
    assert all(len(l) <= state.OLED_COLS for l in state.title_lines("x" * 80 + " \n tail"))
    assert state.title_lines("caf\u00e9") == ["caf?"]
    g = state.title_gvs(3, title)
    assert g[state.GV_SHOW_KEY] == 3 and sorted(g) == list(range(16))
    raw = struct.pack("<15i", *(g[i] for i in range(15)))
    assert [raw[i:i + 20].rstrip(b"\0").decode() for i in (0, 20, 40)] == lines
    assert all(raw[i + 19] == 0 for i in (0, 20, 40))           # every line is terminated
    assert struct.pack("<5i", *(state.title_gvs(1, None)[i] for i in range(5))).rstrip(b"\0") == b"(untitled)"


# ---------- pad profile ----------

def test_build_profile(tmp_path):
    from devinducky.cli import build_profile
    cfg = dict(DEFAULTS, launch=[{"name": "Fix CI", "prompt": "x"}])
    pdir = build_profile(cfg, str(tmp_path))
    files = set(__import__("os").listdir(pdir))
    assert {"config.txt", "key1.txt", "key1-release.txt", "key13.txt", "key20.txt", "key23.txt"} <= files
    assert "key13-release.txt" not in files
    conf = open(f"{pdir}/config.txt").read().splitlines()
    assert "z1 S1" in conf and "dr 1" in conf and "z13 Fix C" in conf and "dr 20" in conf
    assert all(f"z{k} " in "\n".join(conf) for k in range(1, 27))
    assert open(f"{pdir}/key1.txt").read() == keys.HOTKEY_FOR[keys.Event("down", 1)].ducky() + "\n"


def test_slot_release_script_shows_the_bound_title():
    from devinducky.cli import profile_scripts
    scripts, _ = profile_scripts(DEFAULTS)
    rel = scripts[(3, "release")].splitlines()
    assert rel[0] == keys.HOTKEY_FOR[keys.Event("up", 3)].ducky()     # the hotkey still goes first
    assert rel[1] == "IF _GV15 == 3" and rel[-1] == "END_IF"
    assert "    POKE32(0xf400, _GV0)" in rel and "    POKE32(0xf438, _GV14)" in rel
    assert [l.strip() for l in rel if "PUTS" in l] == ["PUTS(0x4000f400)", "PUTS(0x4000f414)", "PUTS(0x4000f428)"]
    assert "    OLED_CPRINT S3 bound" in rel
    assert "\n" not in scripts[(3, "press")] and "\n" not in scripts[(keys.KEY_RESET, "press")]


def test_bind_while_held_puts_the_title_on_the_pad(tmp_path, monkeypatch):
    import threading, time
    from devinducky import daemon
    from devinducky.render import Frame
    monkeypatch.setattr(daemon, "notify", lambda *a: None)
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.cfg, d.lock, d.frame = DEFAULTS, threading.RLock(), Frame()
    d.sessions, d.states, d.finished_since, d.held, d.oled, d.presses = {}, {}, {}, set(), None, {}
    d.api_health, d.fast_until = daemon.API_OK, 0.0
    d.poll_now = threading.Event()
    d.bindings = Bindings(str(tmp_path / "b.json"), keys.NUM_SLOTS)
    d.devin = type("D", (), {"get_session": lambda self, sid: {
        "session_id": sid, "title": "Fix CI", "status": "running", "status_detail": "working"}})()
    d.held.add(2)                                   # S3 held past hold_ms
    d.bind(2, "abc")
    _, gvs = d.compose(time.monotonic())
    assert gvs[state.GV_SHOW_KEY] == 3 and gvs[0] == struct.unpack("<i", b"Fix ")[0]
    d.handle("hold", 3)                             # let go: the pad draws it, the daemon stands down
    assert d.compose(time.monotonic())[1][state.GV_SHOW_KEY] == 0
    d.bind(5, "def")                                # let go before the bind finished: nothing to show
    assert d.oled is None and d.bindings.get(5) == "def"


def test_lookup_from_key_down_only_binds_if_the_press_is_a_hold(tmp_path, monkeypatch):
    import threading
    from devinducky import daemon
    from devinducky.render import Frame
    monkeypatch.setattr(daemon, "notify", lambda *a: None)
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.cfg, d.lock, d.frame = DEFAULTS, threading.RLock(), Frame()
    d.sessions, d.states, d.finished_since, d.held, d.oled, d.fast_until = {}, {}, {}, set(), None, 0.0
    d.poll_now = threading.Event()
    d.bindings = Bindings(str(tmp_path / "b.json"), keys.NUM_SLOTS)
    d.devin = type("D", (), {"get_session": lambda self, sid: {"session_id": sid, "title": "t", "status": "running"}})()
    tap, hold = daemon.Press(), daemon.Press()
    tap.decide(False)
    d.bind(0, "abc", tap)
    assert d.bindings.ids() == []                   # a tap looked the session up and dropped it
    hold.decide(True)
    d.bind(0, "abc", hold)
    assert d.bindings.get(0) == "abc"
    d.cfg = dict(DEFAULTS, hold_ms=-1000)           # no wait: a press that never resolves is dropped
    d.bind(1, "zzz", daemon.Press())
    assert d.bindings.get(1) is None


def test_slot_keys_report_key_down():
    from devinducky.input import PadInput
    p = PadInput(700)
    p._handle(keys.Event("down", 2))
    p._handle(keys.Event("up", 2))
    assert [p.events.get_nowait() for _ in range(2)] == [("down", 2), ("tap", 2)]


def test_reset_needs_a_second_tap(tmp_path):
    import threading
    from devinducky import daemon
    from devinducky.render import Frame
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.cfg, d.lock, d.armed, d.frame = DEFAULTS, threading.RLock(), {}, Frame()
    d.bindings = Bindings(str(tmp_path / "b.json"), keys.NUM_SLOTS)
    d.bindings.bind(0, "aaa")
    d.bindings.bind(3, "ddd")
    d.bindings.save()
    d.handle("press", keys.KEY_RESET)
    assert d.bindings.ids() == ["aaa", "ddd"]       # armed only
    d.armed[keys.KEY_RESET] = 0                     # arming timed out
    d.handle("press", keys.KEY_RESET)
    assert d.bindings.ids() == ["aaa", "ddd"]       # so this tap arms again
    d.handle("press", keys.KEY_RESET)
    assert d.bindings.ids() == []
    assert Bindings(str(tmp_path / "b.json"), keys.NUM_SLOTS).load().slots == {}
    d.handle("press", keys.KEY_RESET)
    assert d.armed == {}                            # nothing bound: doesn't arm


def test_reset_keeps_the_hotkey_l4_had():
    assert str(keys.HOTKEY_FOR[keys.Event("press", keys.KEY_RESET)]) == "ctrl+alt+shift+f16"


def test_upper_wheel_is_push_to_talk_and_nothing_else_uses_its_modifiers(tmp_path):
    from devinducky.cli import build_profile, profile_scripts
    scripts, _ = profile_scripts(DEFAULTS)
    assert scripts[(keys.ENC1_PUSH, "press")] == "KEYDOWN CTRL\nKEYDOWN COMMAND\nKEYDOWN ALT"
    assert scripts[(keys.ENC1_PUSH, "release")] == "KEYUP ALT\nKEYUP COMMAND\nKEYUP CTRL"
    assert (keys.ENC1_CW, "press") not in scripts and (keys.ENC1_CCW, "press") not in scripts
    held = set(DEFAULTS["wheel_hold"])
    assert not any(held <= set(hk.mods) for hk in keys.HOTKEY_FOR.values())
    files = set(__import__("os").listdir(build_profile(dict(DEFAULTS, wheel_hold=[]), str(tmp_path))))
    assert "key23.txt" not in files and "key21.txt" not in files


def test_parse_tab_list():
    out = "1\t1\thttps://a.com/x,y\n1\t2\thttps://app.devin.ai/sessions/abc\n2\t1\t\njunk\n"
    assert browser.parse_tab_list(out) == [
        (1, 1, "https://a.com/x,y"), (1, 2, "https://app.devin.ai/sessions/abc"), (2, 1, "")]


def test_mods_from_event_flags():
    from devinducky.input import mods_from_flags
    assert mods_from_flags(0x40000 | 0x80000 | 0x800000) == {"ctrl", "alt"}   # Fn flag ignored
    assert decode(mods_from_flags(0x40000 | 0x80000 | 0x20000 | 0x100000), "f13") is None


def test_desktop_session_link_and_title_match(tmp_path):
    import json, sqlite3
    from devinducky import desktop
    assert desktop.session_link("abc") == "devin://acp/session?sessionId=devin-abc&connectorId=devin-cloud"
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("create table ItemTable (key text, value blob)")
    rows = [("aaa", "Fix bug", "2026-01-01"), ("bbb", "Fix bug", "2026-02-01"), ("ccc", "Other", "2026-03-01")]
    for sid, title, ts in rows:
        con.execute("insert into ItemTable values (?, ?)", (f"{desktop._SESSION_KEY}devin-{sid}",
                    json.dumps({"info": {"title": title, "updatedAt": ts}})))
    con.execute("insert into ItemTable values (?, ?)", ("windsurf.acp.sessioninfo.session.acp/devin-cli/x", "{}"))
    con.commit()
    con.close()
    sessions = desktop.stored_sessions(str(db))
    assert sorted(s[0] for s in sessions) == ["aaa", "bbb", "ccc"]
    assert desktop.match_title("Fix bug", sessions) == "bbb"
    assert desktop.match_title("Session", sessions) is None
    assert desktop.match_title("Fix bug fawkes — logo.svg", sessions) == "bbb"
    assert desktop.match_title("Fix bugs fawkes — logo.svg", sessions) is None
    longer = sessions + [("ddd", "Fix bug fawkes", "2025-01-01")]
    assert desktop.match_title("Fix bug fawkes — logo.svg", longer) == "ddd"


def test_desktop_lookup_tries_a_short_page_first_and_returns_the_session(monkeypatch):
    from devinducky import desktop
    monkeypatch.setattr(desktop, "window_title", lambda: "Old one")
    monkeypatch.setattr(desktop, "stored_sessions", lambda: [])
    calls = []

    class FakeDevin:
        user_id = "me"

        def recent_sessions(self, user_id=None, first=200):
            calls.append((user_id, first))
            new = [{"session_id": "n1", "title": "New one"}]
            return new if first == 30 else new + [{"session_id": "o1", "title": "Old one"}]

    assert desktop.active_session_id(FakeDevin()) == ("Old one", "o1", {"session_id": "o1", "title": "Old one"})
    assert calls == [("me", 30), ("me", 200)]
    known = [{"session_id": "o1", "title": "Old one"}]
    assert desktop.active_session_id(FakeDevin(), known)[1:] == ("o1", known[0])
    assert calls == [("me", 30), ("me", 200)]       # found in what was already fetched: no request
    monkeypatch.setattr(desktop, "window_title", lambda: "Session")
    assert desktop.active_session_id(FakeDevin()) == ("Session", None, None)
