; BADINIT.COM - non-persistent BaDCaT/ZiModem listener initializer.
;
; Usage:
;   BADINIT                         57600 baud, TCP port 6603 (defaults)
;   BADINIT /57600                  explicit 57600 baud
;   BADINIT /115200                 switch this session to 115200 baud
;   BADINIT /PORT:7000 /115200      choose a TCP port and baud rate
;
; The saved modem configuration is expected to remain at 57600 baud.  Every
; command below affects only the current session.  In particular, this utility
; never invokes firmware save/reset/factory operations or the persistent
; listener register.  A power cycle therefore restores the user's saved state.

BDOS:                   equ 00005h
EXTBIO:                 equ 0FFCAh
COMMAND_TAIL:           equ 00080h
COMMAND_TEXT:           equ 00081h
JIFFY:                  equ 0FC9Eh

DOS_CONSOLE_OUTPUT:     equ 002h
DOS_PRINT_STRING:       equ 009h
DOS_TERM_ERROR:         equ 062h

UART_DATA:              equ 080h
UART_IER:               equ 081h
UART_FCR:               equ 082h
UART_LCR:               equ 083h
UART_MCR:               equ 084h
UART_LSR:               equ 085h

UART_DIVISOR_57600:     equ 2
UART_DIVISOR_115200:    equ 1
DEFAULT_LISTENER_PORT:  equ 6603
LISTENER_PREFIX_LENGTH: equ 10
LISTENER_PORT_CAPACITY: equ 6         ; five decimal digits plus NUL
LISTENER_COMMAND_CAPACITY: equ 16     ; prefix + five digits plus NUL
COMMAND_BUFFER_CAPACITY: equ 128      ; maximum DOS tail plus NUL
RESPONSE_TIMEOUT:       equ 180       ; about 3 s NTSC / 3.6 s PAL
RESPONSE_LINE_CAPACITY: equ 31
RESPONSE_STREAM_LIMIT:  equ 1024      ; bound an accidental active TCP stream
BAUD_SETTLE_TICKS:      equ 40        ; >650 ms NTSC / >780 ms PAL minimum
LISTENER_SETTLE_TICKS:  equ 4         ; >=50 ms before handing UART to MSXAI
RESPONSE_QUIET_TICKS:   equ 2         ; at least one complete JIFFY of silence
LSR_ERROR_MASK:         equ 09Eh      ; OE|PE|FE|BI|FIFO error
DIAGNOSTIC_SAMPLE_SIZE: equ 8

RESPONSE_OK:            equ 0
RESPONSE_TIMEOUT_ERROR: equ 1
RESPONSE_MODEM_ERROR:   equ 2
RESPONSE_UART_ERROR:    equ 3
RESPONSE_LINE_ERROR:    equ 4
RESPONSE_STREAM_ERROR:  equ 5

MEMMAN_INICHK:          equ 30
MEMMAN_GET_TSR_ID:      equ 62
MEMMAN_MINIMUM_MAJOR:   equ 2
MEMMAN_MINIMUM_MINOR:   equ 4

    org 0100h

badinit_start:
    ei
    call parse_command_line
    jp c,badinit_usage

    call find_resident_agent
    jp nc,badinit_resident_active
    ei                            ; EXTBIO implementations may return with DI

    ; The power-on/saved baseline is always probed at 57600 first, regardless
    ; of the requested final speed.  This prevents /115200 from talking to a
    ; 57600-baud modem with a mismatched local UART.
    ld hl,stage_uart_init_57600
    call diagnostic_begin_stage
    call uart_init_57600
    jp c,badinit_failed
    ld b,2
    call wait_ticks
    ld hl,stage_sync_57600
    call diagnostic_begin_stage
    call synchronize_command_mode
    jp c,badinit_failed

    ld de,message_banner
    call print_string
    ld a,(selected_divisor)
    cp UART_DIVISOR_115200
    ld de,message_57600
    jr nz,badinit_banner_ready
    ld de,message_115200
badinit_banner_ready:
    call print_string

    ld hl,stage_probe_57600
    call diagnostic_begin_stage
    ld hl,command_bootstrap
    call run_visible_command
    jr nc,badinit_link_ready

    ; A previous, unsaved BADINIT /115200 session may still be active.  A
    ; timeout or captured line-status framing errors can indicate the wrong
    ; divisor; a modem ERROR, missing UART, or TX failure remains fatal.
    ld a,(response_status)
    cp RESPONSE_TIMEOUT_ERROR
    jr z,badinit_retry_115200
    cp RESPONSE_LINE_ERROR
    jp nz,badinit_failed
badinit_retry_115200:
    ld de,message_retry_115200
    call print_string
    ld hl,stage_sync_115200
    call diagnostic_begin_stage
    ld a,UART_DIVISOR_115200
    call uart_set_baud
    ld (current_divisor),a
    call synchronize_command_mode
    jp c,badinit_failed
    ld hl,stage_probe_115200
    call diagnostic_begin_stage
    ld hl,command_bootstrap
    call run_visible_command
    jp c,badinit_failed

