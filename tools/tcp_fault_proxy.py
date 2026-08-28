#!/usr/bin/env python3
"""Small loopback TCP proxy with deterministic connection fault injection.

The proxy is intended for integration tests.  It never interprets protocol
bytes: one host-side connection is copied verbatim to one target connection,
while the test may close the pair with either FIN or RST, or temporarily stop
forwarding it.  A single proxy instance can carry successive reconnects.
"""

from __future__ import annotations

import dataclasses
import select
import socket
import struct
import threading
import time
from typing import Literal


FaultMode = Literal["fin", "rst", "blackhole"]


@dataclasses.dataclass(frozen=True)
class SessionSnapshot:
    """Externally observable state for one proxied TCP session."""

    number: int
    connected: bool
    fault: str | None
    client_to_target: int
    target_to_client: int
    opened_at: float
    closed_at: float | None


@dataclasses.dataclass
class _Session:
    number: int
    client: socket.socket
    target: socket.socket
    opened_at: float
    fault: str | None = None
    client_to_target: int = 0
    target_to_client: int = 0
    closed_at: float | None = None
    blackholed: bool = False


class TCPFaultProxy:
    """Forward successive loopback TCP sessions and cut them on demand."""

    def __init__(self, target_host: str, target_port: int, *,
                 listen_host: str = "127.0.0.1", listen_port: int = 0,
                 connect_timeout: float = 1.0):
        if not 1 <= int(target_port) <= 65535:
            raise ValueError("target_port must be in range 1..65535")
        if not 0 <= int(listen_port) <= 65535:
            raise ValueError("listen_port must be in range 0..65535")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        self.target = (str(target_host), int(target_port))
        self.connect_timeout = float(connect_timeout)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((str(listen_host), int(listen_port)))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.endpoint = self._listener.getsockname()
        self._condition = threading.Condition()
        self._stopping = False
        self._next_session = 1
        self._active: _Session | None = None
        self._history: list[SessionSnapshot] = []
        self._workers: list[threading.Thread] = []
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="msx-ai-tcp-fault-proxy",
            daemon=True,
        )
        self._accept_thread.start()

    def __enter__(self) -> "TCPFaultProxy":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @staticmethod
    def _close_socket(stream: socket.socket, *, reset: bool = False) -> None:
        if reset:
            try:
                stream.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))
            except OSError:
                pass
        else:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            stream.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                client, _peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                target = socket.create_connection(
                    self.target, timeout=self.connect_timeout)
                target.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.setblocking(False)
                target.setblocking(False)
            except OSError:
                self._close_socket(client, reset=True)
                continue
            with self._condition:
                if self._stopping:
                    self._close_socket(client)
                    self._close_socket(target)
                    return
                session = _Session(
                    number=self._next_session,
                    client=client,
                    target=target,
                    opened_at=time.monotonic(),
                )
                self._next_session += 1
                previous = self._active
                self._active = session
                self._condition.notify_all()
            if previous is not None and previous.closed_at is None:
                self._finish(previous, "superseded", reset=True)
            worker = threading.Thread(
                target=self._pump,
                args=(session,),
                name=f"msx-ai-tcp-fault-session-{session.number}",
                daemon=True,
            )
            with self._condition:
                self._workers.append(worker)
            worker.start()

    def _finish(self, session: _Session, reason: str, *,
                reset: bool = False) -> None:
        with self._condition:
            if session.closed_at is not None:
                return
            if session.fault is None:
                session.fault = reason
            session.closed_at = time.monotonic()
        self._close_socket(session.client, reset=reset)
        self._close_socket(session.target, reset=reset)
        snapshot = self._snapshot(session)
        with self._condition:
            self._history.append(snapshot)
            if self._active is session:
                self._active = None
            self._condition.notify_all()

    def _pump(self, session: _Session) -> None:
        streams = (session.client, session.target)
        while True:
            with self._condition:
                if self._stopping or session.closed_at is not None:
                    break
                blackholed = session.blackholed
            if blackholed:
                time.sleep(0.01)
                continue
            try:
                readable, _, exceptional = select.select(
                    streams, (), streams, 0.05)
            except (OSError, ValueError):
                break
            if exceptional:
                break
            for source in readable:
                with self._condition:
                    if session.blackholed or session.closed_at is not None:
                        break
                destination = (
                    session.target if source is session.client
                    else session.client)
                try:
                    data = source.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    self._finish(session, "peer-close")
                    return
                try:
                    destination.sendall(data)
                except OSError:
                    self._finish(session, "forward-error", reset=True)
                    return
                with self._condition:
                    if source is session.client:
                        session.client_to_target += len(data)
                    else:
                        session.target_to_client += len(data)
        self._finish(session, "proxy-close")

    @staticmethod
    def _snapshot(session: _Session) -> SessionSnapshot:
        return SessionSnapshot(
            number=session.number,
            connected=session.closed_at is None,
            fault=session.fault,
            client_to_target=session.client_to_target,
            target_to_client=session.target_to_client,
            opened_at=session.opened_at,
            closed_at=session.closed_at,
        )

    def wait_for_session(self, *, after: int = 0,
                         timeout: float = 5.0) -> SessionSnapshot:
        """Wait until an active session newer than *after* is established."""
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while True:
                active = self._active
                if (active is not None and active.closed_at is None and
                        active.number > after):
                    return self._snapshot(active)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for proxied TCP session")
                self._condition.wait(remaining)

    def cut(self, mode: FaultMode = "fin") -> SessionSnapshot:
        """Inject a FIN, RST, or forwarding blackhole into the active session."""
        if mode not in ("fin", "rst", "blackhole"):
            raise ValueError("fault mode must be fin, rst, or blackhole")
        with self._condition:
            session = self._active
            if session is None or session.closed_at is not None:
                raise RuntimeError("no active proxied TCP session")
            session.fault = mode
            if mode == "blackhole":
                session.blackholed = True
                return self._snapshot(session)
        self._finish(session, mode, reset=(mode == "rst"))
        return self._snapshot(session)

    def close(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            active = self._active
            self._condition.notify_all()
        self._close_socket(self._listener)
        if active is not None:
            self._finish(active, "proxy-close", reset=True)
        self._accept_thread.join(timeout=2.0)
        with self._condition:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=2.0)

    @property
    def history(self) -> tuple[SessionSnapshot, ...]:
        with self._condition:
            return tuple(self._history)


__all__ = ["FaultMode", "SessionSnapshot", "TCPFaultProxy"]
