#!/usr/bin/env python3
"""Build and validate the standalone MSX TCP/IP UNAPI physical probe."""

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
DEFAULT_SOURCE = REPOSITORY / "agent" / "msx_unapi_probe.asm"
DEFAULT_OUTPUT = REPOSITORY / "work" / "agent" / "UNAPIPRB.COM"
ORIGIN = 0x0100
PAGE_1_START = 0x4000
DEFAULT_LISTENER_PORT = 6603
TCP_OPEN_PARAMS = bytes.fromhex(
    "00 00 00 00 00 00 cb 19 00 00 03 00 00")
API_ID = b"TCP/IP\0"
LABEL_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*equ\s+\$([0-9A-Fa-f]+)\s*$")
REQUIRED_LABELS = (
    "probe_start",
    "probe_end",
    "parse_port_argument",
    "select_compatible_implementation",
    "implementation_is_callable",
    "implementation_index",
    "api_id",
    "api_id_end",
    "tcp_open_params",
    "tcp_open_params_end",
    "listener_port",
    "local_ip",
    "tcp_info",
)
REQUIRED_CONSTANTS = {
    "UNAPI_EXTBIO_MAGIC": 0x2222,
    "ERR_NOT_IMP": 0x01,
    "DEFAULT_LISTENER_PORT": DEFAULT_LISTENER_PORT,
    "PASSIVE_RESIDENT_FLAGS": 0x03,
    "PASSIVE_ANY_CAPABILITY": 0x20,
    "TCPIP_GET_CAPAB": 0x01,
    "TCPIP_GET_IPINFO": 0x02,
    "TCPIP_NET_STATE": 0x03,
    "TCPIP_TCP_OPEN": 0x0D,
    "TCPIP_TCP_ABORT": 0x0F,
    "TCPIP_TCP_STATE": 0x10,
    "TCPIP_WAIT": 0x1D,
}


class UnapiProbeBuildError(ValueError):
    """The assembled image does not satisfy the physical-probe contract."""


@dataclasses.dataclass(frozen=True)
class ProbeImage:
    """A validated COM image and the assembler labels used to inspect it."""

    data: bytes
    labels: Mapping[str, int]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def parse_labels(text: str) -> dict[str, int]:
    """Parse labels emitted by z80asm 1.8's ``-L`` option."""

    labels: dict[str, int] = {}
    for line in text.splitlines():
        match = LABEL_LINE.fullmatch(line)
        if match is None:
            continue
        name, hexadecimal = match.groups()
        if name in labels:
            raise UnapiProbeBuildError(
                f"assembler emitted duplicate label {name!r}")
        labels[name] = int(hexadecimal, 16)
    return labels


def _slice(data: bytes, labels: Mapping[str, int], start: str,
           end: str) -> bytes:
    first = labels[start] - ORIGIN
    last = labels[end] - ORIGIN
    if not 0 <= first <= last <= len(data):
        raise UnapiProbeBuildError(
            f"invalid image range {start}..{end}: {first}..{last}")
    return data[first:last]


def validate_probe_image(data: bytes, labels: Mapping[str, int]) -> None:
    """Cross-check layout and the exact UNAPI passive-open wire contract."""

    missing = [name for name in REQUIRED_LABELS if name not in labels]
    missing.extend(name for name in REQUIRED_CONSTANTS if name not in labels)
    if missing:
        raise UnapiProbeBuildError(
            "assembler label output is missing: " + ", ".join(missing))

    if labels["probe_start"] != ORIGIN:
        raise UnapiProbeBuildError("probe_start must be the COM origin 0x0100")
    if labels["probe_end"] != ORIGIN + len(data):
        raise UnapiProbeBuildError(
            "probe_end does not match the assembled image length")
    if not data:
        raise UnapiProbeBuildError("assembler emitted an empty COM image")
    if labels["probe_end"] > PAGE_1_START:
        raise UnapiProbeBuildError(
            "probe extends into page 1; UNAPI exchange buffers must remain "
            "in page 0")

    for name, expected in REQUIRED_CONSTANTS.items():
        if labels[name] != expected:
            raise UnapiProbeBuildError(
                f"{name} is 0x{labels[name]:04X}, expected 0x{expected:04X}")

    if _slice(data, labels, "api_id", "api_id_end") != API_ID:
        raise UnapiProbeBuildError("UNAPI identifier is not exactly TCP/IP\\0")
    actual_params = _slice(
        data, labels, "tcp_open_params", "tcp_open_params_end")
    if actual_params != TCP_OPEN_PARAMS:
        raise UnapiProbeBuildError(
            "TCP_OPEN block must be 0.0.0.0, remote port 0, default local "
            "port 6603, timeout 0, flags 0x03, TLS name pointer 0")

    listener_offset = labels["listener_port"] - ORIGIN
    if data[listener_offset:listener_offset + 2] != bytes((0xCB, 0x19)):
        raise UnapiProbeBuildError(
            "runtime listener_port does not start with default 6603")
    if not ORIGIN <= labels["parse_port_argument"] < labels["probe_end"]:
        raise UnapiProbeBuildError("port parser is outside the COM image")

    for name, size in (("local_ip", 4), ("tcp_info", 8)):
        address = labels[name]
        if not (ORIGIN <= address and address + size <= PAGE_1_START):
            raise UnapiProbeBuildError(
                f"UNAPI exchange buffer {name} is not wholly in page 0")


def assemble_probe(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    assembler: str = "z80asm",
) -> ProbeImage:
    """Assemble the source in a temporary directory and validate the result."""

    repository = repository.resolve()
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="msx-unapi-probe-") as directory:
        binary = pathlib.Path(directory) / "UNAPIPRB.COM"
        try:
            process = subprocess.run(
                [assembler, "-L", str(source), "-o", str(binary)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise UnapiProbeBuildError(
                f"cannot execute assembler {assembler!r}: {exc}") from exc
        if process.returncode:
            diagnostics = (process.stderr or process.stdout).strip()
            raise UnapiProbeBuildError(
                f"assembler failed for {source}: {diagnostics}")
        try:
            data = binary.read_bytes()
        except OSError as exc:
            raise UnapiProbeBuildError(
                f"assembler did not emit {binary}: {exc}") from exc
        labels = parse_labels(process.stdout + "\n" + process.stderr)
        validate_probe_image(data, labels)
        return ProbeImage(data=data, labels=labels)


def build_probe(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    output: pathlib.Path = DEFAULT_OUTPUT,
    assembler: str = "z80asm",
) -> ProbeImage:
    """Build, validate and atomically publish ``UNAPIPRB.COM``."""

    image = assemble_probe(repository, source, assembler)
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
        description="build the standalone MSX TCP/IP UNAPI listener probe")
    parser.add_argument("--assembler", default="z80asm")
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    try:
        image = build_probe(
            REPOSITORY, arguments.source, arguments.output,
            arguments.assembler)
    except UnapiProbeBuildError as exc:
        parser.error(str(exc))
    print(
        f"Built {arguments.output} ({len(image.data)} bytes, "
        f"sha256={image.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
