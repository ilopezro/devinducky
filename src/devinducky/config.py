"""~/.devinducky/config.toml merged over defaults."""

import copy
import os
import tomllib

HOME = os.path.expanduser("~/.devinducky")
CONFIG_FILE = os.path.join(HOME, "config.toml")
BINDINGS_FILE = os.path.join(HOME, "bindings.json")
LOG_FILE = os.path.join(HOME, "daemon.log")

DEFAULTS = {
    "poll_s": 10,
    "fast_poll_s": 3,
    "fast_poll_window_s": 60,
    "hold_ms": 700,
    "arm_s": 3,
    "wheel_hold": ["ctrl", "cmd", "alt"],   # modifiers held while the upper wheel is pushed in; [] = none
    "resync_s": 10,
    "browsers": ["Google Chrome", "Arc", "Safari", "Brave Browser", "Microsoft Edge"],
    "desktop_app": True,       # also find and show sessions in the Devin desktop app
    "session_url_regex": r"app\.devin\.ai/sessions/((?:devin-)?[A-Za-z0-9]+)",
    "dashboard_url": "https://app.devin.ai/sessions",
    "new_session_url": "https://app.devin.ai/",   # the OPEN key opens this
    "finished_dim_s": 1800,
    "auto_bind_launched": False,
    "pad_profile": 0,          # only paint when this profile is active; 0 = any
    "notify": {"waiting": True, "finished": True, "error": True},
    "ntfy_url": "",
    "colors": {
        "unbound": [0, 0, 0],
        "waiting": [255, 120, 0],
        "approval": [255, 50, 0],
        "error": [255, 0, 0],
        "starting": [0, 180, 180],
        "working": [0, 60, 255],
        "finished": [0, 200, 40],
        "finished_dim": [0, 40, 8],
        "idle": [14, 14, 14],
        "ended": [4, 4, 4],
        "launch": [40, 0, 90],
        "reset": [50, 0, 0],
        "nav": [10, 10, 30],
    },
    "launch": [],
}


def load(path=CONFIG_FILE):
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except FileNotFoundError:
        return cfg
    for k in ("colors", "notify"):
        cfg[k].update(user.pop(k, {}))
    cfg.update(user)
    return cfg