badinit_link_ready:
    ld hl,initial_command_table
badinit_initial_loop:
    ld e,(hl)
    inc hl
    ld d,(hl)
    inc hl
    ld a,d
    or e
    jr z,badinit_initial_done
    push hl
    push de
    ld hl,stage_runtime_setup
    call diagnostic_begin_stage
    pop hl
    call run_visible_command
    pop hl
    jp c,badinit_failed
    jr badinit_initial_loop

badinit_initial_done:
    ld a,(selected_divisor)
    ld b,a
    ld a,(current_divisor)
    cp b
    jr z,badinit_listener
    ld hl,stage_baud_change
    call diagnostic_begin_stage
    ld a,b
    call change_runtime_baud
    jp c,badinit_failed

badinit_listener:
    ; ATI2 is deliberately issued before opening the listener.  Once a host
    ; connects, S41=1 can enter stream mode and later AT text would become MCP
    ; payload.
    ld hl,listener_command_table
badinit_listener_loop:
    ld e,(hl)
    inc hl
    ld d,(hl)
    inc hl
    ld a,d
    or e
    jr z,badinit_open_listener
    push hl
    push de
    ld hl,stage_ip_query
    call diagnostic_begin_stage
    pop hl
    call run_visible_command
    pop hl
    jp c,badinit_failed
    jr badinit_listener_loop

badinit_open_listener:
    ; Open the runtime listener while automatic stream entry is disabled.
    ; Unlike Q1+A<port>, this leaves any listener creation ERROR visible and
    ; prevents an early host from turning subsequent AT text into MCP payload.
    ld hl,stage_listener_open
    call diagnostic_begin_stage
    ld hl,command_listener_open
    call run_visible_command
    jp c,badinit_failed

badinit_quiet:
    ; The listener now exists with S41=0.  H drops any premature clients,
    ; S41=1 enables automatic streaming, and Q1 suppresses only the final OK.
    ; ZiModem processes the complete serial line before accepting a new client,
    ; so this final send-only command cannot be split by entry into stream mode.
    ld hl,stage_listener_commit
    call diagnostic_begin_stage
    ld hl,command_stream_commit
    call send_command
    jp c,badinit_listener_commit_failed
    call uart_wait_empty
    jp c,badinit_listener_commit_failed
    ld b,LISTENER_SETTLE_TICKS
    call wait_ticks               ; no UART access after the commit

    ld de,message_success_prefix
    call print_string
    ld hl,command_listener_port_text
    call print_c_string
    ld de,message_success_suffix
    call print_string
    xor a
    jp terminate

badinit_failed:
    call diagnostic_report_once
    ld a,(response_status)
    cp RESPONSE_STREAM_ERROR
    jr z,badinit_failed_no_recovery
    ; Best effort: return both ends to visible 57600-baud command mode.  Even
    ; if recovery cannot communicate, no saved modem state was modified and a
    ; power cycle remains a deterministic fallback.
    call restore_visible_57600
badinit_failed_no_recovery:
    ld de,message_failed
    call print_string
    ld a,1
    jp terminate

badinit_listener_commit_failed:
    ; The listener command may already have taken effect.  Do not emit any
    ; more UART bytes from this point: a recovery command could become MCP
    ; stream payload.  Only report through DOS and terminate.
    call diagnostic_report_once
    ld de,message_failed
    call print_string
    ld a,1
    jp terminate

badinit_usage:
    ld de,message_usage
    call print_string
    ld a,2
    jp terminate

badinit_resident_active:
    ld de,message_resident_active
    call print_string
    ld a,3

terminate:
    ld b,a
    ld c,DOS_TERM_ERROR
    call BDOS
    ld c,0                       ; DOS1-compatible terminate fallback
    jp BDOS

; ---------------------------------------------------------------- command line

parse_command_line:
    ld a,UART_DIVISOR_57600
    ld (selected_divisor),a
    ld hl,DEFAULT_LISTENER_PORT
    ld (selected_port),hl
    xor a
    ld (baud_option_seen),a
    ld (port_option_seen),a

    ; Normalize the counted DOS command tail to uppercase. The count is at
    ; most 127, so command_buffer always has room for the terminating NUL.
    ld a,(COMMAND_TAIL)
    and 07Fh
    ld b,a
    ld hl,COMMAND_TEXT
    ld de,command_buffer
parse_copy_tail:
    ld a,b
    or a
    jr z,parse_copy_tail_done
    ld a,(hl)
    cp 'a'
    jr c,parse_copy_tail_store
    cp 'z' + 1
    jr nc,parse_copy_tail_store
    sub 020h
parse_copy_tail_store:
    ld (de),a
    inc hl
    inc de
    dec b
    jr parse_copy_tail
parse_copy_tail_done:
    xor a
    ld (de),a

    ld hl,command_buffer
