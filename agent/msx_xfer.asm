; MSXAIXF.COM - transient protocol-X file-transfer helper for MSX-DOS 2.
;
; The resident agent owns UART framing and a bounded mailbox. This short-lived
; process owns all DOS file calls and optional PackBits decoding, keeping the
; main MSXAI.COM install/control executable independent of transfer workspace.

MSXAI_XFER_HELPER_BUILD: equ 1
MSXAI_MAIN_BUILD: equ 0
FRAMED_MAX: equ 0140h
EXTBIO: equ 0FFCAh

include 'agent/msx_xfer_protocol.inc'
XFER_WORK_CAPACITY: equ XFER_GET_CAPACITY + 6

    org 0100h

xfer_helper_entry:
    call xfer_helper_parse_command
    jp c,xfer_helper_usage
    or a
    jp z,loader_xfer_put_file
    jp loader_xfer_get_file

; Return carry on syntax error; otherwise A=0 PUT or A=1 GET and the binary
; transfer ID is written into the claim descriptor.
xfer_helper_parse_command:
    ld a,(0080h)
    and 07Fh
    ld b,a
    ld hl,0081h
    ld de,xfer_helper_command_buffer
xfer_helper_copy_tail:
    ld a,b
    or a
    jr z,xfer_helper_copy_done
    ld a,(hl)
    cp 'a'
    jr c,xfer_helper_copy_store
    cp 'z' + 1
    jr nc,xfer_helper_copy_store
    sub 020h
xfer_helper_copy_store:
    ld (de),a
    inc hl
    inc de
    djnz xfer_helper_copy_tail
xfer_helper_copy_done:
    xor a
    ld (de),a
    ld hl,xfer_helper_command_buffer
    call xfer_helper_skip_spaces
    ld de,xfer_helper_put_option
    call xfer_helper_token_equals
    jr z,xfer_helper_parse_put
    ld de,xfer_helper_get_option
    call xfer_helper_token_equals
    jr nz,xfer_helper_parse_error
    ld a,XFER_DIRECTION_GET
    jr xfer_helper_parse_action
xfer_helper_parse_put:
    ld a,XFER_DIRECTION_PUT
xfer_helper_parse_action:
    push af
    call xfer_helper_skip_token
    call xfer_helper_skip_spaces
    ld de,loader_xfer_descriptor + XFER_DESC_ID
    ld b,16
xfer_helper_parse_id_byte:
    ld a,(hl)
    call xfer_helper_hex_nibble
    jr c,xfer_helper_parse_id_error
    add a,a
    add a,a
    add a,a
    add a,a
    ld c,a
    inc hl
    ld a,(hl)
    call xfer_helper_hex_nibble
    jr c,xfer_helper_parse_id_error
    or c
    ld (de),a
    inc hl
    inc de
    djnz xfer_helper_parse_id_byte
    call xfer_helper_skip_spaces
    ld a,(hl)
    or a
    jr nz,xfer_helper_parse_id_error
    pop af
    or a
    ret
xfer_helper_parse_id_error:
    pop af
xfer_helper_parse_error:
    scf
    ret

xfer_helper_skip_spaces:
    ld a,(hl)
    cp ' '
    jr z,xfer_helper_skip_one_space
    cp 9
    ret nz
xfer_helper_skip_one_space:
    inc hl
    jr xfer_helper_skip_spaces

xfer_helper_skip_token:
    ld a,(hl)
    or a
    ret z
    cp ' '
    ret z
    cp 9
    ret z
    inc hl
    jr xfer_helper_skip_token

xfer_helper_token_equals:
    push hl
    push de
xfer_helper_token_compare:
    ld a,(de)
    or a
    jr z,xfer_helper_token_end
    cp (hl)
    jr nz,xfer_helper_token_no
    inc hl
    inc de
    jr xfer_helper_token_compare
xfer_helper_token_end:
    ld a,(hl)
    or a
    jr z,xfer_helper_token_yes
    cp ' '
    jr z,xfer_helper_token_yes
    cp 9
    jr z,xfer_helper_token_yes
xfer_helper_token_no:
    pop de
    pop hl
    ld a,1
    or a
    ret
xfer_helper_token_yes:
    pop de
    pop hl
    xor a
    ret

xfer_helper_hex_nibble:
    cp '0'
    jr c,xfer_helper_hex_error
    cp '9' + 1
    jr c,xfer_helper_hex_digit
    cp 'A'
    jr c,xfer_helper_hex_error
    cp 'F' + 1
    jr nc,xfer_helper_hex_error
    sub 'A' - 10
    or a
    ret
xfer_helper_hex_digit:
    sub '0'
    or a
    ret
xfer_helper_hex_error:
    scf
    ret

xfer_helper_usage:
    ld de,xfer_helper_usage_message
    ld c,9
    call 00005h
    ld c,0
    jp 00005h

xfer_helper_usage_message:
    db 13,10,"MSX-AI MCP File Transfer Helper",13,10
    db "Usage: MSXAIXF /PUT|/GET <32-hex-transfer-id>",13,10,"$"
xfer_helper_put_option:
    db "/PUT",0
xfer_helper_get_option:
    db "/GET",0
xfer_helper_command_buffer:
    ds 128,0

; TsrCall maps the resident into page 1. Keep the shared descriptor and mailbox
; in page 0 so they remain visible throughout each copy.
loader_xfer_descriptor:
    ds XFER_DESC_SIZE,0
loader_xfer_buffer:
    ds XFER_WORK_CAPACITY,0
last_error:
    db 0
dos2_available:
    db 0

include 'agent/msx_memman_loader.asm'

; Minimal MemMan discovery needed by the transfer helper. IniChk must precede
; GetTsrID on every invocation.
memman_find_agent:
    xor a                       ; IniChk control code must be zero
    ld d,'M'
    ld e,30
    call EXTBIO
    cp 'M'
    jr nz,memman_find_agent_absent
    ld a,d
    cp 2
    jr c,memman_find_agent_absent
    jr nz,memman_find_agent_ready
    ld a,e
    cp 4
    jr c,memman_find_agent_absent
memman_find_agent_ready:
    ld hl,memman_tsr_name
    ld d,'M'
    ld e,62
    call EXTBIO
    ret
memman_find_agent_absent:
    scf
    ret

memman_tsr_name:
    db "MSXAI MCP1  "

; B=handle, DE=buffer, HL=count. Successful short writes fail closed.
write_exact:
    ld a,h
    or l
    ret z
    push hl
    ld c,DOS_WRITE
    call 00005h
    pop de
    or a
    ret nz
    or a
    sbc hl,de
    ret z
    ld a,ERR_INTERNAL
    ret
