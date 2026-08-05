# MSX-AI physical-target agent

`MSXAI.COM` is the MSX-DOS side of the physical-target backend. The same
executable contains the protocol core, the MemMan resident lifecycle, the
foreground monitor, and every supported UART driver. Runtime command-line
options select the driver and operating mode.

The external contract is a full-duplex byte stream. TCP roles, MCP tools,
application parsing, and screenshot rendering live on the host and are not
embedded in the Z80 code.

## Build

From the repository root:

~~~~sh
make agent
~~~~

The only production executable is:

~~~~text
work/agent/MSXAI.COM
~~~~

Internal build products include:

- `work/agent/MSXAI.TSR`: the relocatable MemMan payload embedded in the COM;
- `work/agent/MSXAI_TSR.INC`: generated relocation/patch metadata;
- `work/agent/vendor/MEMMAN.COM`, `TL.COM`, and `TK.COM`: verified
  public-domain MemMan 2.42 components embedded into the final executable.

These internal files are not alternative agents and should not be copied to a
release disk. Their pinned Base64 sources, SHA-256 values, and redistribution
notice are under `third_party/memman/`.

Set a different assembler when necessary:

~~~~sh
Z80ASM=/path/to/z80asm make agent
~~~~

## Command line

The parser is case-insensitive. Exactly one driver is required for installation
or monitor mode.

~~~~text
MSXAI /DRIVER:8251
MSXAI /DRIVER:16C550
MSXAI /DRIVER:8251 /MONITOR
MSXAI /DRIVER:16C550 /MONITOR
MSXAI /DRIVER:8251 /MONITOR DEBUG ON
MSXAI /DRIVER:16C550 /MONITOR DEBUG ON
MSXAI /UNINSTALL
MSXAI /PUT A:PROGRAM.BAS 1234 29B1
MSXAI /?
MSXAI /HELP
~~~~

`/UNINSTALL` must be used alone. `DEBUG ON` is accepted only with
`/MONITOR`; it is rejected for the default resident lifecycle. `/PUT` is a
separate host-driven transient action: its arguments include a hexadecimal byte
count from `0001` through `4000` and a four-digit CRC-16/CCITT-FALSE value.

The startup banner reports the selected driver and runtime mode before control
passes to MemMan or the foreground monitor.

## Runtime modes

| Behavior | MemMan resident (default) | Foreground `/MONITOR` |
|---|---:|---:|
| Returns to MSX-DOS | Yes | No |
| Monitors DOS-launched software | Yes | No |
| RAM/VRAM and direct I/O | Yes, with resident memory restrictions | Yes, outside protected monitor memory |
| Pause/resume | Bounded snapshot lease; persistent manual pause disabled | Yes |
| BIOS keyboard-buffer input | Yes | No |
| Transient DOS file sink | Yes, by launching the same COM from DOS | Not applicable |
| Direct call/run/stop | No | Yes |
| Slot/mapper selection | No | Yes, pages 0 and 1 |
| `DEBUG ON` | Rejected | Optional |

### Default MemMan resident

The default command installs a genuine named MemMan TSR and returns to the DOS
prompt. It requires MSX-DOS 2 or Nextor and the MemMan 2.4+ API.

The first installation follows this lifecycle:

1. Parse options and print the universal agent banner.
2. Discover a compatible existing MemMan through `EXTBIO`.
3. If MemMan is absent, overlay the verified embedded `MEMMAN.COM`.
4. Create collision-safe temporary loader/TSR filenames without overwriting
   existing files.
5. Patch the selected driver ID into the emitted TSR and load it through
   MemMan.
6. Delete the temporary files and return to DOS.

The resident ID is the fixed 12-byte MemMan name `MSXAI MCP1  `. Re-running
`MSXAI` with a driver finds that ID and uses `TsrCall` to reconfigure the
existing resident instead of installing a duplicate. Reconfiguration restores
the previous UART state, binds the new driver, resets protocol sequencing, and
initializes the new UART.

Changing drivers can drop the active host connection. Reconnect through the
newly selected interface.

`MSXAI /UNINSTALL` discovers the named TSR and overlays the embedded MemMan
`TK.COM` directly with that ID. MemMan detaches the registered `H.KEYI` guard
and `H.TIMI` service hook before the agent's kill entry restores UART state. No
uninstall helper is written to disk. Repeating the command is safe and reports
that the agent is not installed.

