"""MCP server wiring: transports, auth, and the M0 tool surface.

The server is intentionally thin.  Tool implementations live in
:mod:`print3d.tools` and receive a :class:`Runtime`, so they can be unit tested
without speaking MCP.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import (
    LoadedConfig,
    Secrets,
)
from .errors import (
    ConfigError,
)
from .instructions import INSTRUCTIONS
from .runtime import Runtime, job_payload, tool_error
from .tools import register_all

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def build_server(runtime: Runtime | None = None, *, name: str = "print3d") -> FastMCP:
    """Create the FastMCP application with every M0 tool registered."""

    rt = runtime or Runtime.create()
    host = rt.loaded.config.server.host
    port = rt.loaded.config.server.port

    server = FastMCP(
        name=name,
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
    )
    # FastMCP otherwise advertises the ``mcp`` package version; hosts show this
    # string in their server list, so report Print3D's own version instead.
    if hasattr(server, "_mcp_server"):
        server._mcp_server.version = __version__

    register_all(server, rt)
    return server


def _bearer_token(loaded: LoadedConfig, secrets: Secrets) -> str | None:
    env_name = loaded.config.server.token_env or "PRINT3D_TOKEN"
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    token = str(secrets.section("server").get("token") or "").strip()
    return token or None


class BearerAuth:
    """Minimal ASGI middleware: require ``Authorization: Bearer <token>``."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        # Constant-time compare: a lenient != leaks the token one byte at a time.
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "missing or invalid bearer token",
                        "hint": "Send Authorization: Bearer <token> using the value from PRINT3D_TOKEN or secrets.json server.token.",
                        "details": {},
                    },
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="print3d"'),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def serve(
    runtime: Runtime, *, transport: str = "stdio", host: str | None = None, port: int | None = None
) -> None:
    """Run the server over stdio or streamable HTTP.

    Non-loopback HTTP binds require a bearer token, otherwise we refuse to start:
    an unauthenticated CAD/printer bridge on the LAN is exactly the failure mode
    the design's security section calls out.
    """

    server = build_server(runtime)
    if transport == "stdio":
        server.run("stdio")
        return

    if transport not in {"http", "streamable-http"}:
        raise ConfigError(
            f"unknown transport {transport!r}",
            hint="Use `print3d serve --stdio` or `print3d serve --http`.",
        )

    cfg = runtime.loaded.config.server
    bind_host = host or cfg.host
    bind_port = port or cfg.port
    token = _bearer_token(runtime.loaded, runtime.secrets)

    if bind_host not in LOOPBACK_HOSTS and not token:
        raise ConfigError(
            f"refusing to bind {bind_host} without a bearer token",
            hint=(
                "Set PRINT3D_TOKEN (or server.token in secrets.json) before exposing "
                "Print3D beyond loopback."
            ),
        )

    app = server.streamable_http_app()
    if token:
        app.add_middleware(BearerAuth, token=token)

    import uvicorn

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - convenience
    from .cli import main as cli_main

    return cli_main(argv if argv is not None else sys.argv[1:])


__all__ = [
    "Runtime",
    "build_server",
    "serve",
    "tool_error",
    "job_payload",
]
