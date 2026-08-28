import pathlib
import re
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "agent" / "msx_agent_core.asm"
sys.path.insert(0, str(ROOT))


def _section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def _assert_in_order(test: unittest.TestCase, source: str, *needles: str) -> None:
    position = 0
    for needle in needles:
        found = source.find(needle, position)
        test.assertNotEqual(
            found, -1, f"{needle!r} is missing or out of order")
        position = found + len(needle)


def _equ_value(source: str, name: str) -> int:
    match = re.search(
        rf"(?m)^{re.escape(name)}:\s+equ\s+([0-9A-Fa-f]+h|[0-9]+)\b",
        source,
    )
    if match is None:
        raise AssertionError(f"missing numeric equate {name}")
    value = match.group(1)
    return int(value[:-1], 16) if value.endswith("h") else int(value)


class ResidentHeapStackSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CORE.read_text(encoding="utf-8")

    def test_heap_contract_reserves_one_kib_between_two_guards(self):
        self.assertEqual(
            _equ_value(self.source, "MEMMAN_FUNCTION_HANDLER"), 0x4002)
        self.assertEqual(_equ_value(self.source, "H_CHPU"), 0xFDA4)
        self.assertEqual(_equ_value(self.source, "H_CHGE"), 0xFDC2)
        self.assertEqual(_equ_value(self.source, "H_CRUN"), 0xFF20)
        self.assertEqual(_equ_value(self.source, "BASIC_BUF"), 0xF55E)
        self.assertEqual(_equ_value(self.source, "HOOK_MAPPING_COOLDOWN"), 120)
        self.assertEqual(_equ_value(self.source, "MEMMAN_HEAP_ALLOC"), 70)
        self.assertEqual(_equ_value(self.source, "MEMMAN_HEAP_DEALLOC"), 71)

        stack = _equ_value(self.source, "TSR_HEAP_STACK_SIZE")
        guard = _equ_value(self.source, "TSR_HEAP_GUARD_SIZE")
        block = _equ_value(self.source, "TSR_HEAP_BLOCK_SIZE")
        self.assertEqual(stack, 0x400)
        self.assertEqual(guard, 16)
        self.assertEqual(block, stack + 2 * guard)
        self.assertNotEqual(
            _equ_value(self.source, "TSR_HEAP_LOW_GUARD"),
            _equ_value(self.source, "TSR_HEAP_HIGH_GUARD"),
        )

    def test_heap_allocation_derives_bounds_and_fails_on_null(self):
        allocate = _section(
            self.source, "tsr_heap_allocate:", "tsr_heap_release:")
        _assert_in_order(
            self,
            allocate,
            "di",
            "ld hl,TSR_HEAP_BLOCK_SIZE",
            "ld de,MEMMAN_HEAP_ALLOC",
            "call MEMMAN_FUNCTION_HANDLER",
            "di",
            "ld a,h",
            "or l",
            "jr z,tsr_heap_allocate_failed",
            "ld (tsr_heap_base),hl",
            "ld de,TSR_HEAP_GUARD_SIZE",
            "add hl,de",
            "ld (tsr_heap_stack_bottom),hl",
            "ld de,TSR_HEAP_STACK_SIZE",
            "add hl,de",
            "ld (tsr_heap_stack_top),hl",
            "ld (tsr_heap_fault),a",
            "call tsr_heap_fill_guards",
        )
        failed = allocate.split("tsr_heap_allocate_failed:", 1)[1]
        self.assertRegex(failed, r"(?s)^\s*scf\s+ret\s*$")

    def test_heap_release_invalidates_pointers_before_deallocation(self):
        release = _section(
            self.source, "tsr_heap_release:", "tsr_heap_fill_guards:")
        _assert_in_order(
            self,
            release,
            "ld hl,(tsr_heap_base)",
            "ld a,h",
            "or l",
            "ret z",
            "ld (tsr_heap_base),a",
            "ld (tsr_heap_base + 1),a",
            "ld (tsr_heap_stack_bottom),a",
            "ld (tsr_heap_stack_bottom + 1),a",
            "ld (tsr_heap_stack_top),a",
            "ld (tsr_heap_stack_top + 1),a",
            "ld de,MEMMAN_HEAP_DEALLOC",
            "call MEMMAN_FUNCTION_HANDLER",
            "di",
        )
        self.assertEqual(release.count("call MEMMAN_FUNCTION_HANDLER"), 1)

    def test_heap_guards_cover_both_ends_and_missing_heap_is_failure(self):
        fill = _section(
            self.source, "tsr_heap_fill_guards:", "tsr_heap_guards_ok:")
        _assert_in_order(
            self,
            fill,
            "ld hl,(tsr_heap_base)",
            "ld b,TSR_HEAP_GUARD_SIZE",
            "ld a,TSR_HEAP_LOW_GUARD",
            "tsr_heap_fill_low_guard:",
            "djnz tsr_heap_fill_low_guard",
            "ld hl,(tsr_heap_stack_top)",
            "ld b,TSR_HEAP_GUARD_SIZE",
            "ld a,TSR_HEAP_HIGH_GUARD",
            "tsr_heap_fill_high_guard:",
            "djnz tsr_heap_fill_high_guard",
        )

        check = _section(
            self.source,
            "tsr_heap_guards_ok:",
            "; TsrKill calls this only after",
        )
        _assert_in_order(
            self,
            check,
            "ld hl,(tsr_heap_base)",
            "jr z,tsr_heap_guards_missing",
            "ld a,TSR_HEAP_LOW_GUARD",
            "call tsr_heap_guard_check",
            "ret nz",
            "ld hl,(tsr_heap_stack_top)",
            "ld a,TSR_HEAP_HIGH_GUARD",
            "tsr_heap_guard_check:",
            "ld b,TSR_HEAP_GUARD_SIZE",
            "cp (hl)",
            "ret nz",
            "djnz tsr_heap_guard_check_loop",
            "tsr_heap_guards_missing:",
            "ld a,1",
            "or a",
            "ret",
        )

    def test_h_timi_uses_heap_stack_and_restores_saved_memman_frame(self):
        hooks = _section(
            self.source,
            "; ------------------------------------------------------------ BIOS hooks",
            "else\nresident_keyi_hook:",
        )
        timi_entry = _section(
            hooks, "resident_timi_hook:", "; A nested MemMan hook")
        _assert_in_order(
            self,
            timi_entry,
            "push af",
            "ld a,(in_hook)",
            "jr nz,memman_nested_timi_return",
            "ld a,(tsr_heap_fault)",
            "jr nz,memman_nested_timi_return",
            "ld a,1",
            "ld (in_hook),a",
        )

        nested = _section(
            hooks, "memman_nested_keyi_return:",
            "; H.CHPU recognizes the COMMAND2 prompt")
        _assert_in_order(
            self,
            nested,
            "ld a,(unapi_lifecycle_busy)",
            "jr nz,memman_nested_hook_quit",
            "memman_nested_timi_return:",
            "ld a,(hook_system_suspended)",
            "jr nz,memman_nested_hook_quit",
            "ld a,(unapi_lifecycle_busy)",
            "jr z,memman_nested_hook_continue",
            "memman_nested_hook_quit:",
            "ld a,1",
            "memman_nested_hook_continue:",
            "xor a",
        )

        dispatch = _section(
            hooks, "resident_hook_saved_af:", "hook_done:")
        _assert_in_order(
            self,
            dispatch,
            "push bc",
            "push de",
            "push hl",
            "push ix",
            "push iy",
            "ld (hook_context_sp),sp",
            "di",
            "ld sp,(tsr_heap_stack_top)",
            "ld (hook_dispatch_sp),sp",
            "call transport_service",
        )
        for forbidden in (
                "unapi_relisten_foreground", "unapi_open_listener",
                "unapi_abort_current", "transport_restore_foreground_retry"):
            self.assertNotIn(forbidden, dispatch)

        unwind = _section(hooks, "hook_done:", "memman_hook_continue:")
        _assert_in_order(
            self,
            unwind,
            "call transport_session_finalize",
            "di",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "ld sp,(hook_context_sp)",
            "ld (in_hook),a",
            "pop hl",
            "pop de",
            "pop bc",
        )
        self.assertLess(
            unwind.index("ld sp,(hook_context_sp)"),
            unwind.index("pop iy"),
        )

    def test_unapi_hook_debounces_foreground_page_zero_slot_changes(self):
        initialize = _section(
            self.source, "resident_initialize:", "resident_main:")
        _assert_in_order(
            self,
            initialize,
            "call transport_init",
            "or a",
            "ret nz",
            "ld (hook_mapping_state),a",
            "ld (hook_mapping_cooldown),a",
            "ld (hook_system_suspended),a",
        )

        hooks = _section(
            self.source,
            "; ------------------------------------------------------------ BIOS hooks",
            "else\nresident_keyi_hook:",
        )
        dispatch = _section(
            hooks, "hook_poll_transport:", "hook_dispatch_frame:")
        _assert_in_order(
            self,
            dispatch,
            "call hook_transport_mapping_safe",
            "jr nz,hook_done",
            "call transport_service",
            "call transport_session_guard",
            "call transport_rx_ready",
        )

        guard = _section(
            self.source,
            "hook_transport_mapping_safe:",
            "else\nresident_keyi_hook:",
        )
        _assert_in_order(
            self,
            guard,
            "ld a,(active_transport_id)",
            "cp UNAPI_ID",
            "jr nz,hook_transport_mapping_safe_yes",
            "ld a,(hook_system_suspended)",
            "or a",
            "jr nz,hook_transport_mapping_skip",
            "in a,(0A8h)",
            "and 03h",
            "ld b,a",
            "ld a,(hook_mapping_state)",
            "jr z,hook_transport_mapping_learn_candidate",
            "ld a,(hook_safe_page0_primary)",
            "ld (hook_mapping_state),a",
            "hook_transport_mapping_locked:",
            "ld a,(hook_safe_page0_primary)",
            "jr nz,hook_transport_mapping_changed",
            "hook_transport_mapping_quiet:",
            "ld a,(hook_mapping_cooldown)",
            "dec a",
            "ld (hook_mapping_cooldown),a",
            "hook_transport_mapping_safe_yes:",
            "xor a",
            "ret",
            "hook_transport_mapping_learn_candidate:",
            "ld (hook_safe_page0_primary),a",
            "ld (hook_mapping_state),a",
            "hook_transport_mapping_changed:",
            "ld a,HOOK_MAPPING_COOLDOWN",
            "ld (hook_mapping_cooldown),a",
            "hook_transport_mapping_skip:",
            "ld a,1",
            "or a",
            "ret",
        )
        self.assertNotIn("call MEMMAN_FUNCTION_HANDLER", guard)
        self.assertNotRegex(guard, r"(?i)\b(?:in|out)\s+.*\(0?f[cd-e]h\)")

    def test_exact_basic_system_line_latches_only_unapi_then_chains(self):
        hooks = _section(
            self.source,
            "; ------------------------------------------------------------ BIOS hooks",
            "else\nresident_keyi_hook:",
        )
        command = _section(
            hooks, "resident_basic_crunch_hook:",
            "; Return Z when transport work is safe")
        _assert_in_order(
            self,
            command,
            "push af",
            "push bc",
            "push de",
            "push hl",
            "ld a,(in_hook)",
            "push af",
            "ld a,1",
            "ld (in_hook),a",
            "ld a,(active_transport_id)",
            "cp UNAPI_ID",
            "jp nz,resident_basic_crunch_hook_continue",
            "ld a,h",
            "cp 0F5h",
            "ld a,l",
            "cp 05Eh",
            "ld hl,BASIC_BUF",
            "resident_basic_crunch_skip_leading_space:",
            "cp ' '",
            "resident_basic_crunch_match_start:",
            "cp '_'",
            "jr z,resident_basic_crunch_match_extension",
            "ld de,resident_basic_call_word",
            "ld b,4",
            "call resident_basic_crunch_match_word",
            "jr nz,resident_basic_crunch_maybe_relisten",
            "cp ' '",
            "resident_basic_crunch_skip_call_space:",
            "ld de,resident_basic_call_system_word",
            "ld b,6",
            "call resident_basic_crunch_match_word",
            "resident_basic_crunch_match_extension:",
            "ld de,resident_basic_system_word",
            "ld b,7",
            "call resident_basic_crunch_match_word",
            "resident_basic_crunch_match_word:",
            "and 0DFh",
            "ld c,a",
            "ld a,(de)",
            "cp c",
            "ret nz",
            "djnz resident_basic_crunch_match_word",
            "resident_basic_crunch_skip_trailing_space:",
            "or a",
            "jr z,resident_basic_crunch_match_complete",
            "cp ' '",
            "resident_basic_crunch_maybe_relisten:",
            "ld a,(unapi_relisten_pending)",
            "jr z,resident_basic_crunch_hook_continue",
            "ld a,(transport_session_lost)",
            "jr nz,resident_basic_crunch_hook_continue",
            "push ix",
            "push iy",
            "ex af,af'",
            "push af",
            "ex af,af'",
            "exx",
            "push bc",
            "push de",
            "push hl",
            "exx",
            "call resident_console_relisten_unapi",
            "exx",
            "pop hl",
            "pop de",
            "pop bc",
            "exx",
            "ex af,af'",
            "pop af",
            "ex af,af'",
            "pop iy",
            "pop ix",
            "jr resident_basic_crunch_hook_continue",
            "resident_basic_crunch_match_complete:",
            "xor a",
            "ld (hook_prompt_candidate),a",
            "ld (hook_resume_pending),a",
            "ld (hook_resume_this_tick),a",
            "ld (hook_mapping_state),a",
            "ld (hook_mapping_cooldown),a",
            "ld a,1",
            "ld (hook_system_suspended),a",
            "call resident_basic_system_quiesce_unapi",
            "resident_basic_crunch_hook_continue:",
            "pop af",
            "ld (in_hook),a",
            "pop hl",
            "pop de",
            "pop bc",
            "pop af",
            "ex af,af'",
            "xor a",
            "ex af,af'",
            "ret",
            "resident_basic_system_word:",
            'db "_SYSTEM"',
            "resident_basic_call_word:",
            'db "CALL"',
            "resident_basic_call_system_word:",
            'db "SYSTEM"',
        )
        self.assertNotIn("hook_system_match", command)
        _assert_in_order(
            self,
            command,
            "resident_basic_system_quiesce_unapi:",
            "di",
            "ld (hook_system_sp),sp",
            "call tsr_heap_guards_ok",
            "jr z,resident_basic_system_heap_ready",
            "ld (tsr_heap_fault),a",
            "ld a,0FFh",
            "ld (hook_system_restore_error),a",
            "jr resident_basic_system_restore_stack",
            "resident_basic_system_heap_ready:",
            "call tsr_heap_fill_guards",
            "ld sp,(tsr_heap_stack_top)",
            "call transport_restore_foreground_retry",
            "or a",
            "jr nz,resident_basic_system_store_result",
            "call transport_session_reset",
            "call xfer_reconfigure_detach",
            "xor a",
            "resident_basic_system_store_result:",
            "ld (hook_system_restore_error),a",
            "di",
            "call tsr_heap_guards_ok",
            "jr z,resident_basic_system_restore_stack",
            "ld a,1",
            "ld (tsr_heap_fault),a",
            "ld a,0FFh",
            "ld (hook_system_restore_error),a",
            "resident_basic_system_restore_stack:",
            "ld sp,(hook_system_sp)",
            "ei",
            "ld a,(hook_system_restore_error)",
            "ret",
        )

    def test_console_prompt_reopens_unapi_before_guarded_timi_commit(self):
        hooks = _section(
            self.source,
            "; ------------------------------------------------------------ BIOS hooks",
            "else\nresident_keyi_hook:",
        )
        put_hook = _section(
            hooks, "resident_console_put_hook:", "resident_console_get_hook:")
        _assert_in_order(
            self,
            put_hook,
            "push af",
            "push bc",
            "ld b,a",
            "ld a,(in_hook)",
            "ld a,(hook_system_suspended)",
            "jr nz,resident_console_put_track_prompt",
            "ld a,(active_transport_id)",
            "cp UNAPI_ID",
            "resident_console_put_track_prompt:",
            "ld a,b",
            "cp '>'",
            "jr z,resident_console_put_candidate",
            "ld (hook_prompt_candidate),a",
            "resident_console_put_candidate:",
            "ld a,1",
            "ld (hook_prompt_candidate),a",
            "ld (in_hook),a",
            "pop bc",
            "pop af",
            "ex af,af'",
            "xor a",
            "ex af,af'",
            "ret",
        )
        self.assertNotIn("call transport_", put_hook)

        get_hook = _section(
            hooks, "resident_console_get_hook:", "resident_hook_saved_af:")
        _assert_in_order(
            self,
            get_hook,
            "push af",
            "ld a,(in_hook)",
            "ld a,(hook_system_suspended)",
            "ld a,(hook_prompt_candidate)",
            "jr resident_console_get_save_context",
            "resident_console_get_check_relisten:",
            "ld a,(active_transport_id)",
            "cp UNAPI_ID",
            "ld a,(hook_prompt_candidate)",
            "xor a",
            "ld (hook_prompt_candidate),a",
            "ld a,(unapi_relisten_pending)",
            "ld a,(transport_session_lost)",
            "resident_console_get_save_context:",
            "push bc",
            "push de",
            "push hl",
            "push ix",
            "push iy",
            "call resident_console_resume_unapi",
            "resident_console_get_relisten:",
            "call resident_console_relisten_unapi",
            "resident_console_get_reopen_done:",
            "or a",
            "jr nz,resident_console_get_restore",
            "ld a,(hook_system_suspended)",
            "jr z,resident_console_get_restore",
            "ld a,1",
            "ld (hook_resume_pending),a",
            "resident_console_get_restore:",
            "pop iy",
            "pop ix",
            "pop hl",
            "pop de",
            "pop bc",
            "ld (in_hook),a",
            "pop af",
            "ex af,af'",
            "xor a",
            "ex af,af'",
            "ret",
        )

        ordinary_relisten_gate = _section(
            hooks,
            "resident_console_get_check_relisten:",
            "resident_console_get_save_context:",
        )
        self.assertRegex(
            ordinary_relisten_gate,
            r"(?s)ld a,\(active_transport_id\)\s+"
            r"cp UNAPI_ID\s+jr nz,resident_console_get_leave.*"
            r"ld a,\(hook_prompt_candidate\)\s+or a\s+"
            r"jr z,resident_console_get_leave\s+xor a\s+"
            r"ld \(hook_prompt_candidate\),a\s+"
            r"ld a,\(unapi_relisten_pending\)",
        )
        self.assertNotIn("ld a,i", ordinary_relisten_gate)

        relisten = _section(
            hooks,
            "resident_console_relisten_unapi:",
            "resident_console_resume_unapi:",
        )
        _assert_in_order(
            self,
            relisten,
            "di",
            "ld (hook_system_sp),sp",
            "call tsr_heap_guards_ok",
            "jr z,resident_console_relisten_heap_ready",
            "ld (tsr_heap_fault),a",
            "jr resident_console_resume_store_result",
            "resident_console_relisten_heap_ready:",
            "call tsr_heap_fill_guards",
            "ld sp,(tsr_heap_stack_top)",
            "call unapi_relisten_foreground",
            "jr resident_console_resume_store_result",
        )
        self.assertNotIn("call transport_restore", relisten)
        self.assertNotIn("call transport_init", relisten)
        self.assertNotIn("call xfer_reconfigure_detach", relisten)

        resume = _section(
            hooks,
            "resident_console_resume_unapi:",
            "resident_basic_system_word:",
        )
        _assert_in_order(
            self,
            resume,
            "di",
            "ld (hook_system_sp),sp",
            "call tsr_heap_guards_ok",
            "jr z,resident_console_resume_heap_ready",
            "ld (tsr_heap_fault),a",
            "jr resident_console_resume_restore_stack",
            "resident_console_resume_heap_ready:",
            "call tsr_heap_fill_guards",
            "ld sp,(tsr_heap_stack_top)",
            "call transport_restore_foreground_retry",
            "or a",
            "jr nz,resident_console_resume_store_result",
            "call transport_session_reset",
            "call xfer_reconfigure_detach",
            "call transport_init",
            "resident_console_resume_store_result:",
            "ld (hook_system_restore_error),a",
            "call tsr_heap_guards_ok",
            "resident_console_resume_restore_stack:",
            "ld sp,(hook_system_sp)",
            "ei",
            "ld a,(hook_system_restore_error)",
            "ret",
        )

        dispatch = _section(
            hooks, "resident_hook_saved_af:", "resident_basic_crunch_hook:")
        _assert_in_order(
            self,
            dispatch,
            "ld a,(hook_resume_pending)",
            "ld a,1",
            "ld (hook_resume_this_tick),a",
            "jr hook_done",
            "hook_done:",
            "call transport_session_finalize",
            "call tsr_heap_guards_ok",
            "ld a,(hook_resume_this_tick)",
            "xor a",
            "ld (hook_prompt_candidate),a",
            "ld (hook_resume_pending),a",
            "ld (hook_resume_this_tick),a",
            "ld (hook_mapping_state),a",
            "ld (hook_mapping_cooldown),a",
            "ld (hook_system_suspended),a",
        )
        self.assertNotIn("call transport_init", dispatch)

    def test_tsr_kill_restores_memman_sp_before_heap_release(self):
        kill = _section(
            self.source,
            "tsr_kill:",
            "; Foreground suite programs use TsrCall",
        )
        _assert_in_order(
            self,
            kill,
            "di",
            "ld (tsr_heap_memman_sp),sp",
            "ld hl,(tsr_heap_base)",
            "jr z,tsr_kill_release_heap",
            "call tsr_heap_fill_guards",
            "ld sp,(tsr_heap_stack_top)",
            "call transport_restore_foreground_retry",
            "di",
            "call tsr_heap_guards_ok",
            "ld sp,(tsr_heap_memman_sp)",
            "tsr_kill_release_heap:",
            "call tsr_heap_release",
        )
        self.assertLess(
            kill.index("ld sp,(tsr_heap_memman_sp)"),
            kill.index("call tsr_heap_release"),
        )

    def test_a5_lifecycle_uses_heap_and_fails_closed_on_guard_damage(self):
        lifecycle = _section(
            self.source,
            "tsr_talk_config_begin:",
            "tsr_talk_config_begin_common:",
        )
        _assert_in_order(
            self,
            lifecycle,
            "ld a,(tsr_heap_fault)",
            "jp nz,tsr_talk_unsupported",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "jp tsr_talk_unsupported",
            "tsr_talk_config_heap_ready:",
            "ld (in_hook),a",
            "ld (tsr_heap_memman_sp),sp",
            "ld sp,(tsr_heap_stack_top)",
            "call tsr_talk_config_begin_common",
            "ld (tsr_heap_result),a",
            "di",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "ld (tsr_heap_result),a",
            "ld sp,(tsr_heap_memman_sp)",
            "ld (in_hook),a",
            "ld a,(tsr_heap_result)",
            "ret",
        )

    def test_a7_keeps_caller_guards_but_runs_lifecycle_on_page_three(self):
        lifecycle = _section(
            self.source,
            "tsr_talk_unapi_port:",
            "; Validate that an entire caller range",
        )
        self.assertEqual(lifecycle.count("call tsr_talk_unapi_guards_ok"), 2)
        _assert_in_order(
            self,
            lifecycle,
            "call tsr_talk_unapi_guards_ok",
            "ld a,(tsr_heap_fault)",
            "jp nz,tsr_talk_unapi_bad_stack",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "jp tsr_talk_unapi_bad_stack",
            "tsr_talk_unapi_heap_ready:",
            "ld (in_hook),a",
            "ld (tsr_heap_memman_sp),sp",
            "ld sp,(tsr_heap_stack_top)",
            "call tsr_talk_unapi_port_inner",
            "ld (tsr_unapi_result),a",
            "di",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "ld sp,(tsr_heap_memman_sp)",
            "ld a,(tsr_heap_fault)",
            "jr nz,tsr_talk_unapi_stack_corrupted",
            "call tsr_talk_unapi_guards_ok",
            "jr nz,tsr_talk_unapi_stack_corrupted",
        )
        self.assertNotIn("ld sp,(tsr_unapi_stack_top)", lifecycle)
        self.assertNotIn("tsr_unapi_memman_sp", lifecycle)
        self.assertLess(
            lifecycle.index("ld sp,(tsr_heap_memman_sp)"),
            lifecycle.index("call tsr_talk_unapi_write_result"),
        )

    def test_protocol_x_pump_uses_the_same_guarded_heap_stack(self):
        pump = _section(
            self.source, "tsr_talk_xfer_pump:", "; CLAIM input HL points")
        _assert_in_order(
            self,
            pump,
            "ld a,(in_hook)",
            "jp nz,tsr_talk_unsupported",
            "ld a,(tsr_heap_fault)",
            "jp nz,tsr_talk_unsupported",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "jp tsr_talk_unsupported",
            "tsr_talk_xfer_pump_heap_ready:",
            "ld (in_hook),a",
            "ld (tsr_heap_memman_sp),sp",
            "ld sp,(tsr_heap_stack_top)",
            "ld (xfer_fast_pump_sp),sp",
            "call transport_service",
            "call transport_session_guard",
            "call transport_session_finalize",
            "tsr_talk_xfer_pump_leave_heap:",
            "di",
            "call tsr_heap_guards_ok",
            "ld (tsr_heap_fault),a",
            "ld sp,(tsr_heap_memman_sp)",
            "ld (in_hook),a",
        )
        self.assertEqual(pump.count("ld sp,(tsr_heap_memman_sp)"), 1)
        self.assertLess(
            pump.index("ld (in_hook),a"),
            pump.index("ld sp,(tsr_heap_stack_top)"),
        )
        self.assertLess(
            pump.index("call tsr_heap_guards_ok", pump.index(
                "tsr_talk_xfer_pump_leave_heap:")),
            pump.index("ld sp,(tsr_heap_memman_sp)"),
        )

    def test_protocol_x_loss_and_timeout_unwind_through_heap_exit(self):
        unwind = _section(
            self.source,
            "xfer_fast_pump_session_lost:",
            "xfer_fast_pump_abandon:",
        )
        session_lost = unwind.split("xfer_fast_pump_timeout:", 1)[0]
        timeout = unwind.split("xfer_fast_pump_timeout:", 1)[1]
        _assert_in_order(
            self,
            session_lost,
            "ld sp,(xfer_fast_pump_sp)",
            "call xfer_fast_pump_abandon",
            "call transport_session_finalize",
            "ld (tsr_heap_result),a",
            "jp tsr_talk_xfer_pump_leave_heap",
        )
        _assert_in_order(
            self,
            timeout,
            "ld sp,(xfer_fast_pump_sp)",
            "call xfer_fast_pump_abandon",
            "ld (tsr_heap_result),a",
            "jp tsr_talk_xfer_pump_leave_heap",
        )
        self.assertNotIn("ld sp,(tsr_heap_memman_sp)", unwind)

        put_wait = _section(
            self.source, "ser_put_pump_wait:", "ser_put_pump_ready:")
        get_wait = _section(
            self.source, "ser_get_pump_wait:", "ser_get_pump_ready:")
        self.assertIn("jp xfer_fast_pump_timeout", put_wait)
        self.assertIn("jp xfer_fast_pump_timeout", get_wait)

    def test_tsr_init_allocates_before_transport_and_releases_all_failures(self):
        initializer = _section(self.source, "tsr_init:", "tsr_intro_message:")
        _assert_in_order(
            self,
            initializer,
            "di",
            "call tsr_heap_allocate",
            "jr c,tsr_init_heap_failed",
            "ld (tsr_heap_memman_sp),sp",
            "ld sp,(tsr_heap_stack_top)",
            "ld (unapi_defer_first_open),a",
            "call resident_initialize",
            "jr nz,tsr_init_failed_on_heap",
            "di",
            "call tsr_heap_guards_ok",
            "jr z,tsr_init_heap_succeeded",
            "ld (tsr_heap_fault),a",
            "tsr_init_failed_on_heap:",
            "call transport_restore_foreground_retry",
            "di",
            "ld sp,(tsr_heap_memman_sp)",
            "call tsr_heap_release",
            "tsr_init_failed:",
            "ld a,3",
            "tsr_init_heap_succeeded:",
            "di",
            "ld sp,(tsr_heap_memman_sp)",
            "ld a,2",
            "tsr_init_heap_failed:",
            "ld a,3",
        )
        allocation_failure = initializer.split(
            "tsr_init_heap_failed:", 1)[1]
        self.assertNotIn("resident_initialize", allocation_failure)
        self.assertNotIn("transport_restore", allocation_failure)
        success = initializer.split("tsr_init_heap_succeeded:", 1)[1].split(
            "tsr_init_heap_failed:", 1)[0]
        self.assertNotIn("tsr_heap_release", success)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_heap_contract_is_present_in_the_assembled_tsr(self):
        from tools.build_agent_tsr import BUILD_ORIGINS, _assemble

        with tempfile.TemporaryDirectory() as directory:
            image = _assemble(
                ROOT, pathlib.Path(directory), "z80asm", BUILD_ORIGINS[0])

        for label in (
                "tsr_heap_base", "tsr_heap_stack_bottom",
                "tsr_heap_stack_top", "tsr_heap_fault",
                "tsr_heap_allocate", "tsr_heap_release",
                "tsr_heap_fill_guards", "tsr_heap_guards_ok"):
            self.assertIn(label, image.labels)
            self.assertGreaterEqual(image.labels[label], image.origin)
            self.assertLess(image.labels[label], image.labels["resident_end"])

        allocate = image.data[
            image.labels["tsr_heap_allocate"] - image.origin:
            image.labels["tsr_heap_release"] - image.origin
        ]
        release = image.data[
            image.labels["tsr_heap_release"] - image.origin:
            image.labels["tsr_heap_fill_guards"] - image.origin
        ]
        self.assertIn(b"\x21\x20\x04\x11\x46\x00\xcd\x02\x40", allocate)
        self.assertIn(b"\x11\x47\x00\xcd\x02\x40", release)


if __name__ == "__main__":
    unittest.main()
