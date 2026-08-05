; MemMan lifecycle used by the transient half of the universal MSXAI.COM.
;
; This file is included by msx_agent_core.asm only in the normal COM build.
; The final executable embeds the public-domain MemMan utilities and the
; generated MSXAI TSR.  Nothing remains on disk after the command chain
; succeeds, and CREATE_NEW prevents an interrupted run from overwriting a
; user's file.
;
; Requirements:
;   - MSX-DOS 2 file-handle calls (BDOS functions 44h and above)
;   - loader_transport_id is set to one of the supported transport IDs
;   - generated metadata identifies the transport byte in the TSR blob
;
; Installation creates two collision-resistant files with CREATE_NEW, writes
; the embedded TL and TSR images, builds MemMan's command tail at 0080h, and
; overlays itself with MEMMAN.COM. Uninstall overlays the embedded TK.COM
; directly and never creates a helper file. Pre-overlay failures delete only
; files which this invocation successfully created.

DOS_CREATE:              equ 044h
DOS_CLOSE:               equ 045h
DOS_WRITE:               equ 049h
DOS_DELETE:              equ 04Dh
DOS_HDELETE:             equ 052h
DOS_TERM_ERROR:          equ 062h
DOS_VERSION:             equ 06Fh

CREATE_WRITE_ONLY:       equ 002h
CREATE_NEW:              equ 080h

ERR_INTERNAL:            equ 0DFh
ERR_NO_MEMORY:           equ 0DEh
ERR_NO_FILE:             equ 0D7h
ERR_SYSTEM_EXISTS:       equ 0CDh
ERR_DIRECTORY_EXISTS:    equ 0CCh
ERR_FILE_EXISTS:         equ 0CBh
ERR_INVALID_PARAMETER:   equ 08Bh
ERR_BAD_VERSION:         equ 085h

DRIVER_8251:             equ 0
DRIVER_16C550:           equ 1

CREATED_TL:              equ 001h
CREATED_TSR:             equ 002h
INVALID_HANDLE:          equ 0FFh

JIFFY:                   equ 0FC9Eh
COMMAND_TAIL:            equ 00080h
COMMAND_TEXT:            equ 00081h
TPA_TOP_POINTER:         equ 00006h
COM_ENTRY:               equ 00100h

MEMMAN_ACTION_INSTALL:   equ 0
MEMMAN_ACTION_UNINSTALL: equ 1
MEMMAN_COMMAND_MAX:      equ 40

; Leave normal transient-program stack space above the relocation trampoline.
; Once MEMMAN.COM starts, the old loader image and trampoline are disposable.
OVERLAY_STACK_HEADROOM:  equ 00080h
FILE_UPLOAD_TIMEOUT_TICKS: equ 600 ; ten seconds at NTSC, twelve at PAL

include 'work/agent/MSXAI_TSR.INC'

; ---------------------------------------------------------------------------
; Foreground file sink. The resident receives one CRC-protected framed chunk
; into a mailbox; TsrCall copies it into this process's page-0 buffer, and only
; then does the foreground process call BDOS. No target RAM is overwritten
; before MSXAI.COM owns the TPA. CREATE_NEW protects existing user files, and a
; whole-file CRC detects corruption across mailbox copying and disk writes.

loader_put_file:
    xor a
    ld (dos2_available),a
    ld (loader_put_upload_started),a
    ld a,INVALID_HANDLE
    ld (loader_put_handle),a

    ld c,DOS_VERSION
    call 00005h
    or a
    jp nz,loader_put_bad_version
    ld a,b
    cp 2
    jp c,loader_put_bad_version
    ld a,1
    ld (dos2_available),a

    call memman_find_agent
    jp c,loader_put_internal_error
    ld (loader_put_tsr_id),bc

    ld de,loader_put_filename
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jp nz,loader_put_error
    ld a,b
    ld (loader_put_handle),a

    ld hl,(loader_put_length)
    ld a,TSR_TALK_UPLOAD_BEGIN
    call loader_put_tsr_call
    or a
    jp nz,loader_put_internal_open_error
    ld a,1
    ld (loader_put_upload_started),a
    xor a
    ld (loader_put_written),a
    ld (loader_put_written + 1),a
    ld hl,0FFFFh
    ld (loader_put_crc),hl
    ld hl,(JIFFY)
    ld (loader_put_last_progress),hl

    ld de,loader_put_ready_message
    ld c,9
    call 00005h

