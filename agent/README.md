# MSX-AI physical-target agent

The MSX-DOS side of the physical-target backend is a compact seven-file suite:

- `MSXAI.COM` is the command-line lifecycle front end and foreground monitor;
- `MSXAIXF.COM` is the transient protocol-X PUT/GET worker, 16 KiB fast-I/O
  accumulator, and bounded PackBits decoder;
- `MCP8251.TSR` and `MCP16550.TSR` are fixed-driver resident images selected
  by `/DRIVER` on first installation; and
- `MEMMAN.COM`, `TL.COM`, and `TK.COM` provide the external MemMan lifecycle.

Runtime command-line options select the driver and operating mode. Splitting
the suite keeps unrelated transient utilities out of the main executable and
does not make their file sizes cumulative in MSX RAM.

In the source tree, `msx_xfer.asm` owns the transient transfer executable and
compiles `msx_xfer_engine.inc`, which contains its PUT/GET, resume, progress,
CRC, and PackBits routines. `msx_memman_loader.asm` contains only the resident
installation and removal lifecycle; it has no file-transfer engine code.

The external contract is a full-duplex byte stream. TCP roles, MCP tools,
application parsing, and screenshot rendering live on the host and are not
embedded in the Z80 code.

## Build

From the repository root:

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

Internal build products include:

- `work/agent/build/MSXAI.TSR`: the relocatable template used to generate the
  two fixed-driver TSRs; and
- `work/agent/build/MSXAI_TSR.INC`: generated validation and relocation
  metadata consumed by the loader build.

These two internal files are not alternative agents and should not be copied to
a release disk. `MEMMAN.COM`, `TL.COM`, and `TK.COM`, by contrast, are required
deployable dependencies. Their pinned Base64 sources, SHA-256 values, and
redistribution notice are under `third_party/memman/`.

Keep all seven deployable files together. The recommended installation is a
short dedicated directory such as `A:\MSXAI`, configured from `AUTOEXEC.BAT`:

~~~~bat
SET MSXAI_HOME=A:\MSXAI
PATH A:\MSXAI;%PATH%
~~~~

`PATH` makes `MSXAI` and `MSXAIXF` callable from any DOS directory.
`MSXAI_HOME` lets the lifecycle resolve `MEMMAN.COM`, `TL.COM`, `TK.COM`, and
the selected fixed-driver TSR without depending on the caller's current
directory. A missing or empty `MSXAI_HOME` deliberately falls back to resolving
all seven files in the current directory for compatibility with portable test
disks. Partial packages or files mixed from different builds are unsupported.
Keep the configured path short enough for the MSX-DOS/MemMan 40-byte command
tail; `A:\MSXAI` is the canonical tested value.

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
MSXAI /DRIVER:8251 /MONITOR DEBUG
MSXAI /DRIVER:16C550 /MONITOR DEBUG
MSXAI /UNINSTALL
MSXAIXF /PUT 00112233445566778899AABBCCDDEEFF
MSXAIXF /GET 00112233445566778899AABBCCDDEEFF
MSXAI /?
MSXAI /HELP
~~~~

`/UNINSTALL` must be used alone. `DEBUG` is accepted only with
`/MONITOR`; it is rejected for the default resident lifecycle. The
`MSXAIXF /PUT` and `/GET` forms take the 32-hex-digit transfer ID staged by the
host. `MSXAI.COM` has no file-transfer command; all DOS-file PUT and GET work
is owned by `MSXAIXF.COM` and protocol X.

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
| Transient DOS PUT/GET | Yes, by launching `MSXAIXF.COM` from DOS | Not applicable |
| Direct call/run/stop | No | Yes |
| Slot/mapper selection | No | Yes, pages 0 and 1 |
| `DEBUG` | Rejected | Optional |

### Default MemMan resident

The default command installs a genuine named MemMan TSR and returns to the DOS
prompt. It requires MSX-DOS 2 or Nextor and the MemMan 2.4+ API.

The first installation follows this lifecycle:

1. Parse options and print the agent banner.
2. Discover a compatible existing MemMan through `EXTBIO`.
3. Validate external `MEMMAN.COM`, `TL.COM`, and the fixed-driver TSR selected
   by `/DRIVER` under `MSXAI_HOME`, or in the current directory when it is
   unset, before changing disk or resident state.
4. Read `MEMMAN.COM` into guarded free space at the top of the TPA, close its
   handle, and overlay it at `0100h` for the point-of-no-return handoff.
5. Let the MemMan command chain find external `TL.COM` through `PATH`, pass it
   the fully resolved `MCP8251` or `MCP16550` TSR path, then return to DOS with
   only that selected TSR resident.

No executable or TSR is emitted, patched, renamed, deleted, or left behind by
this lifecycle. Every component was already supplied as a final suite file.

The resident ID is the fixed 12-byte MemMan name `MSXAI MCP1  `. Re-running
`MSXAI` with a driver finds that ID and uses `TsrCall` to reconfigure the
existing resident instead of installing a duplicate. Reconfiguration restores
the previous UART state, binds the new driver, resets protocol sequencing, and
initializes the new UART.

Changing drivers can drop the active host connection. Reconnect through the
newly selected interface.

`MSXAI /UNINSTALL` discovers the named TSR, validates external `TK.COM`, stages
it in free high TPA, and overlays it directly with that ID. MemMan detaches the
registered `H.KEYI` guard and `H.TIMI` service hook before the agent's kill
entry restores UART state. No uninstall helper is written or deleted.
Repeating the command is safe and reports that the agent is not installed.

Replacing suite files does not patch a TSR already loaded in MSX memory. After
copying a rebuilt suite, run `MSXAI /UNINSTALL` and install it again with the
selected `/DRIVER`. This uninstall/reinstall step is required to activate a
resident change.

MemMan versions older than 2.4 are rejected. Before the external overlay
handoff, a loader failure closes any open suite file and returns an error
without creating, overwriting, or deleting a disk file.

### Resumable DOS file transfer

Framed opcode `X` stages an immutable, transfer-ID-bound descriptor containing
direction, encoding, DOS path, 32-bit wire/final sizes, CRC-32 values, and an
optional resume boundary. The host then types only
`MSXAIXF /PUT <32-hex-id>` or `MSXAIXF /GET <32-hex-id>`. The transient process
claims the descriptor through MemMan `TsrCall`; a different ID cannot claim or
replace an active transfer.

All DOS work remains in that foreground process. The resident hook performs
UART framing and bounded mailbox copies but never calls BDOS. This separation
is independent of the selected 8251 or 16C550 byte-stream transport and keeps
interrupted DOS, games, and BIOS hooks out of the filesystem path.

File PUT and GET use `fast-v1` as their sole active data path. Negotiation is
mandatory: the host requires `FAST_CAPABILITIES`, marks OPEN with flag bit
`0x04`, launches `MSXAIXF.COM`, and sends `FAST_BEGIN` only after the helper
reports READY. The resident agent and transient helper must come from the same
current suite. Any missing capability, rejected begin, or mismatched suite
causes the transfer to fail closed before file data is transferred; there is no
slower data-plane fallback.

The helper uses `TSR_TALK_XFER_PUMP` to service one complete opcode-X frame
between DOS calls, outside `H.TIMI`, with up to 2,026 PUT data bytes or 2,040
GET data bytes. Hooks leave the UART untouched while the pump is armed, so the
frame-wake ACK holds the host at its leading byte during disk work. This is
physical receive credit, not another transfer-level ACK. The pump has its own
bounded I/O timeout and saved stack. Reconnect clears pump ownership while
preserving the validated descriptor and durable resume state for an explicit
re-arm.

Existing version-3 host journals already marked `fast-v1` are migrated and can
resume after validation. Journals created for the retired legacy transfer data
plane, and legacy transfers that were active during an upgrade, cannot resume
with the current build. They are rejected rather than reinterpreted through an
incompatible framing and credit model. During automatic discovery, a retired
journal is ignored only when its fully validated binding proves that it belongs
to another transfer; a possibly matching retired journal remains a hard error.

The larger parser window is private to an armed opcode-X pump. Bootstrap and
HELLO still advertise 320 bytes, ordinary commands remain capped at that
hook-safe limit, and the host raises its v3 ceiling only around one fast bulk
frame before restoring it. Transfer negotiation requires both `PUMP` (`0x01`)
and `STREAM` (`0x02`) capability bits. It reuses the existing MCP frame CRC-16
and sequence de-duplication but adds no per-block checksum; the whole-file
CRC-32 remains the end-to-end integrity check.

The host BASIC file mode uses this same path: it materializes the ASCII or
tokenized `.BAS` bytes temporarily, stages a raw protocol-X PUT, launches
`MSXAIXF.COM`, and requires the final CRC-32 and publication checks before
entering BASIC. There is no separate BASIC upload opcode or compatibility
file-transfer command in `MSXAI.COM`.

The caller explicitly confirms the initial DOS prompt; the physical-target
host does not read VRAM to infer it. On every successful PUT/GET path,
`MSXAIXF.COM` completes file cleanup and prints its final status before sending
`TSR_TALK_XFER_FINISH`. Terminal `COMPLETE` therefore leaves only the immediate
DOS termination instruction in the transient process. Host PUT/GET and BASIC
file workflows use that protocol witness instead of hidden pre/post screen
captures. A 32-hex transfer ID naturally wraps on a 40-column DOS screen.

PUT keeps separate accepted and durable boundaries. The foreground pump accepts
up to 2,026 data bytes per physical frame and copies consecutive frames into a
16 KiB accumulator in transient CPU page 1. At its high-water threshold
(normally about 14--16 KiB) or at end of file, `MSXAIXF.COM` updates the rolling
CRC-32 over that contiguous window, performs one exact DOS `WRITE`, runs one
`ENSURE`, and publishes one cumulative durable commit. The ordinary PUT reply
is the only application credit; the host sends no separate STATUS request per
frame. The resident advertises the available window as PUT credit and rejects
requests that would make accepted-minus-durable exceed 16 KiB, so a stalled or
nonconforming host cannot overflow the helper's accumulator.

A same-directory partial named `xxxxxxxx.PRT` is created from 32 ID bits with
`CREATE_NEW`. Its `xxxxxxxx.MTD` sidecar contains an immutable full 128-bit
binding plus a small complemented transaction phase. The phase is itself
`ENSURE`d before ownership of a PackBits `.OUT` or the final rename boundary is
claimed. Publication first refuses an existing target, then uses DOS2 `RENAME`
in the same directory. It never truncates or replaces a user file.

GET opens the requested source read-only and discovers its 32-bit length and
whole-file CRC-32 in the foreground. It performs one DOS `READ` of up to 16 KiB
into the transient accumulator, then slices that window into framed responses
containing at most 2,040 data bytes. Each frame is released only after its
complete response has left the agent, and the next slice streams without an
application ACK. The host flushes and fsyncs its partial and sends `GET_ACK` at
64 KiB checkpoints and EOF. After reconnect, `FAST_BEGIN` rewinds the still-open
DOS handle to that durable checkpoint, so an unacknowledged tail is replayed
rather than skipped.

Both directions support zero-byte files and lengths above 64 KiB; actual media,
filesystem, and MSX-DOS limits still apply. CLOSE is the explicit end-of-stream
signal and is replay-safe while verification is running and after completion.
A one-minute NTSC/72-second PAL no-progress deadline closes foreground handles
without deleting a valid PUT partial. On resume, the sidecar binding and phase
are revalidated, the actual partial length and CRC are scanned, and the host
must confirm the returned prefix before sending more bytes. This recovers an
`ENSURE`d accumulator window whose cumulative commit reply was lost. The
implementation does not write a per-block disk journal: actual partial bytes,
CRC reconciliation, and the few publication phases are the recovery authority.

The foreground worker renders `[##################] 100% 11520 B/s` as one
fixed-width, 35-column, carriage-returned status line. This fits Brazilian
machines whose DOS console exposes 37 columns without touching the auto-wrap
column. PUT updates after accumulator windows are written and committed; GET
updates as 16 KiB read windows are emitted. Percentage uses the complete 32-bit
wire position and size, including a resumed starting offset. Intermediate rates
use intervals of at least one BIOS second and follow the saved VDP PAL/NTSC
setting. At CLOSE, the host appends its monotonic whole-stream B/s measurement,
which the helper renders as the final 100% rate before its OK line. The host
result returns the same interval as `stream_bytes`, `stream_seconds`, and
`stream_rate_bps`. The display code is compiled only into `MSXAIXF.COM`; it adds
no resident TSR memory and disappears when DOS terminates the helper.

When an MCP client cancels a host PUT/GET call, the host keeps its target lock,
signals the synchronous transfer worker, and sends protocol-X `CANCEL` at the
next safe frame boundary. The helper closes foreground handles without
publishing incomplete output; valid sidecar, partial, and host-journal state is
kept for a later bound resume. A lost CANCEL response therefore does not turn a
cancelled operation into success or authorize an unrelated transfer.

Protocol version 1 advertises RAW plus PUT-side `PACKBITS_DECODE`; GET remains
RAW. The host uses deterministic standard PackBits only after capability
negotiation and only when the requested compression policy selects it. A
`.ZIP`, `.GZ`, or other recognized already-compressed input remains byte-exact
in automatic mode. Uncompressed ROM and disk-image data may use transparent
PackBits when it clears the normal savings threshold; the published file is
still restored byte-for-byte.

Before protocol-X planning, the shared host backend recognizes an
unambiguously textual `.BAS` destination and streams it into the canonical
MSX-DOS representation: numbered 8-bit source lines, CRLF endings, and one
final `0x1A` marker. Compression, sizes, CRC-32, and resume binding are all
calculated from that canonical image. Tokenized BASIC beginning with `0xFF`
and non-BASIC files remain byte-exact. This policy belongs to the shared real
MSX backend, not to any MCP-specific interface.

For a PackBits PUT, the verified `.PRT` is never published directly.
`MSXAIXF.COM` reserves a distinct same-directory `.OUT` with `CREATE_NEW`, then
decodes the complete wire stream incrementally with its fixed 2,046-byte
transient buffer.
It rejects reserved control `80h`, the non-canonical two-byte run `FFh`,
truncated packets, trailing input, and output beyond the declared final size.
It independently verifies exact 32-bit final length and CRC-32 before renaming
`.OUT` to the requested basename. A reset during decoding discards only an
`.OUT` whose ownership phase was durably recorded, then restarts decoding from
the verified `.PRT`. A reset around `RENAME` validates whichever source or
target survived before completing the phase idempotently. Ordinary failure
preserves `.PRT` and `.MTD` for retry. After success, the sidecar is removed;
if its terminal reply was lost, a host journal at the complete wire boundary
may replay success only after the existing final target passes exact size and
CRC-32 validation. The host first fsyncs a monotonic close-intent bit at that
complete durable boundary. Only a resumed OPEN carrying this proof may enter
the no-sidecar validation path; an initial zero-byte journal therefore cannot
adopt an unrelated empty file. This requires MSX-DOS 2 and enough free disk
space for the wire and final files, but never loads the complete file into RAM.

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

Debug is disabled by default. `DEBUG` prints each foreground protocol
opcode as `[XX]`, where `XX` is the uppercase hexadecimal byte. A v3 TCP host
also sends its accepted IPv4 source endpoint once after HELLO when the agent
advertises the `debug-peer-label` feature; the screen prints
`MCP client: <ipv4>:<port>`.

For repeatable openMSX testing, the repository-level
`open-msx-mcp.command` launcher builds and stages the complete canonical suite
on a disposable runtime disk, then waits for an IPv4 MCP listener at
`127.0.0.1:6603`. It leaves the agent at the DOS prompt for the user to start
explicitly in resident mode or with `/MONITOR DEBUG`.

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

Feature `cpu-snapshot-v1` exposes that fixed hook-entry register frame through
framed opcode `D`. It is deliberately scoped to the state visible at the BIOS
`H.TIMI` callback boundary. BIOS and MemMan have already entered their private
interrupt/dispatcher paths, so the callback stack and return address are not
the interrupted application's portable SP/PC. The host reports application
PC/SP and interrupt state as unavailable, while retaining separately named
service metadata. An idle foreground monitor rejects the request; a resident
or a running/paused foreground payload can answer while servicing `H.TIMI`.

The resident reports state `running` while DOS or a DOS-launched application is
active. The safe host profile rejects persistent lowercase-`s`/`msx_agent_pause` and
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

The host-side `msx_agent_key` tool maps ESC, Return, Tab, Select, and Space to the
same atomic keyboard-ring operation. STOP and Ctrl+STOP are BIOS events, not
character bytes: the agent backend writes `04h` or `03h` respectively to the
documented `INTFLG` byte at `FC9Bh` through the framed RAM-write command.
Ctrl+C is exposed as a convenience alias for the Ctrl+STOP break event. This
interrupts MSX-BASIC and other cooperative BIOS software without claiming to
emulate a physical key matrix.

## Memory model

The seven deployable files are not simultaneously resident. DOS initially
loads `MSXAI.COM` as a normal transient program. For a first resident install,
the loader validates the external utilities and selected fixed-driver TSR,
stages `MEMMAN.COM` at the guarded high end of free TPA, and overlays it at
`0100h`; the old `MSXAI.COM` image is disposable after that handoff. MemMan then
runs `TL.COM`, which loads only the selected TSR into mapper-managed resident
memory. The unselected TSR, `TK.COM`, and `MSXAIXF.COM` remain disk files.

`MSXAIXF.COM` is loaded later as an ordinary foreground transient only for a
protocol-X PUT or GET. Its transient workspace provides PackBits decoding and
the fast data plane's 16 KiB accumulator; DOS reclaims the complete TPA when it
exits. Uninstall similarly stages and overlays external `TK.COM` for one
action. During a lifecycle handoff the front end and one staged external
overlay briefly share the TPA; the other suite components do not. The
requirement is therefore that active pair plus guarded stack and overlay
headroom, never the sum of all seven file sizes. Only MemMan and the selected
agent TSR remain allocated after installation.

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

The public negotiated payload limit is 320 bytes, and the resident request and
response area remains 320 bytes. File-transfer payloads above that limit are
accepted only for an armed `fast-v1` opcode-X pump, using `MSXAIXF.COM`'s
separate transient 2,048-byte page-zero frame workspace and adjacent 2,046-byte
mailbox. PUT and GET also use a 16 KiB accumulator at `4000h`--`7FFFh`; none of
these areas expands resident BSS. The helper refuses to start unless the DOS
TPA top and entry stack pointer both provide at least `8800h`, leaving 2 KiB of
guarded stack headroom above the accumulator. The build separately guarantees
that the COM image ends before `4000h`. A split lookup-table implementation
accelerates CRC processing.

The v3 HELLO appends an optional feature byte after the runtime-mode byte. Bit
0 advertises `keybuf-input`; bit 1 advertises the foreground-debug-only
`debug-peer-label`; bit 2 advertises the resident-only `snapshot-lease`; bit 3
advertises the transport-independent `frame-wake-ack`; and bit 4 advertises
resident-only `timi-poll-safe`; bit 5 advertises the resident-only
`keybuf-spool`; bit 6 (`0x40`) advertises `cpu-snapshot-v1`; and bit 7
(`0x80`) advertises resident-assisted resumable `file-transfer-v2`. The safe
features are valid only together with `frame-wake-ack`.
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

Opcode `D` accepts exactly one byte, context version `1`, and returns a cached
40-byte CPU-context record. Bytes 0..7 are version, kind, size, validity flags,
run state, hook kind, runtime mode, and transport ID. Bytes 8..27 contain
`HL'`, `DE'`, `BC'`, `AF'`, `IY`, `IX`, `HL`, `DE`, `BC`, and `AF` as
little-endian words copied from the fixed hook frame. Bytes 28..39 contain the
hook-entry service SP, internal callback return address, service-time `I/R`,
16-bit `JIFFY`, screen mode, transport control level, current-service `IFF2`
valid/value bits, and one zero reserved byte. Main, alternate, and index
registers are valid callback-entry values; service metadata must not be
relabeled as application PC/SP. Unknown versions are `UNSUPPORTED`, malformed
lengths are `BAD_ARG`, and a request outside an active `H.TIMI` callback is
`BAD_STATE`.

Opcode `X` carries file-transfer subprotocol version 1. Its subcommands are
`CAPS=0`, `OPEN=1`, `STATUS=2`, `PUT_DATA=3`, `GET_READ=4`, `GET_ACK=5`,
`CLOSE=6`, `CANCEL=7`, `FAST_CAPABILITIES=8`, and `FAST_BEGIN=9`.
`FAST_CAPABILITIES` is deliberately separate, preserving the base CAPS reply
byte-for-byte. Capability bit `0x01` identifies the foreground pump and bit
`0x02` identifies the sequential-stream revision. Both capabilities and a
successful `FAST_BEGIN` are required for every transfer. Every stateful request
after OPEN includes the 16-byte transfer ID. OPEN uses this fixed prefix before
its ASCII path:

~~~~text
sub:u8, version:u8, direction:u8, encoding:u8, flags:u8, id[16],
wire_size:u32, wire_crc32:u32, final_size:u32, final_crc32:u32,
resume_offset:u32, resume_prefix_crc32:u32, path_length:u16, path[]
~~~~

All integers are little-endian. OPEN flag bit 0 requests resume. Bit 1
authorizes receiptless terminal replay and is accepted only together with bit 0
for a PUT whose requested offset and prefix CRC exactly equal its complete wire
size and CRC. Bit 2 is required and selects the negotiated pump. PUT data is
`sub | id | offset:u32 | data`; its response separates
the accepted block length and accepted end from the
batched, `ENSURE`-backed durable end and next credit. GET_READ returns
`offset:u32 | length:u16 | state:u8 | error:u8 | data`; GET_ACK binds the
next durable offset to the rolling prefix CRC-32 and is sent at 64 KiB and EOF
checkpoints. CLOSE is `sub | id | rate_bps:u16`, with the rate measured by the
host over the data-stream interval. STATUS returns state, direction, encoding,
error, result flags, ID, all four integrity fields, durable and accepted
offsets, prefix CRC, and PUT credit. Result flag bits mean active, resumable,
wire verified, final verified, and published. Numeric state/error values and
the complete host parser are defined in `server/msx_transfer.py`.

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

The common resident core includes both current drivers. The build emits
`MCP8251.TSR` and `MCP16550.TSR` by fixing the initial transport-selection byte
in otherwise equivalent TSR images; `/DRIVER` chooses the matching file for a
first install. Re-running `MSXAI` against an existing resident uses its MemMan
`TsrCall` reconfiguration entry, so it does not load a duplicate TSR. In either
case initialization binds six `JP` operands once, with no driver-selection
branch in the per-byte hot path.

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

Do not create another command-front-end COM wrapper. Extend the common core and
canonical suite:

1. Add a namespaced include under `agent/transports/`.
2. Define a stable transport ID, control level, flags, and private state size.
3. Implement namespaced `init`, `restore`, `rx_ready`, `tx_ready`,
   `read`, and `write` routines.
4. Add its six-entry vector table and binding branch to the core.
5. Generate and validate a canonically named fixed-driver TSR for first
   installation, and add that file to suite packaging.
6. Add its command-line option and banner.
7. Increase `TRANSPORT_STATE_SIZE` in both common build entry points if the new
   private state is larger.
8. Add deterministic source/build tests and a serialized integration path.

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

- staging of the canonical seven-file suite and selection of `MCP8251.TSR`;
- MemMan installation returning to DOS;
- intervention in a DOS-launched program;
- bounded snapshot pause, patch, resume, and repeated `H.TIMI` entry;
- versioned CPU-context snapshots in resident and active foreground-monitor
  states, plus idle-monitor rejection;
- an agent-VRAM PNG capture;
- raw ZIP and PackBits protocol-X PUT/GET round trips through the resident TCP
  path in `test_resident_types_and_runs_basic_only_through_agent_tcp`;
- foreground call/run/stop and visible `DEBUG`;
- reconfiguration without a duplicate TSR;
- safe and idempotent uninstall.

Integration processes are headless and muted only at the openMSX host mixer.
Emulated sound hardware, timing, I/O ports, and MSX sound routines remain
unchanged.
