#!/usr/bin/env python3
"""MSX-AI :: transport-neutral MCP bridge for emulated or physical MSX targets.

Zero external dependencies: speaks MCP (JSON-RPC 2.0) over newline-delimited
stdio directly, so it runs anywhere Python 3 does. It can drive an isolated
openMSX through its control channel or a physical MSX through the resident Z80
monitor and TCP/serial transport. Models can upload Z80 builds, inspect or
patch RAM/VRAM, control execution and render screenshots from captured VRAM.

openMSX is an optional local channel selected only by ``msx_local_*`` tools.
The independent ``msx_agent_*`` channel uses RealMSX and requires neither an
openMSX executable nor emulator ROMs. Both may coexist; a simulated bench binds
them to the same machine without creating a mutable "active backend". Emulator
sessions default to an isolated OPENMSX_HOME. Explicit user mode inherits the
normal openMSX setup, while overlay mode adds its ROM/config pools to temporary
MSX-AI templates without writing the user's files.
"""
import sys, os, json, tempfile, subprocess, pathlib, traceback, shutil, re, time
import ipaddress, math
import secrets
from contextvars import ContextVar
import xml.etree.ElementTree as ET

import base64
if __package__:
    from ._version import __version__
    from .msx_client import OpenMSX, OpenMSXError
    from .msx_real import (RealMSX, CAPABILITY_NAMES, AGENT_FEATURE_NAMES,
                           CAPABILITY_RUN, FEATURE_FILE_TRANSFER,
                           UART8251_BAUD, DEFAULT_PORT)
    from .msx_application import load_application, parse_application
    from . import msx_screenshot
    from .msx_transfer import TransferError, normalize_msx_basic_text
    from .execution import (current_cancellation_callback,
                            current_progress_callback)
    from . import msx_docs
    from .paths import (
        agent_directory,
        disks_directory,
        ensure_directory,
        openmsx_home,
        prepare_openmsx_home,
        require_source_root,
        resolve_user_path,
        source_root,
        source_work_root,
        user_root,
        work_root,
    )
else:  # Preserve ``python server/msx_mcp_server.py`` and existing imports.
    from _version import __version__
    from msx_client import OpenMSX, OpenMSXError
    from msx_real import (RealMSX, CAPABILITY_NAMES, AGENT_FEATURE_NAMES,
                          CAPABILITY_RUN, FEATURE_FILE_TRANSFER,
                          UART8251_BAUD, DEFAULT_PORT)
    from msx_application import load_application, parse_application
    import msx_screenshot
    from msx_transfer import TransferError, normalize_msx_basic_text
    from execution import (current_cancellation_callback,
                           current_progress_callback)
    import msx_docs
    from paths import (
        agent_directory,
        disks_directory,
        ensure_directory,
        openmsx_home,
        prepare_openmsx_home,
        require_source_root,
        resolve_user_path,
        source_root,
        source_work_root,
        user_root,
        work_root,
    )

Z80ASM = (os.environ.get("Z80ASM") or shutil.which("z80asm") or
          "/opt/homebrew/bin/z80asm")
MAKE = os.environ.get("MAKE") or shutil.which("make") or "make"
SOURCE_ROOT = source_root()
# Compatibility name retained for callers/tests that inspect the checkout.
PROJ = SOURCE_ROOT if SOURCE_ROOT is not None else user_root()
WORK = work_root()
DISKS = disks_directory()
SOURCE_WORK = source_work_root()
AGENT_DIR = agent_directory()
OPENMSX_HOME = openmsx_home()
AGENT_COM = AGENT_DIR / "MSXAI.COM"
AGENT_XFER_COM = AGENT_DIR / "MSXAIXF.COM"
AGENT_TSR_8251 = AGENT_DIR / "MCP8251.TSR"
AGENT_TSR_16C550 = AGENT_DIR / "MCP16550.TSR"
AGENT_TSR_UNAPI = AGENT_DIR / "MCPUNAPI.TSR"
AGENT_TU_COM = AGENT_DIR / "TU.COM"
AGENT_PORT_COM = AGENT_DIR / "MP.COM"
AGENT_MEMMAN_COM = AGENT_DIR / "MEMMAN.COM"
AGENT_TL_COM = AGENT_DIR / "TL.COM"
AGENT_TK_COM = AGENT_DIR / "TK.COM"
# Local Nextor / MSX-DOS 2 hard-disk image (never distributed by this project).
DOS_HDD = pathlib.Path(os.environ.get(
    "MSX_AI_DOS_HDD",
    SOURCE_WORK / "system-disks" / "msxdos.dsk")).expanduser()
BASIC_MACHINE = os.environ.get("MSX_AI_BASIC_MACHINE", "Gradiente_Expert20")
CBIOS_MACHINE = os.environ.get("MSX_AI_CBIOS_MACHINE", "C-BIOS_MSX2_BR")
MSX2PLUS_MACHINE = os.environ.get(
    "MSX_AI_MSX2PLUS_MACHINE", "Sony_HB-F1XDJ_128K_Lite")
DISK_EXTENSION = os.environ.get("MSX_AI_DISK_EXTENSION", "DDX_3.0")
DOS_EXTENSION = os.environ.get("MSX_AI_DOS_EXTENSION", "SunriseIDE_Nextor")
MCP_SLOT_EXPANDER = os.environ.get(
    "MSX_AI_MCP_SLOT_EXPANDER", "slotexpander")
RESIDENT_INSTALL_SECONDS = 15
RESIDENT_PROMPT_GRACE_SECONDS = 15
BENCH_AGENT_NAME = "MSXAI.COM"
BENCH_XFER_NAME = "MSXAIXF.COM"
BENCH_SUITE_DIR = "MSXAI"
BENCH_SUITE_HOME = f"A:\\{BENCH_SUITE_DIR}"
AGENT_PACKAGE_NAMES = (
    BENCH_AGENT_NAME,
    BENCH_XFER_NAME,
    "MCP8251.TSR",
    "MCP16550.TSR",
    "MCPUNAPI.TSR",
    "TU.COM",
    "MP.COM",
    "MEMMAN.COM",
    "TL.COM",
    "TK.COM",
)
REAL_BASIC_FILE_THRESHOLD = 512
REAL_BASIC_FILE_LIMIT = 0x4000
UART_BITS_PER_BYTE = 10
UART_SCREENSHOT_MARGIN = 1.15
SLOW_SCREENSHOT_SECONDS = 10.0
APPLICATION_ENVIRONMENTS = ("auto", "direct", "basic")
BASIC_PROMPT_ATTEMPTS = 3
BASIC_PROMPT_SCREEN_TIMEOUT = 10.0
BASIC_PROMPT_SETTLE_SECONDS = 0.5

LOCAL_PROFILES = ("basic", "disk", "dos", "msx2plus", "cbios", "auto")
OPENMSX_CONFIG_MODES = ("isolated", "user", "overlay")
DEFAULT_OPENMSX_CONFIG_MODE = os.environ.get(
    "MSX_AI_OPENMSX_CONFIG_MODE", "isolated")

PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = __version__


def _build_agent_artifacts():
    """Build and return the agent executable package used by MSX-DOS."""
    try:
        project = require_source_root()
    except RuntimeError as exc:
        raise OpenMSXError(str(exc)) from exc
    environment = os.environ.copy()
    environment["Z80ASM"] = Z80ASM
    try:
        build = subprocess.run(
            [MAKE, "agent"], cwd=project, env=environment,
            capture_output=True, text=True)
    except OSError as exc:
        raise OpenMSXError(f"could not run the canonical agent build: {exc}") from exc
    if build.returncode != 0:
        raise OpenMSXError(
            "could not build the MSX-DOS agent package with `make agent`:\n"
            + (build.stderr or build.stdout))
    artifacts = (
        AGENT_COM,
        AGENT_XFER_COM,
        AGENT_TSR_8251,
        AGENT_TSR_16C550,
        AGENT_TSR_UNAPI,
        AGENT_TU_COM,
        AGENT_PORT_COM,
        AGENT_MEMMAN_COM,
        AGENT_TL_COM,
        AGENT_TK_COM,
    )
    missing = [path for path in artifacts
               if not path.is_file() or path.stat().st_size == 0]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise OpenMSXError(
            f"agent build did not produce required artifact(s): {names}")
    return artifacts


def _build_agent_artifact():
    """Compatibility wrapper returning the primary control executable."""
    return _build_agent_artifacts()[0]


def _dos_prompt_visible(screen):
    """Return whether the last non-blank text row is an MSX-DOS prompt."""
    rows = [row.strip() for row in str(screen).splitlines() if row.strip()]
    return bool(rows and re.fullmatch(r"[A-Za-z]:\\[^>]*>", rows[-1]))


def _dos_prompt_drive(screen):
    """Return the current DOS drive letter, or ``None`` without a prompt."""
    rows = [row.strip() for row in str(screen).splitlines() if row.strip()]
    if not rows:
        return None
    match = re.fullmatch(r"([A-Za-z]):\\[^>]*>", rows[-1])
    return match.group(1).upper() if match else None


def _basic_prompt_visible(screen):
    """Return whether BASIC's last content row is its `Ok` prompt.

    MSX BASIC may reserve the final physical row for programmable function-key
    labels.  The default/Nextor label bar includes both LIST and RUN; it is not
    program output and therefore must not hide an otherwise current prompt.
    """
    physical_rows = str(screen).splitlines()
    if physical_rows:
        footer = physical_rows[-1].strip().lower()
        if "list" in footer and "run" in footer:
            physical_rows = physical_rows[:-1]
    rows = [row.strip() for row in physical_rows if row.strip()]
    return bool(rows and rows[-1].lower() == "ok")


def _validate_local_profile(profile):
    if not isinstance(profile, str) or profile not in LOCAL_PROFILES:
        allowed = ", ".join(repr(item) for item in LOCAL_PROFILES)
        raise ValueError(f"profile must be one of {allowed}")
    return profile


def _validate_config_mode(config_mode):
    if config_mode is None:
        config_mode = DEFAULT_OPENMSX_CONFIG_MODE
    if not isinstance(config_mode, str) or config_mode not in OPENMSX_CONFIG_MODES:
        allowed = ", ".join(repr(item) for item in OPENMSX_CONFIG_MODES)
        raise ValueError(f"config_mode must be one of {allowed}")
    return config_mode


def _profile_arguments(profile, *, require_files=True):
    """Return stable OpenMSX constructor arguments for one concrete profile."""
    if profile == "dos":
        if require_files and not DOS_HDD.is_file():
            raise OpenMSXError(f"MSX-DOS image not found: {DOS_HDD}")
        return {
            "machine": BASIC_MACHINE,
            "extensions": [DOS_EXTENSION],
            "harddisk": str(DOS_HDD),
        }
    if profile == "disk":
        return {"machine": BASIC_MACHINE, "extensions": [DISK_EXTENSION]}
    if profile == "msx2plus":
        return {"machine": MSX2PLUS_MACHINE, "extensions": []}
    if profile == "cbios":
        return {"machine": CBIOS_MACHINE, "extensions": []}
    if profile == "basic":
        return {"machine": BASIC_MACHINE, "extensions": []}
    raise ValueError("auto must be resolved before constructing openMSX")


def _profile_machine_name(profile):
    if profile in {"basic", "disk", "dos", "bench"}:
        return BASIC_MACHINE
    if profile == "msx2plus":
        return MSX2PLUS_MACHINE
    if profile == "cbios":
        return CBIOS_MACHINE
    return None


def _new_openmsx(*, config_mode, **arguments):
    """Construct the adapter while remaining compatible with older installs."""
    try:
        return OpenMSX(config_mode=config_mode, **arguments)
    except TypeError as exc:
        # A source checkout can briefly pair a new MCP core with an older
        # installed emulator adapter. Preserve the historical isolated mode,
        # but never pretend that user/overlay policies were honored.
        message = str(exc)
        if "config_mode" not in message or "unexpected keyword" not in message:
            raise
        if config_mode != "isolated":
            raise OpenMSXError(
                "the installed openMSX adapter does not support "
                f"config_mode={config_mode!r}; update msx-ai or use "
                "config_mode='isolated'") from exc
        machine = OpenMSX(**arguments)
        try:
            machine.config_mode = "isolated"
        except Exception:
            pass
        return machine


def _new_profile_machine(profile, *, config_mode):
    return _new_openmsx(
        config_mode=config_mode, **_profile_arguments(profile))

# --------------------------------------------------------------------------
# Independent, explicitly routed local and agent channels
# --------------------------------------------------------------------------
class BackendNotSelectedError(RuntimeError):
    """Raised when an explicit channel has no connected target."""


class BackendAmbiguousError(RuntimeError):
    """Raised when a legacy tool could address more than one live target."""


_TOOL_TARGET = ContextVar("msx_ai_tool_target", default=None)


