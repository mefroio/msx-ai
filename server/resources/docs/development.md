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
builds and inspects the sdist and wheel, rebuilds both the wheel and nine-file
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
validated release files under ignored `dist/`, run:

```sh
make PYTHON=python release-assets
```

On Windows, use `python tools/release_check.py --publish` for strict mode and
`python tools/release_check.py --publish --output-dir dist` to persist assets.

This writes the versioned Python distributions and the deterministic,
MSX-DOS-compatible `MSXAI.ZIP` to `dist/`. The ZIP contains exactly nine binaries,
the project `LICENSE`, `MEMMAN-NOTICE.txt`, `SHA256SUMS`, and
`COMPATIBILITY.json` recording the project release, wire v3, transfer `fast-v1`,
and the pinned assembler. Checksums cover the binaries, license,
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
MCPUNAPI.TSR
MP.COM
MEMMAN.COM
TL.COM
TK.COM
```

`MP.COM` is the ninth, transient first-install helper. It applies the selected
UNAPI listener port after the MemMan warm boot, including the default 6603; its
compact hexadecimal command is private to the install chain.

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
