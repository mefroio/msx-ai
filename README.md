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
- A true MemMan TSR by default: it returns to MSX-DOS and polls from the BIOS
  `H.TIMI` hook while cooperative DOS programs or games run.
- An optional foreground monitor for direct upload, call, run, stop, slot, and
  mapper workflows.
- Runtime driver selection for a standard MSX 8251 RS-232 interface or a
  generic 16C550-compatible UART.
- A stream-safe protocol with sequence numbers, CRC-16/CCITT-FALSE,
  request/response correlation, negotiated payload limits, and transport-failure
  quarantine for the safe resident profile.
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

The current MemMan resident is cooperative. It polls and dispatches protocol
work only from the BIOS `H.TIMI` hook and masks receive interrupts in both UART
drivers. A minimal `H.KEYI` guard prevents an older serial firmware hook from
consuming receive data first, but never polls the UART or enters the protocol
parser. The resident can inspect cooperative software that continues to execute
the standard timer hook, including ordinary DOS programs launched after the TSR
is installed.

Software that keeps `DI` active, replaces the BIOS interrupt service routine,
removes `H.TIMI` from its chain, or pages the required system state out simply
does not service the resident. The host receives a bounded timeout and
quarantines that attachment instead of retrying into an unknown machine state.
Unconditional control requires an independent NMI, bus-master, or equivalent
hardware path. A transparent TCP/UART bridge does not create that capability by
itself.

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
DOS program or game in the visible window, then use `msx_status` and the
atomic `msx_memory_read`, `msx_memory_write`, and `msx_screenshot` operations.
Each operation acquires a bounded snapshot lease and resumes the program
immediately. Persistent `msx_pause`/`msx_resume` is intentionally unavailable
in the safe resident profile.

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

Rebuilding or replacing `MSXAI.COM` does not modify a TSR that is already
resident in MSX memory. To deploy this safety correction, copy the rebuilt
`MSXAI.COM`, run `MSXAI /UNINSTALL`, and then install it again with the selected
`/DRIVER`. A new host connection must negotiate with the reinstalled resident.

### Supported UART drivers

| Driver | Current configuration | Notes |
|---|---|---|
| `8251` | Ports `80h/81h`, standard timer ports `84h/85h/87h`, 19,200 baud, 8N1 | Resident masks `COMMSK` UART IRQs and polls from `H.TIMI` |
| `16C550` | Ports `80h-87h`, 1.8432 MHz reference clock, divisor 1 (115200 baud), 8N1, 16-byte FIFO, automatic RTS/CTS | Resident keeps `IER=0` and polls from `H.TIMI`; hardware flow control is required |

BaDCaT SMD is an intended 16C550-compatible device, not a dependency or a
separate build. The agent contains no BaDCaT-specific AT commands, networking
UI assumptions, or product branches. Physical BaDCaT validation is pending
arrival of the hardware.

The generic driver programs a 115200-baud UART line; that value is not a
guarantee of end-to-end payload throughput. The
[published BaDCaT specification](https://sites.google.com/view/badcatelectronics/msx/badcat-wifi-modem)
lists 57,600 bps effective throughput. Physical validation will measure the
actual sustained MCP rate, including UART flow control and bridge overhead.

See [agent/README.md](agent/README.md) for the lifecycle, memory model, raw and
framed protocols, transport ABI, and driver implementation details.

## Resident and foreground modes

| Behavior | Default resident | `/MONITOR` |
|---|---:|---:|
| Returns to DOS | Yes | No |
| Observe a normally launched DOS program/game | Yes | No |
| Pause/read/patch/screenshot/resume | Bounded atomic lease; no persistent manual pause | Yes |
| BIOS keyboard input | Yes | No |
| Agent-side `call` and `run` | No | Yes |
| Agent-side `stop` | No | Yes |
| Slot and mapper selection | No | Yes, pages 0 and 1 |
| On-screen `DEBUG ON` trace | No | Optional |

The resident reports execution state `running` while DOS or an application is
active. The safe profile disables persistent manual `msx_pause`; atomic memory
and screenshot operations use the bounded `S` lease and resume the saved CPU
context immediately after acquisition. `stop` remains rejected because
discarding the interrupted DOS/application context would be unsafe.

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

Host-created IPv4 TCP streams enable `TCP_NODELAY`. Both current UART drivers
advertise the transport-independent `frame-wake-ack` feature. For each framed
request the host sends the first magic byte and waits until the agent returns
`0x06`, proving that an `H.TIMI` service opportunity has entered the parser,
before releasing the remainder. The handshake is mandatory for the FIFO-less
8251 and gives the 16C550 the same deterministic wake-up contract. Raw command
`N` exposes this feature before the v3 upgrade, so a current host can require
the ACK on the first framed request instead of probing with an uncredited frame.

The same credit applies to reconnection. A current framed agent returns `0x06`
for each `ESC` in the eight-byte recovery marker, and the host sends the next
`ESC` only after receiving that ACK. After the eighth credited byte, the agent
returns to raw bootstrap and emits a fresh HELLO. If the first probe receives no
credit, the host stops without releasing the remaining seven bytes.

Neither UART driver knows about TCP, adapter configuration, or a particular
network product. Both consume the same ordered byte stream and work behind a
transparent TCP/IPv4-to-UART bridge that preserves the selected serial settings
and any required hardware flow-control signals. `/DRIVER:16C550` selects that
generic UART contract; BaDCaT SMD is one intended physical validation target,
not a protocol dependency. Another bridge can occupy the same layer when it
presents the compatible UART interface and transparent IPv4 byte stream.

A cartridge ROM supplies firmware bytes only. By itself it does not emulate the
16C550 register file, FIFO and timing, RTS/CTS behavior, or the transparent TCP
peer. Emulator validation therefore requires a compatible UART device model and
bridge in addition to any ROM image.

## Protocol v3

The host performs a small raw-v2 capability bootstrap before upgrading a
capable agent. A raw agent rejects the initial single-`ESC` state probe with
`E,01`; the host then sends `?` for its four-byte HELLO. A current agent left in
framed mode instead ACKs that byte and each of the remaining seven `ESC` bytes,
then emits the raw HELLO itself. After HELLO, raw command `N` returns `K`
followed by the same one-byte feature bitmap later present in the v3 HELLO.
Older agents reject `N` with `E,01`. Only after this pre-v3 negotiation does the
host send `F` and switch to this transport-independent frame:

~~~~text
"MX" | version | type | flags | sequence:u16le | opcode | status |
length:u16le | payload | crc16:u16le
~~~~

CRC covers the complete frame except the CRC field and uses
CRC-16/CCITT-FALSE. When the negotiated profile permits a retry, it reuses the
identical encoded request and sequence number. The agent de-duplicates
state-changing commands and searches for the next valid magic/header/CRC
combination after damaged input.

The negotiated payload limit of the current agent is 320 bytes. Eight
consecutive credited `ESC` bytes reset a current framed protocol session after
a lost peer; a single noise byte cannot downgrade it.

The optional `keybuf-input` HELLO feature enables opcode `t`. It atomically
enqueues bytes in the BIOS keyboard ring and returns accepted/pending counts.
The host waits for each line to be consumed before sending the next one, so a
BASIC line editor cannot discard commands queued after Return. Cached v3
responses also prevent a retried request from typing duplicate characters.
`msx_key` uses that operation for ESC, Return, Tab, Select, and Space. STOP and
Ctrl+STOP use an idempotent one-byte RAM write to the documented BIOS `INTFLG`
work-area variable (`FC9Bh`, values `04h` and `03h` respectively). On the real
backend, Ctrl+C is a convenience alias for the Ctrl+STOP break event.

When the foreground monitor runs with `DEBUG ON`, the optional
`debug-peer-label` feature enables opcode `I`. Immediately after HELLO, a TCP
host sends the accepted IPv4 source endpoint as printable ASCII and the MSX
displays it as `MCP client: <ipv4>:<port>`. The UART-facing agent remains
transport-neutral: it never attempts to discover network metadata itself.

The optional `snapshot-lease` feature enables v3 opcode `S`. Its one-byte
payload is a lease of 1-255 bounded agent receive-timeout periods; these are not
wall-clock seconds. Valid protocol traffic refreshes the lease, while silence
eventually resumes the interrupted program. Atomic MCP RAM/VRAM reads and
writes use this lease.

Bit 4 of the HELLO feature byte is `timi-poll-safe`. It is valid only for a
resident that also advertises `frame-wake-ack`. After negotiating this profile,
the host makes exactly one attempt per request. A terminal timeout, disconnect,
or send/receive failure with indeterminate delivery quarantines all further
writes on that attachment: the host does not retry the request and does not send
`g`, status, or reconnect bytes whose late arrival could re-enter an
incompatible game. If a snapshot lease had been accepted, its agent-side
countdown is the authoritative recovery path and auto-resumes the MSX. A fresh
connection is required before issuing more commands. Persistent manual
`msx_pause` is rejected for this profile.

The raw `N` exchange makes that decision possible before the first v3 frame. A
legacy resident that rejects `N` or cannot advertise both `timi-poll-safe` and
`frame-wake-ack` is rejected before `F` and before any RAM or VRAM request. If a
legacy peer was already abandoned in framed mode, silence after the initial
single-`ESC` probe also fails safely: the host sends no uncredited recovery
burst and requires the agent to be restarted or updated. A legacy foreground
monitor that begins in raw mode retains the compatibility upgrade path.

On the successful path, the host sends `g` immediately after the final requested
RAM/VRAM byte and completes the resume before rendering or encoding an image.
It also verifies that the lease remained active through acquisition; an expired
or otherwise uncertain capture is discarded. `frame-wake-ack` is transport flow
control rather than an MCP response, so the correlated framed response remains
unchanged.

## Screenshots from VRAM

`msx_screenshot` captures VRAM and VDP/BIOS state, renders the image on the
host, and returns a PNG MCP image content block. It does not require a visible
openMSX renderer. On a running resident target, `atomic=true` requires both
`snapshot-lease` and `timi-poll-safe`. The initial status request is bounded;
failure before `S` leaves the game untouched and quarantines the connection.
After the lease is acknowledged, the target remains paused only for acquisition
and resumes before host-side rendering, PNG compression, file reading, or Base64
encoding. Older residents are rejected before display-memory reads; rebuild,
uninstall, and reinstall the current `MSXAI.COM`. Explicit `atomic=false` is a
non-atomic diagnostic escape hatch and does not provide a consistency guarantee.

For a target that reports the 19,200-baud 8251 driver, the host reads only the
small display metadata under the lease, then estimates payload, frame overhead,
request count, and transfer time before bulk VRAM acquisition. Captures above
the safety threshold are refused by default and the short lease is released
immediately. Pass `allow_slow=true` to opt in to the long transfer. This guard
also applies to `atomic=false`, because it protects the slow link as well as
the paused application.

This API produces snapshots, not a video stream. Typical full bitmap captures
need roughly 27 KiB for SCREEN 5/6 and 54 KiB for SCREEN 7/8/10/11/12, plus
framing and optional sprite tables. That is commonly 17-37 seconds over the
19,200-baud 8251. Roughly 3-6 seconds is an ideal lower-bound range for a generic
115200-baud 16C550 path, not a BaDCaT performance promise. Its published
effective rate is 57,600 bps, and physical validation must measure the sustained
rate and capture time. Real-time visual monitoring would require a future
incremental or compressed capture protocol instead of repeated full-page VRAM
dumps.

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
| Input | `msx_type`, `msx_type_line`, `msx_run_basic`, and `msx_key` (both backends) |
| openMSX/DOS | `msx_dos_asm_run`, `msx_disk_put_text`, `msx_reset`, `msx_cmd` |

Resident keyboard injection feeds the standard BIOS ring and therefore works
with DOS, BASIC, and software that calls BIOS character input. Games that read
the keyboard matrix directly do not observe those synthetic bytes. STOP and
Ctrl+STOP are delivered through the BIOS `INTFLG`; the real backend maps
Ctrl+C to the same break event for MCP client convenience. Physical reset, raw
matrix emulation, and openMSX console commands remain openMSX-only. Physical
operations use the agent byte stream rather than openMSX APIs. On a physical
target, `msx_run_basic` enters BASIC automatically
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

- Resident control is cooperative and depends specifically on the BIOS
  `H.TIMI` chain; software that disables or replaces it times out without
  asynchronous intervention.
- Resident RAM page 1 is unavailable; arbitrary page-3 writes are dangerous.
- Slot/mapper selection and agent-side call/run/stop are foreground-monitor
  features only.
- Exact physical screenshots may require palette/register overrides when
  software does not maintain BIOS shadows.
- SCREEN 9 is not implemented.
- Generic bank-switched cartridge mappers are not implemented.
- Actual BaDCaT SMD hardware validation is pending.
- The current 16C550 profile assumes base port `80h`, a 1.8432 MHz UART clock,
  a 16-byte FIFO, and working MCR AFE. Other mappings or chip variants require
  a separate driver profile.
- The 16C550 driver relies on framed CRC/timeouts for line-error recovery; it
  does not yet report LSR overrun, parity, framing, or break telemetry. Its
  bounded serial wait is an instruction-loop budget, so physical and turbo-CPU
  validation is required before claiming a sustained maximum rate.
- The standard 8251 driver restores the previous interrupt mask, but its UART
  mode and timer programming are not readable as a complete prior profile; the
  interface remains configured for 19,200-baud 8N1 after uninstall.
- Physical reset and raw keyboard-matrix emulation remain openMSX-only. BIOS
  key injection requires a resident agent that negotiates `keybuf-input`;
  STOP/Ctrl+STOP additionally require writable BIOS work-area RAM.