loader_put_receive_loop:
    ld hl,loader_put_buffer
    ld a,TSR_TALK_UPLOAD_POLL
    call loader_put_tsr_call
    cp 0FFh
    jp z,loader_put_internal_open_error
    or a
    jr z,loader_put_wait_for_chunk
    ld a,h
    or l
    jp z,loader_put_internal_open_error
    ld (loader_put_chunk_length),hl

    ; The resident already enforces the negotiated total. Check it again on
    ; the DOS side so a malformed or incompatible talk implementation fails
    ; closed before touching the file.
    ld de,(loader_put_written)
    add hl,de
    push hl
    ld de,(loader_put_length)
    or a
    sbc hl,de
    pop hl
    jr c,loader_put_chunk_size_ok
    jr z,loader_put_chunk_size_ok
    jr loader_put_internal_open_error
loader_put_chunk_size_ok:
    ld (loader_put_next_written),hl

    ld bc,(loader_put_chunk_length)
    ld hl,loader_put_buffer
    call loader_put_crc_update

    ld de,loader_put_buffer
    ld hl,(loader_put_chunk_length)
    ld a,(loader_put_handle)
    ld b,a
    call write_exact
    or a
    jr nz,loader_put_open_error
    ld hl,(loader_put_next_written)
    ld (loader_put_written),hl
    ld de,(loader_put_length)
    or a
    sbc hl,de
    jr z,loader_put_receive_complete
    ld hl,(JIFFY)
    ld (loader_put_last_progress),hl
    jr loader_put_receive_loop

loader_put_wait_for_chunk:
    ld hl,(JIFFY)
    ld de,(loader_put_last_progress)
    or a
    sbc hl,de
    ld de,FILE_UPLOAD_TIMEOUT_TICKS
    or a
    sbc hl,de
    jr nc,loader_put_internal_open_error
    halt
    jr loader_put_receive_loop

loader_put_receive_complete:
    ld hl,(loader_put_crc)
    ld de,(loader_put_crc_expected)
    or a
    sbc hl,de
    jr nz,loader_put_internal_open_error

    ld a,(loader_put_handle)
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    or a
    jr nz,loader_put_open_error
    ld a,INVALID_HANDLE
    ld (loader_put_handle),a
    call loader_put_commit_upload
    or a
    jr nz,loader_put_closed_error
    ld de,loader_put_ok_message
    ld c,9
    call 00005h
    ld c,0
    jp 00005h

loader_put_internal_open_error:
    ld a,ERR_INTERNAL
    jr loader_put_open_error
loader_put_internal_error:
    ld a,ERR_INTERNAL
    jr loader_put_error

loader_put_open_error:
    ld (last_error),a
    call loader_put_abort_upload
    ld a,(loader_put_handle)
    cp INVALID_HANDLE
    jr z,loader_put_saved_error
    ld b,a
    ld c,DOS_HDELETE
    call 00005h
    ld a,INVALID_HANDLE
    ld (loader_put_handle),a
loader_put_saved_error:
    ld a,(last_error)
    jr loader_put_error

; A failed terminal acknowledgement occurs after the DOS handle was closed.
; Remove that closed file by pathname before reporting failure to both sides.
loader_put_closed_error:
    ld a,ERR_INTERNAL
    ld (last_error),a
    ld de,loader_put_filename
    ld c,DOS_DELETE
    call 00005h
    ld a,(last_error)
    jr loader_put_error

loader_put_bad_version:
    ld a,ERR_BAD_VERSION
loader_put_error:
    ld (last_error),a
    ld de,loader_put_error_message
    ld c,9
    call 00005h
    ld a,(dos2_available)
    or a
    jp z,00000h
    ld a,(last_error)
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

; Invoke the resident's upload mailbox through the standardized MemMan talk
; entry. A selects begin/poll/end, HL is the action-specific argument.
loader_put_tsr_call:
    ld bc,(loader_put_tsr_id)
    ld d,'M'
    ld e,63                    ; TsrCall
    call EXTBIO
    ei                          ; MemMan returns from TsrCall with DI
    ret

loader_put_commit_upload:
    ld a,(loader_put_upload_started)
    or a
    jr z,loader_put_commit_missing
    xor a
    ld (loader_put_upload_started),a
    ld hl,1                    ; non-zero acknowledges verified success
    ld a,TSR_TALK_UPLOAD_END
    jp loader_put_tsr_call
loader_put_commit_missing:
    ld a,0FFh
    ret

