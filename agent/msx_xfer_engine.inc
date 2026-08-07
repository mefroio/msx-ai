; ---------------------------------------------------------------------------
; Protocol X/v1 foreground transfer engine.
;
; The resident stages a descriptor and performs bounded UART framing. This
; transient half claims that descriptor with the 128-bit command-line token and
; owns every DOS2 call. RAW remains byte-exact in both directions. PackBits is
; a PUT-only wire encoding decoded with bounded buffers after the compressed
; partial has been durably received and verified.

loader_xfer_put_file:
    ld a,XFER_DIRECTION_PUT
    call loader_xfer_initialize
    jp nz,loader_xfer_early_error
    call loader_xfer_build_paths
    call loader_xfer_prepare_put
    jp nz,loader_xfer_open_error
    call loader_xfer_publish_ready
    jp nz,loader_xfer_open_error
    ld de,loader_xfer_put_ready_message
    call loader_xfer_print
    call loader_xfer_progress_begin
    ld hl,(JIFFY)
    ld (loader_xfer_last_progress),hl

loader_xfer_put_loop:
    call loader_xfer_fast_pump
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_PUT_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 2
    jp z,loader_xfer_put_close
    or a
    jp z,loader_xfer_wait
    ld (loader_xfer_block_length),hl
    jp loader_xfer_put_fast_accumulate

; Fast PUT keeps up to one complete 16 KiB durability window in transient page
; 1. Individual MCP frames are copied out of the page-zero TsrCall mailbox and
; released immediately. Only the high-water threshold or EOF performs CRC,
; DOS WRITE, ENSURE, and cumulative COMMIT, amortizing filesystem overhead without
; weakening the existing accepted/durable resume boundary.
loader_xfer_put_fast_accumulate:
    ld hl,(loader_xfer_accumulated)
    ld de,(loader_xfer_block_length)
    add hl,de
    jp c,loader_xfer_internal_open_error
    push hl
    ld de,XFER_FAST_ACCUMULATOR_CAPACITY
    or a
    sbc hl,de
    pop hl
    jp c,loader_xfer_put_fast_capacity_ok
    jp nz,loader_xfer_internal_open_error
loader_xfer_put_fast_capacity_ok:
    ld (loader_xfer_accumulated),hl

    ; Append this accepted frame to the page-1 accumulator. TsrCall has already
    ; restored the helper's page before control reaches this copy.
    ld hl,(loader_xfer_accumulated)
    ld de,(loader_xfer_block_length)
    or a
    sbc hl,de
    ld de,loader_xfer_accumulator
    add hl,de
    ex de,hl
    ld hl,loader_xfer_buffer
    ld bc,(loader_xfer_block_length)
    ldir

    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_position
    call loader_xfer_position_equals_wire
    jp z,loader_xfer_put_fast_flush
    ld hl,(loader_xfer_accumulated)
    ld de,XFER_FAST_ACCUMULATOR_HIGH_WATER
    or a
    sbc hl,de
    jp nc,loader_xfer_put_fast_flush

    ; The bytes are safely retained in the helper TPA but are not durable yet.
    ; Release physical mailbox credit without advancing the host journal.
    ld hl,(loader_xfer_block_length)
    ld a,TSR_TALK_XFER_PUT_RELEASE
    call loader_xfer_tsr_call
    or a
    jp nz,loader_xfer_internal_open_error
    call loader_xfer_mark_progress
    jp loader_xfer_put_loop

