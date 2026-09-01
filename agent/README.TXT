MSX-AI AGENT @VERSION@
==================

This package lets an MCP client control a physical MSX through a serial
bridge or a TCP/IP UNAPI implementation such as the Wi-Fi support provided
by an original MSX Pico with Wi-Fi or an MSX Pico+ cartridge.

REQUIREMENTS
------------

- MSX-DOS 2 or Nextor.
- A memory mapper with free 16 KiB segments.
- All twelve program files from this ZIP kept together.
- For UNAPI: a TCP/IP UNAPI implementation and configured Wi-Fi.

INSTALLATION
------------

1. Extract all files to a short directory, for example A:\MSXAI.
2. Add these lines to AUTOEXEC.BAT:

   SET MSXAI_HOME=A:\MSXAI
   PATH A:\MSXAI;%PATH%

3. Restart MSX-DOS, enter the directory, and select one driver:

   MSXAI /DRIVER:UNAPI
   MSXAI /DRIVER:UNAPI /PORT:43123
   MSXAI /DRIVER:8251
   MSXAI /DRIVER:16C550
   MSXAI /DRIVER:16C550 /115200

The 8251 driver uses ports 80h/81h, 19200 baud, and 8N1. The 16C550
driver uses ports 80h-87h, 8N1, and hardware RTS/CTS. It defaults to
exactly 57600 baud. /57600 is explicit; /115200 selects divisor 1.

UNAPI listens on TCP port 6603 by default. /PORT accepts 1 through 65534.
Configure the cartridge Wi-Fi before starting MSXAI. The host must connect
to the MSX IP address and selected port. No Pico/Pico+ firmware change is
required.

The default mode installs the agent as a MemMan resident and returns to DOS.
This is the recommended mode for file transfers and DOS applications.

If a resident TCP connection is lost, the listener reopens silently. The host
can connect again to the same IP address and port without typing a command or
pressing a key on the MSX. This was validated on a physical HOTBIT 1.0 with
an MSX Pico+ cartridge.

MemMan A7 v2 is the safe lifecycle request for targets 0, 1, and 2. Offset
14 selects 8251, 16C550, or UNAPI. Offset 15 selects divisor 2 (57600) or
1 (115200) for 16C550 and is zero otherwise. MP.COM selects UNAPI. Its
1 KiB page-2 stack has low/high guards and leaves 256 bytes below caller SP.
A6 is rejected; keep the suite at one version.

On the first UNAPI install only, TU.COM runs after warm boot and before
TL.COM to prepare compatible Pico/Pico+ firmware state. UART installs run
TL.COM directly. MP.COM configures the UNAPI port after TL.COM.

After BASIC-to-DOS _SYSTEM or CALL SYSTEM, the resident restores the same
listener automatically. No second MSXAI command is required.

BADCAT / ZIMODEM
----------------

The preferred prototype uses BaDCaT as a TCP client at 57600. It needs no
FOSSIL driver. From a clean boot, run:

   MSXAI /UNINSTALL
   BADINIT /PREPARE
   MSXAI /DRIVER:16C550 /57600

Start msx_agent_listen on the MCP host LAN IP and fixed port. While it waits,
point BaDCaT at the host address, not the MSX address:

   BADINIT /CONNECT:192.168.0.62 /PORT:6603

The port defaults to 6603. /CONNECT needs the active 57600 native 16C550
resident. It is one-shot for that resident because a second AT line might be
sent as MCP data if the modem is still streaming. Reboot and repeat the full
/PREPARE sequence before another attempt. The host listener confirms the
handshake; BADINIT can report only that it issued the dial.

/PREPARE closes runtime listeners, selects RTS/CTS, disables command echo,
and requests volatile S2=255. The resident repeats S2 in the final dial. This
mitigates ZiModem 3.5.5's +++ stream escape but does not save any setting.

The older inbound listener is still available:

   MSXAI /UNINSTALL
   BADINIT
   MSXAI /DRIVER:16C550

For that legacy mode, /PORT:<decimal> accepts 1 through 65535 and defaults to
6603. /57600 or /115200 selects the temporary baud and must match MSXAI. Use
the selected address and port with msx_agent_connect.

BADINIT never saves, reloads, resets, factory-resets, or changes register S60.
It does not send even S60=0 because that deletes saved listeners. Existing
saved modem settings return after a power cycle. A failed probe shows its
stage, RX bytes in hex, and LSR flags.

MONITOR MODE
------------

Use /MONITOR when direct execution, CPU control, or unrestricted RAM access
is required:

   MSXAI /DRIVER:UNAPI /MONITOR
   MSXAI /DRIVER:UNAPI /MONITOR DEBUG

Monitor mode stays in the foreground. Press Ctrl+Stop to request exit.
DEBUG is accepted only with /MONITOR.

REMOVAL AND UPDATE
------------------

Remove the resident agent with:

   MSXAI /UNINSTALL

Before replacing these files with another release, uninstall the resident
agent, copy the complete new set, and install it again. Do not mix files from
different releases.

FILES
-----

MSXAI.COM      Installer, resident control, and foreground monitor.
MSXAIXF.COM    File-transfer worker launched automatically by the host.
MCPUNAPI.TSR   TCP/IP UNAPI resident transport.
MCP8251.TSR    8251 serial resident transport.
MCP16550.TSR   16C550 serial resident transport.
MCP115K.TSR    16C550 115200-baud resident transport.
BADINIT.COM    Non-persistent BaDCaT/ZiModem session initializer.
TU.COM         Internal pre-TL helper used only for first UNAPI install.
MP.COM         Internal UNAPI port configurator used during installation.
MEMMAN.COM     MemMan resident manager.
TL.COM         MemMan TSR loader.
TK.COM         MemMan TSR remover.

NOTES
-----

- Keep MSXAIXF.COM available in PATH for host-managed file transfers.
- The optional UNAPIPRB.COM diagnostic is distributed separately.
- LICENSE and MEMMAN-NOTICE.txt contain license information.
- Read this file on the MSX with: TYPE README.TXT

Project: https://github.com/mefroio/msx-ai
