; MemMan lifecycle used by the transient half of MSXAI.COM.
;
; The public-domain MemMan utilities and the transport-patched TSR images
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

DOS_CLOSE:               equ 045h
DOS_OPEN:                equ 043h
DOS_READ:                equ 048h
DOS_SEEK:                equ 04Ah
DOS_TERM_ERROR:          equ 062h
DOS_DEFAB:               equ 063h
DOS_GET_ENV:             equ 06Bh
DOS_VERSION:             equ 06Fh

OPEN_READ_ONLY:          equ 001h

ERR_INTERNAL:            equ 0DFh
ERR_NO_MEMORY:           equ 0DEh
ERR_NO_FILE:             equ 0D7h
ERR_FILE_EXISTS:         equ 0CBh
ERR_INVALID_PARAMETER:   equ 08Bh
ERR_BAD_VERSION:         equ 085h

DRIVER_8251:             equ 0
DRIVER_16C550:           equ 1
DRIVER_UNAPI:             equ 2

INVALID_HANDLE:          equ 0FFh

JIFFY:                   equ 0FC9Eh
RG9SAV:                  equ 0FFE8h
COMMAND_TAIL:            equ 00080h
COMMAND_TEXT:            equ 00081h
TPA_TOP_POINTER:         equ 00006h
COM_ENTRY:               equ 00100h

MEMMAN_ACTION_INSTALL:   equ 0
MEMMAN_ACTION_UNINSTALL: equ 1
; MemMan's 40-byte buffer includes its terminator. At most 39 command bytes
; survive the warm boot; a 40th byte is silently lost by version 2.42.
MEMMAN_COMMAND_MAX:      equ 39
SUITE_PATH_MAX:          equ 63
SUITE_PATH_BUFFER_SIZE:  equ SUITE_PATH_MAX + 1
DOS_PATH_SEPARATOR:      equ 05Ch

; Exact sizes of the pinned MemMan 2.42 public-domain utilities.  The build
; materializer verifies their SHA-256 values before these files are packaged.
MEMMAN_FILE_SIZE:        equ 01E00h ; 7680 bytes
TL_FILE_SIZE:            equ 00A00h ; 2560 bytes
TK_FILE_SIZE:            equ 00580h ; 1408 bytes
MP_FILE_SIZE:            equ 0039Ah ; 922-byte guarded-stack port helper

; Leave normal transient-program stack space above the relocation trampoline.
; Once MEMMAN.COM starts, the old loader image and trampoline are disposable.
OVERLAY_STACK_HEADROOM:  equ 00080h
include 'work/agent/build/MSXAI_TSR.INC'

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
    ld (suite_port_helper_required),a
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
    cp DRIVER_UNAPI
    jr z,preflight_select_unapi
    ld a,ERR_INVALID_PARAMETER
    ret

preflight_select_8251:
    ld de,suite_mcp8251_tsr_path
    xor a
    jr preflight_install_selected

preflight_select_16c550:
    ld de,suite_mcp16550_tsr_path
    ld a,DRIVER_16C550
    jr preflight_install_selected

preflight_select_unapi:
    ld de,suite_mcpunapi_tsr_path
    ld a,1
    ld (suite_port_helper_required),a
    ld a,DRIVER_UNAPI

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
    ld a,(suite_port_helper_required)
    or a
    jr z,preflight_command_length
    ld de,suite_mp_path
    ld hl,MP_FILE_SIZE
    call suite_validate_regular_file
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

    ; MSX-DOS accepts 127 characters, but MemMan 2.42 preserves only 39 command
    ; bytes across its warm boot. Keep both supported tails inside that limit.
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
    or a
    ret nz
    ld bc,mcpunapi_tsr_name
    ld de,suite_mcpunapi_tsr_path
    call suite_build_path
    or a
    ret nz
    ld bc,mp_name
    ld de,suite_mp_path
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
    ld a,(suite_port_helper_required)
    or a
    jr z,suite_build_install_command_length
    ld hl,install_port_helper_prefix
    ld bc,install_port_helper_prefix_length
    ldir
    ld hl,(loader_unapi_port)
    ld a,h
    rrca
    rrca
    rrca
    rrca
    call suite_build_install_command_hex_nibble
    ld a,h
    call suite_build_install_command_hex_nibble
    ld a,l
    rrca
    rrca
    rrca
    rrca
    call suite_build_install_command_hex_nibble
    ld a,l
    call suite_build_install_command_hex_nibble
