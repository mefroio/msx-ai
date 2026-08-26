#!/usr/bin/env python3
"""Materialize the redistributable MemMan utilities from committed Base64.

Only MEMMAN.COM, TL.COM and TK.COM are redistributed by this project.  All
inputs are decoded and verified before any output is replaced.  The pinned
MEMMAN.COM configuration is then changed from its upstream 128-byte heap to a
1280-byte heap at a verified structure, without modifying the upstream asset.
A corrupt, incompatible, or incomplete source bundle cannot leave a partially
updated vendor directory.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import hashlib
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Sequence


ASSETS = {
    "memman.com": ("memman.com.b64", "MEMMAN.COM"),
    "tl.com": ("tl.com.b64", "TL.COM"),
    "tk.com": ("tk.com.b64", "TK.COM"),
}
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)")

# CFGMMAN 2.4 locates this configuration block by its FE FD trailer.  The
# preceding fields are recursion depth, TSR entries, hook entries, and the
# little-endian heap size.  Pin both the file location and the complete
# structure so a different MemMan image is rejected instead of patched at a
# coincidental byte sequence.
MEMMAN_CONFIG_FILE_OFFSET = 0x01B8
MEMMAN_UPSTREAM_CONFIG_SIGNATURE = bytes.fromhex("08 1e 32 80 00 fe fd")
MEMMAN_HEAP_WORD_OFFSET = 3
MEMMAN_CONFIGURED_HEAP_SIZE = 0x0500


class MaterializeError(ValueError):
    """The committed MemMan asset bundle failed validation."""


@dataclasses.dataclass(frozen=True)
class MaterializedAsset:
    name: str
    path: pathlib.Path
    size: int
    sha256: str


def _read_checksums(path: pathlib.Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise MaterializeError(f"cannot read {path}: {exc}") from exc

    checksums: dict[str, str] = {}
    lines = text.splitlines()
    if not lines:
        raise MaterializeError(f"{path} is empty")
    for line_number, line in enumerate(lines, 1):
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise MaterializeError(
                f"{path}:{line_number}: expected '<sha256>  <filename>'")
        digest, name = match.groups()
        if name in checksums:
            raise MaterializeError(f"{path}: duplicate entry for {name}")
        checksums[name] = digest

    expected = set(ASSETS)
    actual = set(checksums)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        unexpected = ", ".join(sorted(actual - expected)) or "none"
        raise MaterializeError(
            f"{path}: checksum entries differ from the asset set "
            f"(missing: {missing}; unexpected: {unexpected})")
    return checksums


def _decode_asset(path: pathlib.Path) -> bytes:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise MaterializeError(f"cannot read {path}: {exc}") from exc

    # The repository wraps Base64 for reviewability. Remove ASCII whitespace,
    # then require every remaining byte to be valid canonical Base64 syntax.
    compact = b"".join(encoded.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MaterializeError(f"invalid Base64 in {path}: {exc}") from exc


def _configure_memman_heap(data: bytes) -> bytes:
    """Set the pinned MemMan 2.42 heap after verifying its exact structure."""

    start = MEMMAN_CONFIG_FILE_OFFSET
    end = start + len(MEMMAN_UPSTREAM_CONFIG_SIGNATURE)
    actual = data[start:end]
    if actual != MEMMAN_UPSTREAM_CONFIG_SIGNATURE:
        raise MaterializeError(
            "MEMMAN.COM configuration signature mismatch at file offset "
            f"0x{start:04X}: expected "
            f"{MEMMAN_UPSTREAM_CONFIG_SIGNATURE.hex(' ')}, got "
            f"{actual.hex(' ')}")
    if data.count(MEMMAN_UPSTREAM_CONFIG_SIGNATURE) != 1:
        raise MaterializeError(
            "MEMMAN.COM configuration signature is not unique")

    heap_start = start + MEMMAN_HEAP_WORD_OFFSET
    heap_end = heap_start + 2
    configured = bytearray(data)
    configured[heap_start:heap_end] = (
        MEMMAN_CONFIGURED_HEAP_SIZE.to_bytes(2, "little"))
    return bytes(configured)


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


def materialize_memman(
        source_dir: pathlib.Path, output_dir: pathlib.Path
) -> tuple[MaterializedAsset, ...]:
    """Decode and verify all public-domain MemMan utilities."""

    checksums = _read_checksums(source_dir / "SHA256SUMS")
    decoded: dict[str, bytes] = {}
    for manifest_name, (encoded_name, _output_name) in ASSETS.items():
        data = _decode_asset(source_dir / encoded_name)
        actual = hashlib.sha256(data).hexdigest()
        expected = checksums[manifest_name]
        if actual != expected:
            raise MaterializeError(
                f"SHA-256 mismatch for {manifest_name}: expected {expected}, "
                f"got {actual}")
        if manifest_name == "memman.com":
            data = _configure_memman_heap(data)
        decoded[manifest_name] = data

    results: list[MaterializedAsset] = []
    for manifest_name, (_encoded_name, output_name) in ASSETS.items():
        destination = output_dir / output_name
        data = decoded[manifest_name]
        _atomic_write(destination, data)
        results.append(MaterializedAsset(
            output_name, destination, len(data),
            hashlib.sha256(data).hexdigest()))
    return tuple(results)


def _parser() -> argparse.ArgumentParser:
    repository = pathlib.Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Decode the committed MemMan 2.42 Base64 assets and verify "
            "their exact SHA256SUMS entries; configure MEMMAN.COM with "
            "a verified 1280-byte heap."))
    parser.add_argument(
        "--source-dir", type=pathlib.Path,
        default=repository / "third_party" / "memman")
    parser.add_argument(
        "--output-dir", type=pathlib.Path,
        default=repository / "work" / "agent" / "vendor")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assets = materialize_memman(args.source_dir, args.output_dir)
    except (MaterializeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for asset in assets:
        print(
            f"wrote {asset.path}: {asset.size} bytes, "
            f"sha256 {asset.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
