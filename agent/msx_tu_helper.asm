; TU.COM - prime TCP/IP UNAPI immediately before MemMan's TL.COM.
;
; Pico/Pico+ lazily installs its H.TIMI hook on the first A=1 TCP/IP UNAPI
; enumeration request.  Doing that from a TSR initialization callback is too
; late: MemMan has already started its own hook transaction.  TU occupies the
; TL position in the first-install command chain and performs that lazy setup
; before TL starts.
;
; The operation is intentionally transactional up to the EXTBIO calls:
;   1. require MSX-DOS 2 and resolve TL.COM through MSXAI_HOME;
;   2. validate its exact pinned size and read it wholly into high TPA RAM;
;   3. close the file handle;
;   4. enumerate TCP/IP UNAPI without invoking any implementation API;
;   5. overlay the already staged TL.COM at 0100h and transfer control to it.
;
; The DOS command tail lives at 0080h and is therefore not overwritten.  TL
; receives the exact tail with which COMMAND2 invoked TU and returns normally
; to COMMAND2 after loading the requested TSR.

BDOS:                    equ 00005h
EXTBIO:                  equ 0FFCAh
UNAPI_ARGUMENT:          equ 0F847h
UNAPI_EXTBIO_MAGIC:      equ 02222h
H_TIMI:                  equ 0FD9Fh
CALLF_OPCODE:            equ 0F7h
PICO_TIMI_ENTRY_LOW:     equ 0B8h
PICO_TIMI_ENTRY_HIGH:    equ 04Ch
RET_OPCODE:              equ 0C9h

DOS_OPEN:                equ 043h
DOS_CLOSE:               equ 045h
DOS_READ:                equ 048h
DOS_SEEK:                equ 04Ah
DOS_TERM_ERROR:          equ 062h
DOS_GET_ENV:             equ 06Bh
DOS_VERSION:             equ 06Fh

OPEN_READ_ONLY:          equ 001h
INVALID_HANDLE:          equ 0FFh
ERR_INTERNAL:            equ 0DFh
ERR_NO_MEMORY:           equ 0DEh
ERR_INVALID_PARAMETER:   equ 08Bh
ERR_BAD_VERSION:         equ 085h

COMMAND_TAIL:            equ 00080h
COM_ENTRY:               equ 00100h
TPA_TOP_POINTER:         equ 00006h
TL_FILE_SIZE:            equ 00A00h
TL_OVERLAY_END:          equ COM_ENTRY + TL_FILE_SIZE
OVERLAY_STACK_HEADROOM:  equ 00200h
SUITE_PATH_MAX:          equ 63
SUITE_PATH_BUFFER_SIZE:  equ SUITE_PATH_MAX + 1
DOS_PATH_SEPARATOR:      equ 05Ch

    org COM_ENTRY

tu_helper_start:
    ld (tu_entry_sp),sp
    xor a
    ld (dos2_available),a
    ld a,INVALID_HANDLE
    ld (tl_handle),a

    call require_dos2
    or a
    jp nz,tu_abort
    call resolve_tl_path
    or a
    jp nz,tu_abort
    call plan_high_stage
    or a
    jp nz,tu_abort
    call stage_tl_exact
    or a
    jp nz,tu_abort

    ; POINT OF NO RETURN.  There is no DOS or file operation after this call.
    ; Every A=1 request may lazily mutate the firmware hook state, so all
    ; recoverable preflight work and the CLOSE have completed first.
    call enumerate_tcpip_unapi
    jp handoff_to_staged_tl


; ---------------------------------------------------------------------------
; Recoverable DOS2 preflight and high-TPA staging.

require_dos2:
    ld c,DOS_VERSION
    call BDOS
    or a
    ret nz
    ld a,b
    cp 2
    jr c,require_dos2_unavailable
    ld a,1
    ld (dos2_available),a
    xor a
    ret
require_dos2_unavailable:
    ld a,ERR_BAD_VERSION
    ret

; MSX-DOS 2 GET_ENV returns an empty ASCIIZ string for an undefined item.
; In that case use TL.COM in the current directory, matching MSXAI.COM.
resolve_tl_path:
    ld hl,suite_home_env_name
    ld de,suite_home_buffer
    ld b,SUITE_PATH_BUFFER_SIZE
    ld c,DOS_GET_ENV
    call BDOS
    or a
    ret nz

    ld hl,suite_home_buffer
    ld de,tl_path
    ld b,SUITE_PATH_MAX
    ld c,0                       ; most recently copied home character
resolve_tl_home_loop:
    ld a,(hl)
    or a
    jr z,resolve_tl_home_done
    ld c,a
    ld a,b
    or a
    jr z,resolve_tl_too_long
    ld a,c
    ld (de),a
    inc de
    inc hl
    dec b
    jr resolve_tl_home_loop
