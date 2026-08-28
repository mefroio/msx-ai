#!/usr/bin/env python3
"""Perform an isolated, hardware-free MSX-AI release validation.

The checker deliberately never starts openMSX.  It validates the ordinary
unit suite, distribution contents, a clean wheel install, import-time state
discipline, and both MCP protocol eras over the public transports.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Awaitable, Callable, Mapping
import hashlib
import io
import importlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DATE_EPOCH = "1704067200"  # 2024-01-01T00:00:00Z
COMMAND_TIMEOUT = 600
MCP_TIMEOUT = 60

_CORE_MODULES = ("anyio", "jsonschema", "mcp", "uvicorn")
_BUILD_MODULES = ("build", "setuptools")
_IGNORED_DIRECTORY_NAMES = {
    ".agents",
    ".claude",
    ".codex",
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".openmsx-home",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
    "work",
}
_FORBIDDEN_SEGMENTS = {
    ".openmsx-home",
    "persistent",
    "recordings",
    "replays",
    "savestates",
    "screenshots",
    "setups",
    "systemroms",
    "work",
}
_FORBIDDEN_BASENAMES = {
    ".mcp.json",
}
_FORBIDDEN_SUFFIXES = {
    ".dsk",
    ".fd1",
    ".fd2",
    ".hdd",
    ".oms",
    ".rom",
    ".sav",
    ".savestate",
    ".state",
}
_OPENMSX_WHEEL_RESOURCES = {
    "share/extensions/README",
    "share/extensions/rs232_proto.xml",
    "share/machines/Gradiente_Expert20.xml",
    "share/machines/Gradiente_Expert20_64K.xml",
    "share/machines/Sony_HB-F1XDJ_128K_Lite.xml",
    "share/machines/Sony_HB-F1XDJ_64K_Lite.xml",
    "share/settings.xml",
    "share/software/README",
}
_DOC_WHEEL_RESOURCES = {
    "backends.md",
    "development.md",
    "getting-started.md",
    "manifest.json",
    "overview.md",
    "safety.md",
    "transfers.md",
}
_RUNTIME_MODULES = {
    "__init__.py",
    "__main__.py",
    "_version.py",
    "execution.py",
    "mcp_metadata.py",
    "mcp_runtime.py",
    "msx_application.py",
    "msx_client.py",
    "msx_cpu.py",
    "msx_docs.py",
    "msx_mcp_server.py",
    "msx_protocol.py",
    "msx_real.py",
    "msx_screenshot.py",
    "msx_transfer.py",
    "msx_v3.py",
    "paths.py",
    "windows_sspi.py",
}
_WHEEL_LICENSE_FILES = {
    "LICENSE",
    "third_party/memman/NOTICE",
    "third_party/openmsx/GPL-2.0.txt",
    "third_party/openmsx/NOTICE",
}
_SDIST_REQUIRED_FILES = {
    "AUTHORS.md",
    "CHANGELOG.md",
    "LICENSE",
    "MANIFEST.in",
    "Makefile",
    "README.md",
    "TECHNICAL.md",
    "pyproject.toml",
    "agent/README.md",
    "agent/README.TXT",
    "agent/msx_agent.asm",
    "agent/msx_agent_core.asm",
    "agent/msx_agent_trace.asm",
    "agent/msx_agent_tsr.asm",
    "agent/msx_memman_loader.asm",
    "agent/msx_port_helper.asm",
    "agent/msx_tu_helper.asm",
    "agent/msx_unapi_probe.asm",
    "agent/msx_xfer.asm",
    "agent/msx_xfer_engine.inc",
    "agent/msx_xfer_protocol.inc",
    "agent/msx_version.inc",
    "agent/transports/msx_transport_16c550.inc",
    "agent/transports/msx_transport_8251.inc",
    "agent/transports/msx_transport_unapi.inc",
    "assets/NOTICE.md",
    "assets/msx-ai-robot.png",
    "docs/openmsx-unapi-validation.md",
    "server/resources/docs/manifest.json",
    "tests/test_port_helper.py",
    "tests/test_build_version_include.py",
    "tests/test_release_check.py",
    "tests/test_tu_helper.py",
    "tests/test_openmsx_unapi_validation.py",
    "tests/test_unapi_probe.py",
    "third_party/memman/NOTICE",
    "third_party/memman/SHA256SUMS",
    "third_party/memman/memman.com.b64",
    "third_party/memman/tk.com.b64",
    "third_party/memman/tl.com.b64",
    "third_party/openmsx/GPL-2.0.txt",
    "third_party/openmsx/NOTICE",
    "tools/build_agent_tsr.py",
    "tools/build_memman_tsr.py",
    "tools/build_port_helper.py",
    "tools/build_tu_helper.py",
    "tools/build_unapi_probe.py",
    "tools/build_version_include.py",
    "tools/check_msx_com_size.py",
    "tools/materialize_memman.py",
    "tools/openmsx_mcp_test.tcl",
    "tools/openmsx_unapi_validation.py",
    "tools/release_check.py",
} | {f"server/{name}" for name in _RUNTIME_MODULES}
_AGENT_SUITE_FILES = {
    "MCP16550.TSR",
    "MCP8251.TSR",
    "MCPUNAPI.TSR",
    "MP.COM",
    "MEMMAN.COM",
    "MSXAI.COM",
    "MSXAIXF.COM",
    "TK.COM",
    "TL.COM",
    "TU.COM",
}
_AGENT_COM_SIZE_CEILINGS = {
    "MSXAI.COM": 36_760,
    "MSXAIXF.COM": 16_128,
    "MP.COM": 16_128,
    "TU.COM": 16_128,
}
_WIRE_VERSION = "v3"
_TRANSFER_VERSION = "fast-v1"
_Z80ASM_TOOLCHAIN_ID = "bas-wijnen-z80asm"
_Z80ASM_VERSION = "1.8"
_Z80ASM_VERSION_LINE = "Z80 assembler version 1.8"
_AGENT_ARCHIVE_LICENSES = {"LICENSE", "MEMMAN-NOTICE.txt"}
_AGENT_ARCHIVE_DOCUMENTS = {"README.TXT"}
_AGENT_ARCHIVE_METADATA = {
    "COMPATIBILITY.json", "SHA256SUMS", *_AGENT_ARCHIVE_LICENSES,
    *_AGENT_ARCHIVE_DOCUMENTS}


class ReleaseCheckError(RuntimeError):
    """A release invariant failed."""


def _say(message: str) -> None:
    print(f"[release-check] {message}", flush=True)


def _run(command: list[str], *, cwd: Path, env: dict[str, str],
        timeout: int = COMMAND_TIMEOUT, capture: bool = False) \
        -> subprocess.CompletedProcess[str]:
    display = shlex.join(command)
    _say("running: " + display)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        if capture:
            if completed.stdout:
                print(completed.stdout, file=sys.stderr)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr)
        raise ReleaseCheckError(
            f"command exited with status {completed.returncode}: {display}")
    return completed


def _numeric_version(distribution: str) -> tuple[int, ...]:
    value = metadata.version(distribution)
    release = value.split("+", 1)[0].split("-", 1)[0]
    numbers: list[int] = []
    for component in release.split("."):
        digits = "".join(character for character in component
                         if character.isdigit())
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


def _require_dependencies(
        environment: Mapping[str, str] = os.environ) -> None:
    missing: list[str] = []
    for module_name in _CORE_MODULES + _BUILD_MODULES:
        try:
            importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            missing.append(module_name)
    if missing:
        raise ReleaseCheckError(
            "missing release dependencies: " + ", ".join(missing) +
            ". Install the project and the build frontend in this Python "
            "environment before running release validation.")
    constraints = {
        "anyio": ((4, 5), (5,)),
        "jsonschema": ((4, 20), (5,)),
        "mcp": ((2,), (3,)),
        "uvicorn": ((0, 30), (1,)),
    }
    for distribution, (minimum, maximum) in constraints.items():
        actual = _numeric_version(distribution)
        if actual < minimum or actual >= maximum:
            raise ReleaseCheckError(
                f"release dependency {distribution} has incompatible "
                f"version {metadata.version(distribution)}")
    if _numeric_version("build") < (1,):
        raise ReleaseCheckError("release validation requires build>=1")
    if _numeric_version("setuptools") < (77,):
        raise ReleaseCheckError("release validation requires setuptools>=77")

    _make, assembler = _resolve_build_tools(environment)
    _z80asm_version_line(assembler, environment)


def _resolve_build_tools(
        environment: Mapping[str, str], *, platform: str | None = None
) -> tuple[str | None, str]:
    """Resolve optional Windows make and the required Z80ASM executable."""
    platform = os.name if platform is None else platform
    path = environment.get("PATH")
    make_override = environment.get("MAKE")
    make_candidate = "make" if make_override is None else make_override.strip()
    make_executable = None
    if platform != "nt" or make_override is not None:
        make_executable = (
            shutil.which(make_candidate, path=path) if make_candidate else None)
        if make_executable is None:
            raise ReleaseCheckError(
                f"release build tool MAKE={make_candidate!r} is not an "
                "executable available through the configured PATH")
    make = (str(Path(make_executable).resolve())
            if make_executable is not None else None)

    assembler_candidate = environment.get("Z80ASM", "z80asm").strip()
    assembler_executable = (
        shutil.which(assembler_candidate, path=path)
        if assembler_candidate else None)
    if assembler_executable is None:
        raise ReleaseCheckError(
            f"release build tool Z80ASM={assembler_candidate!r} is not an "
            "executable available through the configured PATH")
    return make, str(Path(assembler_executable).resolve())


def _assert_z80asm_version(output: str) -> str:
    lines = output.splitlines()
    first_line = lines[0].strip() if lines else ""
    if (first_line != _Z80ASM_VERSION_LINE or
            not any("Bas Wijnen" in line for line in lines)):
        raise ReleaseCheckError(
            "release builds require Bas Wijnen z80asm 1.8; got "
            f"{first_line or '<no version output>'!r}")
    return first_line


def _z80asm_version_line(assembler: str,
                         environment: Mapping[str, str]) -> str:
    completed = subprocess.run(
        [assembler, "--version"], cwd=ROOT, env=dict(environment),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise ReleaseCheckError(
            f"{assembler} --version exited with status "
            f"{completed.returncode}")
    return _assert_z80asm_version(completed.stdout)


def _release_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = DEFAULT_SOURCE_DATE_EPOCH
    environment["PYTHONHASHSEED"] = "0"
    # The release suite is intentionally hardware-free even if a developer's
    # interactive shell has integration testing enabled.
    environment["MSX_RUN_INTEGRATION"] = "0"
    environment["MSX_RUN_UNAPI_INTEGRATION"] = "0"
    return environment


def _resolve_git(environment: Mapping[str, str]) -> str:
    candidate = environment.get("GIT", "git").strip()
    executable = shutil.which(candidate, path=environment.get("PATH")) \
        if candidate else None
    if executable is None:
        raise ReleaseCheckError(
            f"publish mode requires GIT={candidate!r} to resolve to an "
            "executable")
    return str(Path(executable).resolve())


def _git_capture(arguments: list[str], environment: Mapping[str, str]) -> bytes:
    git = _resolve_git(environment)
    completed = subprocess.run(
        [git, *arguments], cwd=ROOT, env=dict(environment),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=COMMAND_TIMEOUT, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseCheckError(
            f"git {' '.join(arguments)} failed with status "
            f"{completed.returncode}: {detail}")
    return completed.stdout


def _assert_publish_status(status: bytes) -> None:
    if status:
        records = [record for record in status.split(b"\x00") if record]
        raise ReleaseCheckError(
            "publish mode requires a clean Git checkout; found "
            f"{len(records)} tracked modification(s) or untracked "
            "non-ignored path record(s)")


def _require_clean_git_checkout(environment: Mapping[str, str]) -> None:
    inside = _git_capture(
        ["rev-parse", "--is-inside-work-tree"], environment).strip()
    if inside != b"true":
        raise ReleaseCheckError("publish mode requires a Git worktree")
    status = _git_capture(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        environment)
    _assert_publish_status(status)


def _assert_release_tag(version: str, tags: bytes) -> None:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None:
        raise ReleaseCheckError(
            f"publish version must be stable SemVer MAJOR.MINOR.PATCH: {version!r}")
    expected = f"v{version}".encode("ascii")
    actual = {tag for tag in tags.splitlines() if tag}
    if expected not in actual:
        raise ReleaseCheckError(
            f"publish HEAD must have the annotated release tag v{version}")


def _require_release_tag(environment: Mapping[str, str]) -> None:
    version = _project_version(ROOT)
    _assert_release_tag(
        version, _git_capture(["tag", "--points-at", "HEAD"], environment))
    tag_type = _git_capture(
        ["cat-file", "-t", f"refs/tags/v{version}"], environment).strip()
    if tag_type != b"tag":
        raise ReleaseCheckError(
            f"publish tag v{version} must be an annotated Git tag")


def _ignore_source(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in _IGNORED_DIRECTORY_NAMES or name.endswith(".egg-info"):
            ignored.add(name)
        elif name == ".DS_Store" or name.endswith((".pyc", ".pyo")):
            ignored.add(name)
    return ignored


def _stage_source(destination: Path) -> Path:
    """Create a clean snapshot of the checkout's current on-disk content.

    This deliberately includes current uncommitted source files so the gate is
    useful before a commit. Generated/local-state directories are excluded.
    It is not a claim that every included input is already Git-tracked.
    """
    source = destination / "source"
    shutil.copytree(
        ROOT,
        source,
        symlinks=True,
        ignore=_ignore_source,
    )
    links = [path for path in source.rglob("*") if path.is_symlink()]
    if links:
        relative = ", ".join(str(path.relative_to(source)) for path in links)
        raise ReleaseCheckError(
            "release source snapshot contains symbolic links: " + relative)

    _add_release_canaries(source)
    return source


def _add_release_canaries(source: Path) -> None:
    """Add forbidden files that packaging rules must prove they exclude."""
    canaries = {
        Path(".mcp.json"): b'{"local": "release canary"}\n',
        Path("work/release-secret.dsk"): b"release canary\n",
        Path(".openmsx-home/persistent/release-secret.rom"):
            b"release canary\n",
        Path("local-state.oms"): b"release canary\n",
    }
    for relative, content in canaries.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _extract_git_archive(archive_bytes: bytes, destination: Path) -> None:
    """Safely extract regular tracked files from `git archive` output."""
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        members = archive.getmembers()
        names = [_safe_archive_path(member.name) for member in members]
        if len(names) != len(set(names)):
            raise ReleaseCheckError("Git archive contains duplicate paths")
        for member, safe_path in zip(members, names):
            if not (member.isdir() or member.isfile()):
                raise ReleaseCheckError(
                    f"Git archive contains unsupported entry: {member.name}")
            target = destination.joinpath(*safe_path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseCheckError(
                    f"could not read Git archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def _stage_publish_source(destination: Path,
                          environment: Mapping[str, str]) -> Path:
    """Stage exactly HEAD after clean checks before and after archiving."""
    _require_clean_git_checkout(environment)
    archive_bytes = _git_capture(["archive", "--format=tar", "HEAD"],
                                 environment)
    _require_clean_git_checkout(environment)
    source = destination / "source"
    source.mkdir()
    _extract_git_archive(archive_bytes, source)
    _add_release_canaries(source)
    return source


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    raw_parts = name.split("/")
    if (not name or "\x00" in name or "\\" in name or path.is_absolute() or
            not path.parts or any(part in {"", ".", ".."}
                                  for part in raw_parts) or
            any(":" in part for part in raw_parts)):
        raise ReleaseCheckError(f"unsafe archive member path: {name!r}")
    return path


def _forbidden_reasons(name: str) -> list[str]:
    path = _safe_archive_path(name)
    lowered_parts = tuple(part.lower() for part in path.parts)
    reasons: list[str] = []
    if lowered_parts[-1] in _FORBIDDEN_BASENAMES:
        reasons.append("local client configuration")
    forbidden_segments = sorted(
        set(lowered_parts).intersection(_FORBIDDEN_SEGMENTS))
    if forbidden_segments:
        reasons.append("state directory: " + ", ".join(forbidden_segments))
    suffix = PurePosixPath(lowered_parts[-1]).suffix
    if suffix in _FORBIDDEN_SUFFIXES:
        reasons.append("forbidden media/state suffix: " + suffix)
    return reasons


def _sdist_relative_names(names: list[str]) -> set[str]:
    paths = [_safe_archive_path(name) for name in names]
    top_levels = {path.parts[0] for path in paths}
    if len(top_levels) != 1:
        raise ReleaseCheckError("sdist must have exactly one top directory")
    return {
        PurePosixPath(*path.parts[1:]).as_posix()
        for path in paths if len(path.parts) > 1
    }


def _assert_sdist_contents(names: list[str]) -> None:
    relative_names = _sdist_relative_names(names)
    missing = sorted(_SDIST_REQUIRED_FILES - relative_names)
    if missing:
        raise ReleaseCheckError(
            "sdist is missing required source files:\n  " +
            "\n  ".join(missing))


def _assert_wheel_contents(names: list[str]) -> None:
    members = set(names)
    required_runtime = {f"msx_ai/{name}" for name in _RUNTIME_MODULES}
    missing_runtime = sorted(required_runtime - members)
    if missing_runtime:
        raise ReleaseCheckError(
            "wheel is missing runtime modules:\n  " +
            "\n  ".join(missing_runtime))

    openmsx_prefix = "msx_ai/resources/openmsx/"
    actual_openmsx = {
        name.removeprefix(openmsx_prefix)
        for name in members
        if name.startswith(openmsx_prefix) and
        name != openmsx_prefix + "__init__.py"
    }
    if actual_openmsx != _OPENMSX_WHEEL_RESOURCES:
        raise ReleaseCheckError(
            "wheel openMSX resources differ from the exact public set: "
            f"missing={sorted(_OPENMSX_WHEEL_RESOURCES - actual_openmsx)}, "
            f"unexpected={sorted(actual_openmsx - _OPENMSX_WHEEL_RESOURCES)}")

    docs_prefix = "msx_ai/resources/docs/"
    actual_docs = {
        name.removeprefix(docs_prefix)
        for name in members
        if name.startswith(docs_prefix) and
        name != docs_prefix + "__init__.py"
    }
    if actual_docs != _DOC_WHEEL_RESOURCES:
        raise ReleaseCheckError(
            "wheel documentation resources differ from the expected set: "
            f"missing={sorted(_DOC_WHEEL_RESOURCES - actual_docs)}, "
            f"unexpected={sorted(actual_docs - _DOC_WHEEL_RESOURCES)}")

    dist_info = {
        PurePosixPath(name).parts[0]
        for name in members
        if PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if len(dist_info) != 1:
        raise ReleaseCheckError(
            "wheel must contain exactly one .dist-info directory")
    license_prefix = next(iter(dist_info)) + "/licenses/"
    actual_licenses = {
        name.removeprefix(license_prefix)
        for name in members if name.startswith(license_prefix)
    }
    if actual_licenses != _WHEEL_LICENSE_FILES:
        raise ReleaseCheckError(
            "wheel license/notice files differ from the expected set: "
            f"missing={sorted(_WHEEL_LICENSE_FILES - actual_licenses)}, "
            f"unexpected={sorted(actual_licenses - _WHEEL_LICENSE_FILES)}")


def _inspect_sdist(path: Path) -> list[str]:
    names: list[str] = []
    violations: list[str] = []
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            _safe_archive_path(member.name)
            if not (member.isdir() or member.isfile()):
                violations.append(
                    f"{member.name}: links and special files are forbidden")
            names.append(member.name)
            for reason in _forbidden_reasons(member.name):
                violations.append(f"{member.name}: {reason}")
    if len(names) != len(set(names)):
        violations.append("archive contains duplicate member names")
    if violations:
        raise ReleaseCheckError(
            "forbidden sdist members:\n  " + "\n  ".join(violations))
    _assert_sdist_contents(names)
    return names


def _inspect_wheel(path: Path) -> list[str]:
    names: list[str] = []
    violations: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            _safe_archive_path(member.filename)
            # Unix file type bits are present when the wheel was built on a
            # POSIX host. Reject symlinks instead of dereferencing them later.
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                violations.append(f"{member.filename}: symlink is forbidden")
            names.append(member.filename)
            for reason in _forbidden_reasons(member.filename):
                violations.append(f"{member.filename}: {reason}")
    if len(names) != len(set(names)):
        violations.append("archive contains duplicate member names")
    if violations:
        raise ReleaseCheckError(
            "forbidden wheel members:\n  " + "\n  ".join(violations))
    _assert_wheel_contents(names)
    return names


def _extract_sdist(path: Path, destination: Path) -> Path:
    """Extract a checked sdist without trusting tarfile.extract()."""
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        paths = [_safe_archive_path(member.name) for member in members]
        if len(paths) != len(set(paths)):
            raise ReleaseCheckError("sdist contains duplicate member paths")
        top_levels = {item.parts[0] for item in paths}
        if len(top_levels) != 1:
            raise ReleaseCheckError("sdist must have exactly one top directory")
        top_level = next(iter(top_levels))
        root = destination / top_level
        for member, safe_path in zip(members, paths):
            if not (member.isdir() or member.isfile()):
                raise ReleaseCheckError(
                    f"unsupported sdist member type: {member.name}")
            target = destination.joinpath(*safe_path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseCheckError(
                    f"could not read sdist member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
    return root


def _wheel_payload(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            member.filename: hashlib.sha256(archive.read(member)).hexdigest()
            for member in archive.infolist()
            if not member.is_dir()
        }


def _single_artifact(directory: Path, suffix: str) -> Path:
    matches = sorted(directory.glob(f"*{suffix}"))
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"expected one {suffix} artifact in {directory}, found "
            f"{len(matches)}")
    return matches[0]


def _assert_agent_suite(agent_directory: Path) -> None:
    if not agent_directory.is_dir():
        raise ReleaseCheckError(
            f"agent build did not create {agent_directory}")
    actual = {path.name for path in agent_directory.iterdir()
              if path.is_file()}
    if actual != _AGENT_SUITE_FILES:
        raise ReleaseCheckError(
            "agent suite differs from the exact deployable set: "
            f"missing={sorted(_AGENT_SUITE_FILES - actual)}, "
            f"unexpected={sorted(actual - _AGENT_SUITE_FILES)}")
    empty = sorted(name for name in actual
                   if (agent_directory / name).stat().st_size == 0)
    if empty:
        raise ReleaseCheckError(
            "agent suite contains empty artifacts: " + ", ".join(empty))

    oversized = {
        name: (agent_directory / name).stat().st_size
        for name, ceiling in _AGENT_COM_SIZE_CEILINGS.items()
        if (agent_directory / name).stat().st_size > ceiling
    }
    if oversized:
        details = ", ".join(
            f"{name}={size} (max {_AGENT_COM_SIZE_CEILINGS[name]})"
            for name, size in sorted(oversized.items()))
        raise ReleaseCheckError("agent COM size ceiling exceeded: " + details)


def _agent_suite_payload(agent_directory: Path) -> dict[str, str]:
    _assert_agent_suite(agent_directory)
    return {
        name: hashlib.sha256((agent_directory / name).read_bytes()).hexdigest()
        for name in sorted(_AGENT_SUITE_FILES)
    }


def _assert_matching_agent_suites(staged: Path, rebuilt: Path) -> None:
    staged_payload = _agent_suite_payload(staged)
    rebuilt_payload = _agent_suite_payload(rebuilt)
    if staged_payload != rebuilt_payload:
        changed = sorted(
            name for name in _AGENT_SUITE_FILES
            if staged_payload.get(name) != rebuilt_payload.get(name))
        raise ReleaseCheckError(
            "sdist-rebuilt agent suite differs from the staged build: " +
            ", ".join(changed))


def _project_version(source: Path) -> str:
    version_source = source / "server" / "_version.py"
    tree = ast.parse(version_source.read_text(encoding="utf-8"),
                     filename=str(version_source))
    values = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if (isinstance(target, ast.Name) and target.id == "__version__" and
                isinstance(statement.value, ast.Constant) and
                isinstance(statement.value.value, str)):
            values.append(statement.value.value)
    if len(values) != 1 or not values[0]:
        raise ReleaseCheckError("could not read one project __version__ value")
    return values[0]


def _compatibility_manifest(
        source: Path,
        toolchain_version_line: str = _Z80ASM_VERSION_LINE) \
        -> dict[str, object]:
    core = (source / "agent" / "msx_agent_core.asm").read_text(
        encoding="utf-8")
    transfer = (source / "agent" / "msx_xfer_protocol.inc").read_text(
        encoding="utf-8")
    expected_markers = (
        ("wire v3", "FRAMED_VERSION: equ 3" in core),
        ("transfer fast-v1", "XFER_FAST_VERSION: equ 1" in transfer and
         "fast-v1" in transfer),
    )
    missing = [name for name, present in expected_markers if not present]
    if missing:
        raise ReleaseCheckError(
            "agent source does not match release compatibility metadata: " +
            ", ".join(missing))
    return {
        "schema_version": 1,
        "creator": "Rodrigo Galhardi M. Garcia",
        "release": _project_version(source),
        "wire": _WIRE_VERSION,
        "transfer": _TRANSFER_VERSION,
        "toolchain": {
            "id": _Z80ASM_TOOLCHAIN_ID,
            "version": _Z80ASM_VERSION,
            "version_line": toolchain_version_line,
        },
        "files": sorted(_AGENT_SUITE_FILES),
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    timestamp = time.gmtime(int(DEFAULT_SOURCE_DATE_EPOCH))[:6]
    information = zipfile.ZipInfo(name, date_time=timestamp)
    information.compress_type = zipfile.ZIP_DEFLATED
    information.create_system = 3
    information.external_attr = 0o100644 << 16
    information.flag_bits |= 0x800
    return information


def _msx_readme_payload(source: Path) -> bytes:
    payload = (source / "agent" / "README.TXT").read_bytes()
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseCheckError("agent README.TXT must contain ASCII only") from exc
    if b"\n" in payload.replace(b"\r\n", b""):
        raise ReleaseCheckError("agent README.TXT must use CRLF line endings")
    marker = b"@VERSION@"
    if payload.count(marker) != 1:
        raise ReleaseCheckError(
            "agent README.TXT must contain one @VERSION@ marker")
    payload = payload.replace(
        marker, _project_version(source).encode("ascii"))
    text = payload.decode("ascii")
    if any(len(line) > 78 for line in text.splitlines()):
        raise ReleaseCheckError(
            "agent README.TXT lines must be at most 78 characters")
    if not text.startswith(f"MSX-AI AGENT {_project_version(source)}\r\n"):
        raise ReleaseCheckError(
            "agent README.TXT heading must match the project release")
    return payload


def _build_agent_archive(source: Path, agent_directory: Path,
                         destination: Path,
                         toolchain_version_line: str) -> Path:
    payload = _agent_suite_payload(agent_directory)
    expected_name = "MSXAI.ZIP"
    if destination.name != expected_name:
        raise ReleaseCheckError(
            f"agent archive must be named {expected_name}")
    compatibility = json.dumps(
        _compatibility_manifest(source, toolchain_version_line),
        indent=2, sort_keys=True,
        ensure_ascii=True).encode("utf-8") + b"\n"
    licensed_material = {
        "LICENSE": (source / "LICENSE").read_bytes(),
        "MEMMAN-NOTICE.txt": (
            source / "third_party" / "memman" / "NOTICE").read_bytes(),
    }
    documents = {"README.TXT": _msx_readme_payload(source)}
    checksum_sources = {
        **{name: (agent_directory / name).read_bytes() for name in payload},
        **licensed_material,
        **documents,
        "COMPATIBILITY.json": compatibility,
    }
    checksum_payload = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in checksum_sources.items()
    }
    checksums = "".join(
        f"{checksum_payload[name]}  {name}\n"
        for name in sorted(checksum_payload))
    entries = {
        **{name: (agent_directory / name).read_bytes()
           for name in sorted(_AGENT_SUITE_FILES)},
        **licensed_material,
        **documents,
        "SHA256SUMS": checksums.encode("ascii"),
        "COMPATIBILITY.json": compatibility,
    }
    with zipfile.ZipFile(
            destination, mode="x", compression=zipfile.ZIP_DEFLATED,
            compresslevel=9) as archive:
        for name in sorted(entries):
            archive.writestr(
                _zip_info(name), entries[name],
                compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    _assert_agent_archive(
        destination, source, agent_directory, toolchain_version_line)
    return destination


def _assert_agent_archive(path: Path, source: Path,
                          agent_directory: Path,
                          toolchain_version_line: str) -> None:
    expected_names = _AGENT_SUITE_FILES | _AGENT_ARCHIVE_METADATA
    expected_payload = _agent_suite_payload(agent_directory)
    expected_manifest = _compatibility_manifest(
        source, toolchain_version_line)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise ReleaseCheckError(
                "agent archive must contain exactly ten binaries, LICENSE, "
                "MEMMAN-NOTICE.txt, README.TXT, SHA256SUMS, and "
                "COMPATIBILITY.json")
        for name in _AGENT_SUITE_FILES:
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if digest != expected_payload[name]:
                raise ReleaseCheckError(
                    f"agent archive payload mismatch: {name}")
        compatibility = archive.read("COMPATIBILITY.json")
        licensed_material = {
            "LICENSE": (source / "LICENSE").read_bytes(),
            "MEMMAN-NOTICE.txt": (
                source / "third_party" / "memman" / "NOTICE").read_bytes(),
        }
        documents = {"README.TXT": _msx_readme_payload(source)}
        for name, content in licensed_material.items():
            if archive.read(name) != content:
                raise ReleaseCheckError(
                    f"agent archive license/notice mismatch: {name}")
        if archive.read("README.TXT") != documents["README.TXT"]:
            raise ReleaseCheckError("agent archive README.TXT mismatch")
        checksum_sources = {
            **{name: archive.read(name) for name in _AGENT_SUITE_FILES},
            **licensed_material,
            **documents,
            "COMPATIBILITY.json": compatibility,
        }
        checksum_payload = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in checksum_sources.items()
        }
        checksums = "".join(
            f"{checksum_payload[name]}  {name}\n"
            for name in sorted(checksum_payload)).encode("ascii")
        if archive.read("SHA256SUMS") != checksums:
            raise ReleaseCheckError("agent archive SHA256SUMS mismatch")
        decoded = json.loads(compatibility)
        if decoded != expected_manifest:
            raise ReleaseCheckError("agent compatibility manifest mismatch")


def _publish_new_file(source: Path, target: Path) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp",
                delete=False) as output, source.open("rb") as input_file:
            temporary = Path(output.name)
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ReleaseCheckError(
                f"refusing to overwrite release artifact: {target}") from exc
        except OSError as exc:
            raise ReleaseCheckError(
                "the output filesystem cannot atomically publish release "
                f"artifacts with hard links ({target}: {exc}); choose a local "
                "filesystem that supports hard links") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _persist_release_assets(output_directory: Path, artifacts: list[Path]) \
        -> list[Path]:
    output_directory = output_directory.expanduser().resolve(strict=False)
    if output_directory == ROOT.resolve():
        raise ReleaseCheckError(
            "release output must be a dedicated directory, not the "
            "repository root")
    if output_directory.exists() and not output_directory.is_dir():
        raise ReleaseCheckError(
            f"release output is not a directory: {output_directory}")
    targets = [output_directory / artifact.name for artifact in artifacts]
    existing = [target for target in targets if target.exists()]
    if existing:
        raise ReleaseCheckError(
            "refusing to overwrite existing release artifact(s): " +
            ", ".join(str(path) for path in existing))
    output_directory.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for artifact, target in zip(artifacts, targets):
            _publish_new_file(artifact, target)
            created.append(target)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(created):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        if rollback_errors:
            raise ReleaseCheckError(
                "release publication failed and rollback was incomplete: " +
                "; ".join(rollback_errors)) from exc
        raise
    return targets


def _build_agent_suite_portable(
        source: Path, env: dict[str, str], assembler: str,
        agent_directory: Path) -> None:
    """Run the Makefile agent recipe without requiring a POSIX make tool."""
    build_directory = agent_directory / "build"
    build_directory.mkdir(parents=True, exist_ok=True)
    tools = source / "tools"
    commands = (
        [
            sys.executable, str(tools / "materialize_memman.py"),
            "--source-dir", str(source / "third_party" / "memman"),
            "--output-dir", str(agent_directory),
        ],
        [
            sys.executable, str(tools / "build_agent_tsr.py"),
            "--repository", str(source), "--assembler", assembler,
            "--output", str(build_directory / "MSXAI.TSR"),
            "--metadata-output", str(build_directory / "MSXAI_TSR.INC"),
            "--8251-output", str(agent_directory / "MCP8251.TSR"),
            "--16c550-output", str(agent_directory / "MCP16550.TSR"),
            "--unapi-output", str(agent_directory / "MCPUNAPI.TSR"),
        ],
        [
            sys.executable, str(tools / "build_port_helper.py"),
            "--repository", str(source), "--assembler", assembler,
            "--source", str(source / "agent" / "msx_port_helper.asm"),
            "--output", str(agent_directory / "MP.COM"),
        ],
        [
            sys.executable, str(tools / "build_tu_helper.py"),
            "--repository", str(source), "--assembler", assembler,
            "--source", str(source / "agent" / "msx_tu_helper.asm"),
            "--output", str(agent_directory / "TU.COM"),
        ],
        [
            assembler, str(source / "agent" / "msx_agent.asm"),
            "-o", str(agent_directory / "MSXAI.COM"),
        ],
        [
            sys.executable, str(tools / "check_msx_com_size.py"),
            str(agent_directory / "MSXAI.COM"),
            str(_AGENT_COM_SIZE_CEILINGS["MSXAI.COM"]),
        ],
        [
            assembler, str(source / "agent" / "msx_xfer.asm"),
            "-o", str(agent_directory / "MSXAIXF.COM"),
        ],
        [
            sys.executable, str(tools / "check_msx_com_size.py"),
            str(agent_directory / "MSXAIXF.COM"),
            str(_AGENT_COM_SIZE_CEILINGS["MSXAIXF.COM"]),
        ],
    )
    for command in commands:
        _run(command, cwd=source, env=env)


def _build_agent_suite(source: Path, env: dict[str, str]) -> Path:
    make, assembler = _resolve_build_tools(env)
    _z80asm_version_line(assembler, env)
    _say("building the ten-file Z80 agent suite in the staged snapshot")
    agent_directory = source / "work" / "agent"
    if make is None:
        _say("using the portable Python agent builder on Windows")
        _build_agent_suite_portable(
            source, env, assembler, agent_directory)
    else:
        _run(
            [make, "agent", f"PYTHON={sys.executable}",
             f"Z80ASM={assembler}"],
            cwd=source, env=env)
    _assert_agent_suite(agent_directory)
    return agent_directory


def _venv_paths(root: Path) -> tuple[Path, Path]:
    scripts = root / ("Scripts" if os.name == "nt" else "bin")
    python_name = "python.exe" if os.name == "nt" else "python"
    entry_name = "msx-ai-mcp.exe" if os.name == "nt" else "msx-ai-mcp"
    return scripts / python_name, scripts / entry_name


def _isolated_environment(base: dict[str, str], probe: Path) -> dict[str, str]:
    environment = base.copy()
    home = probe / "home"
    user = probe / "user"
    home.mkdir(parents=True, exist_ok=True)
    user.mkdir(parents=True, exist_ok=True)
    environment["HOME"] = str(home)
    # pathlib uses USERPROFILE instead of HOME on Windows.  Set both so the
    # child MCP processes have the same isolated state root on every host.
    environment["USERPROFILE"] = str(home)
    environment["MSX_AI_USER_ROOT"] = str(user)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    for name in (
            "PYTHONPATH", "MSX_AI_DATA_DIR", "MSX_AI_STATE_DIR",
            "MSX_AI_OPENMSX_HOME", "MSX_AI_SOURCE_ROOT"):
        environment.pop(name, None)
    return environment


def _assert_no_state(probe: Path, operation: str) -> None:
    state = probe / "home" / ".msx-ai"
    if state.exists():
        entries = sorted(str(path.relative_to(state))
                         for path in state.rglob("*"))
        raise ReleaseCheckError(
            f"{operation} created runtime state: {entries or ['state/']}")


_RUNTIME_REQUIRED_TOOLS = frozenset({
    "msx_targets_status",
    "msx_local_boot", "msx_local_status", "msx_local_screen",
    "msx_local_screenshot", "msx_local_memory_read", "msx_local_type_line",
    "msx_agent_listen", "msx_agent_connect", "msx_agent_status",
    "msx_agent_screen", "msx_agent_screenshot", "msx_agent_memory_read",
    "msx_agent_type_line", "msx_agent_disconnect",
    "msx_tcp_bench_start", "msx_tcp_bench_status",
    "msx_tcp_bench_shutdown",
})
_RUNTIME_FORBIDDEN_TOOLS = frozenset({
    "msx_status", "msx_screen", "msx_screenshot", "msx_shutdown",
    "msx_real_listen",
})


def _mcp_assertions(client, transport: str) -> Callable[[], Awaitable[str]]:
    """Return an async MCP smoke coroutine without importing the SDK globally."""
    async def run() -> str:
        async with client:
            protocol = str(client.protocol_version)
            tools = await client.list_tools()
            if len(tools.tools) < 35:
                raise ReleaseCheckError(
                    f"{transport} exposed only {len(tools.tools)} tools")
            tool_names = {tool.name for tool in tools.tools}
            missing = sorted(_RUNTIME_REQUIRED_TOOLS - tool_names)
            forbidden = sorted(_RUNTIME_FORBIDDEN_TOOLS & tool_names)
            if missing:
                raise ReleaseCheckError(
                    f"{transport} omitted explicit tools: {missing}")
            if forbidden:
                raise ReleaseCheckError(
                    f"{transport} exposed ambiguous tools: {forbidden}")
            status = await client.call_tool("msx_targets_status", {})
            if status.is_error or not status.structured_content:
                raise ReleaseCheckError(
                    f"{transport} msx_targets_status failed")
            if status.structured_content.get("state") != "disconnected":
                raise ReleaseCheckError(
                    f"{transport} fresh server was not disconnected")
            resources = await client.list_resources()
            if len(resources.resources) < 8:
                raise ReleaseCheckError(
                    f"{transport} exposed only {len(resources.resources)} resources")
            index = next(
                (resource for resource in resources.resources
                 if str(resource.uri).endswith("/index")), None)
            if index is None:
                raise ReleaseCheckError(f"{transport} has no docs index")
            document = await client.read_resource(str(index.uri))
            if not document.contents or "MSX-AI" not in document.contents[0].text:
                raise ReleaseCheckError(f"{transport} docs index is unreadable")
            prompts = await client.list_prompts()
            if len(prompts.prompts) < 2:
                raise ReleaseCheckError(
                    f"{transport} exposed only {len(prompts.prompts)} prompts")
            return protocol
    return run


def _stdio_smoke(entrypoint: str, mode: str) -> int:
    import anyio
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    parameters = StdioServerParameters(command=entrypoint)
    transport = stdio_client(parameters)
    client = Client(
        transport, mode=mode, read_timeout_seconds=15,
        cache=None)
    protocol = anyio.run(_mcp_assertions(client, f"STDIO {mode}"))
    print(f"STDIO {mode}: protocol {protocol}")
    return 0


def _http_smoke(url: str) -> int:
    import anyio
    from mcp.client import Client

    client = Client(url, mode="auto", read_timeout_seconds=15, cache=None)
    protocol = anyio.run(_mcp_assertions(client, "HTTP"))
    print(f"HTTP: protocol {protocol}")
    return 0


def _installed_paths_smoke() -> int:
    """Validate installed public-template materialization without openMSX."""
    from importlib import resources
    from msx_ai import paths

    destination = paths.prepare_openmsx_home()
    expected = set(paths.OPENMSX_PUBLIC_FILES)
    actual = {
        path.relative_to(destination)
        for path in destination.rglob("*") if path.is_file()
    }
    if actual != expected:
        raise ReleaseCheckError(
            "installed openMSX materialization set mismatch")
    packaged = resources.files("msx_ai.resources.openmsx")
    expected_hashes = {
        relative: hashlib.sha256(
            packaged.joinpath(*relative.parts).read_bytes()).hexdigest()
        for relative in expected
    }
    actual_hashes = {
        relative: hashlib.sha256(
            (destination / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    if actual_hashes != expected_hashes:
        raise ReleaseCheckError(
            "installed openMSX materialization hash mismatch")

    # A second call is byte-idempotent, while a later user-owned change is
    # preserved rather than overwritten by package defaults.
    paths.prepare_openmsx_home()
    repeated_hashes = {
        relative: hashlib.sha256(
            (destination / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    if repeated_hashes != actual_hashes:
        raise ReleaseCheckError(
            "installed openMSX materialization is not idempotent")
    protected = destination / "share" / "settings.xml"
    marker = b"user-owned-settings\n"
    protected.write_bytes(marker)
    paths.prepare_openmsx_home()
    if protected.read_bytes() != marker:
        raise ReleaseCheckError(
            "installed openMSX materialization overwrote a user file")
    print(f"openMSX resources: {len(expected)} exact files")
    return 0


def _ephemeral_ipv4_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_http(process: subprocess.Popen[str], port: int,
                   timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise ReleaseCheckError(
                "HTTP server exited before accepting connections: " + stderr)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise ReleaseCheckError("HTTP server did not become ready in time")


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        # This is the exact child created by the checker, never a broad process
        # match. Killing it is safer than leaking a release-only HTTP server.
        process.kill()
        return process.communicate(timeout=5)


def _run_http_smoke(python: Path, entrypoint: Path, probe: Path,
                    env: dict[str, str]) -> None:
    port = _ephemeral_ipv4_port()
    command = [
        str(entrypoint), "--transport", "http",
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "error",
    ]
    _say("running HTTP server on an ephemeral IPv4 loopback port")
    process = subprocess.Popen(
        command,
        cwd=probe,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    failure: BaseException | None = None
    try:
        _wait_for_http(process, port)
        _run(
            [str(python), str(Path(__file__).resolve()), "_mcp-http",
             f"http://127.0.0.1:{port}/mcp"],
            cwd=probe, env=env, timeout=MCP_TIMEOUT, capture=True)
    except BaseException as exc:
        failure = exc
    finally:
        stdout, stderr = _stop_process(process)
    if failure is not None:
        if stdout:
            print(stdout, file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        raise failure


def _runtime_entrypoint(environment: Mapping[str, str]) -> Path:
    executable = shutil.which(
        "msx-ai-mcp", path=environment.get("PATH"))
    if executable is None:
        raise ReleaseCheckError(
            "runtime smoke requires the installed msx-ai-mcp entry point "
            "to be available on PATH")
    return Path(executable).resolve()


def run_runtime_smoke() -> int:
    """Exercise installed MCP transports without build tools or hardware."""
    base_environment = _release_environment()
    entrypoint = _runtime_entrypoint(base_environment)
    with tempfile.TemporaryDirectory(
            prefix="msx-ai-runtime-smoke-") as directory:
        probe = Path(directory) / "probe"
        probe.mkdir()
        environment = _isolated_environment(base_environment, probe)
        checker = str(Path(__file__).resolve())
        for mode in ("auto", "legacy"):
            _run(
                [sys.executable, checker, "_mcp-stdio",
                 str(entrypoint), mode],
                cwd=probe, env=environment, timeout=MCP_TIMEOUT,
                capture=True)
        _assert_no_state(probe, "STDIO MCP runtime smoke tests")
        _run_http_smoke(
            Path(sys.executable), entrypoint, probe, environment)
        _assert_no_state(probe, "HTTP MCP runtime smoke test")
    _say("PASS: installed MCP STDIO and HTTP transports validated")
    return 0


def _validate_installed_wheel(wheel: Path, temporary: Path,
                              base_env: dict[str, str]) -> None:
    environment_root = temporary / "clean-venv"
    _say("creating a clean virtual environment")
    venv.EnvBuilder(with_pip=True, clear=False).create(environment_root)
    python, entrypoint = _venv_paths(environment_root)
    probe = temporary / "installed-probe"
    probe.mkdir()
    environment = _isolated_environment(base_env, probe)

    _run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--only-binary=:all:", str(wheel)],
        cwd=probe, env=environment, capture=True)
    _run(
        [str(python), "-m", "pip", "check"],
        cwd=probe, env=environment, capture=True)

    imported = _run(
        [str(python), "-c",
         "import pathlib, sys; import msx_ai; "
         "from importlib import metadata; "
         "assert pathlib.Path(msx_ai.__file__).resolve().is_relative_to("
         "pathlib.Path(sys.prefix).resolve()); "
         "assert msx_ai.__version__ == metadata.version('msx-ai'); "
         "print(msx_ai.__version__)"],
        cwd=probe, env=environment, capture=True)
    _assert_no_state(probe, "package import")
    installed_version = imported.stdout.strip()
    version_result = _run(
        [str(entrypoint), "--version"],
        cwd=probe, env=environment, capture=True)
    if version_result.stdout.strip() != f"msx-ai-mcp {installed_version}":
        raise ReleaseCheckError("entry-point version does not match metadata")
    _assert_no_state(probe, "--version")

    _run(
        [str(python), str(Path(__file__).resolve()), "_paths-smoke"],
        cwd=probe, env=environment, capture=True)
    state = probe / "home" / ".msx-ai"
    if not state.is_dir():
        raise ReleaseCheckError(
            "installed path smoke did not materialize private state")
    state.rename(probe / "verified-materialized-state")
    _assert_no_state(probe, "installed path smoke cleanup")

    for mode in ("auto", "legacy"):
        _run(
            [str(python), str(Path(__file__).resolve()), "_mcp-stdio",
             str(entrypoint), mode],
            cwd=probe, env=environment, timeout=MCP_TIMEOUT, capture=True)
    _assert_no_state(probe, "STDIO MCP smoke tests")
    _run_http_smoke(python, entrypoint, probe, environment)
    _assert_no_state(probe, "HTTP MCP smoke test")


def run_release_check(*, publish: bool = False,
                      output_directory: Path | None = None) -> int:
    environment = _release_environment()
    if publish:
        # This is deliberately the first external validation in strict mode.
        # Do not run tests or builds against a checkout that cannot map exactly
        # to one committed HEAD tree.
        _require_clean_git_checkout(environment)
        _require_release_tag(environment)
    _require_dependencies(environment)

    _say("running the unit suite (openMSX integration forced off)")
    _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, env=environment)

    with tempfile.TemporaryDirectory(prefix="msx-ai-release-") as directory:
        temporary = Path(directory)
        source = (_stage_publish_source(temporary, environment)
                  if publish else _stage_source(temporary))
        _make, assembler = _resolve_build_tools(environment)
        toolchain_version_line = _z80asm_version_line(assembler, environment)
        staged_agent = _build_agent_suite(source, environment)
        artifacts = temporary / "artifacts"
        artifacts.mkdir()
        if publish:
            _say("building distributions from the clean committed HEAD tree")
        else:
            _say(
                "building distributions from a clean snapshot of the "
                "current checkout (including uncommitted source content)")
        _run(
            [sys.executable, "-m", "build", "--no-isolation", "--sdist",
             "--wheel", "--outdir", str(artifacts), str(source)],
            cwd=temporary, env=environment)

        sdist = _single_artifact(artifacts, ".tar.gz")
        wheel = _single_artifact(artifacts, ".whl")
        sdist_names = _inspect_sdist(sdist)
        wheel_names = _inspect_wheel(wheel)
        _say(
            f"archive policy passed ({len(sdist_names)} sdist members, "
            f"{len(wheel_names)} wheel members)")

        extracted = temporary / "sdist-source"
        extracted.mkdir()
        rebuilt_source = _extract_sdist(sdist, extracted)
        rebuilt_agent = _build_agent_suite(rebuilt_source, environment)
        _assert_matching_agent_suites(staged_agent, rebuilt_agent)
        _say("sdist-rebuilt agent suite matches all ten staged payloads")

        bundle_a = temporary / "agent-bundle-a"
        bundle_b = temporary / "agent-bundle-b"
        bundle_a.mkdir()
        bundle_b.mkdir()
        agent_archive_name = "MSXAI.ZIP"
        agent_archive = _build_agent_archive(
            rebuilt_source, rebuilt_agent, bundle_a / agent_archive_name,
            toolchain_version_line)
        repeated_archive = _build_agent_archive(
            rebuilt_source, rebuilt_agent, bundle_b / agent_archive_name,
            toolchain_version_line)
        if agent_archive.read_bytes() != repeated_archive.read_bytes():
            raise ReleaseCheckError("agent ZIP is not byte-reproducible")
        _say(
            "same-host agent ZIP rebuild is byte-identical with pinned "
            "Bas Wijnen z80asm 1.8")
        readme_artifact = bundle_a / "README.TXT"
        readme_artifact.write_bytes(_msx_readme_payload(rebuilt_source))

        rebuilt_artifacts = temporary / "rebuilt"
        rebuilt_artifacts.mkdir()
        _say("rebuilding the wheel from the sdist")
        _run(
            [sys.executable, "-m", "build", "--no-isolation", "--wheel",
             "--outdir", str(rebuilt_artifacts), str(rebuilt_source)],
            cwd=temporary, env=environment)
        rebuilt_wheel = _single_artifact(rebuilt_artifacts, ".whl")
        _inspect_wheel(rebuilt_wheel)
        if _wheel_payload(wheel) != _wheel_payload(rebuilt_wheel):
            raise ReleaseCheckError(
                "wheel payload differs when rebuilt from the sdist")
        _say("direct and sdist-rebuilt wheel payloads match")

        _validate_installed_wheel(rebuilt_wheel, temporary, environment)

        if output_directory is not None:
            published = _persist_release_assets(
                output_directory, [sdist, rebuilt_wheel, agent_archive,
                                   readme_artifact])
            _say("published release assets: " +
                 ", ".join(str(path) for path in published))

    _say("PASS: repeatable same-host release and MCP transports validated")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "_mcp-stdio":
            if len(arguments) != 3 or arguments[2] not in {"auto", "legacy"}:
                raise ReleaseCheckError("invalid private STDIO smoke arguments")
            return _stdio_smoke(arguments[1], arguments[2])
        if arguments and arguments[0] == "_mcp-http":
            if len(arguments) != 2:
                raise ReleaseCheckError("invalid private HTTP smoke arguments")
            return _http_smoke(arguments[1])
        if arguments and arguments[0] == "_paths-smoke":
            if len(arguments) != 1:
                raise ReleaseCheckError("invalid private path smoke arguments")
            return _installed_paths_smoke()
        parser = argparse.ArgumentParser(
            description=(
                "Validate MSX-AI releases without launching openMSX."))
        parser.add_argument(
            "--publish", action="store_true",
            help=(
                "require a clean Git checkout and stage exactly committed HEAD"))
        parser.add_argument(
            "--runtime-smoke", action="store_true",
            help=(
                "validate installed MCP STDIO and HTTP transports without "
                "build tools or hardware"))
        parser.add_argument(
            "--output-dir", type=Path,
            help=(
                "persist validated release assets (requires --publish)"))
        options = parser.parse_args(arguments)
        if options.runtime_smoke and options.publish:
            parser.error("--runtime-smoke cannot be combined with --publish")
        if options.runtime_smoke and options.output_dir is not None:
            parser.error(
                "--runtime-smoke cannot be combined with --output-dir")
        if options.output_dir is not None and not options.publish:
            parser.error("--output-dir requires --publish")
        if options.runtime_smoke:
            return run_runtime_smoke()
        output_directory = options.output_dir
        if output_directory is not None and not output_directory.is_absolute():
            output_directory = ROOT / output_directory
        return run_release_check(
            publish=options.publish, output_directory=output_directory)
    except (ReleaseCheckError, subprocess.TimeoutExpired) as exc:
        print(f"[release-check] FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
