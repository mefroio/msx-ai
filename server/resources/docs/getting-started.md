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
contains absolute executable paths.

Every client registers the same thing: a STDIO server named `msx-ai` running
the command `msx-ai-mcp` without arguments. Claude Code adds it from the CLI,
per project or, with `--scope user`, for every project:

```sh
claude mcp add msx-ai -- msx-ai-mcp
claude mcp add --scope user msx-ai -- msx-ai-mcp
```

A client session that was already running does not pick a newly added server
up, because its server list is resolved when the session starts. Start a new
session after registering the server.

Codex adds the same server with:

```sh
codex mcp add msx-ai -- msx-ai-mcp
```

The equivalent portable entry belongs in `~/.codex/config.toml`, or in the
ignored `.codex/config.toml` of a trusted checkout:

```toml
[mcp_servers.msx-ai]
command = "msx-ai-mcp"
```

Clients configured through JSON use their own local file:

```json
{
  "mcpServers": {
    "msx-ai": {
      "command": "msx-ai-mcp"
    }
  }
}
```

When the client does not inherit the interactive shell `PATH`, use the absolute
path of the installed entry point instead of the bare command. Keep that path
and any `OPENMSX_BIN` override local rather than committing them. When running
directly from a checkout without installation, the compatibility entry point
is:

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

Build or obtain one matching twelve-file agent suite and copy it to a short
MSX-DOS directory such as `A:\MSXAI`. Set `MSXAI_HOME` and add that directory
to `PATH`, then install one driver on the MSX:

```text
MSXAI /DRIVER:8251
MSXAI /DRIVER:16C550
MSXAI /DRIVER:16C550 /57600
MSXAI /DRIVER:16C550 /115200
MSXAI /DRIVER:UNAPI
MSXAI /DRIVER:UNAPI /PORT:43123
```

For 8251 or 16C550, configure the transparent bridge for the same UART
settings. The exact 16C550 default is 57600 baud, 8N1 with hardware RTS/CTS;
`/57600` selects that default explicitly. `/115200` is an opt-in line rate and
selects the matching `MCP115K.TSR` resident instead of the default
`MCP16550.TSR`. If the bridge
connects outward, call `msx_agent_listen` with the host machine's specific LAN
IPv4 address; its safe default `127.0.0.1` accepts only local simulation. If
the bridge accepts connections, call `msx_agent_connect` with its IPv4 address.

For the current BaDCaT prototype, use a reverse TCP connection at 57600 with
the native 16C550 resident; do not install the external FOSSIL driver. From a
clean boot, run:

```text
MSXAI /UNINSTALL
BADINIT /PREPARE
MSXAI /DRIVER:16C550 /57600
```

Start the MCP host listener on its specific LAN IPv4 address and fixed port:

```text
msx_agent_listen(host="192.168.0.62", port=6603)
```

While it is waiting, point BaDCaT at the MCP host address, not the MSX address:

```text
BADINIT /CONNECT:192.168.0.62 /PORT:6603
```

The port defaults to 6603 and accepts 1 through 65535. `/CONNECT` is
57600-only, requires the resident to be active already, and is deliberately
one-shot for that installation. The UART provides no trustworthy TCP/command
mode state, so repeating the AT dial could inject it as MCP payload. Reboot and
repeat the complete sequence before another attempt. BADINIT reports only that
the dial was issued; `msx_agent_listen` returning confirms the handshake.

`/PREPARE` closes current listeners, establishes echo-off hardware-flow command
mode, requests volatile `ATS2=255`, and requires `OK` before the resident takes
ownership. The resident repeats `S2=255` in the final silent dial command
before entering streaming. This mitigates ZiModem 3.5.5's unconditional guarded `+++` escape;
that version does not implement the previously assumed `S63` switch.

`BADINIT` changes only the current modem session. It never saves with `AT&W`,
never resets with `ATZ`, never restores factory settings with `AT&F`, and never
writes the persistent `S60` listener register. A power cycle therefore returns
to the modem's previously saved configuration.

The older inbound listener remains available with bare `BADINIT`. It first
requires `OK` from
`ATQ0S41=0A<port>`, where `<port>` is the selected decimal value, so the
runtime listener is verified while automatic stream entry is disabled. With
no `/PORT` option this command is `ATQ0S41=0A6603`. Its final send is
`ATHS41=1Q1`, which drops premature clients, enables auto-stream, and only then
enables quiet mode; no later AT command can race a host entering stream mode.

The initializer enables hardware flow control in its first bootstrap response,
uses BaDCaT's reference UART setup order, and observes ZiModem's delayed baud
transition before validating `/115200`. On failure it reports the stage,
command, hexadecimal RX sample, and UART status instead of printing binary
noise directly.

The 16C550 agent negotiates 128-byte MCP payloads so RAM, VRAM, screenshots,
and file transfers are automatically split into UART bursts validated on a
physical BaDCaT at 57600. This limit is local to the 16C550 driver; 8251 and
UNAPI retain 320-byte public payloads. A bounded 16C550 timeout also clears only
that UART's partial FIFOs and reapplies 8N1 RTS/CTS before a new connection.

The 16C550 driver also uses a one-shot RX-to-TX turnaround guard motivated by
physical BaDCaT timing. Every received byte arms a latch; only the first
following write waits 190 `DJNZ` iterations (about 0.72 ms on a standard
3.579545 MHz Z80), so response payload bytes do not receive a per-byte delay.
This product-neutral behavior applies at 57600 and 115200; 8251 and UNAPI are
unchanged.

The 115200 value is the UART line rate, not an end-to-end MCP throughput
guarantee. BaDCaT's published effective-throughput limit is 57,600 bit/s, and
protocol framing, flow control, modem processing, and MSX execution reduce the
application payload rate further. Use `/115200` for controlled testing rather
than assuming twice the transfer speed.

The earlier FOSSIL implementation remains in source control for comparison,
but it is not loaded or required by this reverse workflow.

For `/DRIVER:UNAPI`, configure the cartridge's existing Wi-Fi connection first.
The MSX opens a passive TCP/IP UNAPI listener, so call `msx_agent_connect` with
the MSX IPv4 address and the same port. Port 6603 is only the default; `/PORT`
accepts a decimal port from 1 through 65534. UNAPI reserves 65535 as its
random-port sentinel, which is unsuitable when the host must know the endpoint.
On a first resident UNAPI install, the bundled transient `TU.COM` runs after
the MemMan warm boot and before `TL.COM` to prepare compatible Pico/Pico+
firmware state. UART installs run `TL.COM` directly. The bundled `MP.COM` still
runs after `TL.COM` to apply the selected listener port, including the default
6603. Both helpers are private; users continue to enter `/PORT` in decimal and
never invoke them directly. On later BASIC-to-DOS transitions, `_SYSTEM` and
`CALL SYSTEM` restore the configured listener automatically.
This capability-based path is intended for MSX Pico+ and original MSX Pico
cartridges equipped with Wi-Fi.
After a resident socket is lost, the listener reopens silently. The host can
reconnect to the same IP address and port without a command or key press on the
MSX. This behavior has been validated on a physical HOTBIT 1.0 with an MSX Pico+
cartridge. `/MONITOR` remains available for foreground diagnostics.
The opt-in
`tools/openmsx_unapi_validation.py` harness can validate the same TCP/IP UNAPI
contract with a matched openMSXnet/UNAPINET pair. It covers discovery, passive
TCP on a custom port, bidirectional traffic, and automatic listener recovery
without machine input, but does not emulate Pico firmware, Wi-Fi, cartridge
registers, bus behavior, or physical timing.
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
