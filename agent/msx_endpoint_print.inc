; Shared transient MSX-DOS endpoint formatter.
;
; Input to mcp_endpoint_print:
;   DE = "$"-terminated prefix
;   HL = four-byte IPv4 address in UNAPI L.H.E.D memory order
;   BC = binary TCP port

MCP_ENDPOINT_BDOS:          equ 00005h
MCP_ENDPOINT_BDOS_CONOUT:   equ 2
MCP_ENDPOINT_BDOS_STROUT:   equ 9

mcp_endpoint_print:
    push bc
    call mcp_endpoint_print_string
    call mcp_endpoint_print_ip
    pop hl
    ld a,":"
    call mcp_endpoint_print_char
    call mcp_endpoint_print_decimal16
    ld de,mcp_endpoint_crlf
    jp mcp_endpoint_print_string

mcp_endpoint_print_string:
    push af
    push bc
    push de
    push hl
    push ix
    push iy
    ld c,MCP_ENDPOINT_BDOS_STROUT
    call MCP_ENDPOINT_BDOS
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    pop af
    ret

mcp_endpoint_print_char:
    push af
    push bc
    push de
    push hl
    push ix
    push iy
    ld e,a
    ld c,MCP_ENDPOINT_BDOS_CONOUT
    call MCP_ENDPOINT_BDOS
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    pop af
    ret

mcp_endpoint_print_decimal8:
    push hl
    ld l,a
    ld h,0
    call mcp_endpoint_print_decimal16
    pop hl
    ret

mcp_endpoint_print_decimal16:
    push af
    push bc
    push de
    push hl
    xor a
    ld (mcp_endpoint_decimal_started),a
    ld de,10000
    call mcp_endpoint_print_decimal_place
    ld de,1000
    call mcp_endpoint_print_decimal_place
    ld de,100
    call mcp_endpoint_print_decimal_place
    ld de,10
    call mcp_endpoint_print_decimal_place
    ld a,l
    add a,"0"
    call mcp_endpoint_print_char
    pop hl
    pop de
    pop bc
    pop af
    ret

mcp_endpoint_print_decimal_place:
    ld b,"0"
mcp_endpoint_print_decimal_place_loop:
    or a
    sbc hl,de
    jr c,mcp_endpoint_print_decimal_place_done
    inc b
    jr mcp_endpoint_print_decimal_place_loop
mcp_endpoint_print_decimal_place_done:
    add hl,de
    ld a,b
    cp "0"
    jr nz,mcp_endpoint_print_decimal_place_emit
    ld a,(mcp_endpoint_decimal_started)
    or a
    ret z
    ld a,b
mcp_endpoint_print_decimal_place_emit:
    push af
    ld a,1
    ld (mcp_endpoint_decimal_started),a
    pop af
    jp mcp_endpoint_print_char

mcp_endpoint_print_ip:
    push af
    push bc
    push hl
    ld b,4
mcp_endpoint_print_ip_loop:
    ld a,(hl)
    call mcp_endpoint_print_decimal8
    inc hl
    dec b
    jr z,mcp_endpoint_print_ip_done
    ld a,"."
    call mcp_endpoint_print_char
    jr mcp_endpoint_print_ip_loop
mcp_endpoint_print_ip_done:
    pop hl
    pop bc
    pop af
    ret

mcp_endpoint_decimal_started:
    db 0
mcp_endpoint_crlf:
    db 13,10,"$"
