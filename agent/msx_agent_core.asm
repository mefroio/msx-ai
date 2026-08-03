; ============================================================================
; MSX-AI universal MCP agent
; ============================================================================
; The default path installs a relocatable MemMan TSR in CPU page 1 and returns
; to MSX-DOS. H.KEYI/H.TIMI keep the UART service reachable while ordinary DOS
; programs and cooperative games run. /MONITOR selects the older foreground
; supervisor below BDOS for explicit upload/call/run workflows.
;
; The single wrapper includes every supported byte-stream driver. The loader
; selects one explicitly from the MSX-DOS command line and binds six resident
; JP vectors once, so protocol throughput has no per-byte selection branch.
; Protocol v2 (all lengths are 1..255):
;   ?                         -> 'M', version, capabilities, resident-page
;   q                         -> 'K', state (0=monitor,1=running,2=paused), version
;   r ah al n                 -> n raw RAM bytes
;   p ah al n data...         -> 'K'
;   v bank ah al n            -> n raw VRAM bytes (17-bit address)
;   w bank ah al n data...    -> 'K'
;   c ah al                   -> call routine synchronously, then 'K'
;   j ah al                   -> 'K', launch code asynchronously from monitor
;   s                         -> 'K', pause interrupted code and service commands
;   g                         -> 'K', resume interrupted code
;   k                         -> 'K', abandon interrupted code and return to monitor
;   i port                    -> one byte read from an arbitrary Z80 I/O port
;   o port value              -> 'K', write an arbitrary Z80 I/O port
;   l page slot               -> 'K', map an MSX slot (foreground monitor only)
;   m page segment            -> 'K', select mapper segment (monitor only)
;   F                         -> 'K',3,max-lo,max-hi; switch to framed v3
;   z                         -> 'K', uninstall (monitor state only)
;
; This cooperative monitor requires maskable interrupts and the BIOS H.KEYI
; chain to remain active.  Arbitrary games that replace the BIOS ISR or keep DI
; require a hardware/NMI monitor; programs developed through this MCP backend
; should leave the standard interrupt chain enabled.
; ============================================================================

; The foreground monitor uses a fixed upper-TPA address.  The default resident
; payload is assembled independently for MemMan and is relocated within page 1
; by TsrLoad; no fixed TPA address is used by that lifecycle.
if MSXAI_TSR_BUILD
RES_BASE:       equ TSR_BUILD_BASE
else
RES_BASE:       equ 08600h
endif
H_KEYI:         equ 0FD9Ah
H_TIMI:         equ 0FD9Fh
MSXVER:         equ 0002Dh
RDSLT:          equ 0000Ch
WRSLT:          equ 00014h
EXTBIO:         equ 0FFCAh
EXPTBL:         equ 0FCC1h
RAMAD0:         equ 0F341h
REG14SAV:       equ 0FFEDh
MODE:           equ 0FAFCh      ; bits 2:1 encode installed VRAM capacity
SCRMOD:         equ 0FCAFh
REG1SAV:        equ 0F3E0h
REG2SAV:        equ 0F3E1h
PROTO_VERSION:  equ 2
FRAMED_VERSION: equ 3
FRAMED_MAX:     equ 0140h      ; 320-byte payload, safe under Nextor TPA
CAPABILITIES:   equ 0FFh       ; core + framed v3 + hardware/mapping
CAPABILITY_RUN: equ 008h
CAPABILITY_MAPPING: equ 080h
; The hook stack has no recursion, no local frame buffers and never calls user
; code. A static high-water audit stays below 64 bytes; 224 bytes leaves more
; than three times that headroom while keeping the universal build below BDOS.
STACK_RESERVE:  equ 00E0h       ; 224 bytes; audited hook high-water is <64

RUNTIME_RESIDENT: equ 0
RUNTIME_MONITOR:  equ 1
LOADER_ACTION_INSTALL: equ 0
LOADER_ACTION_UNINSTALL: equ 1
TRANSPORT_FLAG_KEYI_EXCLUSIVE: equ 1

FRAME_REQUEST:    equ 1
FRAME_RESPONSE:   equ 2
FRAME_FLAG_ERROR: equ 4
FRAME_OK:         equ 0
FRAME_BAD_OPCODE: equ 1
FRAME_BAD_ARG:    equ 2
FRAME_BAD_STATE:  equ 3
FRAME_RANGE:      equ 4
FRAME_BAD_CRC:    equ 5
FRAME_BUSY:       equ 6
FRAME_UNSUPPORTED: equ 7

; Eight consecutive ESC bytes form the out-of-band reconnect marker while the
; framed parser is seeking magic.  A single byte is deliberately insufficient:
; random serial noise must not silently downgrade an active v3 session.
RECONNECT_BYTE:   equ 01Bh
RECONNECT_LENGTH: equ 8

if MSXAI_TSR_BUILD
    org TSR_BUILD_BASE
else
    org 0100h

installer:
    ld (loader_entry_sp),sp
    call loader_parse_command_line
    jp c,loader_usage_exit

    ld de,install_banner
    ld c,9
    call 0005h
    ld a,(loader_action)
    cp LOADER_ACTION_UNINSTALL
    jr z,install_banner_uninstall
    ld a,(loader_transport_id)
    or a
    ld de,transport_8251_banner
    jr z,install_banner_transport_ready
    ld de,transport_16c550_banner
install_banner_transport_ready:
    ld c,9
    call 0005h
    ld a,(loader_runtime_mode)
    or a
    ld de,resident_mode_banner
    jr z,install_banner_mode_ready
    ld de,monitor_mode_banner
install_banner_mode_ready:
    ld c,9
    call 0005h
    ld a,(loader_debug_enabled)
    or a
    jr z,install_banner_done
    ld de,debug_on_banner
    ld c,9
    call 0005h
    jr install_banner_done
install_banner_uninstall:
    ld de,uninstall_mode_banner
    ld c,9
    call 0005h
install_banner_done:

    ld a,(loader_action)
    cp LOADER_ACTION_UNINSTALL
    jp z,loader_uninstall_resident
    ld a,(loader_runtime_mode)
    cp RUNTIME_RESIDENT
    jp z,loader_install_resident

    ; /MONITOR retains the fixed foreground implementation.  The default path
    ; above never inspects or modifies this address: it uses a MemMan segment.
    ld a,(bdos_proxy + 3)
    cp 'M'
    jr nz,install_check_partial
    ld a,(bdos_proxy + 4)
    cp 'A'
    jr nz,install_check_partial
    ld a,(bdos_proxy + 5)
    cp PROTO_VERSION
    jr nz,install_check_partial
    call loader_keyi_is_agent
    jr nz,install_check_partial
    call loader_timi_is_agent
    jp nz,install_inconsistent
    ld de,already_message
    ld c,9
    call 0005h
    ld c,0
    jp 0005h

install_check_partial:
    ; Refuse to overwrite a partial/modified installation. Saving our own JP
    ; as the "old" hook would create a recursive chain.
    call loader_keyi_is_agent
    jp z,install_inconsistent
    call loader_timi_is_agent
    jp z,install_inconsistent

install_new:
    di

    ; The CALL 5 destination is also the documented top of the MSX-DOS TPA.
    ld hl,(0006h)
    ld (loader_old_bdos),hl
    ; The full resident image and hook stack must remain below the original
    ; BDOS entry. Six bytes immediately before BDOS are reserved metadata.
    ld de,hook_stack_top + 6
    or a
    sbc hl,de
    jp c,install_no_room

    ; Copy the position-assembled image into protected RAM below BDOS.
    ld hl,resident_source
    ld de,resident_start
    ld bc,resident_size
    ldir

    ; Persist only the selected runtime configuration. The command parser and
    ; its strings remain transient in the COM loader.
    ld a,(loader_transport_id)
    ld (active_transport_id),a
    ld (bdos_proxy + 6),a
    ld a,(loader_runtime_mode)
    ld (runtime_mode),a
    ld a,(loader_debug_enabled)
    ld (debug_enabled),a

    ; Preserve the CP/M compatibility bytes immediately preceding BDOS and
    ; create a proxy whose low byte remains 06h, as required by MSX-DOS.
    ld hl,(loader_old_bdos)
    ld (old_bdos),hl
    ld (bdos_proxy + 1),hl
    ld de,6
    or a
    sbc hl,de
    ld de,resident_start
    ld bc,6
    ldir

    ; Preserve and replace H.KEYI.  Five bytes fit either JP+padding or CALLF.
    ld hl,H_KEYI
    ld de,old_keyi
    ld bc,5
    ldir
    ld a,0C3h
    ld (H_KEYI),a
    ld hl,resident_keyi_hook
    ld (H_KEYI + 1),hl
    xor a
    ld (H_KEYI + 3),a
    ld (H_KEYI + 4),a

    ld hl,H_TIMI
    ld de,old_timi
    ld bc,5
    ldir
    ld a,0C3h
    ld (H_TIMI),a
    ld hl,resident_timi_hook
    ld (H_TIMI + 1),hl
    xor a
    ld (H_TIMI + 3),a
    ld (H_TIMI + 4),a

    ; Lower the TPA only when its original top is above the resident proxy.
    ; Configurations whose TPA already ends below the proxy are left untouched;
    ; pointing them upward would expose protected DOS RAM.
    xor a
    ld (tpa_lowered),a
    ld hl,(old_bdos)
    ld de,bdos_proxy
    or a
    sbc hl,de
    jr c,install_tpa_ready
    jr z,install_tpa_ready
    ld hl,bdos_proxy
    ld (0006h),hl
    ld a,1
    ld (tpa_lowered),a
install_tpa_ready:

    call resident_initialize

install_enter_monitor:
    ; The foreground monitor stack grows down through the reduced TPA, while
    ; hook parsing uses its independent protected stack above the resident.
    ld sp,RES_BASE
    jp resident_main

install_no_room:
    ei
    ld de,no_room_message
    ld c,9
    call 0005h
    ld c,0
    jp 0005h

; ------------------------------------------------------- resident lifecycle --
; The default mode is a genuine MemMan TSR.  If the ID already exists, use its
; standardized talk entry to select/reselect the transport without installing
; a duplicate.  Otherwise the embedded loader takes over and does not return.
loader_install_resident:
    call memman_find_agent
    jr c,loader_install_resident_new
    call memman_reconfigure_agent
    ld b,a
    ld a,(loader_transport_id)
    cp b
    jr nz,loader_resident_call_error
    ld de,already_message
    jr loader_resident_message_exit
loader_install_resident_new:
    ld a,(memman_present)
    or a
    jr z,loader_install_resident_handoff
    ld a,(memman_compatible)
    or a
    jr z,loader_memman_incompatible
loader_install_resident_handoff:
    jp memman_loader_install

loader_uninstall_resident:
    call memman_find_agent
    jr nc,loader_uninstall_resident_found
    ld a,(memman_present)
    or a
    jr z,loader_resident_not_installed
    ld a,(memman_compatible)
    or a
    jr z,loader_memman_incompatible
    jr loader_resident_not_installed
loader_uninstall_resident_found:
    jp memman_loader_uninstall
loader_resident_not_installed:
    ld de,not_installed_message
    jr loader_resident_message_exit
loader_resident_call_error:
    ld de,resident_call_error_message
    jr loader_resident_message_exit
loader_memman_incompatible:
    ld de,memman_incompatible_message
loader_resident_message_exit:
    ld c,9
    call 0005h
    ld c,0
    jp 0005h

install_banner:
    db 13,10,"MSX-AI universal MCP agent v2",13,10,"$"
transport_8251_banner:
    db "Driver: 8251-compatible MSX RS-232",13,10,"$"
transport_16c550_banner:
    db "Driver: 16C550-compatible UART, 115200 RTS/CTS",13,10,"$"
resident_mode_banner:
    db "Mode: MemMan resident agent (default)",13,10,"$"
monitor_mode_banner:
    db "Mode: foreground monitor",13,10,"$"
uninstall_mode_banner:
    db "Action: uninstall resident agent",13,10,"$"
debug_on_banner:
    db "On-screen command trace: DEBUG ON",13,10,"$"
no_room_message:
    db "Not enough upper TPA space for the resident agent",13,10,"$"
already_message:
    db "Resident agent already active; transport selected",13,10,"$"
not_installed_message:
    db "Resident agent is not installed",13,10,"$"
resident_call_error_message:
    db "Resident agent rejected the transport selection",13,10,"$"
memman_incompatible_message:
    db "MemMan 2.4 or newer is required",13,10,"$"
inconsistent_message:
    db "Inconsistent resident agent state",13,10,"$"
usage_message:
    db 13,10,"Usage:",13,10
    db "  MSXAI /DRIVER:8251",13,10
    db "  MSXAI /DRIVER:16C550",13,10
    db "  MSXAI /DRIVER:8251 /MONITOR [DEBUG ON]",13,10
    db "  MSXAI /DRIVER:16C550 /MONITOR [DEBUG ON]",13,10
    db "  MSXAI /UNINSTALL",13,10
    db "DEBUG ON is intentionally restricted to /MONITOR.",13,10,"$"
driver_required_message:
    db "Select exactly one /DRIVER:8251 or /DRIVER:16C550",13,10,"$"
debug_requires_monitor_message:
    db "DEBUG ON requires /MONITOR",13,10,"$"
debug_syntax_message:
    db "DEBUG must be followed by ON",13,10,"$"
uninstall_syntax_message:
    db "/UNINSTALL cannot be combined with driver, monitor, or debug options",13,10,"$"
unknown_option_message:
    db "Unknown command-line option",13,10,"$"
loader_transport_id:
    db 0FFh
loader_runtime_mode:
    db RUNTIME_RESIDENT
loader_debug_enabled:
    db 0
loader_action:
    db LOADER_ACTION_INSTALL
loader_command_buffer:
    ds 128,0

loader_parse_command_line:
    ld a,0FFh
    ld (loader_transport_id),a
    xor a
    ld (loader_runtime_mode),a
    ld (loader_debug_enabled),a
    ld (loader_action),a

    ; Normalize the counted CP/M command tail to uppercase and terminate it.
    ld a,(0080h)
    and 07Fh
    ld b,a
    ld hl,0081h
    ld de,loader_command_buffer
loader_copy_tail:
    ld a,b
    or a
    jr z,loader_copy_tail_done
    ld a,(hl)
    cp 'a'
    jr c,loader_copy_tail_store
    cp 'z' + 1
    jr nc,loader_copy_tail_store
    sub 020h
loader_copy_tail_store:
    ld (de),a
    inc hl
    inc de
    djnz loader_copy_tail
loader_copy_tail_done:
    xor a
    ld (de),a

    ld hl,loader_command_buffer
loader_parse_token_loop:
    call loader_skip_spaces
    ld a,(hl)
    or a
    jp z,loader_parse_tokens_done

    ld de,option_help_short
    call loader_token_equals
    jp z,loader_parse_help
    ld de,option_help_long
    call loader_token_equals
    jp z,loader_parse_help
    ld de,option_driver_8251
    call loader_token_equals
    jr z,loader_parse_8251
    ld de,option_driver_16c550
    call loader_token_equals
    jr z,loader_parse_16c550
    ld de,option_monitor
    call loader_token_equals
    jr z,loader_parse_monitor
    ld de,option_debug
    call loader_token_equals
    jr z,loader_parse_debug
    ld de,option_uninstall
    call loader_token_equals
    jr z,loader_parse_uninstall
    ld de,unknown_option_message
    jp loader_parse_error

loader_parse_8251:
    ld a,(loader_transport_id)
    cp 0FFh
    jp nz,loader_parse_driver_error
    xor a
    ld (loader_transport_id),a
    call loader_skip_token
    jp loader_parse_token_loop
loader_parse_16c550:
    ld a,(loader_transport_id)
    cp 0FFh
    jr nz,loader_parse_driver_error
    ld a,UART16C550_ID
    ld (loader_transport_id),a
    call loader_skip_token
    jp loader_parse_token_loop
loader_parse_monitor:
    ld a,RUNTIME_MONITOR
    ld (loader_runtime_mode),a
    call loader_skip_token
    jp loader_parse_token_loop
loader_parse_debug:
    call loader_skip_token
    call loader_skip_spaces
    ld de,option_on
    call loader_token_equals
    jr nz,loader_parse_debug_syntax_error
    ld a,1
    ld (loader_debug_enabled),a
    call loader_skip_token
    jp loader_parse_token_loop
loader_parse_uninstall:
    ld a,(loader_action)
    or a
    jr nz,loader_parse_uninstall_error
    ld a,LOADER_ACTION_UNINSTALL
    ld (loader_action),a
    call loader_skip_token
    jp loader_parse_token_loop

loader_parse_tokens_done:
    ld a,(loader_action)
    cp LOADER_ACTION_UNINSTALL
    jr z,loader_parse_uninstall_done
    ld a,(loader_transport_id)
    cp 0FFh
    jr z,loader_parse_driver_error
    ld a,(loader_debug_enabled)
    or a
    jr z,loader_parse_ok
    ld a,(loader_runtime_mode)
    cp RUNTIME_MONITOR
    jr nz,loader_parse_debug_error
    jr loader_parse_ok
loader_parse_uninstall_done:
    ld a,(loader_transport_id)
    cp 0FFh
    jr nz,loader_parse_uninstall_error
    ld a,(loader_runtime_mode)
    or a
    jr nz,loader_parse_uninstall_error
    ld a,(loader_debug_enabled)
    or a
    jr nz,loader_parse_uninstall_error
loader_parse_ok:
    or a
    ret
loader_parse_help:
    scf
    ret
loader_parse_driver_error:
    ld de,driver_required_message
    jr loader_parse_error
loader_parse_debug_error:
    ld de,debug_requires_monitor_message
    jr loader_parse_error
loader_parse_debug_syntax_error:
    ld de,debug_syntax_message
    jr loader_parse_error
loader_parse_uninstall_error:
    ld de,uninstall_syntax_message
loader_parse_error:
    ld c,9
    call 0005h
    scf
    ret

loader_usage_exit:
    ld de,usage_message
    ld c,9
    call 0005h
    ld c,0
    jp 0005h

loader_skip_spaces:
    ld a,(hl)
    cp ' '
    jr z,loader_skip_one_space
    cp 9
    ret nz
loader_skip_one_space:
    inc hl
    jr loader_skip_spaces

loader_skip_token:
    ld a,(hl)
    or a
    ret z
    cp ' '
    ret z
    cp 9
    ret z
    inc hl
    jr loader_skip_token

; Compare the token at HL with the zero-terminated option at DE. Z means an
; exact token match; both input pointers are preserved.
loader_token_equals:
    push hl
    push de
    push bc
loader_token_compare:
    ld a,(de)
    or a
    jr z,loader_token_end
    ld c,a
    ld a,(hl)
    cp c
    jr nz,loader_token_no
    inc hl
    inc de
    jr loader_token_compare
loader_token_end:
    ld a,(hl)
    or a
    jr z,loader_token_yes
    cp ' '
    jr z,loader_token_yes
    cp 9
    jr z,loader_token_yes
loader_token_no:
    pop bc
    pop de
    pop hl
    ld a,1
    or a
    ret
loader_token_yes:
    pop bc
    pop de
    pop hl
    xor a
    ret

option_help_short:
    db "/?",0
option_help_long:
    db "/HELP",0
option_driver_8251:
    db "/DRIVER:8251",0
option_driver_16c550:
    db "/DRIVER:16C550",0
option_monitor:
    db "/MONITOR",0
option_debug:
    db "DEBUG",0
option_on:
    db "ON",0
option_uninstall:
    db "/UNINSTALL",0
loader_old_bdos:
    dw 0

install_inconsistent:
    ld de,inconsistent_message
    ld c,9
    call 0005h
    ld c,0
    jp 0005h

loader_keyi_is_agent:
    ld a,(H_KEYI)
    cp 0C3h
    ret nz
    ld hl,(H_KEYI + 1)
    ld de,resident_keyi_hook
    or a
    sbc hl,de
    ret

loader_timi_is_agent:
    ld a,(H_TIMI)
    cp 0C3h
    ret nz
    ld hl,(H_TIMI + 1)
    ld de,resident_timi_hook
    or a
    sbc hl,de
    ret

; Disk extraction, MemMan discovery, TsrLoad/TsrKill command chains, and the
; embedded public-domain utilities are transient and never enter the TSR.
include 'agent/msx_memman_loader.asm'

; z80asm's ORG changes label addresses without padding the output.  Therefore
; resident_source is the loader-time source and resident_start is its runtime
; destination, while the bytes remain adjacent in the COM file.
resident_source:
    org RES_BASE
endif

