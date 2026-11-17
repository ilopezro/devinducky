# duckyScript compiler (vendored)

The `dsvm_*.py` files are the duckyScript compiler from the
[duckyPad configurator](https://github.com/duckyPad/duckyPad-Configurator),
copied unmodified from `src/` at commit `e8d5d58528265628882324f0ab3f3a55622a9b32`
(2026-03-15, configurator 4.0.2). MIT licensed; see `LICENSE`.

`devinducky/compiler.py` is the only thing that imports them. To update, copy the
same six files from a newer configurator and check that it still matches the
pad's firmware.