parse_token_loop:
    call parse_skip_spaces
    ld a,(hl)
    or a
    jp z,build_listener_command

    ld de,option_57600
    call parse_token_equals
    jr z,parse_select_57600
    ld de,option_115200
    call parse_token_equals
    jr z,parse_select_115200
    ld de,option_port_prefix
    call parse_token_has_prefix
    jp z,parse_select_port
parse_bad:
    scf
    ret

parse_select_57600:
    ld a,UART_DIVISOR_57600
    jr parse_select_baud
parse_select_115200:
    ld a,UART_DIVISOR_115200
parse_select_baud:
    ld c,a
    ld a,(baud_option_seen)
    or a
    jr nz,parse_bad
    ld a,1
    ld (baud_option_seen),a
    ld a,c
    ld (selected_divisor),a
    call parse_skip_token
    jr parse_token_loop

parse_select_port:
    ld a,(port_option_seen)
    or a
    jr nz,parse_bad
    ld a,1
    ld (port_option_seen),a

    ; Advance HL past the already-matched "/PORT:" prefix.
    ld de,option_port_prefix
parse_port_prefix_loop:
    ld a,(de)
    or a
    jr z,parse_port_digits_start
    inc hl
    inc de
    jr parse_port_prefix_loop

parse_port_digits_start:
    ld bc,0
    xor a
    ld (port_digit_count),a
parse_port_digit_loop:
    ld a,(hl)
    or a
    jr z,parse_port_complete
    cp ' '
    jr z,parse_port_complete
    cp 9
    jr z,parse_port_complete
    cp '0'
    jp c,parse_bad
    cp '9' + 1
    jp nc,parse_bad
    ld e,a
    ld a,(port_digit_count)
    inc a
    cp 6
    jp nc,parse_bad              ; a TCP port has at most five digits
    ld (port_digit_count),a
    ld a,e
    sub '0'
    ld (port_digit),a

    ; BC = BC * 10 + digit, rejecting all 16-bit overflow. Unlike the UNAPI
    ; client port option, FFFFh is a valid ZiModem listener port here.
    push hl
    ld h,b
    ld l,c
    add hl,hl                    ; value * 2
    jp c,parse_port_overflow
    ld d,h
    ld e,l
    add hl,hl                    ; value * 4
    jp c,parse_port_overflow
    add hl,hl                    ; value * 8
    jp c,parse_port_overflow
    add hl,de                    ; value * 10
    jp c,parse_port_overflow
    ld a,(port_digit)
    ld e,a
    ld d,0
    add hl,de
    jp c,parse_port_overflow
    ld b,h
    ld c,l
    pop hl
    inc hl
    jr parse_port_digit_loop
parse_port_overflow:
    pop hl
    jp parse_bad

parse_port_complete:
    ld a,(port_digit_count)
    or a
    jp z,parse_bad
    ld a,b
    or c
    jp z,parse_bad               ; zero is not a listener port
    ld (selected_port),bc
    jp parse_token_loop

parse_skip_spaces:
    ld a,(hl)
    cp ' '
    jr z,parse_skip_one_space
    cp 9
    ret nz
parse_skip_one_space:
    inc hl
    jr parse_skip_spaces

parse_skip_token:
    ld a,(hl)
    or a
    ret z
    cp ' '
    ret z
    cp 9
    ret z
    inc hl
    jr parse_skip_token

; Compare the token at HL with the zero-terminated option at DE. Z means an
; exact token match. Both input pointers and BC are preserved.
parse_token_equals:
    push hl
    push de
    push bc
parse_token_compare:
    ld a,(de)
    or a
    jr z,parse_token_end
    ld c,a
    ld a,(hl)
    cp c
    jr nz,parse_token_no
    inc hl
    inc de
    jr parse_token_compare
parse_token_end:
    ld a,(hl)
    or a
    jr z,parse_token_yes
    cp ' '
    jr z,parse_token_yes
    cp 9
    jr z,parse_token_yes
parse_token_no:
    pop bc
    pop de
    pop hl
    ld a,1
    or a
    ret
parse_token_yes:
    pop bc
    pop de
    pop hl
    xor a
    ret

; Compare only the zero-terminated prefix at DE. Z means the token at HL
; begins with it. Both input pointers and BC are preserved.
parse_token_has_prefix:
    push hl
    push de
    push bc
parse_token_prefix_compare:
    ld a,(de)
    or a
    jr z,parse_token_prefix_yes
    ld c,a
    ld a,(hl)
    cp c
    jr nz,parse_token_prefix_no
    inc hl
    inc de
    jr parse_token_prefix_compare
parse_token_prefix_no:
    pop bc
    pop de
    pop hl
    ld a,1
    or a
    ret
parse_token_prefix_yes:
    pop bc
    pop de
    pop hl
    xor a
    ret

; Materialize exactly "ATQ0S41=0A<port>" in the audited 16-byte buffer. The
; builder runs before the resident probe and before any UART access.
build_listener_command:
    ld hl,command_listener_prefix
    ld de,command_listener_open
