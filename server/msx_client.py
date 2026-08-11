#!/usr/bin/env python3
"""Optional, cross-platform emulator adapter for openMSX external control.

The physical-agent backend does not import an openMSX executable, open an
openMSX socket, or use this adapter at runtime. Emulator tools instantiate it
explicitly. It spawns openMSX with an isolated OPENMSX_HOME (never touching the
user's own setups by default), parses the XML reply stream reliably, and
exposes high-level emulator helpers. POSIX hosts use ``-control stdio``;
Windows uses openMSX's loopback TCP endpoint with its mandatory SSPI Negotiate
handshake.
"""
import os, subprocess, threading, queue, time, re, html, pathlib, glob, socket, shutil
import ntpath
import sys
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
_LOG = re.compile(r'<log(?:\s+level="([^"]*)")?>(.*?)</log>', re.S)
_TCL_TRUE = frozenset(("1", "true", "on", "yes"))
_CONFIG_MODES = frozenset(("isolated", "user", "overlay"))
_WINDOWS_CONTROL_PORT_MIN = 9938
# openMSX 21 increased CliServer's range to 64 ports (9938..10001).  The
# published manual still documents the historical 21-port range.
_WINDOWS_CONTROL_PORT_MAX = 10001


def _is_windows(platform=None):
    """Return whether *platform* is Windows, without probing socket features."""
    return (sys.platform if platform is None else platform) == "win32"


def _control_transport(platform=None):
    return "tcp_sspi" if _is_windows(platform) else "stdio"


def _uses_unix_control():
    """Whether openMSX publishes a Unix-domain external-control endpoint."""
    return not _is_windows()


def list_sockets():
    """Candidate openMSX control endpoints, newest first.

    Unix hosts publish domain sockets under openmsx-<user>. Windows publishes
    small socket.<pid> files under openmsx-default; each file contains the
    loopback TCP port of that instance. Stale entries are filtered by attach().
    """
    bases = [os.environ.get(v) for v in ("TMPDIR", "TMP", "TEMP")]
    bases += [tempfile.gettempdir()]
    socks = []
    if _uses_unix_control():
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


def _windows_descriptor_pid(path):
    match = re.fullmatch(r"socket\.(\d+)", pathlib.Path(path).name)
    if match is None or int(match.group(1)) <= 0:
        raise OpenMSXError(
            f"invalid openMSX Windows descriptor name: {path}")
    return int(match.group(1))


def _windows_process_image(pid):
    """Best-effort image lookup for a Windows descriptor owner.

    Process inspection can legitimately be denied across integrity levels, so
    ``None`` means unknown rather than dead.  A known image is still useful for
    rejecting an unrelated process that reused a stale ``socket.<pid>`` file.
    """
    if not _is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD))
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(
            query_limited_information, False, int(pid))
        if not handle:
            return None
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(capacity)):
                return None
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _connect_windows_sspi(host, port, *, timeout=5.0):
    """Connect to openMSX's authenticated Windows TCP attach endpoint."""
    try:
        if __package__:
            from .windows_sspi import connect_openmsx_tcp
        else:  # pragma: no cover - repository-style import is used in tests
            from windows_sspi import connect_openmsx_tcp
    except (ImportError, AttributeError) as exc:
        raise OpenMSXError(
            "booting or attaching to openMSX on Windows requires SSPI "
            "Negotiate support; this MSX-AI installation does not provide "
            "the packaged Windows SSPI transport.") from exc
    return connect_openmsx_tcp(host, port, timeout=timeout)


def _windows_attach_supported():
    try:
        if __package__:
            from .windows_sspi import connect_openmsx_tcp
        else:  # pragma: no cover
            from windows_sspi import connect_openmsx_tcp
    except (ImportError, AttributeError):
        return False
    return callable(connect_openmsx_tcp)


def _open_control_endpoint(path):
    """Connect to one published openMSX control endpoint."""
    if _uses_unix_control():
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            control.connect(path)
        except Exception:
            control.close()
            raise
        return control

    endpoint = pathlib.Path(path)
    pid = _windows_descriptor_pid(endpoint)
    if endpoint.is_symlink() or not endpoint.is_file():
        raise OpenMSXError(
            f"invalid openMSX Windows control descriptor: {path}")
    try:
        raw_port = endpoint.read_text(encoding="ascii").strip()
        port = int(raw_port, 10)
    except (OSError, UnicodeError, ValueError) as exc:
        raise OpenMSXError(
            f"invalid openMSX Windows control endpoint {path}: {exc}") from exc
    if not _WINDOWS_CONTROL_PORT_MIN <= port <= _WINDOWS_CONTROL_PORT_MAX:
        raise OpenMSXError(
            "openMSX Windows control port is outside "
            f"{_WINDOWS_CONTROL_PORT_MIN}..{_WINDOWS_CONTROL_PORT_MAX}: {port}")
    image = _windows_process_image(pid)
    if image is not None and ntpath.basename(image).casefold() != "openmsx.exe":
        raise OpenMSXError(
            f"control descriptor {path} belongs to a non-openMSX process: "
            f"{image}")
    # openMSX 21 authenticates Windows socket clients with SSPI before it
    # accepts XML. Sending raw XML to this TCP port can never establish a
    # valid control session and could accidentally target another service.
    return _connect_windows_sspi("127.0.0.1", port, timeout=5.0)


class OpenMSXError(RuntimeError):
    pass


def _windows_registry_binary_candidates():
    if not _is_windows():
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - only available on Windows
        return []

    candidates = []
    app_paths = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\openmsx.exe",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\openmsx.exe",
    )
    access_modes = [winreg.KEY_READ]
    for attribute in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, attribute, 0)
        if flag:
            access_modes.append(winreg.KEY_READ | flag)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in app_paths:
            for access in access_modes:
                try:
                    with winreg.OpenKey(root, key_name, 0, access) as key:
                        value, _kind = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip().strip('"'))
    return candidates


def _binary_candidates(platform=None):
    """Return executable candidates in priority order, without writing."""
    platform = sys.platform if platform is None else platform
    candidates = []
    explicit = os.environ.get("OPENMSX_BIN")
    if explicit and explicit.strip():
        candidates.append(os.path.expandvars(
            os.path.expanduser(explicit.strip().strip('"'))))

    for command in (("openmsx.exe", "openmsx") if _is_windows(platform)
                    else ("openmsx",)):
        found = shutil.which(command)
        if found:
            candidates.append(found)

    if _is_windows(platform):
        candidates.extend(_windows_registry_binary_candidates())
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(variable)
            if base:
                candidates.append(ntpath.join(
                    base, "openMSX", "openmsx.exe"))
    elif platform == "darwin":
        candidates.extend((
            "/Applications/openMSX.app/Contents/MacOS/openmsx",
            str(pathlib.Path.home() /
                "Applications/openMSX.app/Contents/MacOS/openmsx"),
        ))

    result = []
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _executable_exists(executable):
    path = os.fspath(executable)
    return pathlib.Path(path).is_file() or shutil.which(path) is not None


def _default_binary(platform=None):
    """Resolve openMSX only after an emulator operation is selected."""
    candidates = _binary_candidates(platform)
    for candidate in candidates:
        if _executable_exists(candidate):
            return candidate
    if candidates:
        # Keep the most useful attempted path for an actionable Popen error.
        return candidates[0]
    return "openmsx.exe" if _is_windows(platform) else "openmsx"


def _windows_documents_dir():
    """Return Windows' current Documents known folder without writing."""
    if not _is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetFolderPathW.argtypes = (
            wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
            wintypes.DWORD, wintypes.LPWSTR)
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        documents = ctypes.create_unicode_buffer(32768)
        # CSIDL_PERSONAL is the same known-folder lookup used by openMSX.
        result = shell32.SHGetFolderPathW(None, 5, None, 0, documents)
        if result == 0 and documents.value:
            return documents.value
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _default_user_openmsx_home(platform=None):
    """Return the conventional user home used by openMSX (read-only lookup)."""
    explicit = os.environ.get("OPENMSX_HOME")
    if explicit and explicit.strip():
        return pathlib.Path(os.path.expandvars(
            os.path.expanduser(explicit.strip().strip('"'))))
    if _is_windows(platform):
        documents = _windows_documents_dir()
        if documents:
            return pathlib.Path(ntpath.join(documents, "openMSX"))
        profile = os.environ.get("USERPROFILE") or os.fspath(pathlib.Path.home())
        return pathlib.Path(ntpath.join(profile, "Documents", "openMSX"))
    return pathlib.Path.home() / ".openMSX"


