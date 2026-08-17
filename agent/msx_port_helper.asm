; MP.COM - one-shot custom-port handoff for a freshly loaded MSX-AI MemMan TSR.
;
; MEMMAN.COM starts COMMAND2 after `_SYSTEM`, so the original MSXAI.COM process
; cannot complete the freshly installed resident's custom-port handoff. The
; install command runs this helper after TL.COM has returned. MP validates the
; port and passes its binary value in HL through MemMan TsrCall before exiting.
;
; Usage:
;   MP <1..65534>

; TCP/IP UNAPI reserves FFFFh (65535) as the random-local-port sentinel.


EXTBIO:                    equ 0FFCAh
COMMAND_TAIL:              equ 00080h
COMMAND_TEXT:              equ 00081h

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
MSXAI_TALK_UNAPI_PORT:     equ 0A6h
MSXAI_TRANSPORT_UNAPI:     equ 2

    org 0100h

port_helper_start:
    call require_dos2
    or a
    jr nz,port_helper_bad_version

    call parse_port_argument
    jr c,port_helper_bad_argument

    call find_memman_agent
    jr c,port_helper_agent_missing

    ; BC is MemMan's opaque TSR ID. TsrCall 63 guarantees HL reaches the driver
    ; entry unchanged, so A=A6 uses it as the explicit UNAPI listener port.
    ; The resident opens that port before returning its active transport in A.
    ld hl,(port_value)
    ld a,MSXAI_TALK_UNAPI_PORT
    ld d,'M'
    ld e,MEMMAN_TSR_CALL
    call EXTBIO
    ei
    cp MSXAI_TRANSPORT_UNAPI
    jr nz,port_helper_reconfigure_error

    ld de,message_success
    call print_message
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
    call parse_port_hex_nibble
    jp c,parse_port_bad
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

memman_tsr_name:
    db "MSXAI MCP1  "
memman_tsr_name_end:

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
exit_code:
    db 0

message_success:
    db 13,10,"MSXAI TCP port configured.",13,10,"$"
message_usage:
    db 13,10,"MP: usage: MP <1..65534>",13,10,"$"
message_bad_version:
    db 13,10,"MP: MSX-DOS 2 is required.",13,10,"$"
message_agent_missing:
    db 13,10,"MP: MemMan 2.4+ or MSXAI MCP1 not found.",13,10,"$"
message_reconfigure_error:
    db 13,10,"MP: MSXAI UNAPI relisten failed.",13,10,"$"

port_helper_end:
