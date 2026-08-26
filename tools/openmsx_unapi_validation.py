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

RELEASE = "v0.9.7"
RELEASE_URL = "https://github.com/antxiko/openMSXnet/releases/tag/v0.9.7"
RELEASE_DOWNLOAD_BASE = (
    "https://github.com/antxiko/openMSXnet/releases/download/v0.9.7"
)
DEFAULT_TEST_PORT = 43123
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
    "MCPUNAPI.TSR",
    "TU.COM",
    "MP.COM",
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
    "foreground TCP abort and passive relisten after host disconnect",
)
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


def build_agent_package(settings: Settings, *,
                        runner: Callable[..., subprocess.CompletedProcess[str]] =
                        subprocess.run) -> tuple[pathlib.Path, ...]:
    try:
        result = runner(
            [settings.make, "agent"], cwd=settings.root,
            capture_output=True, text=True)
    except OSError as exc:
        raise PrerequisiteError(
            f"could not run canonical `make agent`: {exc}") from exc
    if result.returncode != 0:
        output = result.stderr or result.stdout or "<no build output>"
        raise PrerequisiteError(
            "canonical `make agent` failed:\n" + output.strip())
    artifact_root = settings.root / "work" / "agent"
    artifacts = tuple(artifact_root / name for name in AGENT_PACKAGE_NAMES)
    missing = [str(path) for path in artifacts
               if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise PrerequisiteError(
            "canonical agent build is incomplete: " + ", ".join(missing))
    return artifacts


def msx_install_commands(port: int) -> tuple[str, ...]:
    port = validate_port(port)
    return (
        f"SET MSXAI_HOME={SUITE_HOME}",
        f"PATH {SUITE_HOME};%PATH%",
        "UNAPINET",
        f"MSXAI /DRIVER:UNAPI /PORT:{port}",
    )


def _dos_entry_size(listing: str, name: str) -> int | None:
    match = re.search(
        rf"(?im)^\s*{re.escape(name)}\s+\S+\s+(\d+)\s*$", listing)
    return None if match is None else int(match.group(1))


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
        output_tail = getattr(machine, "_output_tail", "")
        if output_tail:
            diagnostics["openmsx_output_tail"] = output_tail[-4000:]
    return json.dumps(diagnostics, indent=2, sort_keys=True, default=str)


async def _exercise_public_mcp_async(
        machine: object, settings: Settings, commands: Sequence[str],
        set_phase: Callable[[str], None], mcp_stderr: pathlib.Path,
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

                    set_phase(
                        "public-MCP disconnect and foreground passive relisten")
                    await _mcp_disconnect(client)
                    connected = False
                    machine.cmd("set throttle off")
                    machine.advance(3)
                    # Lifecycle calls are deliberately not issued from H.TIMI:
                    # Pico/Pico+ advertises TCP_OPEN as potentially blocking. A
                    # same-driver TsrCall is the safe foreground relisten path.
                    machine.type_line(commands[3])
                    machine.advance(3)
                    screen = machine.screen_text()
                    grace = 15
                    while not _dos_prompt_visible(screen) and grace > 0:
                        machine.advance(1)
                        grace -= 1
                        screen = machine.screen_text()
                    _assert_command_screen(commands[3], screen, prompt=True)

                    machine.cmd("set throttle on")
                    set_phase("second public-MCP host-to-MSX TCP handshake")
                    await _mcp_connect(
                        client, settings.host, settings.port, settings.timeout)
                    connected = True
                    second_status = await _mcp_status(client)
                    _assert_agent_identity(second_status)
                    second = _agent_snapshot(second_status)
                    await _mcp_memory_read(
                        client, MEMORY_TEST_ADDRESS, 16)
                    await _mcp_disconnect(client)
                    connected = False

                    return {
                        "mcp_protocol": protocol,
                        "mcp_tools_exercised": list(MCP_TOOLS_EXERCISED),
                        "host_control_path": "public MCP tools over STDIO",
                        "first_connection": first,
                        "second_connection": second,
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
        ) -> dict[str, object]:
    import anyio  # pylint: disable=import-outside-toplevel

    return anyio.run(
        _exercise_public_mcp_async,
        machine, settings, commands, set_phase, mcp_stderr,
    )


def run_validation(settings: Settings, *,
                   runner: Callable[..., subprocess.CompletedProcess[str]] =
                   subprocess.run) -> dict[str, object]:
    """Run discovery, passive TCP, send/receive, close, and relisten E2E."""
    validate_local_prerequisites(settings)
    canonical = build_agent_package(settings, runner=runner)
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
                machine.power_on()
                machine.advance(14)
                initial_screen = machine.screen_text()
                if not _dos_prompt_visible(initial_screen):
                    raise ValidationError(
                        "Nextor/MSX-DOS did not reach a prompt after boot:\n"
                        + initial_screen)

                commands = msx_install_commands(settings.port)
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
                _assert_command_screen(commands[3], screen, prompt=True)

                def set_phase(value: str) -> None:
                    nonlocal phase
                    phase = value

                mcp_report = _exercise_public_mcp(
                    machine, settings, commands, set_phase,
                    runtime / "mcp-stderr.log")

                if settings.keep_open:
                    print(
                        "UNAPI E2E passed; openMSX will remain open until "
                        "you close its window.",
                        flush=True,
                    )
                    if machine.proc is not None:
                        machine.proc.wait()

                return {
                    "ok": True,
                    "release": RELEASE,
                    "asset": distribution.asset_name,
                    "custom_port": settings.port,
                    "socket_direction":
                        "host TCP client -> emulated MSX passive listener",
                    "commands": list(commands),
                    "mapper_profile": mapper_profile,
                    **mcp_report,
                    "foreground_relisten_after_host_close": True,
                    "contract_path_exercised": list(CONTRACT_PATH),
                    "pinned_get_capab_block1_hl":
                        f"0x{PINNED_GET_CAPAB_BLOCK1_HL:04X}",
                    "pico_firmware_emulated": False,
                    "not_emulated": list(NOT_EMULATED),
                }
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
    for name in ("preflight", "run"):
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
