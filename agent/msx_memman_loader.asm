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
; Installation validates MEMMAN.COM, TL.COM, and the selected fixed-driver TSR.
; UNAPI additionally validates the pre-TL TU.COM helper and MP.COM.  The loader
; stages external MEMMAN.COM at the top of the TPA and overlays address 0100h.
; Uninstall uses the same mechanism for TK.COM.  No temporary file is created,
; patched, deleted, or left behind by either lifecycle.

DOS_CLOSE:               equ 045h
DOS_CREATE:              equ 044h
DOS_ENSURE:              equ 046h
DOS_OPEN:                equ 043h
DOS_READ:                equ 048h
DOS_WRITE:               equ 049h
DOS_SEEK:                equ 04Ah
DOS_TERM_ERROR:          equ 062h
DOS_DEFAB:               equ 063h
DOS_GET_ENV:             equ 06Bh
DOS_VERSION:             equ 06Fh

OPEN_READ_ONLY:          equ 001h
CREATE_WRITE_ONLY:       equ 002h
CREATE_NEW:              equ 080h

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
MP_FILE_SIZE:            equ 004DAh ; 1242-byte guarded-stack endpoint helper
TU_FILE_SIZE:            equ 0040Bh ; 1035-byte pre-TL UNAPI helper

; Leave normal transient-program stack space above the relocation trampoline.
; Once MEMMAN.COM starts, the old loader image and trampoline are disposable.
OVERLAY_STACK_HEADROOM:  equ 00080h
if MSXAI_DEVELOPMENT_TRACE
include 'work/agent-trace/build/MSXAI_TSR.INC'
else
include 'work/agent/build/MSXAI_TSR.INC'
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
    ; handle.  Installation's MemMan command line invokes external TL.COM for
    ; UART or TU.COM followed by TL.COM internally for UNAPI, then loads one
    ; fixed-driver TSR. Uninstall's tail is consumed directly by TK.COM.
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
    ld a,(loader_uart16c550_divisor)
    cp UART16C550_DIVISOR_115200
    jr nz,preflight_select_16c550_path_ready
    ld de,suite_mcp115k_tsr_path
preflight_select_16c550_path_ready:
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
    ld de,suite_tu_path
    ld hl,TU_FILE_SIZE
    call suite_validate_regular_file
    or a
    ret nz
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
    ld bc,tu_name
    ld de,suite_tu_path
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
    ld bc,mcp115k_tsr_name
    ld de,suite_mcp115k_tsr_path
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

; Build MemMan's post-warm-boot command. COMMAND2 finds TL (UART) or TU
; (UNAPI) through PATH. TU primes TCP/IP UNAPI and overlays TL internally.
; Both receive the fully resolved TSR stem, with its canonical suffix removed.
suite_build_install_command:
    ld a,(suite_port_helper_required)
    or a
    jr nz,suite_build_install_command_select_unapi
    ld hl,install_uart_command_prefix
    ld bc,install_uart_command_prefix_length
    jr suite_build_install_command_copy_prefix
suite_build_install_command_select_unapi:
    ld hl,install_unapi_command_prefix
    ld bc,install_unapi_command_prefix_length
suite_build_install_command_copy_prefix:
    ld de,install_command_buffer
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
    push af
    ld a,(loader_trace_enabled)
    or a
    jr z,suite_build_install_command_high_plain
    pop af
    and 00Fh
    add a,'G'                  ; G..V encodes TRACE plus high nibble 0..15
    ld (de),a
    inc de
    jr suite_build_install_command_high_done
suite_build_install_command_high_plain:
    pop af
    call suite_build_install_command_hex_nibble