class Session:
    def __init__(self):
        # Local openMSX control and the physical-agent protocol are independent
        # channels.  A simulated TCP bench intentionally publishes both, each
        # pointing at the same emulated machine through a different interface.
        self._local_msx = None
        self._local_profile = None
        self._local_requested_profile = None
        self._local_config_mode = None
        self._agent_msx = None
        self.local_id = None
        self.agent_id = None
        self.bench_id = None
        # Compatibility storage for tests/integrators that still assign the
        # historical ``msx``/``profile`` attributes directly.
        self._legacy_msx = None
        self._legacy_profile = None
        self.bench_machine = None
        self.bench_runtime = None

    def _fallback_for(self, target):
        if self._legacy_msx is None:
            return None, None
        is_agent = self._legacy_profile == "real"
        if (target == "agent") == is_agent:
            return self._legacy_msx, self._legacy_profile
        return None, None

    def backend(self, target):
        if target == "local":
            if self._local_msx is not None:
                return self._local_msx, self._local_profile
            return self._fallback_for("local")
        if target == "agent":
            if self._agent_msx is not None:
                return self._agent_msx, "real"
            return self._fallback_for("agent")
        raise ValueError("target must be 'local' or 'agent'")

    def connected_targets(self):
        targets = []
        for target in ("local", "agent"):
            backend, _profile = self.backend(target)
            if backend is not None:
                targets.append(target)
        return tuple(targets)

    def _resolve(self, target=None):
        target = target or _TOOL_TARGET.get()
        if target is not None:
            backend, profile = self.backend(target)
            if backend is None:
                action = ("msx_local_boot or msx_local_attach" if target == "local"
                          else "msx_agent_listen or msx_agent_connect")
                raise BackendNotSelectedError(
                    f"no {target} target is connected; use {action}")
            return backend, profile, target
        targets = self.connected_targets()
        if not targets:
            raise BackendNotSelectedError(
                "no MSX target is connected; use msx_local_boot or "
                "msx_local_attach for openMSX, or msx_agent_listen or "
                "msx_agent_connect for the ASM agent")
        if len(targets) != 1:
            raise BackendAmbiguousError(
                "both local openMSX and TCP agent targets are connected; use "
                "an explicit msx_local_* or msx_agent_* tool")
        backend, profile = self.backend(targets[0])
        return backend, profile, targets[0]

    @property
    def msx(self):
        try:
            return self._resolve()[0]
        except BackendNotSelectedError:
            return None

    @msx.setter
    def msx(self, value):
        self._legacy_msx = value

    @property
    def profile(self):
        try:
            return self._resolve()[1]
        except BackendNotSelectedError:
            return None

    @profile.setter
    def profile(self, value):
        self._legacy_profile = value

    def boot(self, profile="basic", boot_seconds=6, window=False,
             config_mode=None):
        if self.backend("local")[0] is not None:
            raise OpenMSXError(
                "a local openMSX target is already connected; close it "
                "explicitly with msx_local_shutdown first")
        profile = _validate_local_profile(profile)
        config_mode = _validate_config_mode(config_mode)
        candidates = ("basic", "cbios") if profile == "auto" else (profile,)
        errors = []
        machine = None
        resolved_profile = None
        screen = None
        for candidate in candidates:
            machine = None
            try:
                machine = _new_profile_machine(
                    candidate, config_mode=config_mode)
                machine.start(headless=not window)
                machine.power_on()
                if window:
                    # Show a real openMSX window on the user's screen (renderer
                    # none -> SDLGL-PP). The same control channel still drives it,
                    # so the user and the AI operate one shared instance.
                    machine.cmd("set renderer SDLGL-PP")
                    machine.cmd("set throttle on")
                machine.advance(boot_seconds if candidate != "dos" else 14)
                if candidate == "disk":
                    # DDX shows its insert-disk prompt; ESC enters DDX-BASIC.
                    machine.press("ESC")
                    machine.advance(3)
                screen = machine.screen_text()
                resolved_profile = candidate
                break
            except Exception as exc:
                # A partially booted emulator is still owned by this session.
                # Never publish it or leave its process running after failure.
                if machine is not None:
                    machine.close()
                machine = None
                if profile != "auto":
                    raise
                errors.append((candidate, exc))
        if machine is None or resolved_profile is None:
            detail = "; ".join(
                f"{candidate}: {error}" for candidate, error in errors)
            raise OpenMSXError(
                "auto profile could not boot either the configured BASIC "
                f"machine or the C-BIOS fallback ({detail})")
        self._local_msx = machine
        self._local_profile = resolved_profile
        self._local_requested_profile = profile
        self._local_config_mode = config_mode
        self.local_id = "local-" + secrets.token_hex(6)
        return screen

    def attach(self, socket_path=None):
        """Connect to the user's already-running openMSX window (shared instance)."""
        if self.backend("local")[0] is not None:
            raise OpenMSXError(
                "a local openMSX target is already connected; detach or close "
                "it explicitly before attaching another")
        machine = OpenMSX()
        try:
            machine.attach(socket_path)
            # Do not touch throttle/power of the user's session.
            machine.enable_keybuf()
            screen = machine.screen_text()
        except Exception:
            machine.close()
            raise
        self._local_msx = machine
        self._local_profile = "attach"
        self._local_requested_profile = "attach"
        # An attached process owns its own configuration; this session neither
        # isolates nor overlays it.
        self._local_config_mode = "user"
        self.local_id = "local-" + secrets.token_hex(6)
        return screen

    def listen_agent(self, host="127.0.0.1", port=DEFAULT_PORT, timeout=60,
                     cancelled=None):
        """Wait for an ASM agent or transparent adapter to connect over TCP."""
        if self.backend("agent")[0] is not None:
            raise OpenMSXError(
                "an ASM-agent target is already connected; disconnect it "
                "explicitly with msx_agent_disconnect first")
        if self.bench_machine is not None or self.bench_runtime is not None:
            raise OpenMSXError(
                "the hybrid TCP bench still owns the local machine; close "
                "that bench explicitly before listening for another agent")
        real = RealMSX(host=host, port=int(port)).listen()
        try:
            peer = real.accept(timeout=float(timeout), cancelled=cancelled)
        except Exception:
            real.close()
            raise
        self._agent_msx = real
        self.agent_id = "agent-" + secrets.token_hex(6)
        return peer

    def connect_agent(self, host, port=DEFAULT_PORT, timeout=60):
        """Connect to an ASM agent or transparent adapter over TCP."""
        if self.backend("agent")[0] is not None:
            raise OpenMSXError(
                "an ASM-agent target is already connected; disconnect it "
                "explicitly with msx_agent_disconnect first")
        if self.bench_machine is not None or self.bench_runtime is not None:
            raise OpenMSXError(
                "the hybrid TCP bench still owns the local machine; close "
                "that bench explicitly before connecting another agent")
        real = RealMSX(host=host, port=int(port))
        try:
            peer = real.connect(timeout=float(timeout))
        except Exception:
            real.close()
            raise
        self._agent_msx = real
        self.agent_id = "agent-" + secrets.token_hex(6)
        return peer

    def start_tcp_bench(self, host="127.0.0.1", port=0, timeout=60,
                        window=False, mode="resident", debug=False,
                        preload_files=(), cancelled=None):
        """Start one isolated openMSX as a physical-agent TCP simulation."""
        if self.connected_targets():
            raise OpenMSXError(
                "the hybrid TCP bench requires empty local and agent slots; "
                "close existing targets explicitly first")
        if mode not in ("resident", "monitor"):
            raise ValueError("mode must be 'resident' or 'monitor'")
        if not isinstance(debug, bool):
            raise TypeError("debug must be a boolean")
        if debug and mode != "monitor":
            raise ValueError("DEBUG is available only with mode='monitor'")
        if not DOS_HDD.is_file():
            raise OpenMSXError(f"MSX-DOS image not found: {DOS_HDD}")

        runtime = tempfile.TemporaryDirectory(prefix="msx-ai-tcp-bench-")
        root = pathlib.Path(runtime.name)
        disk = root / "msxdos.dsk"
        home = root / "openmsx-home"
        # The disposable bench disk receives the complete ten-file package
        # under A:\MSXAI. Legacy root copies and stale directory copies are
        # removed because diskmanipulator does not overwrite same-name files.
        machine = None
        real = None
        try:
            canonical_artifacts = _build_agent_artifacts()
            if len(canonical_artifacts) != len(AGENT_PACKAGE_NAMES):
                raise OpenMSXError("agent build returned an incomplete package")
            shutil.copyfile(DOS_HDD, disk)
            prepared_openmsx_home = prepare_openmsx_home(OPENMSX_HOME)
            shutil.copytree(
                prepared_openmsx_home, home,
                ignore=shutil.ignore_patterns(
                    ".DS_Store", "persistent", "savestates", "replays",
                    "screenshots", "recordings", "settings.local.xml",
                    ".filecache", "imgui.ini", "software"))
            runtime_artifacts = []
            for dos_name, canonical in zip(
                    AGENT_PACKAGE_NAMES, canonical_artifacts, strict=True):
                artifact = root / dos_name
                shutil.copyfile(canonical, artifact)
                runtime_artifacts.append((dos_name, artifact))

            # Bench resume journals belong to this disposable simulation, not
            # to the persistent physical-target recovery directory.
            real = RealMSX(
                host=host, port=int(port),
                file_transfer_state_directory=root / "transfers").listen()
            machine = OpenMSX(
                machine=BASIC_MACHINE,
                extensions=[
                    MCP_SLOT_EXPANDER, DOS_EXTENSION, "ram512k",
                    "rs232_proto"],
                harddisk=str(disk), home=home,
            ).start(headless=not window)
            # Import while the emulated machine is off. openMSX may otherwise
            # retain stale filesystem sectors for a disk mounted by MSX-DOS.
            machine.cmd("set power off")
            machine.cmd("diskmanipulator chdir hda1 /")
            root_listing = machine.cmd("diskmanipulator dir hda1")
            for dos_name, _artifact in runtime_artifacts:
                legacy = re.search(
                    rf"(?im)^\s*{re.escape(dos_name)}\s+\S+\s+(\d+)\s*$",
                    root_listing)
                if legacy is not None:
                    machine.cmd(
                        f"diskmanipulator delete hda1 {dos_name}")
                    root_listing = machine.cmd("diskmanipulator dir hda1")
            if re.search(
                    rf"(?im)^\s*{re.escape(BENCH_SUITE_DIR)}\b.*<DIR>",
                    root_listing) is None:
                machine.cmd(
                    f"diskmanipulator mkdir hda1 {BENCH_SUITE_DIR}")
            machine.cmd(
                f"diskmanipulator chdir hda1 {BENCH_SUITE_DIR}")
            for dos_name, artifact in runtime_artifacts:
                listing = machine.cmd("diskmanipulator dir hda1")
                entry = re.search(
                    rf"(?im)^\s*{re.escape(dos_name)}\s+\S+\s+(\d+)\s*$",
                    listing)
                if entry is not None:
                    machine.cmd(f"diskmanipulator delete hda1 {dos_name}")
                machine.cmd(f"diskmanipulator import hda1 {{{artifact}}}")
                listing = machine.cmd("diskmanipulator dir hda1")
                entry = re.search(
                    rf"(?im)^\s*{re.escape(dos_name)}\s+\S+\s+(\d+)\s*$",
                    listing)
                expected_size = artifact.stat().st_size
                if entry is None or int(entry.group(1)) != expected_size:
                    observed = "missing" if entry is None else entry.group(1)
                    raise OpenMSXError(
                        f"bench disk {dos_name} verification failed: "
                        f"expected {expected_size} bytes, found {observed}")
            machine.cmd("diskmanipulator chdir hda1 /")
            for preload in preload_files:
                preload = pathlib.Path(preload).resolve()
                if not preload.is_file():
                    raise OpenMSXError(
                        f"bench preload file not found: {preload}")
                machine.cmd(f"diskmanipulator import hda1 {{{preload}}}")
            machine.power_on()
            machine.advance(14)
            for setup_command in (
                    f"SET MSXAI_HOME={BENCH_SUITE_HOME}",
                    f"PATH {BENCH_SUITE_HOME};%PATH%"):
                machine.type_line(setup_command)
                machine.advance(0.5)
            command = f"{pathlib.Path(BENCH_AGENT_NAME).stem} /DRIVER:8251"
            if mode == "monitor":
                command += " /MONITOR"
            if debug:
                command += " DEBUG"
            machine.type_line(command)
            machine.advance(
                RESIDENT_INSTALL_SECONDS if mode == "resident" else 1)

            if mode == "resident":
                screen = machine.screen_text()
                grace = RESIDENT_PROMPT_GRACE_SECONDS
                while not _dos_prompt_visible(screen) and grace > 0:
                    machine.advance(1)
                    grace -= 1
                    screen = machine.screen_text()
                if not _dos_prompt_visible(screen):
                    raise OpenMSXError(
                        "resident agent did not return to an MSX-DOS prompt:\n"
                        + screen)

            # Installation is validated through the same TCP handshake used
            # for physical machines.  A relocated MemMan TSR is intentionally
            # not inspected through openMSX's debugger or a fixed RAM address.
            machine.cmd("set throttle on")
            machine.cmd(f"set rs232-net-address {host}:{real.port}")
            machine.cmd("set rs232-net-ip232 off")
            machine.cmd("plug msx-rs232 rs232-net")
            try:
                peer = real.accept(
                    timeout=float(timeout), cancelled=cancelled)
            except Exception as exc:
                try:
                    failed_screen = machine.screen_text()
                except Exception as screen_exc:
                    failed_screen = f"<screen unavailable: {screen_exc}>"
                raise OpenMSXError(
                    f"{exc}\nMSX screen at TCP handshake failure:\n"
                    f"{failed_screen}") from exc
            expected_runtime = (
                "resident" if mode == "resident" else "foreground-monitor")
            if (real.runtime_mode is not None and
                    real.runtime_mode != expected_runtime):
                raise OpenMSXError(
                    f"agent handshake reported runtime {real.runtime_mode!r}; "
                    f"expected {expected_runtime!r}")
            real.simulation = "openmsx-rs232-net"

            # Keep the renderer disabled during boot and the initial serial
            # negotiation. A visible renderer can delay the foreground DEBUG
            # bootstrap beyond the deliberately short raw-probe timeout.
            if window:
                machine.cmd("set renderer SDLGL-PP")

            self._agent_msx = real
            self._local_msx = machine
            self._local_profile = "bench"
            self._local_requested_profile = "bench"
            self._local_config_mode = "isolated"
            self.bench_id = "bench-" + secrets.token_hex(6)
            self.local_id = self.bench_id + ":local"
            self.agent_id = self.bench_id + ":agent"
            self.bench_machine = machine
            self.bench_runtime = runtime
            return peer
        except Exception:
            if real is not None:
                real.close()
            if machine is not None:
                machine.close()
            runtime.cleanup()
            raise

    # Backward-compatible internal name used by the original MCP tool.
    listen_real = listen_agent

    def require(self, target=None):
        return self._resolve(target)[0]

    def shutdown_local(self):
        local, _profile = self.backend("local")
        if local is None:
            return False
        if local is self.bench_machine:
            raise OpenMSXError(
                "the local target belongs to the hybrid TCP bench; use "
                "msx_tcp_bench_shutdown to close both bench channels")
        try:
            local.close()
        finally:
            self._local_msx = None
            self._local_profile = None
            self._local_requested_profile = None
            self._local_config_mode = None
            self.local_id = None
            if self._legacy_msx is local:
                self._legacy_msx = None
                self._legacy_profile = None
        return True

    def disconnect_agent(self):
        agent, _profile = self.backend("agent")
        if agent is None:
            return False
        if not getattr(agent, "write_quarantined", False):
            try:
                if agent.status()["state"] == "paused":
                    agent.resume()
            except Exception:
                pass
        try:
            agent.close()
        finally:
            self._agent_msx = None
            self.agent_id = None
            if self._legacy_msx is agent:
                self._legacy_msx = None
                self._legacy_profile = None
        return True

    def shutdown_bench(self):
        if self.bench_machine is None and self.bench_runtime is None:
            return False
        agent = self._agent_msx
        if agent is not None:
            if not getattr(agent, "write_quarantined", False):
                try:
                    if agent.status()["state"] == "paused":
                        agent.resume()
                except Exception:
                    pass
            try:
                agent.close()
            except Exception:
                pass
            self._agent_msx = None
            self.agent_id = None
        if self.bench_machine is not None:
            try:
                self.bench_machine.close()
            except Exception:
                pass
            if self._local_msx is self.bench_machine:
                self._local_msx = None
                self._local_profile = None
                self._local_requested_profile = None
                self._local_config_mode = None
                self.local_id = None
            self.bench_machine = None
        if self.bench_runtime is not None:
            try:
                self.bench_runtime.cleanup()
            except Exception:
                pass
            self.bench_runtime = None
        self.bench_id = None
        return True

    def shutdown_all(self):
        if self.bench_machine is not None or self.bench_runtime is not None:
            self.shutdown_bench()
        else:
            self.disconnect_agent()
            self.shutdown_local()
        # Direct-assignment compatibility state may not be represented above.
        legacy = self._legacy_msx
        if legacy is not None:
            try:
                legacy.close()
            except Exception:
                pass
        self._legacy_msx = None
        self._legacy_profile = None
        self._local_requested_profile = None
        self._local_config_mode = None
        self.local_id = None
        self.agent_id = None
        self.bench_id = None

    # Historical internal name.  Server teardown still means every channel;
    # public tools use explicit local/agent/bench lifecycle methods below.
    shutdown = shutdown_all


SESSION = Session()

# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------
def _screen():
    return SESSION.require().screen_text()


def t_boot(profile="basic", window=False, config_mode=None):
    profile = _validate_local_profile(profile)
    config_mode = _validate_config_mode(config_mode)
    scr = SESSION.boot(profile, window=window, config_mode=config_mode)
    tag = "window" if window else "headless"
    machine, resolved = SESSION.backend("local")
    machine_name = getattr(machine, "machine", None)
    details = [
        f"profile={profile}",
        f"resolved_profile={resolved}",
        f"config_mode={config_mode}",
        tag,
    ]
    if isinstance(machine_name, (str, os.PathLike)):
        details.append(f"machine={os.fspath(machine_name)}")
    return f"[boot {' '.join(details)}]\n{scr}"


def t_attach(socket_path=None):
    scr = SESSION.attach(socket_path)
    selected = SESSION.require("local").socket_path
    return (f"[attached to openMSX socket={selected} — shared instance]\n" +
            scr)


def _path_exists(value):
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        return False
    try:
        return pathlib.Path(value).expanduser().is_file()
    except OSError:
        return False


