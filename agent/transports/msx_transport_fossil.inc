; Experimental FOSSIL v1.40 serial transport.
;
; This adapter is intentionally available only in the foreground MSXAI.COM
; build.  It consumes the API published by an already installed DRIVER.COM;
; it neither embeds that third-party driver nor changes BaDCaT firmware or
; persistent modem settings.  The known 16550 backend owns a 512-byte RX ring
; from H.KEYI, so the foreground monitor must leave interrupts enabled and
; continue chaining the predecessor hook.

FOSSIL_ID:                     equ 3
FOSSIL_CONTROL_LEVEL:          equ 1
FOSSIL_FLAGS:                  equ TRANSPORT_FLAG_TIMI_ONLY | TRANSPORT_FLAG_FRAME_WAKE_ACK
FOSSIL_STATE_SIZE:             equ 3
FOSSIL_STATE_INITIALIZED:      equ 0
FOSSIL_STATE_TX:               equ 1
FOSSIL_STATE_SAVED_MCR:        equ 2
FOSSIL_TX_RX_PENDING:          equ 080h
FOSSIL_TX_BURST_BYTES:         equ 020h
FOSSIL_FRAME_DRAIN_ROUNDS:     equ 4

; DRIVER 16550 is pinned below by its v1.40 info block and uses the standard
; BaDCaT register window. Its init leaves a one-byte FIFO trigger and manual
; RTS (FCR=07h, MCR=03h), while ZiModem's transparent stream uses RTS/CTS.
; Enable the UART's volatile automatic flow-control bit only for this adapter;
; do not assert OUT1/OUT2 or change any persistent modem setting.
FOSSIL_UART_FCR:               equ 082h
FOSSIL_UART_MCR:               equ 084h
FOSSIL_UART_FCR_DRIVER:        equ 007h
FOSSIL_UART_FCR_ACTIVE:        equ 087h
FOSSIL_UART_MCR_AFE:           equ 020h

FOSSIL_SIGNATURE_R:            equ 0F3FCh
FOSSIL_SIGNATURE_S:            equ 0F3FDh
FOSSIL_TABLE_POINTER:          equ 0F3FEh
FOSSIL_REQUIRED_VERSION:       equ 0140h
FOSSIL_BAUD_57600:             equ 9
FOSSIL_PROTOCOL_8N1:           equ 007h
FOSSIL_HARDWARE_16550:         equ 8
FOSSIL_API_ENTRY_COUNT:        equ 21
FOSSIL_API_ENTRY_SIZE:         equ 3
FOSSIL_API_TABLE_SIZE:         equ FOSSIL_API_ENTRY_COUNT * FOSSIL_API_ENTRY_SIZE
FOSSIL_JP_OPCODE:              equ 0C3h

FOSSIL_INIT_NOT_FOUND:         equ 090h
FOSSIL_INIT_BAD_TABLE:         equ 091h
FOSSIL_INIT_BAD_VERSION:       equ 092h
FOSSIL_INIT_BAD_CONFIGURATION: equ 093h

; Init returns A=0 on success.  The v1.40 init entry itself returns no status,
; so bind to that exact ABI and verify its public info block after setup.
fossil_init:
    push bc
    push de
    push hl
    xor a
    ld (transport_state + FOSSIL_STATE_INITIALIZED),a
    ld (transport_state + FOSSIL_STATE_TX),a
    call fossil_init_inner
    pop hl
    pop de
    pop bc
    ret

fossil_init_inner:
    ld a,(FOSSIL_SIGNATURE_R)
    cp 'R'
    jp nz,fossil_init_not_found
    ld a,(FOSSIL_SIGNATURE_S)
    cp 'S'
    jp nz,fossil_init_not_found

    ld hl,(FOSSIL_TABLE_POINTER)
    ld a,h
    or l
    jp z,fossil_init_bad_table
    ld b,FOSSIL_API_ENTRY_COUNT