build_listener_prefix_loop:
    ld a,(hl)
    or a
    jr z,build_listener_port
    ld (de),a
    inc hl
    inc de
    jr build_listener_prefix_loop

build_listener_port:
    ld hl,(selected_port)
    xor a
    ld (port_format_started),a
    ld bc,10000
    call build_listener_decimal_place
    ld bc,1000
    call build_listener_decimal_place
    ld bc,100
    call build_listener_decimal_place
    ld bc,10
    call build_listener_decimal_place
    ld a,l                       ; the final remainder is 0..9
    add a,'0'
    ld (de),a
    inc de
    xor a
    ld (de),a                    ; also clears carry for parser success
    ret

build_listener_decimal_place:
    xor a
build_listener_decimal_loop:
    or a                         ; clear carry before each subtraction
    sbc hl,bc
    jr c,build_listener_decimal_done
    inc a
    jr build_listener_decimal_loop
build_listener_decimal_done:
    add hl,bc                    ; restore the first negative subtraction
    ld (port_digit),a
    ld a,(port_format_started)
    or a
    jr nz,build_listener_decimal_emit
    ld a,(port_digit)
    or a
    ret z                        ; suppress a leading zero
    ld a,1
    ld (port_format_started),a
build_listener_decimal_emit:
    ld a,(port_digit)
    add a,'0'
    ld (de),a
    inc de
    ret

strings_equal:
    ld a,(de)
    cp (hl)
    ret nz
    or a
    ret z
    inc de
    inc hl
    jr strings_equal

; --------------------------------------------------------------- TSR safety

; BADINIT must be the only owner of the UART.  A MemMan-resident MSXAI polls
; it from H.TIMI and would race command responses, so fail before touching the
; modem and ask the user to uninstall the agent first.
find_resident_agent:
    xor a
    ld d,'M'
    ld e,MEMMAN_INICHK
    call EXTBIO
    cp 'M'
    jr nz,find_resident_absent
    ld a,d
    cp MEMMAN_MINIMUM_MAJOR
    jr c,find_resident_absent
    jr nz,find_resident_compatible
    ld a,e
    cp MEMMAN_MINIMUM_MINOR
    jr c,find_resident_absent
find_resident_compatible:
    ld hl,memman_tsr_name
    ld d,'M'
    ld e,MEMMAN_GET_TSR_ID
    call EXTBIO
    ret
find_resident_absent:
    scf
    ret

; ---------------------------------------------------------------------- UART

uart_init_57600:
    ; Match the BaDCaT reference initialization order: hold RTS inactive while
    ; clearing/configuring the UART, then enable AFE+RTS only after 8N1 and the
    ; divisor are established.  This avoids exposing setup transients to the
    ; ESP UART on both AFE and older non-AFE boards.
    ; Keep DLAB setup atomic: an old H.TIMI/H.KEYI/FOSSIL hook must not touch
    ; ports 80h/81h while they temporarily select DLL/DLM instead of DATA/IER.
    di
    xor a
    out (UART_IER),a
    ld a,00Dh                    ; DTR|OUT1|OUT2, RTS inactive during setup
    out (UART_MCR),a
    ld a,087h                    ; FIFO on, clear RX/TX, 8-byte RX trigger
    out (UART_FCR),a
    ld a,UART_DIVISOR_57600
    call uart_set_baud_raw
    ld (current_divisor),a
    xor a
    out (UART_IER),a
    in a,(UART_LSR)
    ld (diagnostic_last_lsr),a
    cp 0FFh                      ; an unclaimed MSX I/O port commonly reads FF
    jr z,uart_init_missing
    ld a,02Fh                    ; DTR|RTS|OUT1|OUT2|automatic RTS/CTS
    out (UART_MCR),a
    ei
    or a
    ret
uart_init_missing:
    ei
    ld a,RESPONSE_UART_ERROR
    ld (response_status),a
    scf
    ret

uart_set_baud:
    ; Runtime divisor changes need the same atomic DLAB window as startup.
    di
    call uart_set_baud_raw
    ei
    ret

uart_set_baud_raw:
    push af
    ld a,083h                    ; DLAB, 8N1
    out (UART_LCR),a
    pop af
    out (UART_DATA),a            ; DLL
    push af
    xor a
    out (UART_IER),a             ; DLM while DLAB=1
    ld a,003h                    ; 8N1, DLAB clear
    out (UART_LCR),a
    pop af
    ret

uart_write:
    ld d,a
    ld bc,0
uart_write_wait:
    in a,(UART_LSR)
    ld (diagnostic_last_lsr),a
    cp 0FFh
    jr z,uart_write_failed
    and 020h                     ; THRE
    jr nz,uart_write_ready
    dec bc
    ld a,b
    or c
    jr nz,uart_write_wait
uart_write_failed:
    ld a,RESPONSE_UART_ERROR
    ld (response_status),a
    scf
    ret
uart_write_ready:
    ld a,d
    out (UART_DATA),a
    or a
    ret

