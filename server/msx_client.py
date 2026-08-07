#!/usr/bin/env python3
"""Optional emulator adapter for openMSX's -control stdio protocol.

The physical-agent backend does not import an openMSX executable, open an
openMSX socket, or use this adapter at runtime. Emulator tools instantiate it
explicitly. It spawns openMSX with an isolated OPENMSX_HOME (never touching the
user's own setups), parses the XML reply stream reliably, and exposes
high-level emulator helpers.
"""
import os, subprocess, threading, queue, time, re, html, pathlib, glob, socket, shutil
import tempfile

if __package__:
    from .msx_cpu import capture_openmsx_cpu
    from .paths import (
        ensure_directory,
        openmsx_home,
        prepare_openmsx_home,
        source_root,
        user_root,
        work_root,
    )
else:  # pragma: no cover - exercised by repository-style imports
    from msx_cpu import capture_openmsx_cpu
    from paths import (
        ensure_directory,
        openmsx_home,
        prepare_openmsx_home,
        source_root,
        user_root,
        work_root,
    )

PROJ = source_root() or user_root()
DEFAULT_HOME = openmsx_home()

_REPLY = re.compile(r'<reply result="(ok|nok)">(.*?)</reply>', re.S)
_TCL_TRUE = frozenset(("1", "true", "on", "yes"))


def list_sockets():
    """Candidate openMSX control endpoints, newest first.

    Unix hosts publish domain sockets under openmsx-<user>. Windows publishes
    small socket.<pid> files under openmsx-default; each file contains the
    loopback TCP port of that instance. Stale entries are filtered by attach().
    """
    bases = [os.environ.get(v) for v in ("TMPDIR", "TMP", "TEMP")]
    bases += [tempfile.gettempdir()]
    socks = []
    if hasattr(socket, "AF_UNIX"):
        user = os.environ.get("USER", "")
        bases += ["/tmp"]
        for base in bases:
            if base:
                socks += glob.glob(
                    os.path.join(base, f"openmsx-{user}", "socket.*"))
    else:
        for base in bases:
            if base:
                socks += glob.glob(
                    os.path.join(base, "openmsx-default", "socket.*"))
    socks = list(dict.fromkeys(socks))          # dedupe, keep order
    candidates = []
    for path in socks:
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            # A process may exit between glob() and stat(). attach() will also
            # filter socket files that remain on disk but refuse a connection.
            continue
    return [path for _mtime, path in sorted(candidates, reverse=True)]


def _open_control_endpoint(path):
    """Connect to one published openMSX control endpoint."""
    if hasattr(socket, "AF_UNIX"):
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            control.connect(path)
        except Exception:
            control.close()
            raise
        return control

    try:
        raw_port = pathlib.Path(path).read_text(encoding="ascii").strip()
        port = int(raw_port, 10)
    except (OSError, UnicodeError, ValueError) as exc:
        raise OpenMSXError(
            f"invalid openMSX Windows control endpoint {path}: {exc}") from exc
    if not 9938 <= port <= 9958:
        raise OpenMSXError(
            f"openMSX Windows control port is outside 9938..9958: {port}")
    control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        control.connect(("127.0.0.1", port))
    except Exception:
        control.close()
        raise
    return control


class OpenMSXError(RuntimeError):
    pass


def _default_binary():
    """Resolve openMSX only after an emulator operation is selected."""
    return (os.environ.get("OPENMSX_BIN") or shutil.which("openmsx") or
            "/Applications/openMSX.app/Contents/MacOS/openmsx")


class OpenMSX:
    def __init__(self, machine="Gradiente_Expert20", extensions=("DDX_3.0",),
                 harddisk=None, home=DEFAULT_HOME, bin=None):
        self.machine = machine
        self.extensions = list(extensions)
        self.harddisk = harddisk        # path to an IDE/hda image (MSX-DOS/Nextor)
        self.home = str(home)
        self.bin = _default_binary() if bin is None else bin
        self.proc = None
        self.sock = None            # set when attached to an existing instance
        self.socket_path = None     # exact selected external control socket
        self.attached = False
        self._buf = ""
        self._replies = queue.Queue()
        # High-level operations may hold the channel across several commands
        # and then re-enter cmd(). This keeps debugger snapshots indivisible
        # without deadlocking the command path.
        self._lock = threading.RLock()
        self._runtime_settings_dir = None

    def _prepare_runtime_settings(self):
        """Return a per-process settings file that openMSX may mutate freely.

        Machine and extension definitions still come from the isolated project
        OPENMSX_HOME, but volatile console settings no longer dirty its tracked
        ``share/settings.xml``.  Each spawned instance gets its own copy, which
        also prevents concurrent headless sessions from racing on one file.
        """
        self._runtime_settings_dir = tempfile.TemporaryDirectory(
            prefix="msx-ai-openmsx-")
        target = pathlib.Path(self._runtime_settings_dir.name) / "settings.xml"
        settings_root = pathlib.Path(self.home) / "share"
        local_source = settings_root / "settings.local.xml"
        source = (local_source if local_source.is_file() else
                  settings_root / "settings.xml")
        if source.is_file():
            shutil.copyfile(source, target)
        else:
            target.write_text(
                "<!DOCTYPE settings SYSTEM 'settings.dtd'>\n"
                "<settings><settings/><bindings/><shortcuts/></settings>\n",
                encoding="utf-8")
        return target

    def _cleanup_runtime_settings(self):
        temporary = self._runtime_settings_dir
        self._runtime_settings_dir = None
        if temporary is not None:
            temporary.cleanup()

    # ---- lifecycle -----------------------------------------------------
    def start(self, *, headless=True):
        """Spawn openMSX over a stdio control pipe.

        Headless instances mute only openMSX's host mixer.  PSG/SCC/OPLL state,
        I/O ports and MSX timing remain untouched, so programs continue to run
        their normal sound routines. Each spawned process has temporary settings
        and exits while still muted, so no audible shutdown window or persisted
        setting can leak into a later visible session.
        """
        # Installed wheels materialize only the eight public, ROM-free openMSX
        # templates, and only when an emulator is actually about to start.
        prepare_openmsx_home(self.home)
        env = dict(os.environ, OPENMSX_HOME=self.home)
        argv = [self.bin, "-control", "stdio", "-machine", self.machine]
        for ext in self.extensions:
            argv += ["-ext", ext]
        if self.harddisk:
            argv += ["-hda", str(self.harddisk)]
        runtime_settings = self._prepare_runtime_settings()
        argv += ["-setting", str(runtime_settings)]
        if headless:
            # Execute this as an openMSX startup command, before the control
            # client begins booting the machine.  This avoids even a short
            # audible interval while preserving the complete emulated sound
            # hardware state. The per-process settings copy is discarded after
            # exit, so the process can and must remain muted for its full life.
            argv += [
                "-command",
                "set mute on",
            ]
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env, bufsize=0,
            )
        except Exception:
            self._cleanup_runtime_settings()
            raise
        try:
            threading.Thread(target=self._reader, daemon=True).start()
            self._write(b"<openmsx-control>\n")
            time.sleep(0.3)
        except Exception:
            # Popen succeeded, so this object owns a live process even when
            # control-channel setup fails.  Always terminate it before
            # reporting the startup error; otherwise a failed boot can leak a
            # hidden emulator process.
            self.close()
            raise
        if headless:
            try:
                muted = self.cmd("set mute").strip().lower()
                if muted not in _TCL_TRUE:
                    raise OpenMSXError(
                        "openMSX rejected the mandatory headless host mute")
            except Exception as exc:
                # A headless instance is not allowed to continue if host mute
                # cannot be proved active. close() terminates it immediately.
                self.close()
                if isinstance(exc, OpenMSXError):
                    raise
                raise OpenMSXError(
                    f"could not enable mandatory headless host mute: {exc}") from exc
        return self

    def attach(self, sockpath=None):
        """Attach to an ALREADY-RUNNING openMSX (e.g. the user's window) via its
        UNIX control socket. Shares the same live instance; does not spawn.

        With no explicit path, attachment is allowed only when exactly one
        discovered socket is live. This prevents a destructive MCP call from
        silently selecting the wrong emulator when several windows are open.
        """
        discovered = list_sockets()
        if sockpath is not None:
            requested = pathlib.Path(os.fspath(sockpath)).resolve(strict=False)
            matches = [candidate for candidate in discovered
                       if pathlib.Path(candidate).resolve(strict=False) == requested]
            if not matches:
                raise OpenMSXError(
                    "requested openMSX socket is not among the discovered "
                    f"control sockets: {sockpath}")
            candidates = [matches[0]]
        else:
            candidates = discovered
        if not candidates:
            raise OpenMSXError(
                "no running openMSX found. Launch one first "
                "(e.g. ./open-msx.command) so its control socket exists.")
        last = None
        connected = []
        for path in candidates:                 # skip stale/dead socket files
            try:
                sk = _open_control_endpoint(path)
                connected.append((path, sk))
            except (OSError, OpenMSXError) as e:
                last = e
        if not connected:
            raise OpenMSXError(f"could not connect to any openMSX socket ({last})")
        if sockpath is None and len(connected) > 1:
            for _path, sk in connected:
                sk.close()
            paths = ", ".join(path for path, _sk in connected)
            raise OpenMSXError(
                "multiple running openMSX instances were found; refusing to "
                "choose one implicitly. Call msx_attach again with one of "
                f"these socket_path values: {paths}")
        selected_path, self.sock = connected[0]
        self.socket_path = selected_path
        self.attached = True
        try:
            threading.Thread(target=self._reader, daemon=True).start()
            self._write(b"<openmsx-control>\n")
            time.sleep(0.3)
        except Exception:
            self.sock.close()
            self.sock = None
            self.socket_path = None
            self.attached = False
            raise
        return self

    # ---- transport helpers ---------------------------------------------
    def _write(self, data):
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read_chunk(self):
        if self.sock is not None:
            return self.sock.recv(4096)
        return self.proc.stdout.read(4096)

    def _reader(self):
        while True:
            try:
                chunk = self._read_chunk()
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk.decode(errors="replace")
            # extract every complete <reply>…</reply> in order
            while True:
                m = _REPLY.search(self._buf)
                if not m:
                    break
                self._replies.put((m.group(1), html.unescape(m.group(2))))
                self._buf = self._buf[m.end():]

    # ---- raw command ---------------------------------------------------
    def cmd(self, tcl, timeout=15):
        # The -control channel is XML: the command text must be XML-escaped or
        # characters like & (e.g. BASIC's &H / &B literals) and < > corrupt the
        # stream and openMSX never replies. openMSX unescapes it back to Tcl.
        payload = (tcl.replace("&", "&amp;")
                      .replace("<", "&lt;").replace(">", "&gt;"))
        with self._lock:
            self._write(f"<command>{payload}</command>\n".encode())
            try:
                status, text = self._replies.get(timeout=timeout)
            except queue.Empty:
                raise OpenMSXError(f"timeout waiting for reply to: {tcl}")
            if status == "nok":
                raise OpenMSXError(text.strip())
            return text

    # ---- high level ----------------------------------------------------
    def enable_keybuf(self):
        # Inject text straight into the MSX keyboard buffer instead of the key
        # matrix: types ANY ASCII char reliably, independent of layout.
        self.cmd("set default_type_proc type_via_keybuf")

    def power_on(self):
        self.cmd("set throttle off")   # run as fast as the host allows
        self.enable_keybuf()
        self.cmd("set power on")

    def emutime(self):
        return float(self.cmd("machine_info time"))

    def advance(self, seconds, poll=0.05, wall_timeout=30):
        """Advance at least `seconds` of *emulated* time (throttle off = fast)."""
        target = self.emutime() + seconds
        deadline = time.time() + wall_timeout
        while self.emutime() < target:
            if time.time() > deadline:
                break
            time.sleep(poll)

    @staticmethod
    def _esc(text):
        # Escape for a Tcl double-quoted string: backslash first, then the chars
        # that trigger Tcl substitution inside quotes -- " $ [ -- otherwise BASIC
        # tokens like HEX$( or arrays A[ get eaten by the Tcl interpreter.
        return (text.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("$", "\\$").replace("[", "\\["))

    def type(self, text):
        # openMSX 'type' feeds characters through the keyboard matrix
        self.cmd(f'type "{self._esc(text)}"')

    def type_line(self, text):
        """Type a line and press Enter (Tcl \\r -> CR, openMSX maps it to RETURN)."""
        self.cmd(f'type "{self._esc(text)}\\r"')

    def type_lines(self, lines):
        """Type logical lines without reading the screen between them.

        A small emulated-time barrier after each Return prevents MSX-BASIC
        from clearing characters from the following line while it processes
        the current one.
        """
        if isinstance(lines, (str, bytes, bytearray)):
            raise TypeError("lines must be an iterable of strings")
        try:
            lines = tuple(lines)
        except TypeError as exc:
            raise TypeError("lines must be an iterable of strings") from exc
        for line in lines:
            if not isinstance(line, str):
                raise TypeError("each line must be a string")
            if "\r" in line or "\n" in line:
                raise ValueError("each item must contain exactly one logical line")
            self.type_line(line)
            self.advance(0.3)
        return sum(len(line) + 1 for line in lines)

    # MSX keyboard matrix positions (row, mask) for the special keys we need
    KEYS = {"RET": (7, 0x80), "ESC": (7, 0x04), "SPACE": (8, 0x01),
            "STOP": (7, 0x10), "SELECT": (7, 0x40), "TAB": (7, 0x08)}

    def press(self, key):
        row, mask = self.KEYS[key]
        self.cmd(f"keymatrixdown {row} {mask}; "
                 f"after time 0.06 {{keymatrixup {row} {mask}}}")

    def type_enter(self, text=""):
        self.type_line(text)

    def insert_disk(self, path, drive="diska"):
        self.cmd(f'{drive} {{{path}}}')

    def screen_mode(self):
        return int(self.cmd("get_screen_mode_number"))

    def cpu_snapshot(self):
        """Return one exact debugger snapshot while preserving run/break state."""
        with self._lock:
            return capture_openmsx_cpu(self)

    def read_screen(self):
        """Decode the current text screen (SCREEN 0/1) from VRAM.

        Returns a list of rows (str). Uses the VDP name-table base (R2) and
        picks 40- or 32-column width from the screen mode. Returns hex from
        Tcl (single line, parser-safe) and formats in Python.
        """
        mode = self.screen_mode()
        width = 40 if mode == 0 else 32
        r2 = int(self.cmd('debug read "VDP regs" 2'))
        base = (r2 & 0x7F) << 10
        size = width * 24
        hexstr = self.cmd(f'set d [debug read_block VRAM {base} {size}]; '
                          f'binary scan $d H* h; set h')
        data = bytes.fromhex(hexstr.strip())
        rows = []
        for r in range(24):
            row = data[r * width:(r + 1) * width]
            rows.append("".join(chr(b) if 32 <= b < 127 else " " for b in row).rstrip())
        return rows

    def screen_text(self):
        return "\n".join(self.read_screen())

    def screenshot(self, path):
        return self.cmd(f'screenshot {path}')

    def close(self):
        if self.attached:
            # Just disconnect: leave the user's openMSX window running.
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.socket_path = None
            self.attached = False
            return
        proc = self.proc
        if proc is None:
            self._cleanup_runtime_settings()
            return
        try:
            self.cmd("quit", timeout=3)
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # This process was spawned and is owned by this object.  A
                # final kill prevents leaked emulators and races while their
                # temporary OPENMSX_HOME is being removed by integration tests.
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        self.proc = None
        self._cleanup_runtime_settings()


if __name__ == "__main__":
    m = OpenMSX().start()
    m.power_on()
    m.advance(6)
    print("emulated time :", round(m.emutime(), 1), "s")
    print("screen mode   :", m.screen_mode())
    shot = ensure_directory(work_root()) / "boot.png"
    try:
        print("screenshot    :", m.screenshot(str(shot)), "->", shot.exists())
    except OpenMSXError as e:
        print("screenshot ERR:", e)
    m.close()
