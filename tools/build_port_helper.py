#!/usr/bin/env python3
"""Build and cross-check the MSX-AI first-install port helper."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
from collections.abc import Mapping


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY / "agent" / "msx_port_helper.asm"
DEFAULT_OUTPUT = REPOSITORY / "work" / "agent" / "MP.COM"
ORIGIN = 0x0100
PAGE_1_START = 0x4000
TSR_NAME = b"MSXAI MCP1  "
LABEL_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*equ\s+\$([0-9A-Fa-f]+)\s*$")
REQUIRED_LABELS = (
    "port_helper_start",
    "port_helper_end",
    "parse_port_argument",
    "find_memman_agent",
    "memman_tsr_name",
    "memman_tsr_name_end",
    "unapi_request",
    "unapi_request_port",
    "unapi_request_stack_bottom",
    "unapi_request_stack_top",
    "unapi_request_status",
    "unapi_request_error",
    "unapi_request_transport",
    "unapi_request_connection",
    "unapi_request_target",
    "unapi_request_reserved",
    "port_value",
)
REQUIRED_CONSTANTS = {
    "EXTBIO": 0xFFCA,
    "DOS_VERSION": 0x6F,
    "MEMMAN_INICHK": 30,
    "MEMMAN_GET_TSR_ID": 62,
    "MEMMAN_TSR_CALL": 63,
    "MSXAI_TALK_UNAPI_PORT": 0xA7,
    "MSXAI_TRANSPORT_UNAPI": 2,
    "MSXAI_UNAPI_REQUEST_MAGIC": 0xA75A,
    "MSXAI_UNAPI_REQUEST_VERSION": 1,
    "MSXAI_UNAPI_REQUEST_SIZE": 16,
    "MSXAI_UNAPI_STACK_SIZE": 0x400,
    "MSXAI_UNAPI_GUARD_SIZE": 16,
    "MSXAI_UNAPI_STACK_HEADROOM": 0x100,
    "MSXAI_UNAPI_LOW_GUARD": 0xA5,
    "MSXAI_UNAPI_HIGH_GUARD": 0x5A,
}
REQUEST_FIELD_OFFSETS = {
    "unapi_request_port": 4,
    "unapi_request_stack_bottom": 6,
    "unapi_request_stack_top": 8,
    "unapi_request_status": 10,
    "unapi_request_error": 11,
    "unapi_request_transport": 12,
    "unapi_request_connection": 13,
    "unapi_request_target": 14,
    "unapi_request_reserved": 15,
}


class PortHelperBuildError(ValueError):
    """The assembled helper violates its first-install handoff contract."""


@dataclasses.dataclass(frozen=True)
class PortHelperImage:
    """A validated COM image and its assembler labels."""

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
            raise PortHelperBuildError(
                f"assembler emitted duplicate label {name!r}")
        labels[name] = int(hexadecimal, 16)
    return labels


def _slice(data: bytes, labels: Mapping[str, int], start: str,
           end: str) -> bytes:
    first = labels[start] - ORIGIN
    last = labels[end] - ORIGIN
    if not 0 <= first <= last <= len(data):
        raise PortHelperBuildError(
            f"invalid image range {start}..{end}: {first}..{last}")
    return data[first:last]


def validate_port_helper_image(
        data: bytes, labels: Mapping[str, int]) -> None:
    """Validate layout and the exact MemMan/MSXAI identifiers."""

    missing = [name for name in REQUIRED_LABELS if name not in labels]
    missing.extend(name for name in REQUIRED_CONSTANTS if name not in labels)
    if missing:
        raise PortHelperBuildError(
            "assembler label output is missing: " + ", ".join(missing))

    if labels["port_helper_start"] != ORIGIN:
        raise PortHelperBuildError(
            "port_helper_start must be the COM origin 0x0100")
    if labels["port_helper_end"] != ORIGIN + len(data):
        raise PortHelperBuildError(
            "port_helper_end does not match the assembled image length")
    if not data:
        raise PortHelperBuildError("assembler emitted an empty COM image")
    if labels["port_helper_end"] > PAGE_1_START:
        raise PortHelperBuildError(
            "MP.COM extends into page 1 and is not a minimal COM helper")

    for name, expected in REQUIRED_CONSTANTS.items():
        if labels[name] != expected:
            raise PortHelperBuildError(
                f"{name} is 0x{labels[name]:04X}, expected 0x{expected:04X}")

    if _slice(data, labels, "memman_tsr_name",
              "memman_tsr_name_end") != TSR_NAME:
        raise PortHelperBuildError(
            "GetTsrID name is not the exact 12-byte MSXAI MCP1 identifier")

    parser = labels["parse_port_argument"]
    if not ORIGIN <= parser < labels["port_helper_end"]:
        raise PortHelperBuildError("port parser is outside MP.COM")
    value = labels["port_value"]
    if not (ORIGIN <= value and value + 2 <= labels["port_helper_end"]):
        raise PortHelperBuildError(
            "binary port value is not wholly inside MP.COM")
    request = labels["unapi_request"]
    if not (ORIGIN <= request and
            request + labels["MSXAI_UNAPI_REQUEST_SIZE"] <=
            labels["port_helper_end"]):
        raise PortHelperBuildError(
            "A7 UNAPI request is not wholly inside MP.COM page zero")

    for name, offset in REQUEST_FIELD_OFFSETS.items():
        actual = labels[name] - request
        if actual != offset:
            raise PortHelperBuildError(
                f"{name} is at A7 request offset {actual}, expected {offset}")

    first = request - ORIGIN
    last = first + labels["MSXAI_UNAPI_REQUEST_SIZE"]
    request_data = data[first:last]
    expected_header = bytes((
        labels["MSXAI_UNAPI_REQUEST_MAGIC"] & 0xFF,
        labels["MSXAI_UNAPI_REQUEST_MAGIC"] >> 8,
        labels["MSXAI_UNAPI_REQUEST_VERSION"],
        labels["MSXAI_UNAPI_REQUEST_SIZE"],
    ))
    if request_data[:4] != expected_header:
        raise PortHelperBuildError(
            "A7 request header does not contain the pinned magic, version, "
            "and size")
    target_offset = REQUEST_FIELD_OFFSETS["unapi_request_target"]
    if request_data[target_offset] != labels["MSXAI_TRANSPORT_UNAPI"]:
        raise PortHelperBuildError(
            "MP.COM A7 request target is not the UNAPI transport")
    reserved_offset = REQUEST_FIELD_OFFSETS["unapi_request_reserved"]
    if request_data[reserved_offset] != 0:
        raise PortHelperBuildError(
            "MP.COM A7 request reserved byte is not zero")


def assemble_port_helper(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    assembler: str = "z80asm",
) -> PortHelperImage:
    """Assemble MP.COM in a temporary directory and validate it."""

    repository = repository.resolve()
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="msx-port-helper-") as directory:
        binary = pathlib.Path(directory) / "MP.COM"
        try:
            process = subprocess.run(
                [assembler, "-L", str(source), "-o", str(binary)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise PortHelperBuildError(
                f"cannot execute assembler {assembler!r}: {exc}") from exc
        if process.returncode:
            diagnostics = (process.stderr or process.stdout).strip()
            raise PortHelperBuildError(
                f"assembler failed for {source}: {diagnostics}")
        try:
            data = binary.read_bytes()
        except OSError as exc:
            raise PortHelperBuildError(
                f"assembler did not emit {binary}: {exc}") from exc
        labels = parse_labels(process.stdout + "\n" + process.stderr)
        validate_port_helper_image(data, labels)
        return PortHelperImage(data=data, labels=labels)


def build_port_helper(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    output: pathlib.Path = DEFAULT_OUTPUT,
    assembler: str = "z80asm",
) -> PortHelperImage:
    """Build, validate, and atomically publish ``MP.COM``."""

    image = assemble_port_helper(repository, source, assembler)
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="build the MSX-AI first-install custom-port helper")
    parser.add_argument("--repository", type=pathlib.Path, default=REPOSITORY)
    parser.add_argument("--assembler", default="z80asm")
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    try:
        image = build_port_helper(
            arguments.repository, arguments.source, arguments.output,
            arguments.assembler)
    except PortHelperBuildError as exc:
        parser.error(str(exc))
    print(
        f"Built {arguments.output} ({len(image.data)} bytes, "
        f"sha256={image.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