def _merge_overlay_tree(source, destination):
    """Add missing user files to a temporary overlay without mutating source."""
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if target.exists() or target.is_symlink():
            if item.is_dir() and target.is_dir():
                _merge_overlay_tree(item, target)
            continue
        try:
            os.symlink(item, target, target_is_directory=item.is_dir())
            continue
        except (OSError, NotImplementedError):
            pass
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True)
        else:
            try:
                os.link(item, target)
            except OSError:
                shutil.copy2(item, target)


class OpenMSX:
    def __init__(self, machine="Gradiente_Expert20", extensions=("DDX_3.0",),
                 harddisk=None, home=None, bin=None, *,
                 config_mode="isolated", platform=None):
        if config_mode not in _CONFIG_MODES:
            choices = ", ".join(sorted(_CONFIG_MODES))
            raise ValueError(f"config_mode must be one of: {choices}")
        self.platform = sys.platform if platform is None else platform
        self.config_mode = config_mode
        self.user_home = str(_default_user_openmsx_home(self.platform))
        if config_mode == "user" and home is None:
            home = self.user_home
        elif home is None:
            home = DEFAULT_HOME
        self.machine = machine
        self.extensions = list(extensions)
        self.harddisk = harddisk        # path to an IDE/hda image (MSX-DOS/Nextor)
        self.home = str(home)
        self.effective_home = self.home if config_mode != "user" else None
        self.bin = _default_binary(self.platform) if bin is None else os.fspath(bin)
        self.control_transport = _control_transport(self.platform)
        # ``transport`` is a stable, short alias for callers and diagnostics.
        self.transport = self.control_transport
        self.proc = None
        self.sock = None            # set when attached to an existing instance
        self.socket_path = None     # exact selected external control socket
        self.attached = False
        self._buf = ""
        self._output_tail = ""
        self._output_lock = threading.Lock()
        self._reader_eof = threading.Event()
        self._replies = queue.Queue()
        # High-level operations may hold the channel across several commands
        # and then re-enter cmd(). This keeps debugger snapshots indivisible
        # without deadlocking the command path.
        self._lock = threading.RLock()
        self._runtime_settings_dir = None
        self._runtime_overlay_dir = None

    def _prepare_runtime_settings(self, settings_home=None):
        """Return a per-process settings file that openMSX may mutate freely.

        Machine and extension definitions still come from the isolated project
        OPENMSX_HOME, but volatile console settings no longer dirty its tracked
        ``share/settings.xml``.  Each spawned instance gets its own copy, which
        also prevents concurrent headless sessions from racing on one file.
        """
        self._runtime_settings_dir = tempfile.TemporaryDirectory(
            prefix="msx-ai-openmsx-")
        target = pathlib.Path(self._runtime_settings_dir.name) / "settings.xml"
        settings_root = pathlib.Path(
            self.home if settings_home is None else settings_home) / "share"
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

    def _prepare_overlay_home(self):
        """Build a temporary MCP-first view of project and user file pools."""
        self._runtime_overlay_dir = tempfile.TemporaryDirectory(
            prefix="msx-ai-openmsx-overlay-")
        target = pathlib.Path(self._runtime_overlay_dir.name)
        project_share = pathlib.Path(self.home) / "share"
        if project_share.is_dir():
            shutil.copytree(project_share, target / "share", symlinks=True)
        _merge_overlay_tree(
            pathlib.Path(self.user_home) / "share", target / "share")
        self.effective_home = str(target)
        return target

    def _cleanup_overlay_home(self):
        temporary = self._runtime_overlay_dir
        self._runtime_overlay_dir = None
        if temporary is not None:
            temporary.cleanup()
        self.effective_home = self.home if self.config_mode != "user" else None

    def _machine_config_candidates(self):
        machine_file = f"{self.machine}.xml"
        roots = []
        if self.config_mode in ("isolated", "overlay"):
            roots.append(pathlib.Path(self.home) / "share")
        if self.config_mode in ("user", "overlay"):
            roots.append(pathlib.Path(self.user_home) / "share")

        executable = pathlib.Path(self.bin)
        if executable.is_file():
            roots.extend((
                executable.parent / "share",
                executable.parent.parent / "Resources" / "share",
                executable.parent.parent / "share",
            ))
        candidates = []
        for root in roots:
            candidate = root / "machines" / machine_file
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def preflight(self):
        """Return a read-only platform/configuration readiness report."""
        executable_found = _executable_exists(self.bin)
        candidates = self._machine_config_candidates()
        machine_config_found = any(path.is_file() for path in candidates)
        attach_transport = "tcp_sspi" if _is_windows(self.platform) else "unix_socket"
        attach_supported = (
            _windows_attach_supported() if _is_windows(self.platform) else
            hasattr(socket, "AF_UNIX"))
        boot_supported = (
            _windows_attach_supported() if _is_windows(self.platform) else
            True)
        problems = []
        if not executable_found:
            attempted = _binary_candidates(self.platform)
            detail = ", ".join(attempted) if attempted else self.bin
            problems.append(
                f"openMSX executable not found (tried: {detail})")
        if not machine_config_found:
            problems.append(
                f"machine configuration {self.machine}.xml was not found "
                "in the known project, user, or installation data paths")
        if _is_windows(self.platform) and not boot_supported:
            problems.append(
                "Windows boot and attach require the SSPI Negotiate "
                "transport, which is unavailable in this installation")
        return {
            "ready": executable_found and machine_config_found and boot_supported,
            "platform": self.platform,
            "control_transport": self.control_transport,
            "transport": self.transport,
            "attach_transport": attach_transport,
            "attach_supported": attach_supported,
            "control_transport_supported": boot_supported,
            "boot_supported": boot_supported,
            "config_mode": self.config_mode,
            "machine": self.machine,
            "executable": self.bin,
            "executable_found": executable_found,
            "home": self.home,
            "user_home": self.user_home,
            "home_exists": pathlib.Path(self.home).is_dir(),
            "machine_config_found": machine_config_found,
            "machine_config_candidates": [str(path) for path in candidates],
            "problems": problems,
        }

    def _connect_owned_windows_control(self, timeout):
        """Attach to the owned process's authenticated loopback endpoint.

        Current Windows release binaries try to redirect stdout to the parent
        console. Depending on the launcher, that can sever an inherited output
        pipe, so ``-control pipe`` is not a reliable bidirectional channel.
        openMSX publishes a supported TCP/SSPI endpoint for every process, so
        use that channel directly.
        """
        deadline = time.monotonic() + timeout
        expected_name = f"socket.{self.proc.pid}"
        last_error = None
        while time.monotonic() < deadline:
            returncode = self._process_returncode()
            if returncode is not None:
                raise OpenMSXError(self._failure_context(
                    f"openMSX exited with code {returncode} before publishing "
                    "its Windows control descriptor"))
            candidates = [
                path for path in list_sockets()
                if pathlib.Path(path).name == expected_name
            ]
            for path in candidates:
                try:
                    return path, _open_control_endpoint(path)
                except (OSError, OpenMSXError) as exc:
                    last_error = exc
            time.sleep(0.05)
        detail = f"timed out waiting for owned openMSX descriptor {expected_name}"
        if last_error is not None:
            detail += f" ({last_error})"
        raise OpenMSXError(self._failure_context(detail))

    # ---- lifecycle -----------------------------------------------------
    def start(self, *, headless=True, startup_timeout=10.0):
        """Spawn an owned openMSX instance over the host-native transport.

        Headless instances mute openMSX's host mixer and select renderer none.
        PSG/SCC/OPLL state, I/O ports and MSX timing remain untouched, so
        programs continue to run their normal sound routines. Each spawned
        process has temporary settings and exits while still muted, so no
        audible shutdown window or persisted setting can leak into a later
        visible session.
        """
        self._reader_eof.clear()
        self._buf = ""
        self._output_tail = ""
        self._replies = queue.Queue()
        env = dict(os.environ)
        try:
            if self.config_mode == "isolated":
                # Installed wheels materialize only the public, ROM-free
                # templates, and only when an emulator is about to start.
                prepare_openmsx_home(self.home)
                self.effective_home = self.home
                env["OPENMSX_HOME"] = self.home
            elif self.config_mode == "overlay":
                prepare_openmsx_home(self.home)
                overlay_home = self._prepare_overlay_home()
                env["OPENMSX_HOME"] = str(overlay_home)
            else:
                # User mode deliberately inherits openMSX's native lookup and
                # any OPENMSX_HOME the user already supplied.  MSX-AI does not
                # define or replace it.
                self.effective_home = None

            argv = [self.bin]
            if self.control_transport == "stdio":
                argv += ["-control", "stdio"]
            argv += ["-machine", self.machine]
            for ext in self.extensions:
                argv += ["-ext", ext]
            if self.harddisk:
                argv += ["-hda", str(self.harddisk)]
            settings_home = (
                self.effective_home if self.effective_home is not None
                else self.home)
            runtime_settings = self._prepare_runtime_settings(settings_home)
            argv += ["-setting", str(runtime_settings)]
            if headless:
                # Execute this before the client begins booting the machine,
                # preventing an audible interval or visible renderer window.
                argv += ["-command", "set mute on; set renderer none"]

            uses_stdio = self.control_transport == "stdio"
            self.proc = subprocess.Popen(
                argv,
                stdin=(subprocess.PIPE if uses_stdio else subprocess.DEVNULL),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env, bufsize=0,
            )
            if self.control_transport == "tcp_sspi":
                # Windows release binaries normally redirect this stream to
                # CONOUT$, producing immediate EOF. Drain it separately anyway
                # so early configuration/ROM errors remain diagnosable on
                # builds or hosts where inherited stdout stays available.
                threading.Thread(
                    target=self._capture_process_output,
                    args=(self.proc.stdout,), daemon=True).start()
                self.socket_path, self.sock = (
                    self._connect_owned_windows_control(startup_timeout))
        except OSError as exc:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
                self.socket_path = None
            self._terminate_failed_start()
            self._cleanup_runtime_settings()
            self._cleanup_overlay_home()
            raise OpenMSXError(
                f"could not start openMSX executable {self.bin!r} "
                f"for machine {self.machine!r}: {exc}") from exc
        except Exception as exc:
            detail = (self._failure_context(str(exc))
                      if self.proc is not None else str(exc))
            if self.sock is not None:
                self.sock.close()
                self.sock = None
                self.socket_path = None
            self._terminate_failed_start()
            self._cleanup_runtime_settings()
            self._cleanup_overlay_home()
            if isinstance(exc, OpenMSXError):
                raise OpenMSXError(detail) from exc
            raise
        try:
            threading.Thread(target=self._reader, daemon=True).start()
            self._write(b"<openmsx-control>\n")
            time.sleep(0.3)
            returncode = self._process_returncode()
            if returncode is not None:
                raise OpenMSXError(self._failure_context(
                    f"openMSX exited with code {returncode} during startup"))
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
                renderer = self.cmd("set renderer").strip().lower()
                if renderer != "none":
                    raise OpenMSXError(
                        "openMSX rejected the mandatory headless renderer")
            except Exception as exc:
                # A headless instance is not allowed to continue if host mute
                # cannot be proved active. close() terminates it immediately.
                self.close()
                if isinstance(exc, OpenMSXError):
                    raise
                raise OpenMSXError(
                    f"could not enforce mandatory headless settings: {exc}") from exc
        return self

    def attach(self, sockpath=None):
        """Attach to an already-running openMSX without taking ownership.

        POSIX hosts use the published Unix-domain socket. Windows uses the
        authenticated loopback TCP descriptor and SSPI Negotiate handshake.

        With no explicit path, attachment is allowed only when exactly one
        discovered socket is live. This prevents a destructive MCP call from
        silently selecting the wrong emulator when several windows are open.
        """
        self._reader_eof.clear()
        self._buf = ""
        self._output_tail = ""
        self._replies = queue.Queue()
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
                "choose one implicitly. Call msx_local_attach again with one of "
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
            if self.proc is None or self.proc.stdin is None:
                raise BrokenPipeError("openMSX control input is not available")
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read_chunk(self):
        if self.sock is not None:
            return self.sock.recv(4096)
        return self.proc.stdout.read(4096)

    def _append_output(self, decoded):
        with self._output_lock:
            self._output_tail = (self._output_tail + decoded)[-8192:]

    def _capture_process_output(self, stream=None):
        if stream is None:
            stream = getattr(self.proc, "stdout", None)
        if stream is None:
            return
        while True:
            try:
                chunk = stream.read(4096)
            except OSError:
                return
            if not chunk:
                return
            self._append_output(chunk.decode(errors="replace"))

    def _reader(self):
        while True:
            try:
                chunk = self._read_chunk()
            except OSError:
                self._reader_eof.set()
                break
            if not chunk:
                self._reader_eof.set()
                break
            decoded = chunk.decode(errors="replace")
            self._append_output(decoded)
            self._buf += decoded
            # extract every complete <reply>…</reply> in order
            while True:
                m = _REPLY.search(self._buf)
                if not m:
                    break
                self._replies.put((m.group(1), html.unescape(m.group(2))))
                self._buf = self._buf[m.end():]

    def _process_returncode(self):
        proc = self.proc
        poll = getattr(proc, "poll", None) if proc is not None else None
        return poll() if callable(poll) else None

    def _diagnostic_output(self):
        with self._output_lock:
            output = self._output_tail
        logs = []
        for level, message in _LOG.findall(output):
            clean = html.unescape(re.sub(r"\s+", " ", message)).strip()
            if clean:
                logs.append(f"{level or 'log'}: {clean}")
        if logs:
            return " | ".join(logs[-5:])
        clean = html.unescape(re.sub(r"<[^>]+>", " ", output))
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[-1000:]

    def _failure_context(self, message):
        detail = (
            f"{message}; executable={self.bin!r}; machine={self.machine!r}; "
            f"transport={self.control_transport}; config_mode={self.config_mode}")
        output = self._diagnostic_output()
        if output:
            detail += f"; openMSX output: {output}"
        return detail

    def _terminate_failed_start(self):
        proc = self.proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        for pipe in (getattr(proc, "stdin", None),
                     getattr(proc, "stdout", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
        self.proc = None

    # ---- raw command ---------------------------------------------------
    def cmd(self, tcl, timeout=15):
        # The -control channel is XML: the command text must be XML-escaped or
        # characters like & (e.g. BASIC's &H / &B literals) and < > corrupt the
        # stream and openMSX never replies. openMSX unescapes it back to Tcl.
        payload = (tcl.replace("&", "&amp;")
                      .replace("<", "&lt;").replace(">", "&gt;"))
        with self._lock:
            self._write(f"<command>{payload}</command>\n".encode())
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpenMSXError(self._failure_context(
                        f"timeout waiting for reply to: {tcl}"))
                try:
                    status, text = self._replies.get(
                        timeout=min(remaining, 0.1))
                    break
                except queue.Empty:
                    returncode = self._process_returncode()
                    if returncode is not None:
                        raise OpenMSXError(self._failure_context(
                            f"openMSX exited with code {returncode} while "
                            f"waiting for reply to: {tcl}"))
                    if self._reader_eof.is_set():
                        raise OpenMSXError(self._failure_context(
                            f"openMSX control channel closed while waiting "
                            f"for reply to: {tcl}"))
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
    KEY_CHORDS = {
        # BREAKX scans the physical matrix; INTFLG/key-buffer injection is not
        # equivalent while the foreground monitor owns the CPU.
        "CTRL+STOP": ((6, 0x02), (7, 0x10)),
    }

    def press(self, key):
        key = key.upper()
        chord = self.KEY_CHORDS.get(key)
        if chord is not None:
            downs = "; ".join(
                f"keymatrixdown {row} {mask}" for row, mask in chord)
            ups = "; ".join(
                f"keymatrixup {row} {mask}"
                for row, mask in reversed(chord))
            self.cmd(f"{downs}; after time 0.10 {{{ups}}}")
            return
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
            self._cleanup_overlay_home()
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
        for pipe in (getattr(proc, "stdin", None),
                     getattr(proc, "stdout", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.socket_path = None
        self.proc = None
        self._cleanup_runtime_settings()
        self._cleanup_overlay_home()


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
