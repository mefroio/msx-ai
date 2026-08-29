# Development and validation

The host is Python, while the target agent is Z80 assembly. Generated binaries,
ROMs, disks, captures, and machine-local state belong under the ignored `work`
area and are not source artifacts.

## Python validation

GitHub Actions uses seven explicit hardware-free lanes: Ubuntu with Python
3.10, 3.11, 3.12, 3.13, and 3.14, plus Windows and macOS with Python 3.14.
Every lane installs the project, runs the complete unit suite, and exercises
the installed MCP entry point over STDIO and IPv4-loopback HTTP. TCP framing,
CRC, resume, PUT/GET, and hard-link behavior remain enabled across hosts rather
than being skipped. The Ubuntu-only release gate adds the canonical Z80 build.
These regular lanes do not launch openMSX or claim physical BaDCaT validation.

The ROM-free `openmsx-cbios` integration test can be run explicitly on Ubuntu,
Windows, and macOS, but the committed workflow does not currently schedule a
real-emulator job. The smoke covers adapter preflight, C-BIOS boots through both
adapter and public Session/profile paths, real Tcl/state exchanges, and clean
shutdown. External Windows SSPI attachment, user firmware overlays, and
physical hardware remain separate explicit validation scopes.

Create and activate an editable environment from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . 'build>=1' 'setuptools>=77'
```

The PowerShell equivalent is:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e . "build>=1" "setuptools>=77"
```

Then run the deterministic unit suite with that interpreter:

```sh
make PYTHON=python test
```

The suite covers protocol framing, backend selection, snapshots, memory safety,
application loading, screenshot rendering, transfer integrity and recovery,
agent source invariants, and reproducible helper generation.

The same ROM-free smoke can be run locally with a standard openMSX+C-BIOS
installation:

```sh
MSX_RUN_OPENMSX_SMOKE=1 \
  python -m unittest tests.test_openmsx_cbios_integration -v
```

The larger optional serialized agent-through-emulator suite is:

```sh
make PYTHON=python test-integration
```

It requires local openMSX machine ROMs and the ignored bootable MSX-DOS or
Nextor disk image. It owns at most one openMSX process at a time and exercises
the agent through RS232-Net and TCP rather than debugger-memory shortcuts.

Run the complete, hardware-free release gate with:

```sh
make PYTHON=python release-check
```

This remains the Linux/macOS workflow. On Windows, the equivalent command is:

```powershell
python tools/release_check.py
```

It requires Bas Wijnen `z80asm` 1.8 on `PATH`, or an explicit `Z80ASM`
executable override. Windows uses portable Python subprocesses by default;
an explicit `MAKE` override opts into the existing Makefile recipe and remains
fail-closed. Linux and macOS continue to require Make. This pre-commit gate
builds a clean snapshot
of the checkout's current on-disk source, including uncommitted changes while
excluding generated and local-state directories. It runs the unit suite,
builds and inspects the sdist and wheel, rebuilds both the wheel and twelve-file
agent suite from the sdist, and compares their same-host payloads. It installs
the rebuilt wheel into a clean environment, runs `pip check`, verifies
import/CLI state discipline and packaged openMSX resource materialization, and
exercises modern and legacy STDIO plus IPv4-loopback Streamable HTTP with the
official MCP client.

The separate TCP/IP UNAPI emulator gate is intentionally opt-in. After its
pinned openMSXnet/UNAPINET assets and licensed local ROM/DOS paths pass
`make unapi-emulation-preflight`, run `make test-unapi-emulation`. The normal
release gate neither downloads those assets nor installs native dependencies.

Use strict mode only after committing the intended release tree:

```sh
make PYTHON=python publish-check
```

It refuses tracked modifications and untracked non-ignored paths, then stages
exactly committed `HEAD` through Git before running the same gate. To persist
validated release files under the tracked `dist/`, run:

```sh
make PYTHON=python release-assets
```

On Windows, use `python tools/release_check.py --publish` for strict mode and
`python tools/release_check.py --publish --output-dir dist` to persist assets.

This writes the versioned Python distributions, the deterministic,
MSX-DOS-compatible `MSXAI.ZIP`, and a standalone `README.TXT` to `dist/`.
The ZIP contains the same README and exactly twelve binaries,
the project `LICENSE`, `MEMMAN-NOTICE.txt`, `SHA256SUMS`, and
`COMPATIBILITY.json` recording the project release, wire v3, transfer `fast-v1`,
and the pinned assembler. Checksums cover the binaries, README, license,
notice, and compatibility manifest. Existing output files are never
overwritten, and a failed multi-file publication rolls back files created by
that attempt.
Same-host double builds prove equivalence between the staged and sdist sources;
they are not a claim that unrelated host toolchains produce byte-identical
binaries.

The wheel must contain only public openMSX XML/settings resources; ROMs, disks,
persistent state, captures, `.mcp.json`, and other local configuration are
forbidden. Neither release gate launches openMSX; use the C-BIOS smoke or the
explicitly optional full integration suite for emulator validation.

## Build the MSX-DOS suite

Install `z80asm`, then run:

```sh
make agent
```

The deployable output under `work/agent` is exactly:

```text
MSXAI.COM
MSXAIXF.COM
MCP8251.TSR
MCP16550.TSR
MCP115K.TSR
MCPUNAPI.TSR
TU.COM
MP.COM
BADINIT.COM
MEMMAN.COM
TL.COM
TK.COM
```

`MCP16550.TSR` is the exact 57600-baud default for the 16C550 driver.
`MCP115K.TSR` is the otherwise matching `/115200` variant. `BADINIT.COM` is a
transient BaDCaT/ZiModem runtime initializer; it does not remain resident and
is not a general replacement for configuring another transparent UART bridge.

Keep the host disconnected while preparing BaDCaT and use one matching
sequence:

```text
MSXAI /UNINSTALL
BADINIT
MSXAI /DRIVER:16C550
```

or:

```text
MSXAI /UNINSTALL
BADINIT /115200
MSXAI /DRIVER:16C550 /115200
```

`BADINIT /PORT:<port>` accepts every decimal TCP port from 1 through 65535 and
defaults to 6603. It can precede or follow `/57600` or `/115200`; for example,
`BADINIT /PORT:7000 /115200` and `BADINIT /115200 /PORT:7000` select the same
runtime configuration. Port 65535 is valid here because BaDCaT/ZiModem does
not use the UNAPI driver's random-port sentinel.

The no-option default for both programs is exactly 57600 baud. Do not let the
host connect until the final command completes. `BADINIT` rejects an active
resident before touching the UART, performs no persistent write, and never
issues `AT&W`, reset `ATZ`, factory restore `AT&F`, or the persistent `S60`
listener setting. It requires `OK` from `ATQ0S41=0A<port>`, where `<port>` is
the selected decimal value, opening the runtime listener with automatic stream
entry disabled so creation errors remain visible. The no-option command is
`ATQ0S41=0A6603`. Its final, send-only `ATHS41=1Q1` line drops premature
clients, enables auto-stream, and only then enables quiet mode; no later
command-mode exchange can race the incoming host connection. A power cycle
restores the saved modem configuration. The host must pass that same selected
port to `msx_agent_connect`.

`BADINIT` follows the official BaDCaT UART initialization order and puts `F0`
at the end of the first bootstrap so a blocked XON/XOFF state cannot suppress
all earlier setup results. A quiet baud-change command is followed by TEMT, a
40-JIFFY no-transmit interval, and only then link validation. Diagnostics show
the failing stage, command, bounded RX bytes in hexadecimal, and LSR flags. A
continuous RX stream is bounded and reported without transmitting recovery AT
text; no UART recovery is attempted after listener commit becomes uncertain.

The optional 115200 UART rate does not raise BaDCaT's published 57,600-bit/s
effective-throughput limit. Framing, hardware flow control, bridge processing,
and target execution impose additional overhead, so benchmark actual MCP
payload throughput instead of deriving it from the UART divisor.

`TU.COM` is transient and runs only on a first UNAPI installation, after the
MemMan warm boot and before `TL.COM`. It prepares compatible Pico/Pico+
firmware state and hands off to `TL.COM`. UART installs still run `TL.COM`
directly.
`MP.COM` remains transient and runs after `TL.COM` to apply
the selected listener port, including the default 6603; its compact hexadecimal
command is private to the install chain.

Later BASIC-to-DOS transitions are handled by the already resident agent, not
by `TU.COM`. After `_SYSTEM` or `CALL SYSTEM`, the configured listener is
restored automatically.

Keep files from one build together. Internal relocation templates under the
build subdirectory are not deployable alternatives. The three MemMan utilities
are materialized from pinned, text-reviewable assets; their notice and decoded
hashes live under `third_party/memman`.

Replacing files on an MSX disk does not patch a resident TSR. Run
`MSXAI /UNINSTALL`, install the desired driver again, and negotiate a new host
connection.

## Documentation provenance

The MCP documentation corpus is project-authored and licensed under
GPL-3.0-or-later. Its manifest records stable identifiers, intended audience,
backend scope, local
evidence paths, review date, and SHA-256 for every Markdown resource. Evidence
points to this repository's implementation, tests, or existing project
documentation.

External material must not be added silently. Any external URL or non-project
origin included in a resource must be declared in its manifest entry with its
purpose and licensing status. Third-party binaries retain their own notices and
checksums rather than inheriting the project license.

Before publishing, run tests, verify documentation hashes, inspect tracked
files, and build release archives from version-controlled content. Do not add
proprietary system ROMs, user disk images, settings, logs, or captures.