suite_build_install_command_high_done:
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
    jp nz,suite_close_preserving_error
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
    jp suite_close_preserving_error

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
    jp nz,suite_close_preserving_error
    ld a,d
    or e
    jp nz,suite_exact_size_error
    ld de,suite_probe_byte
    ld hl,1
    ld a,(suite_handle)
    ld b,a
    ld c,DOS_READ
    call 00005h
    or a
    jp nz,suite_close_preserving_error
    ld a,h
    or a
    jp nz,suite_exact_size_error
    ld a,l
    cp 1
    jp nz,suite_exact_size_error
    ld a,(suite_probe_byte)
    ld b,a
    ld a,(suite_expected_transport)
    cp b
    jr nz,suite_exact_size_error

    ld hl,MSXAI_TSR_16C550_DIVISOR_OFFSET
    ld de,0
    ld a,(suite_handle)
    ld b,a
    xor a
    ld c,DOS_SEEK
    call 00005h
    or a
    jp nz,suite_close_preserving_error
    ld a,d
    or e
    jp nz,suite_exact_size_error
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
    jp nz,suite_exact_size_error
    ld a,l
    cp 1
    jp nz,suite_exact_size_error
    ld a,(suite_probe_byte)
    ld b,a
    ld a,UART16C550_DIVISOR_57600
    ld c,a
    ld a,(suite_expected_transport)
    cp DRIVER_16C550
    jr nz,suite_validate_selected_tsr_divisor_ready
    ld a,(loader_uart16c550_divisor)
    ld c,a
suite_validate_selected_tsr_divisor_ready:
    ld a,c
    cp b
    jp nz,suite_exact_size_error
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
    xor a
    ld b,a
    ld a,(loader_transport_id)
    cp DRIVER_16C550
    jr nz,memman_reconfigure_divisor_expected
    ld a,(loader_uart16c550_divisor)
    ld b,a
memman_reconfigure_divisor_expected:
    ld a,(memman_unapi_request_16c550_divisor)
    cp b
    jr nz,memman_reconfigure_unapi_failed
    ld a,(memman_unapi_call_result)
    ret
memman_reconfigure_unapi_failed:
    ld a,0FFh
    ret

; A8 never touches the selected transport. ENABLE is idempotent; SNAPSHOT
; copies the resident export block into the page-zero buffer below.
memman_enable_trace:
    ld a,TSR_TRACE_ACTION_ENABLE
    jr memman_trace_call

memman_snapshot_trace:
    ld a,TSR_TRACE_ACTION_SNAPSHOT
memman_trace_call:
    ld (memman_trace_action),a
    ld (memman_trace_id),bc
    ld (memman_trace_request_action),a
    ld a,0FFh
    ld (memman_trace_request_status),a
    xor a
    ld (memman_trace_request_length),a
    ld (memman_trace_request_length + 1),a
    ld bc,(memman_trace_id)
    ld hl,memman_trace_request
    ld a,TSR_TALK_TRACE
    ld d,'M'
    ld e,63                    ; TsrCall
    call EXTBIO
    di
    or a
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_request_status)
    or a
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_action)
    cp TSR_TRACE_ACTION_SNAPSHOT
    jr nz,memman_trace_call_ok
    ld hl,(memman_trace_request_length)
    ld de,TRACE_EXPORT_SIZE
    or a
    sbc hl,de
    jr nz,memman_trace_call_failed
    ld hl,(memman_trace_export)
    ld de,TRACE_FORMAT_MAGIC
    or a
    sbc hl,de
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_export + 2)
    cp TRACE_FORMAT_VERSION
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_export + 3)
    cp TRACE_RECORD_SIZE
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_export + 4)
    cp TRACE_RECORD_CAPACITY
    jr nz,memman_trace_call_failed
    ld a,(memman_trace_export + TRACE_EXPORT_COUNT)
    cp TRACE_RECORD_CAPACITY + 1
    jr nc,memman_trace_call_failed
    ld a,(memman_trace_export + TRACE_EXPORT_WRITE_INDEX)
    cp TRACE_RECORD_CAPACITY
    jr nc,memman_trace_call_failed
memman_trace_call_ok:
    ld bc,(memman_trace_id)
    xor a
    ret
memman_trace_call_failed:
    ld bc,(memman_trace_id)
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
    ld (memman_unapi_request_16c550_divisor),a
    ld a,(loader_transport_id)
    cp DRIVER_16C550
    jr nz,memman_prepare_unapi_divisor_ready
    ld a,(loader_uart16c550_divisor)
    ld (memman_unapi_request_16c550_divisor),a
