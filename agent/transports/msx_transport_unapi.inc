; Generic TCP/IP UNAPI transport.
;
; The MSX is the TCP server: TCP_OPEN creates a passive, resident connection on
; the configured local port (6603 by default). The host connects to the Pico+
; IPv4 address. No Pico+ firmware change or product-specific memory I/O is used.
;
; MemMan maps this agent into page 1. A page-1 UNAPI implementation temporarily
; hides the agent while CALSLT/RAM-helper dispatch is active, and TCP/IP UNAPI
; clients must not supply page-1 buffers. RX/TX therefore stage at most 64 bytes
; immediately below the current page-3 hook stack, then copy to resident rings
; after the API restores the agent mapping. Nested interrupts grow below that
; staging window, not through it.
;
; TCP/IP UNAPI does not define interrupt-context reentrancy. This resident owns
; its connection exclusively and the core's in_hook/xfer guards prevent its own
; nested calls, but physical validation is still required for each implementation.

UNAPI_ID:                     equ 2
UNAPI_CONTROL_LEVEL:          equ 1
UNAPI_FLAGS:                  equ TRANSPORT_FLAG_TIMI_ONLY | TRANSPORT_FLAG_FRAME_WAKE_ACK
UNAPI_STATE_SIZE:             equ 0       ; state is dedicated below, not overlaid

UNAPI_ARG:                    equ 0F847h
UNAPI_EXTBIO_MAGIC:           equ 02222h
UNAPI_DEFAULT_PORT:           equ 6603
UNAPI_BLOCK_SIZE:             equ 64
UNAPI_RELISTEN_DELAY:         equ 30
UNAPI_POLL_DIVIDER:           equ 4
UNAPI_FLUSH_RETRIES:          equ 16

UNAPI_MODE_NONE:              equ 0
UNAPI_MODE_DIRECT:            equ 1       ; entry outside page 1 (bit 15 set)
UNAPI_MODE_ROM:               equ 2       ; page-1 ROM/non-mapped RAM via CALSLT
UNAPI_MODE_MAPPED:            equ 3       ; page-1 mapped RAM via RAM helper

UNAPI_TCP_GET_CAPAB:          equ 1
UNAPI_TCP_OPEN:               equ 13
UNAPI_TCP_ABORT:              equ 15
UNAPI_TCP_STATE:              equ 16
UNAPI_TCP_SEND:               equ 17
UNAPI_TCP_RCV:                equ 18
UNAPI_TCP_WAIT:               equ 29
UNAPI_TCP_LISTEN:             equ 1
UNAPI_TCP_ESTABLISHED:        equ 4
UNAPI_TCP_CLOSE_WAIT:         equ 7
UNAPI_ERR_OK:                 equ 0
UNAPI_ERR_NO_CONN:            equ 11
UNAPI_ERR_BUFFER:             equ 13

UNAPI_INIT_NOT_FOUND:         equ 080h
UNAPI_INIT_NO_PASSIVE:        equ 081h
UNAPI_INIT_NO_RAM_HELPER:     equ 082h
UNAPI_INIT_BAD_PORT:          equ 083h

; ---------------------------------------------------------------- lifecycle --

unapi_init:
    push bc
    push de
    push hl
    push ix
    push iy
    call unapi_init_inner
    ld (unapi_last_error),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret

unapi_init_inner:
    call unapi_prepare_current_port
    or a
    ret nz
if MSXAI_TSR_BUILD
    ; TsrInit runs inside TL's MemMan install chain, before the post-TL MP.COM
    ; command can pass its explicit port. Discover and validate UNAPI now, but
    ; defer this one initial potentially blocking TCP_OPEN. The foreground A6
    ; TsrCall uses unapi_init_current_port and therefore always opens directly.
    ld a,(unapi_defer_first_open)
    or a
    jr z,unapi_init_open_now
    xor a
    ld (unapi_defer_first_open),a
    ret
unapi_init_open_now:
endif
    jp unapi_open_listener

; Explicit-port and rollback helper used only by foreground TSR reconfiguration.
; It never honors the TsrInit-only defer flag: a successful return means the
; requested binary port has actually been passed to TCP_OPEN.
unapi_init_current_port:
    push bc
    push de
    push hl
    push ix
    push iy
    call unapi_init_current_port_inner
    ld (unapi_last_error),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret
unapi_init_current_port_inner:
    call unapi_prepare_current_port
    or a
    ret nz
    jp unapi_open_listener

unapi_prepare_current_port:
    call unapi_clear_runtime
    ld hl,(unapi_listen_port)
    ld a,h
    or l
    ld a,UNAPI_INIT_BAD_PORT
    ret z
    ld a,h
    and l
    inc a                           ; reject FFFFh (UNAPI random-port sentinel)
    ld a,UNAPI_INIT_BAD_PORT
    ret z
    call unapi_discover
    or a
    ret nz
    ld a,1
    ld (unapi_initialized),a
    xor a
    ret

unapi_restore:
    push bc
    push de
    push hl
    push ix
    push iy
    ld a,(unapi_initialized)
    or a
    jr z,unapi_restore_clear
    call unapi_abort_current
    or a
    jr nz,unapi_restore_finish
unapi_restore_clear:
    call unapi_clear_runtime
    xor a
unapi_restore_finish:
    ld (unapi_last_error),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret

unapi_clear_runtime:
    xor a
    ld hl,unapi_runtime_start
    ld de,unapi_runtime_start + 1
    ld bc,unapi_runtime_end - unapi_runtime_start - 1
    ld (hl),a
    ldir
    ret

; --------------------------------------------------------------- discovery --

unapi_discover:
    call unapi_copy_api_id
    xor a
    ld b,a
    ld de,UNAPI_EXTBIO_MAGIC
    call EXTBIO
    di
    ld a,b
    or a
    jr z,unapi_discover_not_found
    ld (unapi_implementation_count),a
    ld a,1
    ld (unapi_implementation_index),a
    xor a
    ld (unapi_discovery_reason),a
unapi_discover_loop:
    call unapi_copy_api_id
    ld a,(unapi_implementation_index)
    ld de,UNAPI_EXTBIO_MAGIC
    call EXTBIO
    di
    call unapi_prepare_candidate
    or a
    jr nz,unapi_discover_next

    xor a                           ; mandatory UNAPI_GET_INFO
    call unapi_call
    or a
    jr nz,unapi_discover_next
    ld a,d                          ; TCP/IP API major version
    or a
    jr z,unapi_discover_next

    ld a,UNAPI_TCP_GET_CAPAB
    ld b,1                          ; capabilities/features block 1
    call unapi_call
    or a
    jr nz,unapi_discover_next
    bit 5,l                         ; passive TCP, unspecified remote socket
    jr z,unapi_discover_no_passive
    ld a,d
    and 008h                        ; feature bit 11: TCP_OPEN may block
    ld (unapi_open_blocking),a
    xor a
    ret

unapi_discover_no_passive:
    ld a,UNAPI_INIT_NO_PASSIVE
    ld (unapi_discovery_reason),a
unapi_discover_next:
    ld a,(unapi_implementation_index)
    inc a
    ld (unapi_implementation_index),a
    ld b,a
    ld a,(unapi_implementation_count)
    cp b
    jr nc,unapi_discover_loop
    ld a,(unapi_discovery_reason)
    or a
    ret nz
unapi_discover_not_found:
    ld a,UNAPI_INIT_NOT_FOUND
    ret

unapi_copy_api_id:
    ld hl,unapi_api_id_string
    ld de,UNAPI_ARG
    ld bc,7
    ldir
    ret

; Input from discovery: A=slot, B=segment (FFh for ROM), HL=entry.
unapi_prepare_candidate:
    ld (unapi_entry),hl
    ld (unapi_slot_iy + 1),a
    ld (unapi_mapper_iy + 1),a
    ld a,b
    ld (unapi_segment),a
    ld (unapi_mapper_iy),a
    ld a,h
    cp 0C0h                         ; only page 3 is directly callable
    jr nc,unapi_prepare_direct
    ld a,b
    cp 0FFh
    jr z,unapi_prepare_rom

    ; The standard RAM helper accepts mapped targets in page 1 only. ROM
    ; implementations in pages 0-2 use CALSLT and were handled above.
    ld a,h
    cp 040h
    jr c,unapi_prepare_invalid
    cp 080h
    jr nc,unapi_prepare_invalid

    ; A mapped-RAM implementation is callable only through the standard page-3
    ; RAM helper. Query it for every such candidate; EXTBIO may have changed as
    ; implementations were installed after the agent image was built.
    ld a,0FFh
    ld de,UNAPI_EXTBIO_MAGIC
    ld hl,0
    call EXTBIO
    di
    ld a,h
    or l
    jr z,unapi_prepare_no_helper
    ld (unapi_call_map),hl
    ld (unapi_mapped_dispatch + 1),hl
    ld a,UNAPI_MODE_MAPPED
    ld (unapi_mode),a
    xor a
    ret