def _platform_label(value):
    value = str(value or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value in {"darwin", "mac", "macos"}:
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return value


def _fallback_preflight(machine):
    """Minimal read-only report for a temporarily mismatched client module."""
    executable = _json_scalar(getattr(machine, "bin", None))
    executable_found = _path_exists(executable)
    if not executable_found and isinstance(executable, str):
        executable_found = shutil.which(executable) is not None
    home = _json_scalar(getattr(machine, "home", None))
    platform_name = (
        "windows" if sys.platform.startswith("win") else
        "macos" if sys.platform == "darwin" else
        "linux" if sys.platform.startswith("linux") else sys.platform)
    problems = []
    if not executable_found:
        problems.append(
            "openMSX executable was not found; install openMSX or set "
            "OPENMSX_BIN to its executable")
    problems.append(
        "this msx-ai openMSX adapter lacks the static preflight API; update "
        "the package before relying on machine/ROM readiness")
    return {
        "ready": False,
        "platform": getattr(machine, "platform", platform_name),
        "control_transport": getattr(
            machine, "control_transport",
            "tcp_sspi" if platform_name == "windows" else "stdio"),
        "control_transport_supported": platform_name != "windows",
        "boot_supported": platform_name != "windows",
        "attach_transport": (
            "tcp_sspi" if platform_name == "windows" else "unix_socket"),
        "attach_supported": platform_name != "windows",
        "config_mode": getattr(machine, "config_mode", "isolated"),
        "machine": getattr(machine, "machine", None),
        "executable": executable,
        "executable_found": executable_found,
        "home": home,
        "user_home": _json_scalar(getattr(machine, "user_home", None)),
        "home_exists": bool(home and pathlib.Path(home).is_dir()),
        "machine_config_found": None,
        "machine_config_candidates": [],
        "problems": problems,
    }


def _share_roots(report):
    roots = []
    config_mode = report.get("config_mode")
    user_home_value = report.get("user_home")
    user_share = (pathlib.Path(user_home_value) / "share"
                  if isinstance(user_home_value, (str, os.PathLike)) and
                  os.fspath(user_home_value) else None)
    for candidate in report.get("machine_config_candidates", []):
        try:
            path = pathlib.Path(candidate)
        except TypeError:
            continue
        if path.suffix.lower() == ".xml":
            share = path.parent.parent
            if (config_mode == "isolated" and user_share is not None and
                    share.resolve(strict=False) ==
                    user_share.resolve(strict=False)):
                continue
            roots.append(share)
    home_keys = (
        ("home",) if config_mode == "isolated" else
        ("user_home",) if config_mode == "user" else
        ("home", "user_home"))
    for key in home_keys:
        value = report.get(key)
        if isinstance(value, (str, os.PathLike)) and os.fspath(value):
            home = pathlib.Path(value)
            roots.extend((home / "share", home))
    executable = report.get("executable")
    if isinstance(executable, (str, os.PathLike)) and os.fspath(executable):
        parent = pathlib.Path(executable).parent
        roots.extend((parent / "share", parent.parent / "Resources" / "share"))
    if report.get("config_mode") in {"isolated", "overlay"}:
        roots.extend((
            pathlib.Path(OPENMSX_HOME) / "share",
            pathlib.Path(__file__).resolve().parent / "resources" / "openmsx" /
            "share",
        ))
    result = []
    seen = set()
    for root in roots:
        normalized = str(root.resolve(strict=False)).casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(root)
    return result


def _component_config(kind, name, report, shares):
    directory = "machines" if kind == "machine" else "extensions"
    candidates = [root / directory / f"{name}.xml" for root in shares]
    seen = set()
    normalized = []
    for candidate in candidates:
        path = pathlib.Path(candidate)
        marker = str(path.resolve(strict=False)).casefold()
        if marker not in seen:
            seen.add(marker)
            normalized.append(path)
    selected = next((path for path in normalized if path.is_file()), None)
    return {
        "kind": kind,
        "name": name,
        "config_found": selected is not None,
        "config_path": str(selected) if selected is not None else None,
        "config_candidates": [str(path) for path in normalized],
    }


def _rom_requirements(config_path):
    try:
        root = ET.parse(config_path).getroot()
    except (ET.ParseError, OSError):
        return []
    requirements = []
    for rom in root.findall(".//rom"):
        filenames = [
            (node.text or "").strip() for node in rom.findall("filename")
            if (node.text or "").strip()
        ]
        sha1s = [
            (node.text or "").strip().lower() for node in rom.findall("sha1")
            if (node.text or "").strip()
        ]
        if filenames or sha1s:
            requirements.append({"filenames": filenames, "sha1s": sha1s})
    return requirements


def _rom_catalog(shares):
    catalog = []
    seen = set()
    for root in shares:
        pool = root / "systemroms"
        if not pool.is_dir():
            continue
        try:
            entries = pool.rglob("*")
            for path in entries:
                if not path.is_file():
                    continue
                marker = str(path.resolve(strict=False)).casefold()
                if marker in seen:
                    continue
                seen.add(marker)
                catalog.append((path.name.casefold(), str(path)))
        except OSError:
            continue
    return catalog


def _rom_requirement_found(requirement, catalog):
    filenames = [item.casefold() for item in requirement["filenames"]]
    sha1s = [item.casefold() for item in requirement["sha1s"]]
    for basename, _path in catalog:
        if any(
                basename == filename or
                basename.endswith("." + filename) or
                basename.endswith("." + filename + ".gz")
                for filename in filenames):
            return True
        if any(basename.startswith(sha1) for sha1 in sha1s):
            return True
    return False


def _inspect_profile_roms(profile, report, catalog_cache=None):
    """Conservatively inspect XML ROM references in known openMSX pools."""
    shares = _share_roots(report)
    arguments = _profile_arguments(profile, require_files=False)
    components = [
        _component_config("machine", arguments["machine"], report, shares),
    ]
    components.extend(
        _component_config("extension", extension, report, shares)
        for extension in arguments.get("extensions", []))
    missing_configs = [
        f"{item['kind']} config not found for {item['name']}"
        for item in components if not item["config_found"]
    ]
    machine_config_found = components[0]["config_found"]
    report["machine_config_found"] = machine_config_found
    if machine_config_found:
        # A managed isolated/overlay template can be present in package
        # resources before its writable home is materialized. Static doctor
        # checks may therefore prove availability beyond adapter.preflight().
        report["problems"] = [
            problem for problem in report["problems"]
            if not ("machine config" in problem.lower() and
                    "not found" in problem.lower())
        ]
    requirements = []
    for component in components:
        if component["config_path"] is not None:
            requirements.extend(_rom_requirements(component["config_path"]))
    catalog_key = tuple(str(root.resolve(strict=False)).casefold()
                        for root in shares)
    if catalog_cache is not None and catalog_key in catalog_cache:
        catalog = catalog_cache[catalog_key]
    else:
        catalog = _rom_catalog(shares)
        if catalog_cache is not None:
            catalog_cache[catalog_key] = catalog
    missing_roms = []
    for requirement in requirements:
        if _rom_requirement_found(requirement, catalog):
            continue
        label = (requirement["filenames"][0]
                 if requirement["filenames"] else requirement["sha1s"][0])
        missing_roms.append(label)
    report["config_components"] = components
    report["required_roms"] = [
        (item["filenames"][0] if item["filenames"] else item["sha1s"][0])
        for item in requirements
    ]
    report["missing_roms"] = missing_roms
    report["rom_readiness"] = (
        "unverified" if missing_roms else
        "ready" if requirements else "not-required")
    if missing_configs:
        report["ready"] = False
        report["problems"].extend(missing_configs)
    if missing_roms:
        # openMSX can also find SHA-matched files inside archives and external
        # pools that static inspection cannot enumerate. Mark readiness as
        # unverified, not as a definitive licensing/configuration failure.
        report["ready"] = False
        report["problems"].extend(
            "ROM requirement was not found by filename or SHA-1 in the known "
            "systemroms pools; runtime readiness remains unverified: " + name
            for name in missing_roms)
    blocking_problems = []
    for problem in report["problems"]:
        lowered = problem.lower()
        attach_only = (
            report.get("boot_supported") and
            any(marker in lowered for marker in ("attach", "sspi")))
        if not attach_only:
            blocking_problems.append(problem)
    report["ready"] = bool(
        report.get("executable_found") and report.get("boot_supported") and
        not missing_configs and not missing_roms and not blocking_problems)
    return report


def _preflight_candidate(profile, config_mode, *, catalog_cache=None):
    """Inspect one concrete profile without starting or persisting openMSX."""
    try:
        arguments = _profile_arguments(profile, require_files=False)
    except Exception as exc:
        return {
            "profile": profile,
            "machine": _profile_machine_name(profile),
            "ready": False,
            "platform": None,
            "control_transport": None,
            "control_transport_supported": False,
            "boot_supported": False,
            "attach_transport": None,
            "attach_supported": False,
            "config_mode": config_mode,
            "executable": None,
            "executable_found": False,
            "home": None,
            "user_home": None,
            "home_exists": False,
            "machine_config_found": None,
            "machine_config_candidates": [],
            "problems": [str(exc)],
        }
    try:
        machine = _new_openmsx(config_mode=config_mode, **arguments)
        preflight = getattr(machine, "preflight", None)
        report = preflight() if callable(preflight) else _fallback_preflight(machine)
        if not isinstance(report, dict):
            raise TypeError("OpenMSX.preflight() must return a dictionary")
    except Exception as exc:
        report = {
            "ready": False,
            "platform": None,
            "control_transport": None,
            "control_transport_supported": False,
            "boot_supported": False,
            "attach_transport": None,
            "attach_supported": False,
            "config_mode": config_mode,
            "machine": arguments["machine"],
            "executable": None,
            "executable_found": False,
            "home": None,
            "user_home": None,
            "home_exists": False,
            "machine_config_found": None,
            "machine_config_candidates": [],
            "problems": [f"could not inspect openMSX: {exc}"],
        }
    problems = report.get("problems", [])
    if isinstance(problems, str):
        problems = [problems]
    platform_name = _platform_label(report.get("platform"))
    control_transport_supported = bool(report.get(
        "control_transport_supported", platform_name != "windows"))
    normalized = {
        "profile": profile,
        "machine": _json_scalar(report.get("machine", arguments["machine"])),
        "ready": bool(report.get("ready", False)),
        "platform": platform_name,
        "control_transport": _json_scalar(report.get("control_transport")),
        "control_transport_supported": bool(
            control_transport_supported),
        "boot_supported": bool(report.get(
            "boot_supported", control_transport_supported)),
        "attach_transport": _json_scalar(report.get("attach_transport")),
        "attach_supported": bool(report.get("attach_supported", False)),
        "config_mode": _json_scalar(report.get("config_mode", config_mode)),
        "executable": _json_scalar(report.get("executable")),
        "executable_found": bool(report.get("executable_found", False)),
        "home": _json_scalar(report.get("home")),
        "user_home": _json_scalar(report.get("user_home")),
        "home_exists": bool(report.get("home_exists", False)),
        "machine_config_found": report.get("machine_config_found"),
        "machine_config_candidates": [
            _json_scalar(item)
            for item in report.get("machine_config_candidates", [])
        ],
        "problems": [str(item.get("message", item))
                     if isinstance(item, dict) else str(item)
                     for item in problems],
    }
    normalized = _inspect_profile_roms(
        profile, normalized, catalog_cache=catalog_cache)
    if profile == "dos" and not DOS_HDD.is_file():
        normalized["ready"] = False
        normalized["problems"].append(f"MSX-DOS image not found: {DOS_HDD}")
    return normalized


def _doctor_issue(message, *, severity="error"):
    lowered = message.lower()
    if "executable" in lowered or "openmsx_bin" in lowered:
        code = "openmsx-executable-missing"
        action = "Install openMSX or set OPENMSX_BIN to the executable path."
    elif "preflight api" in lowered:
        code = "adapter-preflight-unavailable"
        action = "Update msx-ai so the local adapter can validate this setup."
    elif "msx-dos image" in lowered or "msxdos.dsk" in lowered:
        code = "dos-image-missing"
        action = "Set MSX_AI_DOS_HDD to an existing licensed MSX-DOS/Nextor image."
    elif "rom" in lowered or "firmware" in lowered:
        extension_firmware = any(
            item in lowered for item in ("ddx", "nextor", "sunrise"))
        code = ("extension-rom-missing" if extension_firmware
                else "machine-rom-missing")
        action = (
            "Install the freely distributable C-BIOS ROM set in an openMSX "
            "systemroms pool."
            if "cbios" in lowered or "c-bios" in lowered else
            "Install the required extension firmware in an openMSX systemroms "
            "pool; disk/DOS profile semantics cannot be replaced by C-BIOS."
            if extension_firmware else
            "Add the legally obtained ROM to an openMSX systemroms pool, or "
            "use profile='cbios'/'auto'.")
    elif "config" in lowered and "not found" in lowered:
        is_machine = "machine" in lowered
        code = ("machine-config-missing" if is_machine
                else "extension-config-missing")
        action = (
            "Verify the selected config mode/home and machine XML, or use the "
            "C-BIOS profile bundled with openMSX."
            if is_machine else
            "Install or select an openMSX extension definition available in "
            "the chosen configuration mode.")
    elif "sspi" in lowered or "attach" in lowered:
        code = "windows-sspi-unavailable"
        action = (
            "Owned boot is available; install/configure SSPI attach support "
            "before connecting to an existing openMSX instance."
            if ("owned boot" in lowered and
                any(item in lowered for item in ("available", "supported")))
            else "Install/configure the Windows SSPI control helper before "
            "booting or attaching to a local openMSX instance.")
    else:
        code = "openmsx-preflight-problem"
        action = "Review the selected openMSX executable, machine and config home."
    return {"severity": severity, "code": code,
            "message": message, "action": action}


def t_local_doctor(profile="auto", config_mode=None):
    """Return a read-only local openMSX readiness report."""
    profile = _validate_local_profile(profile)
    config_mode = _validate_config_mode(config_mode)
    candidates = ("basic", "cbios") if profile == "auto" else (profile,)
    catalog_cache = {}
    reports = [
        _preflight_candidate(
            item, config_mode, catalog_cache=catalog_cache)
        for item in candidates
    ]
    selected = next((item for item in reports if item["ready"]), None)
    if selected is None and profile != "auto":
        selected = reports[0]

    issues = []
    if profile == "auto" and selected is not None and selected["profile"] != "basic":
        basic_problems = reports[0]["problems"] or [
            "the configured BASIC profile did not pass preflight"]
        issues.append({
            "severity": "warning",
            "code": "auto-profile-fallback",
            "message": (
                "auto resolved to C-BIOS because the configured BASIC profile "
                f"was not ready: {'; '.join(basic_problems)}"),
            "action": (
                "Use profile='cbios' for deterministic proprietary-ROM-free "
                "startup, or "
                "install the configured machine firmware to restore BASIC."),
        })
    problem_source = reports if selected is None else [selected]
    for report in problem_source:
        for problem in report["problems"]:
            lowered = problem.lower()
            severity = (
                "warning" if "unverified" in lowered else
                "warning" if (any(marker in lowered
                                  for marker in ("attach", "sspi")) and
                              report.get("boot_supported")) else
                "error")
            issues.append(_doctor_issue(problem, severity=severity))

    transport_source = selected or reports[-1]
    if (transport_source.get("platform") == "windows" and
            (not transport_source.get("boot_supported") or
             not transport_source.get("attach_supported"))):
        message = (
            "Windows local control uses the authenticated tcp_sspi endpoint; "
            "the required SSPI helper is not available for owned boot and/or "
            "attach in this environment")
        if not any(item["code"] == "windows-sspi-unavailable"
                   for item in issues):
            issues.append(_doctor_issue(
                message, severity=(
                    "warning" if transport_source.get("boot_supported")
                    else "error")))

    deduplicated_issues = []
    seen_issues = set()
    for issue in issues:
        marker = (issue["code"], issue["message"])
        if marker not in seen_issues:
            seen_issues.add(marker)
            deduplicated_issues.append(issue)

    return {
        "platform": transport_source.get("platform"),
        "executable": transport_source.get("executable"),
        "executable_found": transport_source.get("executable_found", False),
        "control_transport": transport_source.get("control_transport"),
        "control_transport_supported": transport_source.get(
            "control_transport_supported", False),
        "transport_ready": bool(
            transport_source.get("executable_found") and
            transport_source.get("control_transport_supported")),
        "boot_supported": transport_source.get("boot_supported", False),
        "attach_transport": transport_source.get("attach_transport"),
        "attach_supported": transport_source.get("attach_supported", False),
        "config_mode": config_mode,
        "config_home": transport_source.get("home"),
        "user_config_home": transport_source.get("user_home"),
        "config_home_exists": transport_source.get("home_exists", False),
        "requested_profile": profile,
        "resolved_profile": selected["profile"] if selected else None,
        "machine": selected["machine"] if selected else None,
        "machine_config_found": (
            selected.get("machine_config_found") if selected else None),
        "profile_ready": bool(selected and selected["ready"]),
        "ready": bool(selected and selected["ready"]),
        "candidates": reports,
        "issues": deduplicated_issues,
        "persistent_process_started": False,
    }


def t_real_listen(host="127.0.0.1", port=DEFAULT_PORT, timeout=60):
    return t_agent_listen(host=host, port=port, timeout=timeout)


def _format_endpoint(endpoint):
    if isinstance(endpoint, (tuple, list)) and len(endpoint) >= 2:
        return f"{endpoint[0]}:{endpoint[1]}"
    return str(endpoint)


def _validate_agent_endpoint(host, port, timeout, *, maximum_timeout):
    if not isinstance(host, str):
        raise TypeError("host must be an IPv4 address string")
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("host must be a literal IPv4 address") from exc
    if (address.is_unspecified or address.is_multicast or
            int(address) == 0xFFFFFFFF):
        raise ValueError(
            "host must be a specific unicast IPv4 address, not a wildcard, "
            "multicast, or broadcast address")
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("port must be in range 1..65535")
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a number")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise TypeError("timeout must be a number") from exc
    if (not math.isfinite(timeout) or timeout <= 0 or
            timeout > maximum_timeout):
        raise ValueError(
            f"timeout must be a positive finite number no greater than "
            f"{maximum_timeout}")
    return str(address), port, timeout


def t_agent_listen(host="127.0.0.1", port=DEFAULT_PORT, timeout=60):
    host, port, timeout = _validate_agent_endpoint(
        host, port, timeout, maximum_timeout=86400)
    peer = SESSION.listen_agent(
        host=host, port=port, timeout=timeout,
        cancelled=current_cancellation_callback())
    return (f"[MSX agent connected from {_format_endpoint(peer)} "
            f"over TCP/IP to {host}:{int(port)}]")


def t_agent_connect(host, port=DEFAULT_PORT, timeout=60):
    host, port, timeout = _validate_agent_endpoint(
        host, port, timeout, maximum_timeout=300)
    peer = SESSION.connect_agent(host=host, port=port, timeout=timeout)
    return f"[MSX agent connected over TCP/IP to {_format_endpoint(peer)}]"


def t_tcp_bench_start(host="127.0.0.1", port=0, timeout=60, window=False,
                      mode="resident", debug=False):
    """Boot one isolated openMSX and reach its resident agent only over TCP."""
    if host != "127.0.0.1":
        raise ValueError("the simulated TCP bench host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if not 0 <= port <= 65535:
        raise ValueError("port must be in range 0..65535")
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a number")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise TypeError("timeout must be a number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("timeout must be a positive finite number at most 300")
    SESSION.start_tcp_bench(
        host=host, port=port, timeout=timeout, window=window,
        mode=mode, debug=debug,
        cancelled=current_cancellation_callback())
    return t_tcp_bench_status()


def t_screen():
    return _screen()


def _json_scalar(value):
    """Return a JSON-safe scalar for optional adapter metadata."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return str(value)


def _local_runtime_metadata(machine, profile):
    requested = SESSION._local_requested_profile or profile
    config_mode = (SESSION._local_config_mode or
                   getattr(machine, "config_mode", None))
    if config_mode is None and profile != "attach":
        config_mode = "isolated"
    machine_name = (getattr(machine, "attached_machine", None)
                    if profile == "attach" else
                    getattr(machine, "machine", None))
    if machine_name is None:
        machine_name = _profile_machine_name(profile)
    platform_value = getattr(machine, "platform", None)
    metadata = {
        "requested_profile": requested,
        "resolved_profile": profile,
        "machine": _json_scalar(machine_name),
        "config_mode": _json_scalar(config_mode),
        "config_home": _json_scalar(
            None if profile == "attach" else getattr(machine, "home", None)),
        "effective_config_home": _json_scalar(
            None if profile == "attach" else
            getattr(machine, "effective_home", None)),
        "user_config_home": _json_scalar(
            None if profile == "attach" else
            getattr(machine, "user_home", None)),
        "control_transport": _json_scalar(
            getattr(machine, "control_transport",
                    getattr(machine, "transport", None))),
        "executable": _json_scalar(
            None if profile == "attach" else getattr(machine, "bin", None)),
        "platform": (_platform_label(platform_value)
                     if platform_value is not None else None),
    }
    return {name: value for name, value in metadata.items()
            if value is not None}


def _status_for(target):
    m, profile = SESSION.backend(target)
    if m is None:
        return {"target": target, "backend": (
            "openmsx" if target == "local" else "agent"),
            "channel": ("openmsx-control" if target == "local"
                        else "agent-protocol"),
            "target_id": None, "bench_id": SESSION.bench_id,
            "state": "disconnected"}
    if target == "agent":
        state = m.status()
        state.update({
            "target": "agent",
            "backend": "agent",
            "channel": "agent-protocol",
            "target_id": SESSION.agent_id,
            "bench_id": SESSION.bench_id,
            # Socket endpoints are tuples in Python but arrays on the MCP JSON
            # boundary. Normalize them before output-schema validation.
            "peer": list(m.peer) if isinstance(m.peer, tuple) else m.peer,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if m.capabilities & bit],
            "resident_base": m.resident_base,
            "resident_entry": getattr(m, "resident_entry", None),
            # Keep the legacy field while making the independent link and
            # MSX-side hardware layers explicit for new clients.
            "transport": getattr(m, "agent_transport", None),
            "agent_transport": getattr(m, "agent_transport", None),
            "agent_transport_id": getattr(m, "agent_transport_id", None),
            "network_transport": getattr(m, "network_transport", None),
            "network_role": getattr(m, "network_role", None),
            "local_endpoint": (
                list(m.local_endpoint)
                if isinstance(getattr(m, "local_endpoint", None), tuple)
                else getattr(m, "local_endpoint", None)),
            "simulation": getattr(m, "simulation", None),
            "max_payload": (m._v3.max_payload if m._v3 is not None else 255),
            "control_level": getattr(m, "control_level", None),
            "debug": getattr(m, "debug", None),
            "runtime_mode": getattr(m, "runtime_mode", None),
            "runtime_mode_id": getattr(m, "runtime_mode_id", None),
            "features": [name for bit, name in AGENT_FEATURE_NAMES.items()
                         if getattr(m, "feature_bits", 0) & bit],
            "feature_bits": getattr(m, "feature_bits", 0),
            "vdp_generation": getattr(m, "vdp_generation", None),
            "vram_size": m.vram_size,
            "vram_banks": getattr(m, "vram_banks", None),
        })
        return state
    status = {"target": "local", "backend": "openmsx",
              "channel": "openmsx-control",
              "target_id": SESSION.local_id,
              "bench_id": SESSION.bench_id,
              "state": "connected", "profile": profile,
              "screen_mode": m.screen_mode()}
    status.update(_local_runtime_metadata(m, profile))
    if getattr(m, "attached", False):
        status["control_socket"] = getattr(m, "socket_path", None)
    return status


def _identity_for(target):
    """Describe one channel without issuing target I/O."""
    m, profile = SESSION.backend(target)
    connected = m is not None
    identity = {
        "target": target,
        "backend": "openmsx" if target == "local" else "agent",
        "channel": "openmsx-control" if target == "local"
        else "agent-protocol",
        "target_id": (SESSION.local_id if target == "local"
                      else SESSION.agent_id),
        "bench_id": SESSION.bench_id,
        "state": "connected" if connected else "disconnected",
    }
    if connected and target == "local":
        identity["profile"] = profile
        identity.update(_local_runtime_metadata(m, profile))
    if connected and target == "agent":
        identity.update({
            "peer": (list(m.peer) if isinstance(getattr(m, "peer", None), tuple)
                     else getattr(m, "peer", None)),
            "local_endpoint": (
                list(m.local_endpoint)
                if isinstance(getattr(m, "local_endpoint", None), tuple)
                else getattr(m, "local_endpoint", None)),
            "runtime_mode": getattr(m, "runtime_mode", None),
            "agent_transport": getattr(m, "agent_transport", None),
        })
    return identity


def t_status():
    target = _TOOL_TARGET.get()
    if target is not None:
        return _status_for(target)
    targets = SESSION.connected_targets()
    if SESSION.bench_machine is not None:
        local_connected = SESSION.backend("local")[0] is not None
        agent_connected = SESSION.backend("agent")[0] is not None
        return {
            "backend": "hybrid-bench",
            "state": ("connected" if local_connected and agent_connected
                      else "degraded"),
            "bench_id": SESSION.bench_id,
            "targets": {
                "local": _identity_for("local"),
                "agent": _identity_for("agent"),
            },
        }
    if not targets:
        return {"backend": "none", "state": "disconnected"}
    if len(targets) == 1:
        return _identity_for(targets[0])
    return {
        "backend": "multiple",
        "state": "connected",
        "bench_id": SESSION.bench_id,
        "targets": {target: _identity_for(target) for target in targets},
    }


def t_tcp_bench_status():
    if SESSION.bench_machine is None:
        return {"backend": "hybrid-bench", "bench_id": None,
                "state": "disconnected", "targets": {}}
    local_connected = SESSION.backend("local")[0] is not None
    agent_connected = SESSION.backend("agent")[0] is not None
    return {
        "backend": "hybrid-bench",
        "bench_id": SESSION.bench_id,
        "state": "connected" if local_connected and agent_connected
        else "degraded",
        "targets": {
            "local": (_status_for("local") if local_connected
                      else _identity_for("local")),
            "agent": (_status_for("agent") if agent_connected
                      else _identity_for("agent")),
        },
    }


def t_cpu_snapshot():
    """Capture CPU debug state through the fixed public tool route."""
    backend, _profile, target = SESSION._resolve()
    snapshot = dict(backend.cpu_snapshot())
    snapshot["backend"] = "agent" if target == "agent" else "openmsx"
    return snapshot


def _require_real():
    if SESSION.profile != "real":
        raise OpenMSXError(
            "this operation requires a connected ASM-agent session")
    return SESSION.require()


def t_pause():
    return f"[real MSX { _require_real().pause() }]"


def t_resume():
    return f"[real MSX { _require_real().resume() }]"


def t_stop():
    return f"[real MSX { _require_real().stop() }]"


def _int_in_range(value, name, minimum, maximum):
    """Validate an integer MCP argument without accepting JSON booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in range {minimum}..{maximum}")
    return value


def t_io_read(port):
    """Read one hardware I/O port through the resident agent."""
    port = _int_in_range(port, "port", 0, 0xFF)
    value = _require_real().io_read(port)
    return {"port": port, "value": value}


def t_io_write(port, value, verify=False):
    """Write one hardware I/O port through the resident agent."""
    port = _int_in_range(port, "port", 0, 0xFF)
    value = _int_in_range(value, "value", 0, 0xFF)
    if not isinstance(verify, bool):
        raise TypeError("verify must be a boolean")
    _require_real().io_write(port, value, verify=verify)
    return {"port": port, "value": value, "verified": verify}


def t_slot_select(page, slot_id):
    """Map a slot into page 0 or 1 in foreground-monitor mode."""
    page = _int_in_range(page, "page", 0, 1)
    slot_id = _int_in_range(slot_id, "slot_id", 0, 0xFF)
    _require_real().slot_select(page, slot_id)
    return {"page": page, "slot_id": slot_id}


def t_mapper_select(page, segment):
    """Map a segment into page 0 or 1 in foreground-monitor mode."""
    page = _int_in_range(page, "page", 0, 1)
    segment = _int_in_range(segment, "segment", 0, 0xFF)
    _require_real().mapper_select(page, segment)
    return {"page": page, "segment": segment}


def _atomic_real(m, atomic, operation):
    with m.snapshot_lease(atomic=atomic):
        return operation()


def t_memory_read(space, address, length, atomic=True):
    m = SESSION.require()
    real = SESSION.profile == "real"
    backend = m if real else _OpenMSXApplicationBackend(m)
    if space == "ram":
        read = lambda: backend.peek(int(address), int(length))
    elif space == "vram":
        read = lambda: backend.vpeek(int(address), int(length))
    else:
        raise ValueError("space must be 'ram' or 'vram'")
    data = _atomic_real(m, atomic, read) if real else read()
    return f"[{space} 0x{int(address):X}+{len(data)}]\n{data.hex()}"


def t_memory_write(space, address, data_hex, verify=False, atomic=True):
    m = SESSION.require()
    real = SESSION.profile == "real"
    backend = m if real else _OpenMSXApplicationBackend(m)
    try:
        data = bytes.fromhex(data_hex)
    except ValueError as exc:
        raise ValueError("data_hex must contain an even number of hexadecimal digits") from exc
    address = int(address)
    if space == "ram":
        write = lambda: (backend.poke(address, data),
                         backend.peek(address, len(data)) if verify else None)[1]
    elif space == "vram":
        write = lambda: (backend.vpoke(address, data),
                         backend.vpeek(address, len(data)) if verify else None)[1]
    else:
        raise ValueError("space must be 'ram' or 'vram'")
    check = _atomic_real(m, atomic, write) if real else write()
    if check is not None and check != data:
        raise OpenMSXError("write verification failed")
    suffix = " verified" if verify else ""
    return f"[{len(data)} bytes written to {space} 0x{address:X}{suffix}]"


class _OpenMSXApplicationBackend:
    """Duck-typed loader backend implemented only with openMSX debugger APIs.

    Format parsing remains in :mod:`msx_application`; this adapter translates
    its neutral RAM/VRAM/execution contract into control-channel operations.
    Binary blocks are transferred as hex in relatively large chunks, avoiding
    the per-byte Tcl loop used by the legacy assembly helper.
    """

    BLOCK_SIZE = 0x4000

    def __init__(self, msx):
        self.msx = msx

    @staticmethod
    def _range(address, length, limit, space):
        if (isinstance(address, bool) or not isinstance(address, int) or
                isinstance(length, bool) or not isinstance(length, int)):
            raise TypeError("address and length must be integers")
        if address < 0 or length < 0 or address + length > limit:
            raise ValueError(
                f"{space} range 0x{address:X}+{length} exceeds 0x{limit - 1:X}")

    def _write(self, device, address, data, limit):
        data = bytes(data)
        self._range(address, len(data), limit, device)
        for offset in range(0, len(data), self.BLOCK_SIZE):
            block = data[offset:offset + self.BLOCK_SIZE]
            self.msx.cmd(
                f'set d [binary format H* {{{block.hex()}}}]; '
                f'debug write_block {device} {address + offset} $d')
        return len(data)

    def _read(self, device, address, length, limit):
        self._range(address, length, limit, device)
        data = bytearray()
        for offset in range(0, length, self.BLOCK_SIZE):
            size = min(self.BLOCK_SIZE, length - offset)
            encoded = self.msx.cmd(
                f'set d [debug read_block {device} {address + offset} {size}]; '
                'binary scan $d H* h; set h')
            data.extend(bytes.fromhex(encoded.strip()))
        return bytes(data)

    def poke(self, address, data):
        return self._write("memory", address, data, 0x10000)

    def peek(self, address, length):
        return self._read("memory", address, length, 0x10000)

    def vpoke(self, address, data):
        return self._write("VRAM", address, data, 0x20000)

    def vpeek(self, address, length):
        return self._read("VRAM", address, length, 0x20000)

    def call(self, address):
        self._range(address, 1, 0x10000, "entry")
        self.msx.type_line(f"DEFUSR0={address}")
        self.msx.advance(0.4)
        self.msx.type_line("A=USR0(0)")
        self.msx.advance(1)

    def run(self, address):
        self._range(address, 1, 0x10000, "entry")
        self.msx.cmd("debug break")
        self.msx.cmd(
            f'debug write "CPU regs" 20 {(address >> 8) & 0xFF}; '
            f'debug write "CPU regs" 21 {address & 0xFF}; debug cont')


def _application_entry(application, execute):
    """Return the effective entry without touching the selected target."""
    mode = application.entry.mode if execute is None else execute
    address = None if mode == "none" else application.entry.address
    if mode != "none" and address is None:
        raise ValueError(
            f"{application.source_format} has no entry address; use "
            "execute='none' or provide an artifact with an entry point")
    return mode, address


def _validate_basic_bload(application, mode, address):
    """Fail before target I/O when a BLOAD cannot use the BASIC trampoline."""
    if application.source_format != "bload":
        raise ValueError(
            "environment='basic' is supported only for a BLOAD file with "
            "the seven-byte FE header")
    if len(application.segments) != 1 or application.segments[0].space != "ram":
        raise ValueError("BLOAD BASIC execution requires one RAM segment")
    segment = application.segments[0]
    if segment.address < 0x8000:
        raise OpenMSXError(
            "BLOAD BASIC execution requires its complete payload in writable "
            "CPU pages 2 or 3 (0x8000-0xFFFF); page 0 is Main-ROM and page 1 "
            "is occupied while the resident MSXAI agent services requests. "
            "No target state was changed")
    if mode != "none" and not segment.address <= address < segment.end:
        raise ValueError(
            f"BLOAD entry 0x{address:04X} is outside its loaded RAM range "
            f"0x{segment.address:04X}-0x{segment.end - 1:04X}")


def _wait_for_basic_prompt(
        m, *, attempts=BASIC_PROMPT_ATTEMPTS,
        screen_timeout=BASIC_PROMPT_SCREEN_TIMEOUT,
        settle=BASIC_PROMPT_SETTLE_SECONDS):
    """Wait for BASIC without starting a capture on a depleted deadline."""
    for _attempt in range(attempts):
        if settle:
            time.sleep(settle)
        screen = m.screen_text(timeout=screen_timeout)
        if _basic_prompt_visible(screen):
            return screen
    return None


def _preflight_agent_direct(m, application, mode, address):
    """Validate direct-agent loading before STOP or the first target write."""
    runtime = getattr(m, "runtime_mode", None)
    capabilities = getattr(m, "capabilities", None)
    if mode != "none" and (
            runtime == "resident" or
            (capabilities is not None and not capabilities & CAPABILITY_RUN)):
        raise OpenMSXError(
            f"direct execute='{mode}' requires the foreground monitor; the "
            "resident agent can load safe RAM/VRAM data only with "
            "execute='none'. No target state was changed")

    resident = runtime == "resident"
    protected_base = getattr(m, "resident_base", None)
    for segment in application.segments:
        if segment.space != "ram" or not segment.data:
            continue
        if resident and segment.address < 0x8000 and segment.end > 0x4000:
            raise OpenMSXError(
                "application overlaps CPU page 1 (0x4000-0x7FFF), which "
                "contains the resident MSXAI agent; no target state was "
                "changed")
        if (not resident and protected_base is not None and
                segment.end > protected_base):
            raise OpenMSXError(
                "application overlaps the protected foreground monitor area "
                f"at 0x{protected_base:04X}+; no target state was changed")

    if mode == "none" or address is None:
        return
    if resident and 0x4000 <= address < 0x8000:
        raise OpenMSXError(
            "entry point is inside CPU page 1 (0x4000-0x7FFF), which "
            "contains the resident MSXAI agent; no target state was changed")
    if (not resident and protected_base is not None and
            address >= protected_base):
        raise OpenMSXError(
            "entry point is inside the protected foreground monitor area at "
            f"0x{protected_base:04X}+; no target state was changed")


def _load_agent_bload_in_basic(m, application, execute, *, automatic):
    """Enter BASIC, load an FE-header BLOAD verbatim, and submit it via USR."""
    mode, address = _application_entry(application, execute)
    _validate_basic_bload(application, mode, address)
    if getattr(m, "runtime_mode", None) != "resident":
        raise OpenMSXError(
            "automatic BLOAD/BASIC execution requires the resident agent; "
            "restart MSXAI without /MONITOR. Foreground mode cannot enter "
            "BASIC without terminating its TCP session")

    screen = m.screen_text(timeout=10.0)
    if _basic_prompt_visible(screen):
        transition = "already-basic"
    elif _dos_prompt_visible(screen):
        m.type_line("BASIC")
        screen = _wait_for_basic_prompt(m)
        if screen is None:
            raise OpenMSXError(
                "MSX accepted the BASIC command but no BASIC Ok prompt was "
                "observed after three bounded screen probes; BLOAD RAM was "
                "not written")
        transition = "dos-to-basic"
    else:
        raise OpenMSXError(
            "automatic BLOAD loading requires a visible MSX-DOS prompt or "
            "MSX BASIC Ok prompt; target RAM was not written")

    # BASIC owns the correct Main-ROM page-0 environment. Load without asking
    # the resident protocol to CALL/RUN: resident execution is deliberately
    # submitted through BASIC's documented USR trampoline instead.
    result = dict(load_application(
        m, application, execute="none", verify=True,
        stop_before_load=False))
    result["entry"] = {"mode": mode, "address": address}
    capabilities = [
        item for item in result["required_capabilities"]
        if not item.startswith("execute:")
    ]
    if mode != "none":
        capabilities.append("input:basic-usr")
        m.type_line(f"DEFUSR0={address}:A=USR0(0)")
        if mode == "call" and _wait_for_basic_prompt(m) is None:
            raise OpenMSXError(
                "BASIC USR call did not return to an Ok prompt after three "
                "bounded screen probes; the injected routine may still be "
                "running")
    result["required_capabilities"] = list(dict.fromkeys(capabilities))
    result.update({
        "execution_environment": "msx-basic",
        "environment_auto_selected": bool(automatic),
        "target_transition": transition,
        "execution_submission": "basic-usr" if mode != "none" else "none",
        "screen_probe_performed": True,
    })
    return result


def t_app_load(path, format=None, execute=None, verify=False,
               environment="auto"):
    """Load a manifest, COM, BLOAD BIN or flat ROM through the fixed route."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("path must be a non-empty filesystem path")
    if format is not None and not isinstance(format, str):
        raise TypeError("format must be a string")
    if execute is not None:
        if not isinstance(execute, str):
            raise TypeError("execute must be a string")
        execute = execute.lower()
        if execute not in ("none", "call", "run"):
            raise ValueError("execute must be 'none', 'call' or 'run'")
    if not isinstance(verify, bool):
        raise TypeError("verify must be a boolean")
    if not isinstance(environment, str) or environment not in APPLICATION_ENVIRONMENTS:
        allowed = ", ".join(repr(item) for item in APPLICATION_ENVIRONMENTS)
        raise ValueError(f"environment must be one of {allowed}")

    source = resolve_user_path(path)
    msx = SESSION.require()
    real = SESSION.profile == "real"
    backend = msx if real else _OpenMSXApplicationBackend(msx)
    application = parse_application(source, format=format)
    selected_environment = (
        "basic" if real and environment == "auto" and
        application.source_format == "bload" else
        "direct" if environment == "auto" else environment)
    if selected_environment == "basic":
        if not real:
            raise ValueError(
                "environment='basic' is available only through the resident "
                "ASM-agent route")
        result = _load_agent_bload_in_basic(
            msx, application, execute, automatic=environment == "auto")
    else:
        mode, address = _application_entry(application, execute)
        if real:
            _preflight_agent_direct(msx, application, mode, address)
        result = dict(load_application(
            backend, application, execute=execute, verify=verify,
            stop_before_load=(
                real and getattr(msx, "runtime_mode", None) != "resident")))
        result.update({
            "execution_environment": "direct",
            "environment_auto_selected": environment == "auto",
            "target_transition": "none",
            "execution_submission": (
                "none" if mode == "none" else
                ("agent-" if real else "openmsx-") + mode),
            "screen_probe_performed": False,
        })
    result["backend"] = "agent" if real else "openmsx"
    return result


def _real_screenshot_estimate(m, plan):
    """Return target bytes, estimated wire bytes and seconds for one capture."""
    framed = m._v3 is not None
    max_payload = m._v3.max_payload if framed else 255
    metadata_sizes = [1, 8, 16, 3]
    if plan.mode == 0:
        metadata_sizes.append(1)
    # A framed RAM read uses a 17-byte request and a 13-byte response header;
    # raw v2 uses a four-byte command header and an unframed response.
    wire_bytes = sum(
        size + (30 if framed else 4) for size in metadata_sizes)
    read_count = len(metadata_sizes)
    for base, size in plan.ranges:
        offset = 0
        while offset < size:
            address = base + offset
            bank_remaining = 0x4000 - (address & 0x3FFF)
            chunk = min(size - offset, max_payload, bank_remaining)
            # v3 request/response framing contributes 31 bytes around every
            # VRAM payload; the raw v2 VRAM read header is five bytes.
            wire_bytes += chunk + (31 if framed else 5)
            read_count += 1
            offset += chunk
    seconds = (wire_bytes * UART_BITS_PER_BYTE / UART8251_BAUD
               * UART_SCREENSHOT_MARGIN)
    return {
        "target_bytes": plan.target_bytes,
        "wire_bytes": wire_bytes,
        "read_requests": read_count,
        "seconds": seconds,
    }


def _guard_slow_real_screenshot(m, plan, allow_slow):
    estimate = _real_screenshot_estimate(m, plan)
    is_8251 = (getattr(m, "agent_transport_id", None) == 0 or
               getattr(m, "agent_transport", None) == "uart-8251")
    if (is_8251 and estimate["seconds"] > SLOW_SCREENSHOT_SECONDS and
            not allow_slow):
        raise OpenMSXError(
            "slow 8251 screenshot refused before bulk VRAM acquisition: "
            f"SCREEN {plan.mode} needs approximately "
            f"{estimate['target_bytes']} target bytes, "
            f"{estimate['read_requests']} reads and "
            f"{estimate['seconds']:.1f} seconds at {UART8251_BAUD} baud. "
            "Retry with allow_slow=true to opt in.")
    return estimate


def t_screenshot(atomic=True, page=None, sprites=True, palette=None,
                 allow_slow=False):
    """Render a supported MSX screen mode to a PNG image content block."""
    if (not isinstance(atomic, bool) or not isinstance(sprites, bool) or
            not isinstance(allow_slow, bool)):
        raise TypeError("atomic, sprites and allow_slow must be booleans")
    if page is not None:
        page = _int_in_range(page, "page", 0, 3)
    if palette is not None:
        if not isinstance(palette, list) or len(palette) != 16:
            raise ValueError("palette must contain 16 RGB entries")
        palette = [tuple(_int_in_range(component, "palette component", 0, 255)
                         for component in entry)
                   for entry in palette]
        if any(len(entry) != 3 for entry in palette):
            raise ValueError("each palette entry must contain R, G and B")
    m = SESSION.require()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        if SESSION.profile == "real":
            # Enter the bounded lease before the first metadata read. This
            # feature-gates old vulnerable agents without touching their live
            # hook and prevents a mode transition from mixing layouts.
            with m.snapshot_lease(atomic=atomic):
                plan = msx_screenshot.plan_realmsx_capture(
                    m, sprites=sprites, page=page)
                _guard_slow_real_screenshot(m, plan, allow_slow)
                capture = msx_screenshot.acquire_realmsx_capture(
                    m, sprites=sprites, page=page, plan=plan)
            # The lease is released immediately after the last target byte.
            # Rendering, PNG compression and Base64 encoding are host-only.
            _, mode = msx_screenshot.render_realmsx_capture(
                capture, path, palette=palette)
            source = "ASM agent/TCP"
        else:
            _, mode = msx_screenshot.capture_openmsx(
                m, path, palette=palette, sprites=sprites, page=page)
            source = "openMSX control"
        with open(path, "rb") as image_file:
            data = base64.b64encode(image_file.read()).decode()
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    return [{"type": "text", "text":
             f"[screenshot — SCREEN mode {mode} via {source}]"},
            {"type": "image", "data": data, "mimeType": "image/png"}]


def t_type_line(text):
    m = SESSION.require()
    consumed = m.type_line(text)
    if SESSION.profile == "real":
        return {
            "backend": "agent",
            "bytes_consumed": consumed,
            "input": "line",
            "screen_capture_performed": False,
        }
    m.advance(0.6)
    return _screen()


def t_type_lines(lines):
    if not isinstance(lines, list):
        raise TypeError("lines must be an array of strings")
    if any(not isinstance(line, str) for line in lines):
        raise TypeError("each line must be a string")
    m = SESSION.require()
    consumed = m.type_lines(lines)
    if SESSION.profile == "real":
        return {
            "backend": "agent",
            "bytes_consumed": consumed,
            "input": "lines",
            "lines": len(lines),
            "screen_capture_performed": False,
        }
    return _screen()


def t_type(text):
    m = SESSION.require()
    consumed = m.type(text)
    if SESSION.profile == "real":
        return {
            "backend": "agent",
            "bytes_consumed": consumed,
            "input": "text",
            "screen_capture_performed": False,
        }
    m.advance(0.4)
    return _screen()


def t_key(key):
    m = SESSION.require()
    if SESSION.profile == "real":
        m.press(key)
        time.sleep(0.1)
        return {
            "backend": "agent",
            "input": "key",
            "key": key,
            "screen_capture_performed": False,
        }
    else:
        m.press(key.upper())
        m.advance(0.6)
    return _screen()


def _basic_source_lines(program):
    return [line.rstrip() for line in program.splitlines() if line.rstrip()]


def _encode_basic_ascii(program):
    lines = _basic_source_lines(program)
    if not lines:
        return b"\x1a"
    try:
        listing = ("\r\n".join(lines) + "\r\n").encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("MSX BASIC ASCII files support ASCII text only") from exc
    return listing + b"\x1a"


def _normalize_dos_drive(drive):
    if not isinstance(drive, str) or not re.fullmatch(r"[A-Za-z]", drive):
        raise ValueError("drive must be one DOS drive letter")
    return drive.upper()


def _temporary_basic_filename(drive):
    drive = _normalize_dos_drive(drive)
    return drive + ":MX" + secrets.token_hex(3).upper() + ".BAS"


def _run_real_basic_file(m, data, drive):
    data = bytes(data)
    if not 1 <= len(data) <= REAL_BASIC_FILE_LIMIT:
        raise ValueError(
            f"BASIC file must contain 1..{REAL_BASIC_FILE_LIMIT} bytes")
    filename = _temporary_basic_filename(drive)
    # BASIC payloads are small, short-lived staging files. Protocol X still
    # provides framed retries and end-to-end CRC-32, while a private journal
    # directory avoids leaving resume state bound to a deleted temporary file.
    with tempfile.TemporaryDirectory(prefix="msx-ai-basic-xfer-") as staging:
        source = pathlib.Path(staging) / "PROGRAM.BAS"
        source.write_bytes(data)
        options = {
            "compression": "raw",
            "resume": False,
            "state_directory": staging,
        }
        progress = current_progress_callback()
        cancelled = current_cancellation_callback()
        if progress is not None:
            options["progress"] = progress
        if cancelled is not None:
            options["cancelled"] = cancelled
        transfer = m.put_file(source, filename, **options)
    # Protocol-X COMPLETE is emitted only after the transient worker has
    # finished its DOS cleanup and final console message. The BASIC command can
    # therefore be queued immediately; COMMAND2 consumes it after the helper's
    # final termination instruction. No implicit VRAM read is needed here.
    m.type_line("BASIC")
    m.type_line(f'LOAD"{filename}"')
    # KILL and RUN share one direct statement: the temporary file is removed
    # before execution without risking a second line being lost as type-ahead.
    m.type_line(f'KILL"{filename}":RUN')
    return {
        "backend": "agent",
        "bytes_transferred": len(data),
        "delivery": "file-transfer-v2",
        "operation": "run-basic",
        "run_submitted": True,
        "screen_capture_performed": False,
        "transfer_id": transfer.get("transfer_id"),
    }


def _read_basic_file(path, format="auto"):
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("path must be a non-empty filesystem path")
    if format not in ("auto", "ascii", "tokenized"):
        raise ValueError("format must be 'auto', 'ascii' or 'tokenized'")
    source = resolve_user_path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise OpenMSXError(f"could not read BASIC file {source}: {exc}") from exc
    if format == "auto":
        selected = "tokenized" if data.startswith(b"\xff") else "ascii"
    else:
        selected = format
    if selected == "tokenized":
        if not data.startswith(b"\xff"):
            raise ValueError("tokenized MSX BASIC files must start with 0xFF")
        payload = data
    else:
        # MSX BASIC source is an 8-bit DOS text format, not UTF-8 or strict
        # 7-bit ASCII.  Listings commonly embed bytes above 0x7F for graphical
        # characters.  Preserve those bytes while canonicalizing host line
        # endings and the DOS text EOF marker.
        try:
            payload = normalize_msx_basic_text(data)
        except TransferError as exc:
            raise ValueError(f"invalid ASCII MSX BASIC file: {exc}") from exc
    if not 1 <= len(payload) <= REAL_BASIC_FILE_LIMIT:
        raise ValueError(
            f"BASIC file must contain 1..{REAL_BASIC_FILE_LIMIT} bytes")
    return payload, selected, source


def t_run_basic_file(path, dos_prompt_confirmed, format="auto", drive="A"):
    if not isinstance(dos_prompt_confirmed, bool):
        raise TypeError("dos_prompt_confirmed must be a boolean")
    if not dos_prompt_confirmed:
        raise OpenMSXError(
            "msx_agent_run_basic_file requires dos_prompt_confirmed=true; confirm "
            "the MSX-DOS prompt externally before starting the worker")
    if SESSION.profile != "real":
        raise OpenMSXError(
            "direct BASIC-file transfer currently requires a real-agent "
            "session at an MSX-DOS prompt")
    data, _selected, _source = _read_basic_file(path, format=format)
    m = SESSION.require()
    if not m.feature_bits & FEATURE_FILE_TRANSFER:
        raise OpenMSXError(
            "the connected resident does not advertise file-transfer-v2; "
            "reinstall the current agent suite")
    return _run_real_basic_file(m, data, drive)


def _resolve_host_transfer_path(path, *, must_exist):
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("path must be a non-empty filesystem path")
    resolved = resolve_user_path(path).resolve(strict=must_exist)
    if must_exist and not resolved.is_file():
        raise ValueError(f"local transfer source is not a regular file: {resolved}")
    return resolved


def _require_file_transfer_backend(operation):
    m = SESSION.require()
    method = getattr(m, operation, None)
    if not callable(method):
        raise OpenMSXError(
            "the connected ASM-agent backend does not implement resumable "
            "DOS file transfer")
    if (SESSION.profile == "real" and
            not m.feature_bits & FEATURE_FILE_TRANSFER):
        raise OpenMSXError(
            "the connected resident does not advertise file-transfer-v2; "
            "install the current MSXAI.COM")
    return m, method


def _finish_file_transfer_tool(result):
    """Describe protocol completion without an implicit resident VRAM read."""
    result = dict(result)
    result["completion"] = "protocol-x-terminal-verified"
    result["prompt_check"] = "not-performed"
    result["screen_capture_performed"] = False
    return result


def t_file_put(local_path, msx_path, dos_prompt_confirmed,
               compression="auto", resume=True, timeout=600.0):
    """Upload one arbitrary binary file through the connected ASM agent."""
    if compression not in ("auto", "raw", "packbits"):
        raise ValueError("compression must be 'auto', 'raw', or 'packbits'")
    if not isinstance(resume, bool):
        raise TypeError("resume must be a boolean")
    if not isinstance(dos_prompt_confirmed, bool):
        raise TypeError("dos_prompt_confirmed must be a boolean")
    if not dos_prompt_confirmed and not resume:
        raise OpenMSXError(
            "starting a PUT requires dos_prompt_confirmed=true; false is "
            "valid only for active resume recovery")
    source = _resolve_host_transfer_path(local_path, must_exist=True)
    _m, method = _require_file_transfer_backend("put_file")
    options = {
        "compression": compression,
        "resume": resume,
        "existing_only": not dos_prompt_confirmed,
        "timeout": timeout,
    }
    progress = current_progress_callback()
    cancelled = current_cancellation_callback()
    if progress is not None:
        options["progress"] = progress
    if cancelled is not None:
        options["cancelled"] = cancelled
    result = method(source, msx_path, **options)
    return _finish_file_transfer_tool(result)


def t_file_get(msx_path, local_path, dos_prompt_confirmed,
               resume=True, timeout=600.0):
    """Download one arbitrary binary file through the connected ASM agent."""
    if not isinstance(resume, bool):
        raise TypeError("resume must be a boolean")
    if not isinstance(dos_prompt_confirmed, bool):
        raise TypeError("dos_prompt_confirmed must be a boolean")
    if not dos_prompt_confirmed and not resume:
        raise OpenMSXError(
            "starting a GET requires dos_prompt_confirmed=true; false is "
            "valid only for active resume recovery")
    destination = _resolve_host_transfer_path(local_path, must_exist=False)
    _m, method = _require_file_transfer_backend("get_file")
    options = {
        "resume": resume,
        "existing_only": not dos_prompt_confirmed,
        "timeout": timeout,
    }
    progress = current_progress_callback()
    cancelled = current_cancellation_callback()
    if progress is not None:
        options["progress"] = progress
    if cancelled is not None:
        options["cancelled"] = cancelled
    result = method(msx_path, destination, **options)
    return _finish_file_transfer_tool(result)


def t_docs_search(query, backend=None, audience=None, limit=5):
    """Search the bundled, project-authored documentation corpus."""
    return msx_docs.search(
        query, backend=backend, audience=audience, limit=limit)


def t_run_basic(program, clear=True, allow_existing_basic=False,
                transfer="auto", dos_prompt_confirmed=False, dos_drive="A"):
    if not isinstance(program, str):
        raise TypeError("program must be a string")
    if not isinstance(clear, bool):
        raise TypeError("clear must be a boolean")
    if not isinstance(allow_existing_basic, bool):
        raise TypeError("allow_existing_basic must be a boolean")
    if not isinstance(dos_prompt_confirmed, bool):
        raise TypeError("dos_prompt_confirmed must be a boolean")
    if not isinstance(transfer, str) or transfer not in ("auto", "type", "file"):
        raise ValueError("transfer must be 'auto', 'type' or 'file'")
    dos_drive = _normalize_dos_drive(dos_drive)
    m = SESSION.require()
    real = SESSION.profile == "real"
    if real:
        if dos_prompt_confirmed and allow_existing_basic:
            raise ValueError(
                "dos_prompt_confirmed and allow_existing_basic are mutually "
                "exclusive target-state confirmations")
        if not dos_prompt_confirmed and not allow_existing_basic:
            raise OpenMSXError(
                "confirm the target state explicitly: set "
                "dos_prompt_confirmed=true at MSX-DOS, or set "
                "allow_existing_basic=true at an MSX BASIC Ok prompt")
        file_data = _encode_basic_ascii(program)
        file_supported = bool(m.feature_bits & FEATURE_FILE_TRANSFER)
        if transfer == "file" and not file_supported:
            raise OpenMSXError(
                "transfer='file' requires a resident that advertises "
                "file-transfer-v2; reinstall the current agent suite")
        use_file = (transfer == "file" or
                    transfer == "auto" and clear and
                    file_supported and
                    REAL_BASIC_FILE_THRESHOLD <= len(file_data) <=
                    REAL_BASIC_FILE_LIMIT and
                    dos_prompt_confirmed)
        if use_file:
            if not clear:
                raise ValueError(
                    "file transfer replaces the BASIC program and requires "
                    "clear=true")
            if not dos_prompt_confirmed:
                raise OpenMSXError(
                    "BASIC file transfer requires dos_prompt_confirmed=true")
            return _run_real_basic_file(m, file_data, dos_drive)
        if dos_prompt_confirmed:
            m.type_line("BASIC")
    elif transfer == "file":
        raise OpenMSXError(
            "file transfer requires a real-agent session at an MSX-DOS prompt")
    if clear:
        m.type_line("NEW")
        if not real:
            m.advance(0.4)
    lines = _basic_source_lines(program)
    consumed = m.type_lines(lines + ["RUN"])
    if real:
        return {
            "backend": "agent",
            "bytes_consumed": consumed,
            "delivery": "keyboard-spool",
            "lines": len(lines),
            "operation": "run-basic",
            "run_submitted": True,
            "screen_capture_performed": False,
        }
    m.advance(2.5)
    return _screen()


def t_reset():
    if SESSION.profile == "real":
        raise OpenMSXError("physical reset is not implemented by the resident agent")
    if SESSION.bench_machine is SESSION.require():
        raise OpenMSXError(
            "local reset is refused while the paired TCP bench is active; "
            "use msx_tcp_bench_shutdown and start a new bench")
    m = SESSION.require()
    m.cmd("reset")
    m.advance(6)
    return _screen()


def t_cmd(tcl):
    """Escape hatch: run a raw openMSX Tcl console command (peek/poke, debug…)."""
    if SESSION.profile == "real":
        raise OpenMSXError("raw Tcl commands are only available on openMSX")
    if SESSION.bench_machine is SESSION.require():
        raise OpenMSXError(
            "raw Tcl is refused for a paired TCP bench because it could alter "
            "or close the shared machine; use typed msx_local_* diagnostics")
    return SESSION.require().cmd(tcl)


def t_asm_load(source, address=0x8000, run=False, execute=None):
    """Assemble Z80 source (z80asm), load the bytes into MSX RAM at `address`.

    ``execute=call`` invokes a returning routine. ``execute=run`` launches an
    asynchronous program. The legacy ``run=true`` is an alias for the latter.
    """
    m = SESSION.require()
    addr = int(address)
    action = execute or ("run" if run else "none")
    if action not in ("none", "call", "run"):
        raise ValueError("execute must be 'none', 'call' or 'run'")
    if SESSION.profile == "real":
        if action != "none" and (
                getattr(m, "runtime_mode", None) == "resident" or
                (hasattr(m, "capabilities") and
                 not m.capabilities & CAPABILITY_RUN)):
            raise OpenMSXError(
                f"direct ASM execute='{action}' requires the foreground "
                "monitor; the resident agent accepts safe RAM injection only "
                "with execute='none'. No bytes were written")
        return m.asm_load(source, address=addr, execute=action)
    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "a.asm"
        out = pathlib.Path(td) / "a.bin"
        # z80asm needs an org; prepend if the source lacks one
        text = source
        if "org" not in source.lower():
            text = f"    org 0{addr:04X}h\n" + source
        src.write_text(text)
        r = subprocess.run([Z80ASM, str(src), "-o", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OpenMSXError("z80asm error:\n" + (r.stderr or r.stdout))
        data = out.read_bytes()
    # write the bytes into CPU-visible RAM through the debugger
    m.cmd(f'foreach {{a b}} {{{_pairs(addr, data)}}} {{debug write memory $a $b}}')
    summary = f"[asm] assembled {len(data)} bytes -> loaded at 0x{addr:04X}"
    if action == "call":
        # Call returning routines from BASIC through the normal USR trampoline.
        m.type_line(f"DEFUSR0={addr}")
        m.advance(0.4)
        m.type_line("A=USR0(0)")
        m.advance(1)
        summary += " (called via USR0)"
    elif action == "run":
        # Set PC while the emulated CPU is stopped, then immediately continue.
        # This mirrors the resident agent's asynchronous RUN operation.
        m.cmd("debug break")
        m.cmd(f'debug write "CPU regs" 20 {(addr >> 8) & 0xFF}; '
              f'debug write "CPU regs" 21 {addr & 0xFF}; debug cont')
        summary += " (running asynchronously)"
    return summary + "\n" + _screen()


def _pairs(addr, data):
    return " ".join(f"{addr + i} {b}" for i, b in enumerate(data))


def _dos_basename(name, *, required_extension=None):
    """Return one conservative 8.3 DOS basename safe for host/Tcl staging."""
    if not isinstance(name, str):
        raise TypeError("name must be a string")
    if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_~-]{0,7}(?:\.[A-Za-z0-9]{1,3})?",
            name):
        raise ValueError(
            "name must be one DOS 8.3 basename using letters, digits, _, ~, "
            "or -, without a path")
    normalized = name.upper()
    if required_extension is not None:
        extension = "." + required_extension.upper().lstrip(".")
        if not normalized.endswith(extension):
            raise ValueError(f"name must end with {extension}")
    return normalized


def t_dos_asm_run(source, name="A.COM", run=True):
    """Assemble a Z80 MSX-DOS .COM (org 0x100), import it onto the Nextor hard
    disk (partition 1) and optionally run it. Boots the 'dos' profile if needed."""
    if not name.lower().endswith(".com"):
        name += ".COM"
    name = _dos_basename(name, required_extension="COM")
    if SESSION.profile == "real":
        raise OpenMSXError("MSX-DOS disk import is only available on openMSX")
    if SESSION.profile != "dos":
        raise OpenMSXError(
            "msx_local_dos_asm_run requires an existing local 'dos' profile; "
            "start it explicitly with msx_local_boot(profile='dos')")
    m = SESSION.require()
    text = source
    if "org" not in source.lower():
        text = "    org 0100h\n" + source
    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "a.asm"
        out = pathlib.Path(td) / name
        src.write_text(text)
        r = subprocess.run([Z80ASM, str(src), "-o", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OpenMSXError("z80asm error:\n" + (r.stderr or r.stdout))
        size = out.stat().st_size
        m.cmd(f'diskmanipulator import hda1 {{{out}}}')
    summary = f"[dos] assembled {name} ({size} bytes) -> imported to A:\\"
    if run:
        m.type_line(name.rsplit(".", 1)[0])   # run by base name
        m.advance(3)
        summary += " and executed"
    return summary + "\n" + _screen()


def t_disk_put_text(name, content):
    """Write a text file (e.g. a .BAS listing or .asm) into drive A's disk image."""
    name = _dos_basename(name)
    if SESSION.profile == "real":
        raise OpenMSXError("host disk-image import is only available on openMSX")
    if SESSION.profile != "disk":
        raise OpenMSXError(
            "msx_local_disk_put_text requires an existing local 'disk' "
            "profile; start it explicitly with "
            "msx_local_boot(profile='disk')")
    m = SESSION.require()
    ensure_directory(DISKS)
    disk = DISKS / "work.dsk"
    if not disk.exists():
        m.cmd(f'diskmanipulator create {{{disk}}} 720k')
    m.insert_disk(str(disk))          # mount into drive A
    m.advance(0.3)
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / name
        f.write_text(content)
        # import operates on the mounted drive (diska), not the image path
        m.cmd(f'diskmanipulator import diska {{{f}}}')
    return f"[disk] wrote {name} into {disk.name}\n" + _screen()


def t_shutdown():
    target = _TOOL_TARGET.get()
    if target == "local":
        local = SESSION.backend("local")[0]
        attached = bool(local is not None and getattr(local, "attached", False))
        stopped = SESSION.shutdown_local()
        if not stopped:
            return "[local openMSX not connected]"
        return ("[local openMSX detached]" if attached
                else "[local openMSX stopped]")
    if target == "agent":
        stopped = SESSION.disconnect_agent()
        return "[TCP agent disconnected]" if stopped else "[TCP agent not connected]"
    targets = SESSION.connected_targets()
    if len(targets) > 1:
        raise BackendAmbiguousError(
            "msx_shutdown is ambiguous with two connected targets; use "
            "msx_local_shutdown, msx_agent_disconnect, or "
            "msx_tcp_bench_shutdown")
    if targets == ("local",):
        SESSION.shutdown_local()
        return "[local openMSX stopped]"
    if targets == ("agent",):
        SESSION.disconnect_agent()
        return "[TCP agent disconnected]"
    return "[no target connected]"


def t_tcp_bench_shutdown():
    stopped = SESSION.shutdown_bench()
    return "[hybrid TCP bench stopped]" if stopped else "[hybrid TCP bench not running]"


# --------------------------------------------------------------------------
# Tool registry (name -> (fn, description, schema))
# --------------------------------------------------------------------------
def _s(props, required=()):
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }

TOOLS = {
    "msx_docs_search": (t_docs_search,
        "Search the bundled MSX-AI documentation using a deterministic lexical "
        "index. Results link to exact msx-ai://docs resources and include short "
        "snippets. The corpus is project-authored, GPL-3.0-or-later and carries "
        "machine-readable provenance; no target connection is required.",
        _s({"query": {"type": "string", "minLength": 1},
            "backend": {"type": "string",
                        "enum": ["openmsx-direct", "agent-simulated",
                                 "agent-physical"]},
            "audience": {"type": "string",
                         "enum": ["user", "integrator", "operator",
                                  "developer", "contributor", "maintainer"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                      "default": 5}}, ["query"])),
    "msx_boot": (t_boot,
        "Boot a local MSX with an explicit configuration policy. "
        "profile='basic' starts the Gradiente Expert 2.0 "
        "in BASIC; profile='disk' adds its DDX 3.0 floppy; profile='dos' boots "
        "MSX-DOS 2 (Nextor) from the Sunrise IDE hard disk; profile='msx2plus' "
        "starts a Sony HB-F1XDJ MSX2+ with 128 KiB RAM and built-in floppy; "
        "profile='cbios' uses openMSX's freely distributable C-BIOS MSX2 "
        "machine without proprietary firmware. profile='auto' tries the "
        "configured BASIC machine first and "
        "falls back to C-BIOS only when that explicit adaptive profile was "
        "requested. config_mode='isolated' preserves the reproducible project "
        "home, 'user' uses the user's normal openMSX setup, and 'overlay' adds "
        "MSX-AI templates without hiding user ROM pools. Set window=true to open a "
        "visible openMSX window on the user's screen (shared: the user types in it "
        "and the AI drives the same instance). Returns the boot screen.",
        _s({"profile": {"type": "string",
                        "enum": list(LOCAL_PROFILES),
                        "default": "basic"},
            "window": {"type": "boolean", "default": False},
            "config_mode": {
                "type": "string", "enum": list(OPENMSX_CONFIG_MODES),
                "default": (DEFAULT_OPENMSX_CONFIG_MODE
                            if DEFAULT_OPENMSX_CONFIG_MODE in OPENMSX_CONFIG_MODES
                            else "isolated")}})),
    "msx_attach": (t_attach,
        "Attach to the user's ALREADY-RUNNING openMSX window (shared instance) via "
        "its control socket, instead of spawning a headless one. Use this when the "
        "user opened openMSX themselves (e.g. via ./open-msx.command) and wants to "
        "collaborate on the same live machine. Does not change their throttle/power. "
        "If more than one live socket exists, the call fails safely and lists them; "
        "repeat it with the exact socket_path instead of choosing implicitly.",
        _s({"socket_path": {"type": ["string", "null"], "minLength": 1}})),
    "msx_real_listen": (t_real_listen,
        "Compatibility alias for msx_agent_listen. Listen for a physical MSX "
        "running the ASM agent to connect over TCP. "
        "After it connects, msx_agent_screen and msx_agent_screenshot read "
        "RAM/VRAM only through the agent protocol, without using openMSX "
        "control APIs.",
        _s({"host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535,
                     "default": DEFAULT_PORT},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "maximum": 86400,
                        "default": 60}})),
    "msx_agent_listen": (t_agent_listen,
        "Listen for an MSX resident agent or transparent hardware adapter to "
        "connect over TCP/IPv4. Use this when a physical adapter is configured "
        "as a TCP client; the optional openMSX test profile can use the same "
        "endpoint. The safe default listens only on 127.0.0.1; for physical "
        "hardware, pass the host machine's specific LAN IPv4 address and keep "
        "the unauthenticated endpoint on a trusted network. The MCP protocol "
        "is independent of emulator and adapter brands.",
        _s({"host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535,
                     "default": DEFAULT_PORT},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "maximum": 86400,
                        "default": 60}})),
    "msx_agent_connect": (t_agent_connect,
        "Connect over TCP/IP to an MSX resident agent or transparent adapter "
        "configured as a TCP server. This is the inverse connection direction "
        "of msx_agent_listen and uses the identical transport-neutral protocol.",
        _s({"host": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535,
                     "default": DEFAULT_PORT},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "maximum": 300,
                        "default": 60}}, ["host"])),
    "msx_tcp_bench_start": (t_tcp_bench_start,
        "Start one isolated openMSX instance, install the resident ASM "
        "agent, and connect to it through RS232-Net and TCP/IP. A headless "
        "bench requires a source checkout; set MSX_AI_SOURCE_ROOT when the "
        "MCP host itself was installed with pipx. It is restricted to the "
        "IPv4 loopback host. A headless "
        "bench is host-muted; window=true enables its visible renderer with "
        "normal sound after the TCP handshake. It remains alive until "
        "msx_tcp_bench_shutdown. The bench publishes two explicitly named "
        "channels for the same single machine: msx_local_* uses only openMSX "
        "control APIs, while msx_agent_* uses only TCP and the ASM agent. "
        "Call either family in any order; no active-backend switch exists. "
        "mode='resident' returns to "
        "DOS and supports bounded atomic inspect/patch/screenshot operations "
        "on cooperative DOS-launched software; persistent pause is disabled. "
        "Direct call/run/stop and slot/mapper selection require "
        "mode='monitor'. debug=true is valid only for monitor mode.",
        _s({"host": {"const": "127.0.0.1", "default": "127.0.0.1"},
            "port": {"type": "integer", "minimum": 0, "maximum": 65535,
                     "default": 0},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "maximum": 300,
                        "default": 60},
            "window": {"type": "boolean", "default": False},
            "mode": {"type": "string", "enum": ["resident", "monitor"],
                     "default": "resident"},
            "debug": {"type": "boolean", "default": False}})),
    "msx_screen": (t_screen,
        "Return the current MSX text screen through the tool's fixed channel.",
        _s({})),
    "msx_status": (t_status,
        "Return the active backend and state. A physical-agent session reports "
        "its resident or foreground-monitor runtime, execution state, selected "
        "transport, and negotiated capabilities.",
        _s({})),
    "msx_cpu_snapshot": (t_cpu_snapshot,
        "Capture Z80 registers and useful debug context without changing the "
        "fixed route. openMSX briefly stops at an exact instruction "
        "boundary and returns PC/SP, code bytes, and stack words while "
        "preserving its prior run/break state. A physical agent returns the "
        "versioned BIOS H.TIMI callback-entry register frame; it explicitly "
        "does not claim an arbitrary application PC/SP or NMI-style freeze.",
        _s({})),
    "msx_pause": (t_pause,
        "Pause code launched by the foreground monitor. Safe resident mode "
        "rejects persistent pause; use an atomic memory or screenshot operation "
        "to acquire a bounded snapshot lease instead.",
        _s({})),
    "msx_resume": (t_resume,
        "Resume code paused by the foreground real-MSX monitor.", _s({})),
    "msx_stop": (t_stop,
        "Foreground-monitor only: abandon code launched by the agent and return "
        "to its upload monitor. Resident mode rejects this operation because it "
        "would discard the interrupted DOS/application context.", _s({})),
    "msx_io_read": (t_io_read,
        "ASM-agent only: read one byte directly from an MSX hardware I/O "
        "port. Port and returned value are decimal integers in the JSON result.",
        _s({"port": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["port"])),
    "msx_io_write": (t_io_write,
        "ASM-agent only: write one byte directly to an MSX hardware I/O "
        "port. Optional verify reads the port back; leave it false for "
        "write-only or side-effectful devices.",
        _s({"port": {"type": "integer", "minimum": 0, "maximum": 255},
            "value": {"type": "integer", "minimum": 0, "maximum": 255},
            "verify": {"type": "boolean", "default": False}},
           ["port", "value"])),
    "msx_slot_select": (t_slot_select,
        "ASM-agent only: map an encoded primary/expanded slot ID into CPU "
        "page 0 or page 1. Available only in foreground-monitor mode; MemMan "
        "resident hooks cannot make a persistent, safe slot mapping.",
        _s({"page": {"type": "integer", "minimum": 0, "maximum": 1},
            "slot_id": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["page", "slot_id"])),
    "msx_mapper_select": (t_mapper_select,
        "ASM-agent only: select a memory-mapper segment for CPU page 0 "
        "or page 1. Available only in foreground-monitor mode because remapping "
        "an interrupted resident program is unsafe.",
        _s({"page": {"type": "integer", "minimum": 0, "maximum": 1},
            "segment": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["page", "segment"])),
    "msx_memory_read": (t_memory_read,
        "Read RAM or VRAM through the tool's fixed channel; returns hex. On an "
        "agent, atomic=true (default) uses the bounded snapshot lease and "
        "resumes immediately after the read. Older agents require atomic=false. "
        "MemMan resident mode reserves RAM page 1 (0x4000-0x7FFF) "
        "but leaves pages 2 and 3 accessible.",
        _s({"space": {"type": "string", "enum": ["ram", "vram"]},
            "address": {"type": "integer", "minimum": 0},
            "length": {"type": "integer", "minimum": 0},
            "atomic": {"type": "boolean", "default": True}},
           ["space", "address", "length"])),
    "msx_memory_write": (t_memory_write,
        "Write hexadecimal bytes to RAM or VRAM through the fixed channel. "
        "Set verify=true to read back and compare. On a real agent, "
        "atomic=true (default) uses the bounded snapshot lease for the complete "
        "write and then resumes. Older agents require atomic=false. "
        "MemMan resident mode reserves RAM page 1 while allowing pages 2 and 3; "
        "page 3 contains live BIOS/DOS state and arbitrary writes can crash the "
        "machine.",
        _s({"space": {"type": "string", "enum": ["ram", "vram"]},
            "address": {"type": "integer", "minimum": 0},
            "data_hex": {"type": "string"},
            "verify": {"type": "boolean", "default": False},
            "atomic": {"type": "boolean", "default": True}},
           ["space", "address", "data_hex"])),
    "msx_screenshot": (t_screenshot,
        "Capture the current MSX screen as a PNG image by rendering VRAM host-side. "
        "Both backends support standard SCREEN 0-8 and 10-12, display pages, "
        "scroll and sprites without a visible renderer (SCREEN 9 is a vendor "
        "specific Korean mode). Atomic captures of a running real target use "
        "a bounded snapshot lease and resume immediately after acquiring the "
        "last target byte, before host rendering. Slow 8251 transfers are "
        "refused unless allow_slow=true. An explicit 16xRGB palette overrides the VDP/"
        "BIOS palette mirror; this is useful for games that write the real "
        "VDP's write-only palette directly.",
        _s({"atomic": {"type": "boolean", "default": True},
            "allow_slow": {"type": "boolean", "default": False},
            "page": {"type": "integer", "minimum": 0, "maximum": 3},
            "sprites": {"type": "boolean", "default": True},
            "palette": {"type": "array", "minItems": 16, "maxItems": 16,
                        "items": {"type": "array", "minItems": 3,
                                  "maxItems": 3,
                                  "items": {"type": "integer", "minimum": 0,
                                            "maximum": 255}}}})),
    "msx_type_line": (t_type_line,
        "Type one line and press RETURN. A real resident agent injects the "
        "ASCII bytes atomically through the BIOS keyboard ring; software that "
        "reads the hardware key matrix directly will not observe them. A real "
        "target returns a delivery acknowledgement without implicitly reading "
        "VRAM; call the corresponding explicit screen tool when wanted.",
        _s({"text": {"type": "string"}}, ["text"])),
    "msx_type_lines": (t_type_lines,
        "Type multiple logical lines in one operation, adding RETURN after "
        "each line. A new real resident uses its negotiated credit-controlled "
        "keyboard spool; older agents fall back to safe BIOS-ring pacing. The "
        "real path returns an acknowledgement and performs no implicit VRAM "
        "capture.",
        _s({"lines": {"type": "array",
                       "items": {"type": "string"}}}, ["lines"])),
    "msx_type": (t_type,
        "Type raw text without adding RETURN. A real resident agent uses the "
        "BIOS keyboard ring and waits for each batch to be consumed, then "
        "returns an acknowledgement without implicitly reading VRAM.",
        _s({"text": {"type": "string"}}, ["text"])),
    "msx_key": (t_key,
        "Press ESC, RET, STOP, SPACE, SELECT or TAB. A real resident agent "
        "also accepts CTRL+STOP and the CTRL+C break alias, sending the event "
        "through the BIOS keyboard ring or INTFLG over MCP/TCP; software that "
        "reads the physical key matrix directly will not observe it. The real "
        "path does not capture the screen automatically.",
        _s({"key": {"type": "string"}}, ["key"])),
    "msx_run_basic": (t_run_basic,
        "Enter and RUN a BASIC program. Short listings use one "
        "credit-controlled batched input operation. On a real "
        "resident at DOS, larger listings automatically use a temporary ASCII "
        ".BAS file instead of simulated typing. Set transfer='type' or 'file' "
        "to override that choice. For a real target, explicitly confirm exactly "
        "one state: dos_prompt_confirmed=true at MSX-DOS, or "
        "allow_existing_basic=true at a BASIC Ok prompt. No screen is read to "
        "infer that state, and the real path returns a delivery acknowledgement "
        "without an implicit VRAM capture. Does NEW first unless clear=false.",
        _s({"program": {"type": "string"},
            "clear": {"type": "boolean", "default": True},
            "transfer": {"type": "string",
                         "enum": ["auto", "type", "file"],
                         "default": "auto"},
            "dos_prompt_confirmed": {
                "type": "boolean", "default": False},
            "dos_drive": {"type": "string", "pattern": "^[A-Za-z]$",
                          "default": "A"},
            "allow_existing_basic": {
                "type": "boolean", "default": False}}, ["program"])),
    "msx_run_basic_file": (t_run_basic_file,
        "Transfer an ASCII or tokenized .BAS file over the active MCP/TCP "
        "agent, load it from MSX-DOS, remove the temporary target file, and "
        "RUN it. The same transport-neutral MSXAI.COM performs the DOS write "
        "outside its resident hook. Set dos_prompt_confirmed=true only after "
        "externally confirming the DOS prompt; the tool deliberately does not "
        "read VRAM to infer it.",
        _s({"path": {"type": "string", "minLength": 1},
            "dos_prompt_confirmed": {"type": "boolean"},
            "drive": {"type": "string", "pattern": "^[A-Za-z]$",
                      "default": "A"},
            "format": {"type": "string",
                       "enum": ["auto", "ascii", "tokenized"],
                       "default": "auto"}},
           ["path", "dos_prompt_confirmed"])),
    "msx_file_put": (t_file_put,
        "Upload an arbitrary binary file to MSX-DOS using the negotiated "
        "streaming file-transfer-v2 protocol. Uses 32-bit sizes and offsets, "
        "end-to-end CRC-32, collision-safe publication, and durable resume. "
        "An unambiguously textual .BAS target is normalized to MSX-DOS "
        "CRLF plus 0x1A before hashing; tokenized BASIC remains byte-exact. "
        "compression='auto' keeps ZIP and other already-compressed files "
        "unchanged and uses PackBits only when it saves space and the target "
        "advertises a decoder. The fast-v1 foreground helper pump carries up "
        "to 2026 sequential data bytes per frame, with no extra "
        "STATUS round trip per block. Whole-file CRC, sparse durable state, "
        "resume, and publication checks remain mandatory. It requires the "
        "current agent. Results include "
        "host-measured stream bytes, seconds, and B/s. Set "
        "dos_prompt_confirmed=true only after "
        "externally confirming the DOS prompt. False permits active resume "
        "recovery only. The tool performs no automatic VRAM read.",
        _s({"local_path": {"type": "string", "minLength": 1},
            "msx_path": {"type": "string", "minLength": 1},
            "dos_prompt_confirmed": {"type": "boolean"},
            "compression": {"type": "string",
                            "enum": ["auto", "raw", "packbits"],
                            "default": "auto"},
            "resume": {"type": "boolean", "default": True},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "default": 600.0}},
           ["local_path", "msx_path", "dos_prompt_confirmed"])),
    "msx_file_get": (t_file_get,
        "Download an arbitrary MSX-DOS file using file-transfer-v2. Streams "
        "raw bytes with 32-bit offsets, CRC-32, durable resume, and atomic "
        "no-overwrite publication on the host. GET remains "
        "raw until an independently negotiated MSX encoder is available. "
        "The fast-v1 foreground helper pump carries up to 2040 data bytes per "
        "frame, replacing per-block GET ACKs with sparse durable checkpoints. "
        "Whole-file CRC, resume, and publication checks remain mandatory. It "
        "requires the current agent. Results "
        "include host-measured stream bytes, seconds, and B/s. The "
        "MSX must be externally confirmed at a DOS prompt with "
        "dos_prompt_confirmed=true; false permits active resume recovery only. "
        "The tool performs no automatic VRAM read.",
        _s({"msx_path": {"type": "string", "minLength": 1},
            "local_path": {"type": "string", "minLength": 1},
            "dos_prompt_confirmed": {"type": "boolean"},
            "resume": {"type": "boolean", "default": True},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "default": 600.0}},
           ["msx_path", "local_path", "dos_prompt_confirmed"])),
    "msx_reset": (t_reset,
        "OpenMSX only: reset the MSX and return the boot screen.", _s({})),
    "msx_app_load": (t_app_load,
        "Load an application through the tool's fixed channel using the shared, "
        "interface-independent loader. Detects msx-ai-app-v1 manifests, COM, "
        "BLOAD BIN and flat 16/32 KiB ROM files by extension/header. Relative "
        "paths are resolved from MSX_AI_USER_ROOT, or the server's current "
        "working directory by default. Optional execute overrides "
        "the file entry mode; verify reads every segment back (slower over TCP). "
        "On the agent route, environment='auto' recognizes FE-header BLOAD "
        "files, safely enters MSX BASIC from a verified DOS prompt, injects "
        "and always verifies their declared RAM range, then submits the entry through "
        "DEFUSR/USR. BASIC run returns after submission; call waits for up to "
        "three bounded Ok-prompt probes. Other formats keep direct loading "
        "semantics. Foreground direct payloads run in the MSX-DOS mapping: "
        "page 0 is RAM, so they must use BDOS or BIOS inter-slot calls rather "
        "than jumping to Main-ROM BIOS entry addresses. Use execute='run' "
        "for an interactive program and execute='call' only when the routine "
        "will return before the request timeout. A resident "
        "target always rejects RAM page 1, which contains the mapped TSR.",
        _s({"path": {"type": "string", "minLength": 1},
            "format": {"type": "string",
                       "enum": ["manifest", "com", "bload", "flat-rom"]},
            "execute": {"type": "string", "enum": ["none", "call", "run"]},
            "verify": {"type": "boolean", "default": False},
            "environment": {
                "type": "string", "enum": ["auto", "direct", "basic"],
                "default": "auto",
            }},
           ["path"])),
    "msx_asm_load": (t_asm_load,
        "Assemble Z80 source with z80asm and load the bytes into MSX RAM at "
        "`address` (default 0x8000). Set execute='call' for a returning routine "
        "or execute='run' for asynchronous execution; run=true remains an alias "
        "for execute='run'. call/run require openMSX or the foreground agent "
        "monitor; resident mode can transfer safe bytes with execute='none'.",
        _s({"source": {"type": "string"},
            "address": {"type": "integer", "default": 32768},
            "run": {"type": "boolean", "default": False},
            "execute": {"type": "string", "enum": ["none", "call", "run"]}},
           ["source"])),
    "msx_dos_asm_run": (t_dos_asm_run,
        "OpenMSX only: assemble a Z80 .COM (org 0x100 default), import it "
        "onto the Nextor hard disk and run it at the A:\\> prompt. Requires "
        "an existing local 'dos' profile. Returns the DOS output.",
        _s({"source": {"type": "string"},
            "name": {"type": "string", "default": "A.COM"},
            "run": {"type": "boolean", "default": True}}, ["source"])),
    "msx_disk_put_text": (t_disk_put_text,
        "OpenMSX only: write a text file into drive A's disk image so BASIC "
        "can LOAD/RUN it. Requires an existing local 'disk' profile.",
        _s({"name": {"type": "string"}, "content": {"type": "string"}},
           ["name", "content"])),
    "msx_cmd": (t_cmd,
        "OpenMSX only: run a raw Tcl console command (debug peek/poke, "
        "media control, etc.). Advanced use.",
        _s({"tcl": {"type": "string"}}, ["tcl"])),
    "msx_shutdown": (t_shutdown,
        "Close the active emulator or real-agent connection. A paused real "
        "application is resumed before disconnecting.", _s({})),
}

