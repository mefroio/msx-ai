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
PyPI.

From a source checkout, an editable environment is:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

The PowerShell equivalent is:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

MCP configuration is machine/client state and is not published by this
repository. In particular, root `.mcp.json` is ignored because it commonly
contains absolute executable paths. For Codex, add the installed STDIO server
with:

```sh
codex mcp add msx-ai -- msx-ai-mcp
```

The equivalent portable entry belongs in `~/.codex/config.toml`, or in the
ignored `.codex/config.toml` of a trusted checkout:

```toml
[mcp_servers.msx-ai]
command = "msx-ai-mcp"
```

Other MCP clients should configure the same `msx-ai-mcp` command in their own
local format. Keep any `OPENMSX_BIN` override local rather than committing an
absolute path. When running directly from a checkout without installation, the
compatibility entry point is:

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

Install openMSX. `OPENMSX_BIN` may select an executable explicitly; otherwise
MSX-AI searches `PATH`, the standard macOS app bundle, or registered/standard
Windows installations as appropriate. Call `msx_local_doctor` before boot. It
is read-only and reports executable presence/path, owned and attach transports,
`control_transport_supported`, `boot_supported`, `attach_supported`,
configuration homes, requested/resolved profile and machine, readiness, and
structured issues whose `action` fields contain concrete recommendations.
Candidate reports carry the transport/boot flags too, and the top-level result
always records `persistent_process_started=false`.

`auto` uses a validated configured BASIC machine and falls back to `cbios`.
The ROM-free `cbios` profile uses the C-BIOS supplied with openMSX for control,
screen, and cartridge-oriented smoke tests; it does not provide MSX BASIC or
MSX-DOS. Profiles `basic`, `disk`, `dos`, and `msx2plus` remain available when
their legally obtained firmware/media are installed. The default is headless;
set `window=true` for a visible shared display. Omitting `profile` still means
`basic` for compatibility, so request `auto` or `cbios` explicitly for a
portable first boot.

An installed wheel materializes only public MSX-AI machine/extension XML
profiles on the first isolated emulator start. It never supplies proprietary
ROMs. Choose `config_mode="isolated"` (the repeatable default), `"user"` (the
user's openMSX home/file pools with disposable process settings), or
`"overlay"` (managed MSX-AI templates plus user machine/ROM discovery).
`MSX_AI_OPENMSX_HOME` overrides only the managed isolated home.

A minimal check is:

1. Call `msx_local_doctor` with `profile="auto"` and the intended
   `config_mode`; resolve any reported issue.
2. Call `msx_local_boot` with the same profile/mode.
3. Call `msx_local_status` and confirm backend `openmsx`.
4. Call `msx_local_screen`. Send BASIC only if the resolved machine actually
   supplies MSX BASIC.

Alternatively, start the repository launcher and use `msx_local_attach` to control
that already-running openMSX instance without changing its power, throttle, or
audio state. If several live control sockets are discovered, attachment fails
safely and lists them; repeat the call with the intended exact `socket_path`.
Linux and macOS report `control_transport="stdio"` and
`attach_transport="unix_socket"`. Windows reports `tcp_sspi` for both
transports: owned boot waits for its child PID's openMSX descriptor, while
external attachment validates the selected existing descriptor. Both accept
only loopback ports 9938 through 10001 and require SSPI Negotiate. If SSPI is
unavailable, doctor/boot/attach reports an actionable unsupported result
instead of using unauthenticated raw TCP.

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

## Load a BLOAD through resident MSX BASIC

With the agent installed in its default resident mode and the visible MSX at an
MSX-DOS prompt or MSX BASIC `Ok` prompt, call:

```text
msx_agent_app_load(path="/host/path/GAME.BIN")
```

The default `environment="auto"` selects this flow only when the host file has a
valid seven-byte BLOAD `0xFE` header. From DOS, the host types `BASIC` and confirms
the `Ok` prompt. It then injects the exact header-declared RAM range through the
agent, always reads the complete range back, and submits a nonzero entry through
`DEFUSR0`/`USR0`. The host path is not copied onto an MSX disk and no local
openMSX API is involved. `execute="run"` returns after submission, whereas
`execute="call"` waits for up to three bounded screen probes for BASIC to
return to `Ok`.

The flow never relocates code. Its complete segment must fit in CPU pages 2/3
(`0x8000-0xFFFF`), and its entry must lie inside that segment. Page 0 is mapped
as Main-ROM in BASIC, while page 1 is not an available resident/BASIC payload
area. Use `environment="basic"` to request the same checks explicitly. Use
`environment="direct"` only for an artifact intentionally built for the
foreground monitor; `msx_agent_asm_load` remains the separate direct
source-assembly and RAM-injection tool.

ROM images and bootable disk images are not distributed with MSX-AI.