uart_wait_empty:
    ld bc,0
uart_wait_empty_loop:
    in a,(UART_LSR)
    ld (diagnostic_last_lsr),a
    cp 0FFh
    jr z,uart_wait_empty_failed
    and 040h                     ; TEMT: FIFO and shift register both empty
    jr nz,uart_wait_empty_ready
    dec bc
    ld a,b
    or c
    jr nz,uart_wait_empty_loop
uart_wait_empty_failed:
    ld a,RESPONSE_UART_ERROR
    ld (response_status),a
    scf
    ret
uart_wait_empty_ready:
    or a
    ret

drain_input:
    ld b,0                       ; bounded: at most 256 stale bytes
drain_input_loop:
    in a,(UART_LSR)
    ld (diagnostic_last_lsr),a
    cp 0FFh
    jr z,drain_input_failed
    and 001h
    jr z,drain_input_done
    in a,(UART_DATA)
    djnz drain_input_loop
drain_input_done:
    or a
    ret
drain_input_failed:
    ld a,RESPONSE_UART_ERROR
    ld (response_status),a
    scf
    ret

synchronize_command_mode:
    ; Complete any partial line left by a wrong-speed probe.  The two empty
    ; lines are session input only; after a short bounded settle their output
    ; is discarded before the real bootstrap command begins.
    ld a,13
    call uart_write
    ret c
    ld a,13
    call uart_write
    ret c
    call uart_wait_empty
    ret c
    ld b,2
    call wait_ticks
    jp drain_input

wait_ticks:
    ; B is a modulo-safe delay shorter than 256 JIFFYs.
    ld a,(JIFFY)
    ld c,a
wait_ticks_loop:
    halt
    ld a,(JIFFY)
    sub c
    cp b
    jr c,wait_ticks_loop
    ret

send_command:
    ld (diagnostic_command),hl
    call drain_input
    ret c
send_command_loop:
    ld a,(hl)
    or a
    jr z,send_command_cr
    call uart_write
    ret c
    inc hl
    jr send_command_loop
send_command_cr:
    ld a,13
    jp uart_write

change_runtime_baud:
    ; ZiModem flushes and waits roughly 500 ms before applying ATB.  Suppress
    ; that transition's response, switch the local DLL after TEMT, then remain
    ; completely silent for at least 40 JIFFYs before the validating bootstrap.
    ; Sending an immediate AT here races the firmware and loses deterministically
    ; on the physical BaDCaT.
    push af
    cp UART_DIVISOR_115200
    ld hl,command_b115200
    jr z,change_runtime_send
    ld hl,command_b57600
change_runtime_send:
    call send_command
    jr c,change_runtime_failed_pop
    call uart_wait_empty
    jr c,change_runtime_failed_pop
    pop af
    call uart_set_baud
    ld (current_divisor),a
    ld b,BAUD_SETTLE_TICKS
    call wait_ticks
    call drain_input
    ret c
    ld hl,command_bootstrap
    jp run_visible_command
change_runtime_failed_pop:
    pop af
    scf
    ret

restore_visible_57600:
    ld a,(current_divisor)
    cp UART_DIVISOR_115200
    jr nz,restore_visible_command
    ld hl,command_b57600
    call send_command
    call uart_wait_empty
    ld a,UART_DIVISOR_57600
    call uart_set_baud
    ld (current_divisor),a
    ld b,BAUD_SETTLE_TICKS
    call wait_ticks
    call drain_input
restore_visible_command:
    ld hl,command_visible
    call send_command
    call uart_wait_empty
    ret

; --------------------------------------------------------------- AT responses

run_visible_command:
    call response_reset
    call send_command
    jp c,response_uart_failed
    ld a,(JIFFY)
    ld (response_start),a

response_wait:
    ; Drain the complete FIFO before yielding to DOS/interrupt timing.  Printing
    ; while bytes are arriving is too slow and can overrun a 16-byte UART FIFO.
response_drain_fifo:
    in a,(UART_LSR)
    ld (diagnostic_last_lsr),a
    ld b,a
    cp 0FFh
    jp z,response_uart_failed
    and LSR_ERROR_MASK
    ld c,a
    ; Start the line-error quiet interval on the first observed status bit,
    ; including the rare case where the UART reports it with DR clear.
    ld a,(response_lsr_errors)
    or a
    jr nz,response_accumulate_lsr_errors
    ld a,c
    or a
    jr z,response_accumulate_lsr_errors
    ld a,(JIFFY)
    ld (response_pending_start),a
response_accumulate_lsr_errors:
    ld a,(response_lsr_errors)
    or c
    ld (response_lsr_errors),a
    ld a,b
    and 001h
    jr z,response_no_byte_or_error
    in a,(UART_DATA)
    push af
    ld hl,(response_rx_count)
    inc hl
    ld (response_rx_count),hl
    ld de,RESPONSE_STREAM_LIMIT
    or a
    sbc hl,de
    jr c,response_rx_within_limit
    pop af
    jp response_stream_failed