Replacing the COM file does not patch a TSR already loaded in MSX memory. After
copying a rebuilt `MSXAI.COM`, run `MSXAI /UNINSTALL` and install it again with
the selected `/DRIVER`. This uninstall/reinstall step is required to activate
the `timi-poll-safe` resident correction.

MemMan versions older than 2.4 are rejected. A loader failure removes only
temporary files proven to have been created by the current invocation and
reports incomplete cleanup rather than overwriting or deleting an existing
file.

### Transient DOS file sink

The host can transfer a 1..16 KiB file without simulating every character. It
types `MSXAI /PUT <name> <hex-length> <crc16>` at the DOS prompt, waits for the
foreground receiver, and sends credited `U` frames containing up to 318 data
bytes. The resident places one validated frame in a mailbox. The foreground
process copies that mailbox through MemMan `TsrCall`, writes it with MSX-DOS 2,
and releases the next credit. No host write is made into the command processor's
TPA before the transient process owns it. `CREATE_NEW` prevents overwriting an
existing pathname; exact-write checks and a whole-file CRC protect completion.
A terminal success state is exposed to the host only after the CRC, exact-write,
and close checks pass. A ten-second NTSC/twelve-second PAL no-progress timeout
deletes a partial file.

The resident hook itself never calls DOS. `/PUT` runs as an ordinary transient
foreground process, which is why the file operation is safe and independent of
the selected 8251 or 16C550 transport. The host uses random 8.3 names on the
drive shown by the current DOS prompt and deletes the temporary file after
BASIC loads it.
The host uses a separate bounded 30-second finalization window after all bytes
have been accepted, allowing the foreground process to verify the CRC, close
the file, and publish its terminal result even over a slow 8251 connection.
If the host disappears, BASIC cannot start, or LOAD fails between successful
file creation and BASIC cleanup, the random `MXxxxxxx.BAS` pathname can remain
on that drive and may be deleted normally.

### Foreground monitor

`/MONITOR` selects the non-resident supervisor used for direct development
payloads. It remains in the foreground, relocates its protected image to
`0x8600`, installs its hooks and BDOS proxy, and lowers the available TPA.

This mode can:

- upload code and data;
- call a returning routine;
- run code asynchronously;
- pause, inspect, patch, and resume that code;
- stop it and return to the upload monitor;
- select slots or memory-mapper segments for CPU pages 0 and 1.

The foreground image at `0x8600` and above is protected from host writes. The
fixed address is not used by the default MemMan lifecycle.

## Debug tracing

Debug is disabled by default. `DEBUG ON` prints each foreground protocol
opcode as `[XX]`, where `XX` is the uppercase hexadecimal byte. A v3 TCP host
also sends its accepted IPv4 source endpoint once after HELLO when the agent
advertises the `debug-peer-label` feature; the screen prints
`MCP client: <ipv4>:<port>`.

For repeatable openMSX testing, the repository-level
`open-msx-mcp.command` launcher builds and installs this canonical executable,
makes it available on a disposable runtime disk, and waits for an IPv4 MCP
listener at `127.0.0.1:6603`. It leaves the agent at the DOS prompt for the
user to start explicitly in resident mode or with `/MONITOR DEBUG ON`.

Tracing is deliberately limited:

- it is accepted only with `/MONITOR`;
- it uses the foreground BDOS proxy for console output;
- commands serviced from an interrupt hook remain silent, even in debug mode,
  to avoid re-entrant BIOS output and corruption of the running application.

Debug output changes the MSX display and therefore appears in screenshots.
Resident mode never draws protocol activity on the target screen.

## Execution and hooks

The MemMan resident polls and dispatches protocol work only from `H.TIMI`. Both
UART drivers mask receive interrupts while selected, so incoming bytes cannot
asynchronously enter a game during temporary slot, stack, or VDP state. A
minimal `H.KEYI` guard performs no UART I/O and no protocol parsing; it only
suppresses an older serial firmware hook that would otherwise read receive data
before `H.TIMI`. Each timer entry saves the normal and alternate Z80 register
sets, moves protocol dispatch to a dedicated agent stack, and applies a fixed
I/O wait budget. `H.TIMI` always continues its previous chain after service.