suite_build_install_command_port_done:
    ld a,'@'
    ld (de),a
    inc de
    jr suite_build_install_command_length

; Append one uppercase hexadecimal nibble from A to the command at DE.
suite_build_install_command_hex_nibble:
    and 00Fh
    cp 10
    jr c,suite_build_install_command_hex_decimal
    add a,'A' - 10 - '0'
suite_build_install_command_hex_decimal:
    add a,'0'
    ld (de),a
    inc de
    ret
suite_build_install_command_length:
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

; Input BC is the ID returned by GetTsrID. Every transport selection uses the
; A7 request ABI so transitions into or out of UNAPI always run lifecycle work
; on a guarded page-2 stack instead of MemMan's small internal TsrCall stack.
memman_reconfigure_agent:
    ld (memman_reconfigure_id),bc
    call memman_prepare_unapi_request
    jr c,memman_reconfigure_unapi_failed
    ld bc,(memman_reconfigure_id)
    ld hl,memman_unapi_request
    ld a,TSR_TALK_UNAPI_PORT
    ld d,'M'
    ld e,63                    ; TsrCall
    call EXTBIO
    di
    ld (memman_unapi_call_result),a
    call memman_verify_unapi_guards
    jr c,memman_reconfigure_unapi_failed
    ld a,(memman_unapi_request_status)
    or a
    jr nz,memman_reconfigure_unapi_failed
    ld a,(memman_unapi_request_transport)
    ld b,a
    ld a,(loader_transport_id)
    cp b
    jr nz,memman_reconfigure_unapi_failed
    ld a,(memman_unapi_call_result)
    ret
memman_reconfigure_unapi_failed:
    ld a,0FFh
    ret

memman_prepare_unapi_request:
    ld hl,(loader_unapi_port)
    ld (memman_unapi_request_port),hl

    ld hl,0C000h
    ld de,(TPA_TOP_POINTER)
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_limit_is_c000
    ld hl,(TPA_TOP_POINTER)
    jr memman_prepare_unapi_have_tpa_limit
memman_prepare_unapi_limit_is_c000:
    ld hl,0C000h
memman_prepare_unapi_have_tpa_limit:
    ld (memman_unapi_request_stack_limit),hl

    ld hl,0
    add hl,sp
    ld de,0100h
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_no_stack
    ld (memman_unapi_request_sp_limit),hl
    ld de,(memman_unapi_request_stack_limit)
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_limit_is_sp
    ld hl,(memman_unapi_request_stack_limit)
    jr memman_prepare_unapi_have_limit
memman_prepare_unapi_limit_is_sp:
    ld hl,(memman_unapi_request_sp_limit)
memman_prepare_unapi_have_limit:
    ld de,TSR_UNAPI_STACK_GUARD_SIZE
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_no_stack
    res 0,l
    ld (memman_unapi_request_stack_top),hl
    ld de,TSR_UNAPI_STACK_MINIMUM
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_no_stack
    ld (memman_unapi_request_stack_bottom),hl
    ld de,TSR_UNAPI_STACK_GUARD_SIZE
    or a
    sbc hl,de
    jr c,memman_prepare_unapi_no_stack
    ld a,h
    cp 080h
    jr c,memman_prepare_unapi_no_stack

    ; The complete span is now proven to remain within writable page 2.
    ld b,TSR_UNAPI_STACK_GUARD_SIZE
    ld a,TSR_UNAPI_STACK_LOW_GUARD
