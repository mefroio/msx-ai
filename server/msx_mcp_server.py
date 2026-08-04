#!/usr/bin/env python3
"""MSX-AI :: MCP bridge for openMSX and the resident real-MSX agent.

Zero external dependencies: speaks MCP (JSON-RPC 2.0) over newline-delimited
stdio directly, so it runs anywhere Python 3 does. It can drive an isolated
openMSX through its control channel or a physical MSX through the resident Z80
monitor and TCP/serial transport. Models can upload Z80 builds, inspect or
patch RAM/VRAM, control execution and render screenshots from captured VRAM.

Nothing here touches the user's own openMSX setups: OPENMSX_HOME points at the
project-local .openmsx-home built for msx-ai.
"""
import sys, os, json, tempfile, subprocess, pathlib, traceback, shutil, re, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import base64
from msx_client import OpenMSX, OpenMSXError, PROJ
from msx_real import (RealMSX, CAPABILITY_NAMES, AGENT_FEATURE_NAMES,
                      UART8251_BAUD)
from msx_application import load_application
import msx_screenshot

Z80ASM = (os.environ.get("Z80ASM") or shutil.which("z80asm") or
          "/opt/homebrew/bin/z80asm")
MAKE = os.environ.get("MAKE") or shutil.which("make") or "make"
WORK = PROJ / "work"
DISKS = WORK / "disks"
DISKS.mkdir(parents=True, exist_ok=True)
AGENT_COM = WORK / "agent" / "MSXAI.COM"
# Local Nextor / MSX-DOS 2 hard-disk image (never distributed by this project).
DOS_HDD = pathlib.Path(os.environ.get(
    "MSX_AI_DOS_HDD", WORK / "system-disks" / "msxdos.dsk")).expanduser()
BASIC_MACHINE = os.environ.get("MSX_AI_BASIC_MACHINE", "Gradiente_Expert20")
MSX2PLUS_MACHINE = os.environ.get(
    "MSX_AI_MSX2PLUS_MACHINE", "Sony_HB-F1XDJ_128K_Lite")
DISK_EXTENSION = os.environ.get("MSX_AI_DISK_EXTENSION", "DDX_3.0")
DOS_EXTENSION = os.environ.get("MSX_AI_DOS_EXTENSION", "SunriseIDE_Nextor")
MCP_SLOT_EXPANDER = os.environ.get(
    "MSX_AI_MCP_SLOT_EXPANDER", "slotexpander")
RESIDENT_INSTALL_SECONDS = 15
RESIDENT_PROMPT_GRACE_SECONDS = 15
BENCH_AGENT_NAME = "MSXAI.COM"
REAL_BASIC_PROMPT_TIMEOUT_SECONDS = 10.0
REAL_BASIC_PROMPT_POLL_SECONDS = 0.10
UART_BITS_PER_BYTE = 10
UART_SCREENSHOT_MARGIN = 1.15
SLOW_SCREENSHOT_SECONDS = 10.0

PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.5.0"


def _build_agent_artifact():
    """Build and return the one canonical universal agent executable."""
    environment = os.environ.copy()
    environment["Z80ASM"] = Z80ASM
    try:
        build = subprocess.run(
            [MAKE, "agent"], cwd=PROJ, env=environment,
            capture_output=True, text=True)
    except OSError as exc:
        raise OpenMSXError(f"could not run the canonical agent build: {exc}") from exc
    if build.returncode != 0:
        raise OpenMSXError(
            "could not build work/agent/MSXAI.COM with `make agent`:\n"
            + (build.stderr or build.stdout))
    if not AGENT_COM.is_file() or AGENT_COM.stat().st_size == 0:
        raise OpenMSXError(
            "canonical agent build did not produce work/agent/MSXAI.COM")
    return AGENT_COM


def _dos_prompt_visible(screen):
    """Return whether the last non-blank text row is an MSX-DOS prompt."""
    rows = [row.strip() for row in str(screen).splitlines() if row.strip()]
    return bool(rows and re.fullmatch(r"[A-Za-z]:\\[^>]*>", rows[-1]))


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

