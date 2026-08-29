# TCP/IP UNAPI validation with openMSX

openMSX does not directly emulate a Pico/Pico+ cartridge or run its firmware.
This opt-in validation substitutes only the TCP/IP UNAPI layer: the emulated
MSX opens a passive listener and the MSX-AI host connects to it, using the same
connection direction expected from physical hardware.

The harness does not download files or install dependencies. It requires the
matching pair from
[openMSXnet v0.9.7](https://github.com/antxiko/openMSXnet/releases/tag/v0.9.7):

- the modified openMSX binary containing the bridge-v1 `UnapiNet` device;
- `UNAPINET.COM` v0.9.7, which publishes TCP/IP UNAPI 1.1 through EXTBIO;
- a local licensed MSX-DOS 2/Nextor disk image;
- local licensed machine and SunriseIDE Nextor ROMs.

Do not combine `UNAPINET.COM` v0.9.7 with `UnapiNet` from current openMSX
master. The upstream integration uses internal bridge protocol v2, while this
public release uses v1. The emulator and COM file must come from the same
v0.9.7 release.

## Verified pins

| File | SHA-256 |
|---|---|
| [`openmsx-macos-arm64.zip`](https://github.com/antxiko/openMSXnet/releases/download/v0.9.7/openmsx-macos-arm64.zip) | `d259b4104d3e60847d9d748acf26df9b108b83b11fe8a1eff915f82fb58627f2` |
| [`openmsx-linux-x86_64.zip`](https://github.com/antxiko/openMSXnet/releases/download/v0.9.7/openmsx-linux-x86_64.zip) | `2c91d72b8cf7fe18c34d4f42690b1abf9c4fb2bc3e601b49e4abe602f3707be9` |
| [`openmsx-windows-x86_64.zip`](https://github.com/antxiko/openMSXnet/releases/download/v0.9.7/openmsx-windows-x86_64.zip) | `68296ad28751d090c5691264b39629723f748c7bd169182c80be798a9719b49b` |
| [`UNAPINET.COM`](https://github.com/antxiko/openMSXnet/releases/download/v0.9.7/UNAPINET.COM) | `86e7bb27d1f020e235929a6806f5f2dc8188c458119041c4017afd93a3c13227` |
| [`SHA256SUMS.txt`](https://github.com/antxiko/openMSXnet/releases/download/v0.9.7/SHA256SUMS.txt) | `d3e1be55147fdf80a9169bac8f5ff4b55294bb49b33977315a2bec6ce332a810` |
| Extracted `share/extensions/unapinet.xml` | `280dde2a60a7f73f777a4eb9be02eb3628e4b224c1c2d6e480ad35726e1718ae` |

The pinned `UNAPINET.COM` reports `HL=043Dh` for
`TCPIP_GET_CAPAB` block 1. This includes bit 5 (`0020h`), which is required
for passive `TCP_OPEN` with an unspecified remote address.

## Preflight

Supply the two previously downloaded release assets. Port 43123 is only the
suggested test value; any port from 1 through 65534 may be selected. TCP/IP
UNAPI reserves `FFFFh`/65535, so the harness rejects it.

```sh
python3 tools/openmsx_unapi_validation.py preflight \
  --archive /path/to/openmsx-macos-arm64.zip \
  --unapinet-com /path/to/UNAPINET.COM \
  --dos-hdd work/system-disks/msxdos.dsk \
  --openmsx-home .openmsx-home \
  --port 43123
```

The Make targets accept the same settings through environment variables:

```sh
export MSX_AI_UNAPINET_ARCHIVE=/path/to/openmsx-macos-arm64.zip
export MSX_AI_UNAPINET_COM=/path/to/UNAPINET.COM
export MSX_AI_DOS_HDD=work/system-disks/msxdos.dsk
export MSX_AI_OPENMSX_HOME=.openmsx-home
export MSX_AI_UNAPI_TEST_PORT=43123
make unapi-emulation-preflight

# After the preflight reports ready=true:
make test-unapi-emulation
```

The preflight verifies:

- the platform-specific asset and its pinned identity;
- SHA-256 for the ZIP, `UNAPINET.COM`, and `unapinet.xml`;
- safe extraction without traversal, links, or excessive expansion;
- the `unapinet`, `slotexpander`, `SunriseIDE_Nextor`, and `ram512k`
  extension definitions;
- the machine configuration, DOS image, and a Nextor ROM accepted by the XML;
- a temporary `openmsx --version` execution, including actionable diagnostics
  for missing dynamic libraries.

The pinned macOS binary dynamically links to Homebrew libraries. If one is
missing, the preflight reports the first path named by `dyld`; the harness does
not run `brew install` or otherwise modify the host.

## End-to-end execution

After the preflight returns `"ready": true`:

```sh
python3 tools/openmsx_unapi_validation.py run \
  --archive /path/to/openmsx-macos-arm64.zip \
  --unapinet-com /path/to/UNAPINET.COM \
  --dos-hdd work/system-disks/msxdos.dsk \
  --openmsx-home .openmsx-home \
  --port 43123
```

Add `--window` to watch the emulator. The harness makes temporary copies of
the DOS image and openMSX home, runs the canonical `make agent` build, imports
the twelve-file package into `A:\MSXAI`:

```text
MSXAI.COM     MSXAIXF.COM  MCP8251.TSR  MCP16550.TSR
MCP115K.TSR   MCPUNAPI.TSR TU.COM       MP.COM
BADINIT.COM   MEMMAN.COM   TL.COM       TK.COM
```

`MCP115K.TSR` is the prepatched 115200-baud 16C550 image and `BADINIT.COM` is
the physical BaDCaT runtime initializer. The UNAPI harness stages both because
it validates the canonical package, but executes neither one. It then types:

```text
SET MSXAI_HOME=A:\MSXAI
PATH A:\MSXAI;%PATH%
UNAPINET
MSXAI /DRIVER:UNAPI /PORT:43123
```

The public command keeps the port decimal. During the first resident install,
the loader first invokes transient `TU.COM` after the warm boot and before
`TL.COM` to prepare compatible Pico/Pico+ firmware state. This preparation is
a no-op for the UNAPINET fixture. UART installs bypass `TU.COM` and invoke
`TL.COM` directly. The loader also
encodes the selected value as the private fixed-width `MP/HHHH` command;
`MP.COM` runs after `TL.COM` and applies it through a versioned 16-byte MemMan
request with `A=A7h`, `HL=request`. A7 v2 is the general safe-lifecycle ABI for
target transport `0=8251`, `1=16C550`, or `2=UNAPI`; `MP.COM` writes target `2` at
offset 14 and divisor zero at offset 15. A7 v2 uses byte 15 as the requested
16C550 divisor (`1=115200`, `2=57600`), which must remain zero for 8251 or
UNAPI; the same byte returns the active divisor, or zero when 16C550 is not
active. The request also
identifies the port and a caller-owned 1 KiB page-2 stack surrounded by
16-byte low/high guards. Its complete guarded span fits below `C000h`, the TPA
top, and the current SP minus 256 bytes of caller headroom. Caller and resident
check both guards around `TCP_ABORT`/`TCP_OPEN`, keeping those potentially deep
calls off MemMan's small internal `TsrCall` stack. The old `A6h`, `HL=port`
raw ABI is reserved and rejected so mixed suite versions fail closed. The
guarded handoff is also used when `/PORT` is omitted and the selected value is
the default 6603; users never invoke the hexadecimal form directly.

The suite's UART default is 57600 baud. `/57600` and `/115200` are mutually
exclusive and accepted only with `/DRIVER:16C550`; a fresh resident install
selects `MCP16550.TSR` or `MCP115K.TSR`, respectively. Existing-resident
changes carry the divisor through A7 v2.

The host side is exercised exclusively through the project's public MCP
server over STDIO. The harness calls `msx_agent_connect`,
`msx_agent_status`, `msx_agent_memory_read`, and `msx_agent_disconnect`; it
does not instantiate the physical-target backend directly. A separate
openMSX debugger read is used only as an independent oracle for the RAM bytes
returned through MCP.

The first connection must report `agent_transport_id=2`,
`agent_transport=tcpip-unapi`, and runtime mode `resident`. The harness closes
that connection, advances emulated time without machine input, and connects
again to the same port. It repeats the disconnect and reconnection while a
BASIC program is running, then checks the BASIC-to-DOS transition without
reinstalling the resident.

The fixture adds the pinned `ram512k` extension at slot 1.1. Its 512 KiB is
deterministic test headroom for Nextor, the UNAPINET TSR, MemMan, and
MCPUNAPI.TSR; it is not a Pico/Pico+ hardware requirement. A physical MSX needs
an implementation-dependent number of free 16 KiB mapper segments.

The observable end-to-end path covers:

- TCP/IP UNAPI discovery through EXTBIO and the Nextor RAM helper;
- `GET_CAPAB` and the passive-listener requirement;
- `TCP_OPEN` on the selected port;
- `TCP_STATE` while establishing the connection;
- `TCP_SEND` and `TCP_RCV` during the handshake, status, and MCP memory read;
- host disconnect followed by automatic resident listener recovery without
  machine input.

It does not validate Pico/Pico+ firmware, cartridge registers, bus timing,
physical interrupts, Wi-Fi association, DHCP, or radio behaviour. Those remain
physical-hardware validation gates.

## Physical BaDCaT procedure is out of scope

The harness above does not configure or emulate BaDCaT. On physical hardware,
keep the host disconnected from the selected port (6603 by default), run
`MSXAI /UNINSTALL` before the initializer, then use matching commands:

```text
BADINIT
MSXAI /DRIVER:16C550
```

or, only for an explicit faster-line test:

```text
BADINIT /115200
MSXAI /DRIVER:16C550 /115200
```

`BADINIT /PORT:<1..65535>` selects another listener port for the current modem
session. It may appear before or after at most one of `/57600` and `/115200`;
duplicate port or baud options are invalid. Port 65535 is valid for this
ZiModem command, unlike `MSXAI /DRIVER:UNAPI /PORT:65535`, where `FFFFh` is
reserved as the UNAPI random-port sentinel. Pass the selected port to
`msx_agent_connect`.

`BADINIT` uses runtime `ATN0`, never performs firmware save, modem reset,
factory reset, or an `S60` write. It verifies `ATQ0S41=0A<port>` while
automatic stream entry is disabled, then commits with the final silent line
`ATHS41=1Q1`. Start `msx_agent_connect` only after the matching MSXAI driver is
running and use the same selected port. Although the BaDCaT UART line can be
selected up to 115200 baud, its effective throughput is approximately 57600
bps; 57600 is the safe default for both commands.

## Automated tests

Unit tests need neither openMSX nor the external assets:

```sh
python3 -m unittest tests.test_openmsx_unapi_validation -v
```

The E2E test is skipped by default. Enable it explicitly with:

```sh
MSX_RUN_UNAPI_INTEGRATION=1 \
MSX_AI_UNAPINET_ARCHIVE=/path/to/openmsx-macos-arm64.zip \
MSX_AI_UNAPINET_COM=/path/to/UNAPINET.COM \
MSX_AI_UNAPI_TEST_PORT=43123 \
python3 -m unittest \
  tests.test_openmsx_unapi_validation.OpenMSXUNAPIIntegrationTest -v
```

`MSX_AI_DOS_HDD`, `MSX_AI_OPENMSX_HOME`, `MSX_AI_BASIC_MACHINE`, and `MAKE`
may also be set. Missing prerequisites cause an actionable skip. A failure
after emulation begins is a real test failure and includes the MSX screen,
loaded extensions, mapper layout, and the tail of the openMSX output.
