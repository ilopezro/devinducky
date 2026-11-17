"""The Devin desktop app (a Windsurf/VS Code build): find and focus its cloud sessions."""

import json
import logging
import os
import sqlite3
import subprocess
import time

from .browser import BrowserError, _osascript, running

log = logging.getLogger(__name__)

APP = "Devin"
BUNDLE_ID = "com.exafunction.windsurf"
STATE_DB = os.path.expanduser("~/Library/Application Support/Devin/User/globalStorage/state.vscdb")
_SESSION_KEY = "windsurf.acp.sessioninfo.session.acp/devin-cloud/"

# The app only finds a session from the link once it has loaded its session list;
# otherwise it hands the link to the default browser. Bringing it forward first and
# giving it a moment avoids that.
SETTLE_S = 0.5


def session_link(sid):
    return f"devin://acp/session?sessionId=devin-{sid}&connectorId=devin-cloud"


def focus_session(sid):
    """Show cloud session `sid` in the desktop app. Returns False if the app isn't running."""
    try:
        if not running(APP):
            return False
        _osascript(f'tell application "{APP}" to activate')
    except (BrowserError, subprocess.TimeoutExpired) as e:
        log.warning("desktop focus %s: %s", sid, e)
        return False
    time.sleep(SETTLE_S)
    subprocess.run(["open", "-b", BUNDLE_ID, session_link(sid)], capture_output=True)
    return True


def window_title():
    """The front window's title without the trailing app name (the app sets "<session title> Devin")."""
    title = _osascript(f'tell application "System Events" to tell process "{APP}" to get name of front window')
    return title.removesuffix(APP).rstrip(" -—")


def stored_sessions(db=STATE_DB):
    """[(session_id, title, updated_at)] for the cloud sessions the app has saved."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("select key, value from ItemTable where key like ?", (_SESSION_KEY + "%",)).fetchall()
    finally:
        con.close()
    out = []
    for key, value in rows:
        try:
            info = json.loads(value).get("info", {})
        except (ValueError, AttributeError):
            continue
        sid = key[len(_SESSION_KEY):].removeprefix("devin-")
        out.append((sid, info.get("title") or "", info.get("updatedAt") or ""))
    return out


def match_title(title, sessions):
    """The session whose title `title` starts with, else None.

    The window title is "<session title>", or "<session title> <folder> — <open file>"
    when the session has a workspace open, so a session title that is a prefix ending
    at a space matches too. The longest such title wins, then the most recently updated.
    """
    hits = [s for s in sessions if s[1] and (title == s[1] or title.startswith(s[1] + " "))]
    return max(hits, key=lambda s: (len(s[1]), s[2]))[0] if hits else None


def active_session_id(devin=None, known=()):
    """The cloud session in the desktop app's front window, matched by window title.

    The app doesn't expose the session ID, so this looks the title up in the app's
    saved sessions, then via the API in your recent sessions, then in the org's (for
    teammates' sessions you have open). The app saves only a handful of sessions, so
    the API does most of the work. The title is sometimes a generic label, so this
    can find nothing.

    `known` is sessions the caller fetched from the API earlier; a title found there
    costs no request.

    Returns (title, session_id, session): session is the API's session when the API
    found it, which saves the caller fetching it again.
    """
    title = window_title()
    try:
        sid = match_title(title, stored_sessions())
    except sqlite3.Error as e:
        log.warning("can't read %s: %s", STATE_DB, e)
        sid = None
    # A session missing from `known` is nearly always brand new, and a short page
    # comes back in half the time of a full one.
    pages = ((devin.user_id, 30), (devin.user_id, 200), (None, 200)) if devin is not None else ()
    for found in [lambda: known] + [lambda p=p: devin.recent_sessions(*p) for p in pages]:
        if sid:
            break
        found = found()
        sid = match_title(title, [(s["session_id"], s.get("title"), s.get("updated_at") or 0) for s in found])
        if sid:
            return title, sid, next(s for s in found if s["session_id"] == sid)
    return title, sid, None
