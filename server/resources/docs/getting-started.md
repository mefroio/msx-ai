# Getting started

## Install the Python host

MSX-AI requires Python 3.10 or newer. Until the first PyPI release, install the
public source in an isolated environment:

```sh
git clone https://github.com/mefroio/msx-ai.git
cd msx-ai
pipx install .
msx-ai-mcp
```

After publication, `pipx install msx-ai` installs the same entry point from
PyPI. A checkout whose `.mcp.json` names `msx-ai-mcp` must be installed once
with pipx/pip before an IDE can start that command.

From a source checkout, an editable environment is:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Configure the MCP client to launch `msx-ai-mcp`. When running directly from a
checkout without installation, the compatibility entry point is:

```sh
python3 server/msx_mcp_server.py
```

The compatibility entry omits the official-SDK structured interface. Use the
installed `msx-ai-mcp` entry for output schemas, annotations, resources,
prompts, progress, cancellation, and negotiated modern/legacy MCP behavior.
STDIO is the default. Optional Streamable HTTP is restricted to the
unauthenticated IPv4 loopback endpoint `127.0.0.1:8000/mcp`:

```sh
msx-ai-mcp --transport http --host 127.0.0.1 --port 8000
```

Starting the server does not select an active backend. Routes are fixed by tool
name: `msx_local_*` always uses openMSX control, while `msx_agent_*` always uses
the ASM-agent protocol. Both may coexist and calls may alternate without any
selection step. `msx_targets_status` inventories the channels without changing
their routing.

## Direct openMSX

Install openMSX and provide the legally obtained system ROMs required by the
chosen machine. Call `msx_local_boot` with profile `basic`, `disk`, `dos`, or
`msx2plus`. The default is a headless `basic` session; set `window=true` for a
visible shared display.

An installed wheel materializes only the public MSX-AI machine/extension XML
profiles on the first emulator start. It never supplies proprietary ROMs. Set
`MSX_AI_OPENMSX_HOME` when those ROMs and profiles already live in another
isolated openMSX home.

A minimal check is:

1. Call `msx_local_boot` with `profile="basic"`.
2. Call `msx_local_status` and confirm backend `openmsx`.
3. Call `msx_local_screen`, or send a BASIC line with `msx_local_type_line`.

Alternatively, start the repository launcher and use `msx_local_attach` to control
that already-running openMSX instance without changing its power, throttle, or
audio state. If several live control sockets are discovered, attachment fails
safely and lists them; repeat the call with the intended exact `socket_path`.
On Windows this path names openMSX's loopback TCP-port descriptor rather than a
Unix socket; MSX-AI handles that distinction automatically.

## Simulated MSX agent

This source-checkout-only path requires openMSX, suitable ROMs, `z80asm`, and
the local bootable MSX-DOS or Nextor image used by the integration environment.
A separately pipx-installed server must receive
`MSX_AI_SOURCE_ROOT=/absolute/path/to/msx-ai` in its environment.

1. Call `msx_tcp_bench_start` with `mode="resident"`.
2. Call `msx_tcp_bench_status`; it reports local and agent channels with one
   shared `bench_id`.
3. Use `msx_agent_screen` to exercise the protocol path, or
   `msx_local_screen`/`msx_local_screenshot` to inspect the same emulator
   directly. Calls can alternate; neither changes the other route.

Use `mode="monitor"` when the host must call, run, or stop injected code.
`debug=true` is accepted only with monitor mode.

## Physical MSX agent

Build or obtain one matching seven-file agent suite and copy it to a short
MSX-DOS directory such as `A:\MSXAI`. Set `MSXAI_HOME` and add that directory
to `PATH`, then install one driver on the MSX:

```text
MSXAI /DRIVER:8251
MSXAI /DRIVER:16C550
```

Configure the transparent bridge for the same UART settings. If the bridge
connects outward, call `msx_agent_listen` with the host machine's specific LAN
IPv4 address; its safe default `127.0.0.1` accepts only local simulation. If
the bridge accepts connections, call `msx_agent_connect` with its IPv4 address.
After negotiation, call
`msx_agent_status` and verify the runtime mode, transport, and feature list before
performing writes.

ROM images and bootable disk images are not distributed with MSX-AI.
