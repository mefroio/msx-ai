; Relocatable MemMan payload build. tools/build_memman_tsr.py assembles this
; source at three page-1 origins and emits the public MemMan relocation table.
MSXAI_TSR_BUILD: equ 1
MSXAI_DEVELOPMENT_TRACE: equ 0
TRANSPORT_STATE_SIZE: equ 5
TSR_BUILD_BASE: equ 04024h

include 'agent/msx_agent_core.asm'
