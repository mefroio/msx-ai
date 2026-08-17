; MSX TCP/IP UNAPI passive-listener diagnostic probe.
;
; Build with tools/build_unapi_probe.py and run under MSX-DOS as:
;
;   UNAPIPRB [port]
;
; The decimal port is optional (default 6603) and must be in 1..65534.
; UNAPI reserves FFFFh (65535) as the random-local-port sentinel.
; This program is intentionally independent from the MSX-AI resident agent.
; It discovers the first "TCP/IP" UNAPI implementation, reports identity,
; location, address, capabilities and connection-pool information, then opens
; a passive/resident listener and monitors its state.
;
; The UNAPI discovery/call sequence follows the official MSX-UNAPI 1.1
; specification and Konamiman's APILIST example.  All exchange buffers live in
; page 0 because an UNAPI implementation is allowed to reject page-1 buffers.

BDOS:                       equ 00005h
BIOS_RDSLT:                 equ 0000Ch
BIOS_CALSLT:                equ 0001Ch
BIOS_EXTBIO:                equ 0FFCAh
UNAPI_ARGUMENT:             equ 0F847h
UNAPI_EXTBIO_MAGIC:         equ 02222h

BDOS_TERM0:                 equ 00h
BDOS_CONOUT:                equ 02h
BDOS_DIRIO:                 equ 06h
BDOS_STROUT:                equ 09h

UNAPI_GET_INFO:             equ 00h
TCPIP_GET_CAPAB:            equ 01h
TCPIP_GET_IPINFO:           equ 02h
TCPIP_NET_STATE:            equ 03h
TCPIP_TCP_OPEN:             equ 0Dh
TCPIP_TCP_CLOSE:            equ 0Eh
TCPIP_TCP_ABORT:            equ 0Fh
TCPIP_TCP_STATE:            equ 10h
TCPIP_WAIT:                 equ 1Dh

ERR_OK:                     equ 00h
ERR_NOT_IMP:                equ 01h
ERR_NO_CONN:                equ 0Bh
TCP_STATE_ESTABLISHED:      equ 04h
TCP_STATE_CLOSE_WAIT:       equ 07h

DEFAULT_LISTENER_PORT:      equ 019CBh       ; 6603
PASSIVE_RESIDENT_FLAGS:     equ 03h
PASSIVE_ANY_CAPABILITY:     equ 020h         ; block 1, L bit 5
TCP_OPEN_BLOCKING_FEATURE:  equ 008h         ; block 1, D bit 3

        org 00100h

probe_start:
        ld de,msg_banner
        call print_string

        call parse_port_argument
        or a
        jp nz,show_usage_and_exit

        ld de,msg_requested_port
        call print_string
        ld hl,(listener_port)
        call print_decimal16
        call print_crlf

        call discover_tcpip_unapi
        or a
        jp nz,terminate_program

        call query_and_show_identity
        or a
        jp nz,terminate_program

        call query_and_show_network
        call query_and_show_capabilities
        or a
        jp nz,terminate_program

        call open_listener
        or a
        jp nz,terminate_program

        ld a,(ip_error)
        or a
        jr nz,probe_host_hint_unavailable
        ld de,msg_host_hint
        call print_string
        ld hl,local_ip
        call print_ip
        ld a,":"
        call print_char
        ld hl,(listener_port)
        call print_decimal16
        call print_crlf
        jr probe_host_hint_done
probe_host_hint_unavailable:
        ld de,msg_host_hint_unavailable
        call print_string
        ld hl,(listener_port)
        call print_decimal16
        call print_crlf
probe_host_hint_done:
        call show_key_help

probe_loop:
        ; A third-party implementation may return with IRQs masked. Re-enable
        ; them explicitly so HALT remains a one-tick pacing primitive.
        ei
        halt
        ld a,TCPIP_WAIT
        call execute_unapi
        call poll_tcp_state
        call poll_keyboard
        jp probe_loop


; ---------------------------------------------------------------------------
; Command line
; ---------------------------------------------------------------------------

; Parse one optional decimal positional argument from the DOS command tail.
; Returns A=0 on success, A=1 on invalid input.  The pre-multiply comparison
; against 6553, plus the final digit check, prevents 16-bit wraparound.
parse_port_argument:
        ld hl,DEFAULT_LISTENER_PORT
        ld (listener_port),hl
        ld (tcp_open_params+6),hl

        ; Bound every scan by the authoritative DOS command-tail length. A
        ; 127-byte tail ends exactly at 0100h, where this COM image begins, so
        ; reading until an assumed CR would otherwise consume program bytes.
        ld a,(00080h)
        ld (command_tail_remaining),a
        ld ix,00081h
parse_port_skip_leading:
        ld a,(command_tail_remaining)
        or a
        jr z,parse_port_default
        ld a,(ix+0)
        cp " "
        jr z,parse_port_leading_advance
        cp 9
        jr z,parse_port_leading_advance
        or a
        jr z,parse_port_default
        cp 13
        jr z,parse_port_default
        jr parse_port_digits_start

parse_port_leading_advance:
        inc ix
        ld a,(command_tail_remaining)
        dec a
        ld (command_tail_remaining),a
        jr parse_port_skip_leading

