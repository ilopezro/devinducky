"""Pure mapping from Devin session data to what a key shows."""

import logging
import struct
import textwrap
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Display states, most urgent first (used for the OLED's "most urgent" GV).
URGENCY = ["approval", "waiting", "error", "starting", "working", "finished", "idle", "ended"]
CODE = {"unbound": 0, "ended": 1, "idle": 2, "finished": 3, "working": 4,
        "starting": 5, "error": 6, "waiting": 7, "approval": 8}
PULSE = {"waiting": 1.2, "approval": 0.5}       # pulse period in seconds

_IDLE_REASONS = {"inactivity", "user_request"}
_RUNNING = {"working": "working", "waiting_for_user": "waiting",
            "waiting_for_approval": "approval", "finished": "finished", None: "working"}


def display_state(session):
    if session is None:
        return "unbound"
    if session.get("is_archived"):
        return "ended"
    status, detail = session.get("status"), session.get("status_detail")
    if status in ("new", "claimed", "resuming"):
        return "starting"
    if status == "running":
        if detail not in _RUNNING:
            log.warning("unknown running status_detail %r; treating as working", detail)
        return _RUNNING.get(detail, "working")
    if status == "suspended":
        return "idle" if detail in _IDLE_REASONS else "error"
    if status == "exit":
        return "ended"
    if status == "error":
        return "error"
    log.warning("unknown status %r; treating as working", status)
    return "working"


def most_urgent(states):
    for s in URGENCY:
        if s in states:
            return s
    return "unbound"


def transitions(prev, cur):
    """prev/cur: {session_id: display_state} -> [(session_id, old, new)] for notable changes."""
    out = []
    for sid, new in cur.items():
        old = prev.get(sid)
        if old is None or old == new:
            continue
        if new in ("waiting", "approval", "finished", "error"):
            out.append((sid, old, new))
    return out


def parse_ts(value):
    """API timestamps are ISO-8601 strings or unix seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summary_gvs(states, api_health):
    """GV values the INFO key's OLED script reads. states: per-bound-slot display states."""
    vals = list(states)
    return {
        20: CODE[most_urgent(vals)],
        21: sum(s in ("waiting", "approval") for s in vals),
        22: sum(s in ("working", "starting") for s in vals),
        23: sum(s == "finished" for s in vals),
        26: api_health,
        27: len(vals),
    }


OLED_COLS = 18                  # characters per OLED line
TITLE_LINES = 3
TITLE_LINE_GVS = 5              # 20 bytes a line: 18 characters and the terminator
GV_TITLE = 0                    # _GV0.._GV14: the title, four characters to a GV
GV_SHOW_KEY = 15                # key ID whose release script should show the title; 0 = none
GV_BOUND_MASK = 28              # bit n set = slot n+1 is bound; RESET's script draws from it


def title_lines(title):
    """A session title as up to three OLED lines of printable ASCII."""
    text = "".join(c if " " <= c <= "~" else "?" for c in " ".join((title or "").split()))
    return textwrap.wrap(text, OLED_COLS, max_lines=TITLE_LINES, placeholder="...")


def title_gvs(key, title):
    """GVs a slot key's release script turns back into text: each line is a
    NUL-terminated string spread little-endian over five GVs."""
    lines = title_lines(title) or ["(untitled)"]
    lines += [""] * (TITLE_LINES - len(lines))
    out = {GV_SHOW_KEY: key}
    for i, line in enumerate(lines):
        raw = line.encode("ascii").ljust(TITLE_LINE_GVS * 4, b"\0")
        for j, v in enumerate(struct.unpack(f"<{TITLE_LINE_GVS}i", raw)):
            out[GV_TITLE + i * TITLE_LINE_GVS + j] = v
    return out


def now_ts():
    return datetime.now(timezone.utc).timestamp()