resident_start:
if MSXAI_TSR_BUILD
active_transport_id:
    db 0FEh                     ; patched in the embedded TSR before extraction
resident_page:
    db 0
else
    db 0,0,0,0,0,0             ; replaced with original CP/M metadata
bdos_proxy:
    jp 0000h                    ; patched to the original BDOS destination
    db "MA",PROTO_VERSION
active_transport_id:
    db 0FFh
resident_page:
    db 0

old_bdos:
    dw 0
tpa_lowered:
    db 0
old_keyi:
    db 0,0,0,0,0
    ret                         ; CALLF hooks return immediately after byte 5
old_timi:
    db 0,0,0,0,0
    ret
endif
runtime_mode:
    db RUNTIME_RESIDENT
debug_enabled:
    db 0
debug_column:
    db 0
active_transport_flags:
    db 0
active_transport_control_level:
    db 0
run_state:
    db 0                        ; 0 monitor, 1 running, 2 paused
in_hook:
    db 0
resume_requested:
    db 0
vram_bank:
    db 0
saved_r14:
    db 0
chain_keyi:
    db 0
hook_kind:
    db 0                        ; 0=H.KEYI, 1=H.TIMI
transport_state:
    ; Opaque to the core. Offsets are private constants in the selected driver.
    ds TRANSPORT_STATE_SIZE,0
framed_mode:
    db 0
post_action_pending:
    db 0
vram_active:
    db 0
vdp_generation:
    db 0
vram_bank_count:
    db 1                        ; directly addressable 16-KiB R#14 banks
frame_reconnect_count:
    db 0
frame_flags:
    db 0
frame_sequence:
    dw 0
frame_opcode:
    db 0
frame_request_status:
    db 0
frame_length:
    dw 0
frame_crc:
    dw 0
frame_request_crc:
    dw 0
frame_parse_status:
    db 0
frame_response_status:
    db 0
frame_response_length:
    dw 0
last_response_valid:
    db 0
last_request_valid:
    db 0
last_sequence:
    dw 0
next_sequence:
    dw 0
last_opcode:
    db 0
last_request_crc:
    dw 0
last_response_status:
    db 0
last_response_length:
    dw 0
saved_context_sp:
    dw 0
hook_dispatch_sp:
    dw 0

resident_initialize:
    di
    ld hl,resident_start
    ld a,h
    ld (resident_page),a
    ; Under MSX-DOS page 0 is RAM, so read the version byte from the actual
    ; Main-ROM slot rather than from CPU address 002Dh.
    ld a,(EXPTBL)
    ld hl,MSXVER
    call RDSLT
    ld (vdp_generation),a
    call detect_vram_capacity
    call transport_bind
    call transport_init
    xor a
    ld (in_hook),a
    ld (resume_requested),a
    ld (post_action_pending),a
    ld (debug_column),a
    ld a,(runtime_mode)
    cp RUNTIME_RESIDENT
    ret nz
    ld a,1
    ld (run_state),a
    ret

resident_main:
    di
monitor_reset:
    xor a
    ld (run_state),a
    ld (in_hook),a
    ld (resume_requested),a
    ld (post_action_pending),a
main_loop:
    call receive_dispatch
    jr main_loop

; MODE is the BIOS-owned capacity descriptor documented by the MSX2 Technical
; Handbook.  Do not infer capacity from the VDP/machine generation: a V9938 may
; be wired with only 64 KiB.  On MSX1 MODE is not defined, so generation zero
; is forced to the standard single 16-KiB bank.
detect_vram_capacity:
    or a                        ; A still contains the Main-ROM version byte
    jr z,detect_vram_16k
    ld a,(MODE)
    and 06h
    cp 02h
    jr z,detect_vram_64k
    cp 04h
    jr z,detect_vram_128k
    cp 06h
    jr z,detect_vram_128k       ; extended VRAM is not CPU-addressable via R#14
detect_vram_16k:
    ld a,1
    ld (vram_bank_count),a
    ret
detect_vram_64k:
    ld a,4
    ld (vram_bank_count),a
    ret
detect_vram_128k:
    ld a,8
    ld (vram_bank_count),a
    ret

receive_dispatch:
    ld a,(framed_mode)
    or a
    jp nz,frame_receive
    call ser_get
    jp dispatch

; ---------------------------------------------------------------- H.KEYI ----
; The BIOS calls these hooks with interrupts disabled. Preserve the complete
; Z80 context. The selected driver declares whether it exclusively owns H.KEYI;
; a non-exclusive future transport chains the previous hook whenever it did not
; consume a frame. H.TIMI always chains after normal protocol work. Pause
; deliberately remains inside this saved context until resume.
if MSXAI_TSR_BUILD
resident_keyi_hook:
    push af
    xor a
    ld (hook_kind),a
    jr resident_hook_saved_af

resident_timi_hook:
    push af
    ld a,1
    ld (hook_kind),a

resident_hook_saved_af:
    push bc
    push de
    push hl
    push ix
    push iy
    ex af,af'
    push af
    ex af,af'
    exx
    push bc
    push de
    push hl
    exx

    ; MemMan entered through a BIOS hook with its dispatcher and stack in
    ; stable page-3 RAM. Keep that stack: switching page 1 cannot invalidate it.
    ld (hook_dispatch_sp),sp
    ld a,1
    ld (in_hook),a
    ; An exclusive H.KEYI transport must suppress the previous RS-232 handler
    ; even when RxRDY is not set at this exact instruction. Otherwise a byte
    ; arriving between our poll and the old handler's poll can be stolen from
    ; the framed stream. H.TIMI and future non-exclusive transports chain.
    ld a,(hook_kind)
    or a
    jr nz,hook_initial_chain
    ld a,(active_transport_flags)
    and TRANSPORT_FLAG_KEYI_EXCLUSIVE
    jr z,hook_initial_chain
    xor a
    jr hook_chain_ready
hook_initial_chain:
    ld a,1
hook_chain_ready:
    ld (chain_keyi),a
    call transport_rx_ready
    or a
    jr z,hook_done
    ld a,(hook_kind)
    or a
    jr nz,hook_dispatch_frame   ; H.TIMI must always continue its chain
    ld a,(active_transport_flags)
    and TRANSPORT_FLAG_KEYI_EXCLUSIVE
    jr z,hook_dispatch_frame
    xor a
    ld (chain_keyi),a           ; consumed exclusive H.KEYI serial traffic
hook_dispatch_frame:
    call receive_dispatch

hook_done:
    xor a
    ld (in_hook),a
    exx
    pop hl
    pop de
    pop bc
    exx
    ex af,af'
    pop af
    ex af,af'
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(chain_keyi)
    or a
    jr nz,memman_hook_continue
    pop af
    ex af,af'
    ld a,1                     ; MemMan QuitHook: suppress remaining H.KEYI
    ex af,af'
    ret
memman_hook_continue:
    pop af
    ex af,af'
    xor a                      ; MemMan QuitHook clear: call the next handler
    ex af,af'
    ret
else
resident_keyi_hook:
    push af
    ld a,(in_hook)
    or a
    jr nz,nested_keyi_return
    xor a
    ld (hook_kind),a
    jr resident_hook_saved_af
nested_keyi_return:
    ld a,(active_transport_flags)
    and TRANSPORT_FLAG_KEYI_EXCLUSIVE
    jr z,nested_keyi_chain
    pop af
    ret
nested_keyi_chain:
    pop af
    jp old_keyi
resident_timi_hook:
    push af
    ld a,(in_hook)
    or a
    jr nz,nested_timi_chain
    ld a,1
    ld (hook_kind),a
    jr resident_hook_saved_af
nested_timi_chain:
    pop af
    jp old_timi
resident_hook_saved_af:
    push bc
    push de
    push hl
    push ix
    push iy
    ex af,af'
    push af
    ex af,af'
    exx
    push bc
    push de
    push hl
    exx

    ; Keep protocol parsing and pause service off the interrupted program's
    ; stack. Only the fixed-size saved context remains on the application stack.
    ld (saved_context_sp),sp
    ld sp,hook_stack_top
    ld (hook_dispatch_sp),sp
    ld a,1
    ld (in_hook),a
    ; H.TIMI chains by default. An exclusive serial driver suppresses H.KEYI's
    ; previous handler because it could steal a byte in an RxRDY race; a future
    ; non-exclusive driver can request normal no-frame chaining instead.
    ld a,(hook_kind)
    or a
    jr nz,hook_initial_chain
    ld a,(active_transport_flags)
    and TRANSPORT_FLAG_KEYI_EXCLUSIVE
    jr z,hook_initial_chain
    xor a
    jr hook_chain_ready
hook_initial_chain:
    ld a,1
hook_chain_ready:
    ld (chain_keyi),a
    call transport_rx_ready
    or a
    jr z,hook_done
    ; H.TIMI is shared by unrelated resident software and must still chain after
    ; servicing a frame. Only an exclusive H.KEYI owner suppresses its predecessor
    ; so that the old serial handler cannot consume bytes from the same frame.
    ld a,(hook_kind)
    or a
    jr nz,hook_dispatch_frame
    xor a
    ld (chain_keyi),a          ; old RS-232 hook must not consume next frame
hook_dispatch_frame:
    call receive_dispatch
hook_done:
    xor a
    ld (in_hook),a

    ld sp,(saved_context_sp)
    exx
    pop hl
    pop de
    pop bc
    exx
    ex af,af'
    pop af
    ex af,af'
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(chain_keyi)
    or a
    jr z,hook_return_direct
    ld a,(hook_kind)
    or a
    jr nz,hook_chain_timi
    pop af
    jp old_keyi
hook_chain_timi:
    pop af
    jp old_timi
hook_return_direct:
    pop af
    ret
endif

; DEBUG ON is deliberately a foreground-monitor feature. BIOS output from an
; interrupt hook could be re-entrant and would corrupt the application image,
; so hook-side commands remain silent even when diagnostics are enabled.
debug_trace_command:            ; A = raw/v3 opcode, all registers preserved
    push af
    push bc
    push de
    push hl
    ld b,a
    ld a,(debug_enabled)
    or a
    jr z,debug_trace_done
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jr nz,debug_trace_done
    ld a,(in_hook)
    or a
    jr nz,debug_trace_done
    push bc                     ; preserve the opcode across BIOS output calls
    ld a,13
    call debug_putchar
    ld a,10
    call debug_putchar
    ld a,'['
    call debug_putchar
    pop bc
    ld a,b
    rrca
    rrca
    rrca
    rrca
    and 00Fh
    push bc                     ; preserve the opcode while formatting its nibble
    call debug_trace_hex_nibble
    pop bc
    ld a,b
    and 00Fh
    call debug_trace_hex_nibble
    ld a,']'
    call debug_putchar