loader_put_abort_upload:
    ld a,(loader_put_upload_started)
    or a
    ret z
    xor a
    ld (loader_put_upload_started),a
    ld hl,0                    ; zero publishes a terminal failure
    ld a,TSR_TALK_UPLOAD_END
    jp loader_put_tsr_call

; Incremental CRC-16/CCITT-FALSE. Input HL=buffer, BC=count. The accumulated
; value starts at FFFFh and is compared with the command-line CRC at EOF.
loader_put_crc_update:
    ld de,(loader_put_crc)
loader_put_crc_byte_loop:
    ld a,b
    or c
    jr z,loader_put_crc_done
    ld a,(hl)
    inc hl
    xor d
    ld d,a
    push bc
    ld b,8
loader_put_crc_bit_loop:
    sla e
    rl d
    jr nc,loader_put_crc_no_poly
    ld a,e
    xor 021h
    ld e,a
    ld a,d
    xor 010h
    ld d,a
loader_put_crc_no_poly:
    djnz loader_put_crc_bit_loop
    pop bc
    dec bc
    jr loader_put_crc_byte_loop
loader_put_crc_done:
    ld (loader_put_crc),de
    ret

loader_put_ok_message:
    db 13,10,"MSXAI PUT OK",13,10,"$"
loader_put_ready_message:
    db 13,10,"MSXAI PUT READY",13,10,"$"
loader_put_error_message:
    db 13,10,"MSXAI PUT ERROR",13,10,"$"

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
    ld (created_mask),a
    ld (cleanup_failed),a
    ld (cleanup_error),a
    ld (dos2_available),a
    ld a,INVALID_HANDLE
    ld (tl_handle),a
    ld (tsr_handle),a

    call loader_preflight
    or a
    jp nz,loader_abort

    ; TsrKill is already embedded in the universal executable. On uninstall,
    ; overlay it directly at the COM entry point instead of creating a helper
    ; file while the target TSR is still active.
    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jr z,memman_loader_uninstall_direct

    call reserve_temporary_pair
    or a
    jp nz,loader_abort

    call write_temporary_files
    or a
    jp nz,loader_abort

    call close_temporary_files
    or a
    jp nz,loader_abort

    call build_memman_command_tail

    ; POINT OF NO RETURN.
    ;
    ; MEMMAN.COM does not return to this loader.  Its documented behaviour is
    ; to skip the supplied command line if MemMan itself reports an error.
    ; Consequently, a failure after this jump can leave M?.COM/A?.TSR on
    ; disk.  CREATE_NEW prevents data loss, and the distinctive names make the
    ; remnants recognizable, but guaranteed post-handoff cleanup requires a
    ; future MemMan wrapper with a verified return/error channel.
    jp handoff_to_memman

memman_loader_uninstall_direct:
    call build_memman_command_tail
    jp handoff_to_tk

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

    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jr z,preflight_uninstall_images

    ld a,(loader_transport_id)
    cp DRIVER_8251
    jr z,preflight_driver_ok
    cp DRIVER_16C550
    jr z,preflight_driver_ok
    ld a,ERR_INVALID_PARAMETER
    ret

preflight_driver_ok:
    ld hl,tl_blob_size
    ld a,h
    or l
    jp z,preflight_bad_image
    ld hl,tsr_blob_size
    ld a,h
    or l
    jp z,preflight_bad_image
    ld de,MSXAI_TSR_SIZE
    or a
    sbc hl,de
    jp nz,preflight_bad_image
    ld hl,tsr_blob_start + MSXAI_TSR_TRANSPORT_OFFSET
    ld a,(hl)
    cp 0FEh
    jp nz,preflight_bad_image
    ld hl,memman_blob_size
    ld a,h
    or l
    jp z,preflight_bad_image

    ; The selected driver byte must be inside the TSR blob.
    ld hl,tsr_blob_size
    ld de,MSXAI_TSR_TRANSPORT_OFFSET + 1
    or a
    sbc hl,de
    jp c,preflight_bad_image

    ld hl,install_command_length
    jr preflight_command_length

preflight_uninstall_images:
    ld hl,tk_blob_size
    ld a,h
    or l
    jp z,preflight_bad_image
    ld hl,uninstall_command_length

    ; MSX-DOS accepts 127 characters, but the MemMan install path preserves
    ; only 40 across its warm boot. Keep both supported tails inside that limit.
preflight_command_length:
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

    ; Copying the trampoline must not overwrite the selected embedded image.
    ld de,memman_blob_end
    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jr nz,preflight_source_end_ready
    ld de,tk_blob_end
preflight_source_end_ready:
    or a
    sbc hl,de
    jr c,preflight_no_memory

    ; The relocated image must end below the executing stub.
    ld hl,(overlay_target)
    ld de,COM_ENTRY + memman_blob_size
    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jr nz,preflight_destination_end_ready
    ld de,COM_ENTRY + tk_blob_size
preflight_destination_end_ready:
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
; Temporary names.  Both files use the same suffix.  CREATE_NEW guarantees
; that no pre-existing user file is truncated.  FILEX, DIRX and SYSX are name
; collisions; all other create errors are returned immediately.

reserve_temporary_pair:
    ld a,(JIFFY)
reserve_normalize_suffix:
    cp 36
    jr c,reserve_suffix_ready
    sub 36
    jr reserve_normalize_suffix
reserve_suffix_ready:
    ld (temporary_suffix),a
    ld (first_suffix),a

reserve_pair_attempt:
    call patch_temporary_names

    ld de,tl_path
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jr nz,reserve_tl_failed
    ld a,b
    ld (tl_handle),a
    ld a,(created_mask)
    or CREATED_TL
    ld (created_mask),a

    ld de,tsr_path
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jr nz,reserve_tsr_failed
    ld a,b
    ld (tsr_handle),a
    ld a,(created_mask)
    or CREATED_TSR
    ld (created_mask),a
    xor a
    ret

reserve_tl_failed:
    call is_name_collision
    ret nz
    jr reserve_next_suffix

reserve_tsr_failed:
    ld (saved_create_error),a
    call discard_open_tl
    or a
    ret nz
    ld a,(saved_create_error)
    call is_name_collision
    ret nz

reserve_next_suffix:
    ld a,(temporary_suffix)
    inc a
    cp 36
    jr c,reserve_suffix_incremented
    xor a
reserve_suffix_incremented:
    ld (temporary_suffix),a
    ld b,a
    ld a,(first_suffix)
    cp b
    jr nz,reserve_pair_attempt
    ld a,ERR_FILE_EXISTS
    ret

; Z is set only for errors which mean that this candidate name is occupied.
is_name_collision:
    cp ERR_FILE_EXISTS
    ret z
    cp ERR_DIRECTORY_EXISTS
    ret z
    cp ERR_SYSTEM_EXISTS
    ret

discard_open_tl:
    ld a,(tl_handle)
    cp INVALID_HANDLE
    jr z,discard_open_tl_done
    ld b,a
    ld c,DOS_HDELETE
    call 00005h
    ld (saved_cleanup_result),a
    ld a,INVALID_HANDLE
    ld (tl_handle),a
    ld a,(saved_cleanup_result)
    or a
    ret nz
    ld a,(created_mask)
    and 0FEh
    ld (created_mask),a
discard_open_tl_done:
    xor a
    ret

patch_temporary_names:
    ld a,(temporary_suffix)
    call suffix_to_character
    ld (tl_suffix),a
    ld (tsr_suffix),a
    ld (command_tl_run_suffix),a
    ld (command_tsr_load_suffix),a
    ld (command_tsr_delete_suffix),a
    ld (command_tl_delete_suffix),a
    ret

suffix_to_character:
    cp 10
    jr nc,suffix_to_letter
    add a,'0'
    ret
suffix_to_letter:
    add a,'A' - 10
    ret

; ---------------------------------------------------------------------------
; Blob extraction.  The TSR is emitted in three writes so the immutable
; embedded image is never modified: prefix, selected driver byte, suffix.

write_temporary_files:
    ld de,tl_blob_start
    ld hl,tl_blob_size
    ld a,(tl_handle)
    ld b,a
    call write_exact
    or a
    ret nz

    ld a,(tsr_handle)
    ld b,a
    ld de,tsr_blob_start
    ld hl,MSXAI_TSR_TRANSPORT_OFFSET
    call write_exact
    or a
    ret nz

    ld a,(tsr_handle)
    ld b,a
    ld de,loader_transport_id
    ld hl,1
    call write_exact
    or a
    ret nz

    ld de,tsr_blob_start
    ld hl,MSXAI_TSR_TRANSPORT_OFFSET + 1
    add hl,de
    ex de,hl
    ld hl,tsr_blob_size
    ld bc,MSXAI_TSR_TRANSPORT_OFFSET + 1
    or a
    sbc hl,bc
    ld a,(tsr_handle)
    ld b,a
    call write_exact
    ret