unapi_prepare_no_helper:
    ld a,UNAPI_INIT_NO_RAM_HELPER
    ld (unapi_discovery_reason),a
    ret
unapi_prepare_direct:
    ld (unapi_direct_dispatch + 1),hl
    ld a,UNAPI_MODE_DIRECT
    ld (unapi_mode),a
    xor a
    ret
unapi_prepare_rom:
    ld a,UNAPI_MODE_ROM
    ld (unapi_mode),a
    xor a
    ret
unapi_prepare_invalid:
    ld a,UNAPI_INIT_NOT_FOUND
    ret

; Invoke the selected implementation while respecting all three UNAPI locations.
; The caller saves any registers that its own ABI promises to preserve.
unapi_call:
    push af
    ld a,(unapi_mode)
    cp UNAPI_MODE_DIRECT
    jr z,unapi_call_direct
    cp UNAPI_MODE_ROM
    jr z,unapi_call_rom
    cp UNAPI_MODE_MAPPED
    jr z,unapi_call_mapped
    pop af
    ld a,UNAPI_INIT_NOT_FOUND
    ret
unapi_call_direct:
    pop af
unapi_direct_dispatch:
    call 0000h
    di
    ret
unapi_call_rom:
    pop af
    ld ix,(unapi_entry)
    ld iy,(unapi_slot_iy)
    call CALSLT
    di
    ret
unapi_call_mapped:
    pop af
    ld ix,(unapi_entry)
    ld iy,(unapi_mapper_iy)
unapi_mapped_dispatch:
    call 0000h
    di
    ret

; ----------------------------------------------------- connection lifecycle --

unapi_open_listener:
    ld (unapi_saved_sp),sp
    ld hl,0
    add hl,sp
    ld de,13
    or a
    sbc hl,de
    ld sp,hl
    ld (unapi_io_pointer),hl
    push hl
    pop de
    inc de
    ld bc,12
    xor a
    ld (hl),a
    ldir
    ld hl,(unapi_io_pointer)
    ld de,6
    add hl,de
    ld de,(unapi_listen_port)
    ld (hl),e
    inc hl
    ld (hl),d
    inc hl
    inc hl
    inc hl                           ; timeout at +8/+9 remains zero
    ld (hl),003h                    ; +10: passive | resident
    ld hl,(unapi_io_pointer)
    ld a,UNAPI_TCP_OPEN
    call unapi_call
    ld sp,(unapi_saved_sp)
    ld (unapi_last_error),a
    or a
    ret nz
    ld a,b
    or a
    jr nz,unapi_open_listener_valid
    ld a,UNAPI_ERR_NO_CONN         ; success with handle zero is not usable
    ld (unapi_last_error),a
    ret
unapi_open_listener_valid:
    ld (unapi_connection),a
    ld a,UNAPI_TCP_LISTEN
    ld (unapi_connection_state),a
    xor a
    ld (unapi_retry_count),a
    ld (unapi_poll_skip),a
    ret

unapi_abort_current:
    ld a,(unapi_connection)
    or a
    jr z,unapi_abort_deferred
    ld b,a
    ld a,UNAPI_TCP_ABORT
    call unapi_call
    cp UNAPI_ERR_OK
    jr z,unapi_abort_current_clear
    cp UNAPI_ERR_NO_CONN
    ret nz                         ; preserve both handles for a later retry
unapi_abort_current_clear:
    xor a
    ld (unapi_connection),a
    ld (unapi_connection_state),a
unapi_abort_deferred:
    ld a,(unapi_cleanup_connection)
    or a
    jr z,unapi_abort_complete
    ld b,a
    ld a,UNAPI_TCP_ABORT
    call unapi_call
    cp UNAPI_ERR_OK
    jr z,unapi_abort_deferred_clear
    cp UNAPI_ERR_NO_CONN
    ret nz                         ; keep this cleanup handle for retry