fossil_validate_table_loop:
    ld a,(hl)
    cp FOSSIL_JP_OPCODE
    jp nz,fossil_init_bad_table
    inc hl
    inc hl
    inc hl
    djnz fossil_validate_table_loop

    ; Freeze the public JP table locally.  Calls then use ordinary fixed CALL
    ; instructions and do not self-modify operands while interrupts are live.
    ld hl,(FOSSIL_TABLE_POINTER)
    ld de,fossil_api_table
    ld bc,FOSSIL_API_TABLE_SIZE
    ldir

    call fossil_api_get_version
    ld a,h
    cp FOSSIL_REQUIRED_VERSION / 256
    jr nz,fossil_init_bad_version
    ld a,l
    cp FOSSIL_REQUIRED_VERSION & 0FFh
    jr nz,fossil_init_bad_version

    ; Set the speed before init: v1.40 init reapplies its saved speed (19200
    ; after a fresh DRIVER load).  Both halves are specified by the ABI even
    ; though the known 16550 implementation forces RX to the TX value.
    ld hl,FOSSIL_BAUD_57600 * 0101h
    call fossil_api_set_baud
    call fossil_api_init

    ld h,FOSSIL_PROTOCOL_8N1
    ld l,0
    call fossil_api_protocol
    ld h,0FFh
    call fossil_api_dtr
    ld h,0FFh
    call fossil_api_rts

    call fossil_api_get_info
    ld a,h
    or l
    jr z,fossil_init_bad_configuration_started
    ld a,(hl)                    ; info +0: version low byte
    cp FOSSIL_REQUIRED_VERSION & 0FFh
    jr nz,fossil_init_bad_configuration_started
    inc hl
    ld a,(hl)                    ; info +1: version high byte
    cp FOSSIL_REQUIRED_VERSION / 256
    jr nz,fossil_init_bad_configuration_started
    inc hl
    ld a,(hl)                    ; info +2: RX speed index
    cp FOSSIL_BAUD_57600
    jr nz,fossil_init_bad_configuration_started
    inc hl
    ld a,(hl)                    ; info +3: TX speed index
    cp FOSSIL_BAUD_57600
    jr nz,fossil_init_bad_configuration_started
    ld de,7
    add hl,de
    ld a,(hl)                    ; info +10: concrete 16550/FIFO backend
    cp FOSSIL_HARDWARE_16550
    jr nz,fossil_init_bad_configuration_started

    ; Drop only bytes accumulated while DRIVER and MSXAI were being started.
    ; The core transport flush remains a no-op: framed response boundaries
    ; must never discard a pipelined request already present in this RX ring.
    call fossil_api_flush_rx
    call fossil_enable_hardware_flow
    or a
    jr nz,fossil_init_bad_configuration_started
    ld a,1
    ld (transport_state + FOSSIL_STATE_INITIALIZED),a
    xor a
    ret

fossil_init_not_found:
    ld a,FOSSIL_INIT_NOT_FOUND
    ret
fossil_init_bad_table:
    ld a,FOSSIL_INIT_BAD_TABLE
    ret
fossil_init_bad_version:
    ld a,FOSSIL_INIT_BAD_VERSION
    ret
fossil_init_bad_configuration_started:
    call fossil_api_deinit
    ld a,FOSSIL_INIT_BAD_CONFIGURATION
    ret

; DRIVER's TX routine polls only THRE. Automatic flow control makes the UART
; also honor ZiModem's CTS backpressure during sustained MCP responses. The
; MCR readback fails closed on an incompatible UART; FCR is write-only, but
; v1.40's validated hardware-8 backend has the known 07h baseline restored
; below. Keep the register update non-reentrant and preserve the caller's IFF.
fossil_enable_hardware_flow:
    ld a,i
    di
    push af
    in a,(FOSSIL_UART_MCR)
    ld (transport_state + FOSSIL_STATE_SAVED_MCR),a
    ld a,FOSSIL_UART_FCR_ACTIVE
    out (FOSSIL_UART_FCR),a
    ld a,(transport_state + FOSSIL_STATE_SAVED_MCR)
    or FOSSIL_UART_MCR_AFE
    out (FOSSIL_UART_MCR),a
    in a,(FOSSIL_UART_MCR)
    and FOSSIL_UART_MCR_AFE
    ld b,0
    jr nz,fossil_enable_hardware_flow_finish
    inc b
    ld a,FOSSIL_UART_FCR_DRIVER
    out (FOSSIL_UART_FCR),a
    ld a,(transport_state + FOSSIL_STATE_SAVED_MCR)
    out (FOSSIL_UART_MCR),a
