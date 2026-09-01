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
    "DEFAULT_LISTENER_PORT": 6603,
    "LISTENER_PREFIX_LENGTH": 10,
    "LISTENER_PORT_CAPACITY": 6,
    "LISTENER_COMMAND_CAPACITY": 16,
    "IPV4_TEXT_CAPACITY": 16,
    "TSR_TALK_BADCAT_DIAL": 0xB3,
    "BADCAT_DIAL_MAGIC": 0xA95A,
    "BADCAT_DIAL_VERSION": 1,
    "BADCAT_DIAL_REQUEST_SIZE": 12,
    "COMMAND_BUFFER_CAPACITY": 128,
    "UART_DATA": 0x80,
    "UART_IER": 0x81,
    "UART_FCR": 0x82,
    "UART_LCR": 0x83,
    "UART_MCR": 0x84,
    "UART_LSR": 0x85,
    "UART_MCR_RTS_OFF": 0x01,
    "UART_MCR_RTS_ON": 0x03,
    "UART_FCR_FIFO_8": 0x87,
    "UART_RTS_POLL_COUNT": 100,
    "UART_RECEIVE_EVENT_MASK": 0x9F,
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
    "command_s2_255": b"ATS2=255\0",
    "command_s0_1": b"ATS0=1\0",
    "command_b57600": b"ATQ1B57600\0",
    "command_b115200": b"ATQ1B115200\0",
    "command_i2": b"ATI2\0",
    "command_stream_commit": b"ATHS41=1Q1\0",
    "command_visible": b"ATQ0V1E1R1F0\0",
}
LISTENER_COMMAND_PREFIX = b"ATQ0S41=0A\0"
INITIAL_COMMANDS = (
    "command_n0",
    "command_s2_255",
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
    "build_listener_command",
    "build_badcat_dial_request",
    "find_resident_agent",
    "uart_init_57600",
    "uart_set_baud",
    "uart_receive_status",
    "uart_wait_empty",
    "wait_ticks",
    "synchronize_command_mode",
    "change_runtime_baud",
    "restore_visible_57600",
    "run_visible_command",
    "response_reset",
    "send_command",
    "response_drain_fifo",
    "diagnostic_report_once",
    "badinit_listener_commit_failed",
    "badinit_resident_reverse_dial",
    "response_buffer",
    "response_buffer_end",
    "selected_divisor",
    "selected_port",
    "selected_host",
    "selected_host_end",
    "current_divisor",
    "command_listener_prefix",
    "command_listener_open",
    "command_listener_port_text",
    "command_listener_open_end",
    "badcat_dial_request",
    "badcat_dial_request_status",
    "badcat_dial_request_reserved",
    "badcat_dial_request_ipv4",
    "badcat_dial_request_port",
    "badcat_dial_request_end",
    "command_buffer",
    "command_buffer_end",
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

    visible_command = _offset(labels, "run_visible_command")
    expected_entry = (
        b"\xE5\xCD"
        + labels["response_reset"].to_bytes(2, "little")
        + b"\xE1\xCD"
        + labels["send_command"].to_bytes(2, "little")
    )
    if data[visible_command:visible_command + len(expected_entry)] != expected_entry:
        raise BadcatInitBuildError(
            "run_visible_command must preserve HL across response_reset")

    for name in ("selected_divisor", "current_divisor"):
        offset = _offset(labels, name)
        if data[offset:offset + 1] != bytes((2,)):
            raise BadcatInitBuildError(
                f"{name} must initialize to the safe 57600-baud divisor 2")

    selected_port = _offset(labels, "selected_port")
    expected_port = labels["DEFAULT_LISTENER_PORT"].to_bytes(2, "little")
    if data[selected_port:selected_port + 2] != expected_port:
        raise BadcatInitBuildError(
            "selected_port must initialize to the default TCP port 6603")

    listener_start = labels["command_listener_open"]
    listener_port_text = labels["command_listener_port_text"]
    listener_end = labels["command_listener_open_end"]
    if not (
        ORIGIN <= listener_start <= listener_port_text <= listener_end
        <= ORIGIN + len(data)
    ):
        raise BadcatInitBuildError(
            "dynamic listener command buffer is outside the COM image")
    if (listener_end - listener_start
            != labels["LISTENER_COMMAND_CAPACITY"]):
        raise BadcatInitBuildError(
            "command_listener_open must reserve exactly 16 bytes")
    if (listener_port_text - listener_start
            != labels["LISTENER_PREFIX_LENGTH"]):
        raise BadcatInitBuildError(
            "command_listener_port_text must follow the 10-byte prefix")
    if (listener_end - listener_port_text
            != labels["LISTENER_PORT_CAPACITY"]):
        raise BadcatInitBuildError(
            "command_listener_port_text must reserve exactly 6 bytes")
    listener_offset = listener_start - ORIGIN
    listener_size = listener_end - listener_start
    if data[listener_offset:listener_offset + listener_size] != bytes(
            listener_size):
        raise BadcatInitBuildError(
            "dynamic listener command buffer must initialize to zero")

    host_start = labels["selected_host"]
    host_end = labels["selected_host_end"]
    if not (
        ORIGIN <= host_start <= host_end <= ORIGIN + len(data)
    ):
        raise BadcatInitBuildError(
            "selected_host is outside the COM image")
    if host_end - host_start != labels["IPV4_TEXT_CAPACITY"]:
        raise BadcatInitBuildError(
            "selected_host must reserve exactly 16 bytes")
    host_offset = host_start - ORIGIN
    if data[host_offset:host_offset + host_end - host_start] != bytes(
            host_end - host_start):
        raise BadcatInitBuildError(
            "selected_host must initialize to zero")

    request_start = labels["badcat_dial_request"]
    request_status = labels["badcat_dial_request_status"]
    request_reserved = labels["badcat_dial_request_reserved"]
    request_ipv4 = labels["badcat_dial_request_ipv4"]
    request_port = labels["badcat_dial_request_port"]
    request_end = labels["badcat_dial_request_end"]
    if not (
        ORIGIN <= request_start <= request_status <= request_reserved
        <= request_ipv4 <= request_port <= request_end
        <= ORIGIN + len(data)
    ):
        raise BadcatInitBuildError(
            "binary BaDCaT dial request is outside the COM image")
    if request_end - request_start != labels["BADCAT_DIAL_REQUEST_SIZE"]:
        raise BadcatInitBuildError(
            "badcat_dial_request must reserve exactly 12 bytes")
    expected_request_offsets = (
        request_status - request_start,
        request_reserved - request_start,
        request_ipv4 - request_start,
        request_port - request_start,
    )
    if expected_request_offsets != (4, 5, 6, 10):
        raise BadcatInitBuildError(
            "badcat_dial_request field offsets do not match B3 ABI")
    request_offset = request_start - ORIGIN
    expected_request = (
        labels["BADCAT_DIAL_MAGIC"].to_bytes(2, "little")
        + bytes((labels["BADCAT_DIAL_VERSION"],
                 labels["BADCAT_DIAL_REQUEST_SIZE"], 0xFF, 0))
        + bytes(4)
        + labels["DEFAULT_LISTENER_PORT"].to_bytes(2, "little")
    )
    if data[request_offset:request_offset + len(expected_request)] != (
            expected_request):
        raise BadcatInitBuildError(
            "badcat_dial_request has unsafe or incompatible defaults")

    command_buffer_start = labels["command_buffer"]
    command_buffer_end = labels["command_buffer_end"]
    if not (
        ORIGIN <= command_buffer_start <= command_buffer_end
        <= ORIGIN + len(data)
    ):
        raise BadcatInitBuildError(
            "command_buffer is outside the COM image")
    if (command_buffer_end - command_buffer_start
            != labels["COMMAND_BUFFER_CAPACITY"]):
        raise BadcatInitBuildError(
            "command_buffer must reserve exactly 128 bytes")
    command_buffer_offset = command_buffer_start - ORIGIN
    command_buffer_size = command_buffer_end - command_buffer_start
    if data[command_buffer_offset:
            command_buffer_offset + command_buffer_size] != bytes(
                command_buffer_size):
        raise BadcatInitBuildError(
            "command_buffer must initialize to zero")
    actual_prefix = _read_c_string(
        data, labels, "command_listener_prefix")
    if actual_prefix != LISTENER_COMMAND_PREFIX:
        raise BadcatInitBuildError(
            f"command_listener_prefix is {actual_prefix!r}, "
            f"expected {LISTENER_COMMAND_PREFIX!r}")
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
    for fragment in (b"ATS62=", b"ATS63=", b'ATQ1D"'):
        if fragment in uppercase:
            raise BadcatInitBuildError(
                "BADINIT must pass a binary B3 request instead of embedding "
                f"the deprecated/runtime command {fragment!r}")
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