unapi_abort_deferred_clear:
    xor a
    ld (unapi_cleanup_connection),a
unapi_abort_complete:
    xor a
    ret

unapi_drop_connection:
    ; Never ABORT or OPEN from the parser's stack.  Pico/Pico+ advertises a
    ; blocking TCP_OPEN and its hardware transaction can wait far longer than
    ; a BIOS hook may.  Move the handle to a cleanup slot, mark the stream
    ; boundary, and let the core unwind before service considers relistening.
    ld a,(unapi_connection)
    or a
    jr z,unapi_drop_no_live_handle
    ld (unapi_cleanup_connection),a
unapi_drop_no_live_handle:
    xor a
    ld (unapi_connection),a
    ld (unapi_connection_state),a
    ld (unapi_rx_count),a
    ld (unapi_rx_position),a
    ld (unapi_tx_count),a
    ld a,1
    ld (transport_session_lost),a
    ld a,UNAPI_RELISTEN_DELAY
    ld (unapi_retry_count),a
    ret

unapi_service:
    push bc
    push de
    push hl
    push ix
    push iy
    ld a,(unapi_busy)
    or a
    jr nz,unapi_service_done
    ld a,1
    ld (unapi_busy),a
    call unapi_service_inner
    ld (unapi_last_error),a
    xor a
    ld (unapi_busy),a
unapi_service_done:
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret

unapi_service_inner:
    ld a,(unapi_initialized)
    or a
    ret z
    ; Foreground monitor mode keeps interrupts disabled between calls. Give a
    ; software TCP/IP implementation one processing step; externally assisted
    ; implementations such as Pico/Pico+ define WAIT as a harmless no-op.
    ld a,(runtime_mode)
    cp RUNTIME_MONITOR
    jr nz,unapi_service_after_wait
    ld a,UNAPI_TCP_WAIT
    call unapi_call
unapi_service_after_wait:
    ld a,(unapi_connection)
    or a
    jr nz,unapi_service_have_connection
    ld a,(unapi_retry_count)
    or a
    jr z,unapi_service_relisten
    dec a
    ld (unapi_retry_count),a
    xor a
    ret
unapi_service_relisten:
    ld a,(transport_session_lost)
    or a
    ret nz                           ; old parser stack has not unwound yet
    ; Lifecycle calls are foreground-only for generic UNAPI implementations.
    ; Pico/Pico+ explicitly reports blocking OPEN, and even an implementation
    ; that clears that feature bit does not promise interrupt reentrancy.
    ld a,(in_hook)
    or a
    ret nz
    call unapi_abort_current
    or a
    jr nz,unapi_service_relisten_abort_failed
    call unapi_open_listener
    ret z
    jr unapi_service_relisten_failed
unapi_service_relisten_abort_failed:
    ld (unapi_last_error),a
unapi_service_relisten_failed:
    ld a,UNAPI_RELISTEN_DELAY
    ld (unapi_retry_count),a
    ld a,(unapi_last_error)
    ret

unapi_service_have_connection:
    ld a,(unapi_poll_skip)
    or a
    jr z,unapi_service_poll
    dec a
    ld (unapi_poll_skip),a
    xor a
    ret
unapi_service_poll:
    ld a,UNAPI_POLL_DIVIDER
    ld (unapi_poll_skip),a
    ld a,(unapi_connection)
    ld b,a
    ld hl,0                         ; no page-1 information block
    ld a,UNAPI_TCP_STATE
    call unapi_call
    or a
    jr nz,unapi_service_state_error
    ld a,b
    ld (unapi_connection_state),a
    ld (unapi_pending_rx),hl
    ld a,b
    cp UNAPI_TCP_ESTABLISHED
    jr z,unapi_service_connected
    cp UNAPI_TCP_CLOSE_WAIT
    jr z,unapi_service_connected
    cp UNAPI_TCP_LISTEN
    jr z,unapi_service_ok
    cp 2                            ; SYN-SENT
    jr z,unapi_service_ok
    cp 3                            ; SYN-RECEIVED
    jr z,unapi_service_ok
    call unapi_drop_connection
    xor a
    ret
unapi_service_state_error:
    ld (unapi_last_error),a
    cp UNAPI_ERR_NO_CONN
    jr nz,unapi_service_return_error
    call unapi_drop_connection
    xor a
    ret
