; Generic 16C550-compatible UART driver at ports 80h-87h.
;
; A BaDCaT SMD is one known device exposing this register interface.  Neither
; the agent core nor the wire protocol depends on that product.

UART16C550_ID:            equ 1
UART16C550_CONTROL_LEVEL: equ 1
UART16C550_FLAGS:         equ TRANSPORT_FLAG_KEYI_EXCLUSIVE | TRANSPORT_FLAG_TIMI_ONLY | TRANSPORT_FLAG_FRAME_WAKE_ACK
UART16C550_STATE_SIZE:    equ 6
UART16C550_DIVISOR_115200: equ 1
UART16C550_DIVISOR_57600:  equ 2

UART16C550_DATA:          equ 080h
UART16C550_IER:           equ 081h
UART16C550_FCR:           equ 082h
UART16C550_LCR:           equ 083h
UART16C550_MCR:           equ 084h
UART16C550_LSR:           equ 085h
UART16C550_MSR:           equ 086h
UART16C550_FCR_ACTIVE:    equ 087h
UART16C550_FCR_REARM:     equ 081h
UART16C550_LCR_ACTIVE:    equ 003h
UART16C550_MCR_RTS_OFF:   equ 001h
UART16C550_MCR_ACTIVE:    equ 02Fh
UART16C550_SAVED_IER:     equ 0
UART16C550_SAVED_LCR:     equ 1
UART16C550_SAVED_MCR:     equ 2
UART16C550_SAVED_DLL:     equ 3
UART16C550_SAVED_DLM:     equ 4
; The sixth opaque state byte combines the RX-to-TX latch with a bounded TX
; burst counter. Bit 7 means that a read has rearmed the turnaround guard;
; otherwise values 00h-20h count bytes written in the current burst.
UART16C550_TX_STATE:       equ 5
UART16C550_TX_RX_PENDING:  equ 080h
UART16C550_TX_BURST_BYTES: equ 020h

uart16c550_init:
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    ld a,(active_uart16c550_divisor)
    cp UART16C550_DIVISOR_115200
    jr z,uart16c550_init_divisor_valid
    cp UART16C550_DIVISOR_57600
    jr z,uart16c550_init_divisor_valid
    ld a,1
    ret
uart16c550_init_divisor_valid:
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
    ld a,UART16C550_FCR_ACTIVE ; FIFO on, clear RX/TX, 8-byte RX trigger
    out (UART16C550_FCR),a
    ld a,083h                  ; DLAB, 8N1
    out (UART16C550_LCR),a
    ld a,(active_uart16c550_divisor)
    out (UART16C550_DATA),a
    xor a
    out (UART16C550_IER),a     ; DLM=0 while DLAB=1
    ld a,UART16C550_LCR_ACTIVE
    out (UART16C550_LCR),a
    ld a,UART16C550_MCR_ACTIVE ; DTR|RTS|OUT1|OUT2|automatic RTS/CTS
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
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
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
    ; Transparent TCP bridges need both a short direction-change guard and
    ; bounded response bursts. Rearm both on every received byte while
    ; preserving the byte and flags returned to the transport-neutral parser.
    push af
    ld a,UART16C550_TX_RX_PENDING
    ld (transport_state + UART16C550_TX_STATE),a
    pop af
    ret

uart16c550_write:
    ; Delay the first transmitted byte after one or more reads. Thereafter,
    ; insert one bridge-drain interval before bytes 33, 65, 97, ... rather than
    ; delaying every byte. The lower seven state bits hold the current count.
    push af
    ld a,(transport_state + UART16C550_TX_STATE)
    bit 7,a
    jr z,uart16c550_write_check_burst
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    call uart16c550_turnaround_guard
    jr uart16c550_write_count
uart16c550_write_check_burst:
    cp UART16C550_TX_BURST_BYTES
    jr c,uart16c550_write_count
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    call uart16c550_burst_guard
uart16c550_write_count:
    ld a,(transport_state + UART16C550_TX_STATE)
    inc a
    ld (transport_state + UART16C550_TX_STATE),a
    pop af
    out (UART16C550_DATA),a
    ret

; 190 DJNZ iterations plus register save/restore and CALL cost take about
; 0.70 ms on a standard 3.579545 MHz Z80: roughly four 57600-baud character
; times. BC and the caller's byte/flags remain intact.
uart16c550_turnaround_guard:
    push bc
    ld b,190
uart16c550_turnaround_guard_loop:
    djnz uart16c550_turnaround_guard_loop
    pop bc
    ret

; A zero B value makes DJNZ execute 256 iterations. Including CALL and register
; preservation, the pause is about 0.94 ms on a standard 3.579545 MHz Z80.
; Because THRE may become true while the preceding byte is still shifting,
; roughly 0.77 ms remains as an idle bridge-drain interval at 57600 baud.
uart16c550_burst_guard:
    push bc
    ld b,0
uart16c550_burst_guard_loop:
    djnz uart16c550_burst_guard_loop
    pop bc
    ret

uart16c550_service:
    xor a
    ret

