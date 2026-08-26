MSX-AI AGENT 0.1.3
==================

This package lets an MCP client control a physical MSX through a serial
bridge or a TCP/IP UNAPI implementation such as the Wi-Fi support provided
by an original MSX Pico with Wi-Fi or an MSX Pico+ cartridge.

REQUIREMENTS
------------

- MSX-DOS 2 or Nextor.
- A memory mapper with free 16 KiB segments.
- All ten program files from this ZIP kept together.
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

The 8251 driver uses ports 80h/81h, 19200 baud, and 8N1. The 16C550
driver uses ports 80h-87h, 115200 baud, 8N1, and hardware RTS/CTS.

UNAPI listens on TCP port 6603 by default. /PORT accepts 1 through 65534.
Configure the cartridge Wi-Fi before starting MSXAI. The host must connect
to the MSX IP address and selected port. No Pico/Pico+ firmware change is
required.

The default mode installs the agent as a MemMan resident and returns to DOS.
This is the recommended mode for file transfers and DOS applications.

MemMan A7 v1 is the safe lifecycle request for targets 0, 1, and 2. Offset
14 selects 8251, 16C550, or UNAPI; offset 15 is reserved and stays zero.
MP.COM selects UNAPI. Its 1 KiB page-2 stack has low/high guards and leaves
256 bytes below the caller SP. A6 is rejected; keep the suite at one version.

On the first UNAPI install only, TU.COM runs after warm boot and before
TL.COM to prepare compatible Pico/Pico+ firmware state. UART installs run
TL.COM directly. MP.COM configures the UNAPI port after TL.COM.

After BASIC-to-DOS _SYSTEM or CALL SYSTEM, the resident restores the same
listener automatically. No second MSXAI command is required.

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
