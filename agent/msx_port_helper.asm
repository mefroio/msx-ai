; MP.COM - one-shot custom-port handoff for a freshly loaded MSX-AI MemMan TSR.
;
; MEMMAN.COM starts COMMAND2 after `_SYSTEM`, so the original MSXAI.COM process
; cannot complete the freshly installed resident's custom-port handoff. The
; install command runs this helper after TL.COM has returned. MP validates the
; port and passes a guarded A7 request through MemMan TsrCall before exiting.
;
; Usage:
;   MP <1..65534>

; TCP/IP UNAPI reserves FFFFh (65535) as the random-local-port sentinel.


EXTBIO:                    equ 0FFCAh
COMMAND_TAIL:              equ 00080h
COMMAND_TEXT:              equ 00081h
TPA_TOP_POINTER:           equ 00006h

DOS_TERM_ERROR:            equ 062h
DOS_VERSION:               equ 06Fh

ERR_INTERNAL:              equ 0DFh
ERR_INVALID_PARAMETER:     equ 08Bh
ERR_BAD_VERSION:           equ 085h

MEMMAN_MINIMUM_MAJOR:      equ 2
MEMMAN_MINIMUM_MINOR:      equ 4
MEMMAN_INICHK:             equ 30
MEMMAN_GET_TSR_ID:         equ 62
MEMMAN_TSR_CALL:           equ 63
MSXAI_TALK_UNAPI_PORT:     equ 0A7h
MSXAI_TALK_TRACE:          equ 0A8h
MSXAI_TRANSPORT_UNAPI:     equ 2
MSXAI_UNAPI_REQUEST_MAGIC: equ 0A75Ah
MSXAI_UNAPI_REQUEST_VERSION: equ 3
MSXAI_UNAPI_REQUEST_SIZE:  equ 20
MSXAI_UNAPI_STACK_SIZE:    equ 0400h
MSXAI_UNAPI_GUARD_SIZE:    equ 16
MSXAI_UNAPI_STACK_HEADROOM: equ 0100h
MSXAI_UNAPI_LOW_GUARD:     equ 0A5h
MSXAI_UNAPI_HIGH_GUARD:    equ 05Ah
MSXAI_TRACE_REQUEST_MAGIC: equ 0A85Ah
MSXAI_TRACE_REQUEST_VERSION: equ 1
MSXAI_TRACE_REQUEST_SIZE:  equ 16
MSXAI_TRACE_ACTION_ENABLE: equ 1

    org 0100h

port_helper_start:
    call require_dos2
    or a
    jr nz,port_helper_bad_version

    call parse_port_argument
    jr c,port_helper_bad_argument

    call find_memman_agent
    jp c,port_helper_agent_missing

    ; Preserve MemMan's opaque ID while constructing the A7 request. The
    ; resident moves lifecycle work to this process's guarded page-2 stack and
    ; restores MemMan's own stack before returning.
    ld (memman_tsr_id),bc
    ld a,(trace_requested)
    or a
    jr z,port_helper_trace_ready
    ld bc,(memman_tsr_id)
    ld hl,trace_request
    ld a,MSXAI_TALK_TRACE
    ld d,'M'
    ld e,MEMMAN_TSR_CALL
    call EXTBIO
    di
    or a
    jr nz,port_helper_trace_error_ei
    ld a,(trace_request_status)
    or a
    jr nz,port_helper_trace_error_ei
