import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = ROOT / "agent" / "msx_memman_loader.asm"
MAKEFILE = ROOT / "Makefile"


class MemManLoaderSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = LOADER.read_text(encoding="utf-8")

    def test_loader_is_integrated_into_the_single_production_build(self):
        makefile = MAKEFILE.read_text(encoding="utf-8")
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        self.assertIn("msx_memman_loader", makefile)
        self.assertIn("include 'agent/msx_memman_loader.asm'", core)
        self.assertIn("work/agent/MSXAI.COM", makefile)

    def test_verified_build_artifacts_are_embedded(self):
        paths = {
            "tl": "work/agent/vendor/TL.COM",
            "tk": "work/agent/vendor/TK.COM",
            "tsr": "work/agent/MSXAI.TSR",
            "memman": "work/agent/vendor/MEMMAN.COM",
        }
        for name, path in paths.items():
            with self.subTest(name=name):
                section = self.source.split(f"{name}_blob_start:", 1)[1].split(
                    f"{name}_blob_end:", 1)[0]
                self.assertIn(f"incbin '{path}'", section)

        preflight = self.source.split("preflight_driver_ok:", 1)[1].split(
            "; The selected driver byte", 1)[0]
        for size in ("tl_blob_size", "tsr_blob_size", "memman_blob_size"):
            self.assertIn(f"ld hl,{size}", preflight)
        self.assertEqual(preflight.count("jp z,preflight_bad_image"), 3)

    def test_create_new_and_collision_retry_prevent_overwrite(self):
        self.assertRegex(
            self.source, r"(?m)^DOS_CREATE:\s+equ\s+044h$")
        self.assertRegex(
            self.source, r"(?m)^CREATE_NEW:\s+equ\s+080h$")
        reserve = self.source.split("reserve_temporary_pair:", 1)[1].split(
            "; Blob extraction", 1)[0]
        self.assertEqual(reserve.count("ld b,CREATE_NEW"), 2)
        for error in (
                "ERR_FILE_EXISTS", "ERR_DIRECTORY_EXISTS",
                "ERR_SYSTEM_EXISTS"):
            self.assertIn(f"cp {error}", reserve)
        self.assertIn("jr nz,reserve_pair_attempt", reserve)
        self.assertIn("cp 36", reserve)

    def test_dos2_is_checked_before_file_handle_calls(self):
        self.assertRegex(
            self.source, r"(?m)^DOS_VERSION:\s+equ\s+06Fh$")
        preflight = self.source.split("loader_preflight:", 1)[1].split(
            "preflight_driver_ok:", 1)[0]
        self.assertLess(preflight.index("ld c,DOS_VERSION"),
                        preflight.index("ld a,(loader_transport_id)"))
        self.assertIn("cp 2", preflight)
        self.assertIn("ld (dos2_available),a", preflight)
        abort = self.source.split("loader_abort_terminate:", 1)[1].split(
            "; MemMan handoff", 1)[0]
        self.assertIn("ld a,(dos2_available)", abort)
        self.assertIn("jp z,00000h", abort)

    def test_failure_cleanup_covers_open_and_closed_files(self):
        self.assertRegex(
            self.source, r"(?m)^DOS_HDELETE:\s+equ\s+052h$")
        self.assertRegex(
            self.source, r"(?m)^DOS_DELETE:\s+equ\s+04Dh$")
        cleanup = self.source.split("cleanup_temporaries:", 1)[1].split(
            "loader_abort:", 1)[0]
        for routine in (
                "cleanup_open_tl", "cleanup_open_tsr",
                "cleanup_closed_tsr", "cleanup_closed_tl"):
            self.assertIn(f"call {routine}", cleanup.split("\n\n", 1)[0])
        self.assertEqual(cleanup.count("ld c,DOS_HDELETE"), 2)
        self.assertEqual(cleanup.count("ld c,DOS_DELETE"), 2)
        self.assertIn("ld (cleanup_failed),a", cleanup)

    def test_driver_is_validated_and_patched_during_tsr_write(self):
        preflight = self.source.split("loader_preflight:", 1)[1].split(
            "preflight_driver_ok:", 1)[0]
        self.assertIn("ld a,(loader_transport_id)", preflight)
        self.assertIn("cp DRIVER_8251", preflight)
        self.assertIn("cp DRIVER_16C550", preflight)

        write = self.source.split("write_temporary_files:", 1)[1].split(
            "write_exact:", 1)[0]
        self.assertIn("ld de,loader_transport_id", write)
        self.assertIn("ld hl,MSXAI_TSR_TRANSPORT_OFFSET", write)
        self.assertIn("ld bc,MSXAI_TSR_TRANSPORT_OFFSET + 1", write)
        image_checks = self.source.split("preflight_driver_ok:", 1)[1].split(
            "preflight_uninstall_images:", 1)[0]
        self.assertIn("ld de,MSXAI_TSR_SIZE", image_checks)
        self.assertIn(
            "ld hl,tsr_blob_start + MSXAI_TSR_TRANSPORT_OFFSET",
            image_checks)
        self.assertIn("cp 0FEh", image_checks)

    def test_memman_install_tail_and_direct_tk_uninstall(self):
        command = self.source.split("install_command:", 1)[1].split(
            "install_command_end:", 1)[0]
        strings = re.findall(r'db\s+"([^"]*)"', command)
        self.assertEqual(
            "".join(strings),
            " _SYSTEM@@M0 A0@DEL A0.TSR@DEL M0.COM@")
        uninstall = self.source.split("uninstall_command:", 1)[1].split(
            "uninstall_command_end:", 1)[0]
        self.assertIn('db " ",34,"MSXAI MCP1",34', uninstall)
        self.assertNotIn("_SYSTEM", uninstall)
        entry = self.source.split("memman_loader_entry:", 1)[1].split(
            "; ---------------------------------------------------------------------------\n"
            "; Preflight", 1)[0]
        self.assertLess(
            entry.index("jr z,memman_loader_uninstall_direct"),
            entry.index("call reserve_temporary_pair"))
        direct = self.source.split("handoff_to_tk:", 1)[1].split(
            "; ---------------------------------------------------------------------------\n"
            "; MemMan discovery", 1)[0]
        self.assertIn("ld hl,tk_blob_start", direct)
        self.assertIn("ld bc,tk_blob_size", direct)
        self.assertRegex(
            self.source, r"(?m)^MEMMAN_COMMAND_MAX:\s+equ\s+40$")
        self.assertLessEqual(
            len(" _SYSTEM@@M0 A0@DEL A0.TSR@DEL M0.COM@"), 40)
        self.assertLessEqual(len(' "MSXAI MCP1"'), 40)
        self.assertIn("ld de,COMMAND_TEXT", self.source)
        self.assertIn("ld (COMMAND_TAIL),a", self.source)

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
        self.assertIn("ld de,memman_blob_end", self.source)
        self.assertIn("ld de,tk_blob_end", self.source)
        self.assertIn("ld de,COM_ENTRY + memman_blob_size", self.source)
        handoff = self.source.split("handoff_to_memman:", 1)[1].split(
            "; Mutable state", 1)[0]
        self.assertIn("ld hl,overlay_stub", handoff)
        self.assertIn("ld hl,memman_blob_start", handoff)
        self.assertIn("ld de,COM_ENTRY", handoff)
        self.assertIn("ldir\n    jp COM_ENTRY", handoff)

    def test_put_action_streams_mailbox_chunks_only_from_foreground(self):
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        self.assertIn("LOADER_ACTION_PUT: equ 2", core)
        self.assertIn('db "/PUT",0', core)
        self.assertIn(
            'db "  MSXAI /PUT <DOS-file> <hex-bytes> <crc16>"', core)
        parser = core.split("loader_parse_put:", 1)[1].split(
            "loader_parse_tokens_done:", 1)[0]
        self.assertIn("call loader_copy_put_filename", parser)
        self.assertIn("call loader_parse_put_length", parser)
        self.assertIn("call loader_parse_put_crc", parser)
        self.assertIn("ld a,(loader_action)", parser)
        self.assertIn("ld a,LOADER_ACTION_PUT", parser)

        put = self.source.split("loader_put_file:", 1)[1].split(
            "; Both entry points consume", 1)[0]
        self.assertNotIn("08000h", put)
        self.assertIn("call memman_find_agent", put)
        self.assertIn("TSR_TALK_UPLOAD_BEGIN", put)
        self.assertIn("TSR_TALK_UPLOAD_POLL", put)
        self.assertIn("TSR_TALK_UPLOAD_END", put)
        self.assertIn("ld hl,loader_put_buffer", put)
        self.assertIn("call loader_put_crc_update", put)
        self.assertIn("FILE_UPLOAD_TIMEOUT_TICKS", put)
        self.assertIn("ld b,CREATE_NEW", put)
        self.assertIn("call write_exact", put)
        self.assertIn("ld c,DOS_CLOSE", put)
        self.assertIn("ld c,DOS_HDELETE", put)
        self.assertIn("ld c,DOS_DELETE", put)
        self.assertIn("MSXAI PUT READY", put)
        self.assertIn("MSXAI PUT OK", put)
        self.assertIn("MSXAI PUT ERROR", put)
        talk = self.source.split("loader_put_tsr_call:", 1)[1].split(
            "loader_put_commit_upload:", 1)[0]
        self.assertIn("call EXTBIO", talk)
        self.assertIn("ei", talk)
        terminal = self.source.split("loader_put_commit_upload:", 1)[1].split(
            "; Incremental CRC", 1)[0]
        self.assertIn("ld hl,1", terminal)
        self.assertIn("loader_put_abort_upload:", terminal)
        self.assertIn("ld hl,0", terminal)


if __name__ == "__main__":
    unittest.main()