debug_trace_done:
    pop hl
    pop de
    pop bc
    pop af
    ret

debug_trace_hex_nibble:         ; A=0..15, emits one printable uppercase digit
    add a,'0'
    cp '9' + 1
    jr c,debug_trace_hex_emit
    add a,'A' - '9' - 1
debug_trace_hex_emit:
    jp debug_putchar

debug_putchar:                  ; A=character, foreground MSX-DOS only
if MSXAI_TSR_BUILD
    ; DEBUG ON cannot be selected for a MemMan TSR, so this path is
    ; unreachable and the transient loader's BDOS proxy is intentionally absent.
    ret
else
    ; Page 0 is RAM under MSX-DOS, so a direct call to the Main-ROM CHPUT
    ; address (00A2h) would execute the command-tail/FCB area. Route console
    ; output through the resident BDOS proxy instead. Preserve every index and
    ; working register that the trace formatter may still be using.
    push bc
    push de
    push hl
    push ix
    push iy
    ld e,a
    ld a,1
    ld (in_hook),a             ; do not let BDOS/BIOS re-enter the protocol hook
    ld c,2                     ; CP/M-compatible direct console output
    call bdos_proxy
    di                         ; the foreground receive loop deliberately owns I/O
    xor a
    ld (in_hook),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ret
endif

; --------------------------------------------------------------- protocol ----
dispatch:                       ; A = command
    call debug_trace_command
    cp '?'
    jp z,cmd_hello
    cp 'q'
    jp z,cmd_status
    cp 'r'
    jp z,cmd_ram_read
    cp 'p'
    jp z,cmd_ram_write
    cp 'v'
    jp z,cmd_vram_read
    cp 'w'
    jp z,cmd_vram_write
    cp 'c'
    jp z,cmd_call
    cp 'j'
    jp z,cmd_run
    cp 's'
    jp z,cmd_pause
    cp 'g'
    jp z,cmd_resume
    cp 'k'
    jp z,cmd_stop
    cp 'i'
    jp z,cmd_io_read
    cp 'o'
    jp z,cmd_io_write
    cp 'l'
    jp z,cmd_slot_select
    cp 'm'
    jp z,cmd_mapper_select
    cp 'F'
    jp z,cmd_frame_enable
    cp 'z'
    jp z,cmd_uninstall
    ld a,'E'
    call ser_put
    ld a,1                     ; unknown command
    jp ser_put

cmd_hello:
    ld a,'M'
    call ser_put
    ld a,PROTO_VERSION
    call ser_put
    call current_capabilities
    call ser_put
    ld a,(resident_page)
    jp ser_put

current_capabilities:
    ld a,CAPABILITIES
    ld b,a
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    ld a,b
    ret z
    ; Mapping changes made by a MemMan hook are either restored on inter-slot
    ; return (slots) or unsafe for the interrupted page-0 code (mapper).
    and 0FFh - CAPABILITY_RUN - CAPABILITY_MAPPING
    ret

cmd_status:
    ld a,'K'
    call ser_put
    ld a,(run_state)
    call ser_put
    ld a,PROTO_VERSION
    jp ser_put

cmd_ram_read:
    call get_addr
    call ser_get
    ld b,a
    or a
    ret z
    call raw_range_wraps
    jr c,ram_read_zero_fill
if MSXAI_TSR_BUILD
    push bc
    ld c,b
    ld b,0
    call tsr_page1_overlap
    pop bc
    jr c,ram_read_zero_fill
    ld a,h
    cp 040h
    jr c,ram_read_page0_loop
endif
ram_read_loop:
    ld a,(hl)
    call ser_put
    inc hl
    djnz ram_read_loop
    ret
if MSXAI_TSR_BUILD
ram_read_page0_loop:
    push bc
    ld a,(RAMAD0)
    call RDSLT                 ; read the interrupted DOS RAM, not BIOS ROM
    pop bc
    call ser_put
    inc hl
    djnz ram_read_page0_loop
    ret
endif
ram_read_zero_fill:
    ; Raw v2 has no read-status byte. Preserve framing with a fixed-size zero
    ; fill for invalid ranges instead of wrapping or exposing the private TSR.
    xor a
ram_read_zero_fill_loop:
    call ser_put
    djnz ram_read_zero_fill_loop
    ret

cmd_ram_write:
    call get_addr
    call ser_get
    ld b,a
    call raw_range_wraps
    jr c,ram_write_reject
if MSXAI_TSR_BUILD
    ; MemMan maps the TSR segment into all of page 1 while this hook runs.
    ; Never mistake that private segment for the interrupted program's RAM.
    push bc
    ld c,b
    ld b,0
    call tsr_page1_overlap
    pop bc
    jr c,ram_write_reject
    ld a,h
    cp 040h
    jr c,ram_write_page0
else
    ; The resident, its hook stack and DOS live at RES_BASE and above. Enforce
    ; that boundary in the agent too, not only in the Python client.
    ld a,h
    cp RES_BASE >> 8
    jr nc,ram_write_reject
    push hl
    ld d,0
    ld e,b
    add hl,de
    jr c,ram_write_reject_pop
    ld de,RES_BASE
    or a
    sbc hl,de
    pop hl
    jr c,ram_write_allowed
    jr z,ram_write_allowed
    jr ram_write_reject
ram_write_reject_pop:
    pop hl
endif
ram_write_reject:
    ld a,b
    or a
    jr z,ram_write_reject_reply
ram_write_discard_loop:
    call ser_get
    djnz ram_write_discard_loop
ram_write_reject_reply:
    ld a,'E'
    call ser_put
    ld a,3                     ; protected RAM range
    jp ser_put
ram_write_allowed:
    ld a,b
    or a
    jr z,ram_write_ack
ram_write_loop:
    call ser_get
    ld (hl),a
    inc hl
    djnz ram_write_loop
ram_write_ack:
    ld a,'K'
    jp ser_put
if MSXAI_TSR_BUILD
ram_write_page0:
    ld a,b
    or a
    jr z,ram_write_ack
ram_write_page0_loop:
    call ser_get
    ld e,a
    push bc
    ld a,(RAMAD0)
    call WRSLT                 ; write the RAM slot hidden by the BIOS ISR
    pop bc
    inc hl
    djnz ram_write_page0_loop
    jr ram_write_ack
endif

cmd_vram_read:
    call get_vram_frame        ; bank, HL, B=count
    ld a,b
    or a
    ret z
    call set_vram_read
vram_read_loop:
    in a,(098h)
    call ser_put
    djnz vram_read_loop
    jp restore_r14

cmd_vram_write:
    call get_vram_frame
    ld a,b
    or a
    jr z,vram_write_ack
    call set_vram_write
vram_write_loop:
    call ser_get
    out (098h),a
    djnz vram_write_loop
    call restore_r14
vram_write_ack:
    ld a,'K'
    jp ser_put

cmd_call:
    call get_addr
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jp nz,error_busy
    ld a,(in_hook)
    or a
    jp nz,error_busy           ; never execute arbitrary code inside an ISR
    call jump_hl
    ld a,'K'
    jp ser_put

cmd_run:
    call get_addr
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jp nz,error_busy
    ld a,(in_hook)
    or a
    jp nz,error_busy           ; launch only from the monitor loop
    push hl
    ld a,'K'                   ; acknowledge before the application takes over
    call ser_put
    ld a,1
    ld (run_state),a
    pop hl
    ei
    call jump_hl
    di
    xor a
    ld (run_state),a
    ret

cmd_pause:
    ld a,'K'
    call ser_put
    ld a,(in_hook)
    or a
    ret z                      ; monitor loop is already effectively paused
    ld a,(run_state)
    cp 2
    ret z
    ld a,2
    ld (run_state),a
    xor a
    ld (resume_requested),a
pause_service_loop:
    call receive_dispatch
    ld a,(resume_requested)
    or a
    jr z,pause_service_loop
    ld a,1
    ld (run_state),a
    ret

cmd_resume:
    ld a,1
    ld (resume_requested),a
    ld a,'K'
    jp ser_put

cmd_stop:
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jp nz,error_busy
    ld a,'K'
    call ser_put
    ld a,(in_hook)
    or a
    ret z
    ; Deliberately abandon the interrupted application/BIOS stack.  The BDOS
    ; proxy and hooks remain installed, so a new build can be uploaded/run.
    di
    ld sp,RES_BASE
    jp monitor_reset

; ------------------------------------------------------ hardware control ----
; Dynamic IN/OUT is deliberately exposed: this is the low-level escape hatch
; needed while bringing up unknown real hardware.  Writing the active UART or
; VDP ports can naturally disrupt the session/display, so normal host APIs use
; the higher-level RAM/VRAM operations unless explicitly asked for raw I/O.
cmd_io_read:
    call ser_get
    ld c,a
    ld b,0
    in a,(c)
    jp ser_put

cmd_io_write:
    call ser_get
    ld c,a
    call ser_get
    ld b,0
    out (c),a
    ld a,'K'
    jp ser_put

raw_range_wraps:              ; HL=start, B=size; carry iff end exceeds 10000h
    push hl
    ld d,0
    ld e,b
    add hl,de
    jr nc,raw_range_valid
    ld a,h                    ; carry with a zero result is the exact valid end
    or l
    jr z,raw_range_valid
    pop hl
    scf
    ret
raw_range_valid:
    pop hl
    or a
    ret

cmd_slot_select:
    call ser_get
    ld b,a                     ; consume both operands before any rejection
    call ser_get
    ld d,a
    ld a,b
if MSXAI_TSR_BUILD
    jp error_protected_page    ; mapping is monitor-only under MemMan
else
    cp 2
    jr nc,error_protected_page
endif
    rrca                       ; 0,1,2 -> H=00h,40h,80h
    rrca
    ld h,a
    ld a,d                     ; BIOS slot id, including expanded-slot bits
    call 0024h                 ; ENASLT(A=slot,H=page address)
    ld a,'K'
    jp ser_put

cmd_mapper_select:
    call ser_get               ; mapper ports FCh..FFh correspond to pages
    ld b,a
    call ser_get
    ld d,a
    ld a,b
if MSXAI_TSR_BUILD
    jp error_protected_page    ; never remap the interrupted program's page 0
