#!/usr/bin/env python3
"""Interface-independent MSX application parsing and loading.

The loader deliberately knows nothing about TCP, MCP, openMSX, or a specific
cartridge.  A backend only needs to provide memory and execution methods (see
``load_application``).  This keeps the file formats and the safety checks
usable by every interface.

Canonical ``msx-ai-app-v1`` manifest example::

    {
      "format": "msx-ai-app-v1",
      "name": "demo",
      "segments": [
        {"space": "ram", "address": "0x8000", "hex": "3e01c9"},
        {"space": "vram", "address": 0, "file": "assets/title.sc5",
         "sha256": "..."}
      ],
      "entry": {"mode": "run", "address": "0x8000"}
    }

Each segment accepts exactly one of ``hex``/``data_hex``,
``base64``/``data_base64``, ``file``, or ``fill``.  ``fill`` may be an object
with ``value`` (or ``byte``) and ``length``, or a byte value accompanied by a
segment-level ``length``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Union


MANIFEST_FORMAT = "msx-ai-app-v1"
RAM_SIZE = 0x10000
VRAM_SIZE = 0x20000
SPACE_LIMITS = {"ram": RAM_SIZE, "vram": VRAM_SIZE}
ENTRY_MODES = {"none", "call", "run"}


class ApplicationError(RuntimeError):
    """Base error raised by the application parser/loader."""


class ApplicationFormatError(ApplicationError, ValueError):
    """The file or manifest is malformed or unsupported."""


class ApplicationRangeError(ApplicationFormatError):
    """A segment or entry point is outside the MSX address space."""


class ApplicationIntegrityError(ApplicationFormatError):
    """A declared SHA-256 digest does not match the decoded payload."""


class ApplicationPathError(ApplicationFormatError):
    """A manifest asset escapes its allowed base directory."""


class BackendError(ApplicationError):
    """The selected backend cannot perform the requested operation."""


class BackendCapabilityError(BackendError):
    """The backend does not advertise a manifest requirement."""


class UnsupportedMapperError(BackendCapabilityError):
    """A mapper image needs an explicit mapper-aware backend."""


@dataclass(frozen=True)
class Segment:
    """One fully decoded RAM or VRAM segment."""

    space: str
    address: int
    data: bytes = field(repr=False)
    expected_sha256: Optional[str] = None
    source: Optional[str] = None

    @property
    def end(self) -> int:
        """Exclusive end address."""

        return self.address + len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class EntryPoint:
    mode: str = "none"
    address: Optional[int] = None


@dataclass(frozen=True)
class Application:
    """Normalized application independent from its source file format."""

    name: str
    source_format: str
    segments: Tuple[Segment, ...]
    entry: EntryPoint = EntryPoint()
    requirements: Tuple[str, ...] = ()
    mapper: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    origin: Optional[str] = None

    @property
    def required_capabilities(self) -> Tuple[str, ...]:
        """All explicit and mechanically inferred backend capabilities."""

        return _required_capabilities(self, self.entry)


def _required_capabilities(
        application: Application, entry: EntryPoint) -> Tuple[str, ...]:
    """Infer capabilities for the effective, possibly overridden entry."""

    capabilities = list(application.requirements)
    for segment in application.segments:
        capabilities.append(f"write:{segment.space}")
    if entry.mode != "none":
        capabilities.append(f"execute:{entry.mode}")
    mapper_type = _mapper_type(application.mapper)
    if mapper_type not in (None, "none", "flat"):
        capabilities.append(f"mapper:{mapper_type}")
    return tuple(dict.fromkeys(capabilities))


Source = Union[Application, Mapping[str, Any], str, os.PathLike, bytes, bytearray]


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ApplicationFormatError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ApplicationFormatError(
                f"{label} is not a valid integer: {value!r}") from exc
    raise ApplicationFormatError(f"{label} must be an integer")


def _validate_range(address: int, length: int, limit: int, label: str) -> None:
    if address < 0 or length < 0 or address > limit or address + length > limit:
        last = limit - 1
        raise ApplicationRangeError(
            f"{label} range 0x{address:X}+{length} exceeds 0x{last:X}")


def _normalize_digest(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ApplicationFormatError(f"{label} must be a hexadecimal string")
    digest = value.strip().lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ApplicationFormatError(f"{label} must contain 64 hexadecimal digits")
    return digest


def _safe_asset_path(base_dir: Optional[Path], value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ApplicationPathError("segment file must be a non-empty relative path")
    if base_dir is None:
        raise ApplicationPathError(
            "a base directory is required when a manifest uses file segments")
    relative = Path(value)
    # Backslashes are separators on Windows but ordinary characters on POSIX.
    # Rejecting them makes the traversal rule identical on every host.
    if relative.is_absolute() or "\\" in value or ".." in relative.parts:
        raise ApplicationPathError(f"asset path escapes manifest directory: {value!r}")
    root = base_dir.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ApplicationPathError(
            f"asset path escapes manifest directory: {value!r}") from exc
    if not candidate.is_file():
        raise ApplicationPathError(f"asset file does not exist: {value!r}")
    return candidate


def _payload_keys(raw: Mapping[str, Any]) -> Sequence[str]:
    groups = (("hex", "data_hex"), ("base64", "data_base64"),
              ("file",), ("fill",))
    selected = []
    for aliases in groups:
        present = [key for key in aliases if key in raw]
        if len(present) > 1:
            raise ApplicationFormatError(
                f"segment contains duplicate payload aliases: {present}")
        selected.extend(present)
    return selected


def _decode_segment_payload(
        raw: Mapping[str, Any], address: int, limit: int,
        base_dir: Optional[Path], index: int) -> Tuple[bytes, Optional[str]]:
    selected = _payload_keys(raw)
    if len(selected) != 1:
        raise ApplicationFormatError(
            f"segment {index} must contain exactly one payload source")
    key = selected[0]
    source = None
    remaining = limit - address if 0 <= address <= limit else 0

    if key in ("hex", "data_hex"):
        value = raw[key]
        if not isinstance(value, str):
            raise ApplicationFormatError(f"segment {index} {key} must be a string")
        compact = "".join(value.split())
        if len(compact) % 2 or len(compact) // 2 > remaining:
            if len(compact) % 2:
                raise ApplicationFormatError(
                    f"segment {index} contains an odd number of hex digits")
            raise ApplicationRangeError(f"segment {index} payload is too large")
        try:
            data = bytes.fromhex(compact)
        except ValueError as exc:
            raise ApplicationFormatError(
                f"segment {index} contains invalid hexadecimal data") from exc
    elif key in ("base64", "data_base64"):
        value = raw[key]
        if not isinstance(value, str):
            raise ApplicationFormatError(f"segment {index} {key} must be a string")
        compact = "".join(value.split())
        # Reject huge input before allocating the decoded object.
        if len(compact) > ((remaining + 2) // 3) * 4 + 4:
            raise ApplicationRangeError(f"segment {index} payload is too large")
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApplicationFormatError(
                f"segment {index} contains invalid base64 data") from exc
    elif key == "file":
        path = _safe_asset_path(base_dir, raw[key])
        size = path.stat().st_size
        if size > remaining:
            raise ApplicationRangeError(f"segment {index} file is too large")
        data = path.read_bytes()
        source = str(path)
    else:
        fill = raw[key]
        if isinstance(fill, Mapping):
            byte_value = fill.get("value", fill.get("byte"))
            length_value = fill.get("length")
        else:
            byte_value = fill
            length_value = raw.get("length")
        byte_value = _integer(byte_value, f"segment {index} fill value")
        length = _integer(length_value, f"segment {index} fill length")
        if not 0 <= byte_value <= 0xFF:
            raise ApplicationFormatError(
                f"segment {index} fill value must be between 0 and 255")
        if length < 0 or length > remaining:
            raise ApplicationRangeError(f"segment {index} fill is too large")
        data = bytes([byte_value]) * length

    return data, source


def _parse_segment(
        raw: Any, index: int, base_dir: Optional[Path]) -> Segment:
    if not isinstance(raw, Mapping):
        raise ApplicationFormatError(f"segment {index} must be an object")
    space = raw.get("space")
    if not isinstance(space, str) or space.lower() not in SPACE_LIMITS:
        raise ApplicationFormatError(
            f"segment {index} space must be 'ram' or 'vram'")
    space = space.lower()
    address = _integer(raw.get("address"), f"segment {index} address")
    limit = SPACE_LIMITS[space]
    _validate_range(address, 0, limit, space.upper())
    data, source = _decode_segment_payload(raw, address, limit, base_dir, index)
    _validate_range(address, len(data), limit, space.upper())

    expected = None
    if "sha256" in raw:
        expected = _normalize_digest(raw["sha256"], f"segment {index} sha256")
        actual = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise ApplicationIntegrityError(
                f"segment {index} SHA-256 mismatch: expected {expected}, got {actual}")
    return Segment(space, address, data, expected, source)


def _parse_entry(raw: Any) -> EntryPoint:
    if raw is None:
        return EntryPoint()
    if isinstance(raw, str):
        mode = raw.lower()
        address = None
    elif isinstance(raw, Mapping):
        mode_value = raw.get("mode", "none")
        if not isinstance(mode_value, str):
            raise ApplicationFormatError("entry mode must be a string")
        mode = mode_value.lower()
        address = raw.get("address")
    else:
        raise ApplicationFormatError("entry must be an object or 'none'")
    if mode not in ENTRY_MODES:
        raise ApplicationFormatError("entry mode must be 'none', 'call' or 'run'")
    if mode == "none":
        if address is not None:
            raise ApplicationFormatError("entry address is not valid with mode 'none'")
        return EntryPoint()
    if address is None:
        raise ApplicationFormatError(f"entry address is required for mode {mode!r}")
    address_int = _integer(address, "entry address")
    _validate_range(address_int, 1, RAM_SIZE, "entry")
    return EntryPoint(mode, address_int)


def _parse_requirements(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ApplicationFormatError("requires must be an array of capability names")
    result = []
    for requirement in raw:
        if not isinstance(requirement, str) or not requirement.strip():
            raise ApplicationFormatError("each required capability must be a string")
        result.append(requirement.strip())
    return tuple(dict.fromkeys(result))


def _mapper_type(mapper: Optional[Mapping[str, Any]]) -> Optional[str]:
    if mapper is None:
        return None
    value = mapper.get("type")
    return value.lower() if isinstance(value, str) else None


def _parse_mapper(raw: Any) -> Optional[Mapping[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ApplicationFormatError("mapper must be an object")
    mapper = dict(raw)
    mapper_type = mapper.get("type")
    if not isinstance(mapper_type, str) or not mapper_type.strip():
        raise ApplicationFormatError("mapper type must be a non-empty string")
    mapper["type"] = mapper_type.strip().lower()
    for key in ("base", "size"):
        if key in mapper:
            mapper[key] = _integer(mapper[key], f"mapper {key}")
    return mapper


def parse_manifest(
        manifest: Mapping[str, Any], *, base_dir: Optional[Union[str, os.PathLike]] = None,
        origin: Optional[str] = None) -> Application:
    """Parse and normalize an ``msx-ai-app-v1`` manifest mapping."""

    if not isinstance(manifest, Mapping):
        raise ApplicationFormatError("manifest root must be an object")
    marker = manifest.get("format", manifest.get("schema"))
    if marker != MANIFEST_FORMAT:
        raise ApplicationFormatError(
            f"manifest format must be {MANIFEST_FORMAT!r}")
    if "format" in manifest and "schema" in manifest:
        if manifest["format"] != manifest["schema"]:
            raise ApplicationFormatError("manifest format and schema disagree")
    raw_segments = manifest.get("segments")
    if not isinstance(raw_segments, list):
        raise ApplicationFormatError("manifest segments must be an array")
    root = Path(base_dir) if base_dir is not None else None
    segments = tuple(
        _parse_segment(segment, index, root)
        for index, segment in enumerate(raw_segments)
    )
    name = manifest.get("name", "application")
    if not isinstance(name, str) or not name.strip():
        raise ApplicationFormatError("manifest name must be a non-empty string")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ApplicationFormatError("metadata must be an object")
    return Application(
        name=name.strip(),
        source_format=MANIFEST_FORMAT,
        segments=segments,
        entry=_parse_entry(manifest.get("entry")),
        requirements=_parse_requirements(manifest.get("requires")),
        mapper=_parse_mapper(manifest.get("mapper")),
        metadata=dict(metadata),
        origin=origin,
    )


def parse_com(data: bytes, *, name: str = "application.com") -> Application:
    """Normalize an MSX-DOS COM image (load/run address 0x0100)."""

    data = bytes(data)
    if not data:
        raise ApplicationFormatError("COM image is empty")
    _validate_range(0x0100, len(data), RAM_SIZE, "COM")
    return Application(
        name=name,
        source_format="com",
        segments=(Segment("ram", 0x0100, data),),
        entry=EntryPoint("run", 0x0100),
    )


def _le16(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def parse_bload(data: bytes, *, name: str = "application.bin") -> Application:
    """Normalize an MSX BLOAD binary with the seven-byte ``FE`` header."""

    data = bytes(data)
    if len(data) < 7 or data[0] != 0xFE:
        raise ApplicationFormatError("BLOAD image must start with a 7-byte FE header")
    start, end, execute = (_le16(data, 1), _le16(data, 3), _le16(data, 5))
    if end < start:
        raise ApplicationRangeError("BLOAD end address precedes start address")
    expected = end - start + 1
    payload = data[7:]
    if len(payload) != expected:
        raise ApplicationFormatError(
            f"BLOAD header declares {expected} bytes, file contains {len(payload)}")
    _validate_range(start, len(payload), RAM_SIZE, "BLOAD")
    entry = EntryPoint() if execute == 0 else EntryPoint("run", execute)
    if execute:
        _validate_range(execute, 1, RAM_SIZE, "BLOAD entry")
    return Application(
        name=name,
        source_format="bload",
        segments=(Segment("ram", start, payload),),
        entry=entry,
        metadata={"header": {"start": start, "end": end, "execute": execute}},
    )


def parse_flat_rom(
        data: bytes, *, name: str = "application.rom",
        base: Optional[int] = None) -> Application:
    """Normalize a headered, non-mapper 16 KiB or 32 KiB ROM image.

    This only describes a flat image.  It does not emulate bank switching and
    must not be used to claim support for arbitrary mapper ROMs.
    """

    data = bytes(data)
    if len(data) not in (0x4000, 0x8000):
        raise ApplicationFormatError("flat ROM size must be exactly 16 KiB or 32 KiB")
    if data[:2] != b"AB":
        raise ApplicationFormatError("flat ROM must start with the MSX AB header")
    vectors = {
        "init": _le16(data, 2),
        "statement": _le16(data, 4),
        "device": _le16(data, 6),
        "text": _le16(data, 8),
    }
    if base is None:
        # A 16 KiB image can be built for page 2.  Non-zero header vectors are
        # a useful, deterministic signal; callers can override ambiguous ROMs.
        nonzero = [value for value in vectors.values() if value]
        page2_only = (len(data) == 0x4000 and nonzero and
                      all(0x8000 <= value < 0xC000 for value in nonzero))
        base = 0x8000 if page2_only else 0x4000
    base = _integer(base, "ROM base")
    _validate_range(base, len(data), RAM_SIZE, "flat ROM")
    init = vectors["init"]
    if init:
        _validate_range(init, 1, RAM_SIZE, "ROM init")
    return Application(
        name=name,
        source_format="flat-rom",
        segments=(Segment("ram", base, data),),
        entry=EntryPoint("call", init) if init else EntryPoint(),
        mapper={"type": "flat", "base": base, "size": len(data)},
        metadata={"rom_header": vectors},
    )


def _json_manifest(data: bytes, *, base_dir: Optional[Path], origin: str) -> Application:
    try:
        text = data.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplicationFormatError(f"invalid JSON manifest: {exc}") from exc
    return parse_manifest(document, base_dir=base_dir, origin=origin)


def _normalize_format(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApplicationFormatError("format must be a string")
    aliases = {
        MANIFEST_FORMAT: "manifest", "manifest": "manifest", "json": "manifest",
        "com": "com", ".com": "com",
        "bin": "bload", ".bin": "bload", "bload": "bload",
        "rom": "flat-rom", ".rom": "flat-rom", "flat": "flat-rom",
        "flat-rom": "flat-rom",
    }
    normalized = aliases.get(value.lower())
    if normalized is None:
        raise ApplicationFormatError(f"unsupported application format: {value!r}")
    return normalized


def parse_application(
        source: Source, *, format: Optional[str] = None,
        base_dir: Optional[Union[str, os.PathLike]] = None,
        rom_base: Optional[int] = None) -> Application:
    """Parse a manifest, COM, BLOAD BIN, or flat ROM into an Application.

    Paths are detected from their extension.  Byte strings require ``format``
    for headerless COM data; BLOAD and flat ROM signatures can be detected.
    """

    if isinstance(source, Application):
        if format is not None:
            raise ApplicationFormatError("format cannot override an Application object")
        return source
    normalized = _normalize_format(format)
    if isinstance(source, Mapping):
        if normalized not in (None, "manifest"):
            raise ApplicationFormatError("a mapping source can only be a manifest")
        return parse_manifest(source, base_dir=base_dir)

    path = None
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ApplicationFormatError(f"cannot read application {path}: {exc}") from exc
        origin = str(path.resolve())
        if normalized is None:
            suffix = path.suffix.lower()
            normalized = {
                ".json": "manifest", ".msxapp": "manifest",
                ".com": "com", ".bin": "bload", ".rom": "flat-rom",
            }.get(suffix)
        asset_root = Path(base_dir) if base_dir is not None else path.parent
        name = path.name
    elif isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        origin = "<bytes>"
        asset_root = Path(base_dir) if base_dir is not None else None
        name = "application"
    else:
        raise ApplicationFormatError("unsupported application source type")

    if normalized is None:
        stripped = data.lstrip()
        if stripped.startswith(b"{"):
            normalized = "manifest"
        elif data.startswith(b"\xFE"):
            normalized = "bload"
        elif len(data) in (0x4000, 0x8000) and data.startswith(b"AB"):
            normalized = "flat-rom"
        else:
            raise ApplicationFormatError(
                "cannot detect headerless input; specify format='com' if appropriate")

    if normalized == "manifest":
        return _json_manifest(data, base_dir=asset_root, origin=origin)
    if normalized == "com":
        app = parse_com(data, name=name if path else "application.com")
    elif normalized == "bload":
        app = parse_bload(data, name=name if path else "application.bin")
    else:
        app = parse_flat_rom(
            data, name=name if path else "application.rom", base=rom_base)
    return Application(
        name=app.name,
        source_format=app.source_format,
        segments=app.segments,
        entry=app.entry,
        requirements=app.requirements,
        mapper=app.mapper,
        metadata=app.metadata,
        origin=origin,
    )


def _method(backend: Any, operation: str, names: Sequence[str]):
    for name in names:
        candidate = getattr(backend, name, None)
        if callable(candidate):
            return candidate
    raise BackendError(
        f"backend cannot {operation}; expected one of: {', '.join(names)}")


def _supports_capability(backend: Any, capability: str) -> Optional[bool]:
    for method_name in ("supports_capability", "supports"):
        method = getattr(backend, method_name, None)
        if callable(method):
            return bool(method(capability))
    capabilities = getattr(backend, "capabilities", None)
    if callable(capabilities):
        capabilities = capabilities()
    if isinstance(capabilities, Mapping):
        return bool(capabilities.get(capability, False))
    if isinstance(capabilities, (set, frozenset, list, tuple)):
        return capability in capabilities
    # Integer protocol bitmasks do not carry stable symbolic names here.
    return None


def _check_declared_requirements(backend: Any, requirements: Sequence[str]) -> None:
    for requirement in requirements:
        supported = _supports_capability(backend, requirement)
        if supported is not True:
            reason = "not advertised" if supported is None else "unsupported"
            raise BackendCapabilityError(
                f"backend capability {requirement!r} is {reason}")


def _mapper_configurator(backend: Any, mapper: Optional[Mapping[str, Any]]):
    """Validate mapper support without changing the target, then return its hook."""

    mapper_type = _mapper_type(mapper)
    if mapper_type in (None, "none", "flat"):
        return None
    supports_mapper = getattr(backend, "supports_mapper", None)
    supported = (bool(supports_mapper(mapper_type)) if callable(supports_mapper)
                 else _supports_capability(backend, f"mapper:{mapper_type}"))
    if supported is not True:
        raise UnsupportedMapperError(
            f"mapper {mapper_type!r} requires an explicitly mapper-aware backend")
    return _method(
        backend, f"configure mapper {mapper_type!r}", ("configure_mapper",))


def _entry_with_override(
        entry: EntryPoint, execute: Optional[str],
        entry_address: Optional[int]) -> EntryPoint:
    if execute is None:
        mode = entry.mode
    elif isinstance(execute, str) and execute.lower() in ENTRY_MODES:
        mode = execute.lower()
    else:
        raise ApplicationFormatError("execute must be 'none', 'call' or 'run'")
    address = entry.address if entry_address is None else _integer(
        entry_address, "entry address")
    if mode == "none":
        return EntryPoint()
    if address is None:
        raise ApplicationFormatError(f"entry address is required for mode {mode!r}")
    _validate_range(address, 1, RAM_SIZE, "entry")
    return EntryPoint(mode, address)


def load_application(
        backend: Any, source: Source, *, format: Optional[str] = None,
        base_dir: Optional[Union[str, os.PathLike]] = None,
        rom_base: Optional[int] = None, execute: Optional[str] = None,
        entry_address: Optional[int] = None, verify: bool = False,
        stop_before_load: bool = False) -> Mapping[str, Any]:
    """Parse and load an application using a duck-typed backend.

    Accepted backend method pairs are:

    * RAM write/read: ``write_ram``/``read_ram`` or ``poke``/``peek``
    * VRAM write/read: ``write_vram``/``read_vram`` or ``vpoke``/``vpeek``
    * execution: ``execute_call``/``execute_run`` or ``call``/``run``

    ``stop()`` is required only when ``stop_before_load`` is true.  Non-flat
    mappers additionally require ``supports_mapper(type)`` and
    ``configure_mapper(spec)``; no generic mapper emulation is implied.
    """

    application = parse_application(
        source, format=format, base_dir=base_dir, rom_base=rom_base)
    entry = _entry_with_override(application.entry, execute, entry_address)

    # Resolve every mandatory operation before changing backend state.
    writers = {}
    readers = {}
    for space in dict.fromkeys(segment.space for segment in application.segments):
        if space == "ram":
            writers[space] = _method(
                backend, "write RAM", ("write_ram", "poke"))
            if verify:
                readers[space] = _method(
                    backend, "read RAM", ("read_ram", "peek"))
        else:
            writers[space] = _method(
                backend, "write VRAM", ("write_vram", "vpoke"))
            if verify:
                readers[space] = _method(
                    backend, "read VRAM", ("read_vram", "vpeek"))
    executor = None
    if entry.mode == "call":
        executor = _method(backend, "call entry point", ("execute_call", "call"))
    elif entry.mode == "run":
        executor = _method(backend, "run entry point", ("execute_run", "run"))
    stopper = (_method(backend, "stop the current application", ("stop",))
               if stop_before_load else None)
    _check_declared_requirements(backend, application.requirements)

    # Mapper rejection also occurs before any backend state is changed.
    configure_mapper = _mapper_configurator(backend, application.mapper)
    preflight = getattr(backend, "preflight_application", None)
    prepare = getattr(backend, "prepare_application", None)
    # A preflight is observational by contract. Run it before STOP, mapper
    # changes, or writes so an incompatible image cannot partially mutate the
    # target before it is rejected. Keep the older prepare hook in its original
    # post-STOP/post-mapper position for backend compatibility.
    if callable(preflight):
        preflight(application)
    if stopper is not None:
        stopper()
    if configure_mapper is not None:
        configure_mapper(dict(application.mapper or {}))
    if callable(prepare):
        prepare(application)

    loaded = []
    for segment in application.segments:
        result = writers[segment.space](segment.address, segment.data)
        if isinstance(result, int) and not isinstance(result, bool) and result != len(segment.data):
            raise BackendError(
                f"backend wrote {result} of {len(segment.data)} bytes to {segment.space}")
        if verify:
            actual = bytes(readers[segment.space](segment.address, len(segment.data)))
            if actual != segment.data:
                raise ApplicationIntegrityError(
                    f"verification failed for {segment.space} at 0x{segment.address:X}")
        loaded.append({
            "space": segment.space,
            "address": segment.address,
            "length": len(segment.data),
            "sha256": segment.sha256,
            "verified": bool(verify),
        })

    if executor is not None:
        executor(entry.address)
    return {
        "name": application.name,
        "format": application.source_format,
        "origin": application.origin,
        "segments": loaded,
        "bytes_loaded": sum(item["length"] for item in loaded),
        "entry": {"mode": entry.mode, "address": entry.address},
        "mapper": dict(application.mapper) if application.mapper is not None else None,
        "required_capabilities": list(_required_capabilities(application, entry)),
    }


__all__ = [
    "MANIFEST_FORMAT", "RAM_SIZE", "VRAM_SIZE", "Application", "Segment",
    "EntryPoint", "ApplicationError", "ApplicationFormatError",
    "ApplicationRangeError", "ApplicationIntegrityError", "ApplicationPathError",
    "BackendError", "BackendCapabilityError", "UnsupportedMapperError",
    "parse_manifest", "parse_com", "parse_bload", "parse_flat_rom",
    "parse_application", "load_application",
]