unapi_service_return_error:
    ret

unapi_service_connected:
    ld a,(unapi_tx_count)
    or a
    call nz,unapi_send_pending
    or a
    jr nz,unapi_service_connected_error
    ld a,(unapi_rx_count)
    or a
    jr nz,unapi_service_ok
    ld hl,(unapi_pending_rx)
    ld a,h
    or l
    call nz,unapi_receive_block
    or a
    jr nz,unapi_service_connected_error
    ld a,(unapi_rx_count)
    or a
    jr nz,unapi_service_ok
    ld a,(unapi_connection_state)
    cp UNAPI_TCP_CLOSE_WAIT
    jr nz,unapi_service_ok
    ; FIN has arrived and every advertised byte has been staged and consumed.
    ; Abort this resident handle and create a fresh passive listener later.
    call unapi_drop_connection
    xor a
    ret
unapi_service_connected_error:
    cp UNAPI_ERR_NO_CONN
    ret nz
    call unapi_drop_connection
    xor a
    ret
unapi_service_ok:
    xor a
    ret

; ---------------------------------------------------------- buffered bytes --

unapi_rx_ready:
    ld a,(unapi_rx_count)
    ret

unapi_read:
    push de
    push hl
    ld a,(unapi_rx_count)
    or a
    jr z,unapi_read_empty
    ld a,(unapi_rx_position)
    ld e,a
    ld d,0
    ld hl,unapi_rx_buffer
    add hl,de
    ld a,(hl)
    push af
    ld a,(unapi_rx_position)
    inc a
    ld (unapi_rx_position),a
    ld a,(unapi_rx_count)
    dec a
    ld (unapi_rx_count),a
    jr nz,unapi_read_restore
    ld (unapi_rx_position),a
unapi_read_restore:
    pop af
    pop hl
    pop de
    ret
unapi_read_empty:
    xor a
    pop hl
    pop de
    ret

unapi_tx_ready:
    ld a,(unapi_connection_state)
    cp UNAPI_TCP_ESTABLISHED
    jr z,unapi_tx_ready_connected
    cp UNAPI_TCP_CLOSE_WAIT
    jr z,unapi_tx_ready_connected
    xor a
    ret
unapi_tx_ready_connected:
    ld a,(unapi_tx_count)
    cp UNAPI_BLOCK_SIZE
    jr nc,unapi_tx_not_ready
    ld a,1
    ret
unapi_tx_not_ready:
    xor a
    ret

unapi_write:
    push bc
    push de
    push hl
    push ix
    push iy
    push af
    ld a,(unapi_tx_count)
    ld c,a
    ld b,0
    ld hl,unapi_tx_buffer
    add hl,bc
    pop af
    ld (hl),a
    ld a,c
    inc a
    ld (unapi_tx_count),a
    cp UNAPI_BLOCK_SIZE
    jr z,unapi_write_send
    ld a,(framed_mode)
    or a
    jr z,unapi_write_send            ; raw negotiation must never wait buffered
    xor a
    jr unapi_write_done
unapi_write_send:
    call unapi_send_pending
unapi_write_done:
    ld (unapi_last_error),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret

unapi_flush:
    push bc
    push de
    push hl
    push ix
    push iy
    ld a,UNAPI_FLUSH_RETRIES
    ld (unapi_flush_retries),a
unapi_flush_retry:
    call unapi_send_pending
    or a
    jr z,unapi_flush_done
    cp UNAPI_ERR_BUFFER
    jr nz,unapi_flush_done            ; fatal send already marked session lost
    ld a,UNAPI_TCP_WAIT
    call unapi_call
    ld a,(unapi_flush_retries)
    dec a
    ld (unapi_flush_retries),a
    jr nz,unapi_flush_retry
    ld a,UNAPI_ERR_BUFFER
    push af
    call unapi_drop_connection
    pop af
unapi_flush_done:
    ld (unapi_last_error),a
    pop iy
    pop ix
    pop hl
    pop de
    pop bc
    ld a,(unapi_last_error)
    ret

