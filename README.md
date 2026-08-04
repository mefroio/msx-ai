# MSX-AI

MSX-AI is an MCP server for live MSX development. It can control openMSX
directly or communicate with an ASM agent over a transport-neutral TCP/IP byte
stream. The physical-target path can inspect and patch RAM/VRAM, pause and
resume cooperative software, access hardware ports, and render screenshots
from captured VRAM.

The project is intentionally independent from a particular AI interface or
network adapter. MCP is the current public interface; the application loader,
framed protocol, screenshot renderer, and MSX-side command core are separate
layers.

~~~~text
AI client / MCP
       |
       v
server/msx_mcp_server.py
       |
       +-- openMSX control API ----------------------> emulated MSX
       |
       `-- framed agent protocol
                 |
                 `-- TCP/IP byte stream
                           |
                           `-- transparent bridge --> MSX UART
                                                        |
                                                        `-- MSXAI.COM
~~~~

## Current capabilities

- Isolated openMSX sessions in headless or visible shared-window mode.
- One canonical `MSXAI.COM` containing both supported UART drivers.
- A true MemMan TSR by default: it returns to MSX-DOS and remains reachable
  while cooperative DOS programs or games run.
- An optional foreground monitor for direct upload, call, run, stop, slot, and
  mapper workflows.
- Runtime driver selection for a standard MSX 8251 RS-232 interface or a
  generic 16C550-compatible UART.
- A stream-safe protocol with sequence numbers, CRC-16/CCITT-FALSE,
  request/response correlation, retries, resynchronization, and negotiated
  payload limits.
- RAM, VRAM, direct I/O-port access, pause/resume, and host-rendered
  screenshots through the physical agent.
- BIOS keyboard-buffer input through the resident agent, enabling the same
  `msx_type`, `msx_type_line`, and `msx_run_basic` MCP workflows on openMSX
  and physical targets.
- Rendering for standard SCREEN 0-8 and SCREEN 10-12 modes, including display
  pages, scroll, palettes, and sprites.
- A backend-neutral loader for `msx-ai-app-v1` manifests, MSX-DOS COM files,
  BLOAD binaries, and flat 16/32 KiB ROM images.

## Hardware control boundary

The current resident agent is cooperative. It relies on maskable interrupts
and the BIOS `H.KEYI`/`H.TIMI` hook chain. It can pause and modify software
that leaves that path operational, including ordinary DOS programs launched
after the TSR is installed.

Software that keeps `DI` active, replaces the interrupt path, or otherwise
prevents the BIOS hooks from running cannot be pre-empted by a software-only
agent. Unconditional control would require an independent NMI, bus-master, or
equivalent hardware path. A transparent TCP/UART bridge does not create that
capability by itself.

## Requirements

- Python 3.10 or newer.
- [openMSX](https://openmsx.org/) for the emulator backend. Version 21.0 is the
  currently tested version.
- Bas Wijnen's `z80asm` for assembling uploaded source and the ASM agent.
- Legally obtained MSX system ROMs for machine configurations that require
  proprietary firmware.
- For the default physical resident mode: MSX-DOS 2 or Nextor, a compatible
  memory mapper, and the MemMan 2.4+ API. A verified public-domain MemMan 2.42
  runtime is embedded in `MSXAI.COM` and is installed automatically when it
  is not already present.
- A supported MSX UART connected to a transparent TCP/IP bridge for a physical
  target.

ROM images and bootable disk images are not distributed by this repository.

The emulator defaults can be overridden with these environment variables:

| Variable | Purpose |
|---|---|
| `OPENMSX_BIN` | Path to the openMSX executable |
| `Z80ASM` | Path to the `z80asm` executable |
| `MSX_AI_OPENMSX_HOME` | Isolated openMSX data/configuration directory |
| `MSX_AI_DOS_HDD` | Bootable MSX-DOS/Nextor hard-disk image |
| `MSX_AI_BASIC_MACHINE` | Machine used by the `basic`, `disk`, and `dos` profiles |
| `MSX_AI_MSX2PLUS_MACHINE` | Machine used by the `msx2plus` profile |
| `MSX_AI_DISK_EXTENSION` | Storage extension used by the `disk` profile |
| `MSX_AI_DOS_EXTENSION` | Storage extension used by the `dos` profile |

The repository includes machine XML definitions but not their ROM files. Place
local ROMs below `.openmsx-home/share/systemroms/`, or point
`MSX_AI_OPENMSX_HOME` at another isolated configuration. This command helps
diagnose missing firmware:

~~~~sh
openmsx -machine Gradiente_Expert20 -testconfig
~~~~

## MCP setup

The project-local `.mcp.json` uses a relative path:

~~~~json
{
  "mcpServers": {
    "msx-ai": {
      "command": "python3",
      "args": ["server/msx_mcp_server.py"]
    }
  }
}
~~~~

Clients with a global configuration should use the absolute path to
`server/msx_mcp_server.py`.

## openMSX workflows

`msx_boot` starts an isolated headless emulator by default. Every headless
process is muted at openMSX's host mixer for its complete lifetime. This does
not change PSG, SCC, OPLL, MSX I/O ports, emulated timing, or sound routines
executed inside the MSX. Startup fails closed if the host mute cannot be
verified.

Visible instances are not forcibly muted. Use `window=true`, or start a
shared instance and attach to it:

~~~~sh
./open-msx.command basic
~~~~

Then call `msx_attach`. Attaching does not change the existing instance's
power, throttle, renderer, or audio settings.

Available profiles are:

- `basic`: Gradiente Expert 2.0 in BASIC.
- `disk`: the same machine with the DDX 3.0 disk extension.
- `dos`: the same machine with Sunrise IDE/Nextor and a local hard-disk image.
- `msx2plus`: Sony HB-F1XDJ MSX2+ configuration.
- `mcp`: the reusable, user-owned foreground TCP test instance described below.

### TCP agent test bench

`msx_tcp_bench_start` starts one isolated openMSX process, imports the
canonical `MSXAI.COM`, selects the 8251 driver, and connects it to the MCP
server through RS232-Net/TCP. All physical-agent operations then use the TCP
protocol; they do not use openMSX debugger memory APIs.

The isolated bench also loads the generic four-way slot expander before its
Sunrise/Nextor and RS-232 cartridges.

For an interactive resident test, call it with:

~~~~json
{"window": true, "mode": "resident"}
~~~~

The MSX displays the installation banner and returns to the DOS prompt. Run a
DOS program or game in the visible window, then use `msx_status`,
`msx_pause`, `msx_memory_read`, `msx_memory_write`, `msx_screenshot`,
and `msx_resume`.

For direct ASM upload and on-screen command tracing:

~~~~json
{"window": true, "mode": "monitor", "debug": true}
~~~~

### Reusable user-launched TCP instance

On macOS, double-click `open-msx-mcp.command`, or run:

~~~~sh
./open-msx-mcp.command
~~~~

The launcher builds the canonical `MSXAI.COM`, copies the local MSX-DOS disk
to a disposable runtime image, and starts one visible openMSX instance with
normal sound. It makes the agent available on the runtime disk but does not
start it automatically, so the user can choose resident or foreground-monitor
mode at the DOS prompt. It never modifies the base disk, rejects a second
openMSX process, and accepts only an IPv4 target.

The MCP profile inserts openMSX's generic four-way slot expander before the
Sunrise/Nextor and RS-232 cartridges. Additional hardware can therefore be
added through the same launcher, for example:

~~~~sh
./open-msx-mcp.command -ext MegaRAM_2MB -ext DDX_3.0
~~~~

`MSX_AI_MCP_SLOT_EXPANDER` can select another compatible slot-expander
extension; its default is `slotexpander`. Extra openMSX arguments are appended
after the MCP profile's required hardware while the four expanded secondary
slots are available.

The instance retries its transparent RS232-Net connection to
`127.0.0.1:6603`. It can therefore be started before the MCP listener. Once the
agent has been started at the DOS prompt, connect with `msx_agent_listen` using
host `127.0.0.1` and port `6603`. Do not call `msx_tcp_bench_start`: the
emulator is already user-owned and running.

If the MCP client disconnects while openMSX remains open, press F11 once to
rearm the TCP connection, then call `msx_agent_listen` again. Closing the
window removes only its disposable runtime disk; the original MSX-DOS image
remains unchanged.

Optional IPv4 endpoint overrides are available for local testing:

~~~~sh
MSX_AI_MCP_IPV4=127.0.0.1 MSX_AI_MCP_PORT=6603 ./open-msx-mcp.command
~~~~

Then use `msx_asm_load` with `execute="call"` or `execute="run"`.
`debug=true` is intentionally rejected in resident mode.

Headless test-bench instances are host-muted; visible ones retain normal sound.
The integration harness serializes its cases and never runs more than one
openMSX process at a time.

## Building the physical agent

Build the single production executable:

~~~~sh
make agent
~~~~

The canonical output is:

~~~~text
work/agent/MSXAI.COM
~~~~

The build also creates an internal relocatable `MSXAI.TSR` and materializes
verified MemMan utilities under the ignored `work/agent/` tree. Those are
build inputs, not alternative agent executables.

Copy only `MSXAI.COM` to the MSX-DOS system. Driver selection is explicit and
case-insensitive:

| Command | Result |
|---|---|
| `MSXAI /DRIVER:8251` | Install/reconfigure the default resident TSR for a standard 8251 interface |
| `MSXAI /DRIVER:16C550` | Install/reconfigure the default resident TSR for a generic 16C550 interface |
| `MSXAI /DRIVER:8251 /MONITOR` | Start the non-resident foreground monitor with the 8251 driver |
| `MSXAI /DRIVER:16C550 /MONITOR` | Start the non-resident foreground monitor with the 16C550 driver |
| `MSXAI /DRIVER:8251 /MONITOR DEBUG ON` | Start the foreground monitor with visible command tracing |
| `MSXAI /UNINSTALL` | Remove the named resident TSR safely through MemMan |
| `MSXAI /?` or `MSXAI /HELP` | Display command-line help |

Exactly one `/DRIVER` is required for install or monitor mode.
`/UNINSTALL` must be used alone. Running the resident command again finds the
existing named TSR and changes its selected driver through MemMan instead of
installing a duplicate. Changing the live driver can disconnect the current
link, so reconnect through the newly selected interface.

### Supported UART drivers

| Driver | Current configuration | Notes |
|---|---|---|
| `8251` | Ports `80h/81h`, standard timer ports `84h/85h/87h`, 19,200 baud, 8N1 | Reference path used by the openMSX RS232-Net integration tests |
| `16C550` | Ports `80h-87h`, 115200 baud, 8N1, 16-byte FIFO, automatic RTS/CTS | Generic register-compatible path; hardware flow control is required |

BaDCaT SMD is an intended 16C550-compatible device, not a dependency or a
separate build. The agent contains no BaDCaT-specific AT commands, networking
UI assumptions, or product branches. Physical BaDCaT validation is pending
arrival of the hardware.

See [agent/README.md](agent/README.md) for the lifecycle, memory model, raw and
framed protocols, transport ABI, and driver implementation details.

## Resident and foreground modes

| Behavior | Default resident | `/MONITOR` |
|---|---:|---:|
| Returns to DOS | Yes | No |
| Observe a normally launched DOS program/game | Yes | No |
| Pause/read/patch/screenshot/resume | Yes | Yes |
| BIOS keyboard input | Yes | No |
| Agent-side `call` and `run` | No | Yes |
| Agent-side `stop` | No | Yes |
| Slot and mapper selection | No | Yes, pages 0 and 1 |
| On-screen `DEBUG ON` trace | No | Optional |

The resident reports execution state `running` while DOS or an application is
active. `pause` saves the complete interrupted CPU context and remains paused
until an explicit `resume`, including across temporary transport silence.
`stop` is rejected because discarding the interrupted DOS/application context
would be unsafe.

The foreground monitor is intended for injected development payloads. It owns
the foreground process, can launch code asynchronously, and can abandon that
code back to its own monitor.

## Resident memory and hardware safety

During a MemMan hook call the TSR occupies CPU page 1. Resident-mode RAM access
therefore follows these rules:

- Page 0 (`0x0000-0x3FFF`) is accessed through the DOS RAM slot with BIOS
  inter-slot routines.
- Page 1 (`0x4000-0x7FFF`) is unavailable and rejected.
- Pages 2 and 3 (`0x8000-0xFFFF`) are directly accessible.
- Page 3 contains live BIOS, DOS, stack, hook, and system state. Arbitrary
  writes can immediately crash or corrupt the machine.

Slot and mapper commands are unavailable in resident mode. An inter-slot return
restores slot state, and changing the interrupted program's mapper segment is
not safe. Both capabilities remain available only in the foreground monitor.

Direct I/O is an expert escape hatch. Writing the active UART, VDP ports, mapper
ports, or unrelated hardware can terminate the protocol session or damage live
machine state.

## TCP/IPv4 transport

The external contract is a transparent, ordered, full-duplex byte stream over
TCP/IPv4. IPv6 endpoints are intentionally unsupported. The MSX-side driver
knows only UART bytes; the host protocol does not depend on how the adapter was
configured.

~~~~text
MCP/backend operations
        |
protocol v3 frames
        |
TCP/IPv4 (host listens or connects)
        |
transparent network/UART adapter
        |
selected MSX byte driver
~~~~

Use `msx_agent_listen` when the adapter is a TCP client. Use
`msx_agent_connect` when the adapter exposes a TCP server. The older
`msx_real_listen` name remains as a compatibility alias. `msx_status`
reports `network_transport`, `network_role`, `agent_transport`, and
`agent_transport_id` separately.

Host-created IPv4 TCP streams enable `TCP_NODELAY`. A current 8251 agent
advertises `frame-wake-ack`: for each framed request the host sends the first
magic byte, waits until the agent returns `0x06` after actually entering the
parser, and only then sends the rest continuously at 19,200 baud. This avoids
the one-byte 8251 receiver overrun without throttling the payload. The first
HELLO probes this ACK with a bounded compatibility fallback because its feature
bit is not known yet; older agents retain conservative first-byte pacing. The
16C550 path does not add this round trip. The eight-ESC recovery marker uses a
conservative first-byte gap on an 8251 or before the transport is known.

## Protocol v3

The host performs a small raw-v2 capability bootstrap and upgrades capable
agents to this transport-independent frame:

~~~~text
"MX" | version | type | flags | sequence:u16le | opcode | status |
length:u16le | payload | crc16:u16le
~~~~

CRC covers the complete frame except the CRC field and uses
CRC-16/CCITT-FALSE. A retry reuses the identical encoded request and sequence
number. The agent de-duplicates state-changing commands and searches for the
next valid magic/header/CRC combination after damaged input.

The negotiated payload limit of the current agent is 320 bytes. Eight
consecutive ESC bytes reset a framed protocol session after a lost peer; a
single noise byte cannot downgrade it.

The optional `keybuf-input` HELLO feature enables opcode `t`. It atomically
enqueues bytes in the BIOS keyboard ring and returns accepted/pending counts.
The host waits for each line to be consumed before sending the next one, so a
BASIC line editor cannot discard commands queued after Return. Cached v3
responses also prevent a retried request from typing duplicate characters.

When the foreground monitor runs with `DEBUG ON`, the optional
`debug-peer-label` feature enables opcode `I`. Immediately after HELLO, a TCP
host sends the accepted IPv4 source endpoint as printable ASCII and the MSX
displays it as `MCP client: <ipv4>:<port>`. The UART-facing agent remains
transport-neutral: it never attempts to discover network metadata itself.

The optional `snapshot-lease` feature enables v3 opcode `S`. Its one-byte
payload is a lease of 1-255 agent receive-timeout periods; these are transport
timeouts, not wall-clock seconds. Valid protocol traffic refreshes the timeout,
so a long active transfer remains paused. Silence after a lost connection
consumes the lease and eventually resumes the interrupted program. The host
still sends `g` immediately after the final requested byte and uses the lease
only as failure recovery. If that resume acknowledgement is lost, the host
tries `g` directly, resets a damaged framed session with eight ESC bytes, and
verifies that the target is running. Manual `msx_pause` remains unbounded and
is released only by `msx_resume`. Atomic MCP RAM/VRAM reads and writes use the
same bounded lease instead of the manual pause command. While it owns a lease,
the host caps each framed-request attempt at one second and verifies that the
agent is still paused after acquisition. If the lease expired during a host
suspension or stalled transfer, the potentially mixed capture is discarded.

The `frame-wake-ack` feature (8251 only) makes the agent return raw byte `0x06`
after consuming the leading `M` of each framed request. It is transport flow
control rather than an MCP response; the real framed response remains unchanged.

## Screenshots from VRAM

`msx_screenshot` captures VRAM and VDP/BIOS state, renders the image on the
host, and returns a PNG MCP image content block. It does not require a visible
openMSX renderer. On a running physical target, `atomic=true` uses the bounded
snapshot lease for a consistent capture. The target resumes immediately after
the final RAM/VRAM byte is received, before host-side rendering, PNG
compression, file reading, or Base64 encoding. An older agent without the
`snapshot-lease` feature is rejected before any display-memory read; install
the current `MSXAI.COM` or explicitly use `atomic=false` (after a manual pause
if consistency is required).

For a target that reports the 19,200-baud 8251 driver, the host reads only the
small display metadata under the lease, then estimates payload, frame overhead,
request count, and transfer time before bulk VRAM acquisition. Captures above
the safety threshold are refused by default and the short lease is released
immediately. Pass `allow_slow=true` to opt in to the long transfer. This guard
also applies to `atomic=false`, because it protects the slow link as well as
the paused application.

Supported standard modes:

- Text and tile modes: SCREEN 0-4.
- Bitmap modes: SCREEN 5-8.
- YJK/YAE modes: SCREEN 10-12.
- Sprites, display pages, vertical/horizontal scroll, and palette overrides.

SCREEN 9 is a vendor-specific Korean mode and is reported as unsupported.

The V9938/V9958 palette interface is write-only. Games that change it directly
without maintaining the BIOS palette mirror may require an explicit 16-entry
RGB `palette` argument for exact colors. The same limitation applies to
software that changes VDP registers without updating the readable BIOS
shadows. `DEBUG ON` changes the foreground display and therefore appears in a
capture. Raster effects, borders/overscan, analog artifacts, and exact
interlaced-field timing are not reconstructed.

## Loading applications

`msx_app_load` uses one parser and validation path for both backends. It
recognizes:

- `.com`: loaded at `0x0100`.
- BLOAD `.bin`: start, end, and entry addresses come from the `0xFE` header.
- Flat 16/32 KiB `.rom`: requires an `AB` header.
- `.json`/`.msxapp`: a `msx-ai-app-v1` manifest containing RAM/VRAM
  segments.

Manifest payloads can use `hex`, `base64`, a relative `file`, or `fill`.
Paths cannot escape the manifest directory; optional SHA-256 values and all
address ranges are validated before target state changes.

Execution constraints depend on the runtime:

- openMSX and the foreground monitor support `execute="call"` and
  `execute="run"`.
- The default resident supports safe transfers with `execute="none"`, but
  rejects page 1 and agent-side call/run. Launch DOS software normally after
  installing the TSR.
- Copying a game or ROM into VRAM does not make it executable.
- Bank-switched cartridge images require an explicitly mapper-aware backend;
  the generic loader does not emulate arbitrary cartridge hardware.

## Main MCP tools

| Group | Tools |
|---|---|
| Session | `msx_boot`, `msx_attach`, `msx_tcp_bench_start`, `msx_agent_listen`, `msx_agent_connect`, `msx_status`, `msx_shutdown` |
| Execution | `msx_asm_load`, `msx_app_load`, `msx_pause`, `msx_resume`, `msx_stop` |
| Memory/video | `msx_memory_read`, `msx_memory_write`, `msx_screen`, `msx_screenshot` |
| Hardware | `msx_io_read`, `msx_io_write`, `msx_slot_select`, `msx_mapper_select` |
| Input | `msx_type`, `msx_type_line`, `msx_run_basic` (both backends); `msx_key` (openMSX) |
| openMSX/DOS | `msx_dos_asm_run`, `msx_disk_put_text`, `msx_reset`, `msx_cmd` |

Resident keyboard injection feeds the standard BIOS ring and therefore works
with DOS, BASIC, and software that calls BIOS character input. Games that read
the keyboard matrix directly do not observe those synthetic bytes. Physical
reset, individual matrix-key presses, and raw openMSX console commands remain
openMSX-only. Physical operations use the agent byte stream rather than
openMSX APIs. On a physical target, `msx_run_basic` enters BASIC automatically
only from an unambiguous DOS prompt. Reusing an already-visible BASIC prompt
requires `allow_existing_basic=true`, an explicit safety opt-in that prevents a
screen merely ending in `Ok` from being treated as permission to overwrite a
running application.

## Validation

Run the deterministic suite:

~~~~sh
make test
~~~~

Run the opt-in end-to-end suite:

~~~~sh
make test-integration
~~~~

The integration suite uses one openMSX process at a time and validates:

- default MemMan installation returning to DOS;
- intervention in a normally launched DOS COM program;
- repeated pause/resume, RAM/VRAM access, and a PNG rendered from agent-captured
  VRAM;
- foreground ASM upload/run/stop with visible `DEBUG ON` tracing;
- reconfiguration without a duplicate TSR;
- safe uninstall and an idempotent second uninstall.

It requires local machine ROMs and the ignored bootable image at
`work/system-disks/msxdos.dsk`. Headless instances remain host-muted without
changing emulated MSX sound behavior.

## Repository layout

~~~~text
agent/                    universal ASM agent, lifecycle, and UART drivers
agent/transports/         hardware-specific byte-stream implementations
server/msx_client.py      openMSX control client
server/msx_mcp_server.py  MCP JSON-RPC server and backend adapters
server/msx_protocol.py    protocol-v3 codec and incremental parser
server/msx_v3.py          framed stream session, retries, and correlation
server/msx_real.py        physical-agent stream/TCP client
server/msx_screenshot.py  host-side VRAM renderer
server/msx_application.py backend-neutral application parser/loader
tests/                    deterministic and opt-in integration tests
third_party/memman/       pinned public-domain MemMan assets and hashes
tools/                    reproducible MemMan/TSR build helpers
work/                     ignored binaries, disks, captures, and local data
~~~~

## Publication safety

Generated and machine-specific data are intentionally ignored:

- `work/` and assembled binaries;
- ROMs, disk images, savestates, replays, persistent CMOS/SRAM, and captures;
- local openMSX settings, caches, editor state, logs, and temporary files.

The test files are part of the project and should be published; they define the
protocol, renderer, lifecycle, and safety contracts. The many historical COM
variants are not source artifacts and should not be published.

Review `git status` and `git ls-files` before release. Publish from Git or
`git archive`, not by zipping the working directory, because ignored local
media remains on disk. Do not force-add proprietary ROMs or disk images.

The embedded MemMan assets have their own provenance and checksums in
`third_party/memman/NOTICE`.

## License

MSX-AI is released under the [MIT License](LICENSE). Third-party components
retain the terms documented in their respective notices.

## Known limitations

- Resident control is cooperative and depends on the BIOS interrupt hook chain.
- Resident RAM page 1 is unavailable; arbitrary page-3 writes are dangerous.
- Slot/mapper selection and agent-side call/run/stop are foreground-monitor
  features only.
- Exact physical screenshots may require palette/register overrides when
  software does not maintain BIOS shadows.
- SCREEN 9 is not implemented.
- Generic bank-switched cartridge mappers are not implemented.
- Actual BaDCaT SMD hardware validation is pending.
- Keyboard injection and physical reset are currently openMSX-only.
