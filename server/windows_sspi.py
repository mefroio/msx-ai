"""Windows SSPI client used by openMSX loopback control sockets.

openMSX authenticates every Windows TCP control connection with the native
``Negotiate`` security package before it starts the XML control protocol.  The
wire format for each SSPI token is a four-byte network-order length followed by
the token bytes.  No message signing or sealing is used after authentication;
the socket carries the ordinary openMSX XML stream.

This module deliberately has no effect on non-Windows hosts.  Keeping the
Win32 declarations here prevents the portable openMSX adapter from importing
platform-only modules or gaining a mandatory third-party dependency.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
from ctypes import wintypes


_SECPKG_CRED_OUTBOUND = 2
_SECURITY_NETWORK_DREP = 0
_SECBUFFER_VERSION = 0
_SECBUFFER_TOKEN = 2

_ISC_REQ_ALLOCATE_MEMORY = 0x00000100
_ISC_REQ_CONNECTION = 0x00000800
_ISC_REQ_STREAM = 0x00008000

_SEC_E_OK = 0x00000000
_SEC_I_CONTINUE_NEEDED = 0x00090312
_SEC_I_COMPLETE_NEEDED = 0x00090313
_SEC_I_COMPLETE_AND_CONTINUE = 0x00090314

_MAX_NEGOTIATE_TOKEN = 16 * 1024 * 1024


class WindowsSspiError(RuntimeError):
    """The Windows Negotiate handshake could not be completed."""


class WindowsSspiUnavailable(WindowsSspiError):
    """SSPI was requested on a host that does not provide it."""


class _SecHandle(ctypes.Structure):
    _fields_ = [
        ("dwLower", ctypes.c_void_p),
        ("dwUpper", ctypes.c_void_p),
    ]


class _SecBuffer(ctypes.Structure):
    _fields_ = [
        ("cbBuffer", wintypes.ULONG),
        ("BufferType", wintypes.ULONG),
        ("pvBuffer", ctypes.c_void_p),
    ]


class _SecBufferDesc(ctypes.Structure):
    _fields_ = [
        ("ulVersion", wintypes.ULONG),
        ("cBuffers", wintypes.ULONG),
        ("pBuffers", ctypes.POINTER(_SecBuffer)),
    ]


def _unsigned_status(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise WindowsSspiError(
                "openMSX closed the control socket during SSPI negotiation")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_chunk(stream: socket.socket) -> bytes:
    (size,) = struct.unpack("!I", _recv_exact(stream, 4))
    if size == 0 or size > _MAX_NEGOTIATE_TOKEN:
        raise WindowsSspiError(
            f"openMSX returned an invalid SSPI token length: {size}")
    return _recv_exact(stream, size)


def _send_chunk(stream: socket.socket, token: bytes) -> None:
    if not token or len(token) > _MAX_NEGOTIATE_TOKEN:
        raise WindowsSspiError(
            f"refusing to send invalid SSPI token length: {len(token)}")
    stream.sendall(struct.pack("!I", len(token)) + token)


class _Secur32:
    """Small typed facade over the subset of secur32.dll that we use."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise WindowsSspiUnavailable(
                "openMSX Windows socket attachment requires native SSPI")

        self.dll = ctypes.WinDLL("secur32.dll", use_last_error=True)

        self.acquire = self.dll.AcquireCredentialsHandleW
        self.acquire.argtypes = [
            wintypes.LPWSTR,
            wintypes.LPWSTR,
            wintypes.ULONG,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_SecHandle),
            ctypes.POINTER(ctypes.c_longlong),
        ]
        self.acquire.restype = wintypes.LONG

        self.initialize = self.dll.InitializeSecurityContextW
        self.initialize.argtypes = [
            ctypes.POINTER(_SecHandle),
            ctypes.POINTER(_SecHandle),
            wintypes.LPWSTR,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.POINTER(_SecBufferDesc),
            wintypes.ULONG,
            ctypes.POINTER(_SecHandle),
            ctypes.POINTER(_SecBufferDesc),
            ctypes.POINTER(wintypes.ULONG),
            ctypes.POINTER(ctypes.c_longlong),
        ]
        self.initialize.restype = wintypes.LONG

        self.complete = self.dll.CompleteAuthToken
        self.complete.argtypes = [
            ctypes.POINTER(_SecHandle),
            ctypes.POINTER(_SecBufferDesc),
        ]
        self.complete.restype = wintypes.LONG

        self.free_context_buffer = self.dll.FreeContextBuffer
        self.free_context_buffer.argtypes = [ctypes.c_void_p]
        self.free_context_buffer.restype = wintypes.LONG

        self.delete_context = self.dll.DeleteSecurityContext
        self.delete_context.argtypes = [ctypes.POINTER(_SecHandle)]
        self.delete_context.restype = wintypes.LONG

        self.free_credentials = self.dll.FreeCredentialsHandle
        self.free_credentials.argtypes = [ctypes.POINTER(_SecHandle)]
        self.free_credentials.restype = wintypes.LONG