else
    cp 2
    jr nc,error_protected_page
endif
    add a,0FCh
    ld c,a
    ld a,d
    out (c),a
    ld a,'K'
    jp ser_put

error_protected_page:
    ld a,'E'
    call ser_put
    ld a,3                     ; protected resident page / invalid mapping
    jp ser_put

if MSXAI_TSR_BUILD
; Input HL=start and BC=length. Carry is set when the half-open range overlaps
; page 1 (4000h..7FFFh), which contains this TSR during a MemMan call.
tsr_page1_overlap:
    ld a,b
    or c
    jr z,tsr_page1_clear
    ld a,h
    cp 040h
    jr c,tsr_page1_starts_below
    cp 080h
    jr c,tsr_page1_overlaps
tsr_page1_clear:
    or a                       ; clear carry
    ret
tsr_page1_starts_below:
    push hl
    add hl,bc
    ld a,h
    cp 040h
    jr c,tsr_page1_clear_pop
    jr nz,tsr_page1_overlap_pop
    ld a,l
    or a
    jr z,tsr_page1_clear_pop   ; an exclusive end of 4000h is safe
tsr_page1_overlap_pop:
    pop hl
tsr_page1_overlaps:
    scf
    ret
tsr_page1_clear_pop:
    pop hl
    or a
    ret
endif

cmd_frame_enable:
    ; Raw v2 remains the bootstrap/fallback.  After this four-byte reply both
    ; peers speak framed v3 until the connection/monitor is restarted.
    ld a,'K'
    call ser_put
    ld a,FRAMED_VERSION
    call ser_put
    ld a,FRAMED_MAX & 0FFh
    call ser_put
    ld a,FRAMED_MAX >> 8
    call ser_put
    ld a,1
    ld (framed_mode),a
    xor a
    ld (last_response_valid),a
    ld (last_request_valid),a
    ld (frame_reconnect_count),a
    ld hl,0
    ld (next_sequence),hl
    ret

cmd_uninstall:
if MSXAI_TSR_BUILD
    ; A MemMan TSR is removed only through the foreground `/UNINSTALL` loader,
    ; which lets TsrKill detach hooks before invoking tsr_kill.
    jp error_busy
else
    ld a,(in_hook)
    or a
    jr nz,error_busy
    ; Only the top resident may remove itself. A newer TSR may depend on our
    ; hook or reduced TPA and must not be cut out from underneath.
    call current_bdos_matches
    jr nz,error_ownership
    call current_hooks_match
    jr nz,error_ownership
    ld a,'K'
    call ser_put
    di
    ld hl,old_keyi
    ld de,H_KEYI
    ld bc,5
    ldir
    ld hl,old_timi
    ld de,H_TIMI
    ld bc,5
    ldir
    ld hl,(old_bdos)
    ld (0006h),hl
    call transport_restore
    ei
    ld c,0
    jp 0005h
endif

error_busy:
    ld a,'E'
    call ser_put
    ld a,2                     ; command invalid in the current state
    jp ser_put

error_ownership:
    ld a,'E'
    call ser_put
    ld a,4                     ; monitor is no longer top of the resident chain
    jp ser_put

if MSXAI_TSR_BUILD
else
current_bdos_matches:
    ld hl,(0006h)
    ld a,(tpa_lowered)
    or a
    ld de,old_bdos
    jr z,current_bdos_compare_old
    ld de,bdos_proxy
    jr current_bdos_compare
current_bdos_compare_old:
    ld de,(old_bdos)
current_bdos_compare:
    or a
    sbc hl,de
    ret

current_hooks_match:
    ld a,(H_KEYI)
    cp 0C3h
    ret nz
    ld hl,(H_KEYI + 1)
    ld de,resident_keyi_hook
    or a
    sbc hl,de
    ret nz
    ld a,(H_TIMI)
    cp 0C3h
    ret nz
    ld hl,(H_TIMI + 1)
    ld de,resident_timi_hook
    or a
    sbc hl,de
    ret
endif

jump_hl:
    jp (hl)

; -------------------------------------------------------- framed protocol ----
; v3 is transport-independent and stream-safe:
;   "MX",version,type,flags,seq16,opcode,status,len16,payload,crc16
; All words are little-endian. Requests are first received and CRC-checked in
; full before any state-changing command runs. The most recent response is
; cached, so a retry with the same sequence is idempotent. New requests must
; advance monotonically; an old sequence can therefore never execute again.
frame_receive:
frame_seek_magic:
    call ser_get
    cp RECONNECT_BYTE
    jr z,frame_reconnect_byte
    push af
    xor a
    ld (frame_reconnect_count),a
    pop af
    cp 'M'
    jr nz,frame_seek_magic
frame_have_magic_m:
    call ser_get
    cp 'X'
    jr z,frame_magic_found
    cp 'M'
    jr z,frame_have_magic_m
    jr frame_seek_magic

frame_reconnect_byte:
    ld a,(frame_reconnect_count)
    inc a
    cp RECONNECT_LENGTH
    jr z,frame_rebootstrap
    ld (frame_reconnect_count),a
    jr frame_seek_magic

frame_rebootstrap:
    ; A TCP/serial peer can disappear while the resident monitor keeps running.
    ; The eight-byte marker above is an out-of-band resynchronization escape so
    ; a new host can negotiate v3 without resetting the MSX. Its false-positive
    ; probability in independent byte noise is 1/2^64.
    xor a
    ld (framed_mode),a
    ld (last_response_valid),a
    ld (last_request_valid),a
    ld (frame_reconnect_count),a
    ld hl,0
    ld (next_sequence),hl
    jp cmd_hello

frame_magic_found:
    call frame_crc_reset
    ld a,'M'
    call frame_crc_update
    ld a,'X'
    call frame_crc_update
    xor a
    ld (frame_parse_status),a

    call frame_get_crc
    cp FRAMED_VERSION
    jr z,frame_version_ok
    ld a,FRAME_UNSUPPORTED
    ld (frame_parse_status),a
frame_version_ok:
    call frame_get_crc
    cp FRAME_REQUEST
    jr z,frame_type_ok
    ld a,FRAME_BAD_ARG
    ld (frame_parse_status),a
frame_type_ok:
    call frame_get_crc
    ld (frame_flags),a
    call frame_get_crc
    ld l,a
    call frame_get_crc
    ld h,a
    ld (frame_sequence),hl
    call frame_get_crc
    ld (frame_opcode),a
    call frame_get_crc
    ld (frame_request_status),a
    or a
    jr z,frame_request_status_ok
    ld a,FRAME_BAD_ARG
    ld (frame_parse_status),a
frame_request_status_ok:
    call frame_get_crc
    ld l,a
    call frame_get_crc
    ld h,a
    ld (frame_length),hl

    ; Never trust an oversized declared length: consuming a corrupted 65535
    ; byte length would wedge the foreground monitor. Reject immediately; the
    ; next invocation scans the remaining stream for a fresh magic marker.
    ld de,(frame_length)
    ld hl,FRAMED_MAX
    or a
    sbc hl,de
    jr nc,frame_payload_store
    ld a,FRAME_RANGE
    jp frame_reply_error_uncached

frame_payload_store:
    ld bc,(frame_length)
    ld hl,frame_request_buffer
frame_payload_store_loop:
    ld a,b
    or c
    jr z,frame_payload_complete
    call frame_get_crc
    ld (hl),a
    inc hl
    dec bc
    jr frame_payload_store_loop

frame_payload_complete:
    ld hl,(frame_crc)
    ld (frame_request_crc),hl
    call ser_get
    ld e,a
    call ser_get
    ld d,a
    ld hl,(frame_request_crc)
    or a
    sbc hl,de
    jr z,frame_crc_valid
    ld a,FRAME_BAD_CRC
    jp frame_reply_error_uncached

frame_crc_valid:
    ld a,(frame_parse_status)
    or a
    jp nz,frame_reply_error_uncached

    ; Only the immediately expected sequence may dispatch. The previous
    ; sequence remains a retry candidate, but it is never dispatched twice:
    ; either its cached result is replayed or BUSY is returned when transmission
    ; failed before the result became replayable. This is at-most-once execution
    ; for every accepted sequence while still allowing the 16-bit counter to
    ; wrap after FFFFh.
    ld a,(last_request_valid)
    or a
    jr z,frame_expect_new_sequence
    ld hl,(last_sequence)
    ld de,(frame_sequence)
    or a
    sbc hl,de
    jr nz,frame_expect_new_sequence
    ld a,(last_opcode)
    ld b,a
    ld a,(frame_opcode)
    cp b
    jr nz,frame_duplicate_conflict
    ld hl,(last_request_crc)
    ld de,(frame_request_crc)
    or a
    sbc hl,de
    jr nz,frame_duplicate_conflict
    ld a,(last_response_valid)
    or a
    jr z,frame_duplicate_incomplete
    ld a,(last_response_status)
    ld (frame_response_status),a
    ld hl,(last_response_length)
    ld (frame_response_length),hl
    ld a,h
    or a
    jp nz,frame_dispatch       ; bulk RAM/VRAM reads are safe to recompute
    ld a,l
    cp 17
    jp nc,frame_dispatch
    or a
    jp z,frame_emit_response
    ld c,a
    ld b,0
    ld hl,last_response_small
    ld de,frame_response_buffer
    ldir
    jp frame_emit_response

frame_duplicate_incomplete:
    ld a,FRAME_BUSY
    jp frame_reply_error_uncached

frame_expect_new_sequence:
    ld hl,(next_sequence)
    ld de,(frame_sequence)
    or a
    sbc hl,de
    jr nz,frame_duplicate_conflict

    ; Record identity before dispatch. Even if a hook-side response times out,
    ; this sequence cannot be reused to execute either the same or a different
    ; state-changing request.
    ld hl,(frame_sequence)
    ld (last_sequence),hl
    inc hl
    ld (next_sequence),hl
    ld a,(frame_opcode)
    ld (last_opcode),a
    ld hl,(frame_request_crc)
    ld (last_request_crc),hl
    ld a,1
    ld (last_request_valid),a
    xor a
    ld (last_response_valid),a
    jr frame_dispatch

frame_duplicate_conflict:
    ld a,FRAME_BAD_ARG
    jp frame_reply_error_uncached

frame_dispatch:
    ld a,(frame_opcode)
    call debug_trace_command
    cp '?'
    jp z,frame_cmd_hello
    cp 'q'
    jp z,frame_cmd_status
    cp 'r'
    jp z,frame_cmd_ram_read
    cp 'p'
    jp z,frame_cmd_ram_write
    cp 'v'
    jp z,frame_cmd_vram_read
    cp 'w'
    jp z,frame_cmd_vram_write
    cp 'c'
    jp z,frame_cmd_call
    cp 'j'
    jp z,frame_cmd_run
    cp 's'
    jp z,frame_cmd_pause
    cp 'g'
    jp z,frame_cmd_resume
    cp 'k'
    jp z,frame_cmd_stop
    cp 'i'
    jp z,frame_cmd_io_read
    cp 'o'
    jp z,frame_cmd_io_write
    cp 'l'
    jp z,frame_cmd_slot_select
    cp 'm'
    jp z,frame_cmd_mapper_select
    ld a,FRAME_BAD_OPCODE
    jp frame_reply_error_cached

frame_require_length:          ; DE=expected, Z when exact
    ld hl,(frame_length)
    or a
    sbc hl,de
    ret

frame_reply_ok:
    xor a
    ld (frame_response_status),a
    ld hl,0
    ld (frame_response_length),hl
    jp frame_cache_and_send

frame_reply_bad_arg:
    ld a,FRAME_BAD_ARG
    jr frame_reply_error_cached
frame_reply_range:
    ld a,FRAME_RANGE
    jr frame_reply_error_cached
frame_reply_bad_state:
    ld a,FRAME_BAD_STATE
    jr frame_reply_error_cached
frame_reply_unsupported:
    ld a,FRAME_UNSUPPORTED
frame_reply_error_cached:
    push af
    ld hl,0
    ld (frame_response_length),hl
    pop af
    ld (frame_response_status),a
    jp frame_cache_and_send

frame_reply_error_uncached:
    ld (frame_response_status),a
    ld hl,0
    ld (frame_response_length),hl
    jp frame_emit_response

frame_cache_and_send:
    ld a,(frame_response_status)
    ld (last_response_status),a
    ld hl,(frame_response_length)
    ld (last_response_length),hl
    ld a,h
    or a
    jr nz,frame_cache_payload_done
    ld a,l
    cp 17
    jr nc,frame_cache_payload_done
    or a
    jr z,frame_cache_payload_done
    ld c,a
    ld b,0
    ld hl,frame_response_buffer
    ld de,last_response_small
    ldir
frame_cache_payload_done:
    ld a,1
    ld (last_response_valid),a

frame_emit_response:
    call frame_crc_reset
    ld a,'M'
    call frame_send_crc_byte
    ld a,'X'
    call frame_send_crc_byte
    ld a,FRAMED_VERSION
    call frame_send_crc_byte
    ld a,FRAME_RESPONSE
    call frame_send_crc_byte
    ld a,(frame_response_status)
    or a
    jr z,frame_emit_no_error_flag
    ld a,FRAME_FLAG_ERROR
frame_emit_no_error_flag:
    call frame_send_crc_byte
    ld hl,(frame_sequence)
    ld a,l
    call frame_send_crc_byte
    ld a,h
    call frame_send_crc_byte
    ld a,(frame_opcode)
    call frame_send_crc_byte
    ld a,(frame_response_status)
    call frame_send_crc_byte
    ld hl,(frame_response_length)
    ld a,l
    call frame_send_crc_byte
    ld a,h
    call frame_send_crc_byte
    ld bc,(frame_response_length)
    ld hl,frame_response_buffer
frame_emit_payload_loop:
    ld a,b
    or c
    jr z,frame_emit_crc
    ld a,(hl)
    call frame_send_crc_byte
    inc hl
    dec bc
    jr frame_emit_payload_loop
frame_emit_crc:
    ld hl,(frame_crc)
    ld a,l
    call ser_put
    ld a,h
    jp ser_put

frame_crc_reset:
    ld hl,0FFFFh
    ld (frame_crc),hl
    ret

frame_get_crc:
    call ser_get
    push af
    call frame_crc_update
    pop af
    ret

frame_send_crc_byte:
    push af
    call frame_crc_update
    pop af
    jp ser_put

frame_crc_update:              ; table-driven CRC-16/CCITT-FALSE, A=byte
    ; The hook already preserves the alternate register set. Using it here
    ; avoids six stack operations per byte; contiguous split tables avoid the
    ; index multiply used by the interleaved word table. The high table is one
    ; page after the low table, so the second lookup is just INC H.
    exx
    ld hl,(frame_crc)
    xor h
    ld e,a
    ld d,0
    ld b,l                     ; old CRC low byte
    ld hl,frame_crc_table_low
    add hl,de
    ld a,(hl)
    ld c,a
    inc h                      ; high table is exactly 256 bytes after low
    ld a,(hl)
    xor b                      ; high = old low XOR table high
    ld h,a
    ld l,c                     ; low = table low
    ld (frame_crc),hl
    exx
    ret

; With the split lookup and alternate registers, CRC work is roughly 110
; T-states per byte. The receive loop is substantially faster than the original
; stacked word-table implementation; the 16C550 driver still keeps UART AFE
; enabled as the mandatory guard against interrupt latency and parser stalls.
frame_crc_table_low:
    db 000h,021h,042h,063h,084h,0A5h,0C6h,0E7h,008h,029h,04Ah,06Bh,08Ch,0ADh,0CEh,0EFh
    db 031h,010h,073h,052h,0B5h,094h,0F7h,0D6h,039h,018h,07Bh,05Ah,0BDh,09Ch,0FFh,0DEh
    db 062h,043h,020h,001h,0E6h,0C7h,0A4h,085h,06Ah,04Bh,028h,009h,0EEh,0CFh,0ACh,08Dh
    db 053h,072h,011h,030h,0D7h,0F6h,095h,0B4h,05Bh,07Ah,019h,038h,0DFh,0FEh,09Dh,0BCh
    db 0C4h,0E5h,086h,0A7h,040h,061h,002h,023h,0CCh,0EDh,08Eh,0AFh,048h,069h,00Ah,02Bh
    db 0F5h,0D4h,0B7h,096h,071h,050h,033h,012h,0FDh,0DCh,0BFh,09Eh,079h,058h,03Bh,01Ah
    db 0A6h,087h,0E4h,0C5h,022h,003h,060h,041h,0AEh,08Fh,0ECh,0CDh,02Ah,00Bh,068h,049h
    db 097h,0B6h,0D5h,0F4h,013h,032h,051h,070h,09Fh,0BEh,0DDh,0FCh,01Bh,03Ah,059h,078h
    db 088h,0A9h,0CAh,0EBh,00Ch,02Dh,04Eh,06Fh,080h,0A1h,0C2h,0E3h,004h,025h,046h,067h
    db 0B9h,098h,0FBh,0DAh,03Dh,01Ch,07Fh,05Eh,0B1h,090h,0F3h,0D2h,035h,014h,077h,056h
    db 0EAh,0CBh,0A8h,089h,06Eh,04Fh,02Ch,00Dh,0E2h,0C3h,0A0h,081h,066h,047h,024h,005h
    db 0DBh,0FAh,099h,0B8h,05Fh,07Eh,01Dh,03Ch,0D3h,0F2h,091h,0B0h,057h,076h,015h,034h
    db 04Ch,06Dh,00Eh,02Fh,0C8h,0E9h,08Ah,0ABh,044h,065h,006h,027h,0C0h,0E1h,082h,0A3h
    db 07Dh,05Ch,03Fh,01Eh,0F9h,0D8h,0BBh,09Ah,075h,054h,037h,016h,0F1h,0D0h,0B3h,092h
    db 02Eh,00Fh,06Ch,04Dh,0AAh,08Bh,0E8h,0C9h,026h,007h,064h,045h,0A2h,083h,0E0h,0C1h
    db 01Fh,03Eh,05Dh,07Ch,09Bh,0BAh,0D9h,0F8h,017h,036h,055h,074h,093h,0B2h,0D1h,0F0h
frame_crc_table_high:
    db 000h,010h,020h,030h,040h,050h,060h,070h,081h,091h,0A1h,0B1h,0C1h,0D1h,0E1h,0F1h
    db 012h,002h,032h,022h,052h,042h,072h,062h,093h,083h,0B3h,0A3h,0D3h,0C3h,0F3h,0E3h
    db 024h,034h,004h,014h,064h,074h,044h,054h,0A5h,0B5h,085h,095h,0E5h,0F5h,0C5h,0D5h
    db 036h,026h,016h,006h,076h,066h,056h,046h,0B7h,0A7h,097h,087h,0F7h,0E7h,0D7h,0C7h
    db 048h,058h,068h,078h,008h,018h,028h,038h,0C9h,0D9h,0E9h,0F9h,089h,099h,0A9h,0B9h
    db 05Ah,04Ah,07Ah,06Ah,01Ah,00Ah,03Ah,02Ah,0DBh,0CBh,0FBh,0EBh,09Bh,08Bh,0BBh,0ABh
    db 06Ch,07Ch,04Ch,05Ch,02Ch,03Ch,00Ch,01Ch,0EDh,0FDh,0CDh,0DDh,0ADh,0BDh,08Dh,09Dh
    db 07Eh,06Eh,05Eh,04Eh,03Eh,02Eh,01Eh,00Eh,0FFh,0EFh,0DFh,0CFh,0BFh,0AFh,09Fh,08Fh
    db 091h,081h,0B1h,0A1h,0D1h,0C1h,0F1h,0E1h,010h,000h,030h,020h,050h,040h,070h,060h
    db 083h,093h,0A3h,0B3h,0C3h,0D3h,0E3h,0F3h,002h,012h,022h,032h,042h,052h,062h,072h
    db 0B5h,0A5h,095h,085h,0F5h,0E5h,0D5h,0C5h,034h,024h,014h,004h,074h,064h,054h,044h
    db 0A7h,0B7h,087h,097h,0E7h,0F7h,0C7h,0D7h,026h,036h,006h,016h,066h,076h,046h,056h
    db 0D9h,0C9h,0F9h,0E9h,099h,089h,0B9h,0A9h,058h,048h,078h,068h,018h,008h,038h,028h
    db 0CBh,0DBh,0EBh,0FBh,08Bh,09Bh,0ABh,0BBh,04Ah,05Ah,06Ah,07Ah,00Ah,01Ah,02Ah,03Ah
    db 0FDh,0EDh,0DDh,0CDh,0BDh,0ADh,09Dh,08Dh,07Ch,06Ch,05Ch,04Ch,03Ch,02Ch,01Ch,00Ch
    db 0EFh,0FFh,0CFh,0DFh,0AFh,0BFh,08Fh,09Fh,06Eh,07Eh,04Eh,05Eh,02Eh,03Eh,00Eh,01Eh

