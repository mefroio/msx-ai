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
    "harden_pico_htimi",
    "harden_pico_htimi_end",
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
    "UNAPI_ARGUMENT": 0xF847,
    "UNAPI_EXTBIO_MAGIC": 0x2222,
    "H_TIMI": 0xFD9F,
    "CALLF_OPCODE": 0xF7,
    "PICO_TIMI_ENTRY_LOW": 0xB8,
    "PICO_TIMI_ENTRY_HIGH": 0x4C,
    "RET_OPCODE": 0xC9,
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
    if data[candidate_loop:candidate].count(preparer_call) != 1:
        raise TuHelperBuildError(
            "candidate enumeration must prepare normalized H.TIMI before "
            "EXTBIO")
    if data[after_candidate:after_candidate + 3] != hardener_call:
        raise TuHelperBuildError(
            "each candidate enumeration must immediately harden Pico H.TIMI")
    if enumeration.count(hardener_call) != 1:
        raise TuHelperBuildError(
            "candidate loop must contain one hardener call site")

    # Pin the success boundary: after the exact staged read call returns and
    # closes, execution reaches enumeration and then the in-memory TL handoff.
    success_tail = (
        _call(labels["stage_tl_exact"])
        + bytes((0xB7,))
        + _jump(labels["tu_abort"], opcode=0xC2)
        + _call(labels["enumerate_tcpip_unapi"])
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