resolve_tl_home_done:
    ld a,(suite_home_buffer)
    or a
    jr z,resolve_tl_name
    ld a,c
    cp DOS_PATH_SEPARATOR
    jr z,resolve_tl_name
    cp '/'
    jr z,resolve_tl_name
    ld a,b
    or a
    jr z,resolve_tl_too_long
    ld a,DOS_PATH_SEPARATOR
    ld (de),a
    inc de
    dec b
resolve_tl_name:
    ld hl,tl_name
resolve_tl_name_loop:
    ld a,(hl)
    or a
    jr z,resolve_tl_complete
    ld c,a
    ld a,b
    or a
    jr z,resolve_tl_too_long
    ld a,c
    ld (de),a
    inc de
    inc hl
    dec b
    jr resolve_tl_name_loop
resolve_tl_complete:
    xor a
    ld (de),a
    ret
resolve_tl_too_long:
    ld a,ERR_INVALID_PARAMETER
    ret

; Choose min(TPA top, entry SP), leave private stack/DOS headroom, then put the
; fixed-size TL image immediately below the relocated overlay stub.  Source
; and destination must be disjoint and the source may not cover live TU data.
plan_high_stage:
    ld hl,(TPA_TOP_POINTER)
    ld de,(tu_entry_sp)
    or a
    sbc hl,de
    jr c,plan_limit_is_tpa
    ld hl,(tu_entry_sp)
    jr plan_have_limit
plan_limit_is_tpa:
    ld hl,(TPA_TOP_POINTER)
plan_have_limit:
    ld de,OVERLAY_STACK_HEADROOM + overlay_stub_size
    or a
    sbc hl,de
    jr c,plan_high_stage_no_memory
    ld (overlay_target),hl

    ld de,TL_FILE_SIZE
    or a
    sbc hl,de
    jr c,plan_high_stage_no_memory
    ld (tl_stage_source),hl

    push hl
    ld de,tu_helper_end
    or a
    sbc hl,de
    pop hl
    jr c,plan_high_stage_no_memory

    ; The final LDIR runs upward.  A source at or above 0B00h cannot be
    ; overwritten by the 0100h..0AFFh destination before it is consumed.
    ld de,TL_OVERLAY_END
    or a
    sbc hl,de
    jr c,plan_high_stage_no_memory
    xor a
    ret
plan_high_stage_no_memory:
    ld a,ERR_NO_MEMORY
    ret

; Open once, prove exact 2560-byte length, rewind, read the complete file in a
; single DOS2 call, then close.  Every error path also closes the handle.
stage_tl_exact:
    ld de,tl_path
    ld a,OPEN_READ_ONLY
    ld c,DOS_OPEN
    call BDOS
    or a
    ret nz
    ld a,b
    ld (tl_handle),a

    ld hl,0
    ld de,0
    ld a,2                       ; seek from EOF
    ld c,DOS_SEEK
    call BDOS
    or a
    jr nz,close_tl_preserving_error
    ld a,d
    or e
    jr nz,stage_tl_bad_image
    ld bc,TL_FILE_SIZE
    or a
    sbc hl,bc
    jr nz,stage_tl_bad_image

    ld hl,0
    ld de,0
    ld a,(tl_handle)
    ld b,a
    xor a                        ; seek from start
    ld c,DOS_SEEK
    call BDOS
    or a
    jr nz,close_tl_preserving_error
    ld a,d
    or e
    jr nz,stage_tl_bad_image
    ld a,h
    or l
    jr nz,stage_tl_bad_image

    ld de,(tl_stage_source)
    ld hl,TL_FILE_SIZE
    push hl
    ld a,(tl_handle)
    ld b,a
    ld c,DOS_READ
    call BDOS
    pop de
    or a
    jr nz,close_tl_preserving_error
    or a
    sbc hl,de
    jr nz,stage_tl_bad_image
    xor a
    jp close_tl_preserving_error

stage_tl_bad_image:
    ld a,ERR_INTERNAL

; Keep the operation's error unless it succeeded and CLOSE itself failed.
close_tl_preserving_error:
    push af
    ld a,(tl_handle)
    cp INVALID_HANDLE
    jr z,close_tl_no_handle
    ld b,a
    ld c,DOS_CLOSE
    call BDOS
    ld (tl_close_error),a
    ld a,INVALID_HANDLE
    ld (tl_handle),a
    pop af
    or a
    ret nz
    ld a,(tl_close_error)
    ret
close_tl_no_handle:
    pop af
    ret


; ---------------------------------------------------------------------------
; Side-effect window: enumerate, harden the Pico+ CALLF hook, then enter TL.

; Query A=0 first, then every implementation A=1..B.  The identifier is
; restored before each EXTBIO request as required by UNAPI.  No implementation
; API is called.  Some firmware executes EI internally, so each EXTBIO has an
; explicit EI immediately before it and DI immediately after it.
enumerate_tcpip_unapi:
    call copy_tcpip_api_id
    ld de,UNAPI_EXTBIO_MAGIC
    xor a
    ld b,0
