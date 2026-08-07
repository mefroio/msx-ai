# MSX-AI

<p align="center">
  <img src="https://raw.githubusercontent.com/mefroio/msx-ai/main/assets/msx-ai-robot.png" alt="MSX-AI retro robot mascot" width="320">
</p>

<p align="center">
  <a href="https://github.com/mefroio"><img alt="Built by Rodrigo Galhardi M. Garcia" src="https://img.shields.io/badge/Built%20by-Rodrigo%20Galhardi%20M.%20Garcia-blue"></a>
  <a href="https://github.com/mefroio/msx-ai/blob/main/LICENSE"><img alt="License GPLv3 or later" src="https://img.shields.io/badge/License-GPLv3%2B-blue.svg"></a>
  <a href="https://github.com/mefroio/msx-ai/actions/workflows/ci.yml"><img alt="CI status" src="https://github.com/mefroio/msx-ai/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/mefroio/msx-ai/blob/main/pyproject.toml"><img alt="Python 3.10 or newer" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&amp;logoColor=white"></a>
</p>

Control emulated and physical MSX computers through one Python MCP
server.

MSX-AI gives an MCP client a common set of tools for direct openMSX automation,
an agent running on physical MSX hardware, or that same physical-agent path
simulated through openMSX. Backend selection is explicit: starting the server
does not launch an emulator or connect to hardware.

## What you can do

- Read screens, capture host-rendered PNG screenshots, and inspect CPU context.
- Type BASIC, send BIOS-visible keys, load applications, and inject Z80 code.
- Inspect or patch RAM and VRAM, and access I/O, slots, and mapper segments where
  the selected runtime can do so safely.
- Transfer arbitrary MSX-DOS files with CRC-32, durable resume, atomic
  publication, optional PackBits compression, progress, and cancellation.
- Use the same framed agent protocol with a simulated target and a real MSX.
- Search a bundled, hash-verified documentation corpus directly through MCP.

## Install and start

MSX-AI requires Python 3.10 or newer. Until the first PyPI release, install the
current public source in an isolated environment:

```sh
git clone https://github.com/mefroio/msx-ai.git
cd msx-ai
pipx install .
msx-ai-mcp
```

After publication on PyPI, the shorter equivalent will be:

```sh
pipx install msx-ai
msx-ai-mcp
```

The default transport is STDIO. A typical MCP client entry is:

```json
{
  "mcpServers": {
    "msx-ai": {
      "command": "msx-ai-mcp"
    }
  }
}
```

Local Streamable HTTP is optional:

```sh
msx-ai-mcp --transport http --host 127.0.0.1 --port 8000
```

The HTTP endpoint is `http://127.0.0.1:8000/mcp`. It is intentionally restricted
to unauthenticated IPv4 loopback. STDIO remains the recommended default.

## Choose a backend

### Direct openMSX

Use this path for fast emulator automation and exact debugger snapshots. It
does not require TCP or the MSX-side agent.

1. Call `msx_boot` with `profile="basic"` and optionally `window=true`.
2. Call `msx_status`; confirm `backend` is `openmsx`.
3. Call `msx_screen`, or use `msx_run_basic` for a first visible result.

Use `msx_attach` instead when openMSX is already running. MSX-AI then shares
that instance without changing its power, throttle, or audio state. If several
live openMSX sockets exist, the call refuses to guess: repeat it with one of the
exact `socket_path` values reported by the error. On Windows, openMSX publishes
the loopback TCP port in that descriptor file; MSX-AI discovers and connects to
it automatically, so attaching remains available without Unix sockets.

### Simulated physical agent

Use this path to validate the real resident/monitor protocol and restrictions
without physical hardware.

This workflow is source-checkout-only in version 0.6.0 because it builds the
agent and isolated bench locally. A pipx-installed host can use it when
`MSX_AI_SOURCE_ROOT` points to a checkout.

1. In that checkout, build the agent suite with `make agent` and prepare the
   local test ROMs and MSX-DOS/Nextor image.
2. Call `msx_tcp_bench_start` with `mode="resident"`; add `window=true` for a
   visible emulator.
3. Call `msx_status` and verify the negotiated agent features.

Use `mode="monitor"` for direct call, run, stop, slot, and mapper experiments.
The bench uses one isolated openMSX process and reaches the agent through TCP,
not through debugger shortcuts.

### Physical MSX

Use this path when actual MSX hardware behavior is the subject of the session.

1. Install the seven-file suite described below and run either
   `MSXAI /DRIVER:8251` or `MSXAI /DRIVER:16C550`.
2. Configure a compatible adapter as a transparent binary TCP/IPv4 bridge with
   matching UART settings and flow control.
3. If the adapter connects outward, call `msx_agent_listen` with the host
   machine's specific LAN IPv4 address. The safe default `127.0.0.1` accepts
   only local simulation. If the adapter accepts an incoming connection, call
   `msx_agent_connect` with its IPv4 address.
4. Call `msx_status` before any mutation and verify runtime, transport, and
   feature negotiation.