; ----------------------------------------------------- framed commands ----
frame_cmd_hello:
    ld de,0
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld hl,frame_response_buffer
    ld (hl),FRAMED_VERSION
    inc hl
    call current_capabilities
    ld (hl),a
    inc hl
    ld a,(resident_page)
    ld (hl),a
    inc hl
    ld a,(active_transport_id)
    ld (hl),a
    inc hl
    ld (hl),FRAMED_MAX & 0FFh
    inc hl
    ld (hl),FRAMED_MAX >> 8
    inc hl
    ld a,(active_transport_control_level)
    ld (hl),a
    inc hl
    ld a,(debug_enabled)
    ld (hl),a
    inc hl
    ld a,(vdp_generation)
    ld (hl),a
    inc hl
    ld a,(vram_bank_count)
    ld (hl),a                  ; directly addressable 16-KiB banks
    inc hl
    xor a
    ld (hl),a                  ; capacity in bytes, little-endian u24
    inc hl
    ld a,(vram_bank_count)
    and 03h
    rrca
    rrca                       ; (banks & 3) << 6
    ld (hl),a
    inc hl
    ld a,(vram_bank_count)
    srl a
    srl a                      ; banks >> 2
    ld (hl),a
    inc hl
    ld a,(runtime_mode)
    ld (hl),a
    ld hl,14
    ld (frame_response_length),hl
    xor a
    ld (frame_response_status),a
    jp frame_cache_and_send

frame_cmd_status:
    ld de,0
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld hl,frame_response_buffer
    ld a,(run_state)
    ld (hl),a
    inc hl
    ld (hl),FRAMED_VERSION
    ld hl,2
    ld (frame_response_length),hl
    xor a
    ld (frame_response_status),a
    jp frame_cache_and_send

frame_cmd_ram_read:
    ld de,4
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld hl,(frame_request_buffer)
    ld de,(frame_request_buffer + 2)
    push hl
    ld hl,FRAMED_MAX
    or a
    sbc hl,de
    pop hl
    jp c,frame_reply_range
    ld a,d
    or e
    jr z,frame_ram_read_empty
    ld (frame_response_length),de
    push hl
    add hl,de
    jr nc,frame_ram_read_range_ok
    ld a,h
    or l
    jr nz,frame_ram_read_range_bad_pop
frame_ram_read_range_ok:
    pop hl
    push de
    pop bc
if MSXAI_TSR_BUILD
    call tsr_page1_overlap
    jp c,frame_reply_range
    ld a,h
    cp 040h
    jr c,frame_ram_read_page0
endif
    ld de,frame_response_buffer
    ; Request and response share resident storage. A diagnostic read may itself
    ; overlap that buffer, so use memmove direction rather than blindly copying
    ; forward and feeding freshly written bytes back into the source stream.
    push hl                     ; preserve source while comparing source/dest
    or a
    sbc hl,de
    pop hl
    jr nc,frame_ram_read_ldir  ; source >= destination: forward is safe
    push hl
    add hl,bc                  ; exclusive source end
    jr c,frame_ram_read_lddr
    or a
    sbc hl,de
    jr c,frame_ram_read_ldir_pop
    jr z,frame_ram_read_ldir_pop
frame_ram_read_lddr:
    pop hl                     ; source start
    add hl,bc
    dec hl                     ; source end
    ex de,hl                   ; DE=source end, HL=destination start
    add hl,bc
    dec hl
    ex de,hl                   ; HL=source end, DE=destination end
    lddr
    jr frame_ram_read_copied
frame_ram_read_ldir_pop:
    pop hl
frame_ram_read_ldir:
    ldir
frame_ram_read_copied:
    xor a
    ld (frame_response_status),a
    jp frame_cache_and_send
if MSXAI_TSR_BUILD
frame_ram_read_page0:
    ld de,frame_response_buffer
frame_ram_read_page0_loop:
    push bc
    push de
    ld a,(RAMAD0)
    call RDSLT
    pop de
    pop bc
    ld (de),a
    inc hl
    inc de
    dec bc
    ld a,b
    or c
    jr nz,frame_ram_read_page0_loop
    jr frame_ram_read_copied
endif
frame_ram_read_range_bad_pop:
    pop hl
    jp frame_reply_range
frame_ram_read_empty:
    jp frame_reply_ok

frame_cmd_ram_write:
    ld hl,(frame_length)
    ld de,2
    or a
    sbc hl,de
    jp c,frame_reply_bad_arg
    ld bc,(frame_length)
    dec bc
    dec bc                     ; BC=data length
    ld hl,(frame_request_buffer)
if MSXAI_TSR_BUILD
    call tsr_page1_overlap
    jp c,frame_reply_range
    push hl
    add hl,bc
    jr nc,frame_ram_write_range_ok
    ld a,h
    or l
    jr nz,frame_ram_write_range_bad_pop
    jr frame_ram_write_range_ok
else
    ld a,h
    cp RES_BASE >> 8
    jp nc,frame_reply_range
    push hl
    add hl,bc
    jr c,frame_ram_write_range_bad_pop
    ld de,RES_BASE
    or a
    sbc hl,de
    jr c,frame_ram_write_range_ok
    jr z,frame_ram_write_range_ok
endif
frame_ram_write_range_bad_pop:
    pop hl
    jp frame_reply_range
frame_ram_write_range_ok:
    pop de                     ; destination
    ld a,b
    or c
    jp z,frame_reply_ok
if MSXAI_TSR_BUILD
    ld a,d
    cp 040h
    jr c,frame_ram_write_page0
endif
    ld hl,frame_request_buffer + 2
    ldir
    jp frame_reply_ok
if MSXAI_TSR_BUILD
frame_ram_write_page0:
    ex de,hl                   ; HL=RAM destination in page 0
    ld ix,frame_request_buffer + 2
frame_ram_write_page0_loop:
    ld e,(ix + 0)
    push bc
    ld a,(RAMAD0)
    call WRSLT
    pop bc
    inc ix
    inc hl
    dec bc
    ld a,b
    or c
    jr nz,frame_ram_write_page0_loop
    jp frame_reply_ok
endif

frame_decode_vram_address:     ; request[0..2] -> bank + HL low 14 bits
    ld hl,frame_request_buffer
    ld e,(hl)
    inc hl
    ld d,(hl)
    inc hl
    ld a,(hl)
    and 0FEh
    ret nz                     ; Z clear: address above 17-bit VRAM
    ld a,d
    and 0C0h
    rlca
    rlca
    ld b,a
    ld a,(hl)
    and 1
    rlca
    rlca
    or b
    ld (vram_bank),a
    ld l,e
    ld h,d
    ld a,h
    and 03Fh
    ld h,a
    ld a,(vram_bank)
    ld b,a
    ld a,(vram_bank_count)
    ld c,a
    ld a,b
    cp c
    ret nc                     ; reject banks not backed by installed VRAM
    xor a                      ; Z set
    ret

frame_cmd_vram_read:
    ld de,5
    call frame_require_length
    jp nz,frame_reply_bad_arg
    call frame_decode_vram_address
    jp nz,frame_reply_range
    ld de,(frame_request_buffer + 3)
    push hl
    ld hl,FRAMED_MAX
    or a
    sbc hl,de
    pop hl
    jp c,frame_reply_range
    ld (frame_response_length),de
    push hl
    add hl,de
    ld bc,04000h
    or a
    sbc hl,bc
    jr c,frame_vram_read_range_ok
    jr z,frame_vram_read_range_ok
    pop hl
    jp frame_reply_range
frame_vram_read_range_ok:
    pop hl
    call set_vram_read
    ld bc,(frame_request_buffer + 3)
    ld de,frame_response_buffer
frame_vram_read_loop:
    ld a,b
    or c
    jr z,frame_vram_read_done
    in a,(098h)
    ld (de),a
    inc de
    dec bc
    jr frame_vram_read_loop
frame_vram_read_done:
    call restore_r14
    xor a
    ld (frame_response_status),a
    jp frame_cache_and_send

frame_cmd_vram_write:
    ld hl,(frame_length)
    ld de,3
    or a
    sbc hl,de
    jp c,frame_reply_bad_arg
    call frame_decode_vram_address
    jp nz,frame_reply_range
    ld bc,(frame_length)
    dec bc
    dec bc
    dec bc                     ; BC=data length
    push hl
    add hl,bc
    ld de,04000h
    or a
    sbc hl,de
    jr c,frame_vram_write_range_ok
    jr z,frame_vram_write_range_ok
    pop hl
    jp frame_reply_range
frame_vram_write_range_ok:
    pop hl
    call set_vram_write
    ld hl,frame_request_buffer + 3
frame_vram_write_loop:
    ld a,b
    or c
    jr z,frame_vram_write_done
    ld a,(hl)
    out (098h),a
    inc hl
    dec bc
    jr frame_vram_write_loop
frame_vram_write_done:
    call restore_r14
    jp frame_reply_ok

frame_cmd_call:
    ld de,2
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jr nz,frame_call_state
    ld a,(in_hook)
    or a
    jr nz,frame_call_busy
    ld a,(run_state)
    or a
    jr z,frame_call_allowed
frame_call_busy:
    ld a,FRAME_BUSY
    jp frame_reply_error_cached
frame_call_state:
    ld a,FRAME_BAD_STATE
    jp frame_reply_error_cached
frame_call_allowed:
    ld hl,(frame_request_buffer)
    call jump_hl
    jp frame_reply_ok

frame_cmd_run:
    ld de,2
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jr nz,frame_run_state
    ld a,(in_hook)
    or a
    jr nz,frame_run_busy
    ld a,(run_state)
    or a
    jr z,frame_run_allowed
frame_run_busy:
    ld a,FRAME_BUSY
    jp frame_reply_error_cached
frame_run_state:
    ld a,FRAME_BAD_STATE
    jp frame_reply_error_cached
frame_run_allowed:
    call frame_reply_ok_prepare
    call frame_cache_and_send
    ld hl,(frame_request_buffer)
    ld a,1
    ld (run_state),a
    ei
    call jump_hl
    di
    xor a
    ld (run_state),a
    ret

frame_reply_ok_prepare:
    xor a
    ld (frame_response_status),a
    ld hl,0
    ld (frame_response_length),hl
    ret

frame_cmd_pause:
    ld de,0
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(in_hook)
    or a
    jp z,frame_reply_bad_state
    ld a,(run_state)
    cp 1
    jp nz,frame_reply_bad_state
    ld a,1
    ld (post_action_pending),a
    call frame_reply_ok_prepare
    call frame_cache_and_send
    xor a
    ld (post_action_pending),a
    ld a,2
    ld (run_state),a
    xor a
    ld (resume_requested),a
frame_pause_service_loop:
    call receive_dispatch
    ld a,(resume_requested)
    or a
    jr z,frame_pause_service_loop
frame_pause_complete:
    ld a,1
    ld (run_state),a
    xor a
    ld (resume_requested),a
    ; A pause may span any number of protocol frames and transport timeouts.
    ; Discard the nested parser stack and unwind through the one saved hook
    ; context instead of depending on the original PAUSE call chain.
    ld sp,(hook_dispatch_sp)
    jp hook_done

frame_cmd_resume:
    ld de,0
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(run_state)
    cp 2
    jp nz,frame_reply_bad_state
    ld a,1
    ld (resume_requested),a
    jp frame_reply_ok

frame_cmd_stop:
    ld de,0
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jp nz,frame_reply_bad_state
    ld a,1
    ld (post_action_pending),a
    call frame_reply_ok_prepare
    call frame_cache_and_send
    xor a
    ld (post_action_pending),a
    ld a,(in_hook)
    or a
    ret z
    di
    ld sp,RES_BASE
    jp monitor_reset

frame_cmd_io_read:
    ld de,1
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(frame_request_buffer)
    ld c,a
    ld b,0
    in a,(c)
    ld (frame_response_buffer),a
    ld hl,1
    ld (frame_response_length),hl
    xor a
    ld (frame_response_status),a
    jp frame_cache_and_send

frame_cmd_io_write:
    ld de,2
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(frame_request_buffer)
    ld c,a
    ld a,(frame_request_buffer + 1)
    ld b,0
    out (c),a
    jp frame_reply_ok

frame_cmd_slot_select:
    ld de,2
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(frame_request_buffer)
if MSXAI_TSR_BUILD
    jp frame_reply_unsupported
else
    cp 2
    jp nc,frame_reply_range
endif
    rrca
    rrca
    ld h,a
    ld a,(frame_request_buffer + 1)
    call 0024h
    jp frame_reply_ok

frame_cmd_mapper_select:
    ld de,2
    call frame_require_length
    jp nz,frame_reply_bad_arg
    ld a,(frame_request_buffer)
if MSXAI_TSR_BUILD
    jp frame_reply_unsupported
else
    cp 2
    jp nc,frame_reply_range
endif
    add a,0FCh
    ld c,a
    ld a,(frame_request_buffer + 1)
    out (c),a
    jp frame_reply_ok

; ----------------------------------------------------------- VDP helpers ----
get_vram_frame:
    call ser_get
    and 07h
    ld (vram_bank),a
    call get_addr
    call ser_get
    ld b,a
    ret

set_vram_read:
    ld a,(vram_bank_count)
    cp 2
    jr c,set_vram_read_address
    ld a,(REG14SAV)
    ld (saved_r14),a
    ld a,1
    ld (vram_active),a
    ld a,(vram_bank)
    out (099h),a
    ld a,08Eh
    out (099h),a
set_vram_read_address:
    ld a,l
    out (099h),a
    ld a,h
    and 03Fh
    out (099h),a
    ret

set_vram_write:
    ld a,(vram_bank_count)
    cp 2
    jr c,set_vram_write_address
    ld a,(REG14SAV)
    ld (saved_r14),a
    ld a,1
    ld (vram_active),a
    ld a,(vram_bank)
    out (099h),a
    ld a,08Eh
    out (099h),a
set_vram_write_address:
    ld a,l
    out (099h),a
    ld a,h
    and 03Fh
    or 040h
    out (099h),a
    ret

restore_r14:
    ld a,(vram_bank_count)
    cp 2
    jr c,restore_r14_done
    ld a,(saved_r14)
    out (099h),a
    ld a,08Eh
    out (099h),a
restore_r14_done:
    xor a
    ld (vram_active),a
    ret

get_addr:
    call ser_get
    ld h,a
    call ser_get
    ld l,a
    ret

; ------------------------------------------------------- transport layer ----
; The installer selects one descriptor and patches these six JP operands once.
; There is therefore no 8251/16C550 branch in the per-byte hot path.
transport_init:
    jp 0000h
transport_restore:
    jp 0000h
transport_rx_ready:
    jp 0000h
transport_tx_ready:
    jp 0000h
transport_read:
    jp 0000h
transport_write:
    jp 0000h

transport_bind:
    ld a,(active_transport_id)
    cp UART8251_ID
    jr z,transport_bind_8251
    ld hl,uart16c550_vector_table
    ld a,UART16C550_FLAGS
    ld (active_transport_flags),a
    ld a,UART16C550_CONTROL_LEVEL
    ld (active_transport_control_level),a
    jr transport_bind_vectors
transport_bind_8251:
    ld hl,uart8251_vector_table
    ld a,UART8251_FLAGS
    ld (active_transport_flags),a
    ld a,UART8251_CONTROL_LEVEL
    ld (active_transport_control_level),a
transport_bind_vectors:
    ld de,transport_init + 1
    ld b,6
transport_bind_loop:
    ld a,(hl)
    ld (de),a
    inc hl
    inc de
    ld a,(hl)
    ld (de),a
    inc hl
    inc de
    inc de
    djnz transport_bind_loop
    ret

uart8251_vector_table:
    dw uart8251_init,uart8251_restore,uart8251_rx_ready
    dw uart8251_tx_ready,uart8251_read,uart8251_write
uart16c550_vector_table:
    dw uart16c550_init,uart16c550_restore,uart16c550_rx_ready
    dw uart16c550_tx_ready,uart16c550_read,uart16c550_write

include 'agent/transports/msx_transport_8251.inc'
include 'agent/transports/msx_transport_16c550.inc'

; In foreground monitor mode serial I/O waits indefinitely. Inside a BIOS hook
; a missing byte/peer must not freeze the MSX forever; after ~one busy-loop
; period, abandon the frame and restore the interrupted application context.
ser_put:
    push af
    ld a,(in_hook)
    or a
    jr z,ser_put_wait
    push bc
    ld bc,0
ser_put_hook_wait:
    call transport_tx_ready
    or a
    jr nz,ser_put_hook_ready
    dec bc
    ld a,b
    or c
    jr nz,ser_put_hook_wait
    jp hook_transport_timeout
ser_put_hook_ready:
    pop bc
    pop af
    jp transport_write
ser_put_wait:
    call transport_tx_ready
    or a
    jr z,ser_put_wait
    pop af
    jp transport_write

ser_get:
    ld a,(in_hook)
    or a
    jr z,ser_get_wait
    push bc
    ld bc,0
ser_get_hook_wait:
    call transport_rx_ready
    or a
    jr nz,ser_get_hook_ready
    dec bc
    ld a,b
    or c
    jr nz,ser_get_hook_wait
    jp hook_transport_timeout
ser_get_hook_ready:
    pop bc
    jp transport_read
ser_get_wait:
    call transport_rx_ready
    or a
    jr z,ser_get_wait
    jp transport_read

hook_transport_timeout:
    ; Drop all nested dispatcher return addresses, resume a paused application
    ; and unwind through the single saved hook context.
    ld sp,(hook_dispatch_sp)
    ld a,(vram_active)
    or a
    call nz,restore_r14
    ld a,(post_action_pending)
    or a
    jr z,hook_timeout_no_pending_action
    xor a
    ld (post_action_pending),a
    ld (last_response_valid),a
hook_timeout_no_pending_action:
    ld a,(run_state)
    cp 2
    jr nz,hook_timeout_state_done
    ; An explicit pause is persistent. A quiet or temporarily disconnected
    ; transport must not silently resume the application. Restart the framed
    ; scanner on the saved hook stack; a later RESUME (or its retry) performs
    ; the single controlled unwind in frame_pause_complete.
    ld a,(resume_requested)
    or a
    jp nz,frame_pause_complete
    jp frame_pause_service_loop
hook_timeout_state_done:
    xor a
    ld (resume_requested),a
    jp hook_done

; Request and response deliberately share the negotiated work area. This keeps
; both the page-1 TSR and the foreground monitor compact. Small responses
; (including every state-changing command) are cached separately; bulk RAM/
; VRAM reads are side-effect free and may be recomputed on retry.
frame_request_buffer:
    ds FRAMED_MAX,0
frame_response_buffer: equ frame_request_buffer
last_response_small:
    ds 16,0

if MSXAI_TSR_BUILD
; TsrKill calls this only after MemMan has detached both hooks.
tsr_kill:
    di
    call transport_restore
    ret

; Foreground MSXAI.COM invocations use TsrCall to verify or change the selected
; hardware driver without installing a duplicate TSR. Input: A=A5h, H=driver.
; Output: A=active driver, or FFh for an unsupported request.
tsr_talk:
    cp 0A5h
    jr nz,tsr_talk_unsupported
    ld a,h
    cp 2
    jr nc,tsr_talk_unsupported
    ld b,a
    ld a,(active_transport_id)
    cp b
    jr z,tsr_talk_done
    push bc
    di
    call transport_restore
    pop bc
    ld a,b
    ld (active_transport_id),a
    call transport_bind
    call transport_init
    xor a
    ld (framed_mode),a
    ld (last_response_valid),a
    ld (last_request_valid),a
    ld (frame_reconnect_count),a
    ld hl,0
    ld (next_sequence),hl
tsr_talk_done:
    ld a,(active_transport_id)
    ret
tsr_talk_unsupported:
    ld a,0FFh
    ret
endif

resident_end:
if MSXAI_TSR_BUILD
; Initialization is deliberately last: TsrLoad releases this tail after the
; resident code and hook table have been registered.
tsr_init:
    call resident_initialize
    ld de,tsr_intro_message
    ld a,2                     ; install and ask TsrLoad to print the intro
    ret
tsr_intro_message:
    db "MSX-AI MCP resident agent installed",13,10,0
tsr_init_end:
else
hook_stack_top: equ resident_end + STACK_RESERVE
resident_size: equ resident_end - resident_start
endif