memman_prepare_unapi_low_guard_loop:
    ld (hl),a
    inc hl
    djnz memman_prepare_unapi_low_guard_loop
    ld hl,(memman_unapi_request_stack_top)
    ld b,TSR_UNAPI_STACK_GUARD_SIZE
    ld a,TSR_UNAPI_STACK_HIGH_GUARD
memman_prepare_unapi_high_guard_loop:
    ld (hl),a
    inc hl
    djnz memman_prepare_unapi_high_guard_loop

    ld a,0FFh
    ld (memman_unapi_request_status),a
    ld (memman_unapi_request_error),a
    ld (memman_unapi_request_transport),a
    ld a,(loader_transport_id)
    ld (memman_unapi_request_target),a
    xor a
    ld (memman_unapi_request_connection),a
    ld (memman_unapi_request_reserved),a
    or a
    ret
memman_prepare_unapi_no_stack:
    scf
    ret

memman_verify_unapi_guards:
    ld hl,(memman_unapi_request_stack_bottom)
    ld de,TSR_UNAPI_STACK_GUARD_SIZE
    or a
    sbc hl,de
    ld a,TSR_UNAPI_STACK_LOW_GUARD
    call memman_verify_unapi_guard
    jr nz,memman_verify_unapi_guards_bad
    ld hl,(memman_unapi_request_stack_top)
    ld a,TSR_UNAPI_STACK_HIGH_GUARD
    call memman_verify_unapi_guard
    jr nz,memman_verify_unapi_guards_bad
    or a
    ret
memman_verify_unapi_guards_bad:
    scf
    ret
memman_verify_unapi_guard:
    ld b,TSR_UNAPI_STACK_GUARD_SIZE
memman_verify_unapi_guard_loop:
    cp (hl)
    ret nz
    inc hl
    djnz memman_verify_unapi_guard_loop
    ret

memman_tsr_name:
    db "MSXAI MCP1  "           ; exactly 12 bytes, padded for GetTsrID

memman_reconfigure_id:
    dw 0
memman_unapi_call_result:
    db 0FFh
memman_unapi_request:
    dw TSR_UNAPI_REQUEST_MAGIC
    db TSR_UNAPI_REQUEST_VERSION
    db TSR_UNAPI_REQUEST_SIZE
memman_unapi_request_port:
    dw 0
memman_unapi_request_stack_bottom:
    dw 0
memman_unapi_request_stack_top:
    dw 0
memman_unapi_request_status:
    db 0FFh
memman_unapi_request_error:
    db 0FFh
memman_unapi_request_transport:
    db 0FFh
memman_unapi_request_connection:
    db 0
memman_unapi_request_target:
    db DRIVER_UNAPI
memman_unapi_request_reserved:
    db 0

memman_unapi_request_stack_limit:
    dw 0
memman_unapi_request_sp_limit:
    dw 0

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
suite_port_helper_required:
    db 0
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
mcpunapi_tsr_name:
    db "MCPUNAPI.TSR",0
mp_name:
    db "MP.COM",0
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
suite_mcpunapi_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mp_path:
    ds SUITE_PATH_BUFFER_SIZE,0

; '@' is MemMan's documented representation of Return.  MemMan 2.42 consumes
; the first Return while warm-booting COMMAND2, so the second '@' is required
; before the visible TL command. TL accepts a TSR path without extension.
install_command_prefix:
    ; MemMan consumes this normal command-tail leading blank before injecting
    ; the post-warm-boot commands into COMMAND2.
    db " _SYSTEM@@TL "
install_command_prefix_end:
install_command_prefix_length: equ install_command_prefix_end - install_command_prefix
install_port_helper_prefix:
    ; COMMAND2 recognizes slash as the executable/tail delimiter. The following
    ; fixed four hex digits keep `@MP/FFFE@` within the 39-byte payload limit.
    db "MP/"
install_port_helper_prefix_end:
install_port_helper_prefix_length: equ install_port_helper_prefix_end - install_port_helper_prefix
install_command_buffer:
    ds install_command_prefix_length + SUITE_PATH_MAX + 1 + install_port_helper_prefix_length + 5 + 1,0

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