; Input: B=handle, DE=buffer, HL=count.  Output: A=0 only when the complete
; request was written.  A successful short write is treated as an internal
; error even though regular disk files should not produce one.
write_exact:
    ld a,h
    or l
    ret z
    push hl
    ld c,DOS_WRITE
    call 00005h
    pop de
    or a
    ret nz
    or a
    sbc hl,de
    ret z
    ld a,ERR_INTERNAL
    ret

close_temporary_files:
    ld a,(tl_handle)
    cp INVALID_HANDLE
    jr z,close_tsr_file
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    or a
    ret nz
    ld a,INVALID_HANDLE
    ld (tl_handle),a

close_tsr_file:
    ld a,(tsr_handle)
    cp INVALID_HANDLE
    jr z,close_files_done
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    or a
    ret nz
    ld a,INVALID_HANDLE
    ld (tsr_handle),a
close_files_done:
    xor a
    ret

; ---------------------------------------------------------------------------
; Failure rollback.  HDELETE is used for open files.  Closed files are removed
; by pathname only when their created-mask bit proves this invocation created
; them.  Cleanup errors are recorded and reported, while cleanup continues for
; the other file.

cleanup_temporaries:
    call cleanup_open_tl
    call cleanup_open_tsr
    call cleanup_closed_tsr
    call cleanup_closed_tl
    ret

cleanup_open_tl:
    ld a,(tl_handle)
    cp INVALID_HANDLE
    ret z
    ld b,a
    ld c,DOS_HDELETE
    call 00005h
    ld (saved_cleanup_result),a
    ld a,INVALID_HANDLE
    ld (tl_handle),a
    ld a,(saved_cleanup_result)
    or a
    jr nz,record_cleanup_error
    ld a,(created_mask)
    and 0FEh
    ld (created_mask),a
    ret

cleanup_open_tsr:
    ld a,(tsr_handle)
    cp INVALID_HANDLE
    ret z
    ld b,a
    ld c,DOS_HDELETE
    call 00005h
    ld (saved_cleanup_result),a
    ld a,INVALID_HANDLE
    ld (tsr_handle),a
    ld a,(saved_cleanup_result)
    or a
    jr nz,record_cleanup_error
    ld a,(created_mask)
    and 0FDh
    ld (created_mask),a
    ret

cleanup_closed_tsr:
    ld a,(created_mask)
    and CREATED_TSR
    ret z
    ld de,tsr_path
    ld c,DOS_DELETE
    call 00005h
    or a
    jr z,cleanup_closed_tsr_clear
    cp ERR_NO_FILE
    jr nz,record_cleanup_error
cleanup_closed_tsr_clear:
    ld a,(created_mask)
    and 0FDh
    ld (created_mask),a
    ret

cleanup_closed_tl:
    ld a,(created_mask)
    and CREATED_TL
    ret z
    ld de,tl_path
    ld c,DOS_DELETE
    call 00005h
    or a
    jr z,cleanup_closed_tl_clear
    cp ERR_NO_FILE
    jr nz,record_cleanup_error
cleanup_closed_tl_clear:
    ld a,(created_mask)
    and 0FEh
    ld (created_mask),a
    ret

record_cleanup_error:
    ld (saved_cleanup_result),a
    ld a,(cleanup_error)
    or a
    jr nz,record_cleanup_flag
    ld a,(saved_cleanup_result)
    ld (cleanup_error),a
record_cleanup_flag:
    ld a,1
    ld (cleanup_failed),a
    ret

loader_abort:
    ld (last_error),a
    call cleanup_temporaries

    ld de,loader_error_message
    ld c,9
    call 00005h
    ld a,(cleanup_failed)
    or a
    jr z,loader_abort_terminate
    ld de,cleanup_error_message
    ld c,9
    call 00005h

loader_abort_terminate:
    ld a,(dos2_available)
    or a
    jp z,00000h
    ld a,(last_error)
    or a
    jr nz,loader_abort_have_code
    ld a,(cleanup_error)
    or a
    jr nz,loader_abort_have_code
    ld a,ERR_INTERNAL
loader_abort_have_code:
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

; ---------------------------------------------------------------------------
; MemMan handoff.  The tail is copied to the standard DOS location.  A tiny
; position-independent stub is copied above every embedded byte, then executes
; LDIR while the loader at 0100h is being overwritten.