enumerate_count_extbio:
    ei
    call EXTBIO
    di
    ld a,b
    ld (implementation_remaining),a
    or a
    jr z,enumerate_tcpip_complete
    ld a,1
    ld (implementation_index),a

enumerate_tcpip_candidate_loop:
    call copy_tcpip_api_id
    ld de,UNAPI_EXTBIO_MAGIC
    ; A normalized pre-TL hook starts with RET, so its fifth byte is inert.
    ; Pre-fill that inert byte before Pico can install its four-byte CALLF body;
    ; this closes the interrupt boundary between EXTBIO's RET and our DI.
    call prepare_pico_htimi_tail
    ld a,(implementation_index)
enumerate_candidate_extbio:
    ei
    call EXTBIO
    di
    call harden_pico_htimi

    ld a,(implementation_index)
    inc a
    ld (implementation_index),a
    ld a,(implementation_remaining)
    dec a
    ld (implementation_remaining),a
    jr nz,enumerate_tcpip_candidate_loop

enumerate_tcpip_complete:
    ei
    ret
enumerate_tcpip_unapi_end:

; If the current pre-TL hook is already a one-byte RET, its remaining bytes are
; unreachable.  Seed the future CALLF terminator before EXTBIO so even an IRQ at
; the callee-return boundary sees a complete Pico hook.  Any other hook is left
; completely untouched and remains protected by the exact post-call hardener.
prepare_pico_htimi_tail:
    ld a,(H_TIMI)
    cp RET_OPCODE
    ret nz
    ld (H_TIMI+4),a
    ret
prepare_pico_htimi_tail_end:

copy_tcpip_api_id:
    ld hl,tcpip_api_id
    ld de,UNAPI_ARGUMENT
    ld bc,tcpip_api_id_end-tcpip_api_id
    ldir
    ret

; Pico/Pico+ 2.12 writes only the four-byte CALLF body at FD9Fh..FDA2h.
; The five-byte hook contract also needs RET at FDA3h.  Repair that byte only
; when the exact firmware signature F7 <slot> B8 4C is present; the slot byte
; is intentionally unconstrained.  The firmware's saved prior hook, HIMEM and
; its private pointer block are left untouched.  On an already clean boot the
; write is idempotent because FDA3h is C9h.
harden_pico_htimi:
    ld a,(H_TIMI)
    cp CALLF_OPCODE
    ret nz
    ld a,(H_TIMI+2)
    cp PICO_TIMI_ENTRY_LOW
    ret nz
    ld a,(H_TIMI+3)
    cp PICO_TIMI_ENTRY_HIGH
    ret nz
    ld a,RET_OPCODE
    ld (H_TIMI+4),a
    ret
harden_pico_htimi_end:


; ---------------------------------------------------------------------------
; Irreversible in-memory overlay.  The command tail at 0080h is unchanged.

handoff_to_staged_tl:
    ld hl,overlay_stub
    ld de,(overlay_target)
    ld bc,overlay_stub_size
    ldir

    ; RET enters the relocated stub while restoring the original COM stack.
    ; The stub copies exactly TL_FILE_SIZE bytes and jumps to TL's 0100h entry.
    ld hl,(tu_entry_sp)
    ld sp,hl
    ld hl,(overlay_target)
    push hl
    ld hl,(tl_stage_source)
    ld de,COM_ENTRY
    ld bc,TL_FILE_SIZE
    ret
handoff_to_staged_tl_end:

overlay_stub:
    ldir
    jp COM_ENTRY
overlay_stub_end:
overlay_stub_size: equ overlay_stub_end-overlay_stub


; ---------------------------------------------------------------------------
; Error exit.  This is reachable only before UNAPI enumeration begins.

tu_abort:
    ld (last_error),a
    call close_tl_preserving_error
    ld de,error_message
    ld c,9
    call BDOS
    ld a,(dos2_available)
    or a
    jp z,00000h
    ld a,(last_error)
    or a
    jr nz,tu_abort_have_code
    ld a,ERR_INTERNAL
tu_abort_have_code:
    ld b,a
    ld c,DOS_TERM_ERROR
    call BDOS
    jp 00000h


; ---------------------------------------------------------------------------
; Mutable page-0 state.  It is consumed before TL overwrites TU at 0100h.

tu_entry_sp:
    dw 0
overlay_target:
    dw 0
tl_stage_source:
    dw 0
tl_handle:
    db INVALID_HANDLE
tl_close_error:
    db 0
last_error:
    db 0
dos2_available:
    db 0
implementation_remaining:
    db 0
implementation_index:
    db 0

suite_home_env_name:
    db "MSXAI_HOME",0
tl_name:
    db "TL.COM",0
tcpip_api_id:
    db "TCP/IP",0
tcpip_api_id_end:
suite_home_buffer:
    ds SUITE_PATH_BUFFER_SIZE,0
tl_path:
    ds SUITE_PATH_BUFFER_SIZE,0
error_message:
    db "TU: cannot prime TCP/IP UNAPI before TL.",13,10,"$"

tu_helper_end:
