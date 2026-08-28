; Development-only wrapper. The canonical public build keeps resident tracing
; compiled but does not expose its command-line or TsrCall entry points.
MSXAI_TSR_BUILD: equ 0
MSXAI_XFER_HELPER_BUILD: equ 0
MSXAI_MAIN_BUILD: equ 1
MSXAI_DEVELOPMENT_TRACE: equ 1
TSR_BUILD_BASE: equ 04024h
TRANSPORT_STATE_SIZE: equ 5

include 'agent/msx_agent_core.asm'
