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
MSXAI /?
MSXAI /HELP
~~~~

`/UNINSTALL` must be used alone. `DEBUG ON` is accepted only with
`/MONITOR`; it is rejected for the default resident lifecycle.

The startup banner reports the selected driver and runtime mode before control
passes to MemMan or the foreground monitor.

## Runtime modes

| Behavior | MemMan resident (default) | Foreground `/MONITOR` |
|---|---:|---:|
| Returns to MSX-DOS | Yes | No |
| Monitors DOS-launched software | Yes | No |
| RAM/VRAM and direct I/O | Yes, with resident memory restrictions | Yes, outside protected monitor memory |
| Pause/resume | Yes | Yes |
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
`TK.COM` directly with that ID. MemMan detaches both hooks before the
agent's kill entry restores UART state. No uninstall helper is written to disk.
Repeating the command is safe and reports that the agent is not installed.

MemMan versions older than 2.4 are rejected. A loader failure removes only
temporary files proven to have been created by the current invocation and
reports incomplete cleanup rather than overwriting or deleting an existing
file.

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
opcode as `[XX]`, where `XX` is the uppercase hexadecimal byte.

Tracing is deliberately limited:

- it is accepted only with `/MONITOR`;
- it uses the foreground BDOS proxy for console output;
- commands serviced from an interrupt hook remain silent, even in debug mode,
  to avoid re-entrant BIOS output and corruption of the running application.

Debug output changes the MSX display and therefore appears in screenshots.
Resident mode never draws protocol activity on the target screen.

## Execution and hooks

The agent installs handlers for `H.KEYI` and `H.TIMI`. The selected UART
owns its receive interrupt path; the timer hook provides another cooperative
service opportunity. The hook saves normal and alternate Z80 register sets and
moves protocol dispatch to a dedicated agent stack before processing a frame.

The resident reports state `running` while DOS or a DOS-launched application
is active. A pause preserves the interrupted CPU context until an explicit
resume. Temporary transport silence does not resume it implicitly. A truncated
frame unwinds safely instead of leaving the MSX frozen inside the parser.

Direct `call`, `run`, and `stop` are not advertised by the resident TSR:

- DOS software must be launched normally after installation;
- `stop` cannot safely discard the interrupted DOS/application context;
- use `/MONITOR` when the host must launch or abandon injected code.

The control path remains cooperative. Code that holds `DI`, removes the BIOS
hook chain, or prevents maskable interrupts cannot be interrupted by this
software-only agent.

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
q                         state/status
r/p                       RAM read/write
v/w                       VRAM read/write
c/j                       call/run (foreground monitor only)
s/g                       pause/resume
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
larger negotiated chunks, explicit status codes, CRC, and retry safety.

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

If a TCP/serial peer disappears, eight consecutive ESC bytes reset only the
framed session. A new host can repeat the raw hello and v3 upgrade without
resetting the MSX. One accidental ESC byte is not enough to downgrade the
session.

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

### Standard 8251 MSX RS-232

`/DRIVER:8251` uses:

- 8251 data/status ports `80h/81h`;
- standard 8253/8254 timer ports `84h`, `85h`, and `87h`;
- explicitly programmed 4800 baud, asynchronous 8N1 x16;
- the standard `COMMSK` receive-ready interrupt path.

The driver saves `COMMSK`, masks unrelated UART interrupt sources, and
restores the previous value on reconfiguration or uninstall. A plain 8251 has
no receive FIFO or automatic flow control; 4800 baud is the reliability-first
configuration validated by the openMSX RS232-Net integration suite.

### Generic 16C550-compatible UART

`/DRIVER:16C550` uses ports `80h-87h` and configures:

- 115200 baud, 8N1;
- the 16-byte FIFO with an 8-byte receive trigger;
- received-data interrupts;
- DTR, RTS, OUT1, and OUT2;
- automatic RTS/CTS through MCR AFE.

AFE and a correctly wired and respected RTS/CTS path are required at this
speed. The driver saves readable LCR, IER, MCR, DLL, and DLM state and restores
them on reconfiguration or uninstall. FIFO state is write-only, so restore
leaves it disabled.

BaDCaT SMD is an intended example of a compatible 16C550 device. It is not a
separate build and the code contains no BaDCaT-specific command or networking
assumption. Physical validation remains pending arrival of that hardware.

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
- persistent pause, patch, resume, and repeated hook entry;
- an agent-VRAM PNG capture;
- foreground call/run/stop and visible `DEBUG ON`;
- reconfiguration without a duplicate TSR;
- safe and idempotent uninstall.

Integration processes are headless and muted only at the openMSX host mixer.
Emulated sound hardware, timing, I/O ports, and MSX sound routines remain
unchanged.