# --------------------------------------------------------------------------
# Emulator session (lazy, single instance kept alive across tool calls)
# --------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.msx = None
        self.profile = None
        self.bench_machine = None
        self.bench_runtime = None

    def boot(self, profile="basic", boot_seconds=6, window=False):
        self.shutdown()
        if profile == "dos":
            if not DOS_HDD.exists():
                raise OpenMSXError(f"MSX-DOS image not found: {DOS_HDD}")
            self.msx = OpenMSX(machine=BASIC_MACHINE,
                               extensions=[DOS_EXTENSION],
                               harddisk=str(DOS_HDD)).start(headless=not window)
        elif profile == "disk":
            self.msx = OpenMSX(machine=BASIC_MACHINE,
                               extensions=[DISK_EXTENSION]).start(headless=not window)
        elif profile == "msx2plus":
            self.msx = OpenMSX(machine=MSX2PLUS_MACHINE,
                               extensions=[]).start(headless=not window)
        else:
            self.msx = OpenMSX(machine=BASIC_MACHINE, extensions=[]).start(
                headless=not window)
        self.msx.power_on()
        if window:
            # Show a real openMSX window on the user's screen (renderer none ->
            # SDLGL-PP). Works because the MCP server runs in the GUI session.
            # The same -control channel still drives it, so the user types in the
            # window AND the AI operates the same shared instance.
            self.msx.cmd("set renderer SDLGL-PP")
            self.msx.cmd("set throttle on")   # real speed for interactive use
        self.msx.advance(boot_seconds if profile != "dos" else 14)
        if profile == "disk":
            # DDX shows its insert-disk prompt; ESC drops into DDX-BASIC.
            self.msx.press("ESC")
            self.msx.advance(3)
        self.profile = profile
        return self.msx.screen_text()

    def attach(self):
        """Connect to the user's already-running openMSX window (shared instance)."""
        self.shutdown()
        self.msx = OpenMSX().attach()
        self.msx.enable_keybuf()      # do NOT touch throttle/power of their session
        self.profile = "attach"
        return self.msx.screen_text()

    def listen_agent(self, host="0.0.0.0", port=6603, timeout=60):
        """Wait for an ASM agent or transparent adapter to connect over TCP."""
        self.shutdown()
        real = RealMSX(host=host, port=int(port)).listen()
        try:
            peer = real.accept(timeout=float(timeout))
        except Exception:
            real.close()
            raise
        self.msx = real
        self.profile = "real"
        return peer

    def connect_agent(self, host, port=6603, timeout=60):
        """Connect to an ASM agent or transparent adapter over TCP."""
        self.shutdown()
        real = RealMSX(host=host, port=int(port))
        try:
            peer = real.connect(timeout=float(timeout))
        except Exception:
            real.close()
            raise
        self.msx = real
        self.profile = "real"
        return peer

    def start_tcp_bench(self, host="127.0.0.1", port=0, timeout=60,
                        window=False, mode="resident", debug=False,
                        preload_files=()):
        """Start one isolated openMSX as a physical-agent TCP simulation."""
        self.shutdown()
        if mode not in ("resident", "monitor"):
            raise ValueError("mode must be 'resident' or 'monitor'")
        if not isinstance(debug, bool):
            raise TypeError("debug must be a boolean")
        if debug and mode != "monitor":
            raise ValueError("DEBUG ON is available only with mode='monitor'")
        if not DOS_HDD.is_file():
            raise OpenMSXError(f"MSX-DOS image not found: {DOS_HDD}")

        runtime = tempfile.TemporaryDirectory(prefix="msx-ai-tcp-bench-")
        root = pathlib.Path(runtime.name)
        disk = root / "msxdos.dsk"
        home = root / "openmsx-home"
        # The disposable bench disk exposes exactly the same canonical name as
        # a physical target. A stale copy is removed before import because
        # diskmanipulator does not overwrite an existing same-name file.
        agent_com = root / BENCH_AGENT_NAME
        machine = None
        real = None
        try:
            canonical_agent = _build_agent_artifact()
            shutil.copyfile(DOS_HDD, disk)
            shutil.copytree(
                PROJ / ".openmsx-home", home,
                ignore=shutil.ignore_patterns(
                    ".DS_Store", "persistent", "savestates", "replays",
                    "screenshots", "recordings", "settings.local.xml",
                    ".filecache", "imgui.ini"))
            shutil.copyfile(canonical_agent, agent_com)

            real = RealMSX(host=host, port=int(port)).listen()
            machine = OpenMSX(
                machine=BASIC_MACHINE,
                extensions=[
                    MCP_SLOT_EXPANDER, DOS_EXTENSION, "rs232_proto"],
                harddisk=str(disk), home=home,
            ).start(headless=not window)
            # Import while the emulated machine is off. openMSX may otherwise
            # retain stale filesystem sectors for a disk mounted by MSX-DOS.
            machine.cmd("set power off")
            listing = machine.cmd("diskmanipulator dir hda1")
            entry = re.search(
                rf"(?im)^\s*{re.escape(BENCH_AGENT_NAME)}\s+\S+\s+(\d+)\s*$",
                listing)
            if entry is not None:
                machine.cmd(
                    f"diskmanipulator delete hda1 {BENCH_AGENT_NAME}")
            machine.cmd(f"diskmanipulator import hda1 {{{agent_com}}}")
            listing = machine.cmd("diskmanipulator dir hda1")
            entry = re.search(
                rf"(?im)^\s*{re.escape(BENCH_AGENT_NAME)}\s+\S+\s+(\d+)\s*$",
                listing)
            expected_size = agent_com.stat().st_size
            if entry is None or int(entry.group(1)) != expected_size:
                observed = "missing" if entry is None else entry.group(1)
                raise OpenMSXError(
                    f"bench disk {BENCH_AGENT_NAME} verification failed: "
                    f"expected {expected_size} bytes, found {observed}")
            for preload in preload_files:
                preload = pathlib.Path(preload).resolve()
                if not preload.is_file():
                    raise OpenMSXError(
                        f"bench preload file not found: {preload}")
                machine.cmd(f"diskmanipulator import hda1 {{{preload}}}")
            machine.power_on()
            machine.advance(14)
            command = f"{pathlib.Path(BENCH_AGENT_NAME).stem} /DRIVER:8251"
            if mode == "monitor":
                command += " /MONITOR"
            if debug:
                command += " DEBUG ON"
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
                peer = real.accept(timeout=float(timeout))
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

            self.msx = real
            self.profile = "real"
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

    def require(self):
        if self.msx is None:
            self.boot("basic")
        return self.msx

    def shutdown(self):
        if self.msx is not None:
            if self.profile == "real":
                try:
                    if self.msx.status()["state"] == "paused":
                        self.msx.resume()
                except Exception:
                    pass
            try:
                self.msx.close()
            except Exception:
                pass
            self.msx = None
            self.profile = None
        if self.bench_machine is not None:
            try:
                self.bench_machine.close()
            except Exception:
                pass
            self.bench_machine = None
        if self.bench_runtime is not None:
            try:
                self.bench_runtime.cleanup()
            except Exception:
                pass
            self.bench_runtime = None


SESSION = Session()

# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------
def _screen():
    return SESSION.require().screen_text()


def _wait_for_real_screen(m, predicate,
                          timeout=REAL_BASIC_PROMPT_TIMEOUT_SECONDS):
    """Poll a screen captured through the agent until predicate matches."""
    deadline = time.monotonic() + float(timeout)
    screen = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return screen
        screen = m.screen_text(timeout=remaining)
        if predicate(screen):
            return screen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return screen
        time.sleep(min(REAL_BASIC_PROMPT_POLL_SECONDS, remaining))


def t_boot(profile="basic", window=False):
    if profile not in ("basic", "disk", "dos", "msx2plus"):
        raise ValueError(
            "profile must be 'basic', 'disk', 'dos' or 'msx2plus'")
    scr = SESSION.boot(profile, window=window)
    tag = "window" if window else "headless"
    return f"[boot profile={profile} {tag}]\n{scr}"


def t_attach():
    scr = SESSION.attach()
    return ("[attached to your running openMSX window — shared instance]\n" + scr)


def t_real_listen(host="0.0.0.0", port=6603, timeout=60):
    return t_agent_listen(host=host, port=port, timeout=timeout)


def _format_endpoint(endpoint):
    if isinstance(endpoint, (tuple, list)) and len(endpoint) >= 2:
        return f"{endpoint[0]}:{endpoint[1]}"
    return str(endpoint)


