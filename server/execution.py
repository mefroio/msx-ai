"""Request-scoped execution hooks shared by MCP and synchronous tools.

The MSX backends remain ordinary synchronous Python APIs.  The modern MCP
runtime binds this tiny context while a tool runs in its worker thread so
long transfers can publish progress and observe cancellation without coupling
the hardware layer to a particular MCP transport.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


ProgressCallback = Callable[[float, float | None, str | None], None]
CancellationCallback = Callable[[], bool]


@dataclass(frozen=True)
class ExecutionHooks:
    progress: ProgressCallback | None = None
    cancelled: CancellationCallback | None = None


_HOOKS: ContextVar[ExecutionHooks | None] = ContextVar(
    "msx_ai_execution_hooks", default=None)


@contextmanager
def bind_execution_hooks(*, progress: ProgressCallback | None = None,
                         cancelled: CancellationCallback | None = None
                         ) -> Iterator[None]:
    """Bind callbacks for one synchronous tool invocation."""
    token = _HOOKS.set(ExecutionHooks(progress=progress, cancelled=cancelled))
    try:
        yield
    finally:
        _HOOKS.reset(token)


def current_progress_callback() -> ProgressCallback | None:
    hooks = _HOOKS.get()
    return hooks.progress if hooks is not None else None


def current_cancellation_callback() -> CancellationCallback | None:
    hooks = _HOOKS.get()
    return hooks.cancelled if hooks is not None else None