_CANONICAL_TOOL_NAMES = tuple(TOOLS)


def _targeted_handler(handler, target):
    """Bind one public tool permanently to one backend family."""
    def routed(**arguments):
        token = _TOOL_TARGET.set(target)
        try:
            return handler(**arguments)
        finally:
            _TOOL_TARGET.reset(token)
    routed.__name__ = f"{handler.__name__}_{target}"
    return routed


# Explicit public API.  The canonical handlers stay shared so parsing and
# behavior cannot drift, but routing is fixed by tool name and cannot be
# changed by arguments or by whichever backend connected most recently.
LOCAL_TOOL_ALIASES = {
    "msx_local_boot": "msx_boot",
    "msx_local_attach": "msx_attach",
    "msx_local_status": "msx_status",
    "msx_local_screen": "msx_screen",
    "msx_local_cpu_snapshot": "msx_cpu_snapshot",
    "msx_local_memory_read": "msx_memory_read",
    "msx_local_memory_write": "msx_memory_write",
    "msx_local_screenshot": "msx_screenshot",
    "msx_local_type_line": "msx_type_line",
    "msx_local_type_lines": "msx_type_lines",
    "msx_local_type": "msx_type",
    "msx_local_key": "msx_key",
    "msx_local_run_basic": "msx_run_basic",
    "msx_local_reset": "msx_reset",
    "msx_local_app_load": "msx_app_load",
    "msx_local_asm_load": "msx_asm_load",
    "msx_local_dos_asm_run": "msx_dos_asm_run",
    "msx_local_disk_put_text": "msx_disk_put_text",
    "msx_local_cmd": "msx_cmd",
    "msx_local_shutdown": "msx_shutdown",
}