def t_agent_listen(host="0.0.0.0", port=6603, timeout=60):
    peer = SESSION.listen_agent(host=host, port=port, timeout=timeout)
    return (f"[MSX agent connected from {_format_endpoint(peer)} "
            f"over TCP/IP to {host}:{int(port)}]")


def t_agent_connect(host, port=6603, timeout=60):
    peer = SESSION.connect_agent(host=host, port=port, timeout=timeout)
    return f"[MSX agent connected over TCP/IP to {_format_endpoint(peer)}]"


def t_tcp_bench_start(host="127.0.0.1", port=0, timeout=60, window=False,
                      mode="resident", debug=False):
    """Boot one isolated openMSX and reach its resident agent only over TCP."""
    SESSION.start_tcp_bench(
        host=host, port=port, timeout=timeout, window=window,
        mode=mode, debug=debug)
    return t_status()


def t_screen():
    return _screen()


def t_status():
    if SESSION.msx is None:
        return json.dumps({"backend": "none", "state": "disconnected"},
                          sort_keys=True)
    m = SESSION.msx
    if SESSION.profile == "real":
        state = m.status()
        state.update({
            "backend": "real",
            "peer": m.peer,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if m.capabilities & bit],
            "resident_base": m.resident_base,
            # Keep the legacy field while making the independent link and
            # MSX-side hardware layers explicit for new clients.
            "transport": getattr(m, "agent_transport", None),
            "agent_transport": getattr(m, "agent_transport", None),
            "agent_transport_id": getattr(m, "agent_transport_id", None),
            "network_transport": getattr(m, "network_transport", None),
            "network_role": getattr(m, "network_role", None),
            "local_endpoint": getattr(m, "local_endpoint", None),
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
        return json.dumps(state, sort_keys=True)
    return json.dumps({"backend": "openmsx", "profile": SESSION.profile,
                       "screen_mode": m.screen_mode()}, sort_keys=True)


def _require_real():
    if SESSION.profile != "real":
        raise OpenMSXError("this operation requires an active real ASM-agent session")
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
    return json.dumps({"port": port, "value": value}, sort_keys=True)


def t_io_write(port, value, verify=False):
    """Write one hardware I/O port through the resident agent."""
    port = _int_in_range(port, "port", 0, 0xFF)
    value = _int_in_range(value, "value", 0, 0xFF)
    if not isinstance(verify, bool):
        raise TypeError("verify must be a boolean")
    _require_real().io_write(port, value, verify=verify)
    return json.dumps({"port": port, "value": value, "verified": verify},
                      sort_keys=True)


def t_slot_select(page, slot_id):
    """Map a slot into page 0 or 1 in foreground-monitor mode."""
    page = _int_in_range(page, "page", 0, 1)
    slot_id = _int_in_range(slot_id, "slot_id", 0, 0xFF)
    _require_real().slot_select(page, slot_id)
    return json.dumps({"page": page, "slot_id": slot_id}, sort_keys=True)


def t_mapper_select(page, segment):
    """Map a segment into page 0 or 1 in foreground-monitor mode."""
    page = _int_in_range(page, "page", 0, 1)
    segment = _int_in_range(segment, "segment", 0, 0xFF)
    _require_real().mapper_select(page, segment)
    return json.dumps({"page": page, "segment": segment}, sort_keys=True)


def _atomic_real(m, atomic, operation):
    with m.snapshot_lease(atomic=atomic):
        return operation()


def t_memory_read(space, address, length, atomic=True):
    m = _require_real()
    if space == "ram":
        read = lambda: m.peek(int(address), int(length))
    elif space == "vram":
        read = lambda: m.vpeek(int(address), int(length))
    else:
        raise ValueError("space must be 'ram' or 'vram'")
    data = _atomic_real(m, atomic, read)
    return f"[{space} 0x{int(address):X}+{len(data)}]\n{data.hex()}"


def t_memory_write(space, address, data_hex, verify=False, atomic=True):
    m = _require_real()
    try:
        data = bytes.fromhex(data_hex)
    except ValueError as exc:
        raise ValueError("data_hex must contain an even number of hexadecimal digits") from exc
    address = int(address)
    if space == "ram":
        write = lambda: (m.poke(address, data),
                         m.peek(address, len(data)) if verify else None)[1]
    elif space == "vram":
        write = lambda: (m.vpoke(address, data),
                         m.vpeek(address, len(data)) if verify else None)[1]
    else:
        raise ValueError("space must be 'ram' or 'vram'")
    check = _atomic_real(m, atomic, write)
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


def t_app_load(path, format=None, execute=None, verify=False):
    """Load a manifest, COM, BLOAD BIN or flat ROM through the active backend."""
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

    source = pathlib.Path(path).expanduser()
    if not source.is_absolute():
        source = PROJ / source
    msx = SESSION.require()
    real = SESSION.profile == "real"
    backend = msx if real else _OpenMSXApplicationBackend(msx)
    result = dict(load_application(
        backend, source, format=format, execute=execute, verify=verify,
        stop_before_load=real))
    result["backend"] = "real" if real else "openmsx"
    return json.dumps(result, sort_keys=True)


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
    m.type_line(text)
    if SESSION.profile != "real":
        m.advance(0.6)
    return _screen()


def t_type(text):
    m = SESSION.require()
    m.type(text)
    if SESSION.profile != "real":
        m.advance(0.4)
    return _screen()


def t_key(key):
    if SESSION.profile == "real":
        raise OpenMSXError("keyboard injection is not implemented by the real agent")
    m = SESSION.require()
    m.press(key.upper())
    m.advance(0.6)
    return _screen()


def t_run_basic(program, clear=True, allow_existing_basic=False):
    if not isinstance(program, str):
        raise TypeError("program must be a string")
    if not isinstance(clear, bool):
        raise TypeError("clear must be a boolean")
    if not isinstance(allow_existing_basic, bool):
        raise TypeError("allow_existing_basic must be a boolean")
    m = SESSION.require()
    real = SESSION.profile == "real"
    # A resident is normally installed from DOS.  Enter BASIC automatically
    # only when the last visible row is an unambiguous DOS prompt; never type
    # BASIC over an arbitrary application or game.
    if real:
        screen = m.screen_text()
        if _dos_prompt_visible(screen):
            m.type_line("BASIC")
            screen = _wait_for_real_screen(m, _basic_prompt_visible)
            if not _basic_prompt_visible(screen):
                raise OpenMSXError(
                    "BASIC did not reach its Ok prompt after leaving MSX-DOS. "
                    "Last captured screen:\n" + screen)
        elif not allow_existing_basic:
            raise OpenMSXError(
                "msx_run_basic requires a visible MSX-DOS prompt by default; "
                "refusing to type over an unverified target. "
                "Set allow_existing_basic=true only after confirming that the "
                "target is waiting at an MSX BASIC Ok prompt")
        elif not _basic_prompt_visible(screen):
            raise OpenMSXError(
                "allow_existing_basic was requested, but the visible screen "
                "does not end at an MSX BASIC Ok prompt; refusing to type over "
                "a running application or game")
    if clear:
        m.type_line("NEW")
        if real:
            screen = _wait_for_real_screen(m, _basic_prompt_visible)
            if not _basic_prompt_visible(screen):
                raise OpenMSXError(
                    "BASIC did not return to Ok after NEW. "
                    "Last captured screen:\n" + screen)
        else:
            m.advance(0.4)
    for line in program.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m.type_line(line)
        # MSX BASIC does not print a fresh `Ok` after storing a numbered line.
        # RealMSX.type_line() already waits for the BIOS queue to be consumed
        # and gives the interpreter a short post-CR settle time.
        if not real:
            m.advance(0.3)
    m.type_line("RUN")
    if real:
        time.sleep(2.5)
    else:
        m.advance(2.5)
    return _screen()


def t_reset():
    if SESSION.profile == "real":
        raise OpenMSXError("physical reset is not implemented by the resident agent")
    m = SESSION.require()
    m.cmd("reset")
    m.advance(6)
    return _screen()


def t_cmd(tcl):
    """Escape hatch: run a raw openMSX Tcl console command (peek/poke, debug…)."""
    if SESSION.profile == "real":
        raise OpenMSXError("raw Tcl commands are only available on openMSX")
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


def t_dos_asm_run(source, name="A.COM", run=True):
    """Assemble a Z80 MSX-DOS .COM (org 0x100), import it onto the Nextor hard
    disk (partition 1) and optionally run it. Boots the 'dos' profile if needed."""
    if SESSION.profile == "real":
        raise OpenMSXError("MSX-DOS disk import is only available on openMSX")
    if SESSION.profile != "dos":
        SESSION.boot("dos")
    m = SESSION.require()
    if not name.lower().endswith(".com"):
        name += ".COM"
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
    if SESSION.profile == "real":
        raise OpenMSXError("host disk-image import is only available on openMSX")
    if SESSION.profile != "disk":
        SESSION.boot("disk")
    m = SESSION.require()
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
    backend = "real agent connection" if SESSION.profile == "real" else "emulator"
    SESSION.shutdown()
    return f"[{backend} stopped]"


# --------------------------------------------------------------------------
# Tool registry (name -> (fn, description, schema))
# --------------------------------------------------------------------------
def _s(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}

TOOLS = {
    "msx_boot": (t_boot,
        "Boot an isolated MSX. profile='basic' starts the Gradiente Expert 2.0 "
        "in BASIC; profile='disk' adds its DDX 3.0 floppy; profile='dos' boots "
        "MSX-DOS 2 (Nextor) from the Sunrise IDE hard disk; profile='msx2plus' "
        "starts a Sony HB-F1XDJ MSX2+ with 128 KiB RAM and built-in floppy. "
        "Set window=true to also open a "
        "visible openMSX window on the user's screen (shared: the user types in it "
        "and the AI drives the same instance). Returns the boot screen.",
        _s({"profile": {"type": "string",
                        "enum": ["basic", "disk", "dos", "msx2plus"],
                        "default": "basic"},
            "window": {"type": "boolean", "default": False}})),
    "msx_attach": (t_attach,
        "Attach to the user's ALREADY-RUNNING openMSX window (shared instance) via "
        "its control socket, instead of spawning a headless one. Use this when the "
        "user opened openMSX themselves (e.g. via ./open-msx.command) and wants to "
        "collaborate on the same live machine. Does not change their throttle/power.",
        _s({})),
    "msx_real_listen": (t_real_listen,
        "Compatibility alias for msx_agent_listen. Listen for a physical MSX "
        "running the ASM agent to connect over TCP. "
        "After it connects, msx_screen and msx_screenshot read RAM/VRAM only "
        "through the agent protocol, without using openMSX control APIs.",
        _s({"host": {"type": "string", "default": "0.0.0.0"},
            "port": {"type": "integer", "default": 6603},
            "timeout": {"type": "number", "default": 60}})),
    "msx_agent_listen": (t_agent_listen,
        "Listen for an MSX resident agent or transparent hardware adapter to "
        "connect over TCP/IPv4. Use this after the user starts "
        "open-msx-mcp.command, or when a hardware adapter is configured as a "
        "TCP client. The MCP protocol is independent of its UART hardware.",
        _s({"host": {"type": "string", "default": "0.0.0.0"},
            "port": {"type": "integer", "default": 6603},
            "timeout": {"type": "number", "default": 60}})),
    "msx_agent_connect": (t_agent_connect,
        "Connect over TCP/IP to an MSX resident agent or transparent adapter "
        "configured as a TCP server. This is the inverse connection direction "
        "of msx_agent_listen and uses the identical transport-neutral protocol.",
        _s({"host": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "default": 6603},
            "timeout": {"type": "number", "default": 60}}, ["host"])),
    "msx_tcp_bench_start": (t_tcp_bench_start,
        "Start one isolated openMSX instance, install the resident ASM "
        "agent, and connect to it through RS232-Net and TCP/IP. A headless "
        "bench is host-muted; window=true enables its visible renderer with "
        "normal sound after the TCP handshake. It remains alive until "
        "msx_shutdown. Supported memory, "
        "hardware, execution, application, and screenshot operations use the "
        "TCP agent path, not openMSX debugger APIs. mode='resident' returns to "
        "DOS and supports pause/inspect/patch/resume of cooperative DOS-launched "
        "software; direct call/run/stop and slot/mapper selection require "
        "mode='monitor'. debug=true is valid only for monitor mode.",
        _s({"host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer", "default": 0},
            "timeout": {"type": "number", "default": 60},
            "window": {"type": "boolean", "default": False},
            "mode": {"type": "string", "enum": ["resident", "monitor"],
                     "default": "resident"},
            "debug": {"type": "boolean", "default": False}})),
    "msx_screen": (t_screen,
        "Return the current MSX text screen (decoded from VRAM, headless).",
        _s({})),
    "msx_status": (t_status,
        "Return the active backend and state. A physical-agent session reports "
        "its resident or foreground-monitor runtime, execution state, selected "
        "transport, and negotiated capabilities.",
        _s({})),
    "msx_pause": (t_pause,
        "Pause the cooperative DOS environment/application under the resident "
        "agent, or code launched by the foreground monitor. The complete "
        "interrupted CPU context remains frozen until msx_resume.",
        _s({})),
    "msx_resume": (t_resume,
        "Resume code paused by the resident real-MSX monitor.", _s({})),
    "msx_stop": (t_stop,
        "Foreground-monitor only: abandon code launched by the agent and return "
        "to its upload monitor. Resident mode rejects this operation because it "
        "would discard the interrupted DOS/application context.", _s({})),
    "msx_io_read": (t_io_read,
        "Real ASM-agent only: read one byte directly from an MSX hardware I/O "
        "port. Port and returned value are decimal integers in the JSON result.",
        _s({"port": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["port"])),
    "msx_io_write": (t_io_write,
        "Real ASM-agent only: write one byte directly to an MSX hardware I/O "
        "port. Optional verify reads the port back; leave it false for "
        "write-only or side-effectful devices.",
        _s({"port": {"type": "integer", "minimum": 0, "maximum": 255},
            "value": {"type": "integer", "minimum": 0, "maximum": 255},
            "verify": {"type": "boolean", "default": False}},
           ["port", "value"])),
    "msx_slot_select": (t_slot_select,
        "Real ASM-agent only: map an encoded primary/expanded slot ID into CPU "
        "page 0 or page 1. Available only in foreground-monitor mode; MemMan "
        "resident hooks cannot make a persistent, safe slot mapping.",
        _s({"page": {"type": "integer", "minimum": 0, "maximum": 1},
            "slot_id": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["page", "slot_id"])),
    "msx_mapper_select": (t_mapper_select,
        "Real ASM-agent only: select a memory-mapper segment for CPU page 0 "
        "or page 1. Available only in foreground-monitor mode because remapping "
        "an interrupted resident program is unsafe.",
        _s({"page": {"type": "integer", "minimum": 0, "maximum": 1},
            "segment": {"type": "integer", "minimum": 0, "maximum": 255}},
           ["page", "segment"])),
    "msx_memory_read": (t_memory_read,
        "Read RAM or VRAM through the resident agent; returns hex. With atomic=true "
        "(default), a running application uses the bounded snapshot lease and "
        "resumes immediately after the read. Older agents require atomic=false. "
        "MemMan resident mode reserves RAM page 1 (0x4000-0x7FFF) "
        "but leaves pages 2 and 3 accessible.",
        _s({"space": {"type": "string", "enum": ["ram", "vram"]},
            "address": {"type": "integer", "minimum": 0},
            "length": {"type": "integer", "minimum": 0},
            "atomic": {"type": "boolean", "default": True}},
           ["space", "address", "length"])),
    "msx_memory_write": (t_memory_write,
        "Write hexadecimal bytes to RAM or VRAM through the resident agent. "
        "Set verify=true to read back and compare. With atomic=true (default), "
        "a running application uses the bounded snapshot lease for the complete "
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
        "reads the hardware key matrix directly will not observe them. Returns "
        "the text screen.",
        _s({"text": {"type": "string"}}, ["text"])),
    "msx_type": (t_type,
        "Type raw text without adding RETURN. A real resident agent uses the "
        "BIOS keyboard ring and waits for each batch to be consumed.",
        _s({"text": {"type": "string"}}, ["text"])),
    "msx_key": (t_key,
        "OpenMSX only: press ESC, RET, STOP, SPACE, SELECT or TAB.",
        _s({"key": {"type": "string"}}, ["key"])),
    "msx_run_basic": (t_run_basic,
        "Enter a full BASIC program one line at a time, then RUN it and return "
        "the text screen. On a real resident target, enters BASIC automatically "
        "when the current screen ends at a DOS prompt. An already-visible BASIC "
        "prompt requires explicit allow_existing_basic=true so an arbitrary "
        "application displaying 'Ok' is not modified. Does NEW first unless "
        "clear=false.",
        _s({"program": {"type": "string"},
            "clear": {"type": "boolean", "default": True},
            "allow_existing_basic": {
                "type": "boolean", "default": False}}, ["program"])),
    "msx_reset": (t_reset,
        "OpenMSX only: reset the MSX and return the boot screen.", _s({})),
    "msx_app_load": (t_app_load,
        "Load an application through the active backend using the shared, "
        "interface-independent loader. Detects msx-ai-app-v1 manifests, COM, "
        "BLOAD BIN and flat 16/32 KiB ROM files by extension/header. Relative "
        "paths are resolved from the project root. Optional execute overrides "
        "the file entry mode; verify reads every segment back (slower over TCP). "
        "A MemMan resident target accepts safe segment transfers but rejects "
        "call/run entry modes and RAM page 1; use the foreground monitor for "
        "agent-launched code, or launch a DOS program normally after install.",
        _s({"path": {"type": "string", "minLength": 1},
            "format": {"type": "string",
                       "enum": ["manifest", "com", "bload", "flat-rom"]},
            "execute": {"type": "string", "enum": ["none", "call", "run"]},
            "verify": {"type": "boolean", "default": False}},
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
        "onto the Nextor hard disk and run it at the A:\\> prompt. Boots the 'dos' "
        "profile automatically. Returns the DOS output.",
        _s({"source": {"type": "string"},
            "name": {"type": "string", "default": "A.COM"},
            "run": {"type": "boolean", "default": True}}, ["source"])),
    "msx_disk_put_text": (t_disk_put_text,
        "OpenMSX only: write a text file into drive A's disk image so BASIC "
        "can LOAD/RUN it. Switches to the disk profile if needed.",
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
            # tools may return a str (wrapped as text) or a ready content list
            content = out if isinstance(out, list) else [{"type": "text", "text": out}]
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
