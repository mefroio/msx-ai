; Generic 16C550-compatible UART driver at ports 80h-87h.
;
; A BaDCaT SMD is one known device exposing this register interface.  Neither
; the agent core nor the wire protocol depends on that product.

UART16C550_ID:            equ 1
UART16C550_CONTROL_LEVEL: equ 1
UART16C550_FLAGS:         equ TRANSPORT_FLAG_KEYI_EXCLUSIVE | TRANSPORT_FLAG_TIMI_ONLY | TRANSPORT_FLAG_FRAME_WAKE_ACK
UART16C550_STATE_SIZE:    equ 5

UART16C550_DATA:          equ 080h
UART16C550_IER:           equ 081h
UART16C550_FCR:           equ 082h
UART16C550_LCR:           equ 083h
UART16C550_MCR:           equ 084h
UART16C550_LSR:           equ 085h
UART16C550_SAVED_IER:     equ 0
UART16C550_SAVED_LCR:     equ 1
UART16C550_SAVED_MCR:     equ 2
UART16C550_SAVED_DLL:     equ 3
UART16C550_SAVED_DLM:     equ 4

uart16c550_init:
    ; Preserve readable state so a safe uninstall restores the prior UART setup.
    in a,(UART16C550_LCR)
    ld (transport_state + UART16C550_SAVED_LCR),a
    and 07Fh
    out (UART16C550_LCR),a
    in a,(UART16C550_IER)
    ld (transport_state + UART16C550_SAVED_IER),a
    in a,(UART16C550_MCR)
    ld (transport_state + UART16C550_SAVED_MCR),a
    ld a,(transport_state + UART16C550_SAVED_LCR)
    or 080h
    out (UART16C550_LCR),a
    in a,(UART16C550_DATA)
    ld (transport_state + UART16C550_SAVED_DLL),a
    in a,(UART16C550_IER)
    ld (transport_state + UART16C550_SAVED_DLM),a

    ld a,(transport_state + UART16C550_SAVED_LCR)
    and 07Fh
    out (UART16C550_LCR),a
    xor a
    out (UART16C550_IER),a
    out (UART16C550_MCR),a
    ld a,087h                  ; FIFO on, clear RX/TX, 8-byte RX trigger
    out (UART16C550_FCR),a
    ld a,083h                  ; DLAB, 8N1
    out (UART16C550_LCR),a
    ld a,1                     ; 1.8432 MHz / (16 * 1) = 115200 baud
    out (UART16C550_DATA),a
    xor a
    out (UART16C550_IER),a     ; DLM=0 while DLAB=1
    ld a,003h
    out (UART16C550_LCR),a
    ld a,02Fh                  ; DTR|RTS|OUT1|OUT2|automatic RTS/CTS
    out (UART16C550_MCR),a
    xor a                      ; FIFO is polled from H.TIMI; no asynchronous IRQ
    out (UART16C550_IER),a
    ret

uart16c550_restore:
    ld a,(transport_state + UART16C550_SAVED_LCR)
    and 07Fh
    out (UART16C550_LCR),a
    xor a
    out (UART16C550_IER),a
    out (UART16C550_FCR),a     ; FIFO is write-only; leave it disabled
    ld a,(transport_state + UART16C550_SAVED_LCR)
    or 080h
    out (UART16C550_LCR),a
    ld a,(transport_state + UART16C550_SAVED_DLL)
    out (UART16C550_DATA),a
    ld a,(transport_state + UART16C550_SAVED_DLM)
    out (UART16C550_IER),a
    ld a,(transport_state + UART16C550_SAVED_LCR)
    and 07Fh
    out (UART16C550_LCR),a
    ld a,(transport_state + UART16C550_SAVED_MCR)
    out (UART16C550_MCR),a
    ld a,(transport_state + UART16C550_SAVED_IER)
    out (UART16C550_IER),a
    ld a,(transport_state + UART16C550_SAVED_LCR)
    out (UART16C550_LCR),a
    ret

uart16c550_rx_ready:
    in a,(UART16C550_LSR)
    inc a                       ; FFh means an absent/floating UART, not data
    ret z
    dec a
    and 01h                     ; data ready
    ret

uart16c550_tx_ready:
    in a,(UART16C550_LSR)
    and 020h                    ; THR empty
    ret

uart16c550_read:
    in a,(UART16C550_DATA)
    ret

uart16c550_write:
    out (UART16C550_DATA),a
    ret