build_memman_command_tail:
    ld hl,install_command
    ld bc,install_command_length
    ld a,(memman_loader_action)
    cp MEMMAN_ACTION_UNINSTALL
    jr nz,build_memman_command_selected
    ld hl,uninstall_command
    ld bc,uninstall_command_length
build_memman_command_selected:
    ld de,COMMAND_TEXT
    push bc
    ldir
    pop bc
    ld a,c
    ld (COMMAND_TAIL),a
    xor a
    ld (de),a
    ret

handoff_to_memman:
    ld hl,overlay_stub
    ld de,(overlay_target)
    ld bc,overlay_stub_size
    ldir

    ; Reset the stack to its COM-entry value, then RET directly into the copied
    ; stub.  RET consumes the temporary address and restores the original SP
    ; before MEMMAN.COM receives control at 0100h.
    ld hl,(loader_entry_sp)
    ld sp,hl
    ld hl,(overlay_target)
    push hl
    ld hl,memman_blob_start
    ld de,COM_ENTRY
    ld bc,memman_blob_size
    ret

handoff_to_tk:
    ld hl,overlay_stub
    ld de,(overlay_target)
    ld bc,overlay_stub_size
    ldir

    ld hl,(loader_entry_sp)
    ld sp,hl
    ld hl,(overlay_target)
    push hl
    ld hl,tk_blob_start
    ld de,COM_ENTRY
    ld bc,tk_blob_size
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
loader_put_handle:
    db INVALID_HANDLE
loader_put_upload_started:
    db 0
loader_put_tsr_id:
    dw 0
loader_put_written:
    dw 0
loader_put_next_written:
    dw 0
loader_put_chunk_length:
    dw 0
loader_put_crc:
    dw 0FFFFh
loader_put_last_progress:
    dw 0
overlay_target:
    dw 0
tl_handle:
    db INVALID_HANDLE
tsr_handle:
    db INVALID_HANDLE
created_mask:
    db 0
temporary_suffix:
    db 0
first_suffix:
    db 0
saved_create_error:
    db 0
saved_cleanup_result:
    db 0
last_error:
    db 0
cleanup_error:
    db 0
cleanup_failed:
    db 0
dos2_available:
    db 0
memman_loader_action:
    db MEMMAN_ACTION_INSTALL
memman_present:
    db 0
memman_compatible:
    db 0

tl_path:
    db "M"
tl_suffix:
    db "0"
    db ".COM",0

tsr_path:
    db "A"
tsr_suffix:
    db "0"
    db ".TSR",0

; '@' is MemMan's documented representation of Return.  The final two commands
; run only after TL returns, removing the extracted payload and loader utility.
install_command:
    ; MemMan 2.42 consumes the first Return while warm-booting COMMAND2.  The
    ; second '@' is required before the first visible DOS command.
    db " _SYSTEM@@M"
command_tl_run_suffix:
    db "0"
    db " A"
command_tsr_load_suffix:
    db "0"
    db "@DEL A"
command_tsr_delete_suffix:
    db "0"
    db ".TSR@DEL M"
command_tl_delete_suffix:
    db "0"
    db ".COM@"
install_command_end:
install_command_length: equ install_command_end - install_command

uninstall_command:
    db " ",34,"MSXAI MCP1",34
uninstall_command_end:
uninstall_command_length: equ uninstall_command_end - uninstall_command

loader_error_message:
    db 13,10,"MSXAI resident loader failed.",13,10,"$"
cleanup_error_message:
    db "Temporary-file cleanup was incomplete.",13,10,"$"

; ---------------------------------------------------------------------------
; Embedded build artifacts.  Do not move any data after memman_blob_end without
; extending the overlay/source-overlap guard in loader_preflight.

tl_blob_start:
    incbin 'work/agent/vendor/TL.COM'
tl_blob_end:
tl_blob_size: equ tl_blob_end - tl_blob_start

tk_blob_start:
    incbin 'work/agent/vendor/TK.COM'
tk_blob_end:
tk_blob_size: equ tk_blob_end - tk_blob_start

tsr_blob_start:
    incbin 'work/agent/MSXAI.TSR'
tsr_blob_end:
tsr_blob_size: equ tsr_blob_end - tsr_blob_start

memman_blob_start:
    incbin 'work/agent/vendor/MEMMAN.COM'
memman_blob_end:
memman_blob_size: equ memman_blob_end - memman_blob_start
