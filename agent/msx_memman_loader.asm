; MemMan lifecycle used by the transient half of MSXAI.COM.
;
; The public-domain MemMan utilities and the two transport-patched TSR images
; are separate files in the canonical MSX-AI package.  Keeping them outside
; MSXAI.COM reduces transient TPA pressure and lets DOS load each component
; only while it is needed.
;
; Requirements:
;   - MSX-DOS 2 file-handle calls (BDOS functions 44h and above)
;   - loader_transport_id is set to one of the supported transport IDs
;   - every suite file is available under MSXAI_HOME, or in the current DOS
;     directory when that optional environment item is unset
;
; Installation validates MEMMAN.COM, TL.COM, and the selected fixed-driver TSR,
; stages external MEMMAN.COM at the top of the TPA, and overlays address 0100h.
; Uninstall uses the same mechanism for TK.COM.  No temporary file is created,
; patched, deleted, or left behind by either lifecycle.

DOS_CREATE:              equ 044h
DOS_CLOSE:               equ 045h
DOS_ENSURE:              equ 046h
DOS_OPEN:                equ 043h
DOS_READ:                equ 048h
DOS_WRITE:               equ 049h
DOS_SEEK:                equ 04Ah
DOS_DELETE:              equ 04Dh
DOS_RENAME:              equ 04Eh
DOS_HDELETE:             equ 052h
DOS_TERM_ERROR:          equ 062h
DOS_DEFAB:               equ 063h
DOS_GET_ENV:             equ 06Bh
DOS_SET_ENV:             equ 06Ch
DOS_VERSION:             equ 06Fh

CREATE_WRITE_ONLY:       equ 002h
OPEN_READ_ONLY:          equ 001h
OPEN_READ_WRITE:         equ 000h
CREATE_NEW:              equ 080h

ERR_INTERNAL:            equ 0DFh
ERR_NO_MEMORY:           equ 0DEh
ERR_NO_FILE:             equ 0D7h
ERR_FILE_EXISTS:         equ 0CBh
ERR_INVALID_PARAMETER:   equ 08Bh
ERR_BAD_VERSION:         equ 085h

DRIVER_8251:             equ 0
DRIVER_16C550:           equ 1

INVALID_HANDLE:          equ 0FFh

JIFFY:                   equ 0FC9Eh
RG9SAV:                  equ 0FFE8h
COMMAND_TAIL:            equ 00080h
COMMAND_TEXT:            equ 00081h
TPA_TOP_POINTER:         equ 00006h
COM_ENTRY:               equ 00100h

MEMMAN_ACTION_INSTALL:   equ 0
MEMMAN_ACTION_UNINSTALL: equ 1
MEMMAN_COMMAND_MAX:      equ 40
SUITE_PATH_MAX:          equ 63
SUITE_PATH_BUFFER_SIZE:  equ SUITE_PATH_MAX + 1
DOS_PATH_SEPARATOR:      equ 05Ch

; Exact sizes of the pinned MemMan 2.42 public-domain utilities.  The build
; materializer verifies their SHA-256 values before these files are packaged.
MEMMAN_FILE_SIZE:        equ 01E00h ; 7680 bytes
TL_FILE_SIZE:            equ 00A00h ; 2560 bytes
TK_FILE_SIZE:            equ 00580h ; 1408 bytes

; Leave normal transient-program stack space above the relocation trampoline.
; Once MEMMAN.COM starts, the old loader image and trampoline are disposable.
OVERLAY_STACK_HEADROOM:  equ 00080h
FILE_TRANSFER_TIMEOUT_TICKS: equ 3600 ; one minute NTSC, 72 seconds PAL
XFER_ENSURE_BATCH_BYTES: equ 8192
; Keep the complete progress line at 35 columns. Brazilian MSX machines may
; expose only 37 text columns in DOS, and writing the last column can trigger
; an automatic wrap before the next carriage return.
XFER_PROGRESS_BAR_WIDTH: equ 18
XFER_PROGRESS_DIVISOR:   equ 100
XFER_META_PATH:          equ 43
XFER_META_PHASE:         equ 107
XFER_META_PHASE_INV:     equ 108
XFER_META_SIZE:          equ 109
XFER_PHASE_RECEIVING:    equ 0
XFER_PHASE_DECODING:     equ 1
XFER_PHASE_PUBLISHING:   equ 2
XFER_PHASE_PUBLISHED:    equ 3

if MSXAI_MAIN_BUILD
include 'work/agent/build/MSXAI_TSR.INC'
else

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
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_PUT_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 2
    jp z,loader_xfer_put_finalize
    or a
    jp z,loader_xfer_wait
    ld (loader_xfer_block_length),hl
    ld bc,(loader_xfer_block_length)
    ld hl,loader_xfer_buffer
    call loader_xfer_crc_update
    ld de,loader_xfer_buffer
    ld hl,(loader_xfer_block_length)
    ld a,(loader_xfer_handle)
    ld b,a
    call write_exact
    or a
    jp nz,loader_xfer_open_error
    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_position
    ; add_position leaves HL holding the high position word. Reload the exact
    ; current write length before extending the 16-bit ENSURE batch counter.
    ld hl,(loader_xfer_block_length)
    ld de,(loader_xfer_unflushed)
    add hl,de
    ld (loader_xfer_unflushed),hl
    call loader_xfer_position_equals_wire
    jr z,loader_xfer_put_ensure
    ld hl,(loader_xfer_unflushed)
    ld de,XFER_ENSURE_BATCH_BYTES
    or a
    sbc hl,de
    jr nc,loader_xfer_put_ensure

    ; Release the one-block mailbox after exact WRITE, but do not advertise a
    ; durable offset until a later DOS ENSURE covers the whole batch.
    ld hl,(loader_xfer_block_length)
    ld a,TSR_TALK_XFER_PUT_RELEASE
    call loader_xfer_tsr_call
    or a
    jp nz,loader_xfer_internal_open_error
    call loader_xfer_progress_after_block
    call loader_xfer_mark_progress
    jp loader_xfer_put_loop

loader_xfer_put_ensure:
    ld a,(loader_xfer_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    or a
    jp nz,loader_xfer_open_error

    ; Commit covers every exact write in the ensured batch and carries the
    ; rolling standard CRC-32. Only now may the host journal this boundary.
    ld hl,(loader_xfer_unflushed)
    ld (loader_xfer_buffer),hl
    ld de,loader_xfer_buffer + 2
    call loader_xfer_crc_final_to
    ld hl,loader_xfer_buffer
    ld a,TSR_TALK_XFER_PUT_COMMIT
    call loader_xfer_tsr_call
    or a
    jp nz,loader_xfer_internal_open_error
    call loader_xfer_progress_after_block
    xor a
    ld (loader_xfer_unflushed),a
    ld (loader_xfer_unflushed + 1),a
    call loader_xfer_mark_progress
    jp loader_xfer_put_loop

loader_xfer_put_finalize:
    ld hl,(loader_xfer_unflushed)
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
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    or a
    jp nz,loader_xfer_open_error
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
    call loader_xfer_position_equals_wire
    jp z,loader_xfer_get_wait_close
    call loader_xfer_wire_remaining
    ld hl,(loader_xfer_remaining + 2)
    ld a,h
    or l
    ld hl,XFER_GET_CAPACITY
    jr nz,loader_xfer_get_count_ready
    ld de,(loader_xfer_remaining)
    push hl
    or a
    sbc hl,de
    pop hl
    jr c,loader_xfer_get_count_ready
    jr z,loader_xfer_get_count_ready
    ex de,hl
loader_xfer_get_count_ready:
    ld (loader_xfer_block_length),hl
    ld a,(loader_xfer_handle)
    ld b,a
    ld de,loader_xfer_buffer + 6
    ld c,DOS_READ
    push hl
    call 00005h
    pop de
    or a
    jp nz,loader_xfer_open_error
    or a
    sbc hl,de
    jp nz,loader_xfer_internal_open_error
    ld bc,(loader_xfer_block_length)
    ld hl,loader_xfer_buffer + 6
    call loader_xfer_crc_update
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
    ld hl,0
    ld a,TSR_TALK_XFER_GET_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 1
    jr z,loader_xfer_get_acked
    call loader_xfer_wait_timeout
    jp nc,loader_xfer_timeout_error
    halt
    jr loader_xfer_get_wait_ack
loader_xfer_get_acked:
    ld hl,(loader_xfer_block_length)
    call loader_xfer_add_position
    call loader_xfer_progress_after_block
    call loader_xfer_mark_progress
    jp loader_xfer_get_loop

loader_xfer_get_wait_close:
    ld hl,0
    ld a,TSR_TALK_XFER_GET_POLL
    call loader_xfer_tsr_call
    cp 0FFh
    jp z,loader_xfer_cancelled_error
    cp 2
    jr z,loader_xfer_get_finalize
    call loader_xfer_wait_timeout
    jp nc,loader_xfer_timeout_error
    halt
    jr loader_xfer_get_wait_close
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

; Initialize DOS2 and claim the staged descriptor. Input A=expected direction.
loader_xfer_initialize:
    ld (loader_xfer_expected_direction),a
    xor a
    ld (dos2_available),a
    ld (loader_xfer_created_meta),a
    ld (loader_xfer_claimed),a
    ld (loader_xfer_output_owned),a
    ld (loader_xfer_phase),a
    ld (loader_xfer_unflushed),a
    ld (loader_xfer_unflushed + 1),a
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
    ld a,(loader_xfer_descriptor + XFER_DESC_ENCODING)
    cp XFER_ENCODING_RAW
    jr z,loader_xfer_initialize_encoding_ok
    cp XFER_ENCODING_PACKBITS
    jr nz,loader_xfer_initialize_unsupported
    ld a,(loader_xfer_expected_direction)
    cp XFER_DIRECTION_PUT
    jr nz,loader_xfer_initialize_unsupported
loader_xfer_initialize_encoding_ok:
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
    ld (loader_xfer_crc),a
    inc hl
    push bc
    ld b,8
loader_xfer_crc_bit:
    ld a,(loader_xfer_crc)
    and 1
    ld c,a
    ld a,(loader_xfer_crc + 3)
    srl a
    ld (loader_xfer_crc + 3),a
    ld a,(loader_xfer_crc + 2)
    rr a
    ld (loader_xfer_crc + 2),a
    ld a,(loader_xfer_crc + 1)
    rr a
    ld (loader_xfer_crc + 1),a
    ld a,(loader_xfer_crc)
    rr a
    ld (loader_xfer_crc),a
    ld a,c
    or a
    jr z,loader_xfer_crc_no_poly
    ld a,(loader_xfer_crc)
    xor 020h
    ld (loader_xfer_crc),a
    ld a,(loader_xfer_crc + 1)
    xor 083h
    ld (loader_xfer_crc + 1),a
    ld a,(loader_xfer_crc + 2)
    xor 0B8h
    ld (loader_xfer_crc + 2),a
    ld a,(loader_xfer_crc + 3)
    xor 0EDh
    ld (loader_xfer_crc + 3),a
loader_xfer_crc_no_poly:
    djnz loader_xfer_crc_bit
    pop bc
    dec bc
    ld a,b
    or c
    jr nz,loader_xfer_crc_byte
    ret

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

; Update instantaneous throughput after each confirmed transfer block. Bytes
; completed within the same jiffy are accumulated, avoiding a false divide by
; zero on fast transports. Overflow saturates at 65535 B/s, above the current
; 8251 and 16C550 byte-stream ceilings.
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
    ld hl,(loader_xfer_progress_current_jiffy)
    ld (loader_xfer_progress_last_jiffy),hl
    ld de,(loader_xfer_progress_pending)
    ld hl,0
    ld a,(loader_xfer_progress_hz)
    ld b,a
loader_xfer_progress_rate_multiply:
    add hl,de
    jr c,loader_xfer_progress_rate_saturated
    djnz loader_xfer_progress_rate_multiply
    ld de,(loader_xfer_progress_tick_delta)
    call loader_xfer_progress_divide_u16
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
loader_xfer_unflushed:
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

endif

if MSXAI_MAIN_BUILD
; Both entry points consume the current process.  On success MEMMAN.COM takes
; over address 0100h and warm-boots through the supplied command chain; on an
; error loader_abort terminates with a DOS2 error code.
memman_loader_install:
    xor a
    ld (memman_loader_action),a
    jr memman_loader_entry

memman_loader_uninstall:
    ld a,MEMMAN_ACTION_UNINSTALL
    ld (memman_loader_action),a

memman_loader_entry:
    xor a
    ld (dos2_available),a
    ld a,INVALID_HANDLE
    ld (suite_handle),a

    call loader_preflight
    or a
    jp nz,loader_abort

    ; Read and close the external executable before overwriting this process.
    ; All DOS errors therefore remain recoverable until the final LDIR handoff.
    call suite_stage_overlay
    or a
    jp nz,loader_abort

    call build_memman_command_tail

    ; POINT OF NO RETURN.
    ;
    ; MEMMAN.COM or TK.COM takes over address 0100h.  The selected image is
    ; already verified, resident in the high staging area, and has no open
    ; handle.  Installation's MemMan command line invokes external TL.COM and
    ; one fixed-driver TSR; uninstall's tail is consumed directly by TK.COM.
    jp handoff_to_external_overlay

; ---------------------------------------------------------------------------
; Preflight.  No disk state is changed until every static and memory guard has
; passed.  Returns A=0 on success or a DOS-compatible error code otherwise.

loader_preflight:
    ; DOSVER is explicitly backward-compatible: B<2 means that the following
    ; handle calls are unavailable.  Record this before any failure path so an
    ; MSX-DOS 1 host is terminated with JP 0000h instead of DOS2 function 62h.
    ld c,DOS_VERSION
    call 00005h
    or a
    jp nz,preflight_bad_version
    ld a,b
    cp 2
    jp c,preflight_bad_version
    ld a,1
    ld (dos2_available),a

    ; Resolve every external component before selecting an action.  An empty
    ; or undefined MSXAI_HOME deliberately preserves the historical
    ; current-directory behavior.
    call suite_resolve_paths
    or a
    ret nz

    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jp z,preflight_select_uninstall

    ld a,(loader_transport_id)
    cp DRIVER_8251
    jr z,preflight_select_8251
    cp DRIVER_16C550
    jr z,preflight_select_16c550
    ld a,ERR_INVALID_PARAMETER
    ret

preflight_select_8251:
    ld de,suite_mcp8251_tsr_path
    xor a
    jr preflight_install_selected

preflight_select_16c550:
    ld de,suite_mcp16550_tsr_path
    ld a,DRIVER_16C550

preflight_install_selected:
    ld (suite_expected_transport),a
    ld (suite_selected_tsr_path),de
    call suite_build_install_command
    or a
    ret nz
    ld hl,install_command_buffer
    ld (suite_command_source),hl
    ld (suite_command_length),bc
    ld de,suite_memman_path
    ld (suite_overlay_path),de
    ld hl,MEMMAN_FILE_SIZE
    ld (suite_overlay_size),hl

    ld de,suite_memman_path
    ld hl,MEMMAN_FILE_SIZE
    call suite_validate_regular_file
    or a
    ret nz
    ld de,suite_tl_path
    ld hl,TL_FILE_SIZE
    call suite_validate_regular_file
    or a
    ret nz
    call suite_validate_selected_tsr
    or a
    ret nz
    jr preflight_command_length

preflight_select_uninstall:
    ld hl,uninstall_command
    ld (suite_command_source),hl
    ld bc,uninstall_command_length
    ld (suite_command_length),bc
    ld de,suite_tk_path
    ld (suite_overlay_path),de
    ld hl,TK_FILE_SIZE
    ld (suite_overlay_size),hl
    call suite_validate_regular_file
    or a
    ret nz

    ; MSX-DOS accepts 127 characters, but the MemMan install path preserves
    ; only 40 across its warm boot. Keep both supported tails inside that limit.
preflight_command_length:
    ld hl,(suite_command_length)
    ld a,h
    or a
    jp nz,preflight_bad_image
    ld a,l
    or a
    jp z,preflight_bad_image
    cp MEMMAN_COMMAND_MAX + 1
    jp nc,preflight_bad_image

    ; Pick a trampoline address below both the original SP and the documented
    ; TPA top.  This also prevents an unusual entry stack from being clobbered.
    ld hl,(TPA_TOP_POINTER)
    ld de,(loader_entry_sp)
    or a
    sbc hl,de
    jr c,preflight_limit_is_tpa
    ld hl,(loader_entry_sp)
    jr preflight_have_limit
preflight_limit_is_tpa:
    ld hl,(TPA_TOP_POINTER)
preflight_have_limit:
    ld de,OVERLAY_STACK_HEADROOM + overlay_stub_size
    or a
    sbc hl,de
    jr c,preflight_no_memory
    ld (overlay_target),hl

    ; Stage the external executable immediately below the trampoline.  It may
    ; replace the unused monitor/resident source bytes on the install path, but
    ; it must remain above every loader instruction and datum still in use.
    ld de,(suite_overlay_size)
    or a
    sbc hl,de
    jr c,preflight_no_memory
    ld (overlay_source),hl
    ld de,suite_loader_live_end
    or a
    sbc hl,de
    jr c,preflight_no_memory

    ; LDIR copies upward.  Requiring a disjoint source keeps the overlay safe
    ; even if a future pinned utility grows substantially.
    ld hl,(suite_overlay_size)
    ld de,COM_ENTRY
    add hl,de
    ex de,hl
    ld hl,(overlay_source)
    or a
    sbc hl,de
    jr c,preflight_no_memory

    xor a
    ret

preflight_bad_image:
    ld a,ERR_INTERNAL
    ret
preflight_bad_version:
    ld a,ERR_BAD_VERSION
    ret
preflight_no_memory:
    ld a,ERR_NO_MEMORY
    ret

; ---------------------------------------------------------------------------
; Suite path resolution. MSX-DOS 2 function 6Bh returns a null string when an
; environment item is absent. Every destination is a bounded ASCIIZ path, and
; a trailing slash in MSXAI_HOME is accepted without duplication.

suite_resolve_paths:
    ld hl,suite_home_env_name
    ld de,suite_home_buffer
    ld b,SUITE_PATH_BUFFER_SIZE
    ld c,DOS_GET_ENV
    call 00005h
    or a
    ret nz

    ld bc,memman_name
    ld de,suite_memman_path
    call suite_build_path
    or a
    ret nz
    ld bc,tl_name
    ld de,suite_tl_path
    call suite_build_path
    or a
    ret nz
    ld bc,tk_name
    ld de,suite_tk_path
    call suite_build_path
    or a
    ret nz
    ld bc,mcp8251_tsr_name
    ld de,suite_mcp8251_tsr_path
    call suite_build_path
    or a
    ret nz
    ld bc,mcp16550_tsr_name
    ld de,suite_mcp16550_tsr_path
    call suite_build_path
    ret

; Input BC=canonical filename and DE=destination buffer. Return A=0 on success.
suite_build_path:
    ld (suite_path_name_source),bc
    ld hl,suite_home_buffer
    ld b,SUITE_PATH_MAX
    ld c,0                     ; last copied home character
suite_build_path_home_loop:
    ld a,(hl)
    or a
    jr z,suite_build_path_home_done
    ld c,a
    ld a,b
    or a
    jr z,suite_build_path_too_long
    ld a,c
    ld (de),a
    inc de
    inc hl
    dec b
    jr suite_build_path_home_loop
suite_build_path_home_done:
    ld a,(suite_home_buffer)
    or a
    jr z,suite_build_path_name
    ld a,c
    cp DOS_PATH_SEPARATOR
    jr z,suite_build_path_name
    cp '/'
    jr z,suite_build_path_name
    ld a,b
    or a
    jr z,suite_build_path_too_long
    ld a,DOS_PATH_SEPARATOR
    ld (de),a
    inc de
    dec b
suite_build_path_name:
    ld hl,(suite_path_name_source)
suite_build_path_name_loop:
    ld a,(hl)
    or a
    jr z,suite_build_path_complete
    ld c,a
    ld a,b
    or a
    jr z,suite_build_path_too_long
    ld a,c
    ld (de),a
    inc de
    inc hl
    dec b
    jr suite_build_path_name_loop
suite_build_path_complete:
    xor a
    ld (de),a
    ret
suite_build_path_too_long:
    ld a,ERR_INVALID_PARAMETER
    ret

; Build MemMan's post-warm-boot command. COMMAND2 finds TL through PATH; TL is
; given the fully resolved TSR stem so it never depends on the current
; directory. The canonical .TSR suffix is removed from the selected path.
suite_build_install_command:
    ld hl,install_command_prefix
    ld de,install_command_buffer
    ld bc,install_command_prefix_length
    ldir
    ld hl,(suite_selected_tsr_path)
suite_build_install_command_copy:
    ld a,(hl)
    or a
    jr z,suite_build_install_command_suffix
    ld (de),a
    inc de
    inc hl
    jr suite_build_install_command_copy
suite_build_install_command_suffix:
    dec de                       ; strip .TSR
    dec de
    dec de
    dec de
    ld a,'@'
    ld (de),a
    inc de
    ld hl,install_command_buffer
    ex de,hl                     ; HL=end, DE=start
    or a
    sbc hl,de
    ld a,h
    or a
    jr nz,suite_build_install_command_too_long
    ld a,l
    or a
    jr z,suite_build_install_command_too_long
    cp MEMMAN_COMMAND_MAX + 1
    jr nc,suite_build_install_command_too_long
    ld c,l
    ld b,0
    xor a
    ret
suite_build_install_command_too_long:
    ld a,ERR_INVALID_PARAMETER
    ret

; ---------------------------------------------------------------------------
; External suite validation.  Every component is opened read-only, checked
; against its pinned size, and closed before the lifecycle can hand off.

; Input DE=ASCIIZ path, HL=expected size. Return A=0 only for an exact file.
; On success the handle remains open so the TSR validator can inspect its
; patched transport byte before closing it.
suite_open_exact_file:
    ld (suite_expected_size),hl
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call 00005h
    or a
    ret nz
    ld a,b
    ld (suite_handle),a

    ld hl,0
    ld de,0
    ld a,2
    ld c,DOS_SEEK
    call 00005h
    or a
    jr nz,suite_close_preserving_error
    ld a,d
    or e
    jp nz,suite_exact_size_error
    ld bc,(suite_expected_size)
    or a
    sbc hl,bc
    jp nz,suite_exact_size_error
    xor a
    ret

suite_exact_size_error:
    ld a,ERR_INTERNAL
    jr suite_close_preserving_error

suite_validate_regular_file:
    call suite_open_exact_file
    ret nz
    xor a
    jp suite_close_preserving_error

suite_validate_selected_tsr:
    ld de,(suite_selected_tsr_path)
    ld hl,MSXAI_TSR_SIZE
    call suite_open_exact_file
    ret nz

    ld hl,MSXAI_TSR_TRANSPORT_OFFSET
    ld de,0
    ld a,(suite_handle)
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    or a
    jr nz,suite_close_preserving_error
    ld a,d
    or e
    jr nz,suite_exact_size_error
    ld de,suite_probe_byte
    ld hl,1
    ld a,(suite_handle)
    ld b,a
    ld c,DOS_READ
    call 00005h
    or a
    jr nz,suite_close_preserving_error
    ld a,h
    or a
    jr nz,suite_exact_size_error
    ld a,l
    cp 1
    jr nz,suite_exact_size_error
    ld a,(suite_probe_byte)
    ld b,a
    ld a,(suite_expected_transport)
    cp b
    jr nz,suite_exact_size_error
    xor a
    jp suite_close_preserving_error

; Preserve the operation error unless the operation succeeded and CLOSE did
; not.  No lifecycle failure may leak an open suite-file handle.
suite_close_preserving_error:
    push af
    ld a,(suite_handle)
    cp INVALID_HANDLE
    jr z,suite_close_no_handle
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    ld (suite_close_error),a
    ld a,INVALID_HANDLE
    ld (suite_handle),a
    pop af
    or a
    ret nz
    ld a,(suite_close_error)
    ret
suite_close_no_handle:
    pop af
    ret

; Read the preflight-selected executable into its disjoint high-TPA stage and
; prove EOF before closing it.  No BDOS operation occurs after this returns.
suite_stage_overlay:
    ld de,(suite_overlay_path)
    ld hl,(suite_overlay_size)
    call suite_open_exact_file
    ret nz

    ; suite_open_exact_file leaves the verified handle positioned at EOF.
    ; Rewind that same handle before the single exact-size read.  MSX-DOS 2
    ; reports an attempted read beyond EOF as an error, so there must not be a
    ; one-byte EOF probe after the already size-verified payload.
    ld hl,0
    ld de,0
    ld a,(suite_handle)
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    or a
    jr nz,suite_close_preserving_error
    ld a,d
    or e
    jp nz,suite_exact_size_error
    ld a,h
    or l
    jp nz,suite_exact_size_error

    ld de,(overlay_source)
    ld hl,(suite_overlay_size)
    push hl
    ld a,(suite_handle)
    ld b,a
    ld c,DOS_READ
    call 00005h
    pop de
    or a
    jr nz,suite_close_preserving_error
    or a
    sbc hl,de
    jp nz,suite_exact_size_error
    xor a
    jp suite_close_preserving_error

loader_abort:
    ld (last_error),a
    call suite_close_preserving_error

    ld de,loader_error_message
    ld c,9
    call 00005h

loader_abort_terminate:
    ld a,(dos2_available)
    or a
    jp z,00000h
    ld a,(last_error)
    or a
    jr nz,loader_abort_have_code
    ld a,ERR_INTERNAL
loader_abort_have_code:
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

; ---------------------------------------------------------------------------
; MemMan handoff.  The selected tail is copied to the standard DOS location.
; A tiny position-independent stub sits above the staged external executable
; and performs the only write over this process.

build_memman_command_tail:
    ld hl,(suite_command_source)
    ld bc,(suite_command_length)
    ld de,COMMAND_TEXT
    push bc
    ldir
    pop bc
    ld a,c
    ld (COMMAND_TAIL),a
    xor a
    ld (de),a
    ret

handoff_to_external_overlay:
    ld hl,overlay_stub
    ld de,(overlay_target)
    ld bc,overlay_stub_size
    ldir

    ; Reset the stack to its COM-entry value, then RET directly into the copied
    ; stub.  RET consumes the temporary address and restores the original SP
    ; before the external utility receives control at 0100h.
    ld hl,(loader_entry_sp)
    ld sp,hl
    ld hl,(overlay_target)
    push hl
    ld hl,(overlay_source)
    ld de,COM_ENTRY
    ld bc,(suite_overlay_size)
    ret

overlay_stub:
    ldir
    jp COM_ENTRY
overlay_stub_end:
overlay_stub_size: equ overlay_stub_end - overlay_stub

; ---------------------------------------------------------------------------
; MemMan discovery and TSR interaction from the transient foreground process.
; IniChk must be the first MemMan call and is issued at most once per MSXAI.COM
; invocation.  GetTsrID returns BC as the opaque handle used by TsrCall.

memman_find_agent:
    xor a
    ld (memman_present),a
    ld (memman_compatible),a
    ld d,'M'
    ld e,30                    ; IniChk
    call EXTBIO
    cp 'M'
    jr nz,memman_agent_absent
    ld a,1
    ld (memman_present),a
    ld a,d
    cp 2
    jr c,memman_agent_incompatible
    jr nz,memman_agent_compatible
    ld a,e
    cp 4
    jr c,memman_agent_incompatible
memman_agent_compatible:
    ld a,1
    ld (memman_compatible),a
    ld hl,memman_tsr_name
    ld d,'M'
    ld e,62                    ; GetTsrID
    call EXTBIO
    ret                        ; carry clear and BC=id when installed
memman_agent_absent:
    scf
    ret
memman_agent_incompatible:
    scf
    ret

; Input BC is the ID returned by GetTsrID.  The TSR's talk entry accepts A=A5h
; and H=the desired byte-stream transport, then returns the active transport.
memman_reconfigure_agent:
    ld a,(loader_transport_id)
    ld h,a
    ld a,0A5h
    ld d,'M'
    ld e,63                    ; TsrCall
    jp EXTBIO

memman_tsr_name:
    db "MSXAI MCP1  "           ; exactly 12 bytes, padded for GetTsrID

; ---------------------------------------------------------------------------
; Mutable state and command/name templates.

loader_entry_sp:
    dw 0
overlay_target:
    dw 0
overlay_source:
    dw 0
suite_overlay_path:
    dw 0
suite_overlay_size:
    dw 0
suite_selected_tsr_path:
    dw 0
suite_command_source:
    dw 0
suite_command_length:
    dw 0
suite_expected_size:
    dw 0
suite_handle:
    db INVALID_HANDLE
suite_close_error:
    db 0
suite_expected_transport:
    db DRIVER_8251
suite_probe_byte:
    db 0
last_error:
    db 0
dos2_available:
    db 0
memman_loader_action:
    db MEMMAN_ACTION_INSTALL
memman_present:
    db 0
memman_compatible:
    db 0

suite_path_name_source:
    dw 0
suite_home_env_name:
    db "MSXAI_HOME",0
memman_name:
    db "MEMMAN.COM",0
tl_name:
    db "TL.COM",0
tk_name:
    db "TK.COM",0
mcp8251_tsr_name:
    db "MCP8251.TSR",0
mcp16550_tsr_name:
    db "MCP16550.TSR",0

suite_home_buffer:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_memman_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_tl_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_tk_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcp8251_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcp16550_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0

; '@' is MemMan's documented representation of Return.  MemMan 2.42 consumes
; the first Return while warm-booting COMMAND2, so the second '@' is required
; before the visible TL command. TL accepts a TSR path without extension.
install_command_prefix:
    db " _SYSTEM@@TL "
install_command_prefix_end:
install_command_prefix_length: equ install_command_prefix_end - install_command_prefix
install_command_buffer:
    ds install_command_prefix_length + SUITE_PATH_MAX + 1,0

uninstall_command:
    db " ",34,"MSXAI MCP1",34
uninstall_command_end:
uninstall_command_length: equ uninstall_command_end - uninstall_command

loader_error_message:
    db 13,10,"MSXAI resident loader failed; verify the suite files.",13,10,"$"

; Nothing at or above this label is needed by the external-overlay path.  The
; preflight may safely use later monitor/resident source bytes as staging RAM.
suite_loader_live_end:
endif
