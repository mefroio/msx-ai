# MSX-AI

<p align="center">
  <img src="assets/msx-ai-robot.png" alt="MSX-AI retro robot mascot" width="320">
</p>

Use MSX-AI to control MSX systems through the Model Context Protocol (MCP).
Choose direct openMSX automation, an agent running inside a physical MSX, or the
same agent path simulated in openMSX.

## Choose a backend

| Mode | Select with | Requirements |
|---|---|---|
| Direct openMSX | `msx_boot` or `msx_attach` | openMSX; no TCP or MSX agent |
| Physical MSX agent | `msx_agent_listen` or `msx_agent_connect` | MSX agent and a compatible host transport |
| Simulated MSX agent | `msx_tcp_bench_start` | openMSX, MSX agent, and local TCP/IPv4 |

Backend selection is explicit. No emulator starts automatically. Keep one
active target per MCP server session and switch modes when needed.

openMSX, TCP/IP, and BaDCaT are optional at the project level. The current agent
host adapter uses TCP/IPv4, but the protocol and MCP tools do not contain
BaDCaT-specific commands or depend on one network-adapter product.

## Features

- Direct headless or visible-window control of openMSX.
- Resident MSX-DOS agent with an optional foreground development monitor.
- RAM, VRAM, hardware I/O, slot, and mapper operations where the active mode
  can perform them safely.
- Text input and special keys, including `STOP`, `CTRL+STOP`, and `CTRL+C`.
- BASIC source entry, batched line input, file-based BASIC loading, and RUN.
- Z80 assembly, binary application loading, and controlled memory injection.
- Backend-neutral Z80 CPU snapshots: exact PC/SP, code, and stack state through
  the openMSX debugger, or a versioned BIOS `H.TIMI` callback context through
  the physical agent.
- Host-rendered SCREEN 0-8 and SCREEN 10-12 captures from VRAM.
- Resumable binary PUT and GET with 32-bit sizes, end-to-end CRC-32, durable
  restart checkpoints, and collision-safe publication.
- The `fast-v1` PUT/GET data path groups near-2 KiB wire frames into transient
  16 KiB disk-I/O windows, with no extra STATUS round trip per PUT frame and
  sparse durable GET checkpoints.
- On-MSX transfer percentage, progress bar, and host-measured final bytes per
  second, also returned to the MCP client as structured stream metrics.
- Lightweight PackBits transport compression when it reduces the wire size;
  already-compressed files remain byte-exact.

## Agent architecture

```text
MCP client
    |
MSX-AI server
    |
agent protocol
    |
selected host transport
    |
8251 or 16C550-compatible MSX interface
    |
resident MSX-DOS agent
```

Install the seven-file agent suite to use the physical or simulated agent path.
The default MemMan resident returns to MSX-DOS and remains available while
compatible software runs. Use the foreground monitor for direct call, run,
stop, slot, mapper, and visible DEBUG workflows.

The recommended MSX-DOS layout keeps the suite out of the disk root:

```text
A:\MSXAI\
  MSXAI.COM    MSXAIXF.COM  MCP8251.TSR  MCP16550.TSR
  MEMMAN.COM   TL.COM       TK.COM
```

Configure it once from `AUTOEXEC.BAT`:

```bat
SET MSXAI_HOME=A:\MSXAI
PATH A:\MSXAI;%PATH%
```

`MSXAI` and `MSXAIXF` can then be called from any DOS directory. Keeping all
seven files in the current directory remains a compatibility fallback when
`MSXAI_HOME` is not set.

## Requirements

- Python 3.10 or newer for the MCP server.
- Optional openMSX is required only for direct emulator control or agent-path simulation.
- Optional `z80asm` is required only to build the agent or assemble Z80 source.
- MSX-DOS 2 or Nextor
- A supported 8251 or 16C550-compatible interface and host transport for a
  physical agent connection.

No third-party Python packages are required. ROM and bootable disk images are
not distributed by this repository.

## Safety and limitations

- The resident agent is cooperative and depends on the normal BIOS timer hook.
  Software that keeps interrupts disabled or replaces that path can make the
  agent temporarily unreachable.
- A physical-agent CPU snapshot is the register frame visible at the BIOS
  `H.TIMI` callback boundary. It is not an NMI or bus-master freeze and cannot
  claim the interrupted application's arbitrary PC/SP. Direct openMSX snapshots
  do provide exact instruction-boundary state.
- Resident page and mapper restrictions protect the interrupted DOS or
  application context. Use the foreground monitor when persistent ownership is
  required.
- Slow serial links can make large VRAM captures expensive. Complex game
  screenshots remain experimental.
- Failed file transfers retain verified recovery state and never publish an
  incomplete target as a successful file.
- File transfer requires a matched current resident and `MSXAIXF.COM`. Existing
  version-3 `fast-v1` journals migrate, but sessions and journals from the
  retired legacy transfer path cannot resume with the current suite.

Physical BaDCaT SMD validation and performance measurements remain pending.
The generic 16C550 driver is not a BaDCaT-specific build.

## Validation

The automated suite currently contains more than 340 tests covering protocol
framing, memory safety, backend selection, keyboard input, screenshots,
CPU snapshots, application loading, file-transfer recovery, and openMSX
integration.

## Documentation

- [Technical reference](TECHNICAL.md): architecture, setup, protocols, hardware,
  safety rules, development, and testing.
- [MSX agent reference](agent/README.md): agent lifecycle, command line, memory
  model, drivers, and MSX-side implementation.

## License

MSX-AI is released under the [MIT License](LICENSE).