unapi_send_pending:
    ld a,(unapi_tx_count)
    or a
    ret z
    ld c,a
    ld b,0
    ld (unapi_io_count),bc
    ld (unapi_saved_sp),sp
    ld hl,0
    add hl,sp
    ld d,0
    ld e,a
    or a
    sbc hl,de
    ld sp,hl
    ld (unapi_io_pointer),hl
    push hl
    pop de
    ld hl,unapi_tx_buffer
    ld bc,(unapi_io_count)
    ldir
    ld a,(unapi_connection)
    ld b,a
    ld c,1                          ; PUSH when supported, harmless otherwise
    ld de,(unapi_io_pointer)
    ld hl,(unapi_io_count)
    ld a,UNAPI_TCP_SEND
    call unapi_call
    ld sp,(unapi_saved_sp)
    or a
    jr nz,unapi_send_error
    ld (unapi_tx_count),a
    ret
unapi_send_error:
    ld (unapi_last_error),a
    cp UNAPI_ERR_BUFFER
    jr z,unapi_send_buffer_full
    push af
    call unapi_drop_connection
    pop af
    ret
unapi_send_buffer_full:
    ; A tight framed response can fill the implementation's output queue before
    ; the current H.TIMI returns. WAIT lets its next processing step run; retain
    ; ERR_BUFFER so the caller retries the still-owned resident TX block.
    ld a,UNAPI_TCP_WAIT
    call unapi_call
    ld a,UNAPI_ERR_BUFFER
    ret

unapi_receive_block:
    ld (unapi_saved_sp),sp
    ld hl,0
    add hl,sp
    ld de,UNAPI_BLOCK_SIZE
    or a
    sbc hl,de
    ld sp,hl
    ld (unapi_io_pointer),hl
    push hl
    pop de
    ld hl,UNAPI_BLOCK_SIZE
    ld a,(unapi_connection)
    ld b,a
    ld a,UNAPI_TCP_RCV
    call unapi_call
    ld (unapi_io_count),bc
    ld sp,(unapi_saved_sp)
    or a
    jr nz,unapi_receive_error
    ld bc,(unapi_io_count)
    ld a,b
    or a
    jr nz,unapi_receive_clamp
    ld a,c
    cp UNAPI_BLOCK_SIZE + 1
    jr c,unapi_receive_count_ready
unapi_receive_clamp:
    ld bc,UNAPI_BLOCK_SIZE
unapi_receive_count_ready:
    ld a,b
    or c
    ret z
    ld a,c
    ld (unapi_rx_count),a
    xor a
    ld (unapi_rx_position),a
    ld hl,(unapi_io_pointer)
    ld de,unapi_rx_buffer
    ldir
    xor a
    ret
unapi_receive_error:
    ld (unapi_last_error),a
    ret

; ------------------------------------------------------------ private state --

unapi_runtime_start:
unapi_initialized:
    db 0
unapi_mode:
    db UNAPI_MODE_NONE
unapi_segment:
    db 0
unapi_implementation_count:
    db 0
unapi_implementation_index:
    db 0
unapi_discovery_reason:
    db 0
unapi_connection:
    db 0
unapi_cleanup_connection:
    db 0
unapi_connection_state:
    db 0
unapi_open_blocking:
    db 0
unapi_retry_count:
    db 0
unapi_poll_skip:
    db 0
unapi_busy:
    db 0
unapi_rx_count:
    db 0
unapi_rx_position:
    db 0
unapi_tx_count:
    db 0
unapi_flush_retries:
    db 0
unapi_last_error:
    db 0
unapi_entry:
    dw 0
unapi_call_map:
    dw 0
unapi_slot_iy:
    dw 0
unapi_mapper_iy:
    dw 0
unapi_saved_sp:
    dw 0
unapi_io_pointer:
    dw 0
unapi_io_count:
    dw 0
unapi_pending_rx:
    dw 0
unapi_runtime_end:

if MSXAI_TSR_BUILD
; The on-disk resident template is deliberately clear. The disposable tsr_init
; tail arms this in the final MemMan mapping immediately before initialization;
; resident_initialize and the deferred UNAPI path both leave it clear afterward.
unapi_defer_first_open:
    db 0
endif
unapi_listen_port:
    dw UNAPI_DEFAULT_PORT
unapi_rx_buffer:
    ds UNAPI_BLOCK_SIZE,0
unapi_tx_buffer:
    ds UNAPI_BLOCK_SIZE,0

unapi_api_id_string:
    db "TCP/IP",0