The resident reports state `running` while DOS or a DOS-launched application is
active. The safe host profile rejects persistent lowercase-`s`/`msx_pause` and
uses only the bounded uppercase-`S` snapshot lease. The lease preserves the
interrupted CPU context, refreshes on valid traffic, and auto-resumes after
bounded transport silence. A partial request or missing peer times out and
unwinds instead of leaving the MSX inside the parser.

Direct `call`, `run`, and `stop` are not advertised by the resident TSR:

- DOS software must be launched normally after installation;
- `stop` cannot safely discard the interrupted DOS/application context;
- use `/MONITOR` when the host must launch or abandon injected code.

The control path remains cooperative. Code that holds `DI`, replaces the BIOS
ISR, removes `H.TIMI` from the chain, or pages required system state out does not
service the resident. The host performs no retry after a bounded timeout or
indeterminate link failure and quarantines further writes, so incompatibility
results in no response rather than asynchronous intervention. Unconditional
access requires hardware NMI, bus-master, or equivalent supervisor support.

The framed `t` command injects ASCII/key codes into the standard 40-byte BIOS
keyboard ring (`KEYBUF`) while the hook already has interrupts disabled. It
publishes `PUTPNT` only after copying the accepted bytes and never calls BIOS,
BDOS, or BASIC from the hook. Its two-byte response reports accepted and
pending counts. Host code stops batches at each Return and waits for pending
input to reach zero before sending another line, because BASIC may clear the
remaining keyboard buffer after accepting a line. Software that scans the
hardware keyboard matrix directly is outside this mechanism.

A current resident additionally advertises `keybuf-spool` and accepts framed
opcode `T`. Its private 256-byte circular allocation holds at most 255 bytes,
preserving an unambiguous empty state. Each response contains
`accepted:u16`, total `pending:u16`, free `credits:u16`, and a one-byte flags
field. A non-empty request starts with a control byte; bit 0 authorizes one
logical line and bit 1 cancels queued MCP/BIOS input when sent alone. `H.TIMI`
drains the spool directly into `KEYBUF`, stopping at every Return. It waits until
the BIOS ring is empty and then four further timer ticks before the host may
authorize the next line. The host can therefore submit multiple lines in one
255-byte data batch and refill only when reported credits are available, while
a lost peer cannot autonomously drain all later commands. Older agents continue
to use opcode `t`.

The host-side `msx_key` tool maps ESC, Return, Tab, Select, and Space to the
same atomic keyboard-ring operation. STOP and Ctrl+STOP are BIOS events, not
character bytes: the real backend writes `04h` or `03h` respectively to the
documented `INTFLG` byte at `FC9Bh` through the framed RAM-write command.
Ctrl+C is exposed as a convenience alias for the Ctrl+STOP break event. This
interrupts MSX-BASIC and other cooperative BIOS software without claiming to
emulate a physical key matrix.

## Memory model

MemMan relocates the TSR within a managed segment. When a hook or talk entry is
running, that segment is mapped into CPU page 1. The agent must execute there,
so it cannot expose the interrupted program's page-1 contents through the same
call.

Resident RAM behavior is therefore:

| CPU range | Resident access |
|---|---|
| `0x0000-0x3FFF` (page 0) | Read/write through `RAMAD0` using BIOS `RDSLT`/`WRSLT` |
| `0x4000-0x7FFF` (page 1) | Rejected for every overlapping range |
| `0x8000-0xBFFF` (page 2) | Direct read/write |
| `0xC000-0xFFFF` (page 3) | Direct read/write, but dangerous |

Page 3 contains BIOS/DOS variables, stacks, hooks, and other live system state.
Reads are needed for status and screenshot metadata; arbitrary writes can
immediately crash or corrupt the machine.

Resident slot and mapper commands are rejected in both raw and framed
protocols and their capability bit is cleared. MemMan's inter-slot return would
restore a changed slot, while changing an interrupted program's page-0 mapper
segment is unsafe. Mapping remains a foreground-monitor feature only.

The 16-bit RAM range checks accept an exclusive end exactly at `0x10000` and
reject every wrapping range. VRAM commands use the separately detected
installed capacity.

## Protocols

### Raw v2 bootstrap

The compact raw protocol remains the discovery and compatibility layer:

~~~~text
?                         hello/capabilities
N                         pre-v3 feature query -> K, feature byte
q                         state/status
r/p                       RAM read/write
v/w                       VRAM read/write
c/j                       call/run (foreground monitor only)
s/g                       legacy/manual pause/resume (not exposed by safe resident host)
k                         stop (foreground monitor only)
i/o                       direct I/O read/write
l/m                       slot/mapper select (foreground monitor only)
F                         upgrade to framed protocol v3
z                         remove the foreground monitor only
~~~~

The MemMan TSR cannot remove itself from inside its active hook. Use a separate
`MSXAI /UNINSTALL` invocation so MemMan can detach the hooks before calling
the kill entry.

Raw transfers use a one-byte length. New hosts upgrade immediately to v3 for
larger negotiated chunks, explicit status codes, CRC, and request correlation.
Before sending `F`, a current host sends `N`; the agent returns `K` followed by
the feature bitmap. An older raw agent treats `N` as unknown and returns
`E,01`.

### Framed v3

Every message uses this little-endian layout:

~~~~text
offset  size  field
0       2     magic: "MX"
2       1     version: 3
3       1     type: request/response/event
4       1     flags
5       2     sequence number
7       1     opcode
8       1     status
9       2     payload length
11      N     payload
11+N    2     CRC-16/CCITT-FALSE
~~~~

CRC covers the full header and payload. Sequence numbers are monotonic modulo
16 bits. Retrying an identical request reuses its sequence; cached responses
prevent state-changing commands from executing twice. Bulk RAM/VRAM reads may
be recomputed because they are side-effect free.

The current negotiated payload limit is 320 bytes. Request and response storage
share one buffer to keep the resident compact. A split lookup-table
implementation accelerates CRC processing.

The v3 HELLO appends an optional feature byte after the runtime-mode byte. Bit
0 advertises `keybuf-input`; bit 1 advertises the foreground-debug-only
`debug-peer-label`; bit 2 advertises the resident-only `snapshot-lease`; bit 3
advertises the transport-independent `frame-wake-ack`; and bit 4 advertises
resident-only `timi-poll-safe`; bit 5 advertises the resident-only
`keybuf-spool`; and bit 6 advertises resident-assisted foreground
`file-upload`. The safe features are valid only together with `frame-wake-ack`.
Raw command `N` exposes the same bitmap before `F`, allowing
the first framed request to use the negotiated wake and no-retry policy. Older
9-byte and 14-byte framed HELLO responses remain valid.
Opcode `t` accepts zero bytes as a queue-status query or up to 39 input bytes
and returns `[accepted, pending]`. Opcode `I` accepts 1..63 printable ASCII
bytes and displays the host-provided peer label. The one unused ring position
preserves the BIOS convention that equal get/put pointers mean empty. Because
the result is cached by sequence, response loss and retry cannot repeat either
side effect.

Opcode `T` accepts zero bytes as a read-only queue/credit query. A non-empty
request is `control:u8 | data`; data is limited to 255 bytes. Control bit 0
authorizes one line and bit 1 cancels when sent without data. Its seven-byte
response is `accepted:u16 | pending:u16 | credits:u16 | flags:u8`; flag bits
0..2 report the post-Return barrier, an active line, and unused authorization.
`pending` includes both the private spool and BIOS ring.

Opcode `U` is available only while a foreground `/PUT` receiver has registered
its mailbox. A request is `offset:u16 | data` with up to 318 data bytes; an
offset-only request polls credit. The response is
`accepted:u16 | received:u16 | credits:u16 | flags:u8`, where flag bits 0..4
mean active, mailbox pending, all bytes accepted, foreground success, and
foreground failure. Success is terminal and is published only after the
foreground CRC, exact-write, and close checks. The hook never calls BDOS.

Opcode `S` accepts exactly one non-zero lease byte while the resident is
servicing a running program from a hook. After acknowledging it, the agent
enters the paused command loop. Every successfully serviced frame reloads the
current lease, while each complete transport timeout decrements only that
current value. Opcode `g` resumes immediately; reaching zero auto-resumes if
`g` or its acknowledgement was lost. Lowercase `s` stores a zero lease, but the
safe resident host deliberately refuses that persistent operation.

With `frame-wake-ack`, consuming the first `M` of a framed request produces raw
byte `0x06`. The host waits for that byte before releasing `X` and the remaining
frame. This proves that an `H.TIMI` service opportunity has entered the parser
and prevents an overrun in the 8251's one-byte receive register. Both the 8251
and 16C550 advertise the same wake contract, keeping framing independent of the
selected byte-stream driver.

The reconnect marker uses the same credit. For each of its eight consecutive
`ESC` bytes, the agent emits `0x06` and the host sends the next byte only after
that ACK. The eighth byte resets framed state and makes the agent emit a fresh
raw HELLO. At initial attachment, an agent already in raw mode rejects the first
single-`ESC` probe with `E,01`, after which the host sends `?` normally. Silence
after that first probe means a legacy framed peer: the host does not send the
remaining seven uncredited bytes.

For `timi-poll-safe`, the host sets framed retries to zero. A terminal timeout,
disconnect, or send/receive failure with indeterminate delivery
write-quarantines the attachment, so no retry, resume, status, or reconnect byte
is sent into an uncertain target state. If `S` was acknowledged, the bounded
agent lease is responsible for automatic recovery. On success, `g` restores the
application before any host-side PNG rendering or encoding begins.

A resident must advertise both `timi-poll-safe` and `frame-wake-ack` through
raw `N` before the host sends `F`. A legacy resident that rejects `N`, or a
legacy framed peer that cannot credit the initial `ESC`, fails closed before
any v3 RAM/VRAM operation. Rebuild, uninstall, and reinstall `MSXAI.COM` to
update a resident. Only a legacy foreground monitor that begins in raw mode
retains the compatibility upgrade path. One accidental `ESC` remains
insufficient to downgrade a session.

## VRAM and screenshots

The agent discovers the Main-ROM version through its actual slot instead of
trusting CPU address `0x002D`, which contains DOS RAM under MSX-DOS. On MSX2
and newer it reads the BIOS `MODE` capacity descriptor.

Reported addressable capacity is:

- MSX1: 16 KiB;
- 64 KiB MSX2: four 16 KiB banks;
- 128 KiB MSX2/MSX2+: eight 16 KiB banks.

Each v3 VRAM request validates its bank and range before changing VDP register
14. The previous register-14 shadow is restored on normal completion and
timeout unwind.

Screenshot rendering is host-side in `server/msx_screenshot.py`. For a
physical target, the agent supplies VRAM plus readable BIOS VDP state; the host
does not use an openMSX renderer or debugger. Standard SCREEN 0-8 and SCREEN
10-12, sprites, pages, scroll, and palette overrides are supported.

Palette registers and some direct VDP state are write-only. Software that does
not update BIOS shadows may need an explicit palette override and can still
produce imperfect captures for other stale state. Raster effects, borders,
analog artifacts, and exact interlaced-field timing are not reconstructed.

## Transport implementation

The universal build includes both current drivers and binds six `JP` operands
once during initialization. There is no driver-selection branch in the
per-byte hot path.

The core calls:

~~~~text
transport_init
transport_restore
transport_rx_ready
transport_tx_ready
transport_read
transport_write
~~~~

The selected vector table maps those entries to namespaced driver routines.
Drivers expose only byte readiness, read/write, lifecycle, control level, and
flags. TCP client/server configuration remains outside the Z80 agent.
The current host network contract is TCP over IPv4 only; IPv6 is not supported.

### Standard 8251 MSX RS-232

`/DRIVER:8251` uses:

- 8251 data/status ports `80h/81h`;
- standard 8253/8254 timer ports `84h`, `85h`, and `87h`;
- explicitly programmed 19200 baud, asynchronous 8N1 x16;
- `COMMSK=FFh` while active, masking every asynchronous UART interrupt source.

The driver saves `COMMSK`, masks every UART IRQ before enabling the receiver,
and restores the previous value on reconfiguration or uninstall. Receive state
is polled only from `H.TIMI`; latched parity, overrun, and framing errors are
cleared while polling. A plain 8251 has no receive FIFO or automatic flow
control. The configured 19200 baud is the 8251A asynchronous maximum in x16
mode; reliable resident operation at that rate uses the negotiated frame-wake
ACK. Faster rates require the separate 16C550 transport and its FIFO/flow-control
safeguards. The prior UART mode and timer setup cannot be reconstructed from
the readable state saved here, so uninstall restores `COMMSK` but leaves the
interface configured for 19,200-baud 8N1.