port_helper_trace_ready:
    ; A8 returns with DI like every resident TsrCall. TCP_OPEN must enter A7
    ; with normal foreground interrupt state, matching the non-TRACE path.
    ei
    call prepare_unapi_request
    jr c,port_helper_reconfigure_error_ei
    ld bc,(memman_tsr_id)
    ld hl,unapi_request
    ld a,MSXAI_TALK_UNAPI_PORT
    ld d,'M'
    ld e,MEMMAN_TSR_CALL
    call EXTBIO
    di
    ld (unapi_call_result),a
    call verify_unapi_guards
    jr c,port_helper_reconfigure_error_ei
    ld a,(unapi_request_status)
    or a
    jr nz,port_helper_reconfigure_error_ei
    ld a,(unapi_request_transport)
    cp MSXAI_TRANSPORT_UNAPI
    jr nz,port_helper_reconfigure_error_ei
    ld a,(unapi_call_result)
    cp MSXAI_TRANSPORT_UNAPI
    jr nz,port_helper_reconfigure_error_ei
port_helper_success:
    ei
    call port_helper_clear_screen
    ld de,port_helper_success_banner
    call print_message

    ld de,mcp_listening_prefix
    ld hl,unapi_request_local_ip
    ld bc,(port_value)
    call mcp_endpoint_print
    xor a
    jp terminate_with_code

port_helper_bad_version:
    ld de,message_bad_version
    ld a,ERR_BAD_VERSION
    jr port_helper_fail
port_helper_bad_argument:
    ld de,message_usage
    ld a,ERR_INVALID_PARAMETER
    jr port_helper_fail
port_helper_agent_missing:
    ld de,message_agent_missing
    ld a,ERR_INTERNAL
    jr port_helper_fail
port_helper_trace_error_ei:
    ei
    ld de,message_trace_error
    ld a,ERR_INTERNAL
    jr port_helper_fail
port_helper_reconfigure_error_ei:
    ei
port_helper_reconfigure_error:
    ld de,message_reconfigure_error
    ld a,ERR_INTERNAL

port_helper_fail:
    ld (exit_code),a
    call print_message
    ld a,(exit_code)

terminate_with_code:
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

print_message:
    ld c,9
    jp 00005h

; MSX-DOS routes form feed through the console driver as clear-screen-and-home.
; Keep this on the fully validated success path so every failure remains
; visible.
port_helper_clear_screen:
    ld e,12
    ld c,2
    jp 00005h

require_dos2:
    ld c,DOS_VERSION
    call 00005h
    or a
    ret nz
    ld a,b
    cp 2
    jr c,require_dos2_unavailable
    xor a
    ret
require_dos2_unavailable:
    ld a,ERR_BAD_VERSION
    ret

; Parse exactly one decimal argument from the counted DOS command tail.
; Leading/trailing spaces and tabs are accepted. A single leading slash is the
; compact COMMAND2 form used by the 39-byte MemMan install chain (`MP/A873`).
; On success carry is clear and BC plus port_value contain 1..65534. No byte
; beyond the counted tail is inspected.
parse_port_argument:
    xor a
    ld (trace_requested),a
    ld a,(COMMAND_TAIL)
    and 07Fh
    ld (port_remaining),a
    ld hl,COMMAND_TEXT

parse_port_skip_leading:
    ld a,(port_remaining)
    or a
    jp z,parse_port_bad
    ld a,(hl)
    cp ' '
    jr z,parse_port_skip_leading_one
    cp 9
    jr z,parse_port_skip_leading_one
    cp '/'
    jr nz,parse_port_digits_begin
    ; COMMAND2 treats slash as a command-name delimiter. The install chain uses
    ; exactly four hexadecimal digits after it (`MP/A873` for decimal 43123),
    ; while the normal manual form remains decimal (`MP 43123`).
    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    jp z,parse_port_bad
    jp parse_port_hex_begin
parse_port_skip_leading_one:
    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    jr parse_port_skip_leading

parse_port_digits_begin:
    ld bc,0
    xor a
    ld (port_digit_count),a

parse_port_digit_loop:
    ld a,(port_remaining)
    or a
    jr z,parse_port_complete
    ld a,(hl)
    cp ' '
    jr z,parse_port_trailing
    cp 9
    jr z,parse_port_trailing
    cp '0'
    jr c,parse_port_bad
    cp '9' + 1
    jr nc,parse_port_bad

    ld e,a
    ld a,(port_digit_count)
    inc a
    cp 6
    jr nc,parse_port_bad
    ld (port_digit_count),a
    ld a,e
    sub '0'
    ld (port_digit),a

    ; BC = BC * 10 + digit, rejecting every 16-bit overflow.
    push hl
    ld h,b
    ld l,c
    add hl,hl
    jr c,parse_port_overflow_pop
    ld d,h
    ld e,l
    add hl,hl
    jr c,parse_port_overflow_pop
    add hl,hl
    jr c,parse_port_overflow_pop
    add hl,de
    jr c,parse_port_overflow_pop
    ld a,(port_digit)
    ld e,a
    ld d,0
    add hl,de
    jr c,parse_port_overflow_pop
    ld b,h
    ld c,l
    pop hl

    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    jr parse_port_digit_loop

parse_port_overflow_pop:
    pop hl
    jr parse_port_bad

parse_port_trailing:
    ld a,(port_remaining)
    or a
    jr z,parse_port_complete
    ld a,(hl)
    cp ' '
    jr z,parse_port_trailing_one
    cp 9
    jr nz,parse_port_bad
parse_port_trailing_one:
    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    jr parse_port_trailing

parse_port_complete:
    ld a,(port_digit_count)
    or a
    jr z,parse_port_bad
    ld a,b
    or c
    jr z,parse_port_bad
    ld a,b
    and c
    inc a                         ; FFFFh is not an explicit UNAPI port
    jr z,parse_port_bad
    xor a
    ld (port_value),bc
    or a                          ; clear carry
    ret

parse_port_bad:
    scf
    ret

; Private compact install-chain form: exactly four hexadecimal nibbles. Manual
; invocation remains decimal, while both forms produce the same binary value.
parse_port_hex_begin:
    ld bc,0
    xor a
    ld (port_hex_digits),a
parse_port_hex_loop:
    ld a,(port_remaining)
    or a
    jr z,parse_port_hex_complete
    ld a,(hl)
    cp ' '
    jr z,parse_port_hex_trailing
    cp 9
    jr z,parse_port_hex_trailing
    ld a,(port_hex_digits)
    or a
    jr nz,parse_port_hex_normal_nibble
    ld a,(hl)
    and 0DFh
    cp 'G'
    jr c,parse_port_hex_normal_nibble
    cp 'V' + 1
    jr nc,parse_port_hex_normal_nibble
    sub 'G'
    ld (port_digit),a
    ld a,1
    ld (trace_requested),a
    ld a,(port_digit)
    or a
    jr parse_port_hex_have_nibble
parse_port_hex_normal_nibble:
    ld a,(hl)
    call parse_port_hex_nibble
    jp c,parse_port_bad
parse_port_hex_have_nibble:
    ld (port_digit),a

    push hl
    ld h,b
    ld l,c
    add hl,hl
    add hl,hl
    add hl,hl
    add hl,hl
    ld a,(port_digit)
    ld e,a
    ld d,0
    add hl,de
    ld b,h
    ld c,l
    pop hl

    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    ld a,(port_hex_digits)
    inc a
    ld (port_hex_digits),a
    cp 4
    jr c,parse_port_hex_loop

parse_port_hex_trailing:
    ld a,(port_remaining)
    or a
    jr z,parse_port_hex_complete
    ld a,(hl)
    cp ' '
    jr z,parse_port_hex_trailing_one
    cp 9
    jp nz,parse_port_bad
parse_port_hex_trailing_one:
    inc hl
    ld a,(port_remaining)
    dec a
    ld (port_remaining),a
    jr parse_port_hex_trailing

parse_port_hex_complete:
    ld a,(port_hex_digits)
    cp 4
    jp nz,parse_port_bad
    ld a,b
    or c
    jp z,parse_port_bad
    ld a,b
    and c
    inc a
    jp z,parse_port_bad           ; reject FFFFh
    ld (port_value),bc
    or a
    ret

parse_port_hex_nibble:
    cp '0'
    jr c,parse_port_hex_nibble_bad
    cp '9' + 1
    jr nc,parse_port_hex_nibble_letter
    sub '0'
    or a
    ret
parse_port_hex_nibble_letter:
    and 0DFh
    cp 'A'
    jr c,parse_port_hex_nibble_bad
    cp 'F' + 1
    jr nc,parse_port_hex_nibble_bad
    sub 'A' - 10
    or a
    ret
parse_port_hex_nibble_bad:
    scf
    ret

; IniChk must be the first MemMan operation in this process.  Require MemMan
; 2.4+, then resolve the exact 12-byte MSX-AI TSR name.  GetTsrID propagates
; carry when the resident is not installed.
find_memman_agent:
    xor a
    ld d,'M'
    ld e,MEMMAN_INICHK
    call EXTBIO
    cp 'M'
    jr nz,find_memman_agent_absent
    ld a,d
    cp MEMMAN_MINIMUM_MAJOR
    jr c,find_memman_agent_absent
    jr nz,find_memman_agent_ready
    ld a,e
    cp MEMMAN_MINIMUM_MINOR
    jr c,find_memman_agent_absent
find_memman_agent_ready:
    ld hl,memman_tsr_name
    ld d,'M'
    ld e,MEMMAN_GET_TSR_ID
    call EXTBIO
    ret
find_memman_agent_absent:
    scf
    ret

; Build a 1 KiB page-2 stack below the strictest of C000h, the DOS TPA top,
; and the current SP minus 256 bytes of caller headroom. Validate the complete
; low-guard/stack/high-guard span before writing either guard.
prepare_unapi_request:
    ld hl,(port_value)
    ld (unapi_request_port),hl

    ld hl,0C000h
    ld de,(TPA_TOP_POINTER)
    or a
    sbc hl,de
    jr c,prepare_unapi_limit_is_c000
    ld hl,(TPA_TOP_POINTER)
    jr prepare_unapi_have_tpa_limit
prepare_unapi_limit_is_c000:
    ld hl,0C000h
prepare_unapi_have_tpa_limit:
    ld (unapi_request_stack_limit),hl

    ld hl,0
    add hl,sp
    ld de,MSXAI_UNAPI_STACK_HEADROOM
    or a
    sbc hl,de
    jr c,prepare_unapi_no_stack
    ld (unapi_request_sp_limit),hl
    ld de,(unapi_request_stack_limit)
    or a
    sbc hl,de
    jr c,prepare_unapi_limit_is_sp
    ld hl,(unapi_request_stack_limit)
    jr prepare_unapi_have_limit
prepare_unapi_limit_is_sp:
    ld hl,(unapi_request_sp_limit)
prepare_unapi_have_limit:
    ld de,MSXAI_UNAPI_GUARD_SIZE
    or a
    sbc hl,de
    jr c,prepare_unapi_no_stack
    res 0,l
    ld (unapi_request_stack_top),hl
    ld de,MSXAI_UNAPI_STACK_SIZE
    or a
    sbc hl,de
    jr c,prepare_unapi_no_stack
    ld (unapi_request_stack_bottom),hl
    ld de,MSXAI_UNAPI_GUARD_SIZE
    or a
    sbc hl,de
    jr c,prepare_unapi_no_stack
    ld a,h
    cp 080h
    jr c,prepare_unapi_no_stack

    ; The complete span is now proven to remain within writable page 2.
    ld b,MSXAI_UNAPI_GUARD_SIZE
    ld a,MSXAI_UNAPI_LOW_GUARD
prepare_unapi_low_guard_loop:
    ld (hl),a
    inc hl
    djnz prepare_unapi_low_guard_loop
    ld hl,(unapi_request_stack_top)
    ld b,MSXAI_UNAPI_GUARD_SIZE
    ld a,MSXAI_UNAPI_HIGH_GUARD
prepare_unapi_high_guard_loop:
    ld (hl),a
    inc hl
    djnz prepare_unapi_high_guard_loop

    ld a,0FFh
    ld (unapi_request_status),a
    ld (unapi_request_error),a
    ld (unapi_request_transport),a
    ld a,MSXAI_TRANSPORT_UNAPI
    ld (unapi_request_target),a
    xor a
    ld (unapi_request_connection),a
    ld (unapi_request_16c550_divisor),a
    or a
    ret
prepare_unapi_no_stack:
    scf
    ret

verify_unapi_guards:
    ld hl,(unapi_request_stack_bottom)
    ld de,MSXAI_UNAPI_GUARD_SIZE
    or a
    sbc hl,de
    ld a,MSXAI_UNAPI_LOW_GUARD
    call verify_unapi_guard
    jr nz,verify_unapi_guards_bad
    ld hl,(unapi_request_stack_top)
    ld a,MSXAI_UNAPI_HIGH_GUARD
    call verify_unapi_guard
    jr nz,verify_unapi_guards_bad
    or a
    ret
verify_unapi_guards_bad:
    scf
    ret

verify_unapi_guard:
    ld b,MSXAI_UNAPI_GUARD_SIZE
verify_unapi_guard_loop:
    cp (hl)
    ret nz
    inc hl
    djnz verify_unapi_guard_loop
    ret

memman_tsr_name:
    db "MSXAI MCP1  "
memman_tsr_name_end:

memman_tsr_id:
    dw 0
unapi_call_result:
    db 0FFh
unapi_request:
    dw MSXAI_UNAPI_REQUEST_MAGIC
    db MSXAI_UNAPI_REQUEST_VERSION
    db MSXAI_UNAPI_REQUEST_SIZE
unapi_request_port:
    dw 0
unapi_request_stack_bottom:
    dw 0
unapi_request_stack_top:
    dw 0
unapi_request_status:
    db 0FFh
unapi_request_error:
    db 0FFh
unapi_request_transport:
    db 0FFh
unapi_request_connection:
    db 0
unapi_request_target:
    db MSXAI_TRANSPORT_UNAPI
unapi_request_16c550_divisor:
    db 0
unapi_request_local_ip:
    ds 4,0

trace_request:
    dw MSXAI_TRACE_REQUEST_MAGIC
    db MSXAI_TRACE_REQUEST_VERSION
    db MSXAI_TRACE_REQUEST_SIZE
    db MSXAI_TRACE_ACTION_ENABLE
trace_request_status:
    db 0FFh
    dw 0
    ds MSXAI_TRACE_REQUEST_SIZE - 8,0

unapi_request_stack_limit:
    dw 0
unapi_request_sp_limit:
    dw 0

port_value:
    dw 0
port_remaining:
    db 0
port_digit_count:
    db 0
port_digit:
    db 0
port_hex_digits:
    db 0
trace_requested:
    db 0
exit_code:
    db 0

mcp_listening_prefix:
    db "MCP listening at: $"
port_helper_success_banner:
    include 'agent/msx_version.inc'
    db "Author: Rodrigo Galhardi M. Garcia",13,10,13,10,"$"
port_helper_success_banner_end:
message_usage:
    db 13,10,"MP: usage: MP <1..65534>",13,10,"$"
message_bad_version:
    db 13,10,"MP: MSX-DOS 2 is required.",13,10,"$"
message_agent_missing:
    db 13,10,"MP: MemMan 2.4+ or MSXAI MCP1 not found.",13,10,"$"
message_trace_error:
    db 13,10,"MP: MSXAI resident trace enable failed.",13,10,"$"
message_reconfigure_error:
    db 13,10,"MP: MSXAI UNAPI relisten failed.",13,10,"$"

include 'agent/msx_endpoint_print.inc'

port_helper_end:
