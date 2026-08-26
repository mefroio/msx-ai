# Changelog

This project follows Semantic Versioning. Published releases are identified by
an annotated Git tag named `vMAJOR.MINOR.PATCH`.

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
