# MSX-AI overview

MSX-AI connects an MCP client to an emulated or physical MSX through an
auditable host implementation written in Python. Code that runs on the MSX is
provided as Z80 assembly source and reproducibly built MSX-DOS programs. The
project does not require an opaque host executable.

## Project origin

MSX-AI was conceived and created by **Rodrigo Galhardi M. Garcia**, originator
of the project's central concept: connect MCP not only to an emulator, but to a
real MSX through an agent executing on the target over a transport-independent
TCP/IPv4 path.

## Three target paths

- **Direct openMSX** controls an emulator through its control API. It does not
  use TCP or install the MSX-side agent.
- **Physical agent** exchanges framed protocol messages with the MSX-DOS agent
  through a selected host byte-stream transport. The current host adapter uses
  TCP over IPv4 and a transparent network-to-UART bridge.
- **Simulated agent** runs the same MSX-DOS agent inside openMSX and reaches it
  through RS232-Net and TCP. Agent operations do not fall back to openMSX
  debugger memory access.

One MCP server may retain an independent local openMSX channel and ASM-agent
channel. Tool names fix routing: `msx_local_*` never uses TCP, and
`msx_agent_*` never uses openMSX control APIs. Calls may alternate without a
selection step. A simulated bench pairs both channels to one emulator under a
shared `bench_id`; starting the server alone still never starts openMSX or
connects to hardware.

The installed `msx-ai-mcp` entry uses the official Python MCP SDK. STDIO is the
default and unauthenticated Streamable HTTP is available only on IPv4 loopback.
Every tool advertises an output schema and explicit behavior hints. The server
also provides hash-verified resources, deterministic documentation search, and
backend-aware prompts.

## Main capabilities

The server can boot or attach to openMSX, inspect status and CPU context,
enter BASIC programs, load applications, capture text screens, and render PNG
screenshots from VRAM. The agent path additionally exposes bounded RAM and VRAM
operations, direct I/O, and resumable MSX-DOS file transfer when the selected
runtime mode supports them.

The MSX-DOS agent has two modes. The default MemMan resident returns to DOS and
cooperatively services requests from the BIOS timer hook. It is intended for
observing compatible DOS software while preserving its execution context. The
foreground monitor owns the running payload and therefore permits direct
call, run, stop, slot, and mapper workflows that are unsafe for the resident.

## Where to continue

- Read `getting-started` for installation and a first connection.
- Read `backends` before choosing tools or an agent runtime mode.
- Read `safety` before writing memory or I/O ports on a live target.
- Read `transfers` before uploading or downloading an MSX-DOS file.
- Read `development` to build the agent and run validation.
