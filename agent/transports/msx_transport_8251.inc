; Intel 8251-compatible MSX RS-232 driver at ports 80h/81h.
;
; This file emits namespaced routines into the single universal agent.  The
; installer binds the transport_* jump vectors to these routines at runtime.

UART8251_ID:            equ 0
UART8251_CONTROL_LEVEL: equ 1
UART8251_FLAGS:         equ 1       ; bit 0: H.KEYI is exclusive
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
UART8251_TIMER_DIVISOR: equ 24      ; 1.8432 MHz / 24 / 16 = 4,800 baud

uart8251_init:
    ; The standard MSX RS-232 interface clocks the 8251 from counters 0 and 1
    ; of an 8253/8254 at ports 84h..87h. Leaving those counters at firmware or
    ; power-on values makes receive delivery intermittent. Program both sides
    ; explicitly for a rate whose 2.08-ms character interval tolerates the
    ; MemMan/BIOS interrupt path without overflowing the one-byte receiver.
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

    ; Enable the standard MSX RS-232 RxRDY interrupt (active-low mask bit 0).
    ld a,(UART8251_COMMSK)
    ld (transport_state + UART8251_SAVED_COMMSK),a
    ld a,0FEh                  ; own only RxRDY; mask TX/modem IRQ sources
    ld (UART8251_COMMSK),a
    out (082h),a
    ret

uart8251_restore:
    ld a,(transport_state + UART8251_SAVED_COMMSK)
    ld (UART8251_COMMSK),a
    out (082h),a
    ret

uart8251_rx_ready:
    in a,(UART8251_STATUS)
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
