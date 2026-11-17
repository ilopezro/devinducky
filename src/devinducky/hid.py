"""duckyPad Pro over its Counted Buffer HID interface.

Protocol: https://github.com/duckyPad/duckyPad-Profile-Autoswitcher/blob/master/HID_details.md

Every request opens and closes the device. On macOS the device is opened
non-exclusively: hidapi >= 0.12 seizes devices by default there, and seizing a
keyboard needs root and would also stop the pad's own keystrokes reaching the OS.
"""

import ctypes
import struct
import sys
import time
from dataclasses import dataclass

VENDOR_ID = 0x0483
PID_DUCKYPAD_PRO = 0xD11D
COUNTED_BUFFER_USAGE = 0x3A
BUF_SIZE = 64
NUM_LEDS = 20

REPORT_ID = 0x05
CMD_QUERY_INFO = 0x00
CMD_SET_LED = 0x04
CMD_SW_RESET = 0x14
CMD_WRITE_GV = 0x19

STATUS_SUCCESS, STATUS_ERROR, STATUS_BUSY = 0, 1, 2
GV_PER_PACKET = 12  # 5 bytes each in a 61-byte payload


class PadNotFound(Exception):
    pass


class PadBusy(Exception):
    """The pad is running a script or showing a menu and rejected the command."""


class PadError(Exception):
    pass


@dataclass
class PadInfo:
    firmware: str
    hw_rev: int
    serial: int
    profile: int
    sleeping: bool
    volume: str = ""       # label of the pad's SD card when it's in USB-drive mode


# ---------- packet builders and parsers (pure) ----------

def build_packet(cmd, payload=b""):
    if len(payload) > BUF_SIZE - 3:
        raise ValueError("payload too long")
    buf = bytearray(BUF_SIZE)
    buf[0] = REPORT_ID
    buf[2] = cmd
    buf[3:3 + len(payload)] = payload
    return bytes(buf)


def build_set_led(index, rgb):
    if not 0 <= index < NUM_LEDS:
        raise ValueError(f"LED index must be 0-{NUM_LEDS - 1}")
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return build_packet(CMD_SET_LED, bytes([index, r, g, b]))


def build_write_gv(values):
    """values: {gv_index: int} -> list of packets (at most 12 GVs per packet)."""
    items = sorted(values.items())
    packets = []
    for start in range(0, len(items), GV_PER_PACKET):
        payload = bytearray()
        for idx, val in items[start:start + GV_PER_PACKET]:
            if not 0 <= idx <= 31:
                raise ValueError("GV index must be 0-31")
            payload.append(idx | 0x80)
            payload += struct.pack("<I", int(val) & 0xFFFFFFFF)
        packets.append(build_packet(CMD_WRITE_GV, bytes(payload)))
    return packets


def parse_info(resp):
    return PadInfo(
        firmware=f"{resp[3]}.{resp[4]}.{resp[5]}",
        hw_rev=resp[6],
        serial=struct.unpack_from("<I", bytes(resp), 7)[0],
        profile=resp[11],
        sleeping=bool(resp[12]),
        volume=f"DP{resp[6]}_{resp[9]:02X}{resp[10]:02X}",
    )


# ---------- device I/O ----------

_hid_mod = None


def _hid():
    global _hid_mod
    if _hid_mod is None:
        import hid
        if sys.platform == "darwin":
            try:
                # The Python binding doesn't expose this switch; call the C symbol.
                ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)
            except (OSError, AttributeError):
                pass  # older hidapi never seizes
        _hid_mod = hid
    return _hid_mod


def find_path():
    for d in _hid().enumerate(VENDOR_ID, PID_DUCKYPAD_PRO):
        if d.get("usage") == COUNTED_BUFFER_USAGE:
            return d["path"]
    raise PadNotFound("duckyPad Pro not found (plugged in? configurator closed?)")


class Pad:
    """One open handle for a batch of packets; use as a context manager."""

    def __init__(self, timeout_ms=500):
        self.timeout_ms = timeout_ms
        self.dev = None

    def __enter__(self):
        self.dev = _hid().device()
        try:
            self.dev.open_path(find_path())
        except OSError as e:
            raise PadNotFound(str(e)) from e
        return self

    def __exit__(self, *exc):
        self.dev.close()

    def send(self, packet):
        self.dev.write(packet)
        resp = self.dev.read(BUF_SIZE, self.timeout_ms)
        if not resp:
            raise PadError("no response from pad")
        if resp[2] == STATUS_BUSY:
            raise PadBusy()
        if resp[2] != STATUS_SUCCESS:
            raise PadError(f"pad returned status {resp[2]}")
        return resp

    def reset(self, msc=False):
        """Reboot the pad (into USB-drive mode if msc). It doesn't answer this one."""
        self.dev.write(build_packet(CMD_SW_RESET, bytes([1 if msc else 0])))

    def info(self):
        return parse_info(self.send(build_packet(CMD_QUERY_INFO)))

    def set_led(self, index, rgb):
        self.send(build_set_led(index, rgb))

    def write_gv(self, values):
        for p in build_write_gv(values):
            self.send(p)


def with_retry(fn, attempts=5, delay_s=0.2):
    """Run fn(pad) in a fresh session, retrying while the pad reports BUSY."""
    for i in range(attempts):
        try:
            with Pad() as pad:
                return fn(pad)
        except PadBusy:
            if i == attempts - 1:
                raise
            time.sleep(delay_s * (i + 1))
