"""Compile duckyScript to the bytecode (.dsb) the pad runs.

The compiler is the duckyPad configurator's own, copied unmodified into
vendor/duckypad_configurator (see the README there). Its modules import each
other by bare name, so that folder goes on sys.path the first time it's needed.
"""

import contextlib
import io
import os
import sys

VENDOR = os.path.join(os.path.dirname(__file__), "vendor", "duckypad_configurator")

_dsvm = None


class CompileError(Exception):
    def __init__(self, message, line_no=0, line=""):
        super().__init__(f"line {line_no}: {message}" + (f"\n    {line.strip()}" if line else ""))
        self.line_no = line_no


def _load():
    global _dsvm
    if _dsvm is None:
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        import dsvm_common
        import dsvm_make_bytecode
        _dsvm = (dsvm_common, dsvm_make_bytecode)
    return _dsvm


def compile_script(text, dont_repeat=True, allow_abort=False):
    """duckyScript source -> bytecode. dont_repeat and allow_abort mirror the profile's
    `dr N` and `ab N` lines: the configurator turns those into this prologue when it
    compiles a key's script."""
    common, make = _load()
    prologue = (["$_ALLOW_ABORT = 1"] if allow_abort else []) + (["$_DONT_REPEAT = 1"] if dont_repeat else [])
    lines = prologue + text.lstrip(" \t").split("\n")
    listing = common.make_list_of_ds_line_obj_from_str_listing(lines)
    with contextlib.redirect_stdout(io.StringIO()):          # it prints its assembly listing
        res = make.make_dsb_no_exception(listing, import_name_to_line_obj_dict={})
    if not res.is_success:
        # Line numbers count the prologue; report them against the script as written.
        at = res.error_line_number_starting_from_1 - len(prologue)
        raise CompileError(res.error_comment, at, res.error_line_str or "")
    if len(res.bin_array) >= common.MAX_BIN_SIZE:
        raise CompileError(f"script too large ({len(res.bin_array)} bytes, limit {common.MAX_BIN_SIZE})")
    return res.bin_array
