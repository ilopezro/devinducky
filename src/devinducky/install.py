"""Put the devinducky profile on the pad without the duckyPad configurator.

What the configurator's Save does for a duckyPad Pro, minus the GUI: reboot the
pad into USB-drive mode, write the profile folder to its SD card, eject, reboot
it back. Only profile_devinducky and its line in profile_info.txt are touched.
"""

import contextlib
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import compiler, config, hid

PROFILE_INFO = "profile_info.txt"
MAX_PROFILES = 64
MOUNT_WAIT_S = 20
BACKUPS = os.path.join(config.HOME, "profile_backups")
NO_FLAGS = {"dont_repeat": False, "allow_abort": False}


class InstallError(Exception):
    pass


def compile_profile(pdir):
    """Write keyN.dsb next to every keyN.txt in pdir, the way the configurator does.
    It also gives every key named in config.txt a script, empty if need be."""
    flags = key_flags(pdir)
    with open(os.path.join(pdir, "config.txt"), encoding="utf8") as f:
        for m in re.finditer(r"^z(\d+) ", f.read(), re.M):
            open(os.path.join(pdir, f"key{m.group(1)}.txt"), "a").close()
    for name in sorted(os.listdir(pdir)):
        m = re.fullmatch(r"key(\d+)(-release)?\.txt", name)
        if not m:
            continue
        with open(os.path.join(pdir, name), newline="") as f:
            text = f.read()
        try:
            code = compiler.compile_script(text, **flags.get(int(m.group(1)), NO_FLAGS))
        except compiler.CompileError as e:
            raise InstallError(f"{name}: {e}") from e
        with open(os.path.join(pdir, name[:-4] + ".dsb"), "wb") as f:
            f.write(code)


def key_flags(pdir):
    """{key: {"dont_repeat": bool, "allow_abort": bool}} from a profile's config.txt."""
    flags = {}
    try:
        with open(os.path.join(pdir, "config.txt"), encoding="utf8") as f:
            for line in f:
                m = re.fullmatch(r"(dr|ab) (\d+)", line.strip())
                if m:
                    which = "dont_repeat" if m.group(1) == "dr" else "allow_abort"
                    flags.setdefault(int(m.group(2)), dict(NO_FLAGS))[which] = True
    except FileNotFoundError:
        pass
    return flags


def compiler_mismatches(card):
    """Scripts on the card whose bytecode differs from what our compiler makes of
    their source. The card was written by the configurator, so any difference means
    our copy of the compiler is not the one that matches this pad."""
    bad = []
    for pdir in sorted(p for p in glob.glob(os.path.join(card, "profile_*")) if os.path.isdir(p)):
        flags = key_flags(pdir)
        for txt in sorted(glob.glob(os.path.join(pdir, "key*.txt"))):
            dsb = txt[:-4] + ".dsb"
            m = re.fullmatch(r"key(\d+)(-release)?\.txt", os.path.basename(txt))
            if not m or not os.path.exists(dsb):
                continue
            with open(txt, newline="") as f:
                text = f.read()
            with open(dsb, "rb") as f:
                want = f.read()
            kw = flags.get(int(m.group(1)), NO_FLAGS)
            try:
                got = compiler.compile_script(text, **kw)
            except compiler.CompileError:
                got = None
            if got != want:
                bad.append(os.path.relpath(txt, card))
    return bad


def read_profile_info(card):
    """[(number, name)] from profile_info.txt, in file order."""
    out = []
    try:
        with open(os.path.join(card, PROFILE_INFO)) as f:
            for line in f:
                num, _, name = line.strip(" \r\n").partition(" ")
                if num.isdigit() and name:
                    out.append((int(num), name))
    except FileNotFoundError:
        pass
    return out


def ensure_listed(card, name):
    """Add `name` to profile_info.txt if it isn't there. Returns its number."""
    listed = read_profile_info(card)
    for num, n in listed:
        if n == name:
            return num
    num = max((n for n, _ in listed), default=0) + 1
    if num >= MAX_PROFILES:
        raise InstallError("the pad has no room for another profile")
    with open(os.path.join(card, PROFILE_INFO), "a", newline="") as f:
        f.write(f"{num} {name}\n")
    return num


