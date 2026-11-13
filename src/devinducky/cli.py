"""devinducky command line."""

import argparse
import logging
import os
import plistlib
import queue
import shutil
import subprocess
import sys
import time

from . import config, keys

LABEL = "com.devinducky.daemon"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _logging(verbose, to_file=False):
    handlers = [logging.StreamHandler()]
    if to_file:
        os.makedirs(config.HOME, exist_ok=True)
        handlers = [logging.FileHandler(config.LOG_FILE)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per poll otherwise
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_run(args):
    from .daemon import Daemon
    _logging(args.verbose, to_file=args.log_file)
    Daemon(config.load(), dry_pad=args.dry_pad).run()


def cmd_test_pad(args):
    from . import hid
    from .input import PadInput
    try:
        info = hid.with_retry(lambda p: p.info())
        print(f"pad: duckyPad hw={info.hw_rev} fw={info.firmware} profile={info.profile}")
        for i in range(hid.NUM_LEDS):
            hid.with_retry(lambda p, i=i: p.set_led(i, (0, 80, 255)))
            time.sleep(0.05)
        time.sleep(0.4)
        hid.with_retry(lambda p: [p.set_led(i, (0, 0, 0)) for i in range(hid.NUM_LEDS)])
        hid.with_retry(lambda p: p.write_gv({27: 42}))
        print("LEDs: cycled blue then off; wrote _GV27=42 (check on the INFO key)")
    except (hid.PadNotFound, hid.PadError, hid.PadBusy) as e:
        print(f"pad HID: {e}")
    t0 = {}

    def raw(ev, now):
        if ev.kind == "down":
            t0[ev.key] = now
            print(f"  key {ev.key:>2} down")
        elif ev.kind == "up":
            ms = (now - t0.pop(ev.key, now)) * 1000
            print(f"  key {ev.key:>2} up    held {ms:.0f} ms")
        else:
            print(f"  key {ev.key:>2} press")

    pin = PadInput(config.load()["hold_ms"], on_raw=raw)
    pin.start()
    print("Press pad keys / turn encoders (Ctrl-C to quit). Nothing printing? Grant Accessibility "
          "to your terminal in System Settings › Privacy & Security.")
    try:
        while True:
            try:
                kind, key = pin.events.get(timeout=0.5)
                print(f"  -> {kind} {key}")
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        pass


def cmd_which_tab(args):
    from . import auth, browser, desktop
    from .devin import Devin, DevinError
    cfg = config.load()
    in_app = cfg["desktop_app"] and browser.running(desktop.APP)
    if in_app:
        try:
            devin = Devin(auth.api_key())
            devin.whoami()
            title, sid = desktop.active_session_id(devin)
            print(f"{desktop.APP} app: {title}")
            print(f"session: {sid or '(no session with that title)'}")
        except (browser.BrowserError, DevinError) as e:
            print(f"{desktop.APP} app: error: {e}")
    try:
        app, url = browser.active_tab_url(cfg["browsers"])
    except browser.BrowserError as e:
        if in_app:
            print(f"browser: {e}")
            return
        sys.exit(f"error: {e}")
    print(f"{app}: {url}")
    sid = browser.session_id_from_url(url, cfg["session_url_regex"])
    print(f"session: {sid or '(not a Devin session)'}")


def cmd_playbooks(args):
    from . import auth
    from .devin import Devin
    d = Devin(auth.api_key())
    d.whoami()
    for pb in d.list_playbooks() or []:
        print(f"{pb.get('playbook_id') or pb.get('id')}\t{pb.get('title') or pb.get('name')}")


def profile_scripts(cfg):
    """{(key_id, "press"|"release"): duckyScript} for the devinducky pad profile."""
    out = {}
    names = {keys.KEY_RESET: "RESET", keys.KEY_OPEN: "NEW", keys.KEY_DASH: "DASH", keys.KEY_REFRESH: "REFR"}
    for ev, hk in keys.HOTKEY_FOR.items():
        part = "release" if ev.kind == "up" else "press"
        out[(ev.key, part)] = hk.ducky()
    out[(keys.KEY_INFO, "press")] = INFO_SCRIPT
    out[(keys.ENC2_CW, "press")] = "MK_VOLUP"      # lower wheel is a plain volume knob,
    out[(keys.ENC2_CCW, "press")] = "MK_VOLDOWN"   # handled on the pad without the daemon
    labels = {k: f"S{k}" for k in keys.SLOT_KEYS}
    for i, k in enumerate(keys.LAUNCH_KEYS):
        labels[k] = (cfg["launch"][i].get("name", f"L{i + 1}")[:5] if i < len(cfg["launch"]) else f"L{i + 1}")
    labels.update(names)
    labels[keys.KEY_INFO] = "INFO"
    return out, labels


INFO_SCRIPT = """\
// devinducky summary. The daemon writes:
// _GV20 most urgent (0 none 1 ended 2 idle 3 done 4 working 5 starting 6 error 7 waiting 8 approval)
// _GV21 waiting  _GV22 working  _GV23 done  _GV26 api (0 ok 1 auth 2 net)  _GV27 bound
// The firmware redraws the key labels as soon as a script that drew to the
// OLED ends, so stay running for a few seconds (or until another key) first.
OLED_CLEAR
OLED_CURSOR 0 10
OLED_CPRINT devinducky
OLED_CURSOR 0 40
IF _GV26 == 1
    OLED_CPRINT API KEY ERROR
ELSE IF _GV26 == 2
    OLED_CPRINT API UNREACHABLE
ELSE IF _GV27 == 0
    OLED_CPRINT hold a key to bind
ELSE IF _GV21 > 0
    OLED_CPRINT $_GV21 WAITING ON YOU
ELSE IF _GV22 > 0
    OLED_CPRINT $_GV22 working
ELSE
    OLED_CPRINT all quiet
END_IF
OLED_CURSOR 0 70
OLED_CPRINT bound $_GV27  done $_GV23
OLED_CURSOR 0 110
OLED_CPRINT any key to close
OLED_UPDATE
VAR start = _TIME_MS
WHILE _TIME_MS - start < 5000
    VAR k = _READKEY
    IF k != 0 && k != 20
        LBREAK
    END_IF
    DELAY 20
END_WHILE
OLED_RESTORE
"""


PROFILE_NAME = "devinducky"
ENCODER_LABELS = {keys.ENC1_CW: "SEL+", keys.ENC1_CCW: "SEL-", keys.ENC1_PUSH: "OPEN",
                  keys.ENC2_CW: "VOL+", keys.ENC2_CCW: "VOL-", keys.ENC2_PUSH: "REFR"}


def build_profile(cfg, out_dir):
    """Write profile_<name>/ in the configurator's on-disk format; return its path.

    Format (duckyPad-Configurator duck_objs.py): config.txt names each key with
    `zN <label>` (a key without a z line isn't loaded), `dr N` turns off
    auto-repeat, `SWCOLOR_N r g b` is the key's idle colour; scripts live in
    keyN.txt (on press) and keyN-release.txt (on release).
    """
    scripts, labels = profile_scripts(cfg)
    labels.update(ENCODER_LABELS)
    pdir = os.path.join(out_dir, f"profile_{PROFILE_NAME}")
    shutil.rmtree(pdir, ignore_errors=True)
    os.makedirs(pdir)
    lines = []
    for k in range(1, 27):
        press, release = scripts.get((k, "press"), ""), scripts.get((k, "release"), "")
        lines.append(f"z{k} {labels.get(k, '-')}")
        lines.append(f"dr {k}")          # holding must not re-fire the script
        for name, body in ((f"key{k}.txt", press), (f"key{k}-release.txt", release)):
            if body:
                with open(os.path.join(pdir, name), "w", newline="\n") as f:
                    f.write(body if body.endswith("\n") else body + "\n")
    for k in range(1, 21):
        lines.append(f"SWCOLOR_{k} 0 0 0")
    lines += ["BG_COLOR 0 0 0", "DIM_UNUSED_KEYS 0"]
    with open(os.path.join(pdir, "config.txt"), "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return pdir


def cmd_gen_profile(args):
    import zipfile
    os.makedirs(args.out, exist_ok=True)
    pdir = build_profile(config.load(), args.out)
    zpath = os.path.join(args.out, f"duckyPad_Profile_{PROFILE_NAME}.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(pdir)):
            z.write(os.path.join(pdir, name), f"{os.path.basename(pdir)}/{name}")
    print(f"wrote {zpath}")
    print("Configurator: Import → pick this zip → Save. Then close the configurator.")


def _service():
    return f"gui/{os.getuid()}/{LABEL}"


def _launchd_pid():
    """PID of the launchd-managed daemon, 0 if loaded but not running, None if not loaded."""
    out = subprocess.run(["launchctl", "print", _service()], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip().startswith("pid = "):
            return int(line.split("=")[1])
    return 0


def _stray_daemons(exclude):
    """Other `devinducky run` processes (e.g. one left running in a terminal)."""
    out = subprocess.run(["pgrep", "-f", "devinducky run"], capture_output=True, text=True)
    pids = {int(p) for p in out.stdout.split()} - {os.getpid(), exclude or -1}
    # `uv run` wraps the real process; keep only processes whose parent isn't also listed.
    def ppid(pid):
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        return int(r.stdout.strip() or 0)
    return sorted(p for p in pids if ppid(p) not in pids and ppid(p) != exclude)


def _write_plist():
    exe = shutil.which("devinducky") or os.path.join(os.path.dirname(sys.executable), "devinducky")
    os.makedirs(config.HOME, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [exe, "run", "--log-file"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardErrorPath": os.path.join(config.HOME, "launchd.err"),
        "EnvironmentVariables": {"SHELL": os.environ.get("SHELL", "/bin/zsh")},
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    return exe


def cmd_up(args):
    exe = _write_plist()
    if _launchd_pid() is None:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST], check=True)
    # Start it now (RunAtLoad alone can leave a fresh bootstrap idle); -k restarts a running one.
    subprocess.run(["launchctl", "kickstart", "-k", _service()], check=True)
    for _ in range(20):
        pid = _launchd_pid()
        if pid:
            break
        time.sleep(0.1)
    if not pid:
        sys.exit(f"devinducky didn't start; see {os.path.join(config.HOME, 'launchd.err')}")
    print(f"devinducky is up (pid {pid}), starts at login. Logs: devinducky logs")
    stray = _stray_daemons(pid)
    if stray:
        print(f"warning: another devinducky is running outside launchd (pid {', '.join(map(str, stray))}); "
              "stop it or every key press will act twice.")
    py = os.path.realpath(sys.executable)
    print(f"If pad keys do nothing, grant Accessibility to {py}\n"
          f"  (System Settings › Privacy & Security › Accessibility, + then Cmd-Shift-G to paste the path).")


def cmd_down(args):
    was = _launchd_pid()
    subprocess.run(["launchctl", "bootout", _service()], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("devinducky is down." if was is not None else "devinducky wasn't running under launchd.")
    stray = _stray_daemons(None)
    if stray:
        print(f"note: devinducky still running outside launchd (pid {', '.join(map(str, stray))}).")


def cmd_status(args):
    pid = _launchd_pid()
    if pid is None:
        print("down")
    elif pid == 0:
        print(f"loaded but not running (crashed?); see {os.path.join(config.HOME, 'launchd.err')}")
    else:
        print(f"up (pid {pid})")
    if os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE) as f:
            tail = f.readlines()[-5:]
        print("".join("  " + l for l in tail), end="")


def cmd_logs(args):
    os.execvp("tail", ["tail", "-n", "40", "-F", config.LOG_FILE])


def main(argv=None):
    p = argparse.ArgumentParser(prog="devinducky")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the daemon in the foreground")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--dry-pad", action="store_true", help="don't touch the pad; log frames instead")
    r.add_argument("--log-file", action="store_true", help=f"log to {config.LOG_FILE}")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("test-pad", help="cycle LEDs and echo key events").set_defaults(fn=cmd_test_pad)
    sub.add_parser("which-tab", help="show the active tab's Devin session").set_defaults(fn=cmd_which_tab)
    sub.add_parser("playbooks", help="list org playbooks").set_defaults(fn=cmd_playbooks)
    g = sub.add_parser("gen-profile", help="write the duckyPad profile scripts")
    g.add_argument("--out", default="profile")
    g.set_defaults(fn=cmd_gen_profile)
    sub.add_parser("up", aliases=["install", "restart"],
                   help="run in the background (launchd; restarts if running, starts at login)").set_defaults(fn=cmd_up)
    sub.add_parser("down", aliases=["uninstall"], help="stop the background daemon").set_defaults(fn=cmd_down)
    sub.add_parser("status", help="is the background daemon running?").set_defaults(fn=cmd_status)
    sub.add_parser("logs", help="follow the daemon log").set_defaults(fn=cmd_logs)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