response_rx_within_limit:
    pop af
    call response_store_character
    ld c,a
    ; Once a terminal token is pending, measure the quiet interval from the
    ; most recently received byte (normally the LF after its CR), not merely
    ; from the token itself.
    ld a,(response_pending)
    ld b,a
    ld a,(response_lsr_errors)
    or b
    jr z,response_received_not_pending
    ld a,(JIFFY)
    ld (response_pending_start),a
response_received_not_pending:
    ld a,(response_lsr_errors)
    or a
    ; Once any received character is corrupt, ignore line tokens but keep
    ; draining the FIFO.  Reporting through DOS before it is empty can itself
    ; cause an overrun and destroy the evidence we need to diagnose the link.
    jr nz,response_drain_fifo
    ld a,c
    or a
    jr z,response_drain_fifo
    ld (response_pending),a
    ld a,(JIFFY)
    ld (response_pending_start),a
    jr response_drain_fifo

response_no_byte_or_error:
    ld a,(response_lsr_errors)
    or a
    jr z,response_no_line_error
    ; Framing/parity/overrun may be observed between characters.  Require one
    ; complete quiet JIFFY after the last received byte before invoking DOS.
    call response_quiet_elapsed
    jp nc,response_line_failed
    jr response_pending_wait
response_no_line_error:
    ld a,(response_pending)
    or a
    jr z,response_no_byte
    ; A terminal line normally ends CR/LF.  Require one quiet JIFFY after the
    ; recognized CR and keep polling during it, so the trailing LF/diagnostic
    ; bytes are consumed before any slow DOS console call.
    call response_quiet_elapsed
    jr c,response_pending_wait
    ld a,(response_pending)
    cp 1
    jr z,response_succeeded
    jr response_modem_failed
response_pending_wait:
    halt
    jp response_wait

response_quiet_elapsed:
    ; Carry means fewer than RESPONSE_QUIET_TICKS changes have elapsed.
    ld a,(JIFFY)
    ld b,a
    ld a,(response_pending_start)
    sub b
    neg
    cp RESPONSE_QUIET_TICKS
    ret
response_no_byte:
    call response_check_deadline
    jp nc,response_timed_out
    halt
    jp response_wait

response_check_deadline:
    ; Carry means there is still time; NC means the 180-JIFFY bound expired.
    ld a,(JIFFY)
    ld b,a
    ld a,(response_start)
    sub b
    neg
    cp RESPONSE_TIMEOUT
    ret

response_succeeded:
    call print_response_buffer
    ld a,RESPONSE_OK
    ld (response_status),a
    or a
    ret
response_timed_out:
    ld a,RESPONSE_TIMEOUT_ERROR
    ld (response_status),a
    call diagnostic_report_once
    scf
    ret
response_modem_failed:
    ld a,RESPONSE_MODEM_ERROR
    ld (response_status),a
    call diagnostic_report_once
    scf
    ret
response_line_failed:
    ld a,RESPONSE_LINE_ERROR
    ld (response_status),a
    call diagnostic_report_once
    scf
    ret
response_uart_failed:
    ld a,RESPONSE_UART_ERROR
    ld (response_status),a
    call diagnostic_report_once
    scf
    ret
response_stream_failed:
    ld a,RESPONSE_STREAM_ERROR
    ld (response_status),a
    call diagnostic_report_once
    scf
    ret

response_reset:
    ld hl,response_buffer
    ld (response_write_pointer),hl
    xor a
    ld (response_line_length),a
    ld (response_line_overflow),a
    ld (response_lsr_errors),a
    ld (response_pending),a
    ld (response_pending_start),a
    ld hl,0
    ld (response_rx_count),hl
    ret

response_store_character:
    ld b,a
    ld hl,(response_write_pointer)
    ld de,response_buffer_end
    ld a,h
    cp d
    jr c,response_store_in_buffer
    jr nz,response_store_line
    ld a,l
    cp e
    jr nc,response_store_line
response_store_in_buffer:
    ld (hl),b
    inc hl
    ld (response_write_pointer),hl

response_store_line:
    ld a,b
    cp 13
    jr z,response_finish_line
    cp 10
    jr z,response_finish_line
    ld a,(response_line_overflow)
    or a
    jr nz,response_line_continue
    ld a,(response_line_length)
    cp RESPONSE_LINE_CAPACITY
    jr nc,response_line_mark_overflow
    ld l,a
    ld h,0
    ld de,response_line_buffer
    add hl,de
    ld (hl),b
    ld a,(response_line_length)
    inc a
    ld (response_line_length),a
response_line_continue:
    xor a
    ret
response_line_mark_overflow:
    ld a,1
    ld (response_line_overflow),a
    xor a
    ret