The agent does not configure Wi-Fi, issue modem AT commands, or depend on a
specific network-adapter brand. BaDCaT is one planned 16C550-compatible
transport, not a project requirement.

## Reproducible demonstrations

With a visible direct-openMSX BASIC session, these MCP calls draw a simple
test card and return a PNG without proprietary game media:

```text
msx_boot(profile="basic", window=true)
msx_run_basic(program="10 SCREEN 2\n20 COLOR 15,4,4\n30 CIRCLE(128,96),50,15\n40 LINE(40,40)-(216,152),8,B\n50 GOTO 50")
msx_screenshot()
```

For an already installed physical agent, begin read-only and verify the
selected path before doing anything mutable:

```text
msx_agent_listen(host="192.168.1.20", port=6603)
msx_status()
msx_cpu_snapshot()
```

The status result is structured; a resident agent reports, for example,
`{"backend":"real","state":"running","runtime_mode":"resident",...}` before
the snapshot call. An idle foreground monitor cannot provide a CPU snapshot;
run a payload there first. The capability matrix below defines the two capture
semantics.

## Architecture

```text
MCP client
    |
    v
MSX-AI Python server
    |
    +-- openMSX control API ----------------------> emulated MSX
    |
    `-- protocol v3 over a byte stream
            |
            `-- TCP/IPv4
                  |
                  +-- RS232-Net -----------------> simulated MSX agent
                  |
                  `-- transparent UART bridge --> physical MSX agent
                                                     |
                                                     +-- 8251
                                                     `-- 16C550-compatible UART
```

The MCP interface, target protocol, network link, and MSX UART driver are
separate layers. A user can run only direct openMSX, only a physical target, or
both workflows at different times without installing every optional backend.
One MCP server process owns at most one active target session.

## Capability matrix

| Capability | Direct openMSX | Agent resident | Agent monitor |
|---|---:|---:|---:|
| Text screen and standard PNG capture | Yes | Cooperative | Cooperative |
| CPU snapshot | Exact instruction boundary | `H.TIMI` callback context | `H.TIMI` callback while a payload runs; unavailable when idle |
| BIOS text and special-key input | Yes | Yes | No resident keyboard spool |
| RAM/VRAM inspect and patch | Yes | Bounded; page restrictions | Yes; monitor image protected |
| MSX-DOS PUT/GET | No | Yes | No |
| Application load | Yes | Safe segments; no call/run | Yes |
| Direct call/run/stop | Yes | No | Yes |
| I/O-port access | Via expert Tcl/debug tools | Yes | Yes |
| Slot/mapper selection | Via expert Tcl/debug tools | No | Yes |
| Reset/raw openMSX Tcl | Yes | No | No |

“Cooperative” is important: the resident runs through the normal BIOS timer
hook. It is not an NMI, bus master, or universal freeze mechanism.

## Install the MSX-side suite

The physical and simulated agent paths use these files:

```text
A:\MSXAI\
  MSXAI.COM    MSXAIXF.COM  MCP8251.TSR  MCP16550.TSR
  MEMMAN.COM   TL.COM       TK.COM
```

Build them from source with `make agent`, or use matching binaries from a
project release. Keep the suite together and configure MSX-DOS once:

```bat
SET MSXAI_HOME=A:\MSXAI
PATH A:\MSXAI;%PATH%
```

The default MemMan resident returns to MSX-DOS. `MSXAIXF.COM` is its transient
foreground file helper; the other COM/TSR files are part of the same versioned
suite. MSX-DOS 2 or Nextor and a memory mapper are required for resident mode.

## MCP interface

The `msx-ai-mcp` entry point uses the official Python MCP SDK and negotiates
current and supported older MCP protocol revisions. It provides:

- STDIO and local Streamable HTTP transports.
- Structured results and output schemas for every tool.
- Explicit read-only, destructive, idempotent, and open-world annotations.
- Cooperative MCP progress and safe cancellation for PUT/GET transfers.
- Documentation resources under `msx-ai://docs/`.
- The read-only `msx_docs_search` tool.
- `start_msx_session` and `diagnose_msx_connection` prompts.

Start with `msx-ai://docs/index` when a client supports resources, or call:

```text
msx_docs_search(query="resident screenshot safety")
```

## Safety and current limits

- Software that disables interrupts, replaces the timer-hook chain, or pages
  required state away can make a resident temporarily unreachable.
- A resident CPU snapshot is the register frame visible at the BIOS callback
  boundary. It must not be described as the interrupted application's exact
  arbitrary PC/SP. Direct openMSX snapshots have exact debugger semantics.
- Resident RAM page 1 is reserved while servicing requests. Page 3 contains
  live BIOS, DOS, stack, hook, and system state; arbitrary writes can crash the
  machine.
- Resident input feeds the BIOS keyboard ring. Games that scan the physical
  keyboard matrix directly will not observe it.
- Screenshots reconstruct standard SCREEN 0-8 and 10-12 from readable state.
  SCREEN 9, raster effects, exact interlaced timing, and write-only palette
  changes have limits. Large captures are expensive over slow 8251 links.