### Generic 16C550-compatible UART

`/DRIVER:16C550` uses ports `80h-87h`, assumes a 1.8432 MHz UART reference
clock, and configures:

- divisor 1 for 115200 baud, 8N1;
- the 16-byte FIFO with an 8-byte receive trigger;
- `IER=0` while active, with receive state polled from `H.TIMI`;
- DTR, RTS, OUT1, and OUT2;
- automatic RTS/CTS through MCR AFE.

AFE and a correctly wired and respected RTS/CTS path are required at this
speed. The driver saves readable LCR, IER, MCR, DLL, and DLM state and restores
them on reconfiguration or uninstall. FIFO state is write-only, so restore
leaves it disabled. Framed CRC and bounded timeouts are the current safeguards
for corrupt or missing bytes, but the driver does not yet expose LSR overrun,
parity, framing, or break telemetry. The hook's serial deadline is an
instruction-loop budget, not a CPU-independent wall-clock timer; sustained-rate
claims therefore require tests on the target CPU mode and adapter.

The configured 115200 baud describes the UART line, not guaranteed application
throughput. The
[published BaDCaT specification](https://sites.google.com/view/badcatelectronics/msx/badcat-wifi-modem)
lists 57,600 bps effective throughput. End-to-end MCP throughput, flow-control
behavior, and screenshot times remain subject to physical measurement.

The 8251 and 16C550 implementations are transport-neutral UART byte drivers.
Neither contains TCP commands, adapter setup, or assumptions about which
transparent TCP/IPv4 bridge carries the ordered byte stream. TCP listen/connect
roles and endpoint configuration belong to the host and adapter layers.

A cartridge ROM contains firmware but does not emulate the 16C550 register
file, FIFO and timing, RTS/CTS behavior, or TCP bridge. An emulator needs a
compatible UART device model and transparent byte-stream peer in addition to
any ROM image.

`/DRIVER:16C550` selects the generic register and flow-control contract above,
not a product. BaDCaT SMD is one intended physical validation target, not a
separate build, protocol dependency, or required TCP transport. Another
transparent IPv4 bridge can use the same driver when it presents the compatible
UART interface and preserves RTS/CTS. Physical BaDCaT validation remains
pending arrival of that hardware.

### Adding another transport

Do not create another COM wrapper. Extend the universal binary:

1. Add a namespaced include under `agent/transports/`.
2. Define a stable transport ID, control level, flags, and private state size.
3. Implement namespaced `init`, `restore`, `rx_ready`, `tx_ready`,
   `read`, and `write` routines.
4. Add its six-entry vector table and binding branch to the core.
5. Add its command-line option and banner.
6. Increase `TRANSPORT_STATE_SIZE` in both universal build entry points if
   the new private state is larger.
7. Add deterministic source/build tests and a serialized integration path.

Byte routines may change `AF` but must preserve `BC`, `DE`, `HL`, `IX`,
and `IY`; timeout loops and protocol dispatch keep live state in those
registers. Initialization must leave a usable byte interface before returning.
A driver requiring discovery or recoverable initialization errors needs an ABI
extension rather than an implicit product-specific branch.

Changing the reported control level does not add NMI or bus-master behavior.
Those capabilities require matching hardware and supervisor code.

## Validation

Run deterministic validation:

~~~~sh
make test
~~~~

Run the serialized openMSX/RS232-Net/TCP end-to-end suite:

~~~~sh
make test-integration
~~~~

The integration suite owns at most one openMSX process at a time. It validates:

- the single universal COM and runtime 8251 selection;
- MemMan installation returning to DOS;
- intervention in a DOS-launched program;
- bounded snapshot pause, patch, resume, and repeated `H.TIMI` entry;
- an agent-VRAM PNG capture;
- foreground call/run/stop and visible `DEBUG ON`;
- reconfiguration without a duplicate TSR;
- safe and idempotent uninstall.

Integration processes are headless and muted only at the openMSX host mixer.
Emulated sound hardware, timing, I/O ports, and MSX sound routines remain
unchanged.