AGENT_TOOL_ALIASES = {
    "msx_agent_status": "msx_status",
    "msx_agent_screen": "msx_screen",
    "msx_agent_cpu_snapshot": "msx_cpu_snapshot",
    "msx_agent_pause": "msx_pause",
    "msx_agent_resume": "msx_resume",
    "msx_agent_stop": "msx_stop",
    "msx_agent_io_read": "msx_io_read",
    "msx_agent_io_write": "msx_io_write",
    "msx_agent_slot_select": "msx_slot_select",
    "msx_agent_mapper_select": "msx_mapper_select",
    "msx_agent_memory_read": "msx_memory_read",
    "msx_agent_memory_write": "msx_memory_write",
    "msx_agent_screenshot": "msx_screenshot",
    "msx_agent_type_line": "msx_type_line",
    "msx_agent_type_lines": "msx_type_lines",
    "msx_agent_type": "msx_type",
    "msx_agent_key": "msx_key",
    "msx_agent_run_basic": "msx_run_basic",
    "msx_agent_run_basic_file": "msx_run_basic_file",
    "msx_agent_file_put": "msx_file_put",
    "msx_agent_file_get": "msx_file_get",
    "msx_agent_app_load": "msx_app_load",
    "msx_agent_asm_load": "msx_asm_load",
    "msx_agent_disconnect": "msx_shutdown",
}


