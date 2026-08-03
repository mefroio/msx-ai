; Single universal build. The MSX-DOS command line selects the UART driver.
MSXAI_TSR_BUILD: equ 0
TSR_BUILD_BASE: equ 04024h
TRANSPORT_STATE_SIZE: equ 5

include 'agent/msx_agent_core.asm'
