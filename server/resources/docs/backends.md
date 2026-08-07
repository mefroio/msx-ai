# Backends and runtime modes

Backend selection changes both the available tools and the meaning of a CPU
snapshot. The simulated and physical agent paths deliberately share the same
protocol behavior; simulation is not permission to bypass that protocol with
openMSX debugger operations.

## Capability matrix

| Capability | Direct openMSX | Simulated agent | Physical agent |
|---|---|---|---|
| Start or attach target | `msx_boot`, `msx_attach` | `msx_tcp_bench_start` | `msx_agent_listen`, `msx_agent_connect` |
| Text screen and PNG screenshot | Yes | Yes | Yes |
| BASIC and BIOS text input | Yes | Resident | Resident |
| CPU snapshot | Exact debugger boundary | Cooperative callback context | Cooperative callback context |
| Typed RAM and VRAM read/write | Agent tools not used | Resident or monitor, with restrictions | Resident or monitor, with restrictions |
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
Use the simulated agent to test the real protocol and resident restrictions in
a repeatable emulator environment. Use the physical agent when behavior on
actual MSX hardware is the subject of the session. Call `msx_status` after
every selection or reconnect rather than inferring the backend from a previous
operation.
