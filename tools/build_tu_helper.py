#!/usr/bin/env python3
"""Build and structurally validate the pre-TL TCP/IP UNAPI helper."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY / "agent" / "msx_tu_helper.asm"
DEFAULT_OUTPUT = REPOSITORY / "work" / "agent" / "TU.COM"
ORIGIN = 0x0100
PAGE_1_START = 0x4000
API_ID = b"TCP/IP\0"
ENVIRONMENT_NAME = b"MSXAI_HOME\0"
TL_NAME = b"TL.COM\0"
LABEL_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*equ\s+\$([0-9A-Fa-f]+)\s*$")

REQUIRED_LABELS = (
    "tu_helper_start",
    "tu_helper_end",
    "require_dos2",
    "resolve_tl_path",
    "plan_high_stage",
    "stage_tl_exact",
    "close_tl_preserving_error",
    "enumerate_tcpip_unapi",
    "enumerate_count_extbio",
    "enumerate_tcpip_candidate_loop",
    "enumerate_candidate_extbio",
    "enumerate_tcpip_complete",
    "enumerate_tcpip_unapi_end",
    "prepare_pico_htimi_tail",
    "prepare_pico_htimi_tail_end",
    "copy_tcpip_api_id",
    "resolve_memman_handler",
    "resolve_memman_handler_end",
    "harden_pico_htimi",
    "harden_pico_htimi_end",
    "capture_pico_memory_baseline",
    "capture_pico_memory_baseline_end",
    "relocate_pico_private_block",
    "relocate_pico_have_measured_length",
    "relocate_pico_heap_failed",
    "relocate_pico_layout_failed",
    "relocate_pico_private_block_end",
    "call_memman_function",
    "call_memman_function_end",
    "handoff_to_staged_tl",
    "handoff_to_staged_tl_end",
    "overlay_stub",
    "overlay_stub_end",
    "overlay_stub_size",
    "tu_abort",
    "tu_entry_sp",
    "overlay_target",
    "tl_stage_source",
    "tl_handle",
    "implementation_remaining",
    "implementation_index",
    "memman_function_handler",
    "pico_himem_before",
    "pico_heap_pointer",
    "pico_work_length",
    "pico_relocation_applied",
    "pico_relocation_error",
    "suite_home_env_name",
    "tl_name",
    "tcpip_api_id",
    "tcpip_api_id_end",
    "suite_home_buffer",
    "tl_path",
)

REQUIRED_CONSTANTS = {
    "BDOS": 0x0005,
    "EXTBIO": 0xFFCA,
    "MEMMAN_INI_CHECK": 30,
    "MEMMAN_HEAP_ALLOC": 70,
    "UNAPI_ARGUMENT": 0xF847,
    "UNAPI_EXTBIO_MAGIC": 0x2222,
    "H_TIMI": 0xFD9F,
    "HIMEM": 0xFC4A,
    "PICO_WORK_POINTER": 0xFD3E,
    "CALLF_OPCODE": 0xF7,
    "PICO_TIMI_ENTRY_LOW": 0xB8,
    "PICO_TIMI_ENTRY_HIGH": 0x4C,
    "RET_OPCODE": 0xC9,
    "PICO_WORK_MAX": 64,
    "ERR_INTERNAL": 0xDF,
    "ERR_NO_MEMORY": 0xDE,
    "DOS_OPEN": 0x43,
    "DOS_CLOSE": 0x45,
    "DOS_READ": 0x48,
    "DOS_SEEK": 0x4A,
    "DOS_TERM_ERROR": 0x62,
    "DOS_GET_ENV": 0x6B,
    "DOS_VERSION": 0x6F,
    "COMMAND_TAIL": 0x0080,
    "COM_ENTRY": ORIGIN,
    "TPA_TOP_POINTER": 0x0006,
    "TL_FILE_SIZE": 0x0A00,
    "TL_OVERLAY_END": 0x0B00,
    "OVERLAY_STACK_HEADROOM": 0x0200,
    "SUITE_PATH_MAX": 63,
    "SUITE_PATH_BUFFER_SIZE": 64,
}

# Exact code emitted by harden_pico_htimi.  In particular, no access to the
# slot byte at FDA0h and no write other than FDA3h is allowed to drift in.
PICO_HOOK_HARDENER = bytes.fromhex(
    "3a 9f fd fe f7 c0 "
    "3a a1 fd fe b8 c0 "
    "3a a2 fd fe 4c c0 "
    "3e c9 32 a3 fd c9"
)
PICO_HOOK_PREPARER = bytes.fromhex(
    "3a 9f fd fe c9 c0 32 a3 fd c9"
)
OVERLAY_STUB = bytes.fromhex("ed b0 c3 00 01")  # LDIR; JP 0100h
EXTBIO_WINDOW = bytes.fromhex("fb cd ca ff f3")  # EI; CALL FFCAh; DI
BDOS_CALL = bytes.fromhex("cd 05 00")


class TuHelperBuildError(ValueError):
    """The assembled TU.COM image violates its pre-TL handoff contract."""


@dataclasses.dataclass(frozen=True)
class TuHelperImage:
    """A validated helper image and its assembler labels."""

    data: bytes
    labels: Mapping[str, int]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def parse_labels(text: str) -> dict[str, int]:
    """Parse the label format emitted by z80asm 1.8's ``-L`` option."""

    labels: dict[str, int] = {}
    for line in text.splitlines():
        match = LABEL_LINE.fullmatch(line)
        if match is None:
            continue
        name, hexadecimal = match.groups()
        if name in labels:
            raise TuHelperBuildError(
                f"assembler emitted duplicate label {name!r}")
        labels[name] = int(hexadecimal, 16)
    return labels


def _slice(
    data: bytes, labels: Mapping[str, int], start: str, end: str
) -> bytes:
    first = labels[start] - ORIGIN
    last = labels[end] - ORIGIN
    if not 0 <= first <= last <= len(data):
        raise TuHelperBuildError(
            f"invalid image range {start}..{end}: {first}..{last}")
    return data[first:last]


def _call(address: int) -> bytes:
    return bytes((0xCD, address & 0xFF, address >> 8))


def _word(address: int) -> bytes:
    return bytes((address & 0xFF, (address >> 8) & 0xFF))


def _load_a(address: int) -> bytes:
    return bytes((0x3A,)) + _word(address)


def _store_a(address: int) -> bytes:
    return bytes((0x32,)) + _word(address)


def _load_hl(address: int) -> bytes:
    return bytes((0x2A,)) + _word(address)


def _load_de(address: int) -> bytes:
    return bytes.fromhex("ed 5b") + _word(address)


def _store_hl(address: int) -> bytes:
    return bytes((0x22,)) + _word(address)


def _jump(address: int, opcode: int = 0xC3) -> bytes:
    return bytes((opcode, address & 0xFF, address >> 8))


def validate_tu_helper_image(
    data: bytes, labels: Mapping[str, int]
) -> None:
    """Pin TU's image, UNAPI window, Pico hook guard, and TL overlay."""

    missing = [name for name in REQUIRED_LABELS if name not in labels]
    missing.extend(name for name in REQUIRED_CONSTANTS if name not in labels)
    if missing:
        raise TuHelperBuildError(
            "assembler label output is missing: " + ", ".join(missing))

    if labels["tu_helper_start"] != ORIGIN:
        raise TuHelperBuildError("tu_helper_start must be COM origin 0x0100")
    if labels["tu_helper_end"] != ORIGIN + len(data):
        raise TuHelperBuildError(
            "tu_helper_end does not match the assembled image length")
    if not data:
        raise TuHelperBuildError("assembler emitted an empty TU.COM")
    if labels["tu_helper_end"] > PAGE_1_START:
        raise TuHelperBuildError(
            "TU.COM reaches page 1 instead of remaining a transient helper")

    for name, expected in REQUIRED_CONSTANTS.items():
        if labels[name] != expected:
            raise TuHelperBuildError(
                f"{name} is 0x{labels[name]:04X}, expected 0x{expected:04X}")

    preflight_head = (
        _call(labels["require_dos2"])
        + bytes((0xB7,))
        + _jump(labels["tu_abort"], opcode=0xC2)
        + _call(labels["resolve_memman_handler"])
        + bytes((0xB7,))
        + _jump(labels["tu_abort"], opcode=0xC2)
        + _call(labels["resolve_tl_path"])
    )
    if data.count(preflight_head) != 1:
        raise TuHelperBuildError(
            "TU entry must resolve MemMan after the DOS2 check and before "
            "staging TL")

    if _slice(data, labels, "tcpip_api_id", "tcpip_api_id_end") != API_ID:
        raise TuHelperBuildError("UNAPI identifier is not exactly TCP/IP\\0")

    def terminated_at(label: str, expected: bytes) -> None:
        offset = labels[label] - ORIGIN
        if data[offset:offset + len(expected)] != expected:
            raise TuHelperBuildError(
                f"{label} does not contain the pinned {expected!r}")

    terminated_at("suite_home_env_name", ENVIRONMENT_NAME)
    terminated_at("tl_name", TL_NAME)

    hardener = _slice(
        data, labels, "harden_pico_htimi", "harden_pico_htimi_end")
    if hardener != PICO_HOOK_HARDENER:
        raise TuHelperBuildError(
            "Pico H.TIMI hardener is not the guarded F7 ?? B8 4C -> FDA3=C9 "
            "sequence")

    preparer = _slice(
        data, labels,
        "prepare_pico_htimi_tail", "prepare_pico_htimi_tail_end")
    if preparer != PICO_HOOK_PREPARER:
        raise TuHelperBuildError(
            "Pico H.TIMI preparer is not the guarded RET -> FDA3=C9 "
            "sequence")

    stub = _slice(data, labels, "overlay_stub", "overlay_stub_end")
    if stub != OVERLAY_STUB or labels["overlay_stub_size"] != len(OVERLAY_STUB):
        raise TuHelperBuildError(
            "overlay stub must be exactly LDIR followed by JP 0100h")

    enumeration = _slice(
        data, labels, "enumerate_tcpip_unapi", "enumerate_tcpip_unapi_end")
    if BDOS_CALL in enumeration:
        raise TuHelperBuildError(
            "UNAPI enumeration window contains a forbidden DOS call")
    if enumeration.count(EXTBIO_WINDOW) != 2:
        raise TuHelperBuildError(
            "count and candidate EXTBIO calls must each use EI/CALL FFCAh/DI")

    for label in ("enumerate_count_extbio", "enumerate_candidate_extbio"):
        offset = labels[label] - ORIGIN
        if data[offset:offset + len(EXTBIO_WINDOW)] != EXTBIO_WINDOW:
            raise TuHelperBuildError(
                f"{label} is not an explicit EI/EXTBIO/DI window")

    candidate = labels["enumerate_candidate_extbio"] - ORIGIN
    candidate_loop = labels["enumerate_tcpip_candidate_loop"] - ORIGIN
    after_candidate = candidate + len(EXTBIO_WINDOW)
    preparer_call = _call(labels["prepare_pico_htimi_tail"])
    hardener_call = _call(labels["harden_pico_htimi"])
    relocator_call = _call(labels["relocate_pico_private_block"])
    candidate_snapshot = (
        _load_hl(labels["HIMEM"])
        + _store_hl(labels["pico_himem_before"])
        + preparer_call
    )
    if data[candidate_loop:candidate].count(candidate_snapshot) != 1:
        raise TuHelperBuildError(
            "candidate enumeration must snapshot HIMEM independently and "
            "prepare normalized H.TIMI before EXTBIO")
    if data[after_candidate:after_candidate + 6] != (
        hardener_call + relocator_call
    ):
        raise TuHelperBuildError(
            "each candidate enumeration must immediately harden Pico H.TIMI "
            "and relocate its private block")
    if enumeration.count(hardener_call) != 1:
        raise TuHelperBuildError(
            "candidate loop must contain one hardener call site")
    if enumeration.count(relocator_call) != 1:
        raise TuHelperBuildError(
            "candidate loop must contain one Pico relocation call site")

    baseline = _slice(
        data, labels,
        "capture_pico_memory_baseline", "capture_pico_memory_baseline_end")
    expected_baseline = (
        _load_hl(labels["HIMEM"])
        + _store_hl(labels["pico_himem_before"])
        + bytes((0xAF,))
        + _store_a(labels["pico_relocation_applied"])
        + _store_a(labels["pico_relocation_error"])
        + bytes((0xC9,))
    )
    if baseline != expected_baseline:
        raise TuHelperBuildError(
            "Pico relocation baseline must snapshot HIMEM and clear both "
            "relocation status bytes")

    memman_wrapper = _slice(
        data, labels, "call_memman_function", "call_memman_function_end")
    expected_memman_wrapper = (
        bytes.fromhex("dd 2a")
        + _word(labels["memman_function_handler"])
        + bytes.fromhex("dd e9")
    )
    if memman_wrapper != expected_memman_wrapper:
        raise TuHelperBuildError(
            "MemMan wrapper must preserve HL by loading IX indirectly and "
            "tail-jumping through IX")

    relocation = _slice(
        data, labels,
        "relocate_pico_private_block", "relocate_pico_private_block_end")
    if BDOS_CALL in relocation:
        raise TuHelperBuildError(
            "Pico private-block relocation contains a forbidden DOS call")

    # A zero delta is safe only when FD3E is no longer adjacent to HIMEM,
    # which is the idempotent already-relocated layout. If the pointer still
    # starts at HIMEM+1, its original size cannot be recovered and the helper
    # must fail closed instead of handing a dangling allocation to BASIC.
    zero_delta_start = bytes.fromhex("7d b7 20")
    if relocation.count(zero_delta_start) != 1:
        raise TuHelperBuildError(
            "Pico relocation must distinguish a zero per-candidate HIMEM "
            "delta")
    zero_offset = relocation.index(zero_delta_start)
    measured_branch = zero_offset + 2
    measured_displacement = relocation[measured_branch + 1]
    if measured_displacement >= 0x80:
        measured_displacement -= 0x100
    measured_target = (
        labels["relocate_pico_private_block"] + measured_branch + 2
        + measured_displacement
    ) & 0xFFFF
    if measured_target != labels["relocate_pico_have_measured_length"]:
        raise TuHelperBuildError(
            "nonzero Pico HIMEM delta does not reach measured-length path")
    zero_fallback_prefix = (
        _load_hl(labels["HIMEM"])
        + bytes((0x23,))
        + _load_de(labels["PICO_WORK_POINTER"])
        + bytes.fromhex("b7 ed 52 28")
    )
    fallback_offset = zero_offset + len(zero_delta_start) + 1
    if relocation[
        fallback_offset:fallback_offset + len(zero_fallback_prefix)
    ] != zero_fallback_prefix:
        raise TuHelperBuildError(
            "zero-delta Pico layout must reject an adjacent private pointer")
    fallback_branch = fallback_offset + len(zero_fallback_prefix) - 1
    fallback_displacement = relocation[fallback_branch + 1]
    if fallback_displacement >= 0x80:
        fallback_displacement -= 0x100
    fallback_target = (
        labels["relocate_pico_private_block"] + fallback_branch + 2
        + fallback_displacement
    ) & 0xFFFF
    if fallback_target != labels["relocate_pico_layout_failed"]:
        raise TuHelperBuildError(
            "unmeasurable adjacent Pico allocation does not fail closed")
    already_relocated = (
        bytes((0x3E, 0x01))
        + _store_a(labels["pico_relocation_applied"])
        + bytes((0xC9,))
    )
    already_offset = fallback_branch + 2
    if relocation[
        already_offset:already_offset + len(already_relocated)
    ] != already_relocated:
        raise TuHelperBuildError(
            "non-adjacent zero-delta Pico layout must be latched as already "
            "relocated")

    allocation_request = (
        _load_a(labels["pico_work_length"])
        + bytes.fromhex("6f 26 00 11")
        + _word(labels["MEMMAN_HEAP_ALLOC"])
        + _call(labels["call_memman_function"])
    )
    if relocation.count(allocation_request) != 1:
        raise TuHelperBuildError(
            "Pico relocation must issue exactly one MemMan HeapAlloc with "
            "the measured private-block length")
    allocation = relocation.index(allocation_request)
    after_allocation = allocation + len(allocation_request)

    # HeapAlloc returns HL=0 on failure. Keep the test and its branch adjacent
    # to the indirect call so a malformed image cannot proceed to LDIR.
    allocation_guard = bytes.fromhex("f3 7c b5 28")
    if (
        after_allocation + 4 >= len(relocation)
        or relocation[
            after_allocation:after_allocation + 4
        ] != allocation_guard
    ):
        raise TuHelperBuildError(
            "Pico relocation must DI and reject a zero HeapAlloc result")
    branch_address = (
        labels["relocate_pico_private_block"] + after_allocation + 3)
    displacement = relocation[after_allocation + 4]
    if displacement >= 0x80:
        displacement -= 0x100
    branch_target = (branch_address + 2 + displacement) & 0xFFFF
    if branch_target != labels["relocate_pico_heap_failed"]:
        raise TuHelperBuildError(
            "zero HeapAlloc result does not branch to the explicit failure")

    # Pin the successful transaction. The allocated address is saved before
    # the copy; the complete block is copied before FD3E is published; only
    # then may the old bytes be returned by restoring HIMEM.
    relocation_success = (
        _store_hl(labels["pico_heap_pointer"])
        + bytes((0xEB,))
        + _load_hl(labels["PICO_WORK_POINTER"])
        + _load_a(labels["pico_work_length"])
        + bytes.fromhex("4f 06 00 ed b0")
        + _load_hl(labels["pico_heap_pointer"])
        + _store_hl(labels["PICO_WORK_POINTER"])
        + _load_hl(labels["pico_himem_before"])
        + _store_hl(labels["HIMEM"])
        + bytes((0x3E, 0x01))
        + _store_a(labels["pico_relocation_applied"])
        + bytes((0xC9,))
    )
    success_start = after_allocation + 5
    if relocation[
        success_start:success_start + len(relocation_success)
    ] != relocation_success:
        raise TuHelperBuildError(
            "Pico relocation must allocate, copy, publish FD3E, and restore "
            "HIMEM in that order")

    def require_layout_failure_branch(
        prefix: bytes, opcode: int, description: str
    ) -> None:
        marker = prefix + bytes((opcode,))
        if relocation.count(marker) != 1:
            raise TuHelperBuildError(
                f"Pico relocation no longer has one {description} guard")
        marker_offset = relocation.index(marker)
        branch_offset = marker_offset + len(prefix)
        displacement_offset = branch_offset + 1
        if displacement_offset >= len(relocation):
            raise TuHelperBuildError(
                f"Pico relocation has a truncated {description} guard")
        displacement = relocation[displacement_offset]
        if displacement >= 0x80:
            displacement -= 0x100
        branch_address = (
            labels["relocate_pico_private_block"] + branch_offset)
        branch_target = (branch_address + 2 + displacement) & 0xFFFF
        if branch_target != labels["relocate_pico_layout_failed"]:
            raise TuHelperBuildError(
                f"Pico {description} guard does not reach the explicit "
                "layout failure")

    require_layout_failure_branch(
        bytes.fromhex("7c b7"), 0x20, "private-block high-byte")
    require_layout_failure_branch(
        bytes((0xFE, labels["PICO_WORK_MAX"] + 1)),
        0x30,
        "private-block maximum-length",
    )
    require_layout_failure_branch(
        bytes.fromhex("ed 52"), 0x20, "FD3E/HIMEM adjacency")

    relocation_error = labels["pico_relocation_error"]
    heap_failure = _slice(
        data, labels,
        "relocate_pico_heap_failed", "relocate_pico_layout_failed")
    expected_heap_failure = (
        bytes((0x3E, labels["ERR_NO_MEMORY"]))
        + _store_a(relocation_error)
        + bytes((0xC9,))
    )
    layout_failure = _slice(
        data, labels,
        "relocate_pico_layout_failed", "relocate_pico_private_block_end")
    expected_layout_failure = (
        bytes((0x3E, labels["ERR_INTERNAL"]))
        + _store_a(relocation_error)
        + bytes((0xC9,))
    )
    if heap_failure != expected_heap_failure:
        raise TuHelperBuildError(
            "HeapAlloc failure must explicitly publish ERR_NO_MEMORY")
    if layout_failure != expected_layout_failure:
        raise TuHelperBuildError(
            "Pico layout failure must explicitly publish ERR_INTERNAL")

    # Pin the success boundary: after the exact staged read call returns and
    # closes, execution reaches enumeration and then the in-memory TL handoff.
    success_tail = (
        _call(labels["stage_tl_exact"])
        + bytes((0xB7,))
        + _jump(labels["tu_abort"], opcode=0xC2)
        + _call(labels["capture_pico_memory_baseline"])
        + _call(labels["enumerate_tcpip_unapi"])
        + bytes((
            0x3A,
            labels["pico_relocation_error"] & 0xFF,
            labels["pico_relocation_error"] >> 8,
            0xB7,
        ))
        + _jump(labels["tu_abort"], opcode=0xC2)
        + _jump(labels["handoff_to_staged_tl"])
    )
    if data.count(success_tail) != 1:
        raise TuHelperBuildError(
            "TU entry no longer stages/closes TL before enumeration and handoff")

    handoff = _slice(
        data, labels, "handoff_to_staged_tl", "handoff_to_staged_tl_end")
    if BDOS_CALL in handoff:
        raise TuHelperBuildError("TL in-memory handoff contains a DOS call")

    # The counted command tail at 0080h is below the overlay destination and
    # TU itself must not contain an absolute Z80 store that rewrites it.
    forbidden_tail_stores = (
        bytes.fromhex("32 80 00"),  # LD (0080h),A
        bytes.fromhex("22 80 00"),  # LD (0080h),HL
        bytes.fromhex("ed 43 80 00"),  # LD (0080h),BC
        bytes.fromhex("ed 53 80 00"),  # LD (0080h),DE
        bytes.fromhex("ed 73 80 00"),  # LD (0080h),SP
    )
    if any(opcode in data for opcode in forbidden_tail_stores):
        raise TuHelperBuildError("TU.COM contains a command-tail store")


def assemble_tu_helper(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    assembler: str = "z80asm",
) -> TuHelperImage:
    """Assemble TU.COM in a temporary directory and validate it."""

    repository = repository.resolve()
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="msx-tu-helper-") as directory:
        binary = pathlib.Path(directory) / "TU.COM"
        try:
            process = subprocess.run(
                [assembler, "-L", str(source), "-o", str(binary)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise TuHelperBuildError(
                f"cannot execute assembler {assembler!r}: {exc}") from exc
        if process.returncode:
            diagnostics = (process.stderr or process.stdout).strip()
            raise TuHelperBuildError(
                f"assembler failed for {source}: {diagnostics}")
        try:
            data = binary.read_bytes()
        except OSError as exc:
            raise TuHelperBuildError(
                f"assembler did not emit {binary}: {exc}") from exc
        labels = parse_labels(process.stdout + "\n" + process.stderr)
        validate_tu_helper_image(data, labels)
        return TuHelperImage(data=data, labels=labels)


def build_tu_helper(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    output: pathlib.Path = DEFAULT_OUTPUT,
    assembler: str = "z80asm",
) -> TuHelperImage:
    """Build, validate, and atomically publish ``TU.COM``."""

    image = assemble_tu_helper(repository, source, assembler)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(image.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return image


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="build the MSX-AI pre-TL TCP/IP UNAPI helper")
    parser.add_argument("--repository", type=pathlib.Path, default=REPOSITORY)
    parser.add_argument("--assembler", default="z80asm")
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args(argv)

    try:
        image = build_tu_helper(
            arguments.repository,
            arguments.source,
            arguments.output,
            arguments.assembler,
        )
    except TuHelperBuildError as exc:
        parser.error(str(exc))
    print(
        f"Built {arguments.output} ({len(image.data)} bytes, "
        f"sha256={image.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