def copy_profile(pdir, card):
    """Replace <card>/<basename of pdir> with pdir. Returns the backup path, or None.

    The new folder is written in full under another name first, so a failure while
    writing leaves the old profile as it was.
    """
    dest = os.path.join(card, os.path.basename(pdir))
    staged = os.path.join(card, "devinducky_incoming")
    _remove(staged)
    os.makedirs(staged)
    for name in sorted(os.listdir(pdir)):
        with open(os.path.join(pdir, name), "rb") as src, open(os.path.join(staged, name), "wb") as dst:
            dst.write(src.read())            # bytes only: no extended attributes onto FAT
    backup = None
    if os.path.isdir(dest):
        backup = os.path.join(BACKUPS, time.strftime("%Y-%m-%dT%H-%M-%S"), os.path.basename(pdir))
        shutil.copytree(dest, backup, ignore=shutil.ignore_patterns("._*"))
        _remove(dest)
    os.rename(staged, dest)
    for companion in glob.glob(os.path.join(dest, "._*")):     # macOS adds these; the pad has no use for them
        with contextlib.suppress(OSError):
            os.remove(companion)
    return backup


def _remove(path):
    # macOS keeps a "._name" companion for some files on FAT and deletes it along with
    # the file, so a plain rmtree trips over companions that are already gone.
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        raise InstallError(f"couldn't remove {path}")


def find_card(label):
    path = os.path.join("/Volumes", label)
    return path if os.path.isfile(os.path.join(path, PROFILE_INFO)) else None


def _reset_pad(msc, attempts=20):
    """Reboot the pad, into USB-drive mode if msc. It answers HID in either mode."""
    for _ in range(attempts):
        try:
            with hid.Pad() as pad:
                pad.reset(msc)
            return
        except (hid.PadNotFound, OSError):
            time.sleep(0.5)
    raise InstallError("can't reach the pad to reboot it; unplug and replug it")


def install(cfg, build, say=print, force=False):
    """build(cfg, out_dir) -> profile folder with scripts and config.txt."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = build(cfg, tmp)
        compile_profile(pdir)
        say(f"compiled {len(glob.glob(os.path.join(pdir, '*.dsb')))} scripts")
        try:
            info = hid.with_retry(lambda pad: pad.info(), attempts=10)
        except (hid.PadNotFound, hid.PadBusy, hid.PadError, OSError) as e:
            raise InstallError(f"pad not ready: {e or type(e).__name__} (plugged in? configurator closed?)") from e
        card = find_card(info.volume)
        if not card:
            say("rebooting the pad into USB-drive mode...")
            _reset_pad(msc=True)
            deadline = time.monotonic() + MOUNT_WAIT_S
            while not card and time.monotonic() < deadline:
                time.sleep(0.5)
                card = find_card(info.volume)
            if not card:
                raise InstallError(f"the pad's drive ({info.volume}) didn't show up in {MOUNT_WAIT_S} s; "
                                   "unplug and replug the pad, then try again")
        try:
            bad = compiler_mismatches(card)
            if bad and not force:
                raise InstallError(
                    "this compiler does not reproduce the bytecode already on the pad for:\n  "
                    + "\n  ".join(bad[:8]) + ("\n  ..." if len(bad) > 8 else "")
                    + "\nNothing was written. The pad's firmware or configurator is probably newer or older "
                      "than the compiler copied into devinducky. --force installs anyway.")
            num = ensure_listed(card, os.path.basename(pdir).removeprefix("profile_"))
            backup = copy_profile(pdir, card)
            say(f"wrote {os.path.basename(pdir)} (profile {num}) to {card}"
                + (f"; the old one is in {backup}" if backup else ""))
        finally:
            subprocess.run(["diskutil", "unmountDisk", "force", card], capture_output=True)
            _reset_pad(msc=False)
    say("pad rebooted. Done.")