def _status_error(operation: str, status: int) -> WindowsSspiError:
    unsigned = _unsigned_status(status)
    return WindowsSspiError(
        f"Windows SSPI {operation} failed with status 0x{unsigned:08X}")


def _authenticate(stream: socket.socket, api: _Secur32 | None = None) -> None:
    api = api or _Secur32()
    credentials = _SecHandle()
    context = _SecHandle()
    expiry = ctypes.c_longlong()
    credentials_acquired = False
    context_created = False

    status = api.acquire(
        None,
        "Negotiate",
        _SECPKG_CRED_OUTBOUND,
        None,
        None,
        None,
        None,
        ctypes.byref(credentials),
        ctypes.byref(expiry),
    )
    if _unsigned_status(status) != _SEC_E_OK:
        raise _status_error("AcquireCredentialsHandleW", status)
    credentials_acquired = True

    input_storage = None
    input_buffer = _SecBuffer()
    input_descriptor = _SecBufferDesc()
    input_descriptor_ptr = None

    try:
        while True:
            output_buffer = _SecBuffer(0, _SECBUFFER_TOKEN, None)
            output_descriptor = _SecBufferDesc(
                _SECBUFFER_VERSION, 1, ctypes.pointer(output_buffer))
            attributes = wintypes.ULONG()
            context_ptr = ctypes.byref(context) if context_created else None

            status = api.initialize(
                ctypes.byref(credentials),
                context_ptr,
                None,
                (_ISC_REQ_ALLOCATE_MEMORY |
                 _ISC_REQ_CONNECTION |
                 _ISC_REQ_STREAM),
                0,
                _SECURITY_NETWORK_DREP,
                input_descriptor_ptr,
                0,
                ctypes.byref(context),
                ctypes.byref(output_descriptor),
                ctypes.byref(attributes),
                ctypes.byref(expiry),
            )
            context_created = True
            unsigned = _unsigned_status(status)

            if unsigned in {
                    _SEC_I_COMPLETE_NEEDED,
                    _SEC_I_COMPLETE_AND_CONTINUE}:
                complete = api.complete(
                    ctypes.byref(context), ctypes.byref(output_descriptor))
                if _unsigned_status(complete) != _SEC_E_OK:
                    if output_buffer.pvBuffer:
                        api.free_context_buffer(output_buffer.pvBuffer)
                    raise _status_error("CompleteAuthToken", complete)
                unsigned = (
                    _SEC_E_OK if unsigned == _SEC_I_COMPLETE_NEEDED
                    else _SEC_I_CONTINUE_NEEDED)

            if unsigned not in {_SEC_E_OK, _SEC_I_CONTINUE_NEEDED}:
                if output_buffer.pvBuffer:
                    api.free_context_buffer(output_buffer.pvBuffer)
                raise _status_error("InitializeSecurityContextW", status)

            try:
                if output_buffer.cbBuffer:
                    if not output_buffer.pvBuffer:
                        raise WindowsSspiError(
                            "Windows SSPI returned an empty token pointer")
                    token = ctypes.string_at(
                        output_buffer.pvBuffer, output_buffer.cbBuffer)
                    _send_chunk(stream, token)
            finally:
                if output_buffer.pvBuffer:
                    api.free_context_buffer(output_buffer.pvBuffer)

            if unsigned == _SEC_E_OK:
                return

            server_token = _recv_chunk(stream)
            input_storage = ctypes.create_string_buffer(server_token)
            input_buffer = _SecBuffer(
                len(server_token),
                _SECBUFFER_TOKEN,
                ctypes.cast(input_storage, ctypes.c_void_p),
            )
            input_descriptor = _SecBufferDesc(
                _SECBUFFER_VERSION, 1, ctypes.pointer(input_buffer))
            input_descriptor_ptr = ctypes.byref(input_descriptor)
    finally:
        # openMSX uses SSPI only to authenticate and authorize the initial
        # loopback connection.  The subsequent XML stream is intentionally
        # unwrapped, so the security context can be released immediately.
        if context_created:
            api.delete_context(ctypes.byref(context))
        if credentials_acquired:
            api.free_credentials(ctypes.byref(credentials))


def connect_openmsx_tcp(
        host: str, port: int, *, timeout: float = 2.0) -> socket.socket:
    """Connect and authenticate one Windows openMSX control descriptor.

    Only the IPv4 loopback endpoint published by openMSX is accepted.  The
    returned socket has completed SSPI negotiation and is ready for the normal
    ``<openmsx-control>`` XML handshake.
    """
    if os.name != "nt":
        raise WindowsSspiUnavailable(
            "openMSX TCP/SSPI attachment is available only on Windows")
    if host != "127.0.0.1":
        raise WindowsSspiError(
            f"refusing non-loopback openMSX control host: {host}")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WindowsSspiError(f"invalid openMSX control port: {port!r}")
    if timeout <= 0:
        raise WindowsSspiError("openMSX control timeout must be positive")

    stream = socket.create_connection((host, port), timeout=float(timeout))
    try:
        _authenticate(stream)
        return stream
    except Exception:
        stream.close()
        raise