def _schema_without(schema, *properties):
    excluded = set(properties)
    return {
        **schema,
        "properties": {
            name: value for name, value in schema["properties"].items()
            if name not in excluded
        },
        "required": [
            name for name in schema.get("required", []) if name not in excluded
        ],
    }


LOCAL_SCHEMA_OVERRIDES = {
    "msx_local_memory_read": _schema_without(
        TOOLS["msx_memory_read"][2], "atomic"),
    "msx_local_memory_write": _schema_without(
        TOOLS["msx_memory_write"][2], "atomic"),
    "msx_local_screenshot": _schema_without(
        TOOLS["msx_screenshot"][2], "atomic", "allow_slow"),
    "msx_local_run_basic": _schema_without(
        TOOLS["msx_run_basic"][2], "transfer", "dos_prompt_confirmed",
        "dos_drive", "allow_existing_basic"),
    "msx_local_key": _s({
        "key": {
            "type": "string",
            "description": (
                "Case-insensitive named key: 1-5, F1-F5, UP, DOWN, LEFT, "
                "RIGHT, ESC, RET, STOP, SPACE, SELECT, TAB, or CTRL+STOP."
            ),
        },
    }, ["key"]),
    "msx_local_app_load": _schema_without(
        TOOLS["msx_app_load"][2], "environment"),
}

EXPLICIT_DESCRIPTIONS = {
    "msx_local_status": (
        "Return only the local openMSX control-channel state and identity. "
        "This never contacts an ASM agent."),
    "msx_agent_status": (
        "Return only the ASM-agent protocol state, negotiated capabilities, "
        "transport, and identity. This never calls an openMSX API."),
    "msx_local_cpu_snapshot": (
        "Capture an exact Z80 instruction-boundary snapshot through the local "
        "openMSX debugger while preserving its previous run/break state."),
    "msx_agent_cpu_snapshot": (
        "Capture the cooperative Z80 H.TIMI callback-entry frame only through "
        "the ASM-agent protocol; unavailable when that hook is not serviced. "
        "It does not claim an exact application PC or SP."),
    "msx_local_memory_read": (
        "Read RAM or VRAM only through the local openMSX debugger and return "
        "hexadecimal bytes."),
    "msx_agent_memory_read": (
        "Read RAM or VRAM only through the ASM agent. atomic=true uses a "
        "bounded resident snapshot lease when supported."),
    "msx_local_memory_write": (
        "Write RAM or VRAM only through the local openMSX debugger; verify=true "
        "performs a local read-back comparison."),
    "msx_agent_memory_write": (
        "Write RAM or VRAM only through the ASM agent. atomic=true uses a "
        "bounded resident snapshot lease when supported."),
    "msx_local_screenshot": (
        "Capture and render the screen only through the local openMSX control "
        "API. This remains available for bench diagnosis when the TCP agent "
        "is stalled or disconnected."),
    "msx_agent_screenshot": (
        "Capture VRAM only through the ASM-agent protocol and render it "
        "host-side. This validates the same path used by physical hardware "
        "and never reads through openMSX control APIs."),
    "msx_local_type_line": (
        "Type one line plus Return through the local openMSX input API and "
        "return the resulting text screen."),
    "msx_local_type_lines": (
        "Type several lines through the local openMSX input API and return "
        "the resulting text screen."),
    "msx_local_type": (
        "Type text without an implicit Return through the local openMSX input "
        "API and return the resulting text screen."),
    "msx_local_key": (
        "Send one named key through the local openMSX input API and return "
        "the resulting text screen. Accepted names (case-insensitive): 1-5, "
        "F1-F5, UP, DOWN, LEFT, RIGHT, ESC, RET, STOP, SPACE, SELECT, TAB, "
        "and CTRL+STOP. Single keys emit one keyboard-matrix down/up pair; "
        "CTRL+STOP emits the real CTRL and STOP chord required by the "
        "foreground monitor."),
    "msx_agent_type_line": (
        "Send one line through the ASM agent's credited BIOS keyboard spool; "
        "returns an acknowledgement without capturing VRAM."),
    "msx_agent_type_lines": (
        "Send several lines through the ASM agent's credited BIOS keyboard "
        "spool; returns one acknowledgement without capturing VRAM."),
    "msx_agent_type": (
        "Send text without implicit Return through the ASM agent's BIOS "
        "keyboard spool; returns an acknowledgement without a screen read."),
    "msx_agent_key": (
        "Send a supported named key or break event only through the ASM agent; "
        "returns an acknowledgement without a screen read."),
    "msx_local_run_basic": (
        "Enter and run a BASIC listing through local openMSX input. This tool "
        "does not use the ASM-agent file-transfer path."),
    "msx_agent_run_basic": (
        "Enter and run a BASIC listing only through the ASM agent, using the "
        "keyboard spool or confirmed MSX-DOS file delivery as requested."),
    "msx_local_app_load": (
        "Load a validated application only through local openMSX debugger "
        "memory operations, with optional verification and execution."),
    "msx_agent_app_load": (
        "Load a validated application only through the ASM-agent protocol. "
        "The default environment='auto' treats FE-header BLOAD files as BASIC "
        "artifacts: on a resident agent at a verified DOS/BASIC prompt it "
        "enters BASIC when needed, injects and always verifies the declared "
        "RAM payload, and submits its entry through DEFUSR/USR. BASIC run is "
        "asynchronous; call waits for up to three bounded Ok-prompt probes. Use "
        "environment='direct' only for artifacts intentionally built for the "
        "foreground monitor. Direct payloads run with the MSX-DOS mapping, "
        "where page 0 is RAM: use BDOS or BIOS inter-slot calls, not fixed "
        "Main-ROM entry addresses. Prefer execute='run' for interactive code; "
        "execute='call' waits for the routine to return. No openMSX API is "
        "used."),
    "msx_local_asm_load": (
        "Assemble Z80 source and load it only through local openMSX debugger "
        "memory operations, with optional call or run."),
    "msx_agent_asm_load": (
        "Assemble Z80 source and load it only through ASM-agent memory "
        "operations; call/run require foreground-monitor mode."),
    "msx_local_shutdown": (
        "Close an owned standalone openMSX process or detach from an external "
        "one without quitting it. A bench-owned machine must be closed with "
        "msx_tcp_bench_shutdown."),
    "msx_agent_disconnect": (
        "Disconnect only the ASM-agent protocol channel, leaving any local "
        "openMSX diagnostic channel and bench process alive."),
}

for public_name, canonical_name in LOCAL_TOOL_ALIASES.items():
    handler, description, schema = TOOLS[canonical_name]
    TOOLS[public_name] = (
        _targeted_handler(handler, "local"),
        EXPLICIT_DESCRIPTIONS.get(
            public_name,
            "Local openMSX control API only; this tool never uses the TCP "
            "agent. " + description),
        LOCAL_SCHEMA_OVERRIDES.get(public_name, schema),
    )

for public_name, canonical_name in AGENT_TOOL_ALIASES.items():
    handler, description, schema = TOOLS[canonical_name]
    TOOLS[public_name] = (
        _targeted_handler(handler, "agent"),
        EXPLICIT_DESCRIPTIONS.get(
            public_name,
            "ASM-agent protocol only; this tool never uses openMSX debugger "
            "or control APIs. " + description),
        schema,
    )

TOOLS.update({
    "msx_local_doctor": (
        t_local_doctor,
        "Inspect local openMSX readiness without starting an emulator or "
        "creating configuration files. Reports the resolved executable, "
        "platform control/attach transports, config policy and homes, profile "
        "resolution, machine/ROM prerequisites, and actionable problems.",
        _s({
            "profile": {"type": "string", "enum": list(LOCAL_PROFILES),
                        "default": "auto"},
            "config_mode": {
                "type": "string", "enum": list(OPENMSX_CONFIG_MODES),
                "default": (DEFAULT_OPENMSX_CONFIG_MODE
                            if DEFAULT_OPENMSX_CONFIG_MODE in OPENMSX_CONFIG_MODES
                            else "isolated")},
        }),
    ),
    "msx_targets_status": (
        t_status,
        "Inventory the independently connected local openMSX and ASM-agent "
        "channels. This tool reports identities only and never selects a "
        "backend for another operation.",
        _s({}),
    ),
    "msx_tcp_bench_status": (
        t_tcp_bench_status,
        "Return both explicitly identified channels of the hybrid test bench: "
        "local openMSX control and the TCP ASM-agent protocol.",
        _s({}),
    ),
    "msx_tcp_bench_shutdown": (
        t_tcp_bench_shutdown,
        "Close the hybrid bench atomically, including its TCP agent channel, "
        "single owned openMSX process, and disposable runtime directory.",
        _s({}),
    ),
})

# The project is still pre-release, so ambiguous operational names are not
# advertised.  Keeping the shared Python handlers above avoids duplicated
# implementations; clients see only fixed-route local/agent names.
_EXPLICIT_CORE_TOOLS = {
    "msx_docs_search",
    "msx_agent_listen",
    "msx_agent_connect",
    "msx_tcp_bench_start",
}
for _legacy_name in _CANONICAL_TOOL_NAMES:
    if _legacy_name not in _EXPLICIT_CORE_TOOLS:
        TOOLS.pop(_legacy_name, None)

# --------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 over newline-delimited stdio)
# --------------------------------------------------------------------------
def _result(text):
    return {"content": [{"type": "text", "text": text}]}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "msx-ai", "version": SERVER_VERSION}}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = [{"name": n, "description": d, "inputSchema": s}
                 for n, (_, d, s) in TOOLS.items()]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        entry = TOOLS.get(name)
        if entry is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool {name}"}}
        try:
            out = entry[0](**args)
            # The compatibility server exposes structured handler results as
            # JSON text. The SDK runtime publishes the same dict as native
            # structuredContent. Ready content lists pass through unchanged.
            if isinstance(out, list):
                content = out
            else:
                text = (json.dumps(out, sort_keys=True)
                        if isinstance(out, dict) else str(out))
                content = [{"type": "text", "text": text}]
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": content}}
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text",
                               "text": f"ERROR: {e}\n{tb}"}], "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return None


def main():
    out = sys.stdout
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = handle(msg)
            if resp is not None:
                out.write(json.dumps(resp) + "\n")
                out.flush()
    finally:
        SESSION.shutdown()


if __name__ == "__main__":
    main()
