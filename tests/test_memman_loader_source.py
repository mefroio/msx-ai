import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = ROOT / "agent" / "msx_memman_loader.asm"
MAKEFILE = ROOT / "Makefile"


class MemManLoaderSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = LOADER.read_text(encoding="utf-8")

    def test_loader_is_integrated_into_the_canonical_suite_build(self):
        makefile = MAKEFILE.read_text(encoding="utf-8")
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        self.assertIn("msx_memman_loader", makefile)
        self.assertIn("include 'agent/msx_memman_loader.asm'", core)
        self.assertIn("work/agent/MSXAI.COM", makefile)

    def test_verified_suite_artifacts_are_external(self):
        makefile = MAKEFILE.read_text(encoding="utf-8")
        for artifact in (
                "MSXAI.COM", "MSXAIXF.COM", "MCP8251.TSR",
                "MCP16550.TSR", "MEMMAN.COM", "TL.COM", "TK.COM"):
            self.assertIn(artifact, makefile)
        self.assertNotIn("incbin", self.source.lower())
        self.assertIn('db "MEMMAN.COM",0', self.source)
        self.assertIn('db "TL.COM",0', self.source)
        self.assertIn('db "TK.COM",0', self.source)
        self.assertIn('db "MCP8251.TSR",0', self.source)
        self.assertIn('db "MCP16550.TSR",0', self.source)
        self.assertRegex(
            self.source, r"(?m)^MEMMAN_FILE_SIZE:\s+equ\s+01E00h")
        self.assertRegex(
            self.source, r"(?m)^TL_FILE_SIZE:\s+equ\s+00A00h")
        self.assertRegex(
            self.source, r"(?m)^TK_FILE_SIZE:\s+equ\s+00580h")

    def test_resident_lifecycle_creates_no_temporary_files(self):
        lifecycle = self.source.split(
            "memman_loader_install:", 1)[1].split(
                "; MemMan discovery", 1)[0]
        for obsolete in (
                "reserve_temporary_pair", "write_temporary_files",
                "cleanup_temporaries", "tl_blob_start", "tk_blob_start",
                "tsr_blob_start", "memman_blob_start", "@DEL"):
            self.assertNotIn(obsolete, lifecycle)
        self.assertNotIn("ld c,DOS_CREATE", lifecycle)
        self.assertNotIn("ld c,DOS_DELETE", lifecycle)

    def test_dos2_is_checked_before_file_handle_calls(self):
        self.assertRegex(
            self.source, r"(?m)^DOS_VERSION:\s+equ\s+06Fh$")
        preflight = self.source.split("loader_preflight:", 1)[1].split(
            "preflight_select_8251:", 1)[0]
        self.assertLess(preflight.index("ld c,DOS_VERSION"),
                        preflight.index("ld a,(loader_transport_id)"))
        self.assertIn("cp 2", preflight)
        self.assertIn("ld (dos2_available),a", preflight)
        abort = self.source.split("loader_abort_terminate:", 1)[1].split(
            "; MemMan handoff", 1)[0]
        self.assertIn("ld a,(dos2_available)", abort)
        self.assertIn("jp z,00000h", abort)

    def test_failure_path_closes_external_file_without_mutation(self):
        close = self.source.split(
            "suite_close_preserving_error:", 1)[1].split(
                "suite_stage_overlay:", 1)[0]
        self.assertIn("ld c,DOS_CLOSE", close)
        self.assertIn("ld (suite_handle),a", close)
        abort = self.source.split("loader_abort:", 1)[1].split(
            "loader_abort_terminate:", 1)[0]
        self.assertIn("call suite_close_preserving_error", abort)

    def test_driver_selects_a_prepatched_verified_tsr(self):
        preflight = self.source.split("loader_preflight:", 1)[1].split(
            "preflight_select_8251:", 1)[0]
        self.assertIn("ld a,(loader_transport_id)", preflight)
        self.assertIn("cp DRIVER_8251", preflight)
        self.assertIn("cp DRIVER_16C550", preflight)
        validate = self.source.split(
            "suite_validate_selected_tsr:", 1)[1].split(
                "suite_close_preserving_error:", 1)[0]
        self.assertIn("ld hl,MSXAI_TSR_SIZE", validate)
        self.assertIn("ld hl,MSXAI_TSR_TRANSPORT_OFFSET", validate)
        self.assertIn("ld a,(suite_expected_transport)", validate)
        self.assertNotIn("ld de,loader_transport_id", validate)

    def test_memman_install_tail_and_direct_tk_uninstall(self):
        self.assertIn('db " _SYSTEM@@TL "', self.source)
        command_builder = self.source.split(
            "suite_build_install_command:", 1)[1].split(
                "; ---------------------------------------------------------------------------\n"
                "; External suite validation", 1)[0]
        self.assertIn("ld hl,(suite_selected_tsr_path)", command_builder)
        self.assertIn("strip .TSR", command_builder)
        self.assertIn("ld a,'@'", command_builder)
        self.assertIn("cp MEMMAN_COMMAND_MAX + 1", command_builder)
        uninstall = self.source.split("uninstall_command:", 1)[1].split(
            "uninstall_command_end:", 1)[0]
        self.assertIn('db " ",34,"MSXAI MCP1",34', uninstall)
        self.assertNotIn("_SYSTEM", uninstall)
        self.assertRegex(
            self.source, r"(?m)^MEMMAN_COMMAND_MAX:\s+equ\s+40$")
        self.assertLessEqual(len(" _SYSTEM@@TL A:\\MSXAI\\MCP8251@"), 40)
        self.assertLessEqual(len(" _SYSTEM@@TL A:\\MSXAI\\MCP16550@"), 40)
        self.assertLessEqual(len(' "MSXAI MCP1"'), 40)
        self.assertIn("ld de,COMMAND_TEXT", self.source)
        self.assertIn("ld (COMMAND_TAIL),a", self.source)

    def test_suite_home_resolves_every_companion_with_safe_fallback(self):
        resolver = self.source.split("suite_resolve_paths:", 1)[1].split(
            "; ---------------------------------------------------------------------------\n"
            "; External suite validation", 1)[0]
        self.assertRegex(
            self.source, r"(?m)^DOS_GET_ENV:\s+equ\s+06Bh$")
        self.assertIn('db "MSXAI_HOME",0', self.source)
        self.assertIn("ld c,DOS_GET_ENV", resolver)
        for destination in (
                "suite_memman_path", "suite_tl_path", "suite_tk_path",
                "suite_mcp8251_tsr_path", "suite_mcp16550_tsr_path"):
            self.assertIn(f"ld de,{destination}", resolver)
        self.assertIn("ld a,(suite_home_buffer)", resolver)
        self.assertIn("jr z,suite_build_path_name", resolver)
        self.assertIn("cp DOS_PATH_SEPARATOR", resolver)
        self.assertIn("cp '/'", resolver)
        self.assertIn("ld a,ERR_INVALID_PARAMETER", resolver)

    def test_memman_discovery_uses_standard_id_and_tsr_call(self):
        discovery = self.source.split("memman_find_agent:", 1)[1].split(
            "; Mutable state", 1)[0]
        self.assertIn("ld e,30", discovery)
        self.assertIn("cp 2", discovery)
        self.assertIn("cp 4", discovery)
        self.assertIn("ld e,62", discovery)
        self.assertIn("ld e,63", discovery)
        self.assertIn('db "MSXAI MCP1  "', discovery)
        self.assertIn("ld a,0A5h", discovery)

    def test_overlay_has_guards_and_an_explicit_point_of_no_return(self):
        self.assertIn("POINT OF NO RETURN", self.source)
        preflight = self.source.split("preflight_have_limit:", 1)[1].split(
            "preflight_bad_image:", 1)[0]
        self.assertIn("ld de,suite_loader_live_end", preflight)
        self.assertIn("ld (overlay_source),hl", preflight)
        self.assertIn("ld de,COM_ENTRY", preflight)
        stage = self.source.split("suite_stage_overlay:", 1)[1].split(
            "loader_abort:", 1)[0]
        self.assertIn("call suite_open_exact_file", stage)
        self.assertIn("ld c,DOS_SEEK", stage)
        self.assertIn("ld c,DOS_READ", stage)
        self.assertEqual(stage.count("ld c,DOS_READ"), 1)
        self.assertNotIn("suite_probe_byte", stage)
        self.assertIn("ld c,DOS_CLOSE", self.source)
        handoff = self.source.split(
            "handoff_to_external_overlay:", 1)[1].split(
            "; Mutable state", 1)[0]
        self.assertIn("ld hl,overlay_stub", handoff)
        self.assertIn("ld hl,(overlay_source)", handoff)
        self.assertIn("ld de,COM_ENTRY", handoff)
        self.assertIn("ldir\n    jp COM_ENTRY", handoff)

    def test_overlay_rewinds_one_size_verified_handle_without_eof_probe(self):
        stage = self.source.split("suite_stage_overlay:", 1)[1].split(
            "loader_abort:", 1)[0]
        validate = stage.index("call suite_open_exact_file")
        rewind = stage.index("ld c,DOS_SEEK", validate)
        read = stage.index("ld c,DOS_READ", rewind)
        close = stage.index("jp suite_close_preserving_error", read)
        self.assertLess(validate, rewind)
        self.assertLess(rewind, read)
        self.assertLess(read, close)
        self.assertIn("ld hl,(suite_overlay_size)", stage[:validate])
        self.assertIn("ld a,(suite_handle)", stage[validate:read])
        self.assertNotIn("ld c,DOS_OPEN", stage)
        self.assertEqual(stage.count("ld c,DOS_READ"), 1)
        self.assertNotIn("suite_probe_byte", stage)

    def test_legacy_put_action_is_absent_and_protocol_x_worker_remains(self):
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        helper = (ROOT / "agent" / "msx_xfer.asm").read_text(
            encoding="utf-8")

        for obsolete in (
                "LOADER_ACTION_PUT", "loader_parse_put:", "option_put:",
                "loader_copy_put_filename:", "loader_parse_put_length:",
                "loader_parse_put_crc:", "loader_put_file:",
                "TSR_TALK_UPLOAD_BEGIN", "TSR_TALK_UPLOAD_POLL",
                "TSR_TALK_UPLOAD_END", "FILE_UPLOAD_TIMEOUT_TICKS",
                "loader_put_tsr_call:", "loader_put_crc_update:"):
            self.assertNotIn(obsolete, core + self.source)
        self.assertNotIn("MSXAI /PUT", core)

        self.assertIn("loader_xfer_put_file:", self.source)
        self.assertIn("loader_xfer_get_file:", self.source)
        self.assertIn("TSR_TALK_XFER_PUT_POLL", self.source)
        self.assertIn("TSR_TALK_XFER_GET_PUBLISH", self.source)
        self.assertIn("MSXAIXF.COM", helper)
        self.assertIn('db "/PUT",0', helper)
        self.assertIn('db "/GET",0', helper)
        self.assertIn("loader_xfer_buffer:", helper)
        self.assertNotIn("loader_put_buffer", helper + self.source)


if __name__ == "__main__":
    unittest.main()