loader_xfer_put_fast_flush:
    ; Update CRC once across the contiguous window rather than once per MCP
    ; frame, then write and ensure the same exact bytes as one DOS operation.
    ld bc,(loader_xfer_accumulated)
    ld hl,loader_xfer_accumulator
    call loader_xfer_crc_update
    ; Any primary-file mutation invalidates prior ENSURE proof. This byte is set
    ; only after the exact accumulator window has been successfully ENSUREd.
    xor a
    ld (loader_xfer_primary_ensured),a
    ld de,loader_xfer_accumulator
    ld hl,(loader_xfer_accumulated)
    ld a,(loader_xfer_handle)
    ld b,a
    call write_exact
    or a
    jp nz,loader_xfer_open_error
    ld a,(loader_xfer_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    or a
    jp nz,loader_xfer_open_error
    ld a,1
    ld (loader_xfer_primary_ensured),a

    ; The current frame remains pending, allowing COMMIT to atomically cover
    ; all earlier released frames plus this final frame in the 16 KiB window.
    ld hl,(loader_xfer_accumulated)
    ld (loader_xfer_buffer),hl
    ld de,loader_xfer_buffer + 2
    call loader_xfer_crc_final_to
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_PUT_COMMIT
    call loader_xfer_tsr_call
    or a
    jp nz,loader_xfer_internal_open_error

    ld hl,(loader_xfer_accumulated)
    ld (loader_xfer_block_length),hl
    xor a
    ld (loader_xfer_accumulated),a
    ld (loader_xfer_accumulated + 1),a
    call loader_xfer_progress_after_block
    call loader_xfer_mark_progress
    jp loader_xfer_put_loop

loader_xfer_put_close:
    call loader_xfer_apply_fast_final_rate
    jp loader_xfer_put_finalize

loader_xfer_put_finalize:
    ld hl,(loader_xfer_accumulated)
    ld a,h
    or l
    jp nz,loader_xfer_metadata_open_error
    call loader_xfer_position_equals_wire
    jp nz,loader_xfer_metadata_open_error
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    call loader_xfer_crc_matches
    jp nz,loader_xfer_crc_open_error
    ld a,(loader_xfer_handle)
    cp INVALID_HANDLE
    jr z,loader_xfer_put_final_close
    ; A normal fast flush already ENSUREd the exact primary bytes. Empty PUTs
    ; and resumed-already-complete PUTs reach finalization without any flush in
    ; this helper invocation, so they must still establish that proof here.
    ld a,(loader_xfer_primary_ensured)
    or a
    jr nz,loader_xfer_put_final_close
    ld a,(loader_xfer_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    or a
    jp nz,loader_xfer_open_error
    ld a,1
    ld (loader_xfer_primary_ensured),a
loader_xfer_put_final_close:
    call loader_xfer_close_primary
    or a
    jp nz,loader_xfer_closed_error
    call loader_xfer_enter_postprocess
    or a
    jp nz,loader_xfer_closed_error
    ld a,(loader_xfer_descriptor + XFER_DESC_ENCODING)
    cp XFER_ENCODING_PACKBITS
    jp z,loader_xfer_put_packbits_finalize
    call loader_xfer_publish_temp
    or a
    jp nz,loader_xfer_closed_error
    ld de,loader_xfer_temp_path
    ld c,DOS_DELETE
    call 00005h
    ld de,loader_xfer_meta_path
    ld c,DOS_DELETE
    call 00005h                 ; full-journal exact-target replay stays safe
    ld de,loader_xfer_put_ok_message
    jp loader_xfer_success_exit

loader_xfer_get_file:
    ld a,XFER_DIRECTION_GET
    call loader_xfer_initialize
    jp nz,loader_xfer_early_error
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    jp nz,loader_xfer_open_error
    ld a,b
    ld (loader_xfer_handle),a

    ; Discover the complete source length, validate the requested resume
    ; prefix, then scan the whole source for authoritative GET metadata.
    call loader_xfer_seek_end
    or a
    jp nz,loader_xfer_open_error
    ld hl,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld de,loader_xfer_scan_limit
    ld bc,4
    ldir
    call loader_xfer_limit_not_after_position
    jp c,loader_xfer_range_open_error
    call loader_xfer_scan_exact
    or a
    jp nz,loader_xfer_open_error
    ld hl,loader_xfer_descriptor + XFER_DESC_PREFIX_CRC
    call loader_xfer_crc_matches
    jp nz,loader_xfer_crc_open_error
    ld hl,loader_xfer_crc
    ld de,loader_xfer_saved_crc
    ld bc,4
    ldir
    ld hl,loader_xfer_position
    ld de,loader_xfer_scan_limit
    ld bc,4
    ldir
    call loader_xfer_scan_exact
    or a
    jp nz,loader_xfer_open_error
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    call loader_xfer_crc_final_to
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    ld de,loader_xfer_descriptor + XFER_DESC_FINAL_CRC
    ld bc,4
    ldir
    ld hl,loader_xfer_position
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld bc,4
    ldir
    ld hl,loader_xfer_position
    ld de,loader_xfer_descriptor + XFER_DESC_FINAL_SIZE
    ld bc,4
    ldir

    ; Restore the already-validated rolling prefix accumulator rather than
    ; re-reading the prefix a second time, then seek directly to resume.
    ld hl,loader_xfer_saved_crc
    ld de,loader_xfer_crc
    ld bc,4
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld de,loader_xfer_position
    ld bc,4
    ldir
    call loader_xfer_seek_position
    or a
    jp nz,loader_xfer_open_error
    call loader_xfer_publish_ready
    or a
    jp nz,loader_xfer_internal_open_error
    ld de,loader_xfer_get_ready_message
    call loader_xfer_print
    call loader_xfer_progress_begin
    call loader_xfer_mark_progress

loader_xfer_get_loop:
; GET reads up to 16 KiB from DOS into transient page 1, then serves that
; contiguous window as ordinary 2,040-byte MCP frames. This preserves the wire
; ABI while reducing DOS_READ calls by roughly eight times.
loader_xfer_get_fast_loop:
    call loader_xfer_position_equals_wire
    jp z,loader_xfer_get_wait_close
    ld hl,(loader_xfer_accumulated)
    ld de,(loader_xfer_accumulator_offset)
    or a
    sbc hl,de
    jr nz,loader_xfer_get_fast_emit

    call loader_xfer_wire_remaining
    ld hl,XFER_FAST_ACCUMULATOR_CAPACITY
    push hl
    ld hl,(loader_xfer_remaining + 2)
    ld a,h
    or l
    pop hl
    jr nz,loader_xfer_get_fast_refill_count_ready
    ld de,(loader_xfer_remaining)
    push hl
    or a
    sbc hl,de
    pop hl
    jr c,loader_xfer_get_fast_refill_count_ready
    jr z,loader_xfer_get_fast_refill_count_ready
    ex de,hl
loader_xfer_get_fast_refill_count_ready:
    ld (loader_xfer_accumulated),hl
    xor a
    ld (loader_xfer_accumulator_offset),a
    ld (loader_xfer_accumulator_offset + 1),a
    ld a,(loader_xfer_handle)
    ld b,a
    ld de,loader_xfer_accumulator
    ld c,DOS_READ
    push hl
    call 00005h
    pop de
    or a
    jp nz,loader_xfer_open_error
    or a
    sbc hl,de
    jp nz,loader_xfer_internal_open_error

loader_xfer_get_fast_emit:
    ld hl,(loader_xfer_accumulated)
    ld de,(loader_xfer_accumulator_offset)
    or a
    sbc hl,de
    ld de,XFER_FAST_GET_CAPACITY
    push hl
    or a
    sbc hl,de
    pop hl
    jr c,loader_xfer_get_fast_count_ready
    jr z,loader_xfer_get_fast_count_ready
    ex de,hl
loader_xfer_get_fast_count_ready:
    ld (loader_xfer_block_length),hl
    ld hl,loader_xfer_accumulator
    ld de,(loader_xfer_accumulator_offset)
    add hl,de
    ld de,loader_xfer_buffer + 6
    ld bc,(loader_xfer_block_length)
    ldir
    ld hl,(loader_xfer_block_length)
    ld (loader_xfer_buffer),hl
    ld de,loader_xfer_buffer + 2
    call loader_xfer_crc_final_to
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_GET_PUBLISH
    call loader_xfer_tsr_call
    or a
    jp nz,loader_xfer_internal_open_error

loader_xfer_get_wait_ack:
    call loader_xfer_fast_pump
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_GET_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 3
    jp z,loader_xfer_get_rewind
    cp 1
    jr z,loader_xfer_get_acked
    call loader_xfer_wait_timeout
    jp nc,loader_xfer_timeout_error
    halt
    jr loader_xfer_get_wait_ack
loader_xfer_get_acked:
    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_position
    ld hl,(loader_xfer_accumulator_offset)
    ld de,(loader_xfer_block_length)
    add hl,de
    ld (loader_xfer_accumulator_offset),hl
    ld de,(loader_xfer_accumulated)
    or a
    sbc hl,de
    jr nz,loader_xfer_get_fast_acked_partial
    ld hl,(loader_xfer_accumulated)
    ld (loader_xfer_block_length),hl
    call loader_xfer_progress_after_block
    xor a
    ld (loader_xfer_accumulated),a
    ld (loader_xfer_accumulated + 1),a
    ld (loader_xfer_accumulator_offset),a
    ld (loader_xfer_accumulator_offset + 1),a
    jr loader_xfer_get_acked_progress
loader_xfer_get_fast_acked_partial:
    ; Avoid a console redraw for each transport frame; the complete 16 KiB DOS
    ; read is the visible progress/rate unit. Still refresh the timeout witness.
    call loader_xfer_mark_progress
    jp loader_xfer_get_loop
loader_xfer_get_acked_progress:
    call loader_xfer_mark_progress
    jp loader_xfer_get_loop

loader_xfer_get_wait_close:
    call loader_xfer_fast_pump
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_GET_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 3
    jr z,loader_xfer_get_rewind
    cp 2
    jr z,loader_xfer_get_close
    call loader_xfer_wait_timeout
    jp nc,loader_xfer_timeout_error
    halt
    jr loader_xfer_get_wait_close

loader_xfer_get_close:
    call loader_xfer_apply_fast_final_rate
    jr loader_xfer_get_finalize

loader_xfer_get_rewind:
    ; FAST_BEGIN after reconnect rolls the uncheckpointed TCP window back to
    ; the last host-fsync boundary. Reuse the open DOS handle and continue the
    ; same transfer ID without a second helper process or application ACKs.
    ld hl,loader_xfer_buffer
    ld de,loader_xfer_position
    ld bc,4
    ldir
    xor a
    ld (loader_xfer_accumulated),a
    ld (loader_xfer_accumulated + 1),a
    ld (loader_xfer_accumulator_offset),a
    ld (loader_xfer_accumulator_offset + 1),a
    call loader_xfer_seek_position
    or a
    jp nz,loader_xfer_open_error
    call loader_xfer_progress_begin
    call loader_xfer_mark_progress
    jp loader_xfer_get_loop

loader_xfer_get_finalize:
    call loader_xfer_close_primary
    or a
    jp nz,loader_xfer_closed_error
    call loader_xfer_enter_postprocess
    or a
    jp nz,loader_xfer_closed_error
    ld de,loader_xfer_get_ok_message
    jp loader_xfer_success_exit

loader_xfer_wait:
    call loader_xfer_wait_timeout
    jp nc,loader_xfer_timeout_error
    halt
    jp loader_xfer_put_loop

; Ask the resident to consume one complete framed request synchronously, then
; return here so this helper remains the sole owner of every DOS operation.
loader_xfer_fast_pump:
    ; The pump validates and retains this page-zero workspace. PUT/GET blocks
    ; stay here rather than allocating another 2 KiB in the MemMan segment.
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_PUMP
    jp loader_xfer_tsr_call

; Initialize DOS2 and claim the staged descriptor. Input A=expected direction.
loader_xfer_initialize:
    ld (loader_xfer_expected_direction),a
    xor a
    ld (dos2_available),a
    ld (loader_xfer_created_meta),a
    ld (loader_xfer_claimed),a
    ld (loader_xfer_output_owned),a
    ld (loader_xfer_phase),a
    ld (loader_xfer_primary_ensured),a
    ld (loader_xfer_accumulated),a
    ld (loader_xfer_accumulated + 1),a
    ld (loader_xfer_accumulator_offset),a
    ld (loader_xfer_accumulator_offset + 1),a
    ld a,INVALID_HANDLE
    ld (loader_xfer_handle),a
    ld (loader_xfer_meta_handle),a
    ld (loader_xfer_output_handle),a
    ld c,DOS_VERSION
    call 00005h
    or a
    ret nz
    ld a,b
    cp 2
    jr c,loader_xfer_initialize_version
    ld a,1
    ld (dos2_available),a
    call memman_find_agent
    jr c,loader_xfer_initialize_internal
    ld (loader_xfer_tsr_id),bc
    ld hl,loader_xfer_descriptor
    ld a,TSR_TALK_XFER_CLAIM
    call loader_xfer_tsr_call
    or a
    jr nz,loader_xfer_initialize_internal
    ld a,1
    ld (loader_xfer_claimed),a
    ld a,(loader_xfer_descriptor + XFER_DESC_DIRECTION)
    ld b,a
    ld a,(loader_xfer_expected_direction)
    cp b
    jr nz,loader_xfer_initialize_metadata
    ld a,(loader_xfer_descriptor + XFER_DESC_FLAGS)
    and XFER_FLAG_FAST_PUMP
    jr z,loader_xfer_initialize_unsupported
    ld a,(loader_xfer_descriptor + XFER_DESC_ENCODING)
    cp XFER_ENCODING_RAW
    jr z,loader_xfer_initialize_encoding_ok
    cp XFER_ENCODING_PACKBITS
    jr nz,loader_xfer_initialize_unsupported
    ld a,(loader_xfer_expected_direction)
    cp XFER_DIRECTION_PUT
    jr nz,loader_xfer_initialize_unsupported
loader_xfer_initialize_encoding_ok:
    call loader_xfer_require_fast_accumulator
    ret nz
    xor a
    ret
loader_xfer_initialize_version:
    ld a,ERR_BAD_VERSION
    ret
loader_xfer_initialize_internal:
    ld a,ERR_INTERNAL
    ret
loader_xfer_initialize_metadata:
    ld a,XFER_ERROR_METADATA
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    ret
loader_xfer_initialize_unsupported:
    ld a,XFER_ERROR_UNSUPPORTED
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    ret

; The transfer helper borrows its complete CPU page 1. It is never passed to
; TsrCall, but it must be inside the documented DOS TPA and leave normal stack
; headroom above 8000h.
loader_xfer_require_fast_accumulator:
    ld hl,(TPA_TOP_POINTER)
    ld de,XFER_FAST_ACCUMULATOR_END + XFER_FAST_STACK_HEADROOM
    or a
    sbc hl,de
    jr c,loader_xfer_fast_no_memory
    ld hl,(xfer_helper_entry_sp)
    ld de,XFER_FAST_ACCUMULATOR_END + XFER_FAST_STACK_HEADROOM
    or a
    sbc hl,de
    jr c,loader_xfer_fast_no_memory
    xor a
    ret
loader_xfer_fast_no_memory:
    ld a,XFER_ERROR_UNSUPPORTED
    ld (loader_xfer_protocol_error),a
    ld a,ERR_NO_MEMORY
    ret

loader_xfer_publish_ready:
    ld hl,loader_xfer_descriptor
    ld a,TSR_TALK_XFER_READY
    jp loader_xfer_tsr_call

loader_xfer_finish_success:
    ld hl,1
    ld a,TSR_TALK_XFER_FINISH
    jp loader_xfer_tsr_call

loader_xfer_enter_postprocess:
    ld hl,0
    ld a,TSR_TALK_XFER_POSTPROCESS
    jp loader_xfer_tsr_call

loader_xfer_finish_failure:
    ld a,(loader_xfer_claimed)
    or a
    ret z
    ld a,(loader_xfer_protocol_error)
    ld h,a
    ld l,0
    ld a,TSR_TALK_XFER_FINISH
    jp loader_xfer_tsr_call

loader_xfer_tsr_call:
    ld bc,(loader_xfer_tsr_id)
    ld d,'M'
    ld e,63
    call EXTBIO
    ei
    ret

; Construct same-directory collision-safe names `xxxxxxxx.PRT` and `.MTD`
; from the random transfer ID. RENAME later receives only the target basename,
; as required by DOS2 function 4Eh.
loader_xfer_build_paths:
    ld hl,loader_xfer_descriptor + XFER_DESC_PATH
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
loader_xfer_find_basename:
    ld a,(hl)
    or a
    jr z,loader_xfer_basename_ready
    cp ':'
    jr z,loader_xfer_record_basename
    cp 05Ch
    jr z,loader_xfer_record_basename
    cp '/'
    jr nz,loader_xfer_find_next
loader_xfer_record_basename:
    push hl
    inc hl
    ex de,hl
    pop hl
loader_xfer_find_next:
    inc hl
    jr loader_xfer_find_basename
loader_xfer_basename_ready:
    ld (loader_xfer_target_basename),de
    ld hl,loader_xfer_descriptor + XFER_DESC_PATH
    ex de,hl
    or a
    sbc hl,de
    ld (loader_xfer_prefix_length),hl
    ld de,loader_xfer_temp_path
    ld hl,loader_xfer_part_extension
    call loader_xfer_build_one_path
    ld de,loader_xfer_meta_path
    ld hl,loader_xfer_meta_extension
    call loader_xfer_build_one_path
    ld de,loader_xfer_output_path
    ld hl,loader_xfer_output_extension
    jp loader_xfer_build_one_path

loader_xfer_build_one_path:
    ld (loader_xfer_extension_pointer),hl
    push de
    ld hl,loader_xfer_descriptor + XFER_DESC_PATH
    ld bc,(loader_xfer_prefix_length)
    ld a,b
    or c
    jr z,loader_xfer_build_prefix_done
    ldir
loader_xfer_build_prefix_done:
    ld hl,loader_xfer_descriptor + XFER_DESC_ID
    ld b,4                    ; full 8.3 basename uses 32 transfer-ID bits
loader_xfer_build_id:
    ld a,(hl)
    rrca
    rrca
    rrca
    rrca
    and 00Fh
    call loader_xfer_hex_character
    ld (de),a
    inc de
    ld a,(hl)
    and 00Fh
    call loader_xfer_hex_character
    ld (de),a
    inc de
    inc hl
    djnz loader_xfer_build_id
    ld hl,(loader_xfer_extension_pointer)
loader_xfer_build_extension:
    ld a,(hl)
    ld (de),a
    inc hl
    inc de
    or a
    jr nz,loader_xfer_build_extension
    pop de
    ret

loader_xfer_hex_character:
    cp 10
    jr c,loader_xfer_hex_digit
    add a,'A' - 10
    ret
loader_xfer_hex_digit:
    add a,'0'
    ret

; Prepare a new or resumed PUT partial. Metadata is immutable; current partial
; length and rolling CRC are derived from the actual `.PRT`, so a reset between
; exact WRITE and resident COMMIT can safely advance to the disk-backed length.
loader_xfer_prepare_put:
    ld a,(loader_xfer_descriptor + XFER_DESC_FLAGS)
    and XFER_FLAG_RESUME
    jr z,loader_xfer_prepare_new
    ld de,loader_xfer_meta_path
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    jr z,loader_xfer_prepare_resume_meta
    cp ERR_NO_FILE
    ret nz
    ; A missing sidecar is normally a transfer that never started. Only the
    ; host's fsync-backed close-intent flag proves that an earlier helper
    ; reached the complete durable boundary and may have published before its
    ; terminal reply was lost.
    ld a,(loader_xfer_descriptor + XFER_DESC_FLAGS)
    and XFER_FLAG_RECEIPTLESS_REPLAY
    jr z,loader_xfer_prepare_missing_partial
    call loader_xfer_requested_resume_equals_wire
    jp nz,loader_xfer_prepare_metadata_error
    call loader_xfer_prepare_receiptless_published
    or a
    ret z
    cp ERR_NO_FILE
    ret nz
    ; An empty target that has not yet been published is still a legitimate
    ; new transfer. Non-empty complete replays must fail closed here.
    call loader_xfer_requested_resume_is_zero
    jp nz,loader_xfer_prepare_metadata_error
    jr loader_xfer_prepare_new
loader_xfer_prepare_missing_partial:
    call loader_xfer_requested_resume_is_zero
    jr z,loader_xfer_prepare_new
    jp loader_xfer_prepare_metadata_error
loader_xfer_prepare_new:
    call loader_xfer_requested_resume_is_zero
    jp nz,loader_xfer_prepare_metadata_error
    call loader_xfer_create_metadata
    or a
    ret nz
    ld de,loader_xfer_temp_path
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jr nz,loader_xfer_prepare_new_temp_error
    ld a,b
    ld (loader_xfer_handle),a
    call loader_xfer_crc_reset
    call loader_xfer_zero_position
    xor a
    ret
loader_xfer_prepare_new_temp_error:
    push af
    ld de,loader_xfer_meta_path
    ld c,DOS_DELETE
    call 00005h
    pop af
    ret

loader_xfer_prepare_resume_meta:
    ld a,b
    ld (loader_xfer_meta_handle),a
    call loader_xfer_read_validate_metadata
    push af
    call loader_xfer_close_metadata
    pop af
    or a
    ret nz
    ld a,(loader_xfer_phase)
    cp XFER_PHASE_PUBLISHING
    jp nc,loader_xfer_prepare_published_wire
    ld de,loader_xfer_temp_path
    ld a,OPEN_READ_WRITE
    ld c,DOS_OPEN
    call 00005h
    or a
    ret nz
    ld a,b
    ld (loader_xfer_handle),a
    call loader_xfer_seek_end
    or a
    ret nz
    ; actual partial may be ahead of last host-confirmed durable after a lost
    ; COMMIT reply, but it can never exceed the immutable complete size.
    call loader_xfer_position_not_after_wire
    jp c,loader_xfer_prepare_range_error
    ld hl,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld de,loader_xfer_scan_limit
    ld bc,4
    ldir
    call loader_xfer_limit_not_after_position
    jp c,loader_xfer_prepare_range_error
    call loader_xfer_scan_exact
    or a
    ret nz
    ld hl,loader_xfer_descriptor + XFER_DESC_PREFIX_CRC
    call loader_xfer_crc_matches
    jp nz,loader_xfer_prepare_crc_error
    ld hl,loader_xfer_position
    ld de,loader_xfer_scan_limit
    ld bc,4
    ldir
    call loader_xfer_scan_exact
    or a
    ret nz
    ld hl,loader_xfer_position
    ld de,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld bc,4
    ldir
    ld de,loader_xfer_descriptor + XFER_DESC_PREFIX_CRC
    call loader_xfer_crc_final_to
    jp loader_xfer_seek_position

; PUBLISHING/PUBLISHED means the wire representation was already verified and
; its final source may already have been atomically renamed. Re-advertise the
; complete wire boundary; publication recovery validates the actual target or
; source bytes before FINISH is allowed.
loader_xfer_prepare_published_wire:
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld de,loader_xfer_position
    ld bc,4
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld de,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld bc,4
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    ld de,loader_xfer_descriptor + XFER_DESC_PREFIX_CRC
    ld bc,4
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    ld de,loader_xfer_crc
    ld b,4
loader_xfer_prepare_published_crc:
    ld a,(hl)
    cpl
    ld (de),a
    inc hl
    inc de
    djnz loader_xfer_prepare_published_crc
    xor a
    ret

; A successful helper removes its sidecar after FINISH. If the terminal reply
; is then lost across a machine reset, the host's fsync-backed journal still
; presents the complete wire boundary. Accept that receiptless replay only
; when the full wire CRC and the existing final target both match exactly.
; This path never creates, deletes, renames, or overwrites a file.
loader_xfer_prepare_receiptless_published:
    ld hl,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld b,4
    call loader_xfer_compare_bytes
    jp nz,loader_xfer_prepare_metadata_error
    ld hl,loader_xfer_descriptor + XFER_DESC_PREFIX_CRC
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_CRC
    ld b,4
    call loader_xfer_compare_bytes
    jp nz,loader_xfer_prepare_crc_error
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    call loader_xfer_validate_final_path
    or a
    ret nz
    ld a,XFER_PHASE_PUBLISHED
    ld (loader_xfer_phase),a
    jp loader_xfer_prepare_published_wire
loader_xfer_prepare_metadata_error:
    ld a,XFER_ERROR_METADATA
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    ret
loader_xfer_prepare_range_error:
    ld a,XFER_ERROR_RANGE
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    ret
loader_xfer_prepare_crc_error:
    ld a,XFER_ERROR_CRC
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    ret

loader_xfer_requested_resume_is_zero:
    ld hl,(loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET)
    ld a,h
    or l
    ret nz
    ld hl,(loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET + 2)
    ld a,h
    or l
    ret

loader_xfer_requested_resume_equals_wire:
    ld hl,loader_xfer_descriptor + XFER_DESC_RESUME_OFFSET
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld b,4
    jp loader_xfer_compare_bytes

; Transaction sidecar: immutable binding(107 bytes), then a phase byte and its
; complement. Every phase is ENSUREd before the corresponding destructive
; boundary, so reset recovery can distinguish owned scratch/output files from
; unrelated pre-existing names and can finish an already-completed rename.
loader_xfer_create_metadata:
    ld hl,loader_xfer_buffer
    ld de,loader_xfer_buffer + 1
    ld bc,XFER_META_SIZE - 1
    xor a
    ld (hl),a
    ldir
    ld hl,loader_xfer_meta_magic
    ld de,loader_xfer_buffer
    ld bc,8
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_ID
    ld bc,16
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_DIRECTION
    ld bc,2
    ldir
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld bc,16
    ldir
    ld a,(loader_xfer_descriptor + XFER_DESC_PATH_LENGTH)
    ld (de),a
    inc de
    ld c,a
    ld b,0
    ld hl,loader_xfer_descriptor + XFER_DESC_PATH
    ldir
    ld a,XFER_PHASE_RECEIVING
    ld (loader_xfer_buffer + XFER_META_PHASE),a
    cpl
    ld (loader_xfer_buffer + XFER_META_PHASE_INV),a
    ld de,loader_xfer_meta_path
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    ret nz
    ld a,b
    ld (loader_xfer_meta_handle),a
    ld a,1
    ld (loader_xfer_created_meta),a
    ld de,loader_xfer_buffer
    ld hl,XFER_META_SIZE
    call write_exact
    or a
    jr nz,loader_xfer_create_metadata_close
    ld a,(loader_xfer_meta_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
loader_xfer_create_metadata_close:
    push af
    call loader_xfer_close_metadata
    ld b,a
    pop af
    or a
    jr nz,loader_xfer_create_metadata_failed
    ld a,b
    or a
    jr nz,loader_xfer_create_metadata_failed
    xor a
    ret
loader_xfer_create_metadata_failed:
    push af
    ld de,loader_xfer_meta_path
    ld c,DOS_DELETE
    call 00005h
    xor a
    ld (loader_xfer_created_meta),a
    pop af
    ret

loader_xfer_read_validate_metadata:
    ld a,(loader_xfer_meta_handle)
    ld b,a
    ld de,loader_xfer_buffer
    ld hl,XFER_META_SIZE
    ld c,DOS_READ
    push hl
    call 00005h
    pop de
    or a
    ret nz
    or a
    sbc hl,de
    jr nz,loader_xfer_metadata_bad
    ld hl,loader_xfer_buffer
    ld de,loader_xfer_meta_magic
    ld b,8
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_metadata_bad
    ld hl,loader_xfer_buffer + 8
    ld de,loader_xfer_descriptor + XFER_DESC_ID
    ld b,16
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_metadata_bad
    ld hl,loader_xfer_buffer + 24
    ld de,loader_xfer_descriptor + XFER_DESC_DIRECTION
    ld b,2
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_metadata_bad
    ld hl,loader_xfer_buffer + 26
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld b,16
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_metadata_bad
    ld a,(loader_xfer_buffer + 42)
    ld b,a
    ld a,(loader_xfer_descriptor + XFER_DESC_PATH_LENGTH)
    cp b
    jr nz,loader_xfer_metadata_bad
    ld hl,loader_xfer_buffer + XFER_META_PATH
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_metadata_bad
    ld a,(loader_xfer_buffer + XFER_META_PHASE_INV)
    cpl
    ld b,a
    ld a,(loader_xfer_buffer + XFER_META_PHASE)
    cp b
    jr nz,loader_xfer_metadata_bad
    cp XFER_PHASE_PUBLISHED + 1
    jr nc,loader_xfer_metadata_bad
    ld c,a
    ld a,(loader_xfer_descriptor + XFER_DESC_ENCODING)
    cp XFER_ENCODING_RAW
    jr nz,loader_xfer_metadata_phase_valid
    ld a,c
    cp XFER_PHASE_DECODING
    jr z,loader_xfer_metadata_bad
loader_xfer_metadata_phase_valid:
    ld a,c
    ld (loader_xfer_phase),a
    xor a
    ret
loader_xfer_metadata_bad:
    ld a,ERR_INVALID_PARAMETER
    ret

loader_xfer_compare_bytes:
    ld a,b
    or a
    jr z,loader_xfer_compare_equal
loader_xfer_compare_loop:
    ld a,(de)
    cp (hl)
    ret nz
    inc de
    inc hl
    djnz loader_xfer_compare_loop
loader_xfer_compare_equal:
    xor a
    ret

; Persist a monotonic transaction phase and its complement. Input A=new phase.
; Updating the in-memory phase happens only after WRITE, ENSURE, and CLOSE all
; succeed; a torn phase pair therefore fails metadata validation on restart.
loader_xfer_set_phase:
    ld b,a
    ld a,(loader_xfer_phase)
    cp b
    jr z,loader_xfer_set_phase_same
    jr nc,loader_xfer_set_phase_bad
    ld a,b
    ld (loader_xfer_buffer),a
    cpl
    ld (loader_xfer_buffer + 1),a
    ld de,loader_xfer_meta_path
    ld a,OPEN_READ_WRITE
    ld c,DOS_OPEN
    call 00005h
    or a
    ret nz
    ld a,b
    ld (loader_xfer_meta_handle),a
    ld hl,XFER_META_PHASE
    ld de,0
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    or a
    jr nz,loader_xfer_set_phase_close
    ld a,(loader_xfer_meta_handle)
    ld b,a
    ld de,loader_xfer_buffer
    ld hl,2
    call write_exact
    or a
    jr nz,loader_xfer_set_phase_close
    ld a,(loader_xfer_meta_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
loader_xfer_set_phase_close:
    push af
    call loader_xfer_close_metadata
    ld b,a
    pop af
    or a
    ret nz
    ld a,b
    or a
    ret nz
    ld a,(loader_xfer_buffer)
    ld (loader_xfer_phase),a
loader_xfer_set_phase_same:
    xor a
    ret
loader_xfer_set_phase_bad:
    ld a,ERR_INVALID_PARAMETER
    ret

; Decode a complete standard PackBits stream with fixed memory. The wire file
; remains the resumable artifact; the CREATE_NEW-owned output is disposable
; until exact final size and CRC-32 have both been verified.
loader_xfer_put_packbits_finalize:
    ld a,(loader_xfer_phase)
    cp XFER_PHASE_PUBLISHING
    jp nc,loader_xfer_packbits_publish_recovery
    cp XFER_PHASE_DECODING
    jr z,loader_xfer_packbits_restart_decode

    ; Establish the ownership boundary before CREATE_NEW. A pre-existing .OUT
    ; is never deleted; only a later restart in durable DECODING phase may
    ; discard the scratch file created by this transfer.
    ld de,loader_xfer_output_path
    call loader_xfer_require_absent
    or a
    jp nz,loader_xfer_packbits_create_error
    ld a,XFER_PHASE_DECODING
    call loader_xfer_set_phase
    or a
    jp nz,loader_xfer_closed_error
    jr loader_xfer_packbits_open_input

loader_xfer_packbits_restart_decode:
    ld de,loader_xfer_output_path
    ld c,DOS_DELETE
    call 00005h
    or a
    jr z,loader_xfer_packbits_open_input
    cp ERR_NO_FILE
    jp nz,loader_xfer_closed_error

loader_xfer_packbits_open_input:
    ld de,loader_xfer_temp_path
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    jp nz,loader_xfer_closed_error
    ld a,b
    ld (loader_xfer_handle),a
    ld de,loader_xfer_output_path
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jp nz,loader_xfer_packbits_create_error
    ld a,b
    ld (loader_xfer_output_handle),a
    ld a,1
    ld (loader_xfer_output_owned),a
    call loader_xfer_crc_reset
    call loader_xfer_zero_position
    call loader_xfer_zero_scan_count

loader_xfer_packbits_loop:
    call loader_xfer_position_equals_wire
    jp z,loader_xfer_packbits_complete
    ld de,loader_xfer_buffer
    ld hl,1
    call loader_xfer_packbits_read_exact
    or a
    jp nz,loader_xfer_packbits_metadata_error
    ld a,(loader_xfer_buffer)
    cp 080h
    jp z,loader_xfer_packbits_metadata_error ; reserved no-op is non-canonical
    jr c,loader_xfer_packbits_literal
    cp 0FFh
    jp z,loader_xfer_packbits_metadata_error ; canonical runs are at least 3
    neg
    inc a                       ; 257-control = 3..128
    ld l,a
    ld h,0
    ld (loader_xfer_block_length),hl
    ld de,loader_xfer_buffer
    ld hl,1
    call loader_xfer_packbits_read_exact
    or a
    jp nz,loader_xfer_packbits_metadata_error
    ld a,(loader_xfer_buffer)
    ld hl,loader_xfer_buffer
    ld de,loader_xfer_buffer + 1
    ld bc,(loader_xfer_block_length)
    dec bc
    ld (hl),a
    ldir
    jr loader_xfer_packbits_emit

loader_xfer_packbits_literal:
    inc a                       ; control+1 = 1..128 literal bytes
    ld l,a
    ld h,0
    ld (loader_xfer_block_length),hl
    ld de,loader_xfer_buffer
    call loader_xfer_packbits_read_exact
    or a
    jp nz,loader_xfer_packbits_metadata_error

loader_xfer_packbits_emit:
    call loader_xfer_packbits_final_fits
    jp c,loader_xfer_packbits_metadata_error
    ld bc,(loader_xfer_block_length)
    ld hl,loader_xfer_buffer
    call loader_xfer_crc_update
    ld de,loader_xfer_buffer
    ld hl,(loader_xfer_block_length)
    ld a,(loader_xfer_output_handle)
    ld b,a
    call write_exact
    or a
    jp nz,loader_xfer_closed_error
    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_scan_count
    jp loader_xfer_packbits_loop

loader_xfer_packbits_complete:
    ld hl,loader_xfer_scan_count
    ld de,loader_xfer_descriptor + XFER_DESC_FINAL_SIZE
    ld b,4
    call loader_xfer_compare_bytes
    jp nz,loader_xfer_packbits_metadata_error
    ld hl,loader_xfer_descriptor + XFER_DESC_FINAL_CRC
    call loader_xfer_crc_matches
    jp nz,loader_xfer_packbits_crc_error
    ld a,(loader_xfer_output_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    or a
    jp nz,loader_xfer_closed_error
    call loader_xfer_close_output
    or a
    jp nz,loader_xfer_closed_error
    call loader_xfer_close_primary
    or a
    jp nz,loader_xfer_closed_error
    xor a
    ld (loader_xfer_output_owned),a
    call loader_xfer_publish_output
    or a
    jp nz,loader_xfer_closed_error
    ld de,loader_xfer_temp_path
    ld c,DOS_DELETE
    call 00005h
    ld de,loader_xfer_meta_path
    ld c,DOS_DELETE
    call 00005h                 ; full-journal exact-target replay stays safe
    ld de,loader_xfer_put_ok_message
    jp loader_xfer_success_exit

loader_xfer_packbits_publish_recovery:
    call loader_xfer_publish_output
    or a
    jp nz,loader_xfer_closed_error
    ld de,loader_xfer_temp_path
    ld c,DOS_DELETE
    call 00005h
    ld de,loader_xfer_output_path
    ld c,DOS_DELETE
    call 00005h
    ld de,loader_xfer_meta_path
    ld c,DOS_DELETE
    call 00005h
    ld de,loader_xfer_put_ok_message
    jp loader_xfer_success_exit

loader_xfer_packbits_create_error:
    cp ERR_FILE_EXISTS
    jp nz,loader_xfer_open_error
    ld a,XFER_ERROR_EXISTS
    ld (loader_xfer_protocol_error),a
    ld a,ERR_FILE_EXISTS
    jp loader_xfer_open_error
loader_xfer_packbits_crc_error:
    ld a,XFER_ERROR_CRC
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    jp loader_xfer_closed_error
loader_xfer_packbits_metadata_error:
    ld a,XFER_ERROR_METADATA
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    jp loader_xfer_closed_error

; Read exactly HL encoded bytes into DE without crossing the declared wire
; length. Short reads and truncated packets fail closed. The input position is
; advanced only after a complete read.
loader_xfer_packbits_read_exact:
    push de
    ld (loader_xfer_read_length),hl
    call loader_xfer_wire_remaining
    ld a,(loader_xfer_remaining + 2)
    ld b,a
    ld a,(loader_xfer_remaining + 3)
    or b
    jr nz,loader_xfer_packbits_read_fits
    ld hl,(loader_xfer_remaining)
    ld de,(loader_xfer_read_length)
    or a
    sbc hl,de
    jr c,loader_xfer_packbits_read_range
loader_xfer_packbits_read_fits:
    pop de
    ld hl,(loader_xfer_read_length)
    ld a,(loader_xfer_handle)
    ld b,a
    ld c,DOS_READ
    push hl
    call 00005h
    pop de
    or a
    ret nz
    or a
    sbc hl,de
    jr nz,loader_xfer_packbits_read_short
    ld hl,(loader_xfer_read_length)
    call loader_xfer_add_position
    xor a
    ret
loader_xfer_packbits_read_range:
    pop de
loader_xfer_packbits_read_short:
    ld a,ERR_INVALID_PARAMETER
    ret

; Carry iff emitting the current block would exceed negotiated FINAL_SIZE.
loader_xfer_packbits_final_fits:
    ld hl,(loader_xfer_descriptor + XFER_DESC_FINAL_SIZE)
    ld de,(loader_xfer_scan_count)
    or a
    sbc hl,de
    ld (loader_xfer_remaining),hl
    ld hl,(loader_xfer_descriptor + XFER_DESC_FINAL_SIZE + 2)
    ld de,(loader_xfer_scan_count + 2)
    sbc hl,de
    ret c
    ld (loader_xfer_remaining + 2),hl
    ld a,h
    or l
    ret nz
    ld hl,(loader_xfer_remaining)
    ld de,(loader_xfer_block_length)
    or a
    sbc hl,de
    ret

; Publish a verified PUT with a durable transaction phase. A normal path first
; proves that the target is absent, ENSUREs PUBLISHING, and only then renames.
; A restarted PUBLISHING path accepts an existing target only after exact final
; size/CRC validation, or validates and renames the surviving source. DOS2
; RENAME takes a full old path in DE and only the new basename in HL.
loader_xfer_publish_temp:
    ld hl,loader_xfer_temp_path
    jr loader_xfer_publish_selected
loader_xfer_publish_output:
    ld hl,loader_xfer_output_path
loader_xfer_publish_selected:
    ld (loader_xfer_publish_source),hl
    ld a,(loader_xfer_phase)
    cp XFER_PHASE_PUBLISHED
    jr z,loader_xfer_publish_validate_target
    cp XFER_PHASE_PUBLISHING
    jr z,loader_xfer_publish_recover

    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    call loader_xfer_require_absent
    or a
    ret nz
    ld a,XFER_PHASE_PUBLISHING
    call loader_xfer_set_phase
    or a
    ret nz
    jr loader_xfer_publish_rename

loader_xfer_publish_recover:
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    call loader_xfer_validate_final_path
    or a
    jr z,loader_xfer_publish_mark_published
    cp ERR_NO_FILE
    ret nz
    ld de,(loader_xfer_publish_source)
    call loader_xfer_validate_final_path
    or a
    ret nz
loader_xfer_publish_rename:
    ld de,(loader_xfer_publish_source)
    ld hl,(loader_xfer_target_basename)
    ld c,DOS_RENAME
    call 00005h
    or a
    ret nz
loader_xfer_publish_mark_published:
    ld a,XFER_PHASE_PUBLISHED
    jp loader_xfer_set_phase

loader_xfer_publish_validate_target:
    ld de,loader_xfer_descriptor + XFER_DESC_PATH
    jp loader_xfer_validate_final_path

; Return success only when DE names no file. Existing user data is never
; removed or adopted before a durable phase has established ownership.
loader_xfer_require_absent:
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    jr nz,loader_xfer_require_absent_open_error
    ld c,DOS_CLOSE
    call 00005h
    or a
    ret nz
    ld a,XFER_ERROR_EXISTS
    ld (loader_xfer_protocol_error),a
    ld a,ERR_FILE_EXISTS
    ret
loader_xfer_require_absent_open_error:
    cp ERR_NO_FILE
    ret nz
    xor a
    ret

; Validate one complete final representation named by DE. The reusable scan
; checks exact 32-bit size and CRC, closes on every path, and never modifies or
; deletes the candidate. ERR_NO_FILE is preserved for publication recovery.
loader_xfer_validate_final_path:
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    ret nz
    ld a,b
    ld (loader_xfer_handle),a
    call loader_xfer_seek_end
    or a
    jr nz,loader_xfer_validate_final_close
    ld hl,loader_xfer_position
    ld de,loader_xfer_descriptor + XFER_DESC_FINAL_SIZE
    ld b,4
    call loader_xfer_compare_bytes
    jr nz,loader_xfer_validate_final_bad
    ld hl,loader_xfer_position
    ld de,loader_xfer_scan_limit
    ld bc,4
    ldir
    call loader_xfer_scan_exact
    or a
    jr nz,loader_xfer_validate_final_close
    ld hl,loader_xfer_descriptor + XFER_DESC_FINAL_CRC
    call loader_xfer_crc_matches
    jr nz,loader_xfer_validate_final_bad
    xor a
    jr loader_xfer_validate_final_close
loader_xfer_validate_final_bad:
    ld a,XFER_ERROR_CRC
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
loader_xfer_validate_final_close:
    push af
    call loader_xfer_close_primary
    ld b,a
    pop af
    or a
    ret nz
    ld a,b
    or a
    ret

; Exact CRC scan from offset zero through scan_limit. The known-size contract
; makes any short READ an error and naturally supports 32-bit counts >64 KiB.
loader_xfer_scan_exact:
    call loader_xfer_crc_reset
    call loader_xfer_zero_scan_count
    call loader_xfer_seek_zero
    or a
    ret nz
loader_xfer_scan_loop:
    call loader_xfer_scan_done
    ret z
    call loader_xfer_scan_remaining
    ld hl,(loader_xfer_remaining + 2)
    ld a,h
    or l
    ld hl,XFER_WORK_CAPACITY
    jr nz,loader_xfer_scan_count_ready
    ld de,(loader_xfer_remaining)
    push hl
    or a
    sbc hl,de
    pop hl
    jr c,loader_xfer_scan_count_ready
    jr z,loader_xfer_scan_count_ready
    ex de,hl
loader_xfer_scan_count_ready:
    ld (loader_xfer_block_length),hl
    ld a,(loader_xfer_handle)
    ld b,a
    ld de,loader_xfer_buffer
    ld c,DOS_READ
    push hl
    call 00005h
    pop de
    or a
    ret nz
    or a
    sbc hl,de
    jr nz,loader_xfer_scan_short
    ld bc,(loader_xfer_block_length)
    ld hl,loader_xfer_buffer
    call loader_xfer_crc_update
    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_scan_count
    jr loader_xfer_scan_loop
loader_xfer_scan_short:
    ld a,ERR_INTERNAL
    ret

loader_xfer_crc_reset:
    ld hl,0FFFFh
    ld (loader_xfer_crc),hl
    ld (loader_xfer_crc + 2),hl
    ret

; Reflected IEEE CRC-32 (polynomial EDB88320h), kept internally before the
; final XOR. Input HL=buffer, BC=count.
loader_xfer_crc_update:
    ld a,b
    or c
    ret z
loader_xfer_crc_byte:
    ld a,(loader_xfer_crc)
    xor (hl)
    ld e,a
    ld d,0
    sla e
    rl d
    sla e
    rl d
    inc hl
    push hl
    push bc
    ld a,(loader_xfer_crc + 1)
    ld (loader_xfer_crc),a
    ld a,(loader_xfer_crc + 2)
    ld (loader_xfer_crc + 1),a
    ld a,(loader_xfer_crc + 3)
    ld (loader_xfer_crc + 2),a
    xor a
    ld (loader_xfer_crc + 3),a
    ld hl,loader_xfer_crc_table
    add hl,de
    ld a,(loader_xfer_crc)
    xor (hl)
    ld (loader_xfer_crc),a
    inc hl
    ld a,(loader_xfer_crc + 1)
    xor (hl)
    ld (loader_xfer_crc + 1),a
    inc hl
    ld a,(loader_xfer_crc + 2)
    xor (hl)
    ld (loader_xfer_crc + 2),a
    inc hl
    ld a,(loader_xfer_crc + 3)
    xor (hl)
    ld (loader_xfer_crc + 3),a
    pop bc
    pop hl
    dec bc
    ld a,b
    or c
    jr nz,loader_xfer_crc_byte
    ret

; Byte-indexed table keeps end-to-end CRC-32 integrity without eight
; polynomial rounds per byte. It lives only in transient MSXAIXF.COM.
loader_xfer_crc_table:
    db 000h,000h,000h,000h,096h,030h,007h,077h,02Ch,061h,00Eh,0EEh,0BAh,051h,009h,099h
    db 019h,0C4h,06Dh,007h,08Fh,0F4h,06Ah,070h,035h,0A5h,063h,0E9h,0A3h,095h,064h,09Eh
    db 032h,088h,0DBh,00Eh,0A4h,0B8h,0DCh,079h,01Eh,0E9h,0D5h,0E0h,088h,0D9h,0D2h,097h
    db 02Bh,04Ch,0B6h,009h,0BDh,07Ch,0B1h,07Eh,007h,02Dh,0B8h,0E7h,091h,01Dh,0BFh,090h
    db 064h,010h,0B7h,01Dh,0F2h,020h,0B0h,06Ah,048h,071h,0B9h,0F3h,0DEh,041h,0BEh,084h
    db 07Dh,0D4h,0DAh,01Ah,0EBh,0E4h,0DDh,06Dh,051h,0B5h,0D4h,0F4h,0C7h,085h,0D3h,083h
    db 056h,098h,06Ch,013h,0C0h,0A8h,06Bh,064h,07Ah,0F9h,062h,0FDh,0ECh,0C9h,065h,08Ah
    db 04Fh,05Ch,001h,014h,0D9h,06Ch,006h,063h,063h,03Dh,00Fh,0FAh,0F5h,00Dh,008h,08Dh
    db 0C8h,020h,06Eh,03Bh,05Eh,010h,069h,04Ch,0E4h,041h,060h,0D5h,072h,071h,067h,0A2h
    db 0D1h,0E4h,003h,03Ch,047h,0D4h,004h,04Bh,0FDh,085h,00Dh,0D2h,06Bh,0B5h,00Ah,0A5h
    db 0FAh,0A8h,0B5h,035h,06Ch,098h,0B2h,042h,0D6h,0C9h,0BBh,0DBh,040h,0F9h,0BCh,0ACh
    db 0E3h,06Ch,0D8h,032h,075h,05Ch,0DFh,045h,0CFh,00Dh,0D6h,0DCh,059h,03Dh,0D1h,0ABh
    db 0ACh,030h,0D9h,026h,03Ah,000h,0DEh,051h,080h,051h,0D7h,0C8h,016h,061h,0D0h,0BFh
    db 0B5h,0F4h,0B4h,021h,023h,0C4h,0B3h,056h,099h,095h,0BAh,0CFh,00Fh,0A5h,0BDh,0B8h
    db 09Eh,0B8h,002h,028h,008h,088h,005h,05Fh,0B2h,0D9h,00Ch,0C6h,024h,0E9h,00Bh,0B1h
    db 087h,07Ch,06Fh,02Fh,011h,04Ch,068h,058h,0ABh,01Dh,061h,0C1h,03Dh,02Dh,066h,0B6h
    db 090h,041h,0DCh,076h,006h,071h,0DBh,001h,0BCh,020h,0D2h,098h,02Ah,010h,0D5h,0EFh
    db 089h,085h,0B1h,071h,01Fh,0B5h,0B6h,006h,0A5h,0E4h,0BFh,09Fh,033h,0D4h,0B8h,0E8h
    db 0A2h,0C9h,007h,078h,034h,0F9h,000h,00Fh,08Eh,0A8h,009h,096h,018h,098h,00Eh,0E1h
    db 0BBh,00Dh,06Ah,07Fh,02Dh,03Dh,06Dh,008h,097h,06Ch,064h,091h,001h,05Ch,063h,0E6h
    db 0F4h,051h,06Bh,06Bh,062h,061h,06Ch,01Ch,0D8h,030h,065h,085h,04Eh,000h,062h,0F2h
    db 0EDh,095h,006h,06Ch,07Bh,0A5h,001h,01Bh,0C1h,0F4h,008h,082h,057h,0C4h,00Fh,0F5h
    db 0C6h,0D9h,0B0h,065h,050h,0E9h,0B7h,012h,0EAh,0B8h,0BEh,08Bh,07Ch,088h,0B9h,0FCh
    db 0DFh,01Dh,0DDh,062h,049h,02Dh,0DAh,015h,0F3h,07Ch,0D3h,08Ch,065h,04Ch,0D4h,0FBh
    db 058h,061h,0B2h,04Dh,0CEh,051h,0B5h,03Ah,074h,000h,0BCh,0A3h,0E2h,030h,0BBh,0D4h
    db 041h,0A5h,0DFh,04Ah,0D7h,095h,0D8h,03Dh,06Dh,0C4h,0D1h,0A4h,0FBh,0F4h,0D6h,0D3h
    db 06Ah,0E9h,069h,043h,0FCh,0D9h,06Eh,034h,046h,088h,067h,0ADh,0D0h,0B8h,060h,0DAh
    db 073h,02Dh,004h,044h,0E5h,01Dh,003h,033h,05Fh,04Ch,00Ah,0AAh,0C9h,07Ch,00Dh,0DDh
    db 03Ch,071h,005h,050h,0AAh,041h,002h,027h,010h,010h,00Bh,0BEh,086h,020h,00Ch,0C9h
    db 025h,0B5h,068h,057h,0B3h,085h,06Fh,020h,009h,0D4h,066h,0B9h,09Fh,0E4h,061h,0CEh
    db 00Eh,0F9h,0DEh,05Eh,098h,0C9h,0D9h,029h,022h,098h,0D0h,0B0h,0B4h,0A8h,0D7h,0C7h
    db 017h,03Dh,0B3h,059h,081h,00Dh,0B4h,02Eh,03Bh,05Ch,0BDh,0B7h,0ADh,06Ch,0BAh,0C0h
    db 020h,083h,0B8h,0EDh,0B6h,0B3h,0BFh,09Ah,00Ch,0E2h,0B6h,003h,09Ah,0D2h,0B1h,074h
    db 039h,047h,0D5h,0EAh,0AFh,077h,0D2h,09Dh,015h,026h,0DBh,004h,083h,016h,0DCh,073h
    db 012h,00Bh,063h,0E3h,084h,03Bh,064h,094h,03Eh,06Ah,06Dh,00Dh,0A8h,05Ah,06Ah,07Ah
    db 00Bh,0CFh,00Eh,0E4h,09Dh,0FFh,009h,093h,027h,0AEh,000h,00Ah,0B1h,09Eh,007h,07Dh
    db 044h,093h,00Fh,0F0h,0D2h,0A3h,008h,087h,068h,0F2h,001h,01Eh,0FEh,0C2h,006h,069h
    db 05Dh,057h,062h,0F7h,0CBh,067h,065h,080h,071h,036h,06Ch,019h,0E7h,006h,06Bh,06Eh
    db 076h,01Bh,0D4h,0FEh,0E0h,02Bh,0D3h,089h,05Ah,07Ah,0DAh,010h,0CCh,04Ah,0DDh,067h
    db 06Fh,0DFh,0B9h,0F9h,0F9h,0EFh,0BEh,08Eh,043h,0BEh,0B7h,017h,0D5h,08Eh,0B0h,060h
    db 0E8h,0A3h,0D6h,0D6h,07Eh,093h,0D1h,0A1h,0C4h,0C2h,0D8h,038h,052h,0F2h,0DFh,04Fh
    db 0F1h,067h,0BBh,0D1h,067h,057h,0BCh,0A6h,0DDh,006h,0B5h,03Fh,04Bh,036h,0B2h,048h
    db 0DAh,02Bh,00Dh,0D8h,04Ch,01Bh,00Ah,0AFh,0F6h,04Ah,003h,036h,060h,07Ah,004h,041h
    db 0C3h,0EFh,060h,0DFh,055h,0DFh,067h,0A8h,0EFh,08Eh,06Eh,031h,079h,0BEh,069h,046h
    db 08Ch,0B3h,061h,0CBh,01Ah,083h,066h,0BCh,0A0h,0D2h,06Fh,025h,036h,0E2h,068h,052h
    db 095h,077h,00Ch,0CCh,003h,047h,00Bh,0BBh,0B9h,016h,002h,022h,02Fh,026h,005h,055h
    db 0BEh,03Bh,0BAh,0C5h,028h,00Bh,0BDh,0B2h,092h,05Ah,0B4h,02Bh,004h,06Ah,0B3h,05Ch
    db 0A7h,0FFh,0D7h,0C2h,031h,0CFh,0D0h,0B5h,08Bh,09Eh,0D9h,02Ch,01Dh,0AEh,0DEh,05Bh
    db 0B0h,0C2h,064h,09Bh,026h,0F2h,063h,0ECh,09Ch,0A3h,06Ah,075h,00Ah,093h,06Dh,002h
    db 0A9h,006h,009h,09Ch,03Fh,036h,00Eh,0EBh,085h,067h,007h,072h,013h,057h,000h,005h
    db 082h,04Ah,0BFh,095h,014h,07Ah,0B8h,0E2h,0AEh,02Bh,0B1h,07Bh,038h,01Bh,0B6h,00Ch
    db 09Bh,08Eh,0D2h,092h,00Dh,0BEh,0D5h,0E5h,0B7h,0EFh,0DCh,07Ch,021h,0DFh,0DBh,00Bh
    db 0D4h,0D2h,0D3h,086h,042h,0E2h,0D4h,0F1h,0F8h,0B3h,0DDh,068h,06Eh,083h,0DAh,01Fh
    db 0CDh,016h,0BEh,081h,05Bh,026h,0B9h,0F6h,0E1h,077h,0B0h,06Fh,077h,047h,0B7h,018h
    db 0E6h,05Ah,008h,088h,070h,06Ah,00Fh,0FFh,0CAh,03Bh,006h,066h,05Ch,00Bh,001h,011h
    db 0FFh,09Eh,065h,08Fh,069h,0AEh,062h,0F8h,0D3h,0FFh,06Bh,061h,045h,0CFh,06Ch,016h
    db 078h,0E2h,00Ah,0A0h,0EEh,0D2h,00Dh,0D7h,054h,083h,004h,04Eh,0C2h,0B3h,003h,039h
    db 061h,026h,067h,0A7h,0F7h,016h,060h,0D0h,04Dh,047h,069h,049h,0DBh,077h,06Eh,03Eh
    db 04Ah,06Ah,0D1h,0AEh,0DCh,05Ah,0D6h,0D9h,066h,00Bh,0DFh,040h,0F0h,03Bh,0D8h,037h
    db 053h,0AEh,0BCh,0A9h,0C5h,09Eh,0BBh,0DEh,07Fh,0CFh,0B2h,047h,0E9h,0FFh,0B5h,030h
    db 01Ch,0F2h,0BDh,0BDh,08Ah,0C2h,0BAh,0CAh,030h,093h,0B3h,053h,0A6h,0A3h,0B4h,024h
    db 005h,036h,0D0h,0BAh,093h,006h,0D7h,0CDh,029h,057h,0DEh,054h,0BFh,067h,0D9h,023h
    db 02Eh,07Ah,066h,0B3h,0B8h,04Ah,061h,0C4h,002h,01Bh,068h,05Dh,094h,02Bh,06Fh,02Ah
    db 037h,0BEh,00Bh,0B4h,0A1h,08Eh,00Ch,0C3h,01Bh,0DFh,005h,05Ah,08Dh,0EFh,002h,02Dh

loader_xfer_crc_final_to:
    ld hl,loader_xfer_crc
    ld b,4
loader_xfer_crc_final_loop:
    ld a,(hl)
    cpl
    ld (de),a
    inc hl
    inc de
    djnz loader_xfer_crc_final_loop
    ret

loader_xfer_crc_matches:
    ld de,loader_xfer_crc
    ld b,4
loader_xfer_crc_match_loop:
    ld a,(de)
    cpl
    cp (hl)
    ret nz
    inc de
    inc hl
    djnz loader_xfer_crc_match_loop
    xor a
    ret

loader_xfer_seek_zero:
    ld hl,0
    ld de,0
    ld a,(loader_xfer_handle)
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    ret

loader_xfer_seek_position:
    ld hl,(loader_xfer_position)
    ld de,(loader_xfer_position + 2)
    ld a,(loader_xfer_handle)
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    ret

loader_xfer_seek_end:
    ld hl,0
    ld de,0
    ld a,(loader_xfer_handle)
    ld b,a
    ld a,2
    ld c,DOS_SEEK
    call 00005h
    or a
    ret nz
    ld (loader_xfer_position),hl
    ld (loader_xfer_position + 2),de
    ret

loader_xfer_zero_position:
    xor a
    ld hl,loader_xfer_position
    ld (hl),a
    inc hl
    ld (hl),a
    inc hl
    ld (hl),a
    inc hl
    ld (hl),a
    ret
loader_xfer_zero_scan_count:
    xor a
    ld hl,loader_xfer_scan_count
    ld (hl),a
    inc hl
    ld (hl),a
    inc hl
    ld (hl),a
    inc hl
    ld (hl),a
    ret

loader_xfer_scan_done:
    ld hl,loader_xfer_scan_count
    ld de,loader_xfer_scan_limit
    ld b,4
    jp loader_xfer_compare_bytes

loader_xfer_position_equals_wire:
    ld hl,loader_xfer_position
    ld de,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld b,4
    jp loader_xfer_compare_bytes

loader_xfer_scan_remaining:
    ld hl,(loader_xfer_scan_limit)
    ld de,(loader_xfer_scan_count)
    or a
    sbc hl,de
    ld (loader_xfer_remaining),hl
    ld hl,(loader_xfer_scan_limit + 2)
    ld de,(loader_xfer_scan_count + 2)
    sbc hl,de
    ld (loader_xfer_remaining + 2),hl
    ret

loader_xfer_wire_remaining:
    ld hl,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE)
    ld de,(loader_xfer_position)
    or a
    sbc hl,de
    ld (loader_xfer_remaining),hl
    ld hl,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE + 2)
    ld de,(loader_xfer_position + 2)
    sbc hl,de
    ld (loader_xfer_remaining + 2),hl
    ret

loader_xfer_add_position:
    ld de,(loader_xfer_position)
    add hl,de
    ld (loader_xfer_position),hl
    ld hl,(loader_xfer_position + 2)
    ld de,0
    adc hl,de
    ld (loader_xfer_position + 2),hl
    ret
loader_xfer_add_scan_count:
    ld de,(loader_xfer_scan_count)
    add hl,de
    ld (loader_xfer_scan_count),hl
    ld hl,(loader_xfer_scan_count + 2)
    ld de,0
    adc hl,de
    ld (loader_xfer_scan_count + 2),hl
    ret

; Carry when scan_limit > actual position, or position > immutable wire size.
loader_xfer_limit_not_after_position:
    ld hl,(loader_xfer_position)
    ld de,(loader_xfer_scan_limit)
    or a
    sbc hl,de
    ld hl,(loader_xfer_position + 2)
    ld de,(loader_xfer_scan_limit + 2)
    sbc hl,de
    ret
loader_xfer_position_not_after_wire:
    ld hl,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE)
    ld de,(loader_xfer_position)
    or a
    sbc hl,de
    ld hl,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE + 2)
    ld de,(loader_xfer_position + 2)
    sbc hl,de
    ret

loader_xfer_close_primary:
    ld a,(loader_xfer_handle)
    cp INVALID_HANDLE
    ret z
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    push af
    ld a,INVALID_HANDLE
    ld (loader_xfer_handle),a
    pop af
    ret
loader_xfer_close_metadata:
    ld a,(loader_xfer_meta_handle)
    cp INVALID_HANDLE
    ret z
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    push af
    ld a,INVALID_HANDLE
    ld (loader_xfer_meta_handle),a
    pop af
    ret
loader_xfer_close_output:
    ld a,(loader_xfer_output_handle)
    cp INVALID_HANDLE
    ret z
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    push af
    ld a,INVALID_HANDLE
    ld (loader_xfer_output_handle),a
    pop af
    ret

; Delete only the `.OUT` proven to have been created by this invocation. The
; verified wire `.PRT` and binding `.MTD` deliberately survive every failure.
loader_xfer_delete_owned_output:
    ld a,(loader_xfer_output_owned)
    or a
    ret z
    ld de,loader_xfer_output_path
    ld c,DOS_DELETE
    call 00005h
    xor a
    ld (loader_xfer_output_owned),a
    ret

; Initialize one fixed-width foreground progress line. The transfer size and
; position are 32-bit protocol values, so percentage remains correct above
; 64 KiB and after resume. Dividing the immutable size by 100 once lets every
; later block update advance only the thresholds it crossed.
loader_xfer_progress_begin:
    push af
    push bc
    push de
    push hl
    push ix
    xor a
    ld (loader_xfer_progress_percent),a
    ld (loader_xfer_progress_fraction),a
    ld (loader_xfer_progress_rate),a
    ld (loader_xfer_progress_rate + 1),a
    ld (loader_xfer_progress_pending),a
    ld (loader_xfer_progress_pending + 1),a
    ld hl,(JIFFY)
    ld (loader_xfer_progress_last_jiffy),hl
    ld a,60
    ld (loader_xfer_progress_hz),a
    ld a,(RG9SAV)
    bit 1,a                    ; VDP register 9 bit 1: PAL=50, NTSC=60
    jr z,loader_xfer_progress_frequency_ready
    ld a,50
    ld (loader_xfer_progress_hz),a
loader_xfer_progress_frequency_ready:
    ld hl,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE)
    ld de,(loader_xfer_descriptor + XFER_DESC_WIRE_SIZE + 2)
    ld a,h
    or l
    or d
    or e
    jr nz,loader_xfer_progress_nonempty
    ld a,100
    ld (loader_xfer_progress_percent),a
    jr loader_xfer_progress_begin_render

loader_xfer_progress_nonempty:
    ld hl,loader_xfer_descriptor + XFER_DESC_WIRE_SIZE
    ld de,loader_xfer_progress_step
    ld bc,4
    ldir
    ; In-place unsigned 32/100 long division. The shifted dividend becomes
    ; floor(size/100), while A retains size modulo 100.
    ld b,32
    xor a
loader_xfer_progress_div100_loop:
    ld hl,loader_xfer_progress_step
    sla (hl)
    inc hl
    rl (hl)
    inc hl
    rl (hl)
    inc hl
    rl (hl)
    rla
    cp XFER_PROGRESS_DIVISOR
    jr c,loader_xfer_progress_div100_next
    sub XFER_PROGRESS_DIVISOR
    ld hl,loader_xfer_progress_step
    inc (hl)                   ; shifted quotient bit zero becomes one
loader_xfer_progress_div100_next:
    djnz loader_xfer_progress_div100_loop
    ld (loader_xfer_progress_remainder),a
    ld hl,loader_xfer_progress_step
    ld de,loader_xfer_progress_next
    ld bc,4
    ldir
    ; ceil(size/100) is the exact threshold for the first percentage point.
    ld a,(loader_xfer_progress_remainder)
    add a,99
    cp XFER_PROGRESS_DIVISOR
    jr c,loader_xfer_progress_first_fraction
    sub XFER_PROGRESS_DIVISOR
    push af
    call loader_xfer_progress_increment_next
    pop af
loader_xfer_progress_first_fraction:
    ld (loader_xfer_progress_fraction),a
    call loader_xfer_progress_update_percent
loader_xfer_progress_begin_render:
    call loader_xfer_progress_render
    pop ix
    pop hl
    pop de
    pop bc
    pop af
    ret

; Update a recent-window throughput sample after each confirmed transfer block.
; Accumulate at least one second before replacing the displayed value, so a
; tiny final tail cannot masquerade as the rate of the complete stream. Files
; completing inside the first second simply keep the initial 0 B/s display.
; Overflow saturates at 65535 B/s, above the current UART ceilings.
loader_xfer_progress_after_block:
    push af
    push bc
    push de
    push hl
    push ix
    ld hl,(loader_xfer_progress_pending)
    ld de,(loader_xfer_block_length)
    add hl,de
    jr nc,loader_xfer_progress_pending_ready
    ld hl,0FFFFh
loader_xfer_progress_pending_ready:
    ld (loader_xfer_progress_pending),hl
    ld hl,(JIFFY)
    ld (loader_xfer_progress_current_jiffy),hl
    ld de,(loader_xfer_progress_last_jiffy)
    or a
    sbc hl,de                   ; modulo-16-bit jiffy delta handles wrap
    jr z,loader_xfer_progress_rate_ready
    ld (loader_xfer_progress_tick_delta),hl
    ; Keep both pending bytes and the original sample timestamp until at least
    ; one PAL/NTSC second has elapsed. The bar still renders after every write.
    ld a,h
    or a
    jr nz,loader_xfer_progress_rate_sample
    ld a,(loader_xfer_progress_hz)
    cp l
    jr c,loader_xfer_progress_rate_sample
    jr z,loader_xfer_progress_rate_sample
    jr loader_xfer_progress_rate_ready
loader_xfer_progress_rate_sample:
    ld hl,(loader_xfer_progress_current_jiffy)
    ld (loader_xfer_progress_last_jiffy),hl
    ; Compute bytes * Hz / ticks without overflowing on a 2040-byte fast
    ; block. First divide bytes by ticks, then multiply the quotient and derive
    ; the exact fractional contribution from the remainder.
    ld hl,(loader_xfer_progress_pending)
    ld de,(loader_xfer_progress_tick_delta)
    call loader_xfer_progress_divide_u16
    ld (loader_xfer_progress_rate_remainder),bc
    ex de,hl                   ; DE = whole bytes per tick
    ld hl,0
    ld a,(loader_xfer_progress_hz)
    ld b,a
loader_xfer_progress_rate_multiply:
    add hl,de
    jr c,loader_xfer_progress_rate_saturated
    djnz loader_xfer_progress_rate_multiply
    ld (loader_xfer_progress_rate),hl

    ; floor(remainder * Hz / ticks), evaluated as at most 60 bounded additions.
    ; The running residue stays below tick_delta, avoiding another wide product.
    ld hl,0
    xor a
    ld (loader_xfer_progress_rate_fraction),a
    ld a,(loader_xfer_progress_hz)
loader_xfer_progress_rate_fraction_loop:
    push af
    ld bc,(loader_xfer_progress_rate_remainder)
    add hl,bc
    ld de,(loader_xfer_progress_tick_delta)
    or a
    sbc hl,de
    jr nc,loader_xfer_progress_rate_fraction_increment
    add hl,de                   ; restore a residue smaller than the divisor
    jr loader_xfer_progress_rate_fraction_next
loader_xfer_progress_rate_fraction_increment:
    push hl
    ld hl,loader_xfer_progress_rate_fraction
    inc (hl)
    pop hl
loader_xfer_progress_rate_fraction_next:
    pop af
    dec a
    jr nz,loader_xfer_progress_rate_fraction_loop
    ld a,(loader_xfer_progress_rate_fraction)
    ld e,a
    ld d,0
    ld hl,(loader_xfer_progress_rate)
    add hl,de
    jr c,loader_xfer_progress_rate_saturated
    ld (loader_xfer_progress_rate),hl
    jr loader_xfer_progress_rate_clear_pending
loader_xfer_progress_rate_saturated:
    ld hl,0FFFFh
    ld (loader_xfer_progress_rate),hl
loader_xfer_progress_rate_clear_pending:
    xor a
    ld (loader_xfer_progress_pending),a
    ld (loader_xfer_progress_pending + 1),a
loader_xfer_progress_rate_ready:
    call loader_xfer_progress_update_percent
    call loader_xfer_progress_render
    pop ix
    pop hl
    pop de
    pop bc
    pop af
    ret

; Carry when the completed position is greater than or equal to the next exact
; percentage threshold. Compare most-significant bytes first.
loader_xfer_progress_reached_next:
    ld hl,loader_xfer_position + 3
    ld de,loader_xfer_progress_next + 3
    ld b,4
loader_xfer_progress_compare_loop:
    ld a,(de)
    cp (hl)
    jr c,loader_xfer_progress_compare_yes
    jr nz,loader_xfer_progress_compare_no
    dec hl
    dec de
    djnz loader_xfer_progress_compare_loop
loader_xfer_progress_compare_yes:
    scf
    ret
loader_xfer_progress_compare_no:
    or a
    ret

loader_xfer_progress_update_percent:
    ld a,(loader_xfer_progress_percent)
    cp 100
    ret nc
loader_xfer_progress_percent_loop:
    call loader_xfer_progress_reached_next
    ret nc
    ld hl,loader_xfer_progress_percent
    inc (hl)
    ld a,(hl)
    cp 100
    ret nc
    call loader_xfer_progress_advance_next
    jr loader_xfer_progress_percent_loop

loader_xfer_progress_advance_next:
    ld hl,(loader_xfer_progress_next)
    ld de,(loader_xfer_progress_step)
    add hl,de
    ld (loader_xfer_progress_next),hl
    ld hl,(loader_xfer_progress_next + 2)
    ld de,(loader_xfer_progress_step + 2)
    adc hl,de
    ld (loader_xfer_progress_next + 2),hl
    ld a,(loader_xfer_progress_fraction)
    ld hl,loader_xfer_progress_remainder
    add a,(hl)
    cp XFER_PROGRESS_DIVISOR
    jr c,loader_xfer_progress_store_fraction
    sub XFER_PROGRESS_DIVISOR
    push af
    call loader_xfer_progress_increment_next
    pop af
loader_xfer_progress_store_fraction:
    ld (loader_xfer_progress_fraction),a
    ret

loader_xfer_progress_increment_next:
    ld hl,loader_xfer_progress_next
    inc (hl)
    ret nz
    inc hl
    inc (hl)
    ret nz
    inc hl
    inc (hl)
    ret nz
    inc hl
    inc (hl)
    ret

loader_xfer_progress_render:
    ; floor(percent * 18 / 100) maps the exact percentage to the compact bar.
    ; The generic 16-bit divider keeps this correct for a non-divisor width.
    ld a,(loader_xfer_progress_percent)
    ld l,a
    ld h,0
    add hl,hl                  ; percent * 2
    ld b,h
    ld c,l
    add hl,hl                  ; percent * 4
    add hl,hl                  ; percent * 8
    add hl,hl                  ; percent * 16
    add hl,bc                  ; percent * 18
    ld de,100
    call loader_xfer_progress_divide_u16
    ld c,l
    ld hl,loader_xfer_progress_bar
    ld b,XFER_PROGRESS_BAR_WIDTH
loader_xfer_progress_bar_loop:
    ld a,c
    or a
    jr z,loader_xfer_progress_bar_empty
    ld (hl),'#'
    dec c
    jr loader_xfer_progress_bar_next
loader_xfer_progress_bar_empty:
    ld (hl),'-'
loader_xfer_progress_bar_next:
    inc hl
    djnz loader_xfer_progress_bar_loop

    ld a,(loader_xfer_progress_percent)
    ld l,a
    ld h,0
    ld ix,loader_xfer_progress_percent_digits + 2
    ld a,3
    call loader_xfer_progress_format_u16
    ld hl,loader_xfer_progress_percent_digits
    ld b,2
    call loader_xfer_progress_blank_leading
    ld hl,(loader_xfer_progress_rate)
    ld ix,loader_xfer_progress_rate_digits + 4
    ld a,5
    call loader_xfer_progress_format_u16
    ld hl,loader_xfer_progress_rate_digits
    ld b,4
    call loader_xfer_progress_blank_leading
    ld de,loader_xfer_progress_message
    jp loader_xfer_print

; CLOSE carries the host's monotonic-clock stream average in DE. Render it once
; at the already-complete boundary.
loader_xfer_apply_fast_final_rate:
    ld (loader_xfer_progress_rate),de
    call loader_xfer_progress_update_percent
    jp loader_xfer_progress_render

; Input HL=value, IX=last output digit, A=fixed width.
loader_xfer_progress_format_u16:
    push af
    ld de,10
    call loader_xfer_progress_divide_u16
    ld a,c                     ; remainder 0..9
    add a,'0'
    ld (ix+0),a
    dec ix
    pop af
    dec a
    jr nz,loader_xfer_progress_format_u16
    ret

loader_xfer_progress_blank_leading:
    ld a,(hl)
    cp '0'
    ret nz
    ld (hl),' '
    inc hl
    djnz loader_xfer_progress_blank_leading
    ret

; Unsigned HL/DE. Return quotient HL and remainder BC. The fixed 16-iteration
; implementation avoids a slow repeated-subtraction path at one-jiffy rates.
loader_xfer_progress_divide_u16:
    ld bc,0
    ld a,16
loader_xfer_progress_divide_u16_loop:
    add hl,hl
    rl c
    rl b
    push hl
    ld h,b
    ld l,c
    or a
    sbc hl,de
    jr c,loader_xfer_progress_divide_u16_no_subtract
    ld b,h
    ld c,l
    pop hl
    inc hl
    jr loader_xfer_progress_divide_u16_next
loader_xfer_progress_divide_u16_no_subtract:
    pop hl
loader_xfer_progress_divide_u16_next:
    dec a
    jr nz,loader_xfer_progress_divide_u16_loop
    ret

loader_xfer_mark_progress:
    ld hl,(JIFFY)
    ld (loader_xfer_last_progress),hl
    ret
loader_xfer_wait_timeout:
    ld hl,(JIFFY)
    ld de,(loader_xfer_last_progress)
    or a
    sbc hl,de
    ld de,FILE_TRANSFER_TIMEOUT_TICKS
    or a
    sbc hl,de
    ret

loader_xfer_print:
    ld c,9
    jp 00005h

loader_xfer_success_exit:
    ; COMPLETE is the host's terminal witness. Publish it only after all DOS
    ; cleanup and the final console message, leaving no file or VDP-facing work
    ; between the terminal state and process termination.
    call loader_xfer_print
    call loader_xfer_finish_success
    or a
    jp nz,loader_xfer_closed_error
    ld c,0
    jp 00005h

loader_xfer_timeout_error:
    ld a,XFER_ERROR_TIMEOUT
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INTERNAL
    jr loader_xfer_open_error
loader_xfer_cancelled_error:
    ; CANCELLED is a state, not an error-code slot. Report a binding failure to
    ; the transient process while leaving the resident's CANCELLED state intact.
    ld a,XFER_ERROR_BINDING
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INTERNAL
    jr loader_xfer_open_error
loader_xfer_range_open_error:
    ld a,XFER_ERROR_RANGE
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    jr loader_xfer_open_error
loader_xfer_metadata_open_error:
    ld a,XFER_ERROR_METADATA
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    jr loader_xfer_open_error
loader_xfer_crc_open_error:
    ld a,XFER_ERROR_CRC
    ld (loader_xfer_protocol_error),a
    ld a,ERR_INVALID_PARAMETER
    jr loader_xfer_open_error
loader_xfer_internal_open_error:
    ld a,ERR_INTERNAL

loader_xfer_open_error:
    ld (last_error),a
    call loader_xfer_close_primary
    call loader_xfer_close_metadata
    call loader_xfer_close_output
    call loader_xfer_delete_owned_output
    call loader_xfer_finish_failure
    ld a,(last_error)
    jr loader_xfer_report_error
loader_xfer_closed_error:
    ld (last_error),a
    call loader_xfer_close_primary
    call loader_xfer_close_metadata
    call loader_xfer_close_output
    call loader_xfer_delete_owned_output
    call loader_xfer_finish_failure
    ld a,(last_error)
    jr loader_xfer_report_error
loader_xfer_early_error:
    ld (last_error),a
    ; Claim may have failed before a resident transfer existed. FINISH is best
    ; effort and its rejection must not replace the original DOS error.
    call loader_xfer_finish_failure
    ld a,(last_error)
loader_xfer_report_error:
    ld (last_error),a
    ld de,loader_xfer_error_message
    call loader_xfer_print
    ld a,(dos2_available)
    or a
    jp z,00000h
    ld a,(last_error)
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

loader_xfer_meta_magic:
    db "MXAI2MT2"
loader_xfer_part_extension:
    db ".PRT",0
loader_xfer_meta_extension:
    db ".MTD",0
loader_xfer_output_extension:
    db ".OUT",0
loader_xfer_put_ready_message:
    db 13,10,"MSXAI PUT READY",13,10,"$"
loader_xfer_get_ready_message:
    db 13,10,"MSXAI GET READY",13,10,"$"
loader_xfer_put_ok_message:
    db 13,10,"MSXAI PUT OK",13,10,"$"
loader_xfer_get_ok_message:
    db 13,10,"MSXAI GET OK",13,10,"$"
loader_xfer_error_message:
    db 13,10,"MSXAI TRANSFER ERROR",13,10,"$"
loader_xfer_progress_message:
    db 13,"["
loader_xfer_progress_bar:
    ds XFER_PROGRESS_BAR_WIDTH,'-'
    db "] "
loader_xfer_progress_percent_digits:
    db "  0% "
loader_xfer_progress_rate_digits:
    db "    0 B/s$"

loader_xfer_expected_direction:
    db 0
loader_xfer_protocol_error:
    db XFER_ERROR_IO
loader_xfer_created_meta:
    db 0
loader_xfer_claimed:
    db 0
loader_xfer_handle:
    db INVALID_HANDLE
loader_xfer_meta_handle:
    db INVALID_HANDLE
loader_xfer_output_handle:
    db INVALID_HANDLE
loader_xfer_output_owned:
    db 0
loader_xfer_phase:
    db XFER_PHASE_RECEIVING
loader_xfer_tsr_id:
    dw 0
loader_xfer_target_basename:
    dw 0
loader_xfer_prefix_length:
    dw 0
loader_xfer_extension_pointer:
    dw 0
loader_xfer_publish_source:
    dw 0
loader_xfer_block_length:
    dw 0
loader_xfer_read_length:
    dw 0
loader_xfer_primary_ensured:
    db 0
loader_xfer_accumulated:
    dw 0
loader_xfer_accumulator_offset:
    dw 0
loader_xfer_last_progress:
    dw 0
loader_xfer_progress_last_jiffy:
    dw 0
loader_xfer_progress_current_jiffy:
    dw 0
loader_xfer_progress_tick_delta:
    dw 0
loader_xfer_progress_pending:
    dw 0
loader_xfer_progress_rate:
    dw 0
loader_xfer_progress_rate_remainder:
    dw 0
loader_xfer_progress_rate_fraction:
    db 0
loader_xfer_progress_step:
    ds 4,0
loader_xfer_progress_next:
    ds 4,0
loader_xfer_progress_percent:
    db 0
loader_xfer_progress_fraction:
    db 0
loader_xfer_progress_remainder:
    db 0
loader_xfer_progress_hz:
    db 60
loader_xfer_crc:
    ds 4,0
loader_xfer_saved_crc:
    ds 4,0
loader_xfer_position:
    ds 4,0
loader_xfer_scan_limit:
    ds 4,0
loader_xfer_scan_count:
    ds 4,0
loader_xfer_remaining:
    ds 4,0
loader_xfer_temp_path:
    ds XFER_PATH_MAX + 13,0
loader_xfer_meta_path:
    ds XFER_PATH_MAX + 13,0
loader_xfer_output_path:
    ds XFER_PATH_MAX + 13,0