- PUT/GET never publishes an incomplete file as success. Cancellation keeps
  verified recovery state so the operation can resume.
- Atomic, no-overwrite GET publication and first-run openMSX-template
  materialization require hard-link support on the relevant host filesystem.
  FAT/exFAT destinations are therefore unsuitable. If publication is refused,
  the verified `.msxpart` file and journal remain available for resume on a
  supported destination.
- TCP agent framing is binary but has no authentication, encryption, TLS, or
  replay protection. CRC detects accidental corruption; it is not a security
  mechanism. Because the channel exposes RAM, VRAM, I/O, and execution, keep
  it on an isolated trusted LAN or carry it through a VPN/secure tunnel. Never
  expose it directly to the internet.
- `MSX_AI_USER_ROOT` is the base for relative host paths, not a filesystem
  sandbox. Absolute paths remain available to explicit MCP calls with the same
  permissions as the server process; use an appropriately restricted account
  and working directory.
- ROMs and bootable disk images are not distributed. Physical BaDCaT SMD
  validation and measured performance remain pending until hardware testing.

## Development and validation

The hardware-free host is continuously tested on Ubuntu, Windows, and macOS.
Ubuntu covers Python 3.10 through 3.14; Windows and macOS run the current
Python 3.14 lane. Every host lane installs the project, runs the complete unit
suite, and exercises the installed MCP entry point through STDIO and IPv4
loopback HTTP. The Ubuntu-only release gate additionally rebuilds the Z80 suite
with the pinned assembler. These jobs validate the host and simulated TCP
protocol path; openMSX with local ROMs and physical BaDCaT hardware remain
separate explicit integration gates.

For an editable environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . 'build>=1' 'setuptools>=77'
make PYTHON=python test
make PYTHON=python agent
make PYTHON=python release-check
```

The unit and explicit integration suites together cover protocol framing,
backend isolation, memory safety,
keyboard input, screenshots, CPU snapshots, application loading, transfer
recovery, MCP metadata/resources, packaging paths, and openMSX integration.
The release gate additionally requires Bas Wijnen `z80asm` 1.8 on `PATH` (or
`Z80ASM` set to that executable). It builds and installs the wheel outside the
source tree and exercises both STDIO and local HTTP with an official MCP
client. This regular gate can validate uncommitted work. Once the intended
tree is committed and clean, run the strict committed-source gate or persist
the release assets with:

```sh
make PYTHON=python publish-check
make PYTHON=python release-assets
```

`release-assets` writes the sdist, the wheel rebuilt from it, and
`msx-ai-agent-0.6.0.zip` under ignored `dist/`. The agent ZIP contains the
seven matching MSX files, the project license, the MemMan notice, checksums,
and an explicit host/agent/wire/transfer compatibility manifest. Existing
artifacts are never overwritten. The gate proves same-host equivalence with
the pinned assembler; it does not claim byte-identical output across unrelated
toolchains.

The emulator end-to-end suite is deliberately separate and serialized. With
the required ROMs and ignored MSX-DOS/Nextor image installed, opt in with:

```sh
make PYTHON=python test-integration
```

Version labels describe separate layers: `0.6.0` is the Python distribution,
the current MSX-side banner is Agent `2.0`, protocol v3 is the framed wire
transport, and protocol-X v1 is the file-transfer contract. Release assets
group mutually compatible host and agent builds; these numbers are not
interchangeable.

## Documentation

- [Getting started](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/getting-started.md)
- [Backends and runtime modes](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/backends.md)
- [Safety and operational limits](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/safety.md)
- [File transfers](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/transfers.md)
- [Development](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/development.md)
- [Technical reference](https://github.com/mefroio/msx-ai/blob/main/TECHNICAL.md)
- [MSX agent reference](https://github.com/mefroio/msx-ai/blob/main/agent/README.md)
- [Documentation provenance manifest](https://github.com/mefroio/msx-ai/blob/main/server/resources/docs/manifest.json)

The bundled corpus is original project documentation. Its manifest records
GPL-3.0-or-later licensing, internal evidence paths, review date, and a SHA-256 digest for
every MCP-readable document. Third-party MemMan material is identified
separately in `third_party/memman/NOTICE`. The small openMSX configuration
resource set includes adapted GPL-2.0 material and is identified in
`third_party/openmsx/NOTICE`; it does not change the license of the independent
MSX-AI host, agent, or documentation.

## License

MSX-AI code, project-authored documentation, and original artwork are released
under [GPL-3.0-or-later](https://github.com/mefroio/msx-ai/blob/main/LICENSE).
Bundled third-party or derived resources retain the licenses named in their
notices.
Copyright (C) 2026 Rodrigo Galhardi M. Garcia. Project authorship and the origin
of the real-MSX MCP integration are recorded in
[`AUTHORS.md`](https://github.com/mefroio/msx-ai/blob/main/AUTHORS.md).
The original mascot artwork and its provenance statement are described in
[`assets/NOTICE.md`](https://github.com/mefroio/msx-ai/blob/main/assets/NOTICE.md).
