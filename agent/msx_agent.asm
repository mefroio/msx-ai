; Single universal build. The MSX-DOS command line selects the UART driver.
MSXAI_TSR_BUILD: equ 0
MSXAI_XFER_HELPER_BUILD: equ 0
MSXAI_MAIN_BUILD: equ 1
MSXAI_DEVELOPMENT_TRACE: equ 0
TSR_BUILD_BASE: equ 04024h
TRANSPORT_STATE_SIZE: equ 5

include 'agent/msx_agent_core.asm'
