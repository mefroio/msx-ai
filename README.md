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
- Host-rendered SCREEN 0-8 and SCREEN 10-12 captures from VRAM.
- Resumable binary PUT and GET with 32-bit sizes, CRC-32, explicit block
  acknowledgement, and collision-safe publication.
- On-MSX transfer percentage, progress bar, and confirmed bytes per second.
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
- Resident page and mapper restrictions protect the interrupted DOS or
  application context. Use the foreground monitor when persistent ownership is
  required.
- Slow serial links can make large VRAM captures expensive. Complex game
  screenshots remain experimental.
- Failed file transfers retain verified recovery state and never publish an
  incomplete target as a successful file.

Physical BaDCaT SMD validation and performance measurements remain pending.
The generic 16C550 driver is not a BaDCaT-specific build.

## Validation

The automated suite currently contains more than 330 tests covering protocol
framing, memory safety, backend selection, keyboard input, screenshots,
application loading, file-transfer recovery, and openMSX integration.

## Documentation

- [Technical reference](TECHNICAL.md): architecture, setup, protocols, hardware,
  safety rules, development, and testing.
- [MSX agent reference](agent/README.md): agent lifecycle, command line, memory
  model, drivers, and MSX-side implementation.

## License

MSX-AI is released under the [MIT License](LICENSE).