response_finish_line:
    ld a,(response_line_length)
    or a
    jr z,response_line_reset_continue
    ld a,(response_line_overflow)
    or a
    jr nz,response_line_reset_continue
    ld a,(response_line_length)
    ld l,a
    ld h,0
    ld de,response_line_buffer
    add hl,de
    ld (hl),0
    ld hl,response_line_buffer
    ld de,response_token_ok
    call strings_equal
    jr z,response_line_ok
    ld hl,response_line_buffer
    ld de,response_token_error
    call strings_equal
    jr z,response_line_error
    ld hl,response_line_buffer
    ld de,response_token_no_carrier
    call strings_equal
    jr z,response_line_error
response_line_reset_continue:
    call response_line_reset
    xor a
    ret
response_line_ok:
    call response_line_reset
    ld a,1
    ret
response_line_error:
    call response_line_reset
    ld a,2
    ret

response_line_reset:
    xor a
    ld (response_line_length),a
    ld (response_line_overflow),a
    ret

print_response_buffer:
    ld hl,response_buffer
print_response_loop:
    ld de,(response_write_pointer)
    ld a,h
    cp d
    jr nz,print_response_character
    ld a,l
    cp e
    ret z
print_response_character:
    ld a,(hl)
    inc hl
    call print_character
    jr print_response_loop

; --------------------------------------------------------------- diagnostics

diagnostic_begin_stage:
    ld (diagnostic_stage),hl
    xor a
    ld (failure_reported),a
    ld (response_status),a
    ld (response_lsr_errors),a
    ld (diagnostic_last_lsr),a
    ld hl,0
    ld (diagnostic_command),hl
    ld (response_rx_count),hl
    ld hl,response_buffer
    ld (response_write_pointer),hl
    ret

diagnostic_report_once:
    ld a,(failure_reported)
    or a
    ret nz
    inc a
    ld (failure_reported),a

    ld de,message_diagnostic_stage
    call print_string
    ld hl,(diagnostic_stage)
    call print_c_string

    ld de,message_diagnostic_baud
    call print_string
    ld a,(current_divisor)
    cp UART_DIVISOR_115200
    ld de,message_diagnostic_baud_57600
    jr nz,diagnostic_baud_ready
    ld de,message_diagnostic_baud_115200
diagnostic_baud_ready:
    call print_string

    ld hl,(diagnostic_command)
    ld a,h
    or l
    jr z,diagnostic_without_command
    push hl
    ld de,message_diagnostic_command
    call print_string
    pop hl
    call print_c_string
diagnostic_without_command:
    ld de,message_diagnostic_reason
    call print_string
    ld a,(response_status)
    cp RESPONSE_TIMEOUT_ERROR
    ld hl,reason_timeout
    jr z,diagnostic_reason_ready
    cp RESPONSE_MODEM_ERROR
    ld hl,reason_modem
    jr z,diagnostic_reason_ready
    cp RESPONSE_LINE_ERROR
    ld hl,reason_line
    jr z,diagnostic_reason_ready
    cp RESPONSE_STREAM_ERROR
    ld hl,reason_stream
    jr z,diagnostic_reason_ready
    ld hl,reason_uart
diagnostic_reason_ready:
    call print_c_string

    ld de,message_diagnostic_rx
    call print_string
    ld hl,(response_rx_count)
    call print_hex_word
    ld de,message_diagnostic_hex
    call print_string
    call print_response_sample_hex
    ld de,message_diagnostic_lsr
    call print_string
    ld a,(diagnostic_last_lsr)
    call print_hex_byte
    ld de,message_diagnostic_errors
    call print_string
    ld a,(response_lsr_errors)
    call print_hex_byte
    ld de,message_crlf
    jp print_string

print_response_sample_hex:
    ld hl,response_buffer
    ld de,(response_write_pointer)
    ld a,h
    cp d
    jr nz,print_response_sample_start
    ld a,l
    cp e
    jr nz,print_response_sample_start
    ld de,message_none
    jp print_string
print_response_sample_start:
    ld b,DIAGNOSTIC_SAMPLE_SIZE
print_response_sample_loop:
    ld a,' '
    call print_character
    ld a,(hl)
    call print_hex_byte
    inc hl
    ld a,h
    cp d
    jr nz,print_response_sample_next
    ld a,l
    cp e
    ret z
print_response_sample_next:
    djnz print_response_sample_loop
    ret

print_c_string:
    ld a,(hl)
    or a
    ret z
    inc hl
    call print_character
    jr print_c_string

print_hex_word:
    ld a,h
    call print_hex_byte
    ld a,l
    jp print_hex_byte

print_hex_byte:
    push af
    rrca
    rrca
    rrca
    rrca
    and 00Fh
    call print_hex_nibble
    pop af
    and 00Fh
print_hex_nibble:
    add a,'0'
    cp '9' + 1
    jr c,print_hex_digit
    add a,7
print_hex_digit:
    jp print_character

; ----------------------------------------------------------------------- DOS

print_string:
    ld c,DOS_PRINT_STRING
    jp BDOS

print_character:
    push af
    push bc
    push de
    push hl
    ld e,a
    ld c,DOS_CONSOLE_OUTPUT
    call BDOS
    pop hl
    pop de
    pop bc
    pop af
    ret