fossil_enable_hardware_flow_finish:
    pop af
    jp po,fossil_enable_hardware_flow_iff_done
    ei
fossil_enable_hardware_flow_iff_done:
    ld a,b
    ret

fossil_restore_hardware_flow:
    ld a,i
    di
    push af
    ld a,FOSSIL_UART_FCR_DRIVER
    out (FOSSIL_UART_FCR),a
    ld a,(transport_state + FOSSIL_STATE_SAVED_MCR)
    out (FOSSIL_UART_MCR),a
    pop af
    jp po,fossil_restore_hardware_flow_done
    ei
fossil_restore_hardware_flow_done:
    ret

fossil_restore:
    push bc
    push de
    push hl
    ld a,(transport_state + FOSSIL_STATE_INITIALIZED)
    or a
    jr z,fossil_restore_done
    call fossil_restore_hardware_flow
    call fossil_api_deinit
    xor a
    ld (transport_state + FOSSIL_STATE_INITIALIZED),a
fossil_restore_done:
    xor a
    ld (transport_state + FOSSIL_STATE_TX),a
    pop hl
    pop de
    pop bc
    ret

; v1.40's concrete 16550 implementation returns FFh when data/THRE is ready,
; despite the stale opposite wording in part of the generic API commentary.
; Pinning the exact version above makes that behavior an explicit dependency.
fossil_rx_ready:
    push bc
    push de
    push hl
    call fossil_api_rs_in_stat
    pop hl
    pop de
    pop bc
    ret

fossil_tx_ready:
    push bc
    push de
    push hl
    call fossil_api_rs_out_stat
    pop hl
    pop de
    pop bc
    ret

fossil_read:
    push bc
    push de
    push hl
    call fossil_api_rs_in
    ; A transparent TCP bridge needs a short direction-change pause before
    ; the first response byte. Preserve the received byte while arming it.
    push af
    ld a,FOSSIL_TX_RX_PENDING
    ld (transport_state + FOSSIL_STATE_TX),a
    pop af
    pop hl
    pop de
    pop bc
    ret

fossil_write:
    push bc
    push de
    push hl
    push af
    ld a,(transport_state + FOSSIL_STATE_TX)
    bit 7,a
    jr z,fossil_write_check_burst
    xor a
    ld (transport_state + FOSSIL_STATE_TX),a
    call fossil_turnaround_guard
    jr fossil_write_count
fossil_write_check_burst:
    cp FOSSIL_TX_BURST_BYTES
    jr c,fossil_write_count
    xor a
    ld (transport_state + FOSSIL_STATE_TX),a
    call fossil_burst_guard
fossil_write_count:
    ld a,(transport_state + FOSSIL_STATE_TX)
    inc a
    ld (transport_state + FOSSIL_STATE_TX),a
    pop af
    call fossil_api_rs_out
    pop hl
    pop de
    pop bc
    ret

; Match the bridge-drain pacing already validated by the native BaDCaT path.
; The external FOSSIL v1.40 driver otherwise changes RX->TX direction and emits
; long THRE-paced bursts without allowing the transparent modem to turn around.
fossil_turnaround_guard:
    push bc
    ld b,190
fossil_turnaround_guard_loop:
    djnz fossil_turnaround_guard_loop
    pop bc
    ret

fossil_burst_guard:
    push bc
    ld b,0
fossil_burst_guard_loop:
    djnz fossil_burst_guard_loop
    pop bc
    ret

