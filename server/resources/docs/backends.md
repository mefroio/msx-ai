# Backends and runtime modes

MSX-AI does not maintain an implicit active backend. Tool names fix the route:
`msx_local_*` uses only the openMSX control API, and `msx_agent_*` uses only the
ASM-agent protocol. The channels can coexist and calls can alternate in any
order. The simulated and physical agent paths deliberately share the same
protocol behavior; a local diagnostic call is explicit and never masquerades
as evidence from the agent path.

Earlier checkout configurations must replace `msx_status` with the explicit
local/agent status or `msx_targets_status`, replace `msx_shutdown` with the
matching channel or bench lifecycle tool, and replace `msx_real_listen` with
`msx_agent_listen`. For every other historical `msx_<operation>` name, choose
the intended `msx_local_<operation>` or `msx_agent_<operation>` form. The
server does not publish ambiguous compatibility aliases.

## Capability matrix

| Capability | Direct openMSX | Simulated agent | Physical agent |
|---|---|---|---|
| Start or attach target | `msx_local_boot`, `msx_local_attach` | `msx_tcp_bench_start` | `msx_agent_listen`, `msx_agent_connect` |
| Text screen and PNG screenshot | Yes | Yes | Yes |
| BASIC and BIOS text input | Yes | Resident | Resident |
| CPU snapshot | Exact debugger boundary | Cooperative callback context | Cooperative callback context |
| Typed RAM and VRAM read/write | Yes, through `msx_local_*` | Resident or monitor, with restrictions | Resident or monitor, with restrictions |
| Application loader | Yes | Yes, mode restrictions apply | Yes, mode restrictions apply |
| Resumable MSX-DOS PUT/GET | No | Current resident | Current resident |
| Direct I/O-port access | Use expert openMSX console facilities | Agent | Agent |
| Call, run, stop injected code | Loader and emulator facilities | Monitor | Monitor |
| Slot or mapper selection | Use expert openMSX console facilities | Monitor | Monitor |
| Reset and raw openMSX commands | Yes | No | No |

## Resident agent

The default MemMan resident returns to MSX-DOS and services requests only when
the normal BIOS `H.TIMI` chain runs. It supports BIOS keyboard input, safe file
transfer through the transient helper, direct I/O, and bounded atomic capture
leases. Persistent manual pause, call, run, stop, slot selection, and mapper
selection are rejected.

Resident RAM page 1 (`0x4000` through `0x7FFF`) is unavailable because the TSR
must occupy that CPU page while servicing a request. Page 3 is accessible but
contains live BIOS, DOS, hook, stack, and system state.

## Foreground monitor

The monitor owns the foreground process and protects its relocated image. It
supports direct upload, call, asynchronous run, pause, resume, stop, and page-0
or page-1 slot and mapper selection. It does not return to DOS, does not provide
resident BIOS keyboard injection, and is not the MSX-DOS file-transfer mode.

## Choosing a path

Use direct openMSX for fast emulator automation and exact debugger snapshots.
Before the first boot, call `msx_local_doctor` with the intended profile and
configuration mode. `auto` can fall back to the ROM-free `cbios` profile for
control-channel validation; choose a firmware-backed profile when MSX BASIC or
MSX-DOS is required. Owned control uses stdio on Linux/macOS and an authenticated
loopback TCP descriptor with SSPI Negotiate on Windows.
Use the simulated agent to test the real protocol and resident restrictions in
a repeatable emulator environment. Use the physical agent when behavior on
actual MSX hardware is the subject of the session. Call `msx_local_status` or
`msx_agent_status` for the intended channel after connecting. In a simulated
bench, `msx_tcp_bench_status` reports both identities and their shared
`bench_id`. Connection order and previous operations never alter routing.

## Paired bench

The TCP bench owns exactly one openMSX process and exposes it through two
independent channels. `msx_agent_*` validates what a future physical MSX sees;
`msx_local_*` provides direct emulator diagnostics, including screenshots when
the agent is stalled. `msx_agent_disconnect` leaves the local diagnostic
channel alive. `msx_local_shutdown` is refused for a bench-owned process;
`msx_tcp_bench_shutdown` closes the agent, emulator, and temporary runtime as
one explicit lifecycle operation.
