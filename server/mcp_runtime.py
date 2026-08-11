"""Standards-based MCP runtime for the synchronous MSX-AI tool core.

The official Python SDK supplies dual-era protocol handling, STDIO and
Streamable HTTP. Mutating target operations remain serialized and run outside
the event loop; explicit local diagnostics can stay responsive while an agent
request stalls. File transfers receive cooperative progress and cancellation
hooks.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import queue
import sys
import threading
from typing import Any, Mapping

import anyio
import jsonschema
import mcp.types as types
from mcp.server import stdio
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

if __package__:
    from ._version import __version__
    from .execution import bind_execution_hooks
    from . import mcp_metadata, msx_docs
    from . import msx_mcp_server as core
else:  # Preserve direct source-checkout execution for contributors.
    from _version import __version__
    from execution import bind_execution_hooks
    import mcp_metadata
    import msx_docs
    import msx_mcp_server as core


SERVER_NAME = "msx-ai"
SERVER_TITLE = "MSX-AI MCP Server"
SERVER_DESCRIPTION = (
    "Control emulated and physical MSX computers through one auditable "
    "Python MCP server."
)
SERVER_INSTRUCTIONS = (
    "Tool names select the channel explicitly: msx_local_* always uses the "
    "openMSX control API, while msx_agent_* always uses the ASM-agent "
    "protocol. Both channels may coexist and can be alternated without "
    "changing global selection state. Use msx_local_boot or msx_local_attach "
    "for direct openMSX control; msx_local_doctor validates executable, "
    "transport, profile and config readiness without launching it. Use "
    "msx_tcp_bench_start for a paired simulation, "
    "and msx_agent_listen or msx_agent_connect for physical hardware. Read "
    "msx-ai://docs/index or "
    "call msx_docs_search when backend or safety requirements are unclear."
)
PUBLIC_CACHE_MS = 300_000
_WORKER_POLL_SECONDS = 0.02


@dataclass
class RuntimeState:
    target_lock: anyio.Lock
    local_lock: anyio.Lock = field(default_factory=anyio.Lock)
    agent_lock: anyio.Lock = field(default_factory=anyio.Lock)


_LOCAL_DIAGNOSTIC_TOOLS = frozenset({
    "msx_local_status",
    "msx_local_screen",
    "msx_local_screenshot",
})
_NO_TARGET_IO_TOOLS = frozenset({
    "msx_docs_search", "msx_targets_status", "msx_local_doctor",
})
_BENCH_LIFECYCLE_TOOLS = frozenset({
    "msx_tcp_bench_start",
    "msx_tcp_bench_status",
    "msx_tcp_bench_shutdown",
})


@asynccontextmanager
async def _tool_lock_scope(state: RuntimeState, name: str):
    """Serialize per channel while keeping safe local diagnostics responsive."""
    if name in _NO_TARGET_IO_TOOLS:
        yield
        return
    if name in _LOCAL_DIAGNOSTIC_TOOLS:
        async with state.local_lock:
            yield
        return
    if name in _BENCH_LIFECYCLE_TOOLS:
        async with state.target_lock:
            async with state.local_lock:
                async with state.agent_lock:
                    yield
        return
    if name.startswith("msx_local_"):
        async with state.target_lock:
            async with state.local_lock:
                yield
        return
    if name.startswith("msx_agent_"):
        async with state.target_lock:
            async with state.agent_lock:
                yield
        return
    async with state.target_lock:
        yield


@asynccontextmanager
async def _lifespan(_server: Server):
    state = RuntimeState(target_lock=anyio.Lock())
    try:
        yield state
    finally:
        # Shutdown waits for both channels and every mutating operation. A
        # transport close cannot leave openMSX or a physical-agent socket
        # owned by an exited MCP process.
        with anyio.CancelScope(shield=True):
            async with state.target_lock:
                async with state.local_lock:
                    async with state.agent_lock:
                        await anyio.to_thread.run_sync(core.SESSION.shutdown)


def _tool_models() -> list[types.Tool]:
    tools: list[types.Tool] = []
    for name, (_handler, description, input_schema) in core.TOOLS.items():
        hints = mcp_metadata.hints_for(name)
        tools.append(types.Tool(
            name=name,
            title=mcp_metadata.title_for(name),
            description=description,
            inputSchema=input_schema,
            outputSchema=dict(mcp_metadata.output_schema_for(name)),
            annotations=types.ToolAnnotations(
                title=mcp_metadata.title_for(name),
                readOnlyHint=hints.read_only,
                destructiveHint=hints.destructive,
                idempotentHint=hints.idempotent,
                openWorldHint=hints.open_world,
            ),
        ))
    return tools


def _content_block(block: Mapping[str, Any]):
    kind = block.get("type")
    if kind == "text":
        return types.TextContent(text=str(block.get("text", "")))
    if kind == "image":
        return types.ImageContent(
            data=str(block.get("data", "")),
            mimeType=str(block.get("mimeType", "application/octet-stream")),
        )
    raise TypeError(f"unsupported content block type: {kind!r}")


def normalize_tool_result(name: str, raw: Any) -> types.CallToolResult:
    """Convert legacy string/content returns into MCP structured output."""
    if isinstance(raw, list):
        content = [_content_block(block) for block in raw]
        if mcp_metadata.canonical_tool_name(name) == "msx_screenshot":
            summary = next((block.text for block in content
                            if isinstance(block, types.TextContent)),
                           "MSX screenshot")
            structured: dict[str, Any] = {
                "summary": summary,
                "media_type": "image/png",
                "image_in_content": True,
            }
        else:
            structured = {"result": [dict(block) for block in raw]}
    elif isinstance(raw, str):
        content = [types.TextContent(text=raw)]
        # Never infer structure from screen/Tcl text. Structured handlers
        # return Python dicts explicitly, so a visible string such as
        # ``{"a": 1}`` remains text even for a dual-backend tool.
        structured = {"result": raw}
    elif isinstance(raw, dict):
        structured = dict(raw)
        content = [types.TextContent(
            text=json.dumps(structured, indent=2, sort_keys=True))]
    else:
        structured = {"result": raw}
        content = [types.TextContent(text=str(raw))]

    schema = mcp_metadata.output_schema_for(name)
    jsonschema.validate(structured, schema)
    return types.CallToolResult(
        content=content, structuredContent=structured, isError=False)


async def _drain_progress(ctx: ServerRequestContext,
                          events: queue.SimpleQueue) -> None:
    while True:
        try:
            completed, total, message = events.get_nowait()
        except queue.Empty:
            return
        try:
            await ctx.session.report_progress(completed, total, message)
        except Exception:
            # Progress is optional telemetry. A client that closes its progress
            # channel must not interrupt or unlock a live hardware operation.
            continue


async def _run_sync_handler(ctx: ServerRequestContext, handler,
                            arguments: dict[str, Any]) -> Any:
    """Run one synchronous handler with safe cancellation and progress."""
    cancel_event = threading.Event()
    progress_events: queue.SimpleQueue = queue.SimpleQueue()
    outcome: dict[str, Any] = {}

    def report_progress(completed, total=None, message=None):
        progress_events.put((float(completed),
                             None if total is None else float(total),
                             None if message is None else str(message)))

    def worker():
        try:
            with bind_execution_hooks(
                    progress=report_progress,
                    cancelled=cancel_event.is_set):
                outcome["value"] = handler(**arguments)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(
        target=worker, name="msx-ai-tool-worker", daemon=False)
    thread.start()
    cancelled_class = anyio.get_cancelled_exc_class()
    try:
        while thread.is_alive():
            await _drain_progress(ctx, progress_events)
            await anyio.sleep(_WORKER_POLL_SECONDS)
        thread.join()
        await _drain_progress(ctx, progress_events)
    except cancelled_class:
        # Notify the hardware operation, then keep the target lock until its
        # worker has acknowledged cancellation or reached another safe return.
        # Releasing the lock early could interleave a new command with a live
        # UART request or DOS transfer helper.
        cancel_event.set()
        with anyio.CancelScope(shield=True):
            while thread.is_alive():
                await _drain_progress(ctx, progress_events)
                await anyio.sleep(_WORKER_POLL_SECONDS)
            thread.join()
            await _drain_progress(ctx, progress_events)
        raise
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=f"ERROR: {message}")],
        isError=True,
    )


async def _list_tools(_ctx, _params):
    return types.ListToolsResult(
        tools=_tool_models(), ttlMs=PUBLIC_CACHE_MS, cacheScope="public")


async def _call_tool(ctx: ServerRequestContext,
                     params: types.CallToolRequestParams):
    entry = core.TOOLS.get(params.name)
    if entry is None:
        raise MCPError(
            code=types.INVALID_PARAMS,
            message=f"unknown MSX-AI tool: {params.name}")
    arguments = dict(params.arguments or {})
    try:
        jsonschema.validate(arguments, entry[2])
    except jsonschema.ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path)
        location = f" at {path}" if path else ""
        return _tool_error(f"invalid arguments{location}: {exc.message}")
    try:
        async with _tool_lock_scope(ctx.lifespan_context, params.name):
            raw = await _run_sync_handler(ctx, entry[0], arguments)
        return normalize_tool_result(params.name, raw)
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as exc:
        # No traceback is returned to remote clients. Operators can reproduce
        # with Python logging/debugging without leaking local paths by default.
        return _tool_error(str(exc))


async def _list_resources(_ctx, _params):
    annotations = types.Annotations(
        audience=["user", "assistant"], priority=0.8,
        lastModified="2026-08-07")
    resources_list = [types.Resource(
        name=item["name"], title=item["title"], uri=item["uri"],
        description=item["description"], mimeType=item["mimeType"],
        annotations=annotations,
    ) for item in msx_docs.resource_catalog()]
    return types.ListResourcesResult(
        resources=resources_list, ttlMs=PUBLIC_CACHE_MS,
        cacheScope="public")


async def _read_resource(_ctx, params: types.ReadResourceRequestParams):
    try:
        mime_type, text = msx_docs.read_resource(str(params.uri))
    except KeyError as exc:
        raise MCPError(
            code=types.INVALID_PARAMS, message=str(exc),
            data={"uri": str(params.uri)}) from exc
    return types.ReadResourceResult(
        contents=[types.TextResourceContents(
            uri=str(params.uri), mimeType=mime_type, text=text)],
        ttlMs=PUBLIC_CACHE_MS, cacheScope="public")


def _prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="start_msx_session",
            title="Start an MSX session",
            description=(
                "Choose the correct backend and produce a safe, verifiable "
                "first interaction with an MSX target."),
            arguments=[
                types.PromptArgument(
                    name="backend", required=True,
                    description=(
                        "openmsx-direct, agent-simulated, or agent-physical")),
                types.PromptArgument(
                    name="mode", required=False,
                    description="resident (default) or monitor for agent paths"),
                types.PromptArgument(
                    name="visible", required=False,
                    description="true to request a visible emulator window"),
            ],
        ),
        types.Prompt(
            name="diagnose_msx_connection",
            title="Diagnose an MSX connection",
            description=(
                "Collect read-only status evidence before changing a target "
                "or restarting its transport."),
            arguments=[
                types.PromptArgument(
                    name="backend", required=True,
                    description="Backend currently being tested"),
                types.PromptArgument(
                    name="symptom", required=False,
                    description="Observed failure or unexpected behavior"),
            ],
        ),
    ]


async def _list_prompts(_ctx, _params):
    return types.ListPromptsResult(
        prompts=_prompts(), ttlMs=PUBLIC_CACHE_MS, cacheScope="public")


async def _get_prompt(_ctx, params: types.GetPromptRequestParams):
    arguments = dict(params.arguments or {})
    if params.name == "start_msx_session":
        unknown = set(arguments) - {"backend", "mode", "visible"}
        if unknown:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=f"unknown prompt arguments: {', '.join(sorted(unknown))}")
        backend = arguments.get("backend")
        if backend not in {
                "openmsx-direct", "agent-simulated", "agent-physical"}:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="backend must be openmsx-direct, agent-simulated, or agent-physical")
        mode = arguments.get("mode", "resident")
        if mode not in {"resident", "monitor"}:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="mode must be resident or monitor")
        visible_value = arguments.get("visible", "false")
        if not isinstance(visible_value, str) or visible_value.lower() not in {
                "1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="visible must be true or false")
        visible = visible_value.lower() in {"1", "true", "yes", "on"}
        if backend == "openmsx-direct":
            config_mode = "user" if visible else "isolated"
            action = (
                f"Call msx_local_doctor with profile='auto' and "
                f"config_mode='{config_mode}'. If ready, call msx_local_boot "
                f"with the resolved profile, config_mode='{config_mode}' and "
                f"window={str(visible).lower()}, then call msx_local_status "
                "and perform one read-only msx_local_screen check.")
        elif backend == "agent-simulated":
            action = (
                f"Call msx_tcp_bench_start with mode='{mode}' and "
                f"window={str(visible).lower()}, then verify both channels with "
                "msx_tcp_bench_status. Use msx_local_* for local inspection and "
                "msx_agent_* for protocol validation. Keep only one openMSX "
                "process alive.")
        else:
            action = (
                "Ask whether the adapter is a TCP client or server. Use "
                "msx_agent_listen for a client adapter, or msx_agent_connect for "
                "a server adapter. After handshake, call msx_agent_status before any "
                "mutating tool. Do not assume BaDCaT-specific setup.")
        text = (
            "Start an MSX-AI session using explicit channel tools. " + action +
            " Read msx-ai://docs/backends and msx-ai://docs/safety when a "
            "capability or resident/monitor restriction is uncertain.")
        description = "Backend-aware startup plan"
    elif params.name == "diagnose_msx_connection":
        unknown = set(arguments) - {"backend", "symptom"}
        if unknown:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=f"unknown prompt arguments: {', '.join(sorted(unknown))}")
        backend = arguments.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="backend is required")
        symptom = arguments.get("symptom", "not specified")
        if not isinstance(symptom, str):
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="symptom must be a string")
        text = (
            f"Diagnose the {backend} connection. Reported symptom: {symptom}. "
            "Begin with msx_targets_status, then use msx_local_status or "
            "msx_agent_status for the intended channel. If neither channel is "
            "connected, call msx_local_doctor for openMSX or inspect the agent "
            "startup endpoint without launching another emulator. Use the "
            "corresponding local or agent "
            "screen/CPU tool only when its runtime supports that operation. Do "
            "not issue reset, raw Tcl, "
            "memory writes, or transport restarts until the evidence is summarized. "
            "Consult msx-ai://docs/safety and msx-ai://docs/backends.")
        description = "Read-only connection diagnosis plan"
    else:
        raise MCPError(
            code=types.INVALID_PARAMS,
            message=f"unknown MSX-AI prompt: {params.name}")
    return types.GetPromptResult(
        description=description,
        messages=[types.PromptMessage(
            role="user", content=types.TextContent(text=text))])


def create_server() -> Server[RuntimeState]:
    """Create a fresh server instance for one CLI run or integration test."""
    return Server(
        SERVER_NAME,
        version=__version__,
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://github.com/mefroio/msx-ai",
        lifespan=_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_list_resources=_list_resources,
        on_read_resource=_read_resource,
        on_list_prompts=_list_prompts,
        on_get_prompt=_get_prompt,
    )


async def run_stdio(server: Server) -> None:
    async with stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options())


async def run_http(server: Server, *, host: str, port: int,
                   path: str, log_level: str) -> None:
    import uvicorn

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*"],
        allowed_origins=["http://127.0.0.1:*"],
    )
    app = server.streamable_http_app(
        streamable_http_path=path,
        json_response=False,
        stateless_http=False,
        transport_security=security,
        host=host,
    )
    config = uvicorn.Config(
        app, host=host, port=port, log_level=log_level,
        access_log=False)
    await uvicorn.Server(config).serve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msx-ai-mcp",
        description=SERVER_DESCRIPTION)
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="MCP transport (default: stdio)")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="HTTP bind address; only 127.0.0.1 is accepted")
    parser.add_argument(
        "--port", type=int, default=8000,
        help="HTTP TCP port (default: 8000)")
    parser.add_argument(
        "--path", default="/mcp",
        help="Streamable HTTP endpoint path (default: /mcp)")
    parser.add_argument(
        "--log-level", choices=("critical", "error", "warning", "info", "debug"),
        default="info", help="HTTP server log level")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _validate_cli(parser: argparse.ArgumentParser, args) -> None:
    if args.transport == "http":
        if args.host != "127.0.0.1":
            parser.error(
                "unauthenticated HTTP is restricted to IPv4 loopback "
                "(127.0.0.1)")
        if not 1 <= args.port <= 65535:
            parser.error("--port must be in range 1..65535")
        if (not args.path.startswith("/") or "{" in args.path or
                "}" in args.path or any(char.isspace() for char in args.path)):
            parser.error("--path must be one fixed absolute URL path")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_cli(parser, args)
    server = create_server()
    try:
        if args.transport == "stdio":
            anyio.run(run_stdio, server)
        else:
            async def serve_http():
                await run_http(
                    server, host=args.host, port=args.port,
                    path=args.path, log_level=args.log_level)
            anyio.run(serve_http)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
