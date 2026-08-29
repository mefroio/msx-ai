# Changelog

This project follows Semantic Versioning. Published releases are identified by
an annotated Git tag named `vMAJOR.MINOR.PATCH`.

## 0.1.4 - 2026-08-28

- Expanded the canonical MSX-DOS agent suite from ten to twelve binaries with
  `BADINIT.COM` and the opt-in 115200-baud `MCP115K.TSR`; the exact 16C550
  default is now 57600 baud.
- Added a non-persistent BaDCaT initializer and the coordinated safe sequence
  `MSXAI /UNINSTALL`, `BADINIT [/115200]`, then
  `MSXAI /DRIVER:16C550 [/115200]`. It keeps the host disconnected during
  setup, avoids save/reset/factory/`S60` operations, verifies listener creation
  with auto-stream disabled, and commits quiet auto-stream only afterward.
- Hardened the BaDCaT bootstrap for saved XON/XOFF state, aligned UART setup
  with BaDCaT's reference order, respected ZiModem's delayed baud transition,
  and added stage/hex/LSR failure diagnostics with a no-write post-listener
  failure path.
- Documented that a 115200-baud UART selection does not exceed BaDCaT's
  published 57,600-bit/s effective-throughput limit and does not guarantee a
  proportional MCP payload-rate increase.
- Added silent automatic recovery of the resident UNAPI listener after a host
  disconnect, allowing the host to reconnect without a command or key press on
  the MSX.
- Validated the recovery on a physical HOTBIT 1.0 with an MSX Pico+ cartridge.

## 0.1.3 - 2026-08-26

- Fixed loss of the Pico/Pico+ TCP/IP state across resident
  `DOS -> BASIC -> DOS` transitions.
- Restored the configured listener automatically when DOS returns, without a
  second user command.
- Validated repeated Pico+ transitions and added stricter build checks.

## 0.1.2 - 2026-08-26

- Added the transient `TU.COM` first-install helper so Pico/Pico+ TCP/IP UNAPI
  initialization happens after MemMan's warm boot but before `TL.COM` integrates
  the resident hooks. Its exact hook guard closes the firmware 2.12 four-byte
  `H.TIMI` write, including the interrupt boundary around first initialization.
- Recognized exact BASIC `_SYSTEM` and `CALL SYSTEM` lines in the resident
  `H.CRUN` hook, aborted TCP on the guarded heap stack, and stopped later timer
  handlers before Nextor reclaims the Pico+ cartridge state. This prevents the
  observed DOS corruption, `System error`, crashes, and extra reboot.
- Moved UNAPI lifecycle work off MemMan's small internal stack and added guarded
  heap-stack validation for install, reconfiguration, teardown, and transfer
  calls.
- Kept the 8251 and 16C550 transports outside the new UNAPI-only transition
  guard.

## 0.1.1 - 2026-08-18

- Added an MSX-DOS-readable `README.TXT` beside and inside `MSXAI.ZIP`.
- Simplified the on-machine command help.

## 0.1.0 - 2026-08-18

- First formally versioned release.
- Added physical MSX TCP/IP transport through UNAPI, including Pico/Pico+ support.
- Added resident and monitor agent modes, reliable file transfers, direct memory,
  CPU, application, BASIC, and input operations through MCP.
- Added deterministic `MSXAI.ZIP` distribution for MSX-DOS.
