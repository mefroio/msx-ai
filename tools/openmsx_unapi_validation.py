#!/usr/bin/env python3
"""Opt-in openMSX TCP/IP UNAPI validation for the MSX-AI resident agent.

This harness deliberately does not download or install anything.  It accepts
the matching openMSXnet v0.9.7 emulator archive and UNAPINET.COM release asset,
verifies their pinned hashes, and runs the MSX-AI UNAPI transport end to end.

The bridge emulates the TCP/IP UNAPI contract used by Pico/Pico+ firmware.  It
does not emulate either cartridge, its firmware, or its electrical/timing
behaviour; those remain physical-hardware release gates.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import platform as host_platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Callable, Iterator, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RELEASE = "v0.9.7"
RELEASE_URL = "https://github.com/antxiko/openMSXnet/releases/tag/v0.9.7"
RELEASE_DOWNLOAD_BASE = (
    "https://github.com/antxiko/openMSXnet/releases/download/v0.9.7"
)
DEFAULT_TEST_PORT = 43123
DEFAULT_FAULT_CYCLES = 3
STEADY_STATE_ROUND_TRIPS = 64
AUTO_RELISTEN_WAIT_SECONDS = 3.0
AUTO_RELISTEN_STABILITY_SECONDS = 1.0
TRACE_DUMP_NAMES = ("MSXAI.LOG", "MSXAI2.LOG")
TRACE_FAILURE_DUMP_NAME = "MSXFAIL.LOG"
TRACE_FLAG_ENABLED = 0x01
TRACE_FLAG_INCIDENT = 0x02
TRACE_FLAG_WRAPPED = 0x04
TRACE_RECORD_CAPACITY = 20
TRACE_EVENT_NAMES = frozenset({
    "ENABLE", "STATE", "STATE_ERROR", "DROP", "DOS_RELISTEN",
    "BASIC_RELISTEN", "OPEN_BEGIN", "OPEN_END", "ABORT_BEGIN",
    "ABORT_END", "SYSTEM_SUSPEND", "SYSTEM_RESUME", "RECONFIG_BEGIN",
    "RECONFIG_END", "AUTO_RELISTEN",
})
MIN_TEST_PORT = 1
# TCP/IP UNAPI reserves FFFFh as the random/local-port sentinel.
MAX_TEST_PORT = 65534
# Page 0 is deliberately read by the resident agent through RAMAD0, while the
# openMSX CPU-memory debugger sees the currently selected Main-ROM slot there.
# Page 3 is ordinary CPU-visible RAM in the DOS fixture, so both independent
# paths observe the same bytes and form a valid transport oracle.
MEMORY_TEST_ADDRESS = 0xC000

ASSET_SHA256 = {
    "openmsx-macos-arm64.zip":
        "d259b4104d3e60847d9d748acf26df9b108b83b11fe8a1eff915f82fb58627f2",
    "openmsx-linux-x86_64.zip":
        "2c91d72b8cf7fe18c34d4f42690b1abf9c4fb2bc3e601b49e4abe602f3707be9",
    "openmsx-windows-x86_64.zip":
        "68296ad28751d090c5691264b39629723f748c7bd169182c80be798a9719b49b",
}
UNAPINET_COM_SHA256 = (
    "86e7bb27d1f020e235929a6806f5f2dc8188c458119041c4017afd93a3c13227"
)
UNAPINET_COM_SIZE = 2046
UNAPINET_XML_SHA256 = (
    "280dde2a60a7f73f777a4eb9be02eb3628e4b224c1c2d6e480ad35726e1718ae"
)

# Accepted by the SunriseIDE_Nextor.xml shipped in the pinned archive.
NEXTOR_ROM_SHA1S = frozenset({
    "dca824d7b0ddf25c6e87a8098e97ab7489725f57",  # Nextor 2.1.1
    "d3a4375ff5f58cf59cc609dd41c90af285f033c2",  # Nextor 2.1.0
    "61cba1680ac6cb448dc3e8c710a43f4e7ab49457",  # Nextor 2.0.1
})

AGENT_PACKAGE_NAMES = (
    "MSXAI.COM",
    "MSXAIXF.COM",
    "MCP8251.TSR",
    "MCP16550.TSR",
    "MCP115K.TSR",
    "MCPUNAPI.TSR",
    "TU.COM",
    "MP.COM",
    "BADINIT.COM",
    "MEMMAN.COM",
    "TL.COM",
    "TK.COM",
)
SUITE_DIRECTORY = "MSXAI"
SUITE_HOME = r"A:\MSXAI"
REQUIRED_EXTENSION_FILES = (
    "slotexpander.xml",
    "SunriseIDE_Nextor.xml",
    "ram512k.xml",
    "unapinet.xml",
)

# v0.9.7's pinned UNAPINET.COM advertises HL=043Dh for GET_CAPAB block 1.
# Bit 5 is the TCP passive-open with unspecified remote-address capability that
# the MSX-AI listener requires.  The runtime test then proves the corresponding
# behaviour by connecting to that listener from the host.
PINNED_GET_CAPAB_BLOCK1_HL = 0x043D
PASSIVE_UNSPECIFIED_REMOTE_BIT = 0x0020

CONTRACT_PATH = (
    "public MCP STDIO tools for agent connect, status, memory, and disconnect",
    "UNAPI discovery through EXTBIO/RAM helper",
    "TCPIP_GET_CAPAB passive-unspecified-remote bit",
    "TCPIP_TCP_OPEN passive listener on the configured local port",
    "TCPIP_TCP_STATE establishment",
    "TCPIP_TCP_SEND agent-to-host bytes",
    "TCPIP_TCP_RCV host-to-agent bytes",
    "automatic guarded H.TIMI relisten after host disconnect",
    "automatic guarded H.TIMI relisten while a BASIC program is running",
)
FAULT_CONTRACT_PATH = CONTRACT_PATH[:-1]
MCP_TOOLS_EXERCISED = (
    "msx_agent_connect",
    "msx_agent_status",
    "msx_agent_memory_read",
    "msx_agent_disconnect",
)
NOT_EMULATED = (
    "Pico/Pico+ firmware",
    "cartridge registers and bus timing",
    "interrupt and electrical behaviour of physical hardware",
    "Wi-Fi association, DHCP, and radio failure modes",
)

FAULT_SCENARIOS = (
    "steady-state framed traffic",
    "idle peer FIN followed by zero-input automatic relisten",
    "idle peer RST followed by zero-input automatic relisten",
    "temporary bidirectional blackhole left established until RST",
)

MAX_ZIP_MEMBERS = 4096
MAX_ZIP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
COPYTREE_IGNORES = (
    ".DS_Store",
    "persistent",
    "savestates",
    "replays",
    "screenshots",
    "recordings",
    "settings.local.xml",
    ".filecache",
    "imgui.ini",
    "software",
)


class HarnessError(RuntimeError):
    """Base failure with an actionable, user-facing message."""


class PrerequisiteError(HarnessError):
    """A pinned asset or local licensed prerequisite is unavailable."""


class ValidationError(HarnessError):
    """The emulator launched but the end-to-end contract failed."""


def _exception_tree(error: BaseException) -> str:
    """Render nested async exception groups without losing their root cause."""
    nested = getattr(error, "exceptions", None)
    if isinstance(nested, tuple):
        children = "; ".join(_exception_tree(item) for item in nested)
        return f"{type(error).__name__}: {error} [{children}]"
    return f"{type(error).__name__}: {error}"


@dataclasses.dataclass(frozen=True)
class Settings:
    archive: pathlib.Path
    unapinet_com: pathlib.Path
    dos_hdd: pathlib.Path
    openmsx_home: pathlib.Path
    port: int = DEFAULT_TEST_PORT
    machine: str = "Gradiente_Expert20"
    host: str = "127.0.0.1"
    timeout: float = 60.0
    make: str = "make"
    window: bool = False
    keep_open: bool = False
    root: pathlib.Path = ROOT


@dataclasses.dataclass(frozen=True)
class Distribution:
    root: pathlib.Path
    binary: pathlib.Path
    share: pathlib.Path
    unapinet_xml: pathlib.Path
    asset_name: str


def validate_port(value: object) -> int:
    """Return a concrete TCP port, excluding UNAPI's FFFFh sentinel."""
    if isinstance(value, bool):
        raise TypeError("port must be an integer, not a boolean")
    if isinstance(value, str):
        try:
            value = int(value, 10)
        except ValueError as exc:
            raise ValueError("port must be a decimal integer") from exc
    if not isinstance(value, int):
        raise TypeError("port must be an integer")
    if not MIN_TEST_PORT <= value <= MAX_TEST_PORT:
        raise ValueError(
            f"port must be in range {MIN_TEST_PORT}..{MAX_TEST_PORT}; "
            "65535 (FFFFh) is reserved by TCP/IP UNAPI")
    return value


def _arg_port(text: str) -> int:
    try:
        return validate_port(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _arg_fault_cycles(text: str) -> int:
    try:
        cycles = int(text, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "fault cycles must be a positive integer") from exc
    if cycles <= 0:
        raise argparse.ArgumentTypeError(
            "fault cycles must be a positive integer")
    return cycles


def asset_name_for(platform_name: str | None = None,
                   architecture: str | None = None) -> str:
    """Map one supported host tuple to the exact pinned release asset."""
    system = (platform_name or sys.platform).lower()
    machine = (architecture or host_platform.machine()).lower()
    if system.startswith("darwin") and machine in ("arm64", "aarch64"):
        return "openmsx-macos-arm64.zip"
    if system.startswith("linux") and machine in ("x86_64", "amd64"):
        return "openmsx-linux-x86_64.zip"
    if (system.startswith("win") or system.startswith("cygwin")) and machine in (
            "x86_64", "amd64"):
        return "openmsx-windows-x86_64.zip"
    raise PrerequisiteError(
        f"openMSXnet {RELEASE} has no pinned asset for "
        f"{platform_name or sys.platform}/{architecture or host_platform.machine()}; "
        f"use one of: {', '.join(sorted(ASSET_SHA256))}")


def release_asset_url(name: str) -> str:
    return f"{RELEASE_DOWNLOAD_BASE}/{name}"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha1_file(path: pathlib.Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_hash(path: pathlib.Path, expected: str, label: str) -> str:
    if not path.is_file():
        raise PrerequisiteError(f"{label} not found: {path}")
    observed = sha256_file(path)
    if observed.lower() != expected.lower():
        raise PrerequisiteError(
            f"{label} SHA-256 mismatch: expected {expected}, found {observed}; "
            f"download the unchanged {RELEASE} asset from {RELEASE_URL}")
    return observed


def _safe_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    # ZIP paths are always POSIX-like, including archives made on Windows.
    # Reject backslashes explicitly so a crafted name cannot become traversal
    # only after pathlib applies Windows host semantics.
    if "\\" in info.filename or "\x00" in info.filename:
        raise PrerequisiteError(
            f"unsafe path in openMSXnet archive: {info.filename!r}")
    member = pathlib.PurePosixPath(info.filename)
    parts = tuple(part for part in member.parts if part not in ("", "."))
    if (member.is_absolute() or not parts or ".." in parts or
            any(":" in part for part in parts)):
        raise PrerequisiteError(
            f"unsafe path in openMSXnet archive: {info.filename!r}")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(unix_mode):
        raise PrerequisiteError(
            f"symbolic link is not allowed in openMSXnet archive: "
            f"{info.filename!r}")
    return parts


def safe_extract_zip(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """Extract a small pinned ZIP without traversal, links, or zip bombs."""
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise PrerequisiteError(
                    f"openMSXnet archive has {len(members)} members; "
                    f"limit is {MAX_ZIP_MEMBERS}")
            total_size = sum(info.file_size for info in members)
            if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise PrerequisiteError(
                    f"openMSXnet archive expands to {total_size} bytes; "
                    f"limit is {MAX_ZIP_UNCOMPRESSED_BYTES}")
            for info in members:
                parts = _safe_member_parts(info)
                target = destination.joinpath(*parts)
                if info.is_dir() or info.filename.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as input_stream, target.open("wb") as output:
                    shutil.copyfileobj(input_stream, output)
    except zipfile.BadZipFile as exc:
        raise PrerequisiteError(
            f"invalid openMSXnet ZIP archive {archive}: {exc}") from exc


def _single_path(candidates: Sequence[pathlib.Path], label: str) -> pathlib.Path:
    unique = sorted({path.resolve() for path in candidates if path.is_file()})
    if len(unique) != 1:
        rendered = ", ".join(str(path) for path in unique) or "none"
        raise PrerequisiteError(
            f"pinned archive must contain exactly one {label}; found {rendered}")
    return unique[0]


def locate_distribution(root: pathlib.Path, asset_name: str) -> Distribution:
    binary_name = "openmsx.exe" if "windows" in asset_name else "openmsx"
    binary = _single_path(
        [path for path in root.rglob(binary_name) if path.is_file()],
        binary_name)
    share_candidates = (
        binary.parent / "share",
        binary.parent.parent / "Resources" / "share",
        root / "share",
    )
    share_dirs = sorted({path.resolve() for path in share_candidates
                         if path.is_dir()})
    if len(share_dirs) != 1:
        rendered = ", ".join(str(path) for path in share_dirs) or "none"
        raise PrerequisiteError(
            f"pinned archive must contain exactly one openMSX share directory; "
            f"found {rendered}")
    share = share_dirs[0]
    extensions = share / "extensions"
    missing = [name for name in REQUIRED_EXTENSION_FILES
               if not (extensions / name).is_file()]
    if missing:
        raise PrerequisiteError(
            "pinned archive is missing required extension descriptor(s): "
            + ", ".join(missing))
    unapinet_xml = extensions / "unapinet.xml"
    verify_hash(unapinet_xml, UNAPINET_XML_SHA256, "unapinet.xml")
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return Distribution(
        root=root,
        binary=binary,
        share=share,
        unapinet_xml=unapinet_xml,
        asset_name=asset_name,
    )


@contextlib.contextmanager
def prepared_distribution(settings: Settings, *,
                          platform_name: str | None = None,
                          architecture: str | None = None) -> Iterator[Distribution]:
    asset_name = asset_name_for(platform_name, architecture)
    verify_hash(
        settings.archive, ASSET_SHA256[asset_name],
        f"openMSXnet {RELEASE} {asset_name}")
    with tempfile.TemporaryDirectory(prefix="msx-ai-openmsxnet-") as directory:
        root = pathlib.Path(directory)
        safe_extract_zip(settings.archive, root)
        yield locate_distribution(root, asset_name)


def _find_nextor_rom(openmsx_home: pathlib.Path) -> tuple[pathlib.Path, str] | None:
    rom_root = openmsx_home / "share" / "systemroms"
    if not rom_root.is_dir():
        return None
    for candidate in sorted(path for path in rom_root.rglob("*")
                            if path.is_file()):
        try:
            digest = sha1_file(candidate)
        except OSError:
            continue
        if digest in NEXTOR_ROM_SHA1S:
            return candidate, digest
    return None


def validate_local_prerequisites(settings: Settings) -> dict[str, object]:
    port = validate_port(settings.port)
    if (isinstance(settings.timeout, bool) or
            not isinstance(settings.timeout, (int, float)) or
            not math.isfinite(float(settings.timeout)) or
            not 0 < float(settings.timeout) <= 86400):
        raise PrerequisiteError(
            "timeout must be a finite number from above 0 through 86400 seconds")
    verify_hash(
        settings.unapinet_com, UNAPINET_COM_SHA256,
        f"openMSXnet {RELEASE} UNAPINET.COM")
    if settings.unapinet_com.stat().st_size != UNAPINET_COM_SIZE:
        raise PrerequisiteError(
            f"UNAPINET.COM size mismatch: expected {UNAPINET_COM_SIZE}, "
            f"found {settings.unapinet_com.stat().st_size}")
    if not settings.dos_hdd.is_file() or settings.dos_hdd.stat().st_size == 0:
        raise PrerequisiteError(
            f"licensed MSX-DOS 2/Nextor disk image not found or empty: "
            f"{settings.dos_hdd}; pass --dos-hdd or set MSX_AI_DOS_HDD")
    machine_xml = (
        settings.openmsx_home / "share" / "machines" /
        f"{settings.machine}.xml")
    if not machine_xml.is_file():
        raise PrerequisiteError(
            f"machine configuration not found: {machine_xml}; pass an isolated "
            "openMSX home containing the licensed machine ROM configuration")
    nextor = _find_nextor_rom(settings.openmsx_home)
    if nextor is None:
        raise PrerequisiteError(
            f"no compatible SunriseIDE Nextor ROM was found below "
            f"{settings.openmsx_home / 'share' / 'systemroms'}; accepted SHA-1: "
            + ", ".join(sorted(NEXTOR_ROM_SHA1S)))
    return {
        "port": port,
        "machine_xml": str(machine_xml),
        "nextor_rom": str(nextor[0]),
        "nextor_rom_sha1": nextor[1],
        "dos_hdd": str(settings.dos_hdd),
        "openmsx_home": str(settings.openmsx_home),
    }


def _runtime_dependency_action(output: str) -> str:
    lowered = output.lower()
    dependency_markers = (
        "library not loaded",
        "error while loading shared libraries",
        "cannot open shared object file",
        "dll was not found",
        "image not found",
    )
    if any(marker in lowered for marker in dependency_markers):
        return (
            "Install the native runtime libraries listed by openMSXnet v0.9.7 "
            "for this host, or use its matching self-contained Windows asset. "
            "The harness never installs them and an ordinary openMSX 21.0 "
            "binary cannot replace this v1 bridge build.")
    return (
        "Run the extracted binary manually with --version, resolve the reported "
        "host error, and rerun preflight. Do not pair UNAPINET.COM v0.9.7 with "
        "current upstream openMSX's incompatible v2 bridge.")


def probe_binary(distribution: Distribution, *,
                 runner: Callable[..., subprocess.CompletedProcess[str]] =
                 subprocess.run) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="msx-ai-openmsx-probe-home-") as home:
        env = os.environ.copy()
        env["OPENMSX_SYSTEM_DATA"] = str(distribution.share)
        env["OPENMSX_HOME"] = home
        try:
            result = runner(
                [str(distribution.binary), "--version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise PrerequisiteError(
                f"pinned openMSXnet binary did not answer --version in 15s: "
                f"{exc}; terminate any security prompt and retry") from exc
        except OSError as exc:
            detail = str(exc)
            raise PrerequisiteError(
                f"could not execute pinned openMSXnet binary: {detail}. "
                + _runtime_dependency_action(detail)) from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr)
                       if part and part.strip())
    if result.returncode != 0:
        raise PrerequisiteError(
            f"pinned openMSXnet binary exited with {result.returncode} during "
            f"--version:\n{output or '<no output>'}\n"
            + _runtime_dependency_action(output))
    if "openmsx" not in output.lower():
        raise PrerequisiteError(
            "pinned binary --version output did not identify openMSX: "
            + (output or "<no output>"))
    return {
        "binary": str(distribution.binary),
        "binary_archive_member": str(
            distribution.binary.relative_to(distribution.root.resolve())),
        "system_data": str(distribution.share),
        "system_data_archive_member": str(
            distribution.share.relative_to(distribution.root.resolve())),
        "version_output": output,
    }


def _base_report(settings: Settings, asset_name: str | None) -> dict[str, object]:
    return {
        "ready": False,
        "release": RELEASE,
        "release_url": RELEASE_URL,
        "asset": asset_name,
        "asset_url": (
            None if asset_name is None else release_asset_url(asset_name)),
        "archive": str(settings.archive),
        "unapinet_com": str(settings.unapinet_com),
        "unapinet_com_url": release_asset_url("UNAPINET.COM"),
        "custom_port": settings.port,
        "socket_direction": "host TCP client -> emulated MSX passive listener",
        "pinned_get_capab_block1_hl": f"0x{PINNED_GET_CAPAB_BLOCK1_HL:04X}",
        "passive_unspecified_remote_bit":
            f"0x{PASSIVE_UNSPECIFIED_REMOTE_BIT:04X}",
        "contract_path": list(CONTRACT_PATH),
        "pico_firmware_emulated": False,
        "not_emulated": list(NOT_EMULATED),
        "problems": [],
    }


def preflight(settings: Settings, *, platform_name: str | None = None,
              architecture: str | None = None,
              runner: Callable[..., subprocess.CompletedProcess[str]] =
              subprocess.run) -> dict[str, object]:
    """Perform read-only/static checks plus a temporary ``--version`` probe."""
    try:
        asset_name = asset_name_for(platform_name, architecture)
    except PrerequisiteError as exc:
        report = _base_report(settings, None)
        report["problems"] = [str(exc)]
        return report
    report = _base_report(settings, asset_name)
    try:
        local = validate_local_prerequisites(settings)
        report["local"] = local
        with prepared_distribution(
                settings, platform_name=platform_name,
                architecture=architecture) as distribution:
            report["distribution"] = {
                "binary_archive_member": str(
                    distribution.binary.relative_to(
                        distribution.root.resolve())),
                "system_data_archive_member": str(
                    distribution.share.relative_to(
                        distribution.root.resolve())),
                "archive_sha256": ASSET_SHA256[asset_name],
                "unapinet_com_sha256": UNAPINET_COM_SHA256,
                "unapinet_xml_sha256": UNAPINET_XML_SHA256,
            }
            binary = probe_binary(distribution, runner=runner)
            report["distribution"]["version_output"] = binary["version_output"]
            report["ready"] = True
    except HarnessError as exc:
        report["problems"] = [str(exc)]
    except OSError as exc:
        report["problems"] = [f"preflight filesystem error: {exc}"]
    return report


def build_agent_package(settings: Settings, *, development_trace: bool = False,
                        runner: Callable[..., subprocess.CompletedProcess[str]] =
                        subprocess.run) -> tuple[pathlib.Path, ...]:
    target = "agent-trace" if development_trace else "agent"
    try:
        result = runner(
            [settings.make, target], cwd=settings.root,
            capture_output=True, text=True)
    except OSError as exc:
        raise PrerequisiteError(
            f"could not run `make {target}`: {exc}") from exc
    if result.returncode != 0:
        output = result.stderr or result.stdout or "<no build output>"
        raise PrerequisiteError(
            f"`make {target}` failed:\n" + output.strip())
    artifact_root = settings.root / (
        "work/agent-trace" if development_trace else "work/agent")
    artifacts = tuple(artifact_root / name for name in AGENT_PACKAGE_NAMES)
    missing = [str(path) for path in artifacts
               if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise PrerequisiteError(
            f"{target} build is incomplete: " + ", ".join(missing))
    return artifacts


def _test_only_relisten_symbol_addresses(
        settings: Settings, *, development_trace: bool = False,
        ) -> dict[str, int]:
    """Resolve resident labels used only to certify the UNAPINET fixture.

    Production deliberately certifies only TU's Pico/Pico+ private-work-area
    prefix.  UNAPINET cannot present that certificate, so this emulator-only
    harness patches the resulting boolean while the MemMan segment is mapped.
    """
    from tools.build_agent_tsr import (  # pylint: disable=import-outside-toplevel,protected-access
        BUILD_ORIGINS,
        _assemble,
    )

    assembler = os.environ.get("Z80ASM", "z80asm")
    with tempfile.TemporaryDirectory(
            prefix="msx-ai-unapi-labels-") as directory:
        image = _assemble(
            settings.root, pathlib.Path(directory), assembler,
            BUILD_ORIGINS[0], development_trace=development_trace)
    required = (
        "resident_start", "resident_timi_hook",
        "unapi_service_relisten_certificate_ready",
        "unapi_hook_relisten_certified", "unapi_relisten_pending",
        "unapi_retry_count", "transport_session_lost",
        "unapi_connection", "unapi_connection_state",
        "unapi_lifecycle_busy", "unapi_busy", "unapi_last_error",
        "runtime_mode", "hook_kind", "hook_system_suspended",
        "tsr_heap_fault", "in_hook",
    )
    missing = [name for name in required if name not in image.labels]
    if missing:
        raise PrerequisiteError(
            "resident assembly is missing test-harness labels: " +
            ", ".join(missing))
    return {name: int(image.labels[name]) for name in required}


def msx_install_commands(port: int, *, trace: bool = False) -> tuple[str, ...]:
    port = validate_port(port)
    install = (f"MSXAI {port} /TRACE" if trace else
               f"MSXAI /DRIVER:UNAPI /PORT:{port}")
    return (
        f"SET MSXAI_HOME={SUITE_HOME}",
        f"PATH {SUITE_HOME};%PATH%",
        "UNAPINET",
        install,
    )


def _dos_entry_size(listing: str, name: str) -> int | None:
    match = re.search(
        rf"(?im)^\s*{re.escape(name)}\s+\S+\s+(\d+)\s*$", listing)
    return None if match is None else int(match.group(1))


_TRACE_STATUS_LINE = re.compile(
    r"^FLAGS=(?P<flags>[0-9A-F]{2}) "
    r"COUNT=(?P<count>[0-9A-F]{2}) "
    r"NEXT=(?P<next>[0-9A-F]{2}) "
    r"SEQ=(?P<sequence>[0-9A-F]{4})$")
_TRACE_COUNTER_LINE = re.compile(
    r"^POLLS=(?P<polls>[0-9A-F]{4}) "
    r"CHANGES=(?P<changes>[0-9A-F]{4}) "
    r"TIMI=(?P<timi>[0-9A-F]{4})$")
_TRACE_FIRST_LINE = re.compile(
    r"^FIRST (?P<event>[A-Z_]+) "
    r"E=(?P<error>[0-9A-F]{2}) S=(?P<state>[0-9A-F]{2}) "
    r"C=(?P<active>[0-9A-F]{2}) X=(?P<cleanup>[0-9A-F]{2}) "
    r"F=(?P<flags>[0-9A-F]{2}) T=(?P<jiffy>[0-9A-F]{4}) "
    r"EXTRA=(?P<extra>(?:[0-9A-F]{2}){8})$")
_TRACE_RECORD_LINE = re.compile(
    r"^#(?P<sequence>[0-9A-F]{4}) (?P<event>[A-Z_]+) "
    r"E=(?P<error>[0-9A-F]{2}) S=(?P<state>[0-9A-F]{2}) "
    r"C=(?P<active>[0-9A-F]{2}) X=(?P<cleanup>[0-9A-F]{2}) "
    r"F=(?P<flags>[0-9A-F]{2}) T=(?P<jiffy>[0-9A-F]{4})$")


def _trace_record_from_match(match: re.Match[str]) -> dict[str, object]:
    groups = match.groupdict()
    record: dict[str, object] = {
        "event": groups["event"],
        "error": int(groups["error"], 16),
        "state": int(groups["state"], 16),
        "active": int(groups["active"], 16),
        "cleanup": int(groups["cleanup"], 16),
        "flags": int(groups["flags"], 16),
        "jiffy": int(groups["jiffy"], 16),
    }
    if groups.get("sequence") is not None:
        record["sequence"] = int(groups["sequence"], 16)
    if groups.get("extra") is not None:
        raw = groups["extra"]
        record["extra"] = [
            int(raw[index:index + 2], 16)
            for index in range(0, len(raw), 2)
        ]
    return record


def parse_resident_trace(text: str) -> dict[str, object]:
    """Parse one textual `/DUMPTRACE` file without emulator state."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 4 or lines[0] != "MSXAI TRACE V1":
        raise ValidationError("resident trace has no valid V1 header")
    if any(not line for line in lines):
        raise ValidationError("resident trace contains an unexpected blank line")

    status = _TRACE_STATUS_LINE.fullmatch(lines[1])
    counters = _TRACE_COUNTER_LINE.fullmatch(lines[2])
    if status is None or counters is None:
        raise ValidationError("resident trace status/counter line is malformed")

    if lines[3] == "FIRST NONE":
        first_incident = None
    else:
        first = _TRACE_FIRST_LINE.fullmatch(lines[3])
        if first is None:
            raise ValidationError("resident trace FIRST record is malformed")
        first_incident = _trace_record_from_match(first)

    records: list[dict[str, object]] = []
    for line in lines[4:]:
        match = _TRACE_RECORD_LINE.fullmatch(line)
        if match is None:
            raise ValidationError(f"resident trace record is malformed: {line}")
        records.append(_trace_record_from_match(match))

    return {
        "flags": int(status.group("flags"), 16),
        "count": int(status.group("count"), 16),
        "next_index": int(status.group("next"), 16),
        "sequence": int(status.group("sequence"), 16),
        "polls": int(counters.group("polls"), 16),
        "state_changes": int(counters.group("changes"), 16),
        "timi": int(counters.group("timi"), 16),
        "first_incident": first_incident,
        "records": records,
        "events": [record["event"] for record in records],
    }


def validate_resident_trace(trace: Mapping[str, object]) -> None:
    """Enforce the fixed ring and first-incident invariants."""
    flags = int(trace["flags"])
    count = int(trace["count"])
    next_index = int(trace["next_index"])
    sequence = int(trace["sequence"])
    first = trace["first_incident"]
    records = list(trace["records"])

    if flags & ~(TRACE_FLAG_ENABLED | TRACE_FLAG_INCIDENT |
                 TRACE_FLAG_WRAPPED):
        raise ValidationError(f"resident trace has unknown flags: {flags:02X}")
    if not flags & TRACE_FLAG_ENABLED:
        raise ValidationError("resident trace is not marked enabled")
    if not 0 <= count <= TRACE_RECORD_CAPACITY:
        raise ValidationError(f"resident trace count is invalid: {count}")
    if len(records) != count:
        raise ValidationError(
            f"resident trace COUNT={count} but contains {len(records)} records")
    if not 0 <= next_index < TRACE_RECORD_CAPACITY:
        raise ValidationError(
            f"resident trace NEXT index is invalid: {next_index}")
    if next_index != sequence % TRACE_RECORD_CAPACITY:
        raise ValidationError(
            f"resident trace NEXT={next_index} is inconsistent with SEQ={sequence}")
    if flags & TRACE_FLAG_WRAPPED and count != TRACE_RECORD_CAPACITY:
        raise ValidationError("resident trace WRAPPED flag requires a full ring")
    if bool(flags & TRACE_FLAG_INCIDENT) != (first is not None):
        raise ValidationError(
            "resident trace INCIDENT flag disagrees with the FIRST record")
    if first is not None:
        event = str(first["event"])
        if event not in ("STATE_ERROR", "DROP", "OPEN_END", "ABORT_END"):
            raise ValidationError(
                f"resident trace FIRST event is not an incident: {event}")
        if event != "DROP" and int(first["error"]) == 0:
            raise ValidationError(
                f"resident trace FIRST {event} has no error code")

    if records:
        expected_first = (sequence - count + 1) & 0xFFFF
        expected = [
            (expected_first + index) & 0xFFFF for index in range(count)
        ]
        observed = [int(record["sequence"]) for record in records]
        if observed != expected:
            raise ValidationError(
                "resident trace record sequence is not chronological: "
                f"expected {expected}, found {observed}")
    elif sequence != 0:
        raise ValidationError(
            "resident trace has a nonzero sequence without records")

    previous: tuple[int, int] | None = None
    for record in records:
        if record["event"] not in TRACE_EVENT_NAMES:
            raise ValidationError(
                f"resident trace contains unknown event {record['event']}")
        if record["event"] not in ("STATE", "STATE_ERROR"):
            previous = None
            continue
        current = (int(record["error"]), int(record["state"]))
        if current == previous:
            raise ValidationError(
                "resident trace contains an adjacent duplicate TCP state")
        previous = current


def _dos_partition_offset(disk: pathlib.Path) -> int:
    """Return the first MBR partition byte offset, or zero for a raw disk."""
    with disk.open("rb") as source:
        sector = source.read(512)
    if len(sector) != 512 or sector[510:512] != b"\x55\xAA":
        return 0
    for index in range(4):
        entry = sector[446 + index * 16:462 + index * 16]
        start = int.from_bytes(entry[8:12], "little")
        sectors = int.from_bytes(entry[12:16], "little")
        if entry[4] and start and sectors:
            return start * 512
    return 0


def _require_mcopy() -> str:
    """Return the mtools extractor required by resident trace validation."""
    mcopy = shutil.which("mcopy")
    if mcopy is None:
        raise PrerequisiteError(
            "trace validation requires mcopy (mtools) to inspect the closed "
            "disposable DOS disk")
    return mcopy


def _extract_dos_file(disk: pathlib.Path, dos_name: str,
                      target: pathlib.Path) -> None:
    """Read a closed FAT image through mtools without modifying it."""
    mcopy = _require_mcopy()
    if re.fullmatch(r"[A-Z0-9_]{1,8}(?:\.[A-Z0-9]{1,3})?", dos_name) is None:
        raise ValueError(f"invalid DOS 8.3 trace filename: {dos_name}")
    offset = _dos_partition_offset(disk)
    image = str(disk) + (f"@@{offset}" if offset else "")
    environment = os.environ.copy()
    environment["MTOOLS_SKIP_CHECK"] = "1"
    result = subprocess.run(
        [mcopy, "-n", "-i", image, f"::{dos_name}", str(target)],
        capture_output=True, text=True, timeout=30, env=environment,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no mcopy output").strip()
        raise ValidationError(
            f"could not extract {dos_name} from the trace disk: {detail}")


def _import_suite(machine: object,
                  artifacts: Sequence[tuple[str, pathlib.Path]]) -> None:
    machine.cmd("set power off")
    machine.cmd("diskmanipulator chdir hda1 /")
    root_listing = machine.cmd("diskmanipulator dir hda1")
    for name, _path in artifacts:
        if _dos_entry_size(root_listing, name) is not None:
            machine.cmd(f"diskmanipulator delete hda1 {name}")
            root_listing = machine.cmd("diskmanipulator dir hda1")
    if re.search(
            rf"(?im)^\s*{re.escape(SUITE_DIRECTORY)}\b.*<DIR>",
            root_listing) is None:
        machine.cmd(f"diskmanipulator mkdir hda1 {SUITE_DIRECTORY}")
    machine.cmd(f"diskmanipulator chdir hda1 {SUITE_DIRECTORY}")
    for name, path in artifacts:
        listing = machine.cmd("diskmanipulator dir hda1")
        if _dos_entry_size(listing, name) is not None:
            machine.cmd(f"diskmanipulator delete hda1 {name}")
        machine.cmd(f"diskmanipulator import hda1 {{{path}}}")
        observed = _dos_entry_size(
            machine.cmd("diskmanipulator dir hda1"), name)
        expected = path.stat().st_size
        if observed != expected:
            raise ValidationError(
                f"disk import verification failed for {name}: expected "
                f"{expected} bytes, found "
                f"{'missing' if observed is None else observed}")
    machine.cmd("diskmanipulator chdir hda1 /")


def _dos_prompt_visible(screen: str) -> bool:
    # VDP text extraction preserves the left border used by some MSX-DOS
    # screen modes, so a real prompt may begin after one or more spaces.
    return re.search(
        r"(?im)^[ \t]*[A-Z]:\\[^\r\n]*>\s*$", screen) is not None


def _wait_dos_prompt(machine: object, timeout: float = 15.0) -> str:
    """Advance emulated time until COMMAND2 exposes a DOS prompt."""
    deadline = time.monotonic() + timeout
    screen = machine.screen_text()
    while not _dos_prompt_visible(screen) and time.monotonic() < deadline:
        machine.advance(0.1)
        screen = machine.screen_text()
    if not _dos_prompt_visible(screen):
        raise ValidationError(
            f"MSX did not reach a DOS prompt within {timeout:.1f}s:\n{screen}")
    return screen


def _wait_dos_command_completion(machine: object, command: str,
                                 before: str,
                                 timeout: float = 15.0) -> str:
    """Require fresh command text and a following prompt, not a stale prompt."""
    command_line = re.compile(
        rf"(?im)^[ \t]*[A-Z]:\\[^\r\n]*>[ \t]*"
        rf"{re.escape(command)}[ \t]*$")
    before_count = len(command_line.findall(before))
    deadline = time.monotonic() + timeout
    screen = before
    while time.monotonic() < deadline:
        machine.advance(0.1)
        screen = machine.screen_text()
        if (len(command_line.findall(screen)) > before_count and
                _dos_prompt_visible(screen)):
            return screen
    raise ValidationError(
        f"MSX command {command!r} did not produce a fresh command/prompt "
        f"boundary within {timeout:.1f}s:\n{screen}")


def _trace_dump_success_visible(screen: str) -> bool:
    compact = re.sub(r"\s+", "", screen).casefold()
    return "msxaitracewritten;residentlogpreserved." in compact


def _trace_sequence_delta(first: int, second: int) -> int:
    """Require the second trace snapshot to advance monotonically."""
    delta = (int(second) - int(first)) & 0xFFFF
    if delta == 0:
        raise ValidationError(
            "the second resident trace dump did not advance the sequence")
    if delta >= 0x8000:
        raise ValidationError(
            "the resident trace sequence moved backwards or was reset")
    return delta


def _capture_failed_install_trace(machine: object, disk: pathlib.Path,
                                  runtime: pathlib.Path) -> dict[str, object]:
    """Dump the already-enabled ring after first-install A7 fails."""
    command = rf"MSXAI /DUMPTRACE A:\{TRACE_FAILURE_DUMP_NAME}"
    before = machine.screen_text()
    machine.type_line(command)
    screen = _wait_dos_command_completion(machine, command, before, timeout=30.0)
    if not _trace_dump_success_visible(screen):
        raise ValidationError(
            "could not capture the resident ring after install failure:\n" +
            screen)
    machine.cmd("set power off")
    target = runtime / TRACE_FAILURE_DUMP_NAME.lower()
    _extract_dos_file(disk, TRACE_FAILURE_DUMP_NAME, target)
    parsed = parse_resident_trace(target.read_text(encoding="ascii"))
    validate_resident_trace(parsed)
    return parsed


def _exercise_resident_trace(machine: object, disk: pathlib.Path,
                             runtime: pathlib.Path) -> dict[str, object]:
    """Return BASIC to DOS, dump twice, and validate resident evidence."""
    reset_before, reset_events_before = _reset_vector_hits(machine)
    before_system = machine.screen_text()
    if re.search(r"(?im)^\s*Ok\s*$", before_system) is None:
        raise ValidationError(
            "trace validation expected the public MCP exercise to finish at "
            "the BASIC prompt:\n" + before_system)

    machine.type_line("_SYSTEM")
    system_screen = _wait_dos_prompt(machine, timeout=30.0)
    reset_after_system, reset_events_after_system = _reset_vector_hits(machine)
    if reset_after_system != reset_before + 1:
        raise ValidationError(
            "BASIC -> DOS did not execute exactly one intentional _SYSTEM "
            "warm-boot boundary: " + reset_events_after_system)

    dump_commands = tuple(
        rf"MSXAI /DUMPTRACE A:\{name}" for name in TRACE_DUMP_NAMES)
    for command in dump_commands:
        before = machine.screen_text()
        machine.type_line(command)
        screen = _wait_dos_command_completion(
            machine, command, before, timeout=30.0)
        _assert_command_screen(command, screen, prompt=True)
        if not _trace_dump_success_visible(screen):
            raise ValidationError(
                f"{command} returned to DOS without its success marker:\n"
                + screen)

    reset_after_dump, reset_events_after_dump = _reset_vector_hits(machine)
    expected_after_dump = reset_after_system + len(dump_commands)
    if reset_after_dump != expected_after_dump:
        raise ValidationError(
            "trace commands did not each use exactly one normal CP/M warm-"
            "boot return boundary: " +
            reset_events_after_dump)

    # Power-off flushes Nextor's FAT state before either diskmanipulator or
    # mtools inspects the disposable image. The one openMSX process remains
    # the sole emulator instance for the entire validation.
    machine.cmd("set power off")
    machine.cmd("diskmanipulator chdir hda1 /")
    listing = machine.cmd("diskmanipulator dir hda1")
    sizes: dict[str, int] = {}
    traces: list[dict[str, object]] = []
    for name in TRACE_DUMP_NAMES:
        size = _dos_entry_size(listing, name)
        if size is None or size <= 0:
            raise ValidationError(
                f"{name} was not committed to the disposable DOS disk")
        sizes[name] = size
        target = runtime / name.lower()
        _extract_dos_file(disk, name, target)
        try:
            content = target.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(
                f"could not read extracted resident trace {name}: {exc}") \
                from exc
        parsed = parse_resident_trace(content)
        validate_resident_trace(parsed)
        traces.append(parsed)

    first, second = traces
    if first["first_incident"] != second["first_incident"]:
        raise ValidationError(
            "the resident trace lost or replaced its frozen FIRST incident")
    sequence_delta = _trace_sequence_delta(
        int(first["sequence"]), int(second["sequence"]))
    first_by_sequence = {
        int(record["sequence"]): record for record in first["records"]
    }
    second_by_sequence = {
        int(record["sequence"]): record for record in second["records"]
    }
    overlap = set(first_by_sequence) & set(second_by_sequence)
    expected_overlap = min(
        len(first_by_sequence),
        max(0, TRACE_RECORD_CAPACITY - sequence_delta),
    )
    if len(overlap) < expected_overlap:
        raise ValidationError(
            "the second resident trace dump lost records that still fit in "
            "the ring")
    for sequence in overlap:
        if first_by_sequence[sequence] != second_by_sequence[sequence]:
            raise ValidationError(
                f"resident trace record {sequence:04X} changed between dumps")

    first_incident = second["first_incident"]
    if first_incident is None or first_incident["event"] != "DROP":
        raise ValidationError(
            "the deterministic MCP disconnect did not freeze a DROP incident")
    events = list(second["events"])
    for required in ("AUTO_RELISTEN", "SYSTEM_SUSPEND", "SYSTEM_RESUME"):
        if required not in events:
            raise ValidationError(
                f"resident trace did not retain required event {required}")

    return {
        "resident_trace_enabled": True,
        "resident_trace": second,
        "resident_trace_dump_commands": list(dump_commands),
        "resident_trace_dump_sizes": sizes,
        "resident_log_preserved": True,
        "resident_trace_events_between_dumps": sequence_delta,
        "resident_trace_overlapping_records": len(overlap),
        "basic_to_dos_via_system": True,
        "reset_vector_hits_before_system": reset_before,
        "system_warm_boot_vector_hits": reset_after_system - reset_before,
        "dump_command_warm_boot_vector_hits":
            reset_after_dump - reset_after_system,
        "reset_vector_hits_after_dump": reset_after_dump,
        "reset_vector_events_before_system": reset_events_before,
        "system_screen_reached_dos": _dos_prompt_visible(system_screen),
    }


def _install_reset_vector_trace(machine: object) -> None:
    """Count executions at 0000h after the resident has been installed."""
    machine.cmd(r'''
namespace eval msxaifaultreset {
    variable armed 0
    variable hits {}
    proc mark {} {
        variable armed
        if {!$armed} {return}
        variable hits
        lappend hits [list [machine_info time] [format %04X [reg PC]] \
            [format %04X [reg SP]] [format %04X [reg AF]]]
        if {[llength $hits] > 32} {set hits [lrange $hits end-31 end]}
    }
}
set msxaifaultreset::bp [debug set_bp 0x0000 \
    {$msxaifaultreset::armed} {msxaifaultreset::mark}]
set msxaifaultreset::hits {}
set msxaifaultreset::armed 1
''')


def _reset_vector_hits(machine: object) -> tuple[int, str]:
    raw = machine.cmd("set msxaifaultreset::hits")
    count = int(machine.cmd("llength $msxaifaultreset::hits"))
    return count, raw


def _install_unapi_io_trace(machine: object) -> None:
    """Trace lifecycle commands sent to the emulated UNAPI bridge."""
    machine.cmd(r'''
namespace eval msxaiunapitrace {
    variable events {}
    variable phase setup
    variable opens 0
    variable states 0
    variable aborts 0
    proc mark {} {
        variable events
        variable phase
        variable opens
        variable states
        variable aborts
        set value $::wp_last_value
        if {$value == 3} {incr opens}
        if {$value == 7} {incr states}
        if {$value == 8} {incr aborts}
        # STATE is intentionally counted but not retained: the resident polls
        # it thousands of times and would evict the lifecycle evidence.
        if {$value == 3 || $value == 8} {
            lappend events [list [machine_info time] $phase $value \
                [format %04X [reg PC]] [format %04X [reg SP]]]
            if {[llength $events] > 512} {
                set events [lrange $events end-511 end]
            }
        }
    }
}
set msxaiunapitrace::wp [debug set_watchpoint write_io 0x28 {} \
    {msxaiunapitrace::mark}]
''')


def _set_unapi_trace_phase(machine: object, phase: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", phase):
        raise ValueError(f"invalid UNAPI trace phase {phase!r}")
    machine.cmd(f"set msxaiunapitrace::phase {phase}")


def _unapi_io_trace_report(machine: object) -> dict[str, object]:
    return {
        "tcp_open_commands": int(machine.cmd("set msxaiunapitrace::opens")),
        "tcp_state_commands": int(machine.cmd("set msxaiunapitrace::states")),
        "tcp_abort_commands": int(machine.cmd("set msxaiunapitrace::aborts")),
        "retained_event_count": int(
            machine.cmd("llength $msxaiunapitrace::events")),
        "events_tcl": machine.cmd("set msxaiunapitrace::events"),
    }


def _machine_health(machine: object, previous_time: float,
                    reset_count: int, phase: str) -> dict[str, object]:
    """Assert that a network fault did not reboot or kill the emulator."""
    if machine.proc is None or machine.proc.poll() is not None:
        raise ValidationError(f"openMSX exited during {phase}")
    power = machine.cmd("set power").strip().lower()
    if power not in ("on", "true", "1"):
        raise ValidationError(f"MSX power changed to {power!r} during {phase}")
    emulator_time = float(machine.cmd("machine_info time"))
    if emulator_time <= previous_time:
        raise ValidationError(
            f"machine time did not progress during {phase}: "
            f"{previous_time:.6f} -> {emulator_time:.6f}")
    observed_resets, reset_events = _reset_vector_hits(machine)
    if observed_resets != reset_count:
        raise ValidationError(
            f"MSX executed the reset vector during {phase}: {reset_events}")
    return {
        "power": "on",
        "emulator_time": emulator_time,
        "reset_vector_hits": observed_resets,
        "openmsx_process_alive": True,
    }


def _assert_command_screen(command: str, screen: str, *, prompt: bool) -> None:
    lowered = screen.lower()
    compact = re.sub(r"\s+", "", lowered)
    error_markers = (
        "error:",
        "bad command",
        "file not found",
        "not enough memory",
        "wrong version of dos",
        "unapi implementation not found",
        "unapinet extension not found",
        "no unapi ram helper",
        "no free ram segment",
        "no free segment for memman",
        "passive tcp not supported",
        "passive connections not supported",
        "invalid port",
        "transport initialization failed",
        "msxai unapi relisten failed",
        "resident agent rejected",
        "memman 2.4 or newer is required",
        "inconsistent resident agent state",
        "resident loader failed",
    )
    errors = [
        marker for marker in error_markers
        if marker in lowered or marker.replace(" ", "") in compact
    ]
    if errors:
        raise ValidationError(
            f"MSX command {command!r} reported {', '.join(errors)}:\n{screen}")
    if prompt and not _dos_prompt_visible(screen):
        raise ValidationError(
            f"MSX command {command!r} did not return to a DOS prompt:\n{screen}")


def _openmsx_memory(machine: object, address: int, size: int) -> bytes:
    value = machine.cmd(
        f"set d [debug read_block memory {address} {size}]; "
        "binary scan $d H* h; set h")
    try:
        return bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValidationError(
            f"openMSX returned invalid debugger bytes: {value!r}") from exc


def _mcp_result_text(result: object, tool_name: str) -> str:
    blocks = getattr(result, "content", ())
    text = "\n".join(
        block.text for block in blocks
        if isinstance(getattr(block, "text", None), str))
    if getattr(result, "is_error", False):
        raise ValidationError(
            f"public MCP tool {tool_name} failed: {text or result!r}")
    return text


async def _mcp_call(client: object, tool_name: str,
                    arguments: Mapping[str, object] | None = None,
                    *, read_timeout: float | None = None):
    result = await client.call_tool(
        tool_name, dict(arguments or {}),
        read_timeout_seconds=read_timeout)
    _mcp_result_text(result, tool_name)
    return result


async def _mcp_connect(client: object, host: str, port: int,
                       timeout: float) -> None:
    """Retry the public MCP connect tool while a listener is coming up."""
    import anyio  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        attempt_timeout = min(5.0, max(0.2, remaining))
        try:
            await _mcp_call(
                client,
                "msx_agent_connect",
                {"host": host, "port": port, "timeout": attempt_timeout},
                read_timeout=attempt_timeout + 5.0,
            )
            return
        except ValidationError as exc:
            last_error = exc
        await anyio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    raise ValidationError(
        f"could not connect through msx_agent_connect to the emulated UNAPI "
        f"listener at {host}:{port} within {timeout:.1f}s: {last_error}")


async def _mcp_status(client: object) -> dict[str, object]:
    result = await _mcp_call(client, "msx_agent_status")
    status = getattr(result, "structured_content", None)
    if not isinstance(status, dict):
        raise ValidationError(
            "msx_agent_status did not return MCP structured content")
    return status


async def _mcp_memory_read(client: object, address: int, size: int) -> bytes:
    result = await _mcp_call(
        client,
        "msx_agent_memory_read",
        {"space": "ram", "address": address, "length": size,
         "atomic": False},
    )
    structured = getattr(result, "structured_content", None)
    payload = structured.get("result") if isinstance(structured, dict) else None
    if not isinstance(payload, str) or "\n" not in payload:
        raise ValidationError(
            "msx_agent_memory_read returned an invalid MCP payload")
    try:
        data = bytes.fromhex(payload.rsplit("\n", 1)[-1])
    except ValueError as exc:
        raise ValidationError(
            "msx_agent_memory_read returned invalid hexadecimal bytes") from exc
    if len(data) != size:
        raise ValidationError(
            f"msx_agent_memory_read returned {len(data)} bytes, expected {size}")
    return data


async def _mcp_disconnect(client: object) -> None:
    await _mcp_call(client, "msx_agent_disconnect")
    status = await _mcp_status(client)
    if status.get("state") != "disconnected":
        raise ValidationError(
            "msx_agent_disconnect did not leave the MCP agent channel "
            f"disconnected: {status!r}")


def _agent_snapshot(status: dict[str, object]) -> dict[str, object]:
    if status.get("state") != "running":
        raise ValidationError(
            f"resident agent status is not running: {status!r}")
    return {
        "peer": status.get("peer"),
        "local_endpoint": status.get("local_endpoint"),
        "status": status,
        "agent_transport_id": status.get("agent_transport_id"),
        "agent_transport": status.get("agent_transport"),
        "runtime_mode_id": status.get("runtime_mode_id"),
        "runtime_mode": status.get("runtime_mode"),
        "network_role": status.get("network_role"),
    }


def _assert_agent_identity(status: dict[str, object]) -> None:
    if (status.get("agent_transport_id") != 2 or
            status.get("agent_transport") != "tcpip-unapi"):
        raise ValidationError(
            "agent handshake did not report transport 2/tcpip-unapi: "
            f"{status.get('agent_transport_id')!r}/"
            f"{status.get('agent_transport')!r}")
    if status.get("runtime_mode") != "resident":
        raise ValidationError(
            f"agent handshake did not report resident runtime: "
            f"{status.get('runtime_mode')!r}")


@contextlib.contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _failure_diagnostics(machine: object | None, phase: str,
                         settings: Settings, distribution: Distribution) -> str:
    diagnostics: dict[str, object] = {
        "phase": phase,
        "release": RELEASE,
        "asset": distribution.asset_name,
        "binary": str(distribution.binary),
        "machine": settings.machine,
        "host": settings.host,
        "port": settings.port,
    }
    if machine is not None:
        try:
            machine.cmd("debug break")
            pc = int(machine.cmd("reg PC"))
            sp = int(machine.cmd("reg SP"))
            diagnostics["cpu"] = {
                "registers": machine.cmd("cpuregs"),
                "pc": f"0x{pc:04X}",
                "sp": f"0x{sp:04X}",
                "code": machine.cmd(
                    f"set d [debug read_block memory {pc} 32]; "
                    "binary scan $d H* h; set h"),
                "stack": machine.cmd(
                    f"set d [debug read_block memory {sp} 32]; "
                    "binary scan $d H* h; set h"),
            }
        except Exception as exc:
            diagnostics["cpu_diagnostics_error"] = str(exc)
        try:
            diagnostics["screen"] = machine.screen_text()
        except Exception as exc:
            diagnostics["screen_error"] = str(exc)
        try:
            diagnostics["extensions"] = machine.cmd("list_extensions")
        except Exception as exc:
            diagnostics["extensions_error"] = str(exc)
        try:
            diagnostics["slotmap"] = machine.cmd("slotmap")
            diagnostics["mapper_segments"] = {
                "slot_1_1": machine.cmd("get_mapper_size 1 1"),
                "slot_3_2": machine.cmd("get_mapper_size 3 2"),
            }
        except Exception as exc:
            diagnostics["mapper_diagnostics_error"] = str(exc)
        try:
            diagnostics["unapi_io_trace"] = _unapi_io_trace_report(machine)
        except Exception as exc:
            diagnostics["unapi_io_trace_error"] = str(exc)
        try:
            diagnostics["resident_hook_observer"] = (
                _resident_hook_observer_report(machine))
        except Exception as exc:
            diagnostics["resident_hook_observer_error"] = str(exc)
        output_tail = getattr(machine, "_output_tail", "")
        if output_tail:
            diagnostics["openmsx_output_tail"] = output_tail[-4000:]
    return json.dumps(diagnostics, indent=2, sort_keys=True, default=str)


async def _exercise_public_mcp_async(
        machine: object, settings: Settings, commands: Sequence[str],
        set_phase: Callable[[str], None], mcp_stderr: pathlib.Path,
        relisten_symbols: Mapping[str, int],
        ) -> dict[str, object]:
    """Exercise both connections through the standards-based MCP runtime."""
    from mcp.client import Client  # pylint: disable=import-outside-toplevel
    from mcp.client.stdio import (  # pylint: disable=import-outside-toplevel
        StdioServerParameters,
        stdio_client,
    )

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "server", "--transport", "stdio",
            "--log-level", "error",
        ],
        cwd=settings.root,
        env=os.environ.copy(),
    )
    connected = False
    with mcp_stderr.open("w+", encoding="utf-8") as error_log:
        client = Client(
            stdio_client(parameters, errlog=error_log),
            mode="auto",
            read_timeout_seconds=max(15.0, settings.timeout + 10.0),
            cache=None,
        )
        try:
            async with client:
                tools = await client.list_tools()
                published = {tool.name for tool in tools.tools}
                missing = sorted(set(MCP_TOOLS_EXERCISED) - published)
                if missing:
                    raise ValidationError(
                        f"public MCP server omitted required tools: {missing}")
                protocol = str(client.protocol_version)

                try:
                    # The MCP server is the host TCP client. The emulated MSX
                    # owns the passive listener, matching Pico/Pico+ direction.
                    machine.cmd("set throttle on")
                    set_phase("first public-MCP host-to-MSX TCP handshake")
                    await _mcp_connect(
                        client, settings.host, settings.port, settings.timeout)
                    connected = True
                    first_status = await _mcp_status(client)
                    _assert_agent_identity(first_status)
                    first = _agent_snapshot(first_status)
                    test_certificate = _enable_test_only_hook_relisten(
                        machine, first_status, relisten_symbols)

                    set_phase("public-MCP bidirectional SEND/RCV transaction")
                    via_agent = await _mcp_memory_read(
                        client, MEMORY_TEST_ADDRESS, 64)
                    # Keep a separate openMSX debugger read as the oracle. It
                    # does not carry agent traffic or replace any MCP call.
                    via_debugger = _openmsx_memory(
                        machine, MEMORY_TEST_ADDRESS, 64)
                    if via_agent != via_debugger:
                        raise ValidationError(
                            "agent RAM bytes obtained through MCP differ from "
                            "openMSX debugger bytes at C000h; TCPIP_TCP_SEND/"
                            "RCV path is not transparent")

                    reset_count, _reset_events = _reset_vector_hits(machine)
                    previous_time = float(machine.cmd("machine_info time"))
                    lifecycle_before = _unapi_lifecycle_counts(machine)
                    set_phase(
                        "public-MCP disconnect and zero-input H.TIMI relisten")
                    await _mcp_disconnect(client)
                    connected = False
                    _advance_h_timi_without_input(machine)
                    set_phase("second public-MCP host-to-MSX TCP handshake")
                    await _mcp_connect(
                        client, settings.host, settings.port, settings.timeout)
                    connected = True
                    second_status = await _mcp_status(client)
                    _assert_agent_identity(second_status)
                    second = _agent_snapshot(second_status)
                    await _mcp_memory_read(
                        client, MEMORY_TEST_ADDRESS, 16)
                    _advance_h_timi_without_input(
                        machine, AUTO_RELISTEN_STABILITY_SECONDS)
                    dos_lifecycle = _assert_single_auto_relisten(
                        lifecycle_before, _unapi_lifecycle_counts(machine),
                        "public-MCP DOS FIN recovery")
                    dos_health = _machine_health(
                        machine, previous_time, reset_count,
                        "public-MCP DOS FIN recovery")
                    previous_time = float(dos_health["emulator_time"])

                    # Keep the second socket alive while BASIC starts a tight
                    # program. No prompt, CHGET, H.CRUN, or keyboard boundary is
                    # available after RUN, so recovery here specifically proves
                    # the guarded timer-side lifecycle path.
                    set_phase("enter a running BASIC loop with MCP connected")
                    machine.type_line("BASIC")
                    machine.cmd("set throttle off")
                    machine.advance(1)
                    machine.type_line("10 GOTO 10")
                    machine.advance(0.5)
                    machine.type_line("RUN")
                    machine.advance(1)
                    machine.cmd("set throttle on")
                    _assert_agent_identity(await _mcp_status(client))
                    lifecycle_before = _unapi_lifecycle_counts(machine)
                    await _mcp_disconnect(client)
                    connected = False
                    _advance_h_timi_without_input(machine)
                    set_phase("zero-input MCP reconnect during BASIC loop")
                    await _mcp_connect(
                        client, settings.host, settings.port, settings.timeout)
                    connected = True
                    third_status = await _mcp_status(client)
                    _assert_agent_identity(third_status)
                    third = _agent_snapshot(third_status)
                    await _mcp_memory_read(
                        client, MEMORY_TEST_ADDRESS, 16)
                    _advance_h_timi_without_input(
                        machine, AUTO_RELISTEN_STABILITY_SECONDS)
                    basic_lifecycle = _assert_single_auto_relisten(
                        lifecycle_before, _unapi_lifecycle_counts(machine),
                        "running BASIC loop FIN recovery")
                    basic_health = _machine_health(
                        machine, previous_time, reset_count,
                        "running BASIC loop FIN recovery")

                    # Only after reconnection and stability have been proved may
                    # the harness stop its controlled loop for the optional
                    # BASIC -> DOS trace exercise that follows.
                    machine.press("CTRL+STOP")
                    _advance_h_timi_without_input(machine, 1.0)
                    basic_screen = machine.screen_text()
                    if re.search(r"(?im)^\s*Ok\s*$", basic_screen) is None:
                        raise ValidationError(
                            "CTRL+STOP did not return the controlled BASIC loop "
                            "to its prompt after automatic relisten:\n" +
                            basic_screen)
                    hook_observer = _resident_hook_observer_report(machine)
                    if hook_observer["gate_writes"] != 2:
                        raise ValidationError(
                            "UNAPINET test gate did not authorize exactly the "
                            "two proved automatic relistens: " +
                            repr(hook_observer))
                    await _mcp_disconnect(client)
                    connected = False

                    return {
                        "mcp_protocol": protocol,
                        "mcp_tools_exercised": list(MCP_TOOLS_EXERCISED),
                        "host_control_path": "public MCP tools over STDIO",
                        "test_only_hook_relisten_certificate":
                            test_certificate,
                        "test_only_hook_relisten_observer": hook_observer,
                        "first_connection": first,
                        "second_connection": second,
                        "third_connection": third,
                        "automatic_dos_h_timi_relisten": True,
                        "automatic_basic_loop_h_timi_relisten": True,
                        "machine_input_after_disconnect": False,
                        "dos_relisten_lifecycle": dos_lifecycle,
                        "basic_loop_relisten_lifecycle": basic_lifecycle,
                        "dos_relisten_health": dos_health,
                        "basic_loop_relisten_health": basic_health,
                        "memory_compare": {
                            "address": MEMORY_TEST_ADDRESS,
                            "length": 64,
                            "matched_openmsx_debugger": True,
                            "agent_read_path": "msx_agent_memory_read",
                            "oracle": "independent openMSX debugger read",
                        },
                    }
                finally:
                    if connected:
                        # Exercise the public cleanup operation even on failed
                        # assertions. The MCP lifespan remains the final guard.
                        with contextlib.suppress(Exception):
                            await _mcp_call(
                                client, "msx_agent_disconnect",
                                read_timeout=10.0)
        except Exception as exc:
            error_log.flush()
            error_log.seek(0)
            stderr = error_log.read().strip()
            suffix = f"\nMCP server stderr:\n{stderr}" if stderr else ""
            if isinstance(exc, ValidationError):
                raise ValidationError(str(exc) + suffix) from exc
            raise ValidationError(
                f"public MCP STDIO session failed: "
                f"{_exception_tree(exc)}{suffix}") from exc


def _exercise_public_mcp(
        machine: object, settings: Settings, commands: Sequence[str],
        set_phase: Callable[[str], None], mcp_stderr: pathlib.Path,
        relisten_symbols: Mapping[str, int],
        ) -> dict[str, object]:
    import anyio  # pylint: disable=import-outside-toplevel

    return anyio.run(
        _exercise_public_mcp_async,
        machine, settings, commands, set_phase, mcp_stderr,
        relisten_symbols,
    )


async def _forget_faulted_mcp_session(client: object) -> None:
    """Drop host-side protocol state after the proxy has severed its socket."""
    with contextlib.suppress(Exception):
        await _mcp_call(
            client, "msx_agent_disconnect", read_timeout=5.0)


def _advance_h_timi_without_input(
        machine: object, seconds: float = AUTO_RELISTEN_WAIT_SECONDS) -> None:
    """Run a bounded emulated-time window without injecting machine input."""
    if seconds <= 0:
        raise ValueError("H.TIMI advance must be positive")
    machine.cmd("set throttle off")
    machine.advance(seconds)
    machine.cmd("set throttle on")


def _enable_test_only_hook_relisten(
        machine: object, status: Mapping[str, object],
        symbols: Mapping[str, int]) -> dict[str, object]:
    """Certify only the UNAPINET fixture at a mapped resident-hook boundary."""
    resident_base = status.get("resident_base")
    resident_entry = status.get("resident_entry")
    linked_start = int(symbols["resident_start"])
    expected_page = linked_start & 0xFF00
    if resident_base != expected_page:
        raise ValidationError(
            "resident handshake base does not match the linked test image: "
            f"{resident_base!r} != 0x{expected_page:04X}")
    if not isinstance(resident_entry, int):
        raise ValidationError(
            "resident handshake omitted its exact relocated entry address")
    delta = resident_entry - linked_start
    runtime_symbols = {
        name: int(address) + delta for name, address in symbols.items()
    }
    if (runtime_symbols["resident_start"] != resident_entry or
            any(not 0x4000 <= address < 0x8000
                for address in runtime_symbols.values())):
        raise ValidationError(
            "resident handshake produced an invalid page-1 relocation delta: "
            f"entry=0x{resident_entry:04X}, delta={delta:+d}")
    hook = runtime_symbols["resident_timi_hook"]
    certificate_gate = runtime_symbols[
        "unapi_service_relisten_certificate_ready"]
    certificate = runtime_symbols["unapi_hook_relisten_certified"]
    observed_names = (
        "unapi_hook_relisten_certified", "unapi_relisten_pending",
        "unapi_retry_count", "transport_session_lost",
        "unapi_connection", "unapi_connection_state",
        "unapi_lifecycle_busy", "unapi_busy", "unapi_last_error",
        "runtime_mode", "hook_kind", "hook_system_suspended",
        "tsr_heap_fault", "in_hook",
    )
    observed_reads = " ".join(
            f"[debug read memory {runtime_symbols[name]}]"
            for name in observed_names)
    machine.cmd(f'''
namespace eval msxaitestcert {{
    variable armed 1
    variable hits 0
    variable writes 0
    variable gate_writes 0
    variable observed 0
    variable last {{}}
    variable transitions {{}}
    variable certificate {certificate}
    proc mark {{}} {{
        variable armed
        variable hits
        variable writes
        variable observed
        variable last
        variable transitions
        variable certificate
        if {{$armed}} {{
            debug write memory $certificate 1
            incr writes
            set armed 0
        }}
        set observed [debug read memory $certificate]
        set current [list {observed_reads}]
        if {{$current ne $last}} {{
            lappend transitions [list [machine_info time] $current]
            if {{[llength $transitions] > 128}} {{
                set transitions [lrange $transitions end-127 end]
            }}
            set last $current
        }}
        incr hits
    }}
    proc authorize {{}} {{
        variable gate_writes
        variable certificate
        debug write memory $certificate 1
        incr gate_writes
    }}
}}
set msxaitestcert::bp [debug set_bp {hook} {{}} {{msxaitestcert::mark}}]
set msxaitestcert::gate_bp [debug set_bp {certificate_gate} {{}} \
    {{msxaitestcert::authorize}}]
''')
    _advance_h_timi_without_input(machine, 0.25)
    hits = int(machine.cmd("set msxaitestcert::hits"))
    writes = int(machine.cmd("set msxaitestcert::writes"))
    observed = int(machine.cmd("set msxaitestcert::observed"))
    if hits < 1 or writes != 1 or observed != 1:
        raise ValidationError(
            "test-only UNAPINET relisten certification was not applied at "
            f"the mapped resident H.TIMI boundary: hits={hits}, "
            f"writes={writes}, observed={observed}")
    return {
        "scope": "UNAPINET/openMSX harness only",
        "production_gate_weakened": False,
        "resident_timi_hook": f"0x{hook:04X}",
        "certificate_address": f"0x{certificate:04X}",
        "certificate_gate": f"0x{certificate_gate:04X}",
        "resident_entry": f"0x{resident_entry:04X}",
        "runtime_relocation_delta": delta,
        "mapped_hook_writes": writes,
        "mapped_hook_observations": hits,
        "observer_fields": list(observed_names),
    }


def _resident_hook_observer_report(machine: object) -> dict[str, object]:
    """Return the live mapped-hook state retained by the emulator fixture."""
    return {
        "hits": int(machine.cmd("set msxaitestcert::hits")),
        "writes": int(machine.cmd("set msxaitestcert::writes")),
        "gate_writes": int(
            machine.cmd("set msxaitestcert::gate_writes")),
        "certificate_observed": int(
            machine.cmd("set msxaitestcert::observed")),
        "last_values_tcl": machine.cmd("set msxaitestcert::last"),
        "transitions_tcl": machine.cmd(
            "set msxaitestcert::transitions"),
    }


def _unapi_lifecycle_counts(machine: object) -> dict[str, int]:
    """Snapshot lifecycle commands without copying the large retained trace."""
    report = _unapi_io_trace_report(machine)
    return {
        "tcp_open_commands": int(report["tcp_open_commands"]),
        "tcp_abort_commands": int(report["tcp_abort_commands"]),
    }


def _assert_single_auto_relisten(
        before: Mapping[str, int], after: Mapping[str, int],
        phase: str) -> dict[str, int]:
    """Require one ABORT/OPEN pair and reject retry/open storms."""
    delta = {
        name: int(after[name]) - int(before[name])
        for name in ("tcp_abort_commands", "tcp_open_commands")
    }
    if delta != {"tcp_abort_commands": 1, "tcp_open_commands": 1}:
        raise ValidationError(
            f"{phase} did not perform exactly one automatic TCP_ABORT/TCP_OPEN "
            f"lifecycle: before={dict(before)!r}, after={dict(after)!r}, "
            f"delta={delta!r}")
    return delta


def _assert_blackhole_lifecycle_unchanged(
        before: Mapping[str, int], after: Mapping[str, int], phase: str) -> None:
    """A forwarding blackhole must not masquerade as a detected TCP close."""
    if dict(before) != dict(after):
        raise ValidationError(
            f"{phase} unexpectedly changed the listener lifecycle while "
            f"TCP_STATE remained established: before={dict(before)!r}, "
            f"after={dict(after)!r}")


async def _exercise_fault_matrix_async(
        machine: object, settings: Settings,
        set_phase: Callable[[str], None], mcp_stderr: pathlib.Path,
        cycles: int, relisten_symbols: Mapping[str, int]) -> dict[str, object]:
    """Inject repeatable host-side TCP faults into one emulated MSX."""
    from mcp.client import Client  # pylint: disable=import-outside-toplevel
    from mcp.client.stdio import (  # pylint: disable=import-outside-toplevel
        StdioServerParameters,
        stdio_client,
    )
    from tools.tcp_fault_proxy import (  # pylint: disable=import-outside-toplevel
        TCPFaultProxy,
    )

    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0:
        raise ValueError("fault cycles must be a positive integer")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "server", "--transport", "stdio", "--log-level", "error"],
        cwd=settings.root,
        env=os.environ.copy(),
    )
    events: list[dict[str, object]] = []
    reset_count, _reset_events = _reset_vector_hits(machine)
    previous_time = float(machine.cmd("machine_info time"))

    with TCPFaultProxy(settings.host, settings.port) as proxy:
        proxy_host, proxy_port = proxy.endpoint
        with mcp_stderr.open("w+", encoding="utf-8") as error_log:
            client = Client(
                stdio_client(parameters, errlog=error_log),
                mode="auto",
                read_timeout_seconds=max(15.0, settings.timeout + 10.0),
                cache=None,
            )
            connected = False
            try:
                async with client:
                    set_phase("fault matrix initial proxied MCP handshake")
                    _set_unapi_trace_phase(machine, "initial-handshake")
                    await _mcp_connect(
                        client, proxy_host, proxy_port, settings.timeout)
                    connected = True
                    first_status = await _mcp_status(client)
                    _assert_agent_identity(first_status)
                    first = _agent_snapshot(first_status)
                    test_certificate = _enable_test_only_hook_relisten(
                        machine, first_status, relisten_symbols)

                    set_phase("steady-state framed traffic before fault injection")
                    _set_unapi_trace_phase(machine, "steady-state")
                    for index in range(STEADY_STATE_ROUND_TRIPS):
                        address = MEMORY_TEST_ADDRESS + ((index * 17) & 0xFF)
                        via_agent = await _mcp_memory_read(client, address, 16)
                        via_debugger = _openmsx_memory(machine, address, 16)
                        if via_agent != via_debugger:
                            raise ValidationError(
                                f"steady-state RAM mismatch at {address:04X}h "
                                f"on round trip {index + 1}")
                    health = _machine_health(
                        machine, previous_time, reset_count,
                        "steady-state framed traffic")
                    previous_time = float(health["emulator_time"])
                    events.append({
                        "scenario": "steady_state",
                        "round_trips": STEADY_STATE_ROUND_TRIPS,
                        "recovered": True,
                        "health": health,
                    })

                    for cycle in range(1, cycles + 1):
                        set_phase(f"fault cycle {cycle}: FIN at idle DOS prompt")
                        _set_unapi_trace_phase(machine, f"cycle-{cycle}-idle-fin")
                        lifecycle_before = _unapi_lifecycle_counts(machine)
                        before = proxy.wait_for_session()
                        cut = proxy.cut("fin")
                        connected = False
                        await _forget_faulted_mcp_session(client)
                        _advance_h_timi_without_input(machine)
                        await _mcp_connect(
                            client, proxy_host, proxy_port, settings.timeout)
                        connected = True
                        status = await _mcp_status(client)
                        _assert_agent_identity(status)
                        await _mcp_memory_read(
                            client, MEMORY_TEST_ADDRESS, 16)
                        _advance_h_timi_without_input(
                            machine, AUTO_RELISTEN_STABILITY_SECONDS)
                        lifecycle = _assert_single_auto_relisten(
                            lifecycle_before, _unapi_lifecycle_counts(machine),
                            f"fault cycle {cycle} idle FIN")
                        health = _machine_health(
                            machine, previous_time, reset_count,
                            f"fault cycle {cycle} idle FIN")
                        previous_time = float(health["emulator_time"])
                        events.append({
                            "cycle": cycle,
                            "scenario": "dos_idle_fin",
                            "proxy_session": before.number,
                            "fault": cut.fault,
                            "bytes_before_cut": {
                                "client_to_target": cut.client_to_target,
                                "target_to_client": cut.target_to_client,
                            },
                            "machine_input_after_fault": False,
                            "automatic_lifecycle": lifecycle,
                            "recovered": True,
                            "health": health,
                        })

                        set_phase(f"fault cycle {cycle}: RST at idle DOS prompt")
                        _set_unapi_trace_phase(
                            machine, f"cycle-{cycle}-idle-rst")
                        lifecycle_before = _unapi_lifecycle_counts(machine)
                        before = proxy.wait_for_session()
                        cut = proxy.cut("rst")
                        connected = False
                        await _forget_faulted_mcp_session(client)
                        _advance_h_timi_without_input(machine)
                        await _mcp_connect(
                            client, proxy_host, proxy_port, settings.timeout)
                        connected = True
                        status = await _mcp_status(client)
                        _assert_agent_identity(status)
                        await _mcp_memory_read(
                            client, MEMORY_TEST_ADDRESS, 16)
                        _advance_h_timi_without_input(
                            machine, AUTO_RELISTEN_STABILITY_SECONDS)
                        lifecycle = _assert_single_auto_relisten(
                            lifecycle_before, _unapi_lifecycle_counts(machine),
                            f"fault cycle {cycle} idle RST")
                        health = _machine_health(
                            machine, previous_time, reset_count,
                            f"fault cycle {cycle} idle RST")
                        previous_time = float(health["emulator_time"])
                        events.append({
                            "cycle": cycle,
                            "scenario": "dos_idle_rst",
                            "proxy_session": before.number,
                            "fault": cut.fault,
                            "machine_input_after_fault": False,
                            "automatic_lifecycle": lifecycle,
                            "recovered": True,
                            "health": health,
                        })

                        set_phase(
                            f"fault cycle {cycle}: blackhole then RST")
                        _set_unapi_trace_phase(
                            machine, f"cycle-{cycle}-blackhole-rst")
                        lifecycle_before = _unapi_lifecycle_counts(machine)
                        before = proxy.wait_for_session()
                        silence = proxy.cut("blackhole")
                        _advance_h_timi_without_input(machine)
                        silent_health = _machine_health(
                            machine, previous_time, reset_count,
                            f"fault cycle {cycle} blackhole")
                        previous_time = float(silent_health["emulator_time"])
                        _assert_blackhole_lifecycle_unchanged(
                            lifecycle_before, _unapi_lifecycle_counts(machine),
                            f"fault cycle {cycle} pure blackhole")
                        if not proxy.wait_for_session(
                                after=before.number - 1).connected:
                            raise ValidationError(
                                "blackholed proxy session closed unexpectedly")
                        cut = proxy.cut("rst")
                        connected = False
                        await _forget_faulted_mcp_session(client)
                        _advance_h_timi_without_input(machine)
                        await _mcp_connect(
                            client, proxy_host, proxy_port, settings.timeout)
                        connected = True
                        status = await _mcp_status(client)
                        _assert_agent_identity(status)
                        await _mcp_memory_read(
                            client, MEMORY_TEST_ADDRESS, 16)
                        _advance_h_timi_without_input(
                            machine, AUTO_RELISTEN_STABILITY_SECONDS)
                        lifecycle = _assert_single_auto_relisten(
                            lifecycle_before, _unapi_lifecycle_counts(machine),
                            f"fault cycle {cycle} blackhole terminal RST")
                        health = _machine_health(
                            machine, previous_time, reset_count,
                            f"fault cycle {cycle} post-blackhole recovery")
                        previous_time = float(health["emulator_time"])
                        events.append({
                            "cycle": cycle,
                            "scenario": "blackhole_then_rst",
                            "proxy_session": before.number,
                            "blackhole_observed_open": silence.connected,
                            "blackhole_detected_before_rst": False,
                            "terminal_fault": cut.fault,
                            "machine_input_after_fault": False,
                            "automatic_lifecycle": lifecycle,
                            "recovered": True,
                            "health_during_silence": silent_health,
                            "health": health,
                        })

                    hook_observer = _resident_hook_observer_report(machine)
                    expected_gate_writes = 3 * cycles
                    if hook_observer["gate_writes"] != expected_gate_writes:
                        raise ValidationError(
                            "UNAPINET test gate authorization count differs "
                            f"from {expected_gate_writes} automatic fault "
                            "recoveries: " + repr(hook_observer))
                    await _mcp_disconnect(client)
                    connected = False

                    return {
                        "host_control_path":
                            "public MCP tools through deterministic TCP proxy",
                        "test_only_hook_relisten_certificate":
                            test_certificate,
                        "test_only_hook_relisten_observer": hook_observer,
                        "first_connection": first,
                        "fault_cycles": cycles,
                        "fault_scenarios": list(FAULT_SCENARIOS),
                        "fault_matrix": events,
                        "proxy_endpoint": [proxy_host, proxy_port],
                        "proxy_sessions": [
                            dataclasses.asdict(item) for item in proxy.history
                        ],
                        "reset_vector_hits": _reset_vector_hits(machine)[0],
                        "unapi_io_trace": _unapi_io_trace_report(machine),
                    }
            except Exception as exc:
                error_log.flush()
                error_log.seek(0)
                stderr = error_log.read().strip()
                suffix = f"\nMCP server stderr:\n{stderr}" if stderr else ""
                if isinstance(exc, ValidationError):
                    raise ValidationError(str(exc) + suffix) from exc
                raise ValidationError(
                    f"fault matrix MCP session failed: "
                    f"{_exception_tree(exc)}{suffix}") from exc
            finally:
                if connected:
                    with contextlib.suppress(Exception):
                        await _mcp_call(
                            client, "msx_agent_disconnect", read_timeout=5.0)


def _exercise_fault_matrix(
        machine: object, settings: Settings,
        set_phase: Callable[[str], None], mcp_stderr: pathlib.Path,
        cycles: int, relisten_symbols: Mapping[str, int]) -> dict[str, object]:
    import anyio  # pylint: disable=import-outside-toplevel

    return anyio.run(
        _exercise_fault_matrix_async,
        machine, settings, set_phase, mcp_stderr, cycles,
        relisten_symbols,
    )


def run_validation(settings: Settings, *,
                   runner: Callable[..., subprocess.CompletedProcess[str]] =
                   subprocess.run,
                   fault_cycles: int = 0,
                   trace_validation: bool = False) -> dict[str, object]:
    """Run discovery, passive TCP, send/receive, close, and relisten E2E."""
    if (isinstance(fault_cycles, bool) or not isinstance(fault_cycles, int) or
            fault_cycles < 0):
        raise ValueError("fault_cycles must be a non-negative integer")
    if not isinstance(trace_validation, bool):
        raise TypeError("trace_validation must be a boolean")
    if trace_validation and fault_cycles:
        raise ValueError(
            "trace_validation and fault_cycles are separate single-instance "
            "validation modes")
    if trace_validation and settings.keep_open:
        raise ValueError(
            "trace validation powers off to inspect the FAT image and cannot "
            "be combined with keep_open")
    if trace_validation:
        _require_mcopy()
    validate_local_prerequisites(settings)
    canonical = build_agent_package(
        settings, development_trace=trace_validation, runner=runner)
    relisten_symbols = _test_only_relisten_symbol_addresses(
        settings, development_trace=trace_validation)
    machine = None
    phase = "prepare pinned distribution"
    with prepared_distribution(settings) as distribution:
        probe_binary(distribution, runner=runner)
        with tempfile.TemporaryDirectory(
                prefix="msx-ai-unapi-validation-") as runtime_name:
            runtime = pathlib.Path(runtime_name)
            disk = runtime / "msxdos.dsk"
            home = runtime / "openmsx-home"
            shutil.copy2(settings.dos_hdd, disk)
            shutil.copytree(
                settings.openmsx_home,
                home,
                ignore=shutil.ignore_patterns(*COPYTREE_IGNORES),
            )
            runtime_artifacts: list[tuple[str, pathlib.Path]] = []
            for name, source in zip(AGENT_PACKAGE_NAMES, canonical, strict=True):
                target = runtime / name
                shutil.copy2(source, target)
                runtime_artifacts.append((name, target))
            unapinet_target = runtime / "UNAPINET.COM"
            shutil.copy2(settings.unapinet_com, unapinet_target)
            runtime_artifacts.append(("UNAPINET.COM", unapinet_target))

            server_path = str(settings.root / "server")
            if server_path not in sys.path:
                sys.path.insert(0, server_path)
            from msx_client import OpenMSX  # pylint: disable=import-outside-toplevel

            try:
                phase = "launch openMSXnet with Nextor and UnapiNet extensions"
                machine = OpenMSX(
                    machine=settings.machine,
                    extensions=(
                        "slotexpander",
                        "SunriseIDE_Nextor",
                        "ram512k",
                        "unapinet",
                    ),
                    harddisk=str(disk),
                    home=home,
                    bin=str(distribution.binary),
                    config_mode="isolated",
                )
                with _temporary_environment({
                        "OPENMSX_SYSTEM_DATA": str(distribution.share)}):
                    machine.start(headless=not settings.window)
                mapper_segments = int(machine.cmd("get_mapper_size 1 1"))
                if mapper_segments != 32:
                    raise ValidationError(
                        "pinned ram512k extension was not mapped at slot 1.1: "
                        f"expected 32 segments, found {mapper_segments}")
                mapper_profile = {
                    "extension": "ram512k",
                    "slot": "1.1",
                    "segments": mapper_segments,
                    "capacity_kib": mapper_segments * 16,
                    "purpose": (
                        "resident headroom for Nextor, UNAPINET.COM, MemMan, "
                        "and MCPUNAPI.TSR"
                    ),
                    "physical_requirement": (
                        "implementation-dependent free mapper segments; "
                        "512 KiB is test-fixture headroom, not a Pico+ rule"
                    ),
                }

                phase = "import MSX-AI and UNAPINET.COM onto disposable disk"
                _import_suite(machine, runtime_artifacts)
                if trace_validation:
                    machine.cmd("diskmanipulator chdir hda1 /")
                    root_listing = machine.cmd("diskmanipulator dir hda1")
                    for name in (*TRACE_DUMP_NAMES, TRACE_FAILURE_DUMP_NAME):
                        if _dos_entry_size(root_listing, name) is not None:
                            machine.cmd(
                                f"diskmanipulator delete hda1 {name}")
                            root_listing = machine.cmd(
                                "diskmanipulator dir hda1")
                machine.power_on()
                machine.advance(14)
                initial_screen = machine.screen_text()
                if not _dos_prompt_visible(initial_screen):
                    raise ValidationError(
                        "Nextor/MSX-DOS did not reach a prompt after boot:\n"
                        + initial_screen)

                commands = msx_install_commands(
                    settings.port, trace=trace_validation)
                phase = "configure MSXAI_HOME and PATH"
                for command in commands[:2]:
                    machine.type_line(command)
                    machine.advance(0.5)

                phase = "install pinned UNAPINET.COM implementation"
                machine.type_line(commands[2])
                machine.advance(3)
                _assert_command_screen(
                    commands[2], machine.screen_text(), prompt=True)

                phase = "install resident MSX-AI UNAPI transport"
                machine.type_line(commands[3])
                machine.advance(15)
                screen = machine.screen_text()
                grace = 15
                while not _dos_prompt_visible(screen) and grace > 0:
                    machine.advance(1)
                    grace -= 1
                    screen = machine.screen_text()
                try:
                    _assert_command_screen(commands[3], screen, prompt=True)
                except ValidationError as install_error:
                    if not trace_validation:
                        raise
                    phase = "capture resident trace after first-install failure"
                    failed_trace = _capture_failed_install_trace(
                        machine, disk, runtime)
                    raise ValidationError(
                        f"{install_error}\nresident trace after failure:\n" +
                        json.dumps(failed_trace, indent=2, sort_keys=True)) \
                        from install_error

                _install_reset_vector_trace(machine)
                _install_unapi_io_trace(machine)

                def set_phase(value: str) -> None:
                    nonlocal phase
                    phase = value

                if fault_cycles:
                    phase = "initialize deterministic TCP fault matrix"
                    mcp_report = _exercise_fault_matrix(
                        machine, settings, set_phase,
                        runtime / "mcp-fault-stderr.log", fault_cycles,
                        relisten_symbols)
                else:
                    mcp_report = _exercise_public_mcp(
                        machine, settings, commands, set_phase,
                        runtime / "mcp-stderr.log", relisten_symbols)

                trace_report: dict[str, object] = {}
                if trace_validation:
                    phase = "return BASIC to DOS and validate resident trace"
                    trace_report = _exercise_resident_trace(
                        machine, disk, runtime)

                if settings.keep_open:
                    print(
                        "UNAPI E2E passed; openMSX will remain open until "
                        "you close its window.",
                        flush=True,
                    )
                    if machine.proc is not None:
                        machine.proc.wait()

                result = {
                    "ok": True,
                    "release": RELEASE,
                    "asset": distribution.asset_name,
                    "custom_port": settings.port,
                    "socket_direction":
                        "host TCP client -> emulated MSX passive listener",
                    "commands": list(commands),
                    "mapper_profile": mapper_profile,
                    **mcp_report,
                    **trace_report,
                    "contract_path_exercised": list(
                        FAULT_CONTRACT_PATH if fault_cycles
                        else CONTRACT_PATH),
                    "pinned_get_capab_block1_hl":
                        f"0x{PINNED_GET_CAPAB_BLOCK1_HL:04X}",
                    "pico_firmware_emulated": False,
                    "not_emulated": list(NOT_EMULATED),
                }
                if fault_cycles:
                    result.update({
                        "fault_injection": True,
                        "fault_scenarios_exercised": list(FAULT_SCENARIOS),
                    })
                else:
                    result.update({
                        "automatic_h_timi_relisten_without_machine_input": True,
                        "automatic_basic_loop_h_timi_relisten": True,
                    })
                if trace_validation:
                    result["resident_trace_validation"] = True
                return result
            except Exception as exc:
                if isinstance(exc, HarnessError):
                    message = str(exc)
                else:
                    message = f"{type(exc).__name__}: {exc}"
                diagnostics = _failure_diagnostics(
                    machine, phase, settings, distribution)
                raise ValidationError(
                    f"UNAPI validation failed during {phase}: {message}\n"
                    f"diagnostics:\n{diagnostics}") from exc
            finally:
                if machine is not None:
                    machine.close()


def _path_from_argument(value: str | os.PathLike[str] | None,
                        label: str) -> pathlib.Path:
    if value is None or not os.fspath(value).strip():
        raise PrerequisiteError(
            f"{label} was not supplied; pass its command-line option or set "
            "the documented environment variable")
    return pathlib.Path(value).expanduser().resolve()


def settings_from_namespace(namespace: argparse.Namespace) -> Settings:
    return Settings(
        archive=_path_from_argument(namespace.archive, "openMSXnet archive"),
        unapinet_com=_path_from_argument(
            namespace.unapinet_com, "UNAPINET.COM"),
        dos_hdd=_path_from_argument(namespace.dos_hdd, "MSX-DOS disk image"),
        openmsx_home=_path_from_argument(
            namespace.openmsx_home, "isolated openMSX home"),
        port=validate_port(namespace.port),
        machine=namespace.machine,
        host=namespace.host,
        timeout=namespace.timeout,
        make=namespace.make,
        window=namespace.window,
        keep_open=namespace.keep_open,
        root=ROOT,
    )


def _env(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return fallback if value is None or not value.strip() else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or run the pinned openMSXnet v0.9.7 TCP/IP UNAPI "
            "validation; no downloads or global installs are performed."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "faults", "trace"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--archive", default=_env("MSX_AI_UNAPINET_ARCHIVE"),
            help="matching openMSXnet v0.9.7 platform ZIP")
        command.add_argument(
            "--unapinet-com", default=_env("MSX_AI_UNAPINET_COM"),
            help="pinned v0.9.7 UNAPINET.COM release asset")
        command.add_argument(
            "--dos-hdd", default=_env(
                "MSX_AI_DOS_HDD", str(ROOT / "work/system-disks/msxdos.dsk")),
            help="licensed writable MSX-DOS 2/Nextor hard-disk image")
        command.add_argument(
            "--openmsx-home", default=_env(
                "MSX_AI_OPENMSX_HOME", str(ROOT / ".openmsx-home")),
            help="isolated home containing machine and licensed ROM files")
        command.add_argument(
            "--port", type=_arg_port,
            default=_env(
                "MSX_AI_UNAPI_TEST_PORT", str(DEFAULT_TEST_PORT)),
            help=(f"custom listener port, {MIN_TEST_PORT}..{MAX_TEST_PORT} "
                  f"(default: {DEFAULT_TEST_PORT})"))
        command.add_argument(
            "--machine", default=_env(
                "MSX_AI_BASIC_MACHINE", "Gradiente_Expert20"))
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--timeout", type=float, default=60.0)
        command.add_argument("--make", default=_env("MAKE", "make"))
        command.add_argument(
            "--window", action="store_true",
            help="show the emulator window during the E2E run")
        command.add_argument(
            "--keep-open", action="store_true",
            help="leave the visible emulator running until its window closes")
        if name == "faults":
            command.add_argument(
                "--cycles", type=_arg_fault_cycles,
                default=DEFAULT_FAULT_CYCLES,
                help=("number of FIN/RST/blackhole cycles in the single "
                      f"openMSX instance (default: {DEFAULT_FAULT_CYCLES})"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    try:
        settings = settings_from_namespace(namespace)
        if namespace.command == "preflight":
            result = preflight(settings)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ready"] else 2
        if namespace.command == "faults":
            result = run_validation(settings, fault_cycles=namespace.cycles)
        elif namespace.command == "trace":
            result = run_validation(settings, trace_validation=True)
        else:
            result = run_validation(settings)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except HarnessError as exc:
        print(json.dumps({
            "ok": False,
            "release": RELEASE,
            "problem": str(exc),
            "pico_firmware_emulated": False,
        }, indent=2, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