fossil_service:
    ; DRIVER.COM normally fills its software ring only from H.KEYI. A framed
    ; request can leave 17 bytes after the credited wake byte, one more than a
    ; classic 16C550 FIFO can retain until the next video interrupt. Invoke the
    ; exact predecessor hook that MSXAI saved before installing itself so each
    ; foreground wait drains the hardware immediately. Calling the saved hook
    ; (not H.KEYI) avoids direct recursion; the v1.40 16550 handler is call-safe
    ; and preserves AF while polling every available UART byte into its
    ; 512-byte ring. Run that non-reentrant ring update with interrupts masked,
    ; exactly as it normally runs from H.KEYI, then restore the caller's IFF2
    ; state. No fastint or firmware change is involved.
    push bc
    push de
    push hl
    ld a,i
    di
    push af
    call old_keyi
    pop af
    jp po,fossil_service_restore_done
    ei
fossil_service_restore_done:
    pop hl
    pop de
    pop bc
    xor a
    ret

fossil_flush:
    ; ZiModem's TCP writer can accept roughly 2 KiB of consecutive UART output
    ; before applying RTS backpressure. Its v3.3 overflow path may leave RTS
    ; deasserted after the socket subsequently drains, which makes correct AFE
    ; turn that transient condition into a permanent THRE wait. Before this
    ; guard existed, physical runs stopped reproducibly after about 2,126
    ; response bytes (twelve q/v pairs). Leave a short FOSSIL-only boundary
    ; for the TCP stack to retire each write window instead of accumulating to
    ; that edge. H.KEYI remains enabled during the delay, so an already-credited
    ; request body is still drained into DRIVER's software ring. This routine
    ; never calls flush_rx and therefore cannot discard a pipelined request.
    push bc
    ld c,FOSSIL_FRAME_DRAIN_ROUNDS
fossil_frame_drain_outer:
    ld b,0
fossil_frame_drain_inner:
    djnz fossil_frame_drain_inner
    dec c
    jr nz,fossil_frame_drain_outer
    pop bc
    xor a
    ret

; DRIVER.COM publishes 21 consecutive three-byte JP entries.  Copying all of
; them also validates the ABI shape, but this adapter deliberately never calls
; fastint: patching 0038h would conflict with the cooperative MSX-AI monitor.
fossil_api_table:
fossil_api_get_version: ds FOSSIL_API_ENTRY_SIZE,0 ; +00
fossil_api_init:        ds FOSSIL_API_ENTRY_SIZE,0 ; +03
fossil_api_deinit:      ds FOSSIL_API_ENTRY_SIZE,0 ; +06
fossil_api_set_baud:    ds FOSSIL_API_ENTRY_SIZE,0 ; +09
fossil_api_protocol:    ds FOSSIL_API_ENTRY_SIZE,0 ; +12
fossil_api_channel:     ds FOSSIL_API_ENTRY_SIZE,0 ; +15
fossil_api_rs_in:       ds FOSSIL_API_ENTRY_SIZE,0 ; +18
fossil_api_rs_out:      ds FOSSIL_API_ENTRY_SIZE,0 ; +21
fossil_api_rs_in_stat:  ds FOSSIL_API_ENTRY_SIZE,0 ; +24
fossil_api_rs_out_stat: ds FOSSIL_API_ENTRY_SIZE,0 ; +27
fossil_api_dtr:         ds FOSSIL_API_ENTRY_SIZE,0 ; +30
fossil_api_rts:         ds FOSSIL_API_ENTRY_SIZE,0 ; +33
fossil_api_carrier:     ds FOSSIL_API_ENTRY_SIZE,0 ; +36
fossil_api_chars_in:    ds FOSSIL_API_ENTRY_SIZE,0 ; +39
fossil_api_buffer_size: ds FOSSIL_API_ENTRY_SIZE,0 ; +42
fossil_api_flush_rx:    ds FOSSIL_API_ENTRY_SIZE,0 ; +45
fossil_api_fastint:     ds FOSSIL_API_ENTRY_SIZE,0 ; +48, intentionally unused
fossil_api_hook38stat:  ds FOSSIL_API_ENTRY_SIZE,0 ; +51
fossil_api_chput_hook:  ds FOSSIL_API_ENTRY_SIZE,0 ; +54
fossil_api_keyb_hook:   ds FOSSIL_API_ENTRY_SIZE,0 ; +57
fossil_api_get_info:    ds FOSSIL_API_ENTRY_SIZE,0 ; +60
