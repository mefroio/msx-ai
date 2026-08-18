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

Control emulated and/or physical MSX computers through one Python MCP
server.

MSX-AI gives an MCP client a common set of tools for direct openMSX automation,
an agent running on physical MSX hardware, or that same physical-agent path
simulated through openMSX.

## What you can do

- Read screens, capture host-rendered PNG screenshots, and inspect CPU context.
- Type BASIC, send BIOS-visible keys, load applications, automatically submit
  FE-header BLOAD binaries through resident MSX BASIC, and inject Z80 code.
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

The default transport is STDIO. MCP configuration belongs to the client or to
the developer machine; this repository intentionally does not ship a root
`.mcp.json`. Such a file commonly grows workstation-specific executable paths
and environment values, so it is ignored and excluded from distributions.

### Register the server in an MCP client

Every client needs the same thing: run the STDIO command `msx-ai-mcp` and name
the server `msx-ai`. Only the file format and the registration command differ.

Claude Code, from the CLI:

```sh
claude mcp add msx-ai -- msx-ai-mcp                 # this project only
claude mcp add --scope user msx-ai -- msx-ai-mcp    # every project
```

`claude mcp list` then reports the server as connected, and the tools appear as
`mcp__msx-ai__msx_local_*` and `mcp__msx-ai__msx_agent_*`. A session that was
already running does not pick the server up: the server list is resolved when
the session starts, and the `/mcp` reconnect only re-dials entries that are
already in that list. Start a new session after adding the server.

Codex, from the CLI:

```sh
codex mcp add msx-ai -- msx-ai-mcp
```

or as a portable entry in the user-level `~/.codex/config.toml` or, for a
trusted checkout only, the ignored project file `.codex/config.toml`:

```toml
[mcp_servers.msx-ai]
command = "msx-ai-mcp"
```

The ChatGPT desktop app, Codex CLI, and Codex IDE extension share that Codex
configuration, as described in the
[official Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

Clients that read a JSON configuration — Claude Desktop, the VS Code MCP
integration, Cursor, Windsurf, Zed, and others — express the same server with
their own local file:

```json
{
  "mcpServers": {
    "msx-ai": {
      "command": "msx-ai-mcp"
    }
  }
}
```

If `msx-ai-mcp` is not on the `PATH` seen by the client, use the absolute path
of the entry point produced by the installation, for example
`~/.local/bin/msx-ai-mcp` or `<checkout>/.venv/bin/msx-ai-mcp`, and keep that
path in the local client file only.

Keep any `OPENMSX_BIN` override in the shell environment or in that local
client configuration. Do not commit an absolute path from one workstation.

Local Streamable HTTP is optional:

```sh
msx-ai-mcp --transport http --host 127.0.0.1 --port 8000
```

The HTTP endpoint is `http://127.0.0.1:8000/mcp`. It is intentionally restricted
to unauthenticated IPv4 loopback. STDIO remains the recommended default.

## Choose an explicit channel

MSX-AI has no mutable “active backend.” `msx_local_*` tools always use the
openMSX control API, and `msx_agent_*` tools always use the ASM-agent protocol.
Both channels may remain connected, and calls may alternate between them in
any order without changing the route of a later call. `msx_targets_status`
lists the independent channel identities but never selects one.

For configurations made from an earlier checkout, migrate deliberately:

| Earlier name | Explicit replacement |
|---|---|
| `msx_status` | `msx_local_status`, `msx_agent_status`, or inventory-only `msx_targets_status` |
| `msx_shutdown` | `msx_local_shutdown`, `msx_agent_disconnect`, or `msx_tcp_bench_shutdown` |
| `msx_real_listen` | `msx_agent_listen` |
| Other `msx_<operation>` names | Choose `msx_local_<operation>` or `msx_agent_<operation>` for the intended channel |

Ambiguous names are intentionally not published and never route according to
the most recently connected target.

### openMSX discovery and preflight

`msx_local_doctor` is read-only and does not start an emulator. Run it before
the first boot, especially after installing openMSX or changing profiles. It
reports the host platform, resolved executable/presence, owned and attach
transports/support (`control_transport_supported`, `boot_supported`, and
`attach_supported`), configuration mode/homes, requested and resolved
profile/machine, readiness, and structured issues. Candidate reports repeat
the transport and boot support flags. Every issue includes an `action` field
with the concrete recommendation.

Executable discovery is host-aware:

| Host | Discovery order and owned-process control |
|---|---|
| Linux | `OPENMSX_BIN`, then `openmsx` on `PATH`; `control_transport=stdio`, `attach_transport=unix_socket` |
| macOS | `OPENMSX_BIN`, `PATH`, then the standard openMSX app bundle; `control_transport=stdio`, `attach_transport=unix_socket` |
| Windows | `OPENMSX_BIN`, `PATH`, then registered/standard Program Files installs; `control_transport=tcp_sspi`, `attach_transport=tcp_sspi` |

The portable `cbios` profile uses the free C-BIOS supplied with openMSX and
does not require proprietary firmware. It is intended for control-channel,
screen, and cartridge-oriented smoke tests; C-BIOS does not include MSX BASIC
or MSX-DOS. `auto` first resolves a usable configured BASIC machine and
otherwise falls back to C-BIOS. The existing `basic`, `disk`, `dos`, and
`msx2plus` profiles remain available for installations with their required
firmware and media. Omitting `profile` still means `basic` for compatibility;
request `auto` or `cbios` explicitly for a portable first boot.

Boot configuration is explicit:

| `config_mode` | Behavior |
|---|---|
| `isolated` | Default; uses only MSX-AI's managed openMSX home and temporary settings |
| `user` | Uses the user's openMSX machine/file pools without replacing the user's persistent settings |
| `overlay` | Adds MSX-AI's managed templates while retaining user machine/ROM discovery |

### Direct openMSX

Use this path for fast emulator automation and exact debugger snapshots. It
does not require TCP or the MSX-side agent.

1. Call `msx_local_doctor` with `profile="auto"` and the intended
   `config_mode`; resolve any reported issue.
2. Call `msx_local_boot` with `profile="auto"`, optionally `window=true`, and
   the same `config_mode`.
3. Call `msx_local_status`; confirm `backend` is `openmsx`.
4. Call `msx_local_screen`. Use `msx_local_run_basic` only when the doctor
   resolved a firmware-backed profile that provides MSX BASIC.

Use `msx_local_attach` instead when openMSX is already running. MSX-AI then shares
that instance without changing its power, throttle, or audio state. If several
live openMSX sockets exist, the call refuses to guess: repeat it with one of the
exact `socket_path` values reported by the error. Linux and macOS attach through
the published Unix-domain socket. On Windows, both owned boot and attach use
openMSX's loopback descriptor (ports 9938 through 10001) and the official SSPI
Negotiate exchange. An owned boot waits for the descriptor matching the child
PID; attach additionally validates the selected existing process. If SSPI is
unavailable, MSX-AI reports Windows control as unsupported with an actionable
recommendation rather than opening an unauthenticated raw TCP channel.

### Simulated physical agent

Use this path to validate the real resident/monitor protocol and restrictions
without physical hardware.

This workflow is source-checkout-only because it builds the agent and isolated
bench locally. A pipx-installed host can use it when
`MSX_AI_SOURCE_ROOT` points to a checkout.

1. In that checkout, build the agent suite with `make agent` and prepare the
   local test ROMs and MSX-DOS/Nextor image.
2. Call `msx_tcp_bench_start` with `mode="resident"`; add `window=true` for a
   visible emulator.
3. Call `msx_tcp_bench_status` and verify both explicitly identified channels.
4. Use `msx_agent_*` to validate the physical protocol path and `msx_local_*`
   to inspect the same emulated machine through openMSX control APIs.

Use `mode="monitor"` for direct call, run, stop, slot, and mapper experiments.
The bench uses exactly one isolated openMSX process. Its `local` and `agent`
channels carry the same `bench_id`, so callers can alternate between them
without spawning, attaching, or selecting another emulator. A stalled agent
does not prevent `msx_local_screenshot` from diagnosing the existing machine.

### Physical MSX

Use this path when actual MSX hardware behavior is the subject of the session.

1. Install the nine-file suite described below.
2. For a UART bridge, run `MSXAI /DRIVER:8251` or
   `MSXAI /DRIVER:16C550` and configure the bridge with matching serial
   settings and flow control. If it connects outward, call
   `msx_agent_listen` on the host's specific LAN IPv4 address; if it accepts
   connections, call `msx_agent_connect` with its IPv4 address.
3. For an MSX Pico+ or an original MSX Pico equipped with Wi-Fi, first use the
   cartridge's existing Wi-Fi Setup. Then run `MSXAI /DRIVER:UNAPI`, or add
   `/PORT:<1..65534>` to choose a listener port other than the default `6603`.
   UNAPI reserves `65535` (`FFFFh`) as a random-port sentinel, so it cannot be
   selected as a predictable listener endpoint.
   Call `msx_agent_connect` with the MSX's IPv4 address and the same port.
   For the first physical test, `/MONITOR` is the conservative choice because
   listener lifecycle calls remain in foreground while it is idle. After
   `RUN`, data-path polling still occurs from `H.TIMI` and remains a hardware
   validation item. The Pico stack advertises `TCP_OPEN` as potentially
   blocking; after a resident connection is lost, rerun the same
   `MSXAI /DRIVER:UNAPI /PORT:...` command at DOS to clean up the old handle
   and listen again outside `H.TIMI`.
4. Before installing the resident, an optional `make unapi-probe` builds
   `work/agent/UNAPIPRB.COM`. Run `UNAPIPRB` or `UNAPIPRB 43123` to verify
   discovery, passive capability, the effective IP/port, and TCP state; quit
   the probe before starting `MSXAI` on that port.
5. Before the cartridge arrives, the opt-in
   [openMSX UNAPI validation](docs/openmsx-unapi-validation.md) exercises the
   same EXTBIO, passive-listener, bidirectional-stream, and relisten contract
   with a matched openMSXnet/UNAPINET pair. It is a contract emulator, not Pico/Pico+
   firmware or bus-timing emulation.
6. Call `msx_agent_status` before any mutation and verify runtime, transport,
   and feature negotiation.

The agent does not configure Wi-Fi, issue modem AT commands, or depend on a
specific network-adapter brand. The UNAPI path discovers a TCP/IP UNAPI
implementation by capability, so it is not tied to the Pico+ product name.
BaDCaT is one planned 16C550-compatible transport, not a project requirement.

## Reproducible demonstrations

With a visible direct-openMSX BASIC session, these MCP calls draw a simple
test card and return a PNG without proprietary game media:

```text
msx_local_boot(profile="basic", window=true)
msx_local_run_basic(program="10 SCREEN 2\n20 COLOR 15,4,4\n30 CIRCLE(128,96),50,15\n40 LINE(40,40)-(216,152),8,B\n50 GOTO 50")
msx_local_screenshot()
```

For an already installed physical agent, begin read-only and verify the
selected path before doing anything mutable:

```text
msx_agent_listen(host="192.168.1.20", port=6603)
msx_agent_status()
msx_agent_cpu_snapshot()
```

The status result is structured; a resident agent reports, for example,
`{"target":"agent","backend":"agent","state":"running","runtime_mode":"resident",...}` before
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
                  +-- transparent UART bridge --> physical MSX agent
                  |                                  |
                  |                                  +-- 8251
                  |                                  `-- 16C550-compatible UART
                  |
                  `-- TCP/IP UNAPI listener ------> physical MSX agent
                                                     `-- Pico/Pico+ Wi-Fi
```

The MCP interface, target protocol, network link, and MSX UART driver are
separate layers. A user can run only direct openMSX, only a physical target, or
both channels simultaneously without installing every optional backend. Tool
names fix the route; connection order never changes it.

## Capability matrix

| Capability | Direct openMSX | Agent resident | Agent monitor |
|---|---:|---:|---:|
| Text screen and standard PNG capture | Yes | Cooperative | Cooperative |
| CPU snapshot | Exact instruction boundary | `H.TIMI` callback context | `H.TIMI` callback while a payload runs; unavailable when idle |
| BIOS text and special-key input | Yes | Yes | No resident keyboard spool |
| RAM/VRAM inspect and patch | Yes | Bounded; page restrictions | Yes; monitor image protected |
| MSX-DOS PUT/GET | No | Yes | No |
| Application load | Yes | FE-header BLOAD through verified BASIC; safe direct data segments | Direct load/call/run |
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
  MSXAI.COM     MSXAIXF.COM  MCP8251.TSR  MCP16550.TSR
  MCPUNAPI.TSR  MP.COM       MEMMAN.COM   TL.COM       TK.COM
```

Build them from source with `make agent`, or use matching binaries from a
project release. Keep the suite together and configure MSX-DOS once:

```bat
SET MSXAI_HOME=A:\MSXAI
PATH A:\MSXAI;%PATH%
```

The default MemMan resident returns to MSX-DOS. `MSXAIXF.COM` is its transient
foreground file helper, while `MP.COM` is the one-shot helper that applies the
selected UNAPI port after MemMan's first-install warm boot, including the
default 6603 when `/PORT` is omitted. The public `/PORT` syntax remains decimal;
the compact hexadecimal form passed to `MP.COM` is private to the install
chain. The other COM/TSR files are part of the same versioned suite. MSX-DOS 2
or Nextor and a memory mapper are required for resident mode.

## MCP interface

The `msx-ai-mcp` entry point uses the official Python MCP SDK and negotiates
current and supported older MCP protocol revisions. It provides:

- STDIO and local Streamable HTTP transports.
- Structured results and output schemas for every tool.
- Explicit read-only, destructive, idempotent, and open-world annotations.
- Cooperative MCP progress and safe cancellation for PUT/GET transfers.
- Documentation resources under `msx-ai://docs/`.
- The read-only `msx_docs_search` tool.
- Explicit `msx_local_*`, `msx_agent_*`, and paired `msx_tcp_bench_*` families;
  ambiguous generic operational names are not published.
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
- Automatic BLOAD loading accepts only a complete segment in `0x8000-0xFFFF`,
  verifies that declared payload, and never relocates it. An incompatible range
  or entry is rejected; verification proves the RAM bytes, not that arbitrary
  machine code will cooperate with the agent.
- A paired bench exposes two control channels to one machine, not two machines.
  Local reset, power, quit, and local shutdown are refused while the paired
  bench exists, even after agent disconnect; use `msx_tcp_bench_shutdown` for
  the combined lifecycle.
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
- ROMs and bootable disk images are not distributed. Physical Pico/Pico+ UNAPI
  and BaDCaT SMD validation, including measured performance, remain pending
  until the corresponding hardware tests.

## Development and validation

The hardware-free host is continuously tested on Ubuntu, Windows, and macOS.
Ubuntu covers Python 3.10 through 3.14; Windows and macOS run the current
Python 3.14 lane. Every host lane installs the project, runs the complete unit
suite, and exercises the installed MCP entry point through STDIO and IPv4
loopback HTTP. The Ubuntu-only release gate additionally rebuilds the Z80 suite
with the pinned assembler. These regular jobs validate the host and simulated
TCP protocol path without launching an emulator. The ROM-free C-BIOS smoke can
be run explicitly on Ubuntu, Windows, or macOS; the committed CI does not
currently schedule a real-emulator job. That smoke performs the same read-only
adapter preflight used by the doctor, boots through both adapter and public
Session/profile paths, exercises the platform control transport, reads emulator
state, and shuts down without an orphan process. The larger
MSX-DOS/RS232-Net suite with local media and physical Pico/Pico+ or BaDCaT
hardware remain separate explicit integration gates.

TCP/IP UNAPI has a second opt-in emulator gate. Set the paths documented in
`docs/openmsx-unapi-validation.md`, run `make unapi-emulation-preflight`, then
`make test-unapi-emulation`. The normal unit and release suites never download
that emulator or install host libraries.

For an editable environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . 'build>=1' 'setuptools>=77'
make PYTHON=python test
make PYTHON=python agent
make PYTHON=python release-check
```

The equivalent PowerShell environment setup is:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e . "build>=1" "setuptools>=77"
```

Those Make commands remain the supported Linux/macOS workflow. On Windows,
after activating the environment, the equivalent release gate does not require
installing a Unix Make implementation:

```powershell
python tools/release_check.py
```

Windows uses the portable Python agent builder by default. An explicit `MAKE`
override opts into the existing Makefile recipe and is validated, while
Linux/macOS continue to require and use Make exactly as before.

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

The corresponding direct Windows commands are
`python tools/release_check.py --publish` and
`python tools/release_check.py --publish --output-dir dist`.

`release-assets` writes the sdist, the wheel rebuilt from it, and the stable
`msx-ai-agent.zip` under ignored `dist/`. The agent ZIP contains the nine matching
MSX files, the project license, the MemMan notice, checksums, and explicit
wire-protocol, transfer-protocol, and toolchain metadata. Existing artifacts
are never overwritten. The gate proves same-host equivalence with the pinned
assembler; it does not claim byte-identical output across unrelated toolchains.

The full agent-through-emulator end-to-end suite is deliberately separate and
serialized. With the required ROMs and ignored MSX-DOS/Nextor image installed,
opt in with:

```sh
make PYTHON=python test-integration
```

Runtime compatibility is determined by the negotiated wire protocol,
capabilities, and versioned subprotocols. The Python distribution version names
packages and release archives; there is no separate agent release version to
compare at connection time.

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
