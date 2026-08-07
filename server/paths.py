"""Filesystem locations used by the installed package and source checkout.

The package directory is immutable. Runtime state belongs below
``MSX_AI_STATE_DIR`` (``~/.msx-ai`` by default), while relative paths supplied
through MCP are resolved below ``MSX_AI_USER_ROOT`` (the process working
directory by default). Source-only build operations use a checkout discovered
beside this module or selected explicitly with ``MSX_AI_SOURCE_ROOT``.

Path-query helpers never create files or directories. Call
:func:`ensure_directory` or :func:`prepare_openmsx_home` only at the operation
that needs writable state.
"""

from __future__ import annotations

import errno
import os
import tempfile
from importlib import resources
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
_CHECKOUT_CANDIDATE = PACKAGE_ROOT.parent
_SOURCE_MARKERS = ("Makefile", "agent", "tools")
OPENMSX_PUBLIC_FILES = (
    Path("share/extensions/README"),
    Path("share/extensions/rs232_proto.xml"),
    Path("share/machines/Gradiente_Expert20.xml"),
    Path("share/machines/Gradiente_Expert20_64K.xml"),
    Path("share/machines/Sony_HB-F1XDJ_128K_Lite.xml"),
    Path("share/machines/Sony_HB-F1XDJ_64K_Lite.xml"),
    Path("share/settings.xml"),
    Path("share/software/README"),
)


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve(strict=False)


def source_root() -> Path | None:
    """Return an explicit or detected source checkout, if one is available."""
    override = _environment_path("MSX_AI_SOURCE_ROOT")
    if override is not None:
        return override
    if all((_CHECKOUT_CANDIDATE / marker).exists()
           for marker in _SOURCE_MARKERS):
        return _CHECKOUT_CANDIDATE
    return None


def require_source_root() -> Path:
    """Return the source checkout or fail with an actionable explanation."""
    root = source_root()
    if root is None:
        raise RuntimeError(
            "this operation requires an MSX-AI source checkout; set "
            "MSX_AI_SOURCE_ROOT to a checkout containing Makefile, agent/ "
            "and tools/")
    return root


def state_root() -> Path:
    """Return the private writable state root without creating it."""
    override = (_environment_path("MSX_AI_STATE_DIR") or
                _environment_path("MSX_AI_DATA_DIR"))
    return override if override is not None else Path.home() / ".msx-ai"


def user_root() -> Path:
    """Return the base used for relative user-supplied paths."""
    return _environment_path("MSX_AI_USER_ROOT") or Path.cwd().resolve()


def work_root() -> Path:
    """Return the writable runtime work directory without creating it."""
    return state_root() / "work"


def source_work_root() -> Path:
    """Return checkout build output when available, otherwise runtime work."""
    root = source_root()
    return root / "work" if root is not None else work_root()


def disks_directory() -> Path:
    return work_root() / "disks"


def transfer_state_directory() -> Path:
    return work_root() / "transfers"


def agent_directory() -> Path:
    return source_work_root() / "agent"


def _checkout_openmsx_home() -> Path | None:
    """Return the checkout's existing openMSX home without creating it."""
    root = source_root()
    if root is None:
        return None
    checkout_home = root / ".openmsx-home"
    return checkout_home if (checkout_home / "share").is_dir() else None


def openmsx_home() -> Path:
    """Return an explicit, checkout, or writable openMSX home directory."""
    override = _environment_path("MSX_AI_OPENMSX_HOME")
    if override is not None:
        return override
    checkout_home = _checkout_openmsx_home()
    if checkout_home is not None:
        return checkout_home
    return state_root() / "openmsx-home"


def _managed_openmsx_home() -> Path | None:
    """Return the install-managed home, excluding overrides and checkouts."""
    if _environment_path("MSX_AI_OPENMSX_HOME") is not None:
        return None
    if _checkout_openmsx_home() is not None:
        return None
    return state_root() / "openmsx-home"


def _openmsx_resource_package() -> str:
    prefix = f"{__package__}." if __package__ else ""
    return prefix + "resources.openmsx"


def _publish_new_file(target: Path, content: bytes) -> None:
    """Publish complete content atomically without replacing ``target``."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp",
                delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            # A hard link publishes the fully synced inode atomically and, in
            # contrast to replace(), fails rather than overwriting a race
            # winner or an existing user-owned file.
            os.link(temporary, target)
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno in {
                    errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS,
                    errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise RuntimeError(
                    "the MSX-AI state filesystem must support hard links for "
                    "atomic, non-overwriting openMSX template publication") from exc
            raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_openmsx_home(home: str | os.PathLike[str] | None = None) -> Path:
    """Materialize public openMSX templates for an installed-package start.

    Only the default state home is managed. Explicit ``MSX_AI_OPENMSX_HOME``
    paths, source-checkout configuration, and custom ``OpenMSX(home=...)``
    directories are returned untouched. Existing files are never overwritten.
    """
    destination = (openmsx_home() if home is None else
                   Path(home).expanduser())
    managed = _managed_openmsx_home()
    if managed is None:
        return destination
    if destination.resolve(strict=False) != managed.resolve(strict=False):
        return destination

    packaged = resources.files(_openmsx_resource_package())
    for relative in OPENMSX_PUBLIC_FILES:
        target = destination / relative
        if target.exists():
            continue
        content = packaged.joinpath(*relative.parts).read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        _publish_new_file(target, content)
    return destination


def resolve_user_path(value: str | os.PathLike[str], *,
                      strict: bool = False) -> Path:
    """Make a user path absolute against :func:`user_root`.

    Existing absolute spellings are preserved unless ``strict`` requests
    filesystem resolution. This matters on systems where a public path such as
    ``/var`` is a symlink to a private mount path.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = user_root() / path
    return path.resolve(strict=True) if strict else path


def ensure_directory(path: str | os.PathLike[str]) -> Path:
    """Create one explicitly requested runtime directory and return it."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