; ---------------------------------------------------------------------- data

selected_divisor:
    db UART_DIVISOR_57600
selected_port:
    dw DEFAULT_LISTENER_PORT
current_divisor:
    db UART_DIVISOR_57600
baud_option_seen:
    db 0
port_option_seen:
    db 0
port_digit_count:
    db 0
port_digit:
    db 0
port_format_started:
    db 0
response_status:
    db RESPONSE_OK
response_start:
    db 0
response_write_pointer:
    dw response_buffer
response_rx_count:
    dw 0
response_line_length:
    db 0
response_line_overflow:
    db 0
response_lsr_errors:
    db 0
response_pending:
    db 0
response_pending_start:
    db 0
diagnostic_last_lsr:
    db 0
diagnostic_stage:
    dw stage_uart_init_57600
diagnostic_command:
    dw 0
failure_reported:
    db 0
command_buffer:
    ds COMMAND_BUFFER_CAPACITY,0
command_buffer_end:
response_line_buffer:
    ds RESPONSE_LINE_CAPACITY + 1,0

memman_tsr_name:
    db "MSXAI MCP1  "           ; exactly 12 bytes, padded for GetTsrID

option_57600:
    db "/57600",0
option_115200:
    db "/115200",0
option_port_prefix:
    db "/PORT:",0

initial_command_table:
    dw command_n0,command_s62_0,command_s63_0
    dw command_s0_1,0
listener_command_table:
    dw command_i2,0

command_bootstrap:
    db "ATQ0V1E0R1F0",0
command_n0:
    db "ATN0",0
command_s62_0:
    db "ATS62=0",0
command_s63_0:
    db "ATS63=0",0
command_s0_1:
    db "ATS0=1",0
command_b57600:
    db "ATQ1B57600",0
command_b115200:
    db "ATQ1B115200",0
command_listener_prefix:
    db "ATQ0S41=0A",0
command_listener_open:
    ds LISTENER_PREFIX_LENGTH,0
command_listener_port_text:
    ds LISTENER_PORT_CAPACITY,0
command_listener_open_end:
command_stream_commit:
    db "ATHS41=1Q1",0
command_i2:
    db "ATI2",0
command_visible:
    db "ATQ0V1E1R1F0",0

response_token_ok:
    db "OK",0
response_token_error:
    db "ERROR",0
response_token_no_carrier:
    db "NO CARRIER",0

stage_uart_init_57600:
    db "UART init 57600",0
stage_sync_57600:
    db "command sync 57600",0
stage_probe_57600:
    db "bootstrap 57600",0
stage_sync_115200:
    db "command sync 115200",0
stage_probe_115200:
    db "bootstrap 115200",0
stage_runtime_setup:
    db "runtime setup",0
stage_baud_change:
    db "runtime baud change",0
stage_ip_query:
    db "IP query",0
stage_listener_open:
    db "listener open",0
stage_listener_commit:
    db "listener commit",0

reason_timeout:
    db "response timeout",0
reason_modem:
    db "modem ERROR/NO CARRIER",0
reason_line:
    db "UART RX line-status error",0
reason_stream:
    db "continuous RX/stream limit",0
reason_uart:
    db "UART failure",0

message_banner:
    db 13,10,"BaDCaT MCP listener initializer",13,10,"Baud: $"
message_57600:
    db "57600",13,10,"$"
message_115200:
    db "115200 (temporary)",13,10,"$"
message_retry_115200:
    db 13,10,"No valid reply at 57600; probing 115200.",13,10,"$"
message_success_prefix:
    db 13,10,"Listener $"
message_success_suffix:
    db " ready; start matching MSXAI now.",13,10,"$"
message_failed:
    db 13,10,"BaDCaT initialization failed; power-cycle restores saved state.",13,10,"$"
message_usage:
    db 13,10,"Usage: BADINIT [/57600 | /115200]",13,10
    db "       [/PORT:<1..65535>]",13,10,"$"
message_resident_active:
    db 13,10,"MSXAI resident active; run MSXAI /UNINSTALL first.",13,10,"$"
message_diagnostic_stage:
    db 13,10,"Stage: $"
message_diagnostic_baud:
    db 13,10,"Baud: $"
message_diagnostic_baud_57600:
    db "57600$"
message_diagnostic_baud_115200:
    db "115200$"
message_diagnostic_command:
    db 13,10,"Command: $"
message_diagnostic_reason:
    db 13,10,"Reason: $"
message_diagnostic_rx:
    db 13,10,"RX bytes (hex): $"
message_diagnostic_hex:
    db "  RX hex:$"
message_diagnostic_lsr:
    db 13,10,"LSR last/errors (hex): $"
message_diagnostic_errors:
    db "/$"
message_none:
    db " --$"
message_crlf:
    db 13,10,"$"

response_buffer:
    ds 512,0
response_buffer_end:

badinit_end:
