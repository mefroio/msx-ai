# MSX-AI

<p align="center">
  <img src="assets/msx-ai-robot.png" alt="MSX-AI retro robot mascot" width="320">
</p>

MSX-AI is an open project that allows an AI assistant to interact with an MSX
computer through the Model Context Protocol (MCP).

It can be used in three ways:

- control openMSX directly, without TCP/IP or software running inside the MSX;
- control a physical MSX through a small agent running on the machine; or
- use both approaches, developing and testing in the emulator before moving to
  physical hardware.

openMSX, TCP/IP, and BaDCaT are optional parts of the project. MSX-AI is not
locked to one emulator, computer model, network adapter, or AI interface.

## What it can do

The current MSX agent can remain resident in memory while the user returns to
MSX-DOS and runs compatible software.

Through MCP, an AI assistant can:

- inspect the MSX and connection state;
- read and modify RAM and VRAM;
- type text and send special keys such as `STOP`, `CTRL+STOP`, and `CTRL+C`;
- enter BASIC, write programs, and run them;
- create drawings, animations, and small BASIC games;
- assemble, transfer, and execute machine-language routines;
- access hardware I/O ports;
- load programs and data into memory;
- upload and download binary files;
- verify file transfers with CRC-32;
- resume interrupted transfers without publishing corrupt files;
- display transfer progress, percentage, and bytes per second on the MSX; and
- capture and render screens from VRAM.

File transfers support sizes above 64 KiB, preserve files that are already
compressed, and can use lightweight transport compression when it actually
reduces the amount of data sent.

## Safe transfer recovery

We tested a real connection failure during an upload. The link was disconnected
while the MSX displayed 8%. The agent detected the failure, stopped the
operation safely, and returned to MSX-DOS. After reconnecting, the same transfer
continued and completed with the expected size and CRC-32.

An incomplete file is never silently published as a successful transfer.

## Development with openMSX

Our current development environment can simulate the complete agent path:

```text
AI assistant -> MCP -> TCP/IP -> emulated serial port -> agent inside the MSX
```

In this mode, the AI communicates with the same MSX-side agent intended for
physical hardware. It does not need to use openMSX debugger memory APIs. This
allows the protocol, file transfers, recovery behavior, and agent features to
be tested before physical hardware is available.

MSX-AI can also control openMSX directly. Direct emulator control does not need
TCP/IP or the MSX-side agent.

## Using a physical MSX

The current physical-agent path supports standard MSX 8251 serial hardware and
generic 16C550-compatible UART hardware.

BaDCaT SMD is one device planned for physical testing, but it is not a project
dependency. Other adapters can be used when they expose a compatible serial
interface and provide the ordered byte stream required by the selected host
transport. The MCP protocol contains no BaDCaT-specific commands.

The host adapter currently implemented for the agent uses TCP/IPv4. TCP/IP is
not required when using direct openMSX control, and the protocol architecture
allows other host transports to be added without changing the MCP tools or the
MSX command core.

## Current limitations

The resident agent is cooperative. It relies on the running software continuing
to service the normal MSX BIOS timer hook. A game or demo that permanently
disables interrupts, replaces the relevant BIOS path, or pages required system
state out of memory can temporarily make the resident agent unreachable.

Complex game screenshots are still experimental. Reading a large amount of
VRAM can take time over a slower serial interface, and software that controls
the video hardware in unusual ways may require additional handling.

The goal is not to promise unconditional control of every program. The goal is
to provide a safe and extensible platform for MSX development, automation,
diagnostics, preservation, and experimentation.

## Project status

The project currently includes:

- a resident MSX-DOS agent;
- a foreground development monitor;
- a protected binary protocol with retries and error detection;
- resumable PUT and GET file transfers;
- BASIC and machine-language workflows;
- host-rendered screenshots from captured VRAM;
- hardware-independent connector layers;
- an English technical reference;
- more than 330 automated tests; and
- the MIT License.

The next major milestone is validation on a physical MSX and performance testing
with faster communication hardware.

MSX-AI aims to make the MSX an interactive development platform for AI agents
while respecting the real capabilities and limitations of these machines.

## Documentation

- [Technical reference](TECHNICAL.md): architecture, setup, protocols, hardware,
  safety rules, development, and testing.
- [MSX agent reference](agent/README.md): agent lifecycle, command line, memory
  model, drivers, and MSX-side implementation.

## License

MSX-AI is released under the [MIT License](LICENSE).
