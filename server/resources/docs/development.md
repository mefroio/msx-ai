# Development and validation

The host is Python, while the target agent is Z80 assembly. Generated binaries,
ROMs, disks, captures, and machine-local state belong under the ignored `work`
area and are not source artifacts.

## Python validation

Create and activate an editable environment from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . 'build>=1' 'setuptools>=77'
```

Then run the deterministic unit suite with that interpreter:

```sh
make PYTHON=python test
```

The suite covers protocol framing, backend selection, snapshots, memory safety,
application loading, screenshot rendering, transfer integrity and recovery,
agent source invariants, and reproducible helper generation.

The optional serialized integration suite is:

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

It requires Bas Wijnen `z80asm` 1.8 on `PATH`, or explicit `MAKE` and `Z80ASM`
executable overrides. This pre-commit gate builds a clean temporary snapshot
of the checkout's current on-disk source, including uncommitted changes while
excluding generated and local-state directories. It runs the unit suite,
builds and inspects the sdist and wheel, rebuilds both the wheel and seven-file
agent suite from the sdist, and compares their same-host payloads. It installs
the rebuilt wheel into a clean environment, runs `pip check`, verifies
import/CLI state discipline and packaged openMSX resource materialization, and
exercises modern and legacy STDIO plus IPv4-loopback Streamable HTTP with the
official MCP client.

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

This writes the sdist, the wheel rebuilt from that sdist, and a deterministic
`msx-ai-agent-<host-version>.zip`. The ZIP contains exactly seven binaries,
the project `LICENSE`, `MEMMAN-NOTICE.txt`, `SHA256SUMS`, and
`COMPATIBILITY.json` recording host, Agent 2.0, wire v3, `fast-v1`, and the
pinned assembler. Checksums cover the binaries, license, notice, and
compatibility manifest. Existing output files are never overwritten, and a
failed multi-file publication rolls back files created by that attempt.
Same-host double builds prove equivalence between the staged and sdist sources;
they are not a claim that unrelated host toolchains produce byte-identical
binaries.

The wheel must contain only public openMSX XML/settings resources; ROMs, disks,
persistent state, captures, and local configuration are forbidden. Neither
release gate launches openMSX; use the explicitly optional integration suite
for emulator validation.

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
MEMMAN.COM
TL.COM
TK.COM
```

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
