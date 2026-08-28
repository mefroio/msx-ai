#!/usr/bin/env python3
"""Build and independently cross-check the relocatable MSX-AI MemMan TSR."""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from tools.build_memman_tsr import (  # noqa: E402
    HEADER_SIZE,
    BuildError,
    BuildSpec,
    Hook,
    build_memman_tsr,
    infer_relocations,
)
from tools.build_version_include import materialize_version_include  # noqa: E402


TSR_NAME = "MSXAI MCP1"
# Keep all synthetic link origins low enough that the largest supported TSR
# still fits in page 1.  They only need to be distinct to cross-check inferred
# relocations; high origins unnecessarily reduced the builder's size ceiling.
BUILD_ORIGINS = (0x4024, 0x44B5, 0x4946)
H_KEYI = 0xFD9A
H_TIMI = 0xFD9F
H_CHPU = 0xFDA4
H_CHGE = 0xFDC2
H_CRUN = 0xFF20
TRANSPORT_TEMPLATE = 0xFE
TRANSPORT_8251 = 0
TRANSPORT_16C550 = 1
TRANSPORT_UNAPI = 2
LABEL_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*equ\s+\$([0-9A-Fa-f]+)\s*$")
REQUIRED_LABELS = (
    "resident_start",
    "active_transport_id",
    "resident_keyi_hook",
    "resident_timi_hook",
    "resident_console_put_hook",
    "resident_console_get_hook",
    "resident_basic_crunch_hook",
    "tsr_kill",
    "tsr_talk",
    "resident_end",
    "tsr_init",
    "tsr_init_end",
)


class AgentTsrBuildError(ValueError):
    """The assembly outputs cannot safely produce the resident TSR."""


@dataclasses.dataclass(frozen=True)
class LinkedImage:
    origin: int
    data: bytes
    labels: Mapping[str, int]


@dataclasses.dataclass(frozen=True)
class AgentTsrOutputs:
    tsr_path: pathlib.Path
    metadata_path: pathlib.Path
    driver_8251_path: pathlib.Path
    driver_16c550_path: pathlib.Path
    driver_unapi_path: pathlib.Path
    size: int
    transport_file_offset: int
    relocation_offsets: tuple[int, ...]


def parse_labels(text: str) -> dict[str, int]:
    """Parse the label stream emitted by z80asm 1.8's ``-L`` option."""

    labels: dict[str, int] = {}
    for line in text.splitlines():
        match = LABEL_LINE.fullmatch(line)
        if match is None:
            continue
        name, hexadecimal = match.groups()
        if name in labels:
            raise AgentTsrBuildError(
                f"assembler emitted duplicate label {name!r}")
        labels[name] = int(hexadecimal, 16)
    return labels


def _wrapper(origin: int, development_trace: bool = False) -> str:
    return (
        "; Generated temporarily by tools/build_agent_tsr.py.\n"
        "MSXAI_TSR_BUILD: equ 1\n"
        f"MSXAI_DEVELOPMENT_TRACE: equ {int(development_trace)}\n"
        "TRANSPORT_STATE_SIZE: equ 5\n"
        f"TSR_BUILD_BASE: equ 0{origin:04X}h\n"
        "include 'agent/msx_agent_core.asm'\n"
    )


def _assemble(
        repository: pathlib.Path, temporary_dir: pathlib.Path,
        assembler: str, origin: int, development_trace: bool = False,
) -> LinkedImage:
    wrapper = temporary_dir / f"msxai_tsr_{origin:04x}.asm"
    binary = temporary_dir / f"msxai_tsr_{origin:04x}.bin"
    wrapper.write_text(
        _wrapper(origin, development_trace=development_trace),
        encoding="ascii")
    try:
        process = subprocess.run(
            [assembler, "-L", str(wrapper), "-o", str(binary)],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise AgentTsrBuildError(
            f"cannot execute assembler {assembler!r}: {exc}") from exc
    if process.returncode:
        diagnostics = (process.stderr or process.stdout).strip()
        raise AgentTsrBuildError(
            f"assembler failed for origin 0x{origin:04X}: {diagnostics}")
    try:
        data = binary.read_bytes()
    except OSError as exc:
        raise AgentTsrBuildError(
            f"assembler did not emit {binary}: {exc}") from exc
    labels = parse_labels(process.stdout + "\n" + process.stderr)
    missing = [name for name in REQUIRED_LABELS if name not in labels]
    if missing:
        raise AgentTsrBuildError(
            "assembler label output is missing: " + ", ".join(missing))
    return LinkedImage(origin, data, labels)


def _check_linked_images(images: Sequence[LinkedImage]) -> dict[str, int]:
    if len(images) != 3:
        raise AgentTsrBuildError("exactly three linked images are required")

    primary = images[0]
    if primary.labels["resident_start"] != primary.origin:
        raise AgentTsrBuildError(
            "resident_start does not match the requested primary origin")
    if primary.labels.get("H_KEYI") != H_KEYI:
        raise AgentTsrBuildError("assembly H_KEYI does not match 0xFD9A")
    if primary.labels.get("H_TIMI") != H_TIMI:
        raise AgentTsrBuildError("assembly H_TIMI does not match 0xFD9F")
    if primary.labels.get("H_CHPU") != H_CHPU:
        raise AgentTsrBuildError("assembly H_CHPU does not match 0xFDA4")
    if primary.labels.get("H_CHGE") != H_CHGE:
        raise AgentTsrBuildError("assembly H_CHGE does not match 0xFDC2")
    if primary.labels.get("H_CRUN") != H_CRUN:
        raise AgentTsrBuildError("assembly H_CRUN does not match 0xFF20")
    if not primary.data:
        raise AgentTsrBuildError("assembler emitted an empty TSR payload")

    sizes = {len(image.data) for image in images}
    if len(sizes) != 1:
        raise AgentTsrBuildError(
            "TSR payload size changes with the link origin: "
            + ", ".join(str(len(image.data)) for image in images))

    if primary.labels["tsr_init"] != primary.labels["resident_end"]:
        raise AgentTsrBuildError("tsr_init must begin exactly at resident_end")
    if primary.labels["tsr_init_end"] - primary.origin != len(primary.data):
        raise AgentTsrBuildError(
            "assembled payload does not end exactly at tsr_init_end")

    primary_end = primary.origin + len(primary.data)
    relative_labels = {
        name: value - primary.origin
        for name, value in primary.labels.items()
        if primary.origin <= value <= primary_end
    }
    for required in REQUIRED_LABELS:
        relative_labels.setdefault(
            required, primary.labels[required] - primary.origin)

    for image in images[1:]:
        if image.labels["resident_start"] != image.origin:
            raise AgentTsrBuildError(
                f"resident_start does not match origin 0x{image.origin:04X}")
        if image.origin + len(image.data) > 0x8000:
            raise AgentTsrBuildError(
                f"payload at 0x{image.origin:04X} extends beyond page 1")
        for name, expected_offset in relative_labels.items():
            if name not in image.labels:
                raise AgentTsrBuildError(
                    f"origin 0x{image.origin:04X} is missing label {name}")
            actual_offset = image.labels[name] - image.origin
            if actual_offset != expected_offset:
                raise AgentTsrBuildError(
                    f"label {name} is not origin-invariant: expected offset "
                    f"0x{expected_offset:04X}, got 0x{actual_offset:04X} at "
                    f"origin 0x{image.origin:04X}")

    offsets = {
        name: primary.labels[name] - primary.origin
        for name in REQUIRED_LABELS
    }
    code_length = offsets["resident_end"]
    for name in (
            "active_transport_id", "resident_keyi_hook",
            "resident_timi_hook", "resident_console_put_hook",
            "resident_console_get_hook", "resident_basic_crunch_hook",
            "tsr_kill", "tsr_talk"):
        if not 0 <= offsets[name] < code_length:
            raise AgentTsrBuildError(
                f"{name} must point inside the resident-code section")

    transport_offset = offsets["active_transport_id"]
    if primary.data[transport_offset] != TRANSPORT_TEMPLATE:
        raise AgentTsrBuildError(
            "active_transport_id does not contain the expected 0xFE "
            "loader-patch template byte")
    return offsets


def _assembly_number(value: int) -> str:
    return f"0{value:04X}h"


def _metadata(size: int, transport_file_offset: int) -> bytes:
    return (
        "; Generated by tools/build_agent_tsr.py; do not edit.\n"
        f"MSXAI_TSR_SIZE: equ {_assembly_number(size)}\n"
        "MSXAI_TSR_TRANSPORT_OFFSET: equ "
        f"{_assembly_number(transport_file_offset)}\n"
    ).encode("ascii")


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.",
                delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = pathlib.Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_agent_tsr(
        repository: pathlib.Path, output: pathlib.Path,
        metadata_output: pathlib.Path, assembler: str = "z80asm",
        driver_8251_output: pathlib.Path | None = None,
        driver_16c550_output: pathlib.Path | None = None,
        driver_unapi_output: pathlib.Path | None = None,
        development_trace: bool = False,
) -> AgentTsrOutputs:
    """Build, cross-check, and emit the template plus fixed-driver TSRs."""

    repository = repository.resolve()
    materialize_version_include(repository)
    with tempfile.TemporaryDirectory(prefix="msxai-tsr-") as directory:
        temporary_dir = pathlib.Path(directory)
        images = tuple(
            _assemble(
                repository, temporary_dir, assembler, origin,
                development_trace=development_trace)
            for origin in BUILD_ORIGINS)

    offsets = _check_linked_images(images)
    primary, secondary, tertiary = images
    try:
        relocations_ab = infer_relocations(
            primary.data, secondary.data, primary.origin,
            secondary.origin - primary.origin)
        relocations_ac = infer_relocations(
            primary.data, tertiary.data, primary.origin,
            tertiary.origin - primary.origin)
    except BuildError as exc:
        raise AgentTsrBuildError(str(exc)) from exc
    if relocations_ab != relocations_ac:
        raise AgentTsrBuildError(
            "relocation inference differs across the two independent "
            "origin deltas")

    try:
        result = build_memman_tsr(
            primary.data,
            secondary.data,
            BuildSpec(
                name=TSR_NAME,
                origin=primary.origin,
                delta=secondary.origin - primary.origin,
                code_length=offsets["resident_end"],
                kill_offset=offsets["tsr_kill"],
                talk_offset=offsets["tsr_talk"],
                hooks=(
                    Hook(H_KEYI, offsets["resident_keyi_hook"]),
                    Hook(H_TIMI, offsets["resident_timi_hook"]),
                    Hook(H_CHPU, offsets["resident_console_put_hook"]),
                    Hook(H_CHGE, offsets["resident_console_get_hook"]),
                    Hook(H_CRUN, offsets["resident_basic_crunch_hook"]),
                ),
                record_size=128,
                expected_relocations=relocations_ab,
            ),
        )
    except BuildError as exc:
        raise AgentTsrBuildError(str(exc)) from exc

    relocation_table_length = 2 + 2 * len(result.relocation_offsets)
    transport_file_offset = (
        HEADER_SIZE + relocation_table_length
        + offsets["active_transport_id"])
    if result.data[transport_file_offset] != TRANSPORT_TEMPLATE:
        raise AgentTsrBuildError(
            "computed transport patch offset does not address its template")

    if driver_8251_output is None:
        driver_8251_output = output.with_name("MCP8251.TSR")
    if driver_16c550_output is None:
        driver_16c550_output = output.with_name("MCP16550.TSR")
    if driver_unapi_output is None:
        driver_unapi_output = output.with_name("MCPUNAPI.TSR")
    if len({output.resolve(), driver_8251_output.resolve(),
            driver_16c550_output.resolve(),
            driver_unapi_output.resolve()}) != 4:
        raise AgentTsrBuildError(
            "template and fixed-driver TSR outputs must be distinct")

    driver_8251 = bytearray(result.data)
    driver_8251[transport_file_offset] = TRANSPORT_8251
    driver_16c550 = bytearray(result.data)
    driver_16c550[transport_file_offset] = TRANSPORT_16C550
    driver_unapi = bytearray(result.data)
    driver_unapi[transport_file_offset] = TRANSPORT_UNAPI
    differing = tuple(
        index for index, values in enumerate(
            zip(driver_8251, driver_16c550, driver_unapi, strict=True))
        if len(set(values)) != 1)
    if differing != (transport_file_offset,):
        raise AgentTsrBuildError(
            "fixed-driver TSRs must differ only at the transport byte")

    metadata = _metadata(len(result.data), transport_file_offset)
    _atomic_write(output, result.data)
    _atomic_write(metadata_output, metadata)
    _atomic_write(driver_8251_output, bytes(driver_8251))
    _atomic_write(driver_16c550_output, bytes(driver_16c550))
    _atomic_write(driver_unapi_output, bytes(driver_unapi))
    return AgentTsrOutputs(
        output, metadata_output, driver_8251_output, driver_16c550_output,
        driver_unapi_output, len(result.data), transport_file_offset,
        result.relocation_offsets)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble the MSX-AI resident core at three page-1 origins and "
            "build a cross-checked MemMan v3 TSR."))
    parser.add_argument("--repository", type=pathlib.Path, default=REPOSITORY)
    parser.add_argument(
        "--output", "-o", type=pathlib.Path,
        default=REPOSITORY / "work" / "agent" / "build" / "MSXAI.TSR")
    parser.add_argument(
        "--metadata-output", type=pathlib.Path,
        default=(REPOSITORY / "work" / "agent" / "build" /
                 "MSXAI_TSR.INC"))
    parser.add_argument(
        "--8251-output", dest="driver_8251_output", type=pathlib.Path,
        default=REPOSITORY / "work" / "agent" / "MCP8251.TSR")
    parser.add_argument(
        "--16c550-output", dest="driver_16c550_output", type=pathlib.Path,
        default=REPOSITORY / "work" / "agent" / "MCP16550.TSR")
    parser.add_argument(
        "--unapi-output", dest="driver_unapi_output", type=pathlib.Path,
        default=REPOSITORY / "work" / "agent" / "MCPUNAPI.TSR")
    parser.add_argument(
        "--assembler", default=os.environ.get("Z80ASM", "z80asm"))
    parser.add_argument(
        "--development-trace", action="store_true",
        help="expose the private TRACE/DUMPTRACE development interfaces")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        outputs = build_agent_tsr(
            args.repository, args.output, args.metadata_output,
            args.assembler, args.driver_8251_output,
            args.driver_16c550_output, args.driver_unapi_output,
            development_trace=args.development_trace)
    except (AgentTsrBuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"wrote {outputs.tsr_path}: {outputs.size} bytes, "
        f"{len(outputs.relocation_offsets)} relocation(s); transport patch "
        f"offset 0x{outputs.transport_file_offset:04X}")
    print(f"wrote {outputs.metadata_path}")
    print(f"wrote {outputs.driver_8251_path}: transport 8251")
    print(f"wrote {outputs.driver_16c550_path}: transport 16C550")
    print(f"wrote {outputs.driver_unapi_path}: transport UNAPI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