memman_prepare_unapi_divisor_ready:
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
memman_unapi_request_16c550_divisor:
    db 0
memman_unapi_request_local_ip:
    ds 4,0

memman_unapi_request_stack_limit:
    dw 0
memman_unapi_request_sp_limit:
    dw 0

memman_trace_id:
    dw 0
memman_trace_action:
    db 0
memman_trace_request:
    dw TSR_TRACE_REQUEST_MAGIC
    db TSR_TRACE_REQUEST_VERSION
    db TSR_TRACE_REQUEST_SIZE
memman_trace_request_action:
    db 0
memman_trace_request_status:
    db 0FFh
memman_trace_request_length:
    dw 0
    ds TSR_TRACE_REQUEST_SIZE - 8,0
memman_trace_export:
    ds TRACE_EXPORT_SIZE,0
memman_trace_export_end:

; ---------------------------------------------------------------------------
; /DUMPTRACE. The resident has already copied a coherent RAM snapshot into the
; page-zero buffer above. All filesystem work and text formatting happen here,
; in the transient DOS process.

loader_dump_trace:
    xor a
    ld (dos2_available),a
    ld a,INVALID_HANDLE
    ld (trace_dump_handle),a
    ld c,DOS_VERSION
    call 00005h
    or a
    jp nz,loader_dump_trace_fail
    ld a,b
    cp 2
    jr c,loader_dump_trace_bad_version
    ld a,1
    ld (dos2_available),a

    call memman_find_agent
    jr c,loader_dump_trace_no_agent
    call memman_snapshot_trace
    ei
    or a
    jr nz,loader_dump_trace_unavailable

    ld de,(loader_trace_path)
    ld a,CREATE_WRITE_ONLY
    ld b,CREATE_NEW
    ld c,DOS_CREATE
    call 00005h
    or a
    jr nz,loader_dump_trace_fail
    ld a,b
    ld (trace_dump_handle),a
    call trace_dump_write_all
    ld (trace_dump_error),a
    or a
    jr nz,loader_dump_trace_close
    ld a,(trace_dump_handle)
    ld b,a
    ld c,DOS_ENSURE
    call 00005h
    ld (trace_dump_error),a
loader_dump_trace_close:
    ld a,(trace_dump_handle)
    cp INVALID_HANDLE
    jr z,loader_dump_trace_closed
    ld b,a
    ld c,DOS_CLOSE
    call 00005h
    ld b,a
    ld a,INVALID_HANDLE
    ld (trace_dump_handle),a
    ld a,(trace_dump_error)
    or a
    jr nz,loader_dump_trace_closed
    ld a,b
    ld (trace_dump_error),a
loader_dump_trace_closed:
    ld a,(trace_dump_error)
    or a
    jr nz,loader_dump_trace_fail
    ld de,trace_dump_success_message
    ld c,9
    call 00005h
    xor a
    ld b,a
    ld c,DOS_TERM_ERROR
    call 00005h
    jp 00000h

loader_dump_trace_bad_version:
    ld a,ERR_BAD_VERSION
    jr loader_dump_trace_fail
loader_dump_trace_no_agent:
    ld de,trace_dump_no_agent_message
    jr loader_dump_trace_named_fail
loader_dump_trace_unavailable:
    ld de,trace_dump_unavailable_message
loader_dump_trace_named_fail:
    ei
    ld c,9
    call 00005h
    ld a,ERR_INTERNAL
    jr loader_dump_trace_terminate
loader_dump_trace_fail:
    ld (trace_dump_error),a
    ld de,trace_dump_error_message
    ld c,9
    call 00005h
    ld a,(trace_dump_error)
loader_dump_trace_terminate:
    ld b,a
    ld a,(dos2_available)
    or a
    jr z,loader_dump_trace_warm_boot
    ld c,DOS_TERM_ERROR
    call 00005h
loader_dump_trace_warm_boot:
    jp 00000h

trace_dump_write_all:
    call trace_line_reset
    ld de,trace_text_title
    call trace_line_append_string
    call trace_line_finish_write
    or a
    ret nz

    call trace_line_reset
    ld de,trace_text_status_flags
    call trace_line_append_string
    ld a,(memman_trace_export + TRACE_EXPORT_FLAGS)
    call trace_line_append_hex_byte
    ld de,trace_text_count
    call trace_line_append_string
    ld a,(memman_trace_export + TRACE_EXPORT_COUNT)
    call trace_line_append_hex_byte
    ld de,trace_text_next
    call trace_line_append_string
    ld a,(memman_trace_export + TRACE_EXPORT_WRITE_INDEX)
    call trace_line_append_hex_byte
    ld de,trace_text_sequence
    call trace_line_append_string
    ld hl,(memman_trace_export + TRACE_EXPORT_SEQUENCE)
    call trace_line_append_hex_word
    call trace_line_finish_write
    or a
    ret nz

    call trace_line_reset
    ld de,trace_text_polls
    call trace_line_append_string
    ld hl,(memman_trace_export + TRACE_EXPORT_POLLS)
    call trace_line_append_hex_word
    ld de,trace_text_changes
    call trace_line_append_string
    ld hl,(memman_trace_export + TRACE_EXPORT_STATE_CHANGES)
    call trace_line_append_hex_word
    ld de,trace_text_timi
    call trace_line_append_string
    ld hl,(memman_trace_export + TRACE_EXPORT_TIMI)
    call trace_line_append_hex_word
    call trace_line_finish_write
    or a
    ret nz

    ld a,(memman_trace_export + TRACE_EXPORT_FLAGS)
    and TRACE_FLAG_INCIDENT
    jr z,trace_dump_no_incident
    call trace_line_reset
    ld de,trace_text_first
    call trace_line_append_string
    ld hl,memman_trace_export + TRACE_EXPORT_SNAPSHOT
    call trace_line_append_record
    ld de,trace_text_snapshot_extra
    call trace_line_append_string
    ld hl,memman_trace_export + TRACE_EXPORT_SNAPSHOT + TRACE_RECORD_SIZE
    ld b,TRACE_SNAPSHOT_SIZE - TRACE_RECORD_SIZE
trace_dump_snapshot_extra_loop:
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    djnz trace_dump_snapshot_extra_loop
    call trace_line_finish_write
    or a
    ret nz
    jr trace_dump_records_begin
trace_dump_no_incident:
    call trace_line_reset
    ld de,trace_text_first_none
    call trace_line_append_string
    call trace_line_finish_write
    or a
    ret nz

trace_dump_records_begin:
    ld a,(memman_trace_export + TRACE_EXPORT_COUNT)
    ld (trace_dump_remaining),a
    or a
    ret z
    cp TRACE_RECORD_CAPACITY
    ld a,0
    jr c,trace_dump_index_ready
    ld a,(memman_trace_export + TRACE_EXPORT_WRITE_INDEX)
trace_dump_index_ready:
    ld (trace_dump_index),a
    ld hl,(memman_trace_export + TRACE_EXPORT_SEQUENCE)
    ld a,(trace_dump_remaining)
    dec a
    ld e,a
    ld d,0
    or a
    sbc hl,de
    ld (trace_dump_record_sequence),hl

trace_dump_record_loop:
    ld a,(trace_dump_index)
    ld l,a
    ld h,0
    add hl,hl
    add hl,hl
    add hl,hl
    ld de,memman_trace_export + TRACE_EXPORT_RECORDS
    add hl,de
    ld (trace_dump_record_pointer),hl
    call trace_line_reset
    ld de,trace_text_record_prefix
    call trace_line_append_string
    ld hl,(trace_dump_record_sequence)
    call trace_line_append_hex_word
    ld de,trace_text_record_separator
    call trace_line_append_string
    ld hl,(trace_dump_record_pointer)
    call trace_line_append_record
    call trace_line_finish_write
    or a
    ret nz

    ld hl,(trace_dump_record_sequence)
    inc hl
    ld (trace_dump_record_sequence),hl
    ld a,(trace_dump_index)
    inc a
    cp TRACE_RECORD_CAPACITY
    jr c,trace_dump_record_store_index
    xor a
trace_dump_record_store_index:
    ld (trace_dump_index),a
    ld a,(trace_dump_remaining)
    dec a
    ld (trace_dump_remaining),a
    jr nz,trace_dump_record_loop
    xor a
    ret

; Input HL points to one 8-byte record.
trace_line_append_record:
    push hl
    ld a,(hl)
    call trace_line_append_event_name
    pop hl
    inc hl
    ld de,trace_text_error
    call trace_line_append_string
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    ld de,trace_text_state
    call trace_line_append_string
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    ld de,trace_text_connection
    call trace_line_append_string
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    ld de,trace_text_cleanup
    call trace_line_append_string
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    ld de,trace_text_record_flags
    call trace_line_append_string
    ld a,(hl)
    call trace_line_append_hex_byte
    inc hl
    ld e,(hl)
    inc hl
    ld d,(hl)
    ex de,hl
    push hl
    ld de,trace_text_jiffy
    call trace_line_append_string
    pop hl
    call trace_line_append_hex_word
    ret

trace_line_append_event_name:
    dec a
    cp TRACE_EVENT_AUTO_RELISTEN
    jr nc,trace_line_event_unknown
    add a,a
    ld e,a
    ld d,0
    ld hl,trace_event_name_table
    add hl,de
    ld e,(hl)
    inc hl
    ld d,(hl)
    jp trace_line_append_string
trace_line_event_unknown:
    ld de,trace_event_unknown
    jp trace_line_append_string

trace_line_reset:
    ld hl,trace_line_buffer
    ld (trace_line_cursor),hl
    ret

trace_line_append_string:
    ld a,(de)
    or a
    ret z
    call trace_line_append_char
    inc de
    jr trace_line_append_string

trace_line_append_char:
    push hl
    ld hl,(trace_line_cursor)
    ld (hl),a
    inc hl
    ld (trace_line_cursor),hl
    pop hl
    ret

trace_line_append_hex_word:
    push af
    ld a,h
    call trace_line_append_hex_byte
    ld a,l
    call trace_line_append_hex_byte
    pop af
    ret

trace_line_append_hex_byte:
    push af
    rrca
    rrca
    rrca
    rrca
    call trace_line_append_hex_nibble
    pop af
trace_line_append_hex_nibble:
    and 00Fh
    cp 10
    jr c,trace_line_append_hex_decimal
    add a,'A' - 10 - '0'
trace_line_append_hex_decimal:
    add a,'0'
    jp trace_line_append_char

trace_line_finish_write:
    ld a,13
    call trace_line_append_char
    ld a,10
    call trace_line_append_char
    ld hl,(trace_line_cursor)
    ld de,trace_line_buffer
    or a
    sbc hl,de
    push hl
    ld a,(trace_dump_handle)
    ld b,a
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

trace_event_name_table:
    dw trace_event_enable,trace_event_state,trace_event_state_error
    dw trace_event_drop,trace_event_dos,trace_event_basic
    dw trace_event_open_begin,trace_event_open_end
    dw trace_event_abort_begin,trace_event_abort_end
    dw trace_event_system_suspend,trace_event_system_resume
    dw trace_event_reconfig_begin,trace_event_reconfig_end
    dw trace_event_auto_relisten
trace_event_enable:          db "ENABLE",0
trace_event_state:           db "STATE",0
trace_event_state_error:     db "STATE_ERROR",0
trace_event_drop:            db "DROP",0
trace_event_dos:             db "DOS_RELISTEN",0
trace_event_basic:           db "BASIC_RELISTEN",0
trace_event_open_begin:      db "OPEN_BEGIN",0
trace_event_open_end:        db "OPEN_END",0
trace_event_abort_begin:     db "ABORT_BEGIN",0
trace_event_abort_end:       db "ABORT_END",0
trace_event_system_suspend:  db "SYSTEM_SUSPEND",0
trace_event_system_resume:   db "SYSTEM_RESUME",0
trace_event_reconfig_begin:  db "RECONFIG_BEGIN",0
trace_event_reconfig_end:    db "RECONFIG_END",0
trace_event_auto_relisten:   db "AUTO_RELISTEN",0
trace_event_unknown:         db "UNKNOWN",0
trace_text_title:            db "MSXAI TRACE V1",0
trace_text_status_flags:     db "FLAGS=",0
trace_text_count:            db " COUNT=",0
trace_text_next:             db " NEXT=",0
trace_text_sequence:         db " SEQ=",0
trace_text_polls:            db "POLLS=",0
trace_text_changes:          db " CHANGES=",0
trace_text_timi:             db " TIMI=",0
trace_text_first:            db "FIRST ",0
trace_text_first_none:       db "FIRST NONE",0
trace_text_snapshot_extra:   db " EXTRA=",0
trace_text_record_prefix:    db "#",0
trace_text_record_separator: db " ",0
trace_text_error:            db " E=",0
trace_text_state:            db " S=",0
trace_text_connection:       db " C=",0
trace_text_cleanup:          db " X=",0
trace_text_record_flags:     db " F=",0
trace_text_jiffy:            db " T=",0

trace_dump_success_message:
    db 13,10,"MSXAI trace written; resident log preserved.",13,10,"$"
trace_dump_no_agent_message:
    db 13,10,"MSXAI resident agent is not installed.",13,10,"$"
trace_dump_unavailable_message:
    db 13,10,"Resident trace unavailable; reinstall the matching suite.",13,10,"$"
trace_dump_error_message:
    db 13,10,"MSXAI trace write failed; use a new filename.",13,10,"$"
trace_dump_handle:
    db INVALID_HANDLE
trace_dump_error:
    db 0
trace_dump_remaining:
    db 0
trace_dump_index:
    db 0
trace_dump_record_sequence:
    dw 0
trace_dump_record_pointer:
    dw 0
trace_line_cursor:
    dw trace_line_buffer
trace_line_buffer:
    ds 96,0

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
tu_name:
    db "TU.COM",0
tk_name:
    db "TK.COM",0
mcp8251_tsr_name:
    db "MCP8251.TSR",0
mcp16550_tsr_name:
    db "MCP16550.TSR",0
mcp115k_tsr_name:
    db "MCP115K.TSR",0
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
suite_tu_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_tk_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcp8251_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcp16550_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcp115k_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mcpunapi_tsr_path:
    ds SUITE_PATH_BUFFER_SIZE,0
suite_mp_path:
    ds SUITE_PATH_BUFFER_SIZE,0

; '@' is MemMan's documented representation of Return.  MemMan 2.42 consumes
; the first Return while warm-booting COMMAND2, so the second '@' is required
; before the visible loader command. TL and TU accept a TSR path without an
; extension; TU overlays TL with the same command tail after priming UNAPI.
install_uart_command_prefix:
    ; MemMan consumes this normal command-tail leading blank before injecting
    ; the post-warm-boot commands into COMMAND2.
    db " _SYSTEM@@TL "
install_uart_command_prefix_end:
install_uart_command_prefix_length: equ install_uart_command_prefix_end - install_uart_command_prefix
install_unapi_command_prefix:
    db " _SYSTEM@@TU "
install_unapi_command_prefix_end:
install_unapi_command_prefix_length: equ install_unapi_command_prefix_end - install_unapi_command_prefix
install_port_helper_prefix:
    ; COMMAND2 recognizes slash as the executable/tail delimiter. The following
    ; fixed four hex digits keep `@MP/FFFE@` within the 39-byte payload limit.
    db "MP/"
install_port_helper_prefix_end:
install_port_helper_prefix_length: equ install_port_helper_prefix_end - install_port_helper_prefix
install_command_buffer:
    ds install_unapi_command_prefix_length + SUITE_PATH_MAX + 1 + install_port_helper_prefix_length + 5 + 1,0

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
