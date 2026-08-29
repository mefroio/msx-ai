#!/usr/bin/env python3
"""Build and audit the non-persistent BaDCaT/ZiModem initializer."""

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
DEFAULT_SOURCE = REPOSITORY / "agent" / "msx_badcat_init.asm"
DEFAULT_OUTPUT = REPOSITORY / "work" / "agent" / "BADINIT.COM"
ORIGIN = 0x0100
PAGE_1_START = 0x4000
LABEL_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*equ\s+\$([0-9A-Fa-f]+)\s*$")

REQUIRED_CONSTANTS = {
    "UART_DIVISOR_57600": 2,
    "UART_DIVISOR_115200": 1,
    "UART_DATA": 0x80,
    "UART_IER": 0x81,
    "UART_FCR": 0x82,
    "UART_LCR": 0x83,
    "UART_MCR": 0x84,
    "UART_LSR": 0x85,
    "BAUD_SETTLE_TICKS": 40,
    "LISTENER_SETTLE_TICKS": 4,
    "RESPONSE_QUIET_TICKS": 2,
    "RESPONSE_STREAM_LIMIT": 1024,
    "LSR_ERROR_MASK": 0x9E,
    "RESPONSE_LINE_ERROR": 4,
    "RESPONSE_STREAM_ERROR": 5,
}
COMMANDS = {
    "command_bootstrap": b"ATQ0V1E0R1F0\0",
    "command_n0": b"ATN0\0",
    "command_s62_0": b"ATS62=0\0",
    "command_s63_0": b"ATS63=0\0",
    "command_s0_1": b"ATS0=1\0",
    "command_b57600": b"ATQ1B57600\0",
    "command_b115200": b"ATQ1B115200\0",
    "command_i2": b"ATI2\0",
    "command_listener_open": b"ATQ0S41=0A6603\0",
    "command_stream_commit": b"ATHS41=1Q1\0",
    "command_visible": b"ATQ0V1E1R1F0\0",
}
INITIAL_COMMANDS = (
    "command_n0",
    "command_s62_0",
    "command_s63_0",
    "command_s0_1",
)
LISTENER_COMMANDS = ("command_i2",)

# These operations can write, reload, reset, or delete persistent firmware
# state.  They are forbidden anywhere in the deployable COM image.
FORBIDDEN_COMMAND_FRAGMENTS = (
    b"AT&",
    b"AT+",
    b"ATZ",
    b"ATS60",
)

REQUIRED_LABELS = (
    "badinit_start",
    "badinit_end",
    "parse_command_line",
    "find_resident_agent",
    "uart_init_57600",
    "uart_set_baud",
    "uart_wait_empty",
    "wait_ticks",
    "synchronize_command_mode",
    "change_runtime_baud",
    "restore_visible_57600",
    "run_visible_command",
    "response_drain_fifo",
    "diagnostic_report_once",
    "badinit_listener_commit_failed",
    "response_buffer",
    "response_buffer_end",
    "selected_divisor",
    "current_divisor",
    "initial_command_table",
    "listener_command_table",
    *COMMANDS,
)


class BadcatInitBuildError(ValueError):
    """The assembled initializer does not satisfy its safety contract."""


@dataclasses.dataclass(frozen=True)
class BadcatInitImage:
    """A validated COM image and its assembler labels."""

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
            raise BadcatInitBuildError(
                f"assembler emitted duplicate label {name!r}")
        labels[name] = int(hexadecimal, 16)
    return labels


def _offset(labels: Mapping[str, int], name: str) -> int:
    return labels[name] - ORIGIN


def _read_c_string(data: bytes, labels: Mapping[str, int], name: str) -> bytes:
    start = _offset(labels, name)
    if not 0 <= start < len(data):
        raise BadcatInitBuildError(f"{name} is outside the COM image")
    end = data.find(b"\0", start)
    if end < 0:
        raise BadcatInitBuildError(f"{name} is not NUL terminated")
    return data[start:end + 1]


def _read_word(data: bytes, offset: int) -> int:
    if not 0 <= offset <= len(data) - 2:
        raise BadcatInitBuildError("command table extends outside COM image")
    return data[offset] | data[offset + 1] << 8


def _validate_command_table(
    data: bytes,
    labels: Mapping[str, int],
    table_name: str,
    command_names: tuple[str, ...],
) -> None:
    offset = _offset(labels, table_name)
    expected = tuple(labels[name] for name in command_names) + (0,)
    actual = tuple(
        _read_word(data, offset + index * 2)
        for index in range(len(expected))
    )
    if actual != expected:
        raise BadcatInitBuildError(
            f"{table_name} is {actual!r}, expected {expected!r}")


def validate_badcat_init_image(
    data: bytes, labels: Mapping[str, int]
) -> None:
    """Audit the binary layout, command transcript, and persistence policy."""

    missing = [name for name in REQUIRED_LABELS if name not in labels]
    missing.extend(name for name in REQUIRED_CONSTANTS if name not in labels)
    if missing:
        raise BadcatInitBuildError(
            "assembler label output is missing: " + ", ".join(missing))
    if labels["badinit_start"] != ORIGIN:
        raise BadcatInitBuildError("badinit_start must be COM origin 0x0100")
    if labels["badinit_end"] != ORIGIN + len(data):
        raise BadcatInitBuildError(
            "badinit_end does not match the assembled image length")
    if not data:
        raise BadcatInitBuildError("assembler emitted an empty COM image")
    if labels["badinit_end"] > PAGE_1_START:
        raise BadcatInitBuildError("BADINIT.COM extends into page 1")

    for name, expected in REQUIRED_CONSTANTS.items():
        if labels[name] != expected:
            raise BadcatInitBuildError(
                f"{name} is 0x{labels[name]:04X}, expected 0x{expected:04X}")

    for name in ("selected_divisor", "current_divisor"):
        offset = _offset(labels, name)
        if data[offset:offset + 1] != bytes((2,)):
            raise BadcatInitBuildError(
                f"{name} must initialize to the safe 57600-baud divisor 2")

    for name, expected in COMMANDS.items():
        actual = _read_c_string(data, labels, name)
        if actual != expected:
            raise BadcatInitBuildError(
                f"{name} is {actual!r}, expected {expected!r}")

    _validate_command_table(
        data, labels, "initial_command_table", INITIAL_COMMANDS)
    _validate_command_table(
        data, labels, "listener_command_table", LISTENER_COMMANDS)

    uppercase = data.upper()
    for fragment in FORBIDDEN_COMMAND_FRAGMENTS:
        if fragment in uppercase:
            raise BadcatInitBuildError(
                f"forbidden persistent command fragment in image: "
                f"{fragment.decode('ascii')}")


def assemble_badcat_init(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    assembler: str = "z80asm",
) -> BadcatInitImage:
    """Assemble in a temporary directory and validate before publication."""

    repository = repository.resolve()
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="msx-badcat-init-") as directory:
        binary = pathlib.Path(directory) / "BADINIT.COM"
        try:
            process = subprocess.run(
                [assembler, "-L", str(source), "-o", str(binary)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise BadcatInitBuildError(
                f"cannot execute assembler {assembler!r}: {exc}") from exc
        if process.returncode:
            diagnostics = (process.stderr or process.stdout).strip()
            raise BadcatInitBuildError(
                f"assembler failed for {source}: {diagnostics}")
        try:
            data = binary.read_bytes()
        except OSError as exc:
            raise BadcatInitBuildError(
                f"assembler did not emit {binary}: {exc}") from exc
        labels = parse_labels(process.stdout + "\n" + process.stderr)
        validate_badcat_init_image(data, labels)
        return BadcatInitImage(data=data, labels=labels)


def build_badcat_init(
    repository: pathlib.Path = REPOSITORY,
    source: pathlib.Path = DEFAULT_SOURCE,
    output: pathlib.Path = DEFAULT_OUTPUT,
    assembler: str = "z80asm",
) -> BadcatInitImage:
    """Build, validate, and atomically publish ``BADINIT.COM``."""

    image = assemble_badcat_init(repository, source, assembler)
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
        description="build the non-persistent BaDCaT/ZiModem initializer")
    parser.add_argument("--assembler", default="z80asm")
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    try:
        image = build_badcat_init(
            REPOSITORY, arguments.source, arguments.output,
            arguments.assembler)
    except BadcatInitBuildError as exc:
        parser.error(str(exc))
    print(
        f"Built {arguments.output} ({len(image.data)} bytes, "
        f"sha256={image.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