parse_port_default:
        xor a
        ret

parse_port_digits_start:
        ld hl,0
        ld b,0

parse_port_digit_loop:
        ld a,(command_tail_remaining)
        or a
        jr z,parse_port_digits_done
        ld a,(ix+0)
        cp "0"
        jr c,parse_port_digits_done
        cp "9"+1
        jr nc,parse_port_digits_done
        sub "0"
        ld c,a

        push hl
        ld de,6553
        or a
        sbc hl,de
        pop hl
        jr c,parse_port_multiply
        jr nz,parse_port_invalid
        ld a,c
        cp 5
        jr nc,parse_port_invalid

parse_port_multiply:
        push bc
        ld d,h
        ld e,l
        add hl,hl
        add hl,hl
        add hl,de
        add hl,hl
        pop bc
        ld a,l
        add a,c
        ld l,a
        jr nc,parse_port_digit_added
        inc h
parse_port_digit_added:
        ld b,1
        inc ix
        ld a,(command_tail_remaining)
        dec a
        ld (command_tail_remaining),a
        jr parse_port_digit_loop

parse_port_digits_done:
        ld a,b
        or a
        jr z,parse_port_invalid
        ld a,h
        or l
        jr z,parse_port_invalid

parse_port_skip_trailing:
        ld a,(command_tail_remaining)
        or a
        jr z,parse_port_valid
        ld a,(ix+0)
        cp " "
        jr z,parse_port_trailing_advance
        cp 9
        jr z,parse_port_trailing_advance
        or a
        jr z,parse_port_valid
        cp 13
        jr z,parse_port_valid
        jr parse_port_invalid

parse_port_trailing_advance:
        inc ix
        ld a,(command_tail_remaining)
        dec a
        ld (command_tail_remaining),a
        jr parse_port_skip_trailing

parse_port_valid:
        ld (listener_port),hl
        ld (tcp_open_params+6),hl
        xor a
        ret

parse_port_invalid:
        ld a,1
        ret

show_usage_and_exit:
        ld de,msg_usage
        call print_string
        jp terminate_program


; ---------------------------------------------------------------------------
; UNAPI discovery and generic calls
; ---------------------------------------------------------------------------

discover_tcpip_unapi:
        call copy_tcpip_api_id

        ld de,UNAPI_EXTBIO_MAGIC
        xor a
        ld b,0
        call BIOS_EXTBIO
        ld a,b
        ld (implementation_count),a

        ld de,msg_implementations
        call print_string
        ld a,(implementation_count)
        call print_decimal8
        call print_crlf

        ld a,(implementation_count)
        or a
        jr nz,discover_get_helper
        ld de,msg_no_implementation
        call print_string
        ld a,1
        ret

copy_tcpip_api_id:
        ld hl,api_id
        ld de,UNAPI_ARGUMENT
        ld bc,api_id_end-api_id
        ldir
        ret

discover_get_helper:
        ld de,UNAPI_EXTBIO_MAGIC
        ld hl,0
        ld a,0FFh
        call BIOS_EXTBIO
        ld (ram_helper),hl

        ld a,1
        ld (implementation_index),a
        call select_compatible_implementation
        ret

; Enumerate all implementations returned for "TCP/IP" and retain the first
; one that can be called, reports specification >=1.0 and advertises passive
; TCP with an unspecified peer (GET_CAPAB block 1, HL bit 5).
select_compatible_implementation:
select_implementation_loop:
        ; API calls are allowed to use the BIOS ARG area. Restore the identifier
        ; before every EXTBIO enumeration request as required by UNAPI.
        call copy_tcpip_api_id
        ld de,UNAPI_EXTBIO_MAGIC
        ld a,(implementation_index)
        call BIOS_EXTBIO
        ld (implementation_entry),hl
        ld (implementation_slot),a
        ld a,b
        ld (implementation_segment),a

        call implementation_is_callable
        or a
        jr nz,select_reject_no_helper

        xor a
        call execute_unapi
        ld (get_info_error),a
        or a
        jr nz,select_reject_get_info
        ld (implementation_name),hl
        ld a,d
        ld (specification_major),a
        ld a,e
        ld (specification_minor),a
        ld a,b
        ld (implementation_major),a
        ld a,c
        ld (implementation_minor),a
        ld a,(specification_major)
        or a
        jr z,select_reject_specification

        ld b,1
        ld a,TCPIP_GET_CAPAB
        call execute_unapi
        ld (capabilities_error),a
        ld (capabilities_flags),hl
        ld (features_flags),de
        ld a,b
        ld (link_protocol),a
        ld a,(capabilities_error)
        or a
        jr nz,select_reject_capability
        ld a,(capabilities_flags)
        and PASSIVE_ANY_CAPABILITY
        jr z,select_reject_capability

        ld de,msg_selected_candidate
        call print_string
        ld a,(implementation_index)
        call print_decimal8
        call print_crlf
        call show_implementation_location
        xor a
        ret

select_reject_no_helper:
        ld de,msg_candidate_prefix
        call print_string
        ld a,(implementation_index)
        call print_decimal8
        ld de,msg_candidate_no_helper
        call print_string
        jr select_next_implementation

select_reject_get_info:
        ld de,msg_candidate_prefix
        call print_string
        ld a,(implementation_index)
        call print_decimal8
        ld de,msg_candidate_get_info
        call print_string
        jr select_next_implementation

select_reject_specification:
        ld de,msg_candidate_prefix
        call print_string
        ld a,(implementation_index)
        call print_decimal8
        ld de,msg_candidate_bad_spec
        call print_string
        jr select_next_implementation

select_reject_capability:
        ld de,msg_candidate_prefix
        call print_string
        ld a,(implementation_index)
        call print_decimal8
        ld de,msg_candidate_no_passive
        call print_string

select_next_implementation:
        ld a,(implementation_index)
        ld b,a
        ld a,(implementation_count)
        cp b
        jr z,select_no_compatible_implementation
        ld a,b
        inc a
        ld (implementation_index),a
        jp select_implementation_loop

select_no_compatible_implementation:
        ld de,msg_no_compatible_implementation
        call print_string
        ld a,1
        ret

; Return A=0 for page-3, ROM and callable RAM implementations; A=1 only
; when the candidate is a RAM implementation but EXTBIO returned no helper.
implementation_is_callable:
        ld a,(implementation_entry+1)
        cp 0C0h
        jr nc,implementation_callable
        ld a,(implementation_segment)
        cp 0FFh
        jr z,implementation_callable
        ld a,(implementation_entry+1)
        cp 040h
        jr c,implementation_not_callable
        cp 080h
        jr nc,implementation_not_callable
        ld hl,(ram_helper)
        ld a,h
        or l
        jr z,implementation_not_callable
implementation_callable:
        xor a
        ret
implementation_not_callable:
        ld a,1
        ret

; Execute A=function on the selected implementation, preserving all other
; input registers until control reaches the implementation entry point.
execute_unapi:
        push af
        ld a,(implementation_entry+1)
        cp 0C0h
        jr c,execute_unapi_slotted
        ld ix,(implementation_entry)
        pop af
        jp (ix)

execute_unapi_slotted:
        ld a,(implementation_segment)
        cp 0FFh
        jr nz,execute_unapi_ram

        ld a,(implementation_slot)
        ld (call_slot_pair+1),a
        ld iy,(call_slot_pair)
        ld ix,(implementation_entry)
        pop af
        jp BIOS_CALSLT

execute_unapi_ram:
        ld (call_slot_pair),a
        ld a,(implementation_slot)
        ld (call_slot_pair+1),a
        ld iy,(call_slot_pair)
        push hl
        ld hl,(ram_helper)
        ld a,h
        or l
        pop hl
        jr nz,execute_unapi_ram_helper_ready
        pop af
        ld a,ERR_NOT_IMP
        ret
execute_unapi_ram_helper_ready:
        pop af
        ld ix,(ram_helper)
        push ix
        ld ix,(implementation_entry)
        ret

; Read A=(HL) from the implementation's page 3, ROM slot, or RAM segment.
read_implementation_byte:
        ld a,h
        cp 0C0h
        jr nc,read_implementation_direct

        ld a,(implementation_segment)
        cp 0FFh
        jr nz,read_implementation_ram
        ld a,(implementation_slot)
        jp BIOS_RDSLT

read_implementation_ram:
        ld b,a
        ld de,(ram_helper)
        ld a,d
        or e
        jr z,read_implementation_unavailable
        ld a,(implementation_slot)
        ld ix,(ram_helper)
        inc ix
        inc ix
        inc ix
        jp (ix)

read_implementation_unavailable:
        xor a
        ret

read_implementation_direct:
        ld a,(hl)
        ret


; ---------------------------------------------------------------------------
; Identity, network and capabilities
; ---------------------------------------------------------------------------

query_and_show_identity:
        xor a
        call execute_unapi
        ld (get_info_error),a
        ld (implementation_name),hl
        ld a,d
        ld (specification_major),a
        ld a,e
        ld (specification_minor),a
        ld a,b
        ld (implementation_major),a
        ld a,c
        ld (implementation_minor),a

        ld de,msg_implementation_name
        call print_string
        call print_implementation_name
        call print_crlf

        ld de,msg_api_version
        call print_string
        ld a,(specification_major)
        call print_decimal8
        ld a,"."
        call print_char
        ld a,(specification_minor)
        call print_decimal8
        ld de,msg_impl_version
        call print_string
        ld a,(implementation_major)
        call print_decimal8
        ld a,"."
        call print_char
        ld a,(implementation_minor)
        call print_decimal8
        call print_crlf

        ld a,(specification_major)
        or a
        jr nz,identity_supported
        ld de,msg_bad_specification
        call print_string
        ld a,1
        ret
identity_supported:
        xor a
        ret

show_implementation_location:
        ld de,msg_location_slot
        call print_string
        ld a,(implementation_slot)
        call print_hex8
        ld de,msg_location_segment
        call print_string
        ld a,(implementation_segment)
        call print_hex8
        ld de,msg_location_entry
        call print_string
        ld hl,(implementation_entry)
        call print_hex16
        ld de,msg_location_mode
        call print_string

        ld a,(implementation_entry+1)
        cp 0C0h
        jr nc,show_location_page3
        ld a,(implementation_segment)
        cp 0FFh
        jr z,show_location_rom
        ld de,msg_mode_ram
        call print_string
        ret
show_location_rom:
        ld de,msg_mode_rom
        call print_string
        ret
show_location_page3:
        ld de,msg_mode_page3
        call print_string
        ret

print_implementation_name:
        ld hl,(implementation_name)
        ld c,63
print_implementation_name_loop:
        push hl
        push bc
        call read_implementation_byte
        pop bc
        pop hl
        or a
        ret z
        call print_char
        inc hl
        dec c
        jr nz,print_implementation_name_loop
        ret

query_and_show_network:
        ld a,TCPIP_NET_STATE
        call execute_unapi
        ld (network_error),a
        ld a,b
        ld (network_state),a

        ld b,1
        ld a,TCPIP_GET_IPINFO
        call execute_unapi
        ld (ip_error),a
        ld (local_ip),hl
        ld (local_ip+2),de

        ld de,msg_network_state
        call print_string
        ld a,(network_error)
        call print_error_code
        ld de,msg_value
        call print_string
        ld a,(network_state)
        call print_hex8
        ld de,msg_open_paren
        call print_string
        ld a,(network_state)
        call print_network_state_name
        ld de,msg_close_paren_crlf
        call print_string

        ld de,msg_local_ip
        call print_string
        ld a,(ip_error)
        or a
        jr nz,show_ip_error
        ld hl,local_ip
        call print_ip
        call print_crlf
        ret
show_ip_error:
        call print_error_code
        call print_crlf
        ret

query_and_show_capabilities:
        ld b,1
        ld a,TCPIP_GET_CAPAB
        call execute_unapi
        ld (capabilities_error),a
        ld (capabilities_flags),hl
        ld (features_flags),de
        ld a,b
        ld (link_protocol),a

        ld de,msg_capabilities
        call print_string
        ld a,(capabilities_error)
        call print_error_code
        ld a,(capabilities_error)
        or a
        jp nz,capabilities_unavailable

        ld de,msg_hl_value
        call print_string
        ld hl,(capabilities_flags)
        call print_hex16
        ld de,msg_de_value
        call print_string
        ld hl,(features_flags)
        call print_hex16
        ld de,msg_link_value
        call print_string
        ld a,(link_protocol)
        call print_hex8
        ld de,msg_open_paren
        call print_string
        ld a,(link_protocol)
        call print_link_name
        ld de,msg_close_paren_crlf
        call print_string

        ld de,msg_passive_any
        call print_string
        ld a,(capabilities_flags)
        and PASSIVE_ANY_CAPABILITY
        call print_yes_no
        call print_crlf

        ld de,msg_blocking_open
        call print_string
        ld a,(features_flags+1)
        and TCP_OPEN_BLOCKING_FEATURE
        call print_yes_no
        call print_crlf

        ld b,2
        ld a,TCPIP_GET_CAPAB
        call execute_unapi
        ld (pool_error),a
        ld a,b
        ld (pool_tcp_max),a
        ld a,c
        ld (pool_udp_max),a
        ld a,d
        ld (pool_tcp_free),a
        ld a,e
        ld (pool_udp_free),a
        ld a,h
        ld (pool_raw_max),a
        ld a,l
        ld (pool_raw_free),a

        ld de,msg_pool
        call print_string
        ld a,(pool_error)
        call print_error_code
        ld a,(pool_error)
        or a
        jr z,show_pool_values
        call print_crlf
        jr capabilities_check_required
show_pool_values:
        ld de,msg_pool_tcp
        call print_string
        ld a,(pool_tcp_max)
        call print_decimal8
        ld a,"/"
        call print_char
        ld a,(pool_tcp_free)
        call print_decimal8
        ld de,msg_pool_udp
        call print_string
        ld a,(pool_udp_max)
        call print_decimal8
        ld a,"/"
        call print_char
        ld a,(pool_udp_free)
        call print_decimal8
        ld de,msg_pool_raw
        call print_string
        ld a,(pool_raw_max)
        call print_decimal8
        ld a,"/"
        call print_char
        ld a,(pool_raw_free)
        call print_decimal8
        call print_crlf
        jr capabilities_check_required

capabilities_unavailable:
        call print_crlf
        ld de,msg_capability_query_failed
        call print_string
        ld a,1
        ret

capabilities_check_required:
        ld a,(capabilities_flags)
        and PASSIVE_ANY_CAPABILITY
        jr nz,capabilities_supported
        ld de,msg_passive_not_supported
        call print_string
        ld a,1
        ret
capabilities_supported:
        xor a
        ret


; ---------------------------------------------------------------------------
; Passive TCP listener
; ---------------------------------------------------------------------------

open_listener:
        xor a
        ld (socket_handle),a
        ld (last_state_error),a
        dec a
        ld (last_state_error),a
        ld (last_state_code),a

        ld de,msg_opening_listener
        call print_string
        ld hl,(listener_port)
        call print_decimal16
        ld de,msg_opening_flags
        call print_string
        ld a,PASSIVE_RESIDENT_FLAGS
        call print_hex8
        call print_crlf

        ld hl,tcp_open_params
        ld a,TCPIP_TCP_OPEN
        call execute_unapi
        ld (tcp_open_error),a
        ld a,b
        ld (tcp_open_return_handle),a

        ld de,msg_tcp_open_result
        call print_string
        ld a,(tcp_open_error)
        call print_error_code
        ld de,msg_handle_value
        call print_string
        ld a,(tcp_open_return_handle)
        call print_hex8
        call print_crlf

        ld a,(tcp_open_error)
        or a
        ret nz
        ld a,(tcp_open_return_handle)
        or a
        jr nz,open_listener_valid_handle
        ld de,msg_zero_handle
        call print_string
        ld a,1
        ret

open_listener_valid_handle:
        ld (socket_handle),a
        cp 1
        jr z,open_listener_snapshot
        ld de,msg_nonfirst_handle
        call print_string

open_listener_snapshot:
        call fetch_tcp_state
        call remember_and_show_state
        xor a
        ret

abort_listener:
        ld a,(socket_handle)
        or a
        ret z
        ld (abort_requested_handle),a
        ld b,a
        ld a,TCPIP_TCP_ABORT
        call execute_unapi
        ld (abort_error),a

        ld de,msg_tcp_abort_result
        call print_string
        ld a,(abort_error)
        call print_error_code
        ld de,msg_handle_value
        call print_string
        ld a,(abort_requested_handle)
        call print_hex8
        call print_crlf

        ld a,(abort_error)
        or a
        jr z,abort_listener_forget
        cp ERR_NO_CONN
        ret nz
abort_listener_forget:
        xor a
        ld (socket_handle),a
        ret

fetch_tcp_state:
        ld a,(socket_handle)
        or a
        ret z
        ld b,a
        ld hl,tcp_info
        ld a,TCPIP_TCP_STATE
        call execute_unapi
        ld (tcp_state_error),a
        ld a,b
        ld (tcp_state_code),a
        ld a,c
        ld (tcp_state_aux),a
        ld (tcp_state_rx),hl
        ld (tcp_state_urgent),de
        ld (tcp_state_tx_free),ix
        ret

poll_tcp_state:
        ld a,(socket_handle)
        or a
        ret z
        call fetch_tcp_state
        ld a,(tcp_state_error)
        ld b,a
        ld a,(last_state_error)
        cp b
        jr nz,remember_and_show_state
        ld a,b
        or a
        ret nz
        ld a,(tcp_state_code)
        ld b,a
        ld a,(last_state_code)
        cp b
        ret z

remember_and_show_state:
        ld a,(tcp_state_error)
        ld (last_state_error),a
        ld a,(tcp_state_code)
        ld (last_state_code),a
        jp show_tcp_state

show_tcp_state:
        ld de,msg_state_prefix
        call print_string
        ld a,(socket_handle)
        call print_hex8
        ld de,msg_error_value
        call print_string
        ld a,(tcp_state_error)
        call print_error_code

        ld a,(tcp_state_error)
        or a
        jr z,show_tcp_state_ok
        cp ERR_NO_CONN
        jr nz,show_tcp_state_error_end
        ld de,msg_close_reason
        call print_string
        ld a,(tcp_state_aux)
        call print_hex8
show_tcp_state_error_end:
        call print_crlf
        ret

show_tcp_state_ok:
        ld de,msg_state_value
        call print_string
        ld a,(tcp_state_code)
        call print_hex8
        ld de,msg_open_paren
        call print_string
        ld a,(tcp_state_code)
        call print_tcp_state_name
        ld de,msg_close_paren
        call print_string

        ld de,msg_rx_value
        call print_string
        ld hl,(tcp_state_rx)
        call print_hex16
        ld de,msg_urgent_value
        call print_string
        ld hl,(tcp_state_urgent)
        call print_hex16
        ld de,msg_tx_free_value
        call print_string
        ld hl,(tcp_state_tx_free)
        call print_hex16

        ld a,(tcp_state_code)
        cp TCP_STATE_ESTABLISHED
        jr z,show_tcp_state_flags
        cp TCP_STATE_CLOSE_WAIT
        jr nz,show_tcp_state_socket
show_tcp_state_flags:
        ld de,msg_flags_value
        call print_string
        ld a,(tcp_state_aux)
        call print_hex8

show_tcp_state_socket:
        call print_crlf
        ld de,msg_socket_prefix
        call print_string
        ld hl,tcp_info
        call print_ip
        ld a,":"
        call print_char
        ld hl,(tcp_info+4)
        call print_decimal16
        ld de,msg_socket_arrow
        call print_string
        ld hl,(tcp_info+6)
        call print_decimal16
        call print_crlf
        ret


; ---------------------------------------------------------------------------
; Interactive diagnostics
; ---------------------------------------------------------------------------

poll_keyboard:
        ld e,0FFh
        ld c,BDOS_DIRIO
        call BDOS
        or a
        ret z
        cp 27
        jp z,quit_probe
        cp 3
        jp z,quit_probe
        cp "a"
        jr c,poll_keyboard_upper
        cp "z"+1
        jr nc,poll_keyboard_upper
        and 0DFh
poll_keyboard_upper:
        cp "Q"
        jp z,quit_probe
        cp "S"
        jr z,key_snapshot
        cp "R"
        jr z,key_reopen
        cp "?"
        jr z,key_help
        ret

key_snapshot:
        ld a,(socket_handle)
        or a
        jr z,key_snapshot_no_handle
        call fetch_tcp_state
        jp remember_and_show_state
key_snapshot_no_handle:
        ld de,msg_no_active_handle
        call print_string
        ret

key_reopen:
        call abort_listener
        ld a,(socket_handle)
        or a
        jr nz,key_reopen_failed_abort
        call open_listener
        ret
key_reopen_failed_abort:
        ld de,msg_abort_failed_reopen
        call print_string
        ret

key_help:
        call show_key_help
        ret

show_key_help:
        ld de,msg_keys
        call print_string
        ret

quit_probe:
        call abort_listener
        ld a,(socket_handle)
        or a
        jr z,quit_probe_clean
        ld de,msg_listener_may_remain
        call print_string
quit_probe_clean:
        ld de,msg_done
        call print_string
        jp terminate_program

terminate_program:
        ld c,BDOS_TERM0
        jp BDOS


; ---------------------------------------------------------------------------
; Formatting helpers
; ---------------------------------------------------------------------------

print_string:
        push af
        push bc
        push de
        push hl
        push ix
        push iy
        ld c,BDOS_STROUT
        call BDOS
        pop iy
        pop ix
        pop hl
        pop de
        pop bc
        pop af
        ret

print_char:
        push af
        push bc
        push de
        push hl
        push ix
        push iy
        ld e,a
        ld c,BDOS_CONOUT
        call BDOS
        pop iy
        pop ix
        pop hl
        pop de
        pop bc
        pop af
        ret

print_crlf:
        ld de,msg_crlf
        jp print_string

print_hex8:
        push af
        push af
        rrca
        rrca
        rrca
        rrca
        and 00Fh
        call print_hex_nibble
        pop af
        and 00Fh
        call print_hex_nibble
        pop af
        ret

print_hex_nibble:
        cp 10
        jr c,print_hex_nibble_digit
        add a,"A"-10
        jp print_char
print_hex_nibble_digit:
        add a,"0"
        jp print_char

print_hex16:
        push af
        push hl
        ld a,h
        call print_hex8
        ld a,l
        call print_hex8
        pop hl
        pop af
        ret

print_decimal8:
        push hl
        ld l,a
        ld h,0
        call print_decimal16
        pop hl
        ret

print_decimal16:
        push af
        push bc
        push de
        push hl
        xor a
        ld (decimal_started),a
        ld de,10000
        call print_decimal_place
        ld de,1000
        call print_decimal_place
        ld de,100
        call print_decimal_place
        ld de,10
        call print_decimal_place
        ld a,l
        add a,"0"
        call print_char
        pop hl
        pop de
        pop bc
        pop af
        ret

print_decimal_place:
        ld b,"0"
print_decimal_place_loop:
        or a
        sbc hl,de
        jr c,print_decimal_place_done
        inc b
        jr print_decimal_place_loop
print_decimal_place_done:
        add hl,de
        ld a,b
        cp "0"
        jr nz,print_decimal_place_emit
        ld a,(decimal_started)
        or a
        ret z
        ld a,b
print_decimal_place_emit:
        push af
        ld a,1
        ld (decimal_started),a
        pop af
        jp print_char

print_ip:
        push af
        push bc
        push hl
        ld b,4
print_ip_loop:
        ld a,(hl)
        call print_decimal8
        inc hl
        dec b
        jr z,print_ip_done
        ld a,"."
        call print_char
        jr print_ip_loop
print_ip_done:
        pop hl
        pop bc
        pop af
        ret

print_yes_no:
        or a
        ld de,msg_no
        jr z,print_yes_no_emit
        ld de,msg_yes
print_yes_no_emit:
        jp print_string

print_error_code:
        push af
        call print_hex8
        ld de,msg_open_paren
        call print_string
        pop af
        cp 16
        jr nc,print_error_implementation
        ld l,a
        ld h,0
        add hl,hl
        ld de,error_name_table
        add hl,de
        ld e,(hl)
        inc hl
        ld d,(hl)
        call print_string
        jr print_error_done
print_error_implementation:
        ld de,msg_error_impl
        call print_string
print_error_done:
        ld de,msg_close_paren
        jp print_string

print_network_state_name:
        cp 0FFh
        jr z,print_network_unknown
        cp 4
        jr nc,print_network_unknown
        ld l,a
        ld h,0
        add hl,hl
        ld de,network_state_table
        add hl,de
        ld e,(hl)
        inc hl
        ld d,(hl)
        jp print_string
print_network_unknown:
        ld de,msg_unknown
        jp print_string

print_tcp_state_name:
        cp 11
        jr nc,print_tcp_unknown
        ld l,a
        ld h,0
        add hl,hl
        ld de,tcp_state_table
        add hl,de
        ld e,(hl)
        inc hl
        ld d,(hl)
        jp print_string
print_tcp_unknown:
        ld de,msg_unknown
        jp print_string

print_link_name:
        cp 4
        jr z,print_link_wifi
        cp 3
        jr z,print_link_ethernet
        cp 2
        jr z,print_link_ppp
        cp 1
        jr z,print_link_slip
        ld de,msg_link_other
        jp print_string
