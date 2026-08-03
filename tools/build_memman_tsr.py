#!/usr/bin/env python3
"""Build a deterministic MemMan v3 ``.TSR`` file from two linked images.

The file layout follows the MemMan 2 TSR development specification and the
public v3 files produced by LinkTsr::

    header, relocation table, resident code, initialization code,
    hook table, zero padding

The original LinkTsr program and assembler framework were not redistributable,
so this implementation uses only the Python standard library and does not
contain either tool.

Relocations are inferred by assembling the same ``code + init`` payload at two
page-1 origins.  A changed little-endian word is accepted only when both values
point to the same offset inside that payload.  The changed bytes must have one
and only one non-overlapping decomposition into such words.  An unexplained or
ambiguous difference aborts the build instead of emitting a plausible-looking
but unsafe relocation table.

This proves the binary difference under the full-word relocation model.  It
cannot prove intent hidden in assembler source, so callers may additionally
pin the inferred offsets with repeated ``--expect-relocation`` options.

Specification: https://map.grauw.nl/resources/tsrdev_en.php
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import struct
import sys
from collections.abc import Iterable, Sequence


MAGIC = b"MST TSR\r\n"
HEADER_VERSION = 3
HEADER = struct.Struct("<9s12sB7H")
HEADER_SIZE = HEADER.size
PAGE1_START = 0x4000
PAGE1_END = 0x8000
ALLOWED_RECORD_SIZES = (128, 256)


class BuildError(ValueError):
    """The inputs cannot safely produce a MemMan TSR file."""


@dataclasses.dataclass(frozen=True)
class Hook:
    """One MemMan hook-table entry.

    ``address`` is the system hook address and ``handler_offset`` is relative
    to the beginning of the resident code.
    """

    address: int
    handler_offset: int


@dataclasses.dataclass(frozen=True)
class BuildSpec:
    """Metadata which cannot be recovered from the two raw payloads."""

    name: str
    origin: int
    delta: int
    code_length: int
    kill_offset: int
    talk_offset: int
    hooks: tuple[Hook, ...] = ()
    record_size: int = 128
    expected_relocations: tuple[int, ...] | None = None


@dataclasses.dataclass(frozen=True)
class BuildResult:
    data: bytes
    relocation_offsets: tuple[int, ...]
    unpadded_size: int
    padding_size: int


def _word(data: bytes | bytearray, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _put_word(data: bytearray, offset: int, value: int) -> None:
    data[offset] = value & 0xFF
    data[offset + 1] = (value >> 8) & 0xFF


def _format_offsets(offsets: Iterable[int]) -> str:
    return ", ".join(f"0x{offset:04X}" for offset in offsets)


def _resolve_unique_cover(
        length: int, changed: frozenset[int], candidates: frozenset[int]
) -> tuple[int, ...]:
    """Cover every changed byte with unique, non-overlapping two-byte words.

    At most two paths per position are retained because the caller needs only
    to distinguish zero, one and multiple solutions.
    """

    paths: list[list[tuple[int, ...]]] = [[] for _ in range(length + 1)]
    paths[0].append(())

    def add_path(position: int, path: tuple[int, ...]) -> None:
        if path not in paths[position] and len(paths[position]) < 2:
            paths[position].append(path)

    for position in range(length):
        for path in tuple(paths[position]):
            if position not in changed:
                add_path(position + 1, path)
            if position in candidates:
                add_path(position + 2, path + (position,))

    solutions = paths[length]
    if not solutions:
        raise BuildError(
            "binary differences cannot be represented entirely as "
            "non-overlapping, origin-relative 16-bit words")
    if len(solutions) != 1:
        examples = " or ".join(
            "[" + _format_offsets(solution) + "]" for solution in solutions)
        raise BuildError(
            "ambiguous relocation inference; multiple word decompositions "
            f"are valid: {examples}")
    return solutions[0]


def infer_relocations(
        image_a: bytes, image_b: bytes, origin: int, delta: int
) -> tuple[int, ...]:
    """Return safe word-relocation offsets inferred from two linked images."""

    if len(image_a) != len(image_b):
        raise BuildError(
            f"linked images have different sizes: {len(image_a)} and "
            f"{len(image_b)} bytes")
    if not image_a:
        raise BuildError("linked images are empty")
    if delta == 0:
        raise BuildError("the link-origin delta must be non-zero")

    origin_b = origin + delta
    changed = frozenset(
        offset for offset, (left, right) in
        enumerate(zip(image_a, image_b, strict=True)) if left != right)
    if not changed:
        return ()

    candidates: set[int] = set()
    payload_size = len(image_a)
    for offset in range(payload_size - 1):
        left = _word(image_a, offset)
        right = _word(image_b, offset)
        if left == right:
            continue

        target_offset = left - origin
        if not 0 <= target_offset <= payload_size:
            continue
        if right - origin_b != target_offset:
            continue
        candidates.add(offset)

    relocations = _resolve_unique_cover(
        payload_size, changed, frozenset(candidates))

    # Recreate the second image exactly. This catches any future relaxation of
    # the candidate/cover rules before it can corrupt an emitted REL table.
    recreated = bytearray(image_a)
    for offset in relocations:
        _put_word(recreated, offset, (_word(recreated, offset) + delta) & 0xFFFF)
    if bytes(recreated) != image_b:
        raise BuildError(
            "internal relocation verification failed to recreate the "
            "second linked image")
    return relocations


def _validate_spec(spec: BuildSpec, payload_size: int) -> bytes:
    try:
        encoded_name = spec.name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise BuildError("TSR name must contain ASCII characters only") from exc
    if not encoded_name or len(encoded_name) > 12:
        raise BuildError("TSR name must contain 1 to 12 ASCII characters")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded_name):
        raise BuildError("TSR name must contain printable ASCII characters only")

    if spec.record_size not in ALLOWED_RECORD_SIZES:
        raise BuildError("record size must be 128 or 256 bytes")
    if not PAGE1_START <= spec.origin < PAGE1_END:
        raise BuildError("primary link origin must be in MemMan page 1")
    origin_b = spec.origin + spec.delta
    if not PAGE1_START <= origin_b < PAGE1_END:
        raise BuildError("secondary link origin must be in MemMan page 1")
    if spec.origin + payload_size > PAGE1_END:
        raise BuildError("primary linked payload extends beyond MemMan page 1")
    if origin_b + payload_size > PAGE1_END:
        raise BuildError("secondary linked payload extends beyond MemMan page 1")

    if not 0 < spec.code_length < payload_size:
        raise BuildError(
            "code length must leave non-empty resident code and init sections")
    for label, offset in (
            ("kill", spec.kill_offset), ("talk", spec.talk_offset)):
        if not 0 <= offset < spec.code_length:
            raise BuildError(f"{label} offset must point inside resident code")

    seen_hooks: set[int] = set()
    for hook in spec.hooks:
        if not 0 <= hook.address <= 0xFFFF:
            raise BuildError("hook address must fit in 16 bits")
        if hook.address in seen_hooks:
            raise BuildError(f"duplicate hook address 0x{hook.address:04X}")
        seen_hooks.add(hook.address)
        if not 0 <= hook.handler_offset < spec.code_length:
            raise BuildError(
                f"handler for hook 0x{hook.address:04X} must point inside "
                "resident code")

    return encoded_name.ljust(12, b" ")


def _pack_word_table(values: Sequence[int]) -> bytes:
    length = 2 + 2 * len(values)
    if length > 0xFFFF:
        raise BuildError("word table exceeds the MemMan 16-bit length field")
    return struct.pack("<H", length) + b"".join(
        struct.pack("<H", value) for value in values)


def _emit_hook_table(origin: int, hooks: Sequence[Hook]) -> bytes:
    length = 2 + 4 * len(hooks)
    if length > 0xFFFF:
        raise BuildError("hook table exceeds the MemMan 16-bit length field")
    output = bytearray(struct.pack("<H", length))
    for hook in hooks:
        output += struct.pack(
            "<HH", hook.address, origin + hook.handler_offset)
    return bytes(output)


def _validate_emission(
        data: bytes, image: bytes, spec: BuildSpec,
        name: bytes, relocations: tuple[int, ...], unpadded_size: int
) -> None:
    if len(data) % spec.record_size:
        raise BuildError("internal error: output record padding is invalid")
    if any(data[unpadded_size:]):
        raise BuildError("internal error: output padding is not zero-filled")

    fields = HEADER.unpack_from(data)
    expected_header = (
        MAGIC, name, 0x1A, HEADER_VERSION, spec.origin,
        spec.origin + spec.code_length,
        spec.origin + spec.kill_offset,
        spec.origin + spec.talk_offset,
        spec.code_length, len(image) - spec.code_length,
    )
    if fields != expected_header:
        raise BuildError("internal error: emitted MemMan header is inconsistent")

    rel_length = struct.unpack_from("<H", data, HEADER_SIZE)[0]
    if rel_length != 2 + 2 * len(relocations):
        raise BuildError("internal error: emitted relocation length is invalid")
    payload_start = HEADER_SIZE + rel_length
    if data[payload_start:payload_start + len(image)] != image:
        raise BuildError("internal error: emitted payload differs from input")

    hook_start = payload_start + len(image)
    hook_length = struct.unpack_from("<H", data, hook_start)[0]
    if hook_start + hook_length != unpadded_size:
        raise BuildError("internal error: emitted hook-table length is invalid")


def build_memman_tsr(
        image_a: bytes, image_b: bytes, spec: BuildSpec
) -> BuildResult:
    """Build and validate one complete MemMan v3 file."""

    if len(image_a) != len(image_b):
        raise BuildError(
            f"linked images have different sizes: {len(image_a)} and "
            f"{len(image_b)} bytes")
    name = _validate_spec(spec, len(image_a))
    relocations = infer_relocations(
        image_a, image_b, spec.origin, spec.delta)

    if spec.expected_relocations is not None:
        expected = tuple(sorted(spec.expected_relocations))
        if len(set(expected)) != len(expected):
            raise BuildError("expected relocation offsets contain duplicates")
        if relocations != expected:
            raise BuildError(
                "inferred relocation offsets differ from the pinned set: "
                f"inferred [{_format_offsets(relocations)}], expected "
                f"[{_format_offsets(expected)}]")

    init_length = len(image_a) - spec.code_length
    header = HEADER.pack(
        MAGIC,
        name,
        0x1A,
        HEADER_VERSION,
        spec.origin,
        spec.origin + spec.code_length,
        spec.origin + spec.kill_offset,
        spec.origin + spec.talk_offset,
        spec.code_length,
        init_length,
    )
    relocation_table = _pack_word_table(
        tuple(spec.origin + offset for offset in relocations))
    hook_table = _emit_hook_table(spec.origin, spec.hooks)
    unpadded = header + relocation_table + image_a + hook_table
    padding_size = (-len(unpadded)) % spec.record_size
    data = unpadded + bytes(padding_size)
    _validate_emission(
        data, image_a, spec, name, relocations, len(unpadded))
    return BuildResult(data, relocations, len(unpadded), padding_size)


def _number(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected an integer such as 123 or 0x4024, got {text!r}") from exc


def _hook(text: str) -> Hook:
    try:
        address_text, offset_text = text.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "hook must be HOOK_ADDRESS:HANDLER_OFFSET") from exc
    return Hook(_number(address_text), _number(offset_text))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic MemMan v3 TSR from the same code+init "
            "payload linked at two origins."))
    parser.add_argument("--image-a", required=True, type=pathlib.Path)
    parser.add_argument("--image-b", required=True, type=pathlib.Path)
    parser.add_argument("--origin", required=True, type=_number,
                        help="link origin of image A, normally 0x4024")
    parser.add_argument("--delta", required=True, type=_number,
                        help="image B link origin minus image A link origin")
    parser.add_argument("--name", required=True,
                        help="unique printable ASCII TSR ID, at most 12 bytes")
    parser.add_argument("--code-length", required=True, type=_number,
                        help="resident-code length; remaining bytes are init")
    parser.add_argument("--kill-offset", required=True, type=_number)
    parser.add_argument("--talk-offset", required=True, type=_number)
    parser.add_argument(
        "--hook", action="append", default=[], type=_hook,
        metavar="ADDRESS:OFFSET",
        help="repeat for each system-hook/resident-handler pair")
    parser.add_argument(
        "--expect-relocation", action="append", type=_number,
        metavar="OFFSET",
        help="pin the inferred relocation set; repeat once per expected word")
    parser.add_argument(
        "--record-size", type=int, choices=ALLOWED_RECORD_SIZES, default=128,
        help="zero-padding boundary (default: 128; compatibility: 256)")
    parser.add_argument("--output", "-o", required=True, type=pathlib.Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        image_a = args.image_a.read_bytes()
        image_b = args.image_b.read_bytes()
        expected = (None if args.expect_relocation is None else
                    tuple(args.expect_relocation))
        result = build_memman_tsr(
            image_a,
            image_b,
            BuildSpec(
                name=args.name,
                origin=args.origin,
                delta=args.delta,
                code_length=args.code_length,
                kill_offset=args.kill_offset,
                talk_offset=args.talk_offset,
                hooks=tuple(args.hook),
                record_size=args.record_size,
                expected_relocations=expected,
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(result.data)
    except (BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"wrote {args.output}: {len(result.data)} bytes, "
        f"{len(result.relocation_offsets)} relocation(s), "
        f"{len(args.hook)} hook(s), {result.padding_size} padding byte(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
