; Intel 8251-compatible MSX RS-232 driver at ports 80h/81h.
;
; This file emits namespaced routines into the single universal agent.  The
; installer binds the transport_* jump vectors to these routines at runtime.

UART8251_ID:            equ 0
UART8251_CONTROL_LEVEL: equ 1
UART8251_FLAGS:         equ TRANSPORT_FLAG_KEYI_EXCLUSIVE | TRANSPORT_FLAG_TIMI_ONLY | TRANSPORT_FLAG_FRAME_WAKE_ACK
UART8251_STATE_SIZE:    equ 1

UART8251_DATA:          equ 080h
UART8251_STATUS:        equ 081h
UART8251_COMMSK:        equ 0FB1Dh
UART8251_SAVED_COMMSK:  equ 0
UART8251_TIMER_RX:      equ 084h
UART8251_TIMER_TX:      equ 085h
UART8251_TIMER_CONTROL: equ 087h
UART8251_TIMER_RX_MODE: equ 036h    ; counter 0, LSB/MSB, mode 3, binary
UART8251_TIMER_TX_MODE: equ 076h    ; counter 1, LSB/MSB, mode 3, binary
UART8251_TIMER_DIVISOR: equ 6       ; 1.8432 MHz / 6 / 16 = 19,200 baud

uart8251_init:
    ; Preserve the firmware mask, then disable every asynchronous UART source
    ; before enabling the receiver. The resident polls RxRDY only from H.TIMI.
    ld a,(UART8251_COMMSK)
    ld (transport_state + UART8251_SAVED_COMMSK),a
    ld a,0FFh
    ld (UART8251_COMMSK),a
    out (082h),a

    ; The standard MSX RS-232 interface clocks the 8251 from counters 0 and 1
    ; of an 8253/8254 at ports 84h..87h. Leaving those counters at firmware or
    ; power-on values makes receive delivery intermittent. Program both sides
    ; explicitly for 19,200 baud, the 8251A asynchronous maximum in x16 mode.
    ; Going faster is unsafe on this one-byte receiver without a FIFO or flow
    ; control; higher rates belong to the separate 16C550 transport.
    ld a,UART8251_TIMER_RX_MODE
    out (UART8251_TIMER_CONTROL),a
    ld a,UART8251_TIMER_DIVISOR
    out (UART8251_TIMER_RX),a
    xor a
    out (UART8251_TIMER_RX),a
    ld a,UART8251_TIMER_TX_MODE
    out (UART8251_TIMER_CONTROL),a
    ld a,UART8251_TIMER_DIVISOR
    out (UART8251_TIMER_TX),a
    xor a
    out (UART8251_TIMER_TX),a

    xor a
    out (UART8251_STATUS),a
    out (UART8251_STATUS),a
    out (UART8251_STATUS),a
    ld a,040h                   ; 8251 internal reset
    out (UART8251_STATUS),a
    ld a,04Eh                   ; asynchronous 8N1 x16
    out (UART8251_STATUS),a
    ld a,037h                   ; TxEN|DTR|RxEN|ER|RTS
    out (UART8251_STATUS),a
    xor a                       ; transport init status: success
    ret

uart8251_restore:
    ld a,(transport_state + UART8251_SAVED_COMMSK)
    ld (UART8251_COMMSK),a
    out (082h),a
    xor a
    ret

uart8251_rx_ready:
    in a,(UART8251_STATUS)
    inc a                       ; FFh means an absent/floating UART, not RxRDY
    ret z
    dec a
    push af
    and 038h                    ; PE|OE|FE are sticky until command ER is set
    jr z,uart8251_rx_status_clean
    ld a,037h                   ; preserve Tx/Rx/DTR/RTS and reset errors
    out (UART8251_STATUS),a
uart8251_rx_status_clean:
    pop af
    and 02h                     ; 8251 RxRDY
    ret

uart8251_tx_ready:
    in a,(UART8251_STATUS)
    and 01h                     ; 8251 TxRDY
    ret

uart8251_read:
    in a,(UART8251_DATA)
    ret

uart8251_write:
    out (UART8251_DATA),a
    ret

uart8251_service:
uart8251_flush:
    xor a
    ret

; Terminal operations must not reinitialize the UART while its shift register
; still owns the final framed-response byte. Return zero only after TxEMPTY.
uart8251_terminal_drain:
    ld bc,HOOK_IO_BUDGET
uart8251_terminal_drain_wait:
    in a,(UART8251_STATUS)
    inc a
    jr z,uart8251_terminal_drain_failed ; FFh is an absent/floating device
    dec a
    and 04h                    ; 8251 TxEMPTY, not merely TxRDY
    jr nz,uart8251_terminal_drain_done
    dec bc
    ld a,b
    or c
    jr nz,uart8251_terminal_drain_wait
uart8251_terminal_drain_failed:
    ld a,1
    ret
uart8251_terminal_drain_done:
    xor a
    ret
