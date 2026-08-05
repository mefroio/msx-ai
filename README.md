# MSX-AI

MSX-AI is an MCP server for live MSX development. It can control openMSX
directly, without TCP or an MSX-side agent, or communicate with an ASM agent
through a separately selected host transport. The currently implemented agent
transport is a transparent TCP/IPv4 byte stream. The physical-target path can
inspect and patch RAM/VRAM, pause and resume cooperative software, access
hardware ports, and render screenshots from captured VRAM.

The project is intentionally independent from a particular AI interface or
network adapter. MCP is the current public interface; the application loader,
framed protocol, screenshot renderer, and MSX-side command core are separate
layers.

openMSX, TCP/IP, and BaDCaT are all optional at the project level. A
physical-only installation does not need an openMSX executable, emulator ROMs,
disk images, BaDCaT hardware, a BaDCaT SDK, or BaDCaT firmware. The MCP server
does not probe or start an
emulator implicitly: select `msx_agent_listen` or `msx_agent_connect` for a
physical target, and select `msx_boot`, `msx_attach`, or `msx_tcp_bench_start`
only when the optional emulator backend is wanted.

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
                                                        `-- MSX-AI DOS suite
                                                            + MSXAI.COM
                                                            + MSXAIXF.COM
                                                            + MCP8251.TSR
                                                            + MCP16550.TSR
                                                            + MEMMAN.COM
                                                            + TL.COM
                                                            `-- TK.COM
~~~~

### Backend choices

| Use case | openMSX | MSX agent | TCP/IPv4 |
|---|---:|---:|---:|
| Direct openMSX control | Required | Not used | Not used |
| Physical MSX through the current agent backend | Not used | Required | Required by the current host adapter |
| Agent-path simulation in openMSX | Required | Required | Required between RS232-Net and the host |
| Both capabilities in one installation | Available | Available | Used only when the agent backend is selected |

Backend selection is explicit. One MCP server session controls one active
target at a time, but the same installation can switch between direct openMSX,
an openMSX-hosted agent simulation, and a physical agent without changing the
core server or installing a product-specific edition.

## Current capabilities

- Isolated openMSX sessions in headless or visible shared-window mode.
- A compact seven-file MSX-DOS suite. `MSXAI.COM` provides setup and the
  foreground monitor, one driver-selected `.TSR` becomes resident, and
  `MSXAIXF.COM` provides the transient large-file PUT/GET workspace.
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
- Credit-controlled BIOS keyboard input through the resident agent, including
  255-byte batching for `msx_type_lines` and credited, whole-file-CRC-checked
  ASCII or tokenized `.BAS` transfer for larger programs.
- Streaming, resumable binary `PUT` and `GET` between the host and MSX-DOS,
  with 32-bit sizes and offsets, end-to-end CRC-32, restart recovery, and
  collision-safe publication.
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

### Core MCP server

- Python 3.10 or newer.

The server has no mandatory third-party Python packages. Starting it, listing
its tools, and accepting or initiating a physical-agent TCP connection do not
look for openMSX or any particular network-adapter product.

TCP/IP is not required for direct openMSX control. It is currently required
only when selecting the real-agent protocol path, whether that agent runs on a
physical MSX or is simulated through openMSX RS232-Net. Additional host
transport adapters can implement the same ordered byte-stream contract without
changing the MCP tools or Z80 command core.

### Physical MSX target through the current agent backend

- For the default physical resident mode: MSX-DOS 2 or Nextor, a compatible
  memory mapper, and the MemMan 2.4+ API. The suite supplies verified
  public-domain MemMan 2.42 utilities as separate files and installs MemMan
  automatically when it is not already present.
- Transparent PackBits expansion after a compressed `PUT` uses bounded memory
  inside `MSXAIXF.COM`; raw `PUT` and `GET` use the same transfer state machine
  without codec post-processing.
- A supported MSX UART connected to a transparent TCP/IP bridge for a physical
  target.

The bridge may use any vendor or implementation as long as it exposes the
selected 8251 or 16C550-compatible byte interface to the MSX and provides a
transparent, ordered TCP/IPv4 stream. BaDCaT SMD is one intended validation
device, not a dependency.

### Optional emulator and development tools

- [openMSX](https://openmsx.org/) is required only for the emulator backend and
  emulator integration tests. Version 21.0 is the currently tested version.
- Legally obtained MSX system ROMs are required only by openMSX machine
  configurations that use proprietary firmware.
- Bas Wijnen's `z80asm` is required to build the ASM agent suite or use tools
  that assemble uploaded Z80 source. It is not required to start the MCP server
  with an already-built agent suite.

ROM images and bootable disk images are not distributed by this repository.

The optional emulator defaults can be overridden with these environment
variables:

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

## Physical-only workflow

Do not run `open-msx.command` or `open-msx-mcp.command`. Start the MCP server
through the configuration above, then select exactly one physical TCP role:

- call `msx_agent_listen` when the transparent adapter connects to the host;
- call `msx_agent_connect` when the adapter listens as a TCP server.

After the TCP handshake, backend-neutral tools operate through the resident
agent. With no selected backend, they fail with an instruction to connect a
physical target or explicitly boot the optional emulator; they never start
openMSX automatically.

## Optional openMSX workflows

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

`msx_tcp_bench_start` starts one isolated openMSX process, imports the complete
canonical seven-file suite, selects the 8251 driver, and connects it to the MCP
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

The launcher builds and stages the canonical seven-file suite, copies the local
MSX-DOS disk to a disposable runtime image, and starts one visible openMSX
instance with normal sound. It makes the suite available on the runtime disk
but does not start the agent automatically, so the user can choose resident or
foreground-monitor mode at the DOS prompt. It never modifies the base disk,
rejects a second openMSX process, and accepts only an IPv4 target.

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

Build the production agent package:

~~~~sh
make agent
~~~~

The canonical deployable suite is:

~~~~text
work/agent/MSXAI.COM
work/agent/MSXAIXF.COM
work/agent/MCP8251.TSR
work/agent/MCP16550.TSR
work/agent/MEMMAN.COM
work/agent/TL.COM
work/agent/TK.COM
~~~~

The build also creates `work/agent/build/MSXAI.TSR` and
`work/agent/build/MSXAI_TSR.INC` as internal template and relocation-metadata
inputs. They are
not deployable alternatives to the two fixed-driver TSRs listed above.

Copy all seven files to the same MSX-DOS directory and run the commands from
that current directory. The lifecycle intentionally resolves its external
MemMan utilities and fixed-driver TSR there; mixing files from different builds
is unsupported.

Seven files on disk do not mean seven images occupying RAM at once.
`MSXAI.COM`, the MemMan utilities, and `MSXAIXF.COM` are transient and hand off
execution sequentially. Installation selects only `MCP8251.TSR` or
`MCP16550.TSR` for resident allocation. The external `MEMMAN.COM` or `TK.COM`
image is staged in free high TPA alongside the small lifecycle front end, then
overlaid for its one action; installation subsequently lets external `TL.COM`
load the selected TSR. No temporary loader or TSR file is created, and there is
no installation-time `DEL` cleanup. The PackBits worker is loaded only while a
protocol-X transfer is active and returns its TPA to DOS when it exits.

Driver selection is explicit and case-insensitive:

| Command | Result |
|---|---|
| `MSXAI /DRIVER:8251` | Install/reconfigure the default resident TSR for a standard 8251 interface |
| `MSXAI /DRIVER:16C550` | Install/reconfigure the default resident TSR for a generic 16C550 interface |
| `MSXAI /DRIVER:8251 /MONITOR` | Start the non-resident foreground monitor with the 8251 driver |
| `MSXAI /DRIVER:16C550 /MONITOR` | Start the non-resident foreground monitor with the 16C550 driver |
| `MSXAI /DRIVER:8251 /MONITOR DEBUG` | Start the foreground monitor with visible command tracing |
| `MSXAI /UNINSTALL` | Remove the named resident TSR safely through MemMan |
| `MSXAIXF /PUT <32-hex-transfer-id>` | Run the foreground worker for a staged file-transfer-v2 upload |
| `MSXAIXF /GET <32-hex-transfer-id>` | Run the foreground worker for a staged file-transfer-v2 download |
| `MSXAI /?` or `MSXAI /HELP` | Display command-line help |

Exactly one `/DRIVER` is required for install or monitor mode.
`/UNINSTALL` must be used alone. Running the resident command again finds the
existing named TSR and changes its selected driver through MemMan instead of
installing a duplicate. Changing the live driver can disconnect the current
link, so reconnect through the newly selected interface. On a first install,
`/DRIVER:8251` selects `MCP8251.TSR`, while `/DRIVER:16C550` selects
`MCP16550.TSR`; an already resident agent is reconfigured through its existing
MemMan `TsrCall` entry.

The transfer-ID forms of `/PUT` and `/GET` belong to the small transient
`MSXAIXF.COM` helper; they do not install another agent. The host first stages
an immutable descriptor through framed opcode `X`, then starts the matching
helper at the DOS prompt. The resident performs only bounded framing and
mailbox work from `H.TIMI`; the transient process owns every MSX-DOS file call.

Replacing files on disk does not modify a TSR that is already resident in MSX
memory. To deploy a rebuilt resident, copy the complete matching suite, run
`MSXAI /UNINSTALL`, and then install it again with the selected `/DRIVER`. A new
host connection must negotiate with the reinstalled resident.

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
| On-screen `DEBUG` trace | No | Optional |

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

In the negotiated feature bitmap, bit `0x40` is reserved and remains clear.
Bit `0x80` advertises `file-transfer-v2`, the only DOS-file transfer path in
the current suite.

The optional `keybuf-input` HELLO feature enables compatibility opcode `t`. It
atomically enqueues up to 39 bytes in the BIOS keyboard ring and returns
accepted/pending counts. The host uses those counts as credits instead of
waiting for the ring to become completely empty after every fragment.

Current residents also advertise `keybuf-spool` and accept opcode `T`. It
accepts a control byte followed by up to 255 bytes into a private circular
spool and returns little-endian accepted, total-pending, and free-credit counts
plus state flags. `H.TIMI` drains the spool into the BIOS ring without calling
BIOS, BDOS, or BASIC. The host explicitly authorizes only one logical line at a
time. After Return, the resident waits for the BIOS ring to empty and four more
timer ticks before accepting authorization for the next line. This bounds stale
input instead of letting several queued commands run after a lost client. If a
request fails while the session can still transmit,
the host attempts the cancel control; reconnect also drops private queued
input. Hosts automatically fall back to opcode `t` with older
agents, and cached v3 responses prevent duplicate characters after retries.

The `file-transfer-v2` feature enables opcode `X`. Subprotocol version 1 has
`CAPS`, `OPEN`, `STATUS`, `PUT_DATA`, `GET_READ`, `GET_ACK`, `CLOSE`, and
`CANCEL` operations. An immutable OPEN descriptor binds a 16-byte transfer ID,
direction, encoding, DOS path, 32-bit wire and final sizes, independent CRC-32
values, and any resume boundary. Stateful requests carry that 128-bit ID;
offsets and progress counters are 32-bit. `CLOSE` is an explicit, replay-safe
end-of-stream operation, not an inference from a short block or disconnect.
The detailed state and payload layouts are in
[`agent/README.md`](agent/README.md) and `server/msx_transfer.py`.

`msx_key` uses that operation for ESC, Return, Tab, Select, and Space. STOP and
Ctrl+STOP use an idempotent one-byte RAM write to the documented BIOS `INTFLG`
work-area variable (`FC9Bh`, values `04h` and `03h` respectively). On the real
backend, Ctrl+C is a convenience alias for the Ctrl+STOP break event.

When the foreground monitor runs with `DEBUG`, the optional
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
shadows. `DEBUG` changes the foreground display and therefore appears in a
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

## Batched BASIC input and file transfer

`msx_type_lines` accepts an array of logical lines, appends Return to each one,
and waits for the target to consume the batch. On a current physical resident,
the host fills the negotiated keyboard spool in large packets and uses its
returned credits. On older residents it streams into available BIOS ring slots
while retaining the same Return barrier. Real-agent input tools return a
structured delivery acknowledgement and do not read VRAM automatically; use
`msx_screen` explicitly when a text capture is actually required.

`msx_run_basic` uses one batched input operation for short listings. When a
real target is confirmed at an MSX-DOS prompt with
`dos_prompt_confirmed=true` and the normalized ASCII listing is at least 512
bytes, `transfer="auto"` instead:

1. stages a temporary host-side `.BAS` image and opens a protocol-X PUT;
2. starts `MSXAIXF /PUT <transfer-id>` and streams the image with 32-bit
   offsets, rolling CRC-32, and explicit close/publication checks;
3. enters BASIC and loads the temporary file;
4. deletes the temporary file and runs the loaded program.

Use `transfer="type"` to force keyboard entry or `transfer="file"` to require
the file path. The latter requires `clear=true`, explicit DOS confirmation, and
a resident that advertises `file-transfer-v2`; `auto` falls back to typing
otherwise. Reusing a BASIC prompt requires the mutually exclusive
`allow_existing_basic=true` confirmation. The host never infers either state
from an automatic screen read. `dos_drive` (or `drive` for
`msx_run_basic_file`) selects the temporary target drive and defaults to `A`.
The BASIC tool retains a conservative 16 KiB input policy, but protocol X itself
uses 32-bit sizes and is not limited to 16 KiB. `format="auto"` preserves
tokenized files beginning with `0xFF` and normalizes other files as ASCII. No
BASIC memory layout or host-generated token stream is injected.

The host waits for complete protocol-X size, CRC-32, close, and publication
verification. The foreground worker performs cleanup and prints its final
status before publishing terminal `COMPLETE`, then immediately terminates. The
next BASIC command may therefore wait safely in the BIOS keyboard spool until
COMMAND2 consumes it. No DOS/BASIC prompt polling or hidden VRAM capture occurs.
A BASIC-entry or LOAD failure after successful publication but before BASIC
cleanup can leave the random `MXxxxxxx.BAS` target there; it may be deleted
normally from DOS.

## Main MCP tools

| Group | Tools |
|---|---|
| Session | `msx_boot`, `msx_attach`, `msx_tcp_bench_start`, `msx_agent_listen`, `msx_agent_connect`, `msx_status`, `msx_shutdown` |
| Execution | `msx_asm_load`, `msx_app_load`, `msx_pause`, `msx_resume`, `msx_stop` |
| Memory/video | `msx_memory_read`, `msx_memory_write`, `msx_screen`, `msx_screenshot` |
| Hardware | `msx_io_read`, `msx_io_write`, `msx_slot_select`, `msx_mapper_select` |
| Input | `msx_type`, `msx_type_line`, `msx_type_lines`, `msx_run_basic`, `msx_run_basic_file`, and `msx_key` |
| Files | `msx_file_put`, `msx_file_get` |
| openMSX/DOS | `msx_dos_asm_run`, `msx_disk_put_text`, `msx_reset`, `msx_cmd` |

Resident keyboard injection feeds the standard BIOS ring and therefore works
with DOS, BASIC, and software that calls BIOS character input. Games that read
the keyboard matrix directly do not observe those synthetic bytes. STOP and
Ctrl+STOP are delivered through the BIOS `INTFLG`; the real backend maps
Ctrl+C to the same break event for MCP client convenience. Physical reset, raw
matrix emulation, and openMSX console commands remain openMSX-only. Physical
operations use the agent byte stream rather than openMSX APIs. On a physical
target, `msx_run_basic` enters BASIC only after the caller sets
`dos_prompt_confirmed=true`. Reusing an already-visible BASIC prompt requires
the mutually exclusive `allow_existing_basic=true` opt-in. These confirmations
prevent a tool from typing over a running application without first reading
VRAM and disturbing the resident target's live VDP state.

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
- raw ZIP and PackBits protocol-X PUT/GET round trips through the resident TCP
  path in `test_resident_types_and_runs_basic_only_through_agent_tcp`, including
  a local-debugger screen regression that rejects hidden-capture glyph damage;
- foreground ASM upload/run/stop with visible `DEBUG` tracing;
- reconfiguration without a duplicate TSR;
- safe uninstall and an idempotent second uninstall.

It requires local machine ROMs and the ignored bootable image at
`work/system-disks/msxdos.dsk`. Headless instances remain host-muted without
changing emulated MSX sound behavior.

## Repository layout

~~~~text
agent/                    universal ASM agent, lifecycle, and UART drivers
agent/msx_xfer.asm        transient protocol-X PUT/GET and PackBits helper
agent/transports/         hardware-specific byte-stream implementations
server/msx_client.py      openMSX control client
server/msx_mcp_server.py  MCP JSON-RPC server and backend adapters
server/msx_protocol.py    protocol-v3 codec and incremental parser
server/msx_v3.py          framed stream session, retries, and correlation
server/msx_real.py        physical-agent stream/TCP client
server/msx_transfer.py    file-transfer-v2 wire contract and host helpers
server/msx_screenshot.py  host-side VRAM renderer
server/msx_application.py backend-neutral application parser/loader
tests/                    deterministic and opt-in integration tests
third_party/memman/       pinned public-domain MemMan assets and hashes
tools/                    reproducible agent and MemMan/TSR build helpers
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

The separately deployed MemMan assets have their own provenance and checksums
in `third_party/memman/NOTICE`.

## Resumable file transfer v2

`msx_file_put` uploads an arbitrary binary host file to MSX-DOS, and
`msx_file_get` downloads an MSX-DOS file to the host. Both stream blocks instead
of buffering the complete file in MSX memory. The initial call requires
`dos_prompt_confirmed=true`, set only after the caller has externally confirmed
an MSX-DOS prompt, so the host can start `MSXAIXF /PUT <id>` or
`MSXAIXF /GET <id>`. With `dos_prompt_confirmed=false`, the tool is fail-closed:
it may only reattach to an already-active matching transfer when `resume=true`.
That default resume mode survives a timeout, TCP disconnect, or IDE restart.

Generic PUT remains byte-exact except for an unambiguously textual `.BAS`
target. Numbered BASIC source is automatically converted to the MSX-DOS text
image expected by the interpreter: 8-bit source bytes are preserved, LF/CR
host endings become CRLF, and one final `0x1A` EOF marker is added before size,
compression, and CRC-32 are calculated. The result reports the normalization
and both the original and final sizes. Ambiguous or invalid `.BAS` input fails
before transmission. Tokenized BASIC beginning with `0xFF` and every non-BASIC
file are always transferred byte-exact.

PUT/GET never capture VRAM before or after a transfer. Success is established
by protocol-X terminal state, exact offsets and sizes, CRC-32, close, and the
required verification/publication flags. Results report
`completion="protocol-x-terminal-verified"`,
`prompt_check="not-performed"`, and `screen_capture_performed=false`. This is
intentional: the resident cannot portably inspect the VDP's internal write
address without disturbing it. The 32-hex transfer ID may wrap onto two rows on
a 40-column DOS screen; that wrapping is normal, while binary glyphs are not.

While `MSXAIXF.COM` is active, the MSX displays one compact in-place progress
line. Its 35-column layout also fits Brazilian machines that expose a 37-column
DOS console without touching the auto-wrap column:

```text
[#########---------]  50%   480 B/s
```

PUT refreshes it after every exact DOS write has been released or durably
committed; GET refreshes it after each acknowledged block. Percent thresholds
use the protocol's full 32-bit wire size and start at the recovered offset on a
resume. The displayed `B/s` is the latest confirmed-block rate measured from
BIOS jiffies, using the VDP's PAL 50 Hz or NTSC 60 Hz setting. The fixed
35-column line is rewritten with carriage return instead of scrolling. All
state and formatting code belongs to the transient helper, so DOS reclaims it
when the operation ends and the resident agent size is unchanged.

Protocol `X` protects each operation with an opaque 128-bit transfer ID and
immutable pathname, direction, size, and checksum bindings. It uses 32-bit
sizes and offsets, rolling prefix CRC-32 for resume reconciliation, explicit
GET acknowledgements, and an explicit `CLOSE` before final verification. The
host persists restart journals, while the MSX rechecks the actual partial-file
length and CRC before granting more PUT credit. A stale or mismatched session
cannot append to another transfer.

PUT mailbox writes and durable progress are deliberately distinct. Exact
writes release the 298-byte mailbox immediately, while `DOS_ENSURE` and the
durable offset advance in 8 KiB batches and at end of file. This avoids a disk
flush for every UART frame. The host caps uncommitted progress at 16 KiB and
journals only the target's `ENSURE`-backed boundary.

Publication is fail-closed in both directions. PUT writes a same-directory
partial and refuses an existing MSX destination; GET writes a host-side partial
and refuses an existing local destination. The final pathname appears only
after exact I/O, complete size, CRC-32, close, and publication checks succeed.
The MSX sidecar combines immutable transfer binding with complemented decoding,
publishing, and published phases. A restart can safely discard only owned
PackBits scratch output and can recognize a completed rename only after exact
final size/CRC validation. Successful PUT removes the sidecar; a lost terminal
reply can still be replayed from a complete fsync-backed host journal, again
only after validating the existing target byte-for-byte by size and CRC. A
separate monotonic close-intent bit is fsync'd only after the foreground helper
has reached the full durable boundary and before CLOSE. Receiptless replay is
not authorized without that bit, so a zero-byte journal written before OPEN
cannot mistake an unrelated empty destination for a completed transfer.
Legacy version-1 journals load as ordinary resumes without terminal authority
and therefore fail closed if their MSX sidecar is already gone.
Zero-byte files, files larger than 64 KiB, and interrupted transfers use the
same state machine. Each size field can represent up to 4,294,967,295 bytes,
but the practical limit is the smallest limit imposed by the host staging
space, MSX-DOS/filesystem, and available target media.

Compression is asymmetric and capability-negotiated in protocol version 1:

- `msx_file_put(compression="auto")` may create a deterministic standard
  PackBits stream only when the target advertises `PACKBITS_DECODE` and
  compression saves at least
  the larger of 256 bytes or three percent of the source;
- common archives and already-compressed media, including `.ZIP` and `.GZ`,
  remain raw and arrive byte-for-byte unchanged in the default `auto` mode;
- `compression="raw"` preserves the prepared representation without transport
  encoding, while `compression="packbits"` is an explicit override and
  requires the negotiated decoder; textual BASIC normalization, when selected
  by the `.BAS` target, occurs before either compression policy;
- `msx_file_get` always transfers raw bytes because the current MSX component
  provides a PackBits decoder, not a PackBits encoder.

For a PackBits PUT, the compressed wire stream has its own size and CRC-32. After
explicit CLOSE, the MSX expands it and independently checks the declared final
size and CRC-32 before publishing the requested destination. This is transparent
transport compression: a source ZIP remains a ZIP, and a normally compressed
source is restored under the requested filename rather than left encoded.
`MSXAIXF.COM` decodes the stream incrementally with a fixed 318-byte buffer;
there is no external codec installation or whole-file RAM requirement. The
decoder rejects truncated, overlong, reserved, and non-canonical packets before
publication. The file-transfer state machine is part of the common framed
agent protocol and has no BaDCaT-specific commands or branches. MCP/TCP over
8251, 16C550, or a future transparent byte-stream transport changes throughput
and block pacing, not the transfer semantics.

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