print_link_wifi:
        ld de,msg_link_wifi
        jp print_string
print_link_ethernet:
        ld de,msg_link_ethernet
        jp print_string
print_link_ppp:
        ld de,msg_link_ppp
        jp print_string
print_link_slip:
        ld de,msg_link_slip
        jp print_string


; ---------------------------------------------------------------------------
; Mutable state and UNAPI parameter blocks (all below 4000h)
; ---------------------------------------------------------------------------

api_id:
        db "TCP/IP",0
api_id_end:

; TCPIP_TCP_OPEN block: unspecified peer, selected local port, default timeout,
; passive+resident, no TLS host-name pointer.  Runtime parsing changes only +6.
tcp_open_params:
        db 0,0,0,0
        dw 0
        dw DEFAULT_LISTENER_PORT
        dw 0
        db PASSIVE_RESIDENT_FLAGS
        dw 0
tcp_open_params_end:

implementation_count:       db 0
implementation_index:       db 0
implementation_slot:        db 0
implementation_segment:     db 0
implementation_entry:       dw 0
ram_helper:                  dw 0
call_slot_pair:              dw 0
implementation_name:        dw 0
get_info_error:              db 0
specification_major:         db 0
specification_minor:         db 0
implementation_major:       db 0
implementation_minor:       db 0

network_error:               db 0
network_state:               db 0
ip_error:                    db 0
local_ip:                    ds 4

capabilities_error:          db 0
capabilities_flags:          dw 0
features_flags:              dw 0
link_protocol:               db 0
pool_error:                  db 0
pool_tcp_max:                db 0
pool_udp_max:                db 0
pool_tcp_free:               db 0
pool_udp_free:               db 0
pool_raw_max:                db 0
pool_raw_free:               db 0

listener_port:               dw DEFAULT_LISTENER_PORT
socket_handle:               db 0
tcp_open_error:              db 0
tcp_open_return_handle:      db 0
abort_error:                 db 0
abort_requested_handle:      db 0

tcp_state_error:             db 0
tcp_state_code:              db 0
tcp_state_aux:               db 0
tcp_state_rx:                dw 0
tcp_state_urgent:            dw 0
tcp_state_tx_free:           dw 0
last_state_error:            db 0FFh
last_state_code:             db 0FFh
tcp_info:                    ds 8
decimal_started:             db 0
command_tail_remaining:      db 0


; ---------------------------------------------------------------------------
; Text and lookup tables
; ---------------------------------------------------------------------------

msg_banner:
        db 13,10,"MSX TCP/IP UNAPI passive probe 1.0",13,10,"$"
msg_usage:
        db "Usage: UNAPIPRB [port]",13,10
        db "port must be decimal, 1..65534 (default 6603).",13,10,"$"
msg_requested_port:          db "Listener port: $"
msg_implementations:         db "TCP/IP implementations: $"
msg_no_implementation:       db "ERROR: TCP/IP UNAPI not found.",13,10,"$"
msg_selected_candidate:      db "Selected compatible candidate #$"
msg_candidate_prefix:        db "Candidate #$"
msg_candidate_no_helper:     db ": RAM helper unavailable; skipped.",13,10,"$"
msg_candidate_get_info:      db ": GET_INFO failed; skipped.",13,10,"$"
msg_candidate_bad_spec:      db ": TCP/IP specification < 1.0; skipped.",13,10,"$"
msg_candidate_no_passive:    db ": passive-any capability absent; skipped.",13,10,"$"
msg_no_compatible_implementation:
        db "ERROR: no compatible passive TCP/IP implementation.",13,10,"$"
msg_location_slot:           db "Location: slot=0x$"
msg_location_segment:        db " segment=0x$"
msg_location_entry:          db " entry=0x$"
msg_location_mode:           db " mode=$"
msg_mode_ram:                db "RAM",13,10,"$"
msg_mode_rom:                db "ROM",13,10,"$"
msg_mode_page3:              db "page3",13,10,"$"
msg_implementation_name:     db "Implementation: $"
msg_api_version:             db "TCP/IP spec: $"
msg_impl_version:            db "  implementation: $"
msg_bad_specification:       db "ERROR: TCP/IP UNAPI specification < 1.0.",13,10,"$"
msg_network_state:           db "NET_STATE: err=0x$"
msg_value:                   db " value=0x$"
msg_local_ip:                db "Local IP: $"
msg_capabilities:            db "GET_CAPAB[1]: err=0x$"
msg_hl_value:                db " HL=0x$"
msg_de_value:                db " DE=0x$"
msg_link_value:              db " link=0x$"
msg_passive_any:             db "Passive unspecified peer (HL bit 5): $"
msg_blocking_open:           db "TCP_OPEN blocking flag (DE bit 11): $"
msg_pool:                    db "GET_CAPAB[2]: err=0x$"
msg_pool_tcp:                db " TCP max/free=$"
msg_pool_udp:                db " UDP max/free=$"
msg_pool_raw:                db " RAW max/free=$"
msg_capability_query_failed: db "ERROR: cannot query capabilities.",13,10,"$"
msg_passive_not_supported:   db "ERROR: passive unspecified-peer TCP is unsupported.",13,10,"$"
msg_opening_listener:        db "TCP_OPEN passive/resident port=$"
msg_opening_flags:           db " flags=0x$"
msg_tcp_open_result:         db "TCP_OPEN: err=0x$"
msg_tcp_abort_result:        db "TCP_ABORT: err=0x$"
msg_handle_value:            db " handle=0x$"
msg_zero_handle:             db "ERROR: TCP_OPEN returned handle zero.",13,10,"$"
msg_nonfirst_handle:         db "INFO: connection pool returned a non-first handle.",13,10,"$"
msg_host_hint:               db "Connect a host TCP client to $"
msg_host_hint_unavailable:   db "Local IP unavailable; connect to the MSX address on port $"
msg_state_prefix:            db "TCP_STATE handle=0x$"
msg_error_value:             db " err=0x$"
msg_state_value:             db " state=0x$"
msg_rx_value:                db " rx=0x$"
msg_urgent_value:            db " urgent=0x$"
msg_tx_free_value:           db " txfree=0x$"
msg_flags_value:             db " flags=0x$"
msg_close_reason:            db " close-reason=0x$"
msg_socket_prefix:           db "  peer=$"
msg_socket_arrow:            db " -> local :$"
msg_no_active_handle:        db "No active listener handle; press R to retry.",13,10,"$"
msg_abort_failed_reopen:     db "TCP_ABORT failed; listener was not duplicated.",13,10,"$"
msg_listener_may_remain:     db "WARNING: resident listener may still be open.",13,10,"$"
msg_keys:                    db "Keys: S=snapshot  R=abort/reopen  Q/ESC=abort/quit  ?=help",13,10,"$"
msg_done:                    db "Probe finished.",13,10,"$"

msg_open_paren:              db " ($"
msg_close_paren:             db ")$"
msg_close_paren_crlf:        db ")",13,10,"$"
msg_crlf:                    db 13,10,"$"
msg_yes:                     db "YES$"
msg_no:                      db "NO$"
msg_unknown:                 db "UNKNOWN$"

msg_error_ok:                db "OK$"
msg_error_not_imp:           db "NOT_IMP$"
msg_error_no_network:        db "NO_NETWORK$"
msg_error_no_data:           db "NO_DATA$"
msg_error_inv_param:         db "INV_PARAM$"
msg_error_query_exists:      db "QUERY_EXISTS$"
msg_error_inv_ip:            db "INV_IP$"
msg_error_no_dns:            db "NO_DNS$"
msg_error_dns:               db "DNS$"
msg_error_no_free_conn:      db "NO_FREE_CONN$"
msg_error_conn_exists:       db "CONN_EXISTS$"
msg_error_no_conn:           db "NO_CONN$"
msg_error_conn_state:        db "CONN_STATE$"
msg_error_buffer:            db "BUFFER$"
msg_error_large_dgram:       db "LARGE_DGRAM$"
msg_error_inv_oper:          db "INV_OPER$"
msg_error_impl:              db "IMPLEMENTATION_SPECIFIC$"

error_name_table:
        dw msg_error_ok
        dw msg_error_not_imp
        dw msg_error_no_network
        dw msg_error_no_data
        dw msg_error_inv_param
        dw msg_error_query_exists
        dw msg_error_inv_ip
        dw msg_error_no_dns
        dw msg_error_dns
        dw msg_error_no_free_conn
        dw msg_error_conn_exists
        dw msg_error_no_conn
        dw msg_error_conn_state
        dw msg_error_buffer
        dw msg_error_large_dgram
        dw msg_error_inv_oper

msg_net_closed:              db "CLOSED$"
msg_net_opening:             db "OPENING$"
msg_net_open:                db "OPEN$"
msg_net_closing:             db "CLOSING$"
network_state_table:
        dw msg_net_closed
        dw msg_net_opening
        dw msg_net_open
        dw msg_net_closing

msg_tcp_unknown:             db "UNKNOWN$"
msg_tcp_listen:              db "LISTEN$"
msg_tcp_syn_sent:            db "SYN-SENT$"
msg_tcp_syn_received:        db "SYN-RECEIVED$"
msg_tcp_established:         db "ESTABLISHED$"
msg_tcp_fin_wait_1:          db "FIN-WAIT-1$"
msg_tcp_fin_wait_2:          db "FIN-WAIT-2$"
msg_tcp_close_wait:          db "CLOSE-WAIT$"
msg_tcp_closing:             db "CLOSING$"
msg_tcp_last_ack:            db "LAST-ACK$"
msg_tcp_time_wait:           db "TIME-WAIT$"
tcp_state_table:
        dw msg_tcp_unknown
        dw msg_tcp_listen
        dw msg_tcp_syn_sent
        dw msg_tcp_syn_received
        dw msg_tcp_established
        dw msg_tcp_fin_wait_1
        dw msg_tcp_fin_wait_2
        dw msg_tcp_close_wait
        dw msg_tcp_closing
        dw msg_tcp_last_ack
        dw msg_tcp_time_wait

msg_link_other:              db "other$"
msg_link_slip:               db "SLIP$"
msg_link_ppp:                db "PPP$"
msg_link_ethernet:           db "Ethernet$"
msg_link_wifi:               db "WiFi$"

probe_end:
