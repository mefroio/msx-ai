# Safety and operational limits

MSX-AI can modify live memory and hardware state. Read status first, prefer
read-only operations, and treat write, I/O, slot, mapper, execution, and reset
operations as state-changing actions.

The optional HTTP MCP transport has no authentication in this release and is
therefore restricted to `127.0.0.1`. The physical agent TCP stream is also an
unauthenticated binary control channel with no encryption, TLS, confidentiality,
authenticity, or replay protection. Frame CRC detects accidental corruption,
not a malicious peer. The channel can expose RAM, VRAM, I/O, and code execution;
use an isolated trusted LAN or a VPN/secure tunnel and never publish it directly
to the internet. `msx_agent_listen` defaults to
`127.0.0.1`; bind the host machine's specific LAN IPv4 address only when a
physical adapter must connect, and protect that interface with the host
firewall.

`MSX_AI_USER_ROOT` selects the base for relative host paths; it is not a
containment boundary. Absolute paths and relative paths containing `..` remain
available to explicit application and PUT/GET calls with the permissions of
the MCP server process. Use a dedicated account or restricted working
directory when the MCP client itself is not fully trusted.

Atomic no-overwrite GET publication requires hard-link support on the
destination filesystem. First-run publication of packaged openMSX templates
also requires it in `MSX_AI_STATE_DIR`. FAT/exFAT volumes are unsuitable for
those paths. A failed GET publication leaves its verified `.msxpart` file and
journal intact for resume; it is never reported as success.

## Cooperative resident control

The resident agent runs from the BIOS `H.TIMI` hook. Software that holds
interrupts disabled, replaces the interrupt path, removes the hook from its
chain, or pages required state away can make the agent unreachable. A
transparent UART bridge does not provide NMI, bus-master, or unconditional
control.

The host uses bounded waits and quarantines an attachment after an indeterminate
transport failure. It does not retry a possibly delivered write into an unknown
machine state. A bounded snapshot lease automatically resumes the target after
transport silence.

`msx_agent_reboot` is explicitly destructive. Finish file transfers and disk
writes first: a warm reset can interrupt DOS I/O and damage unfinished work.
The current resident commits the reboot after sending and transport-flushing
its terminal opcode-`R` response, then makes a bounded best-effort UART drain
attempt before it maps Main-ROM and enters `0000h`. Drain timeout cannot cancel
the reboot; if final bytes are truncated, delivery is indeterminate and the
host does not retry. A successful result confirms only submission, never
completion of the following boot, and the server detaches the agent channel.
This is not a power cycle and does not guarantee power-on peripheral state.
After MSX-DOS returns, reinstall or otherwise restore the resident and
reconnect.

An agent CPU snapshot describes the register frame visible at the timer-hook
callback boundary. It must not be presented as the interrupted application's
arbitrary PC or SP. Direct openMSX snapshots use an exact debugger instruction
boundary and have different semantics.

## Memory and hardware writes

- Resident RAM page 1 is rejected because the resident occupies it during
  service.
- Page 3 contains live BIOS, DOS, stack, hook, and system state. An arbitrary
  write can immediately corrupt or crash the target.
- Direct I/O is an expert escape hatch. A read can consume FIFO data or clear
  a device latch or status flag; writing the active UART, VDP, mapper, or an
  unrelated device can disconnect the protocol or damage live state.
- Slot and mapper changes are foreground-monitor operations because a resident
  hook cannot safely make those mappings persistent.

Use verification only where reads are meaningful. Some hardware registers are
write-only or have read side effects.

## Application loading

Automatic `msx_agent_app_load` behavior is deliberately narrow. Only a valid
seven-byte-FE-header BLOAD on a resident agent selects the MSX-BASIC environment,
and only from a recognized DOS or BASIC prompt. The declared RAM payload is
always read back completely before a nonzero entry is submitted through
`DEFUSR`/`USR`; the caller cannot disable this verification.

Verification proves the injected bytes, not compatibility of arbitrary machine
code. The loader never relocates a BLOAD image because the FE header provides no
relocation records. It accepts only a complete segment in CPU pages 2/3
(`0x8000-0xFFFF`) and rejects entries outside that segment. Page 0 belongs to
Main-ROM in BASIC, and page 1 is unavailable for this resident/BASIC path.
`environment="direct"` and `msx_agent_asm_load` remain explicit direct-injection
paths and retain their foreground-monitor safety limits.

## Input and display limits

Resident input feeds the BIOS keyboard ring. Software that scans the physical
keyboard matrix directly will not observe it. STOP and Ctrl+STOP use BIOS work
area state rather than raw key-matrix emulation.

Screenshots reconstruct standard SCREEN 0 through 8 and 10 through 12 from
VRAM and readable state. SCREEN 9, raster effects, borders, analog artifacts,
and exact interlaced-field timing are not reconstructed. Software that changes
write-only palette or VDP state without maintaining BIOS shadows may require an
explicit palette and can still produce an imperfect image.

Full VRAM capture is slow over the 19,200-baud 8251 path. The server refuses an
expensive capture unless the caller explicitly allows it.

## Before changing a target

1. Call `msx_local_status` or `msx_agent_status` for the intended channel and
   verify identity, runtime mode, transport, and features. In a paired bench,
   use `msx_tcp_bench_status` to verify the shared `bench_id`.
2. Confirm that the requested range excludes protected or unrelated state.
3. Prefer an atomic operation for a running resident target.
4. Keep a recoverable copy of files and application data.
5. Stop after a timeout or quarantine error; reconnect and inspect status
   instead of repeating a mutation blindly.