uart16c550_flush:
    xor a
    ret

if MSXAI_TSR_BUILD
; Private BaDCaT reverse-dial handoff. The caller has already validated the
; binary B3 request and built a resident-owned NUL-terminated command at HL.
; A read-only TEMT preflight happens before the one-shot latch, protocol state,
; or UART writes change. Once the first AT byte may escape, failure is closed
; and destructive; only a final successful TEMT reopens ingress without clear.
uart16c550_badcat_dial:
    call uart16c550_badcat_wait_temt
    ret c

    ; A second AT command cannot be distinguished from MCP payload after the
    ; modem may have entered streaming. Consume the resident's one attempt
    ; atomically before the first byte and never clear it on any later error.
    ld a,1
    ld (tsr_badcat_dial_attempted),a
    push hl
    call transport_session_reset
    pop hl
    xor a
    ld (transport_state + UART16C550_TX_STATE),a

    ; Hold TCP-to-UART ingress closed and discard only bytes stale before ATD.
    ld a,UART16C550_MCR_RTS_OFF
    out (UART16C550_MCR),a
    ld a,UART16C550_FCR_ACTIVE
    out (UART16C550_FCR),a
    call uart16c550_badcat_send_raw
    jr c,uart16c550_badcat_started_failed

    ; After the final CR and successful TEMT, never read DATA or clear a FIFO:
    ; the host may already have sent its one-byte bootstrap probe. Re-arm FIFO
    ; operation first and raise MCR as the final hardware write.
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    ld a,UART16C550_FCR_REARM
    out (UART16C550_FCR),a
    ld a,UART16C550_MCR_ACTIVE
    out (UART16C550_MCR),a
    or a
    ret

uart16c550_badcat_started_failed:
    ; Some ATD bytes may already be in the bridge. Clear both FIFOs, hold RTS
    ; low, and leave the one-shot latch consumed. H.TIMI must not parse an
    ; indeterminate stream as MCP traffic.
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    ld a,UART16C550_FCR_ACTIVE
    out (UART16C550_FCR),a
    ld a,UART16C550_MCR_RTS_OFF
    out (UART16C550_MCR),a
    scf
    ret

; Bounded raw writer. After sending CR it only polls TEMT. There is no DATA
; read and no FIFO operation in this routine.
uart16c550_badcat_send_raw:
    ld b,TSR_BADCAT_DIAL_COMMAND_CAPACITY
uart16c550_badcat_send_loop:
    ld a,(hl)
    or a
    jr z,uart16c550_badcat_send_cr
    push hl
    push bc
    call uart16c550_badcat_wait_thre
    pop bc
    pop hl
    ret c
    ld a,(hl)
    out (UART16C550_DATA),a
    inc hl
    djnz uart16c550_badcat_send_loop
    scf
    ret
uart16c550_badcat_send_cr:
    call uart16c550_badcat_wait_thre
    ret c
    ld a,13
    out (UART16C550_DATA),a
    jp uart16c550_badcat_wait_temt

uart16c550_badcat_wait_thre:
    ld de,HOOK_IO_BUDGET
uart16c550_badcat_wait_thre_loop:
    in a,(UART16C550_LSR)
    cp 0FFh
    jr z,uart16c550_badcat_wait_failed
    and 020h
    ret nz
    dec de
    ld a,d
    or e
    jr nz,uart16c550_badcat_wait_thre_loop
uart16c550_badcat_wait_failed:
    scf
    ret

uart16c550_badcat_wait_temt:
    ld de,HOOK_IO_BUDGET
uart16c550_badcat_wait_temt_loop:
    in a,(UART16C550_LSR)
    cp 0FFh
    jr z,uart16c550_badcat_wait_failed
    and 040h
    ret nz
    dec de
    ld a,d
    or e
    jr nz,uart16c550_badcat_wait_temt_loop
    scf
    ret
endif

; Hook-side timeout recovery is deliberately local to this driver. Clearing
; both FIFOs removes a truncated response/request while rewriting LCR/MCR
; restores 8N1 automatic RTS/CTS without changing the selected divisor or
; overwriting the UART state saved for uninstall.
uart16c550_timeout_recover:
if MSXAI_TSR_BUILD
if MSXAI_DEVELOPMENT_TRACE
    ld a,TRACE_EVENT_UART_RECOVER
    call trace_uart_record
endif
endif
    xor a
    ld (transport_state + UART16C550_TX_STATE),a
    out (UART16C550_IER),a
    ld a,UART16C550_FCR_ACTIVE
    out (UART16C550_FCR),a
    ld a,UART16C550_LCR_ACTIVE
    out (UART16C550_LCR),a
    ld a,UART16C550_MCR_ACTIVE
    out (UART16C550_MCR),a
if MSXAI_TSR_BUILD
if MSXAI_DEVELOPMENT_TRACE
    ld a,TRACE_UART_PHASE_IDLE
    call trace_uart_set_phase
    ld a,TRACE_EVENT_UART_REARM
    call trace_uart_record
endif
endif
    ret
