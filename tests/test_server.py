"""In-memory MCP tests: handshake, tool listing and calls, HTTP auth."""

from __future__ import annotations

import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from xprint import __version__
from xprint.server import BearerAuth, Runtime, _bearer_token, build_server
from xprint.workspace import Workspace


def run(coro):
    return asyncio.run(coro)


def make_server():
    return build_server(Runtime.create(workspace=Workspace()))


def test_handshake_and_tools_list() -> None:
    async def scenario():
        async with create_connected_server_and_client_session(make_server()) as client:
            info = await client.initialize()
            listing = await client.list_tools()
            return info, listing

    info, listing = run(scenario())
    assert info.serverInfo.name == "xprint"
    assert info.serverInfo.version == __version__
    assert sorted(t.name for t in listing.tools) == [
        "asset_list",
        "capabilities",
        "job_status",
        "model_build",
        "model_generate",
        "model_show",
        "model_write",
        "system_status",
    ]
    assert all(t.inputSchema["type"] == "object" for t in listing.tools)


def test_system_status_is_read_only() -> None:
    async def scenario():
        async with create_connected_server_and_client_session(make_server()) as client:
            await client.initialize()
            return await client.call_tool("system_status", {})

    result = run(scenario())
    assert result.isError is False
    payload = result.structuredContent or json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert "workspace" in payload


def test_capabilities_never_leaks_keys(monkeypatch) -> None:
    monkeypatch.setenv("TRIPO_API_KEY", "sk-super-secret-value")

    async def scenario():
        async with create_connected_server_and_client_session(make_server()) as client:
            await client.initialize()
            return await client.call_tool("capabilities", {})

    result = run(scenario())
    text = json.dumps(result.structuredContent or {}) + result.content[0].text
    assert "sk-super-secret-value" not in text
    payload = result.structuredContent or json.loads(result.content[0].text)
    tripo = next(p for p in payload["providers"]["cloud"] if p["id"] == "tripo")
    assert tripo["has_key"] is True
    assert tripo["key_source"] == "env"


def test_job_status_missing_job() -> None:
    async def scenario():
        async with create_connected_server_and_client_session(make_server()) as client:
            await client.initialize()
            return await client.call_tool("job_status", {"job_id": "j_20260101T000000Z_deadbeef"})

    result = run(scenario())
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "job_not_found"


# -- HTTP bearer auth --------------------------------------------------------


def _app(token: str) -> Starlette:
    async def homepage(request):
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", homepage)])
    app.add_middleware(BearerAuth, token=token)
    return app


def test_bearer_auth_rejects_missing_token() -> None:
    client = TestClient(_app("t0ken"))
    response = client.get("/")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_bearer_auth_accepts_token() -> None:
    client = TestClient(_app("t0ken"))
    response = client.get("/", headers={"Authorization": "Bearer t0ken"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_bearer_token_resolution(monkeypatch) -> None:
    runtime = Runtime.create(workspace=Workspace())
    assert _bearer_token(runtime.loaded, runtime.secrets) is None
    monkeypatch.setenv("XPRINT_TOKEN", "abc")
    assert _bearer_token(runtime.loaded, runtime.secrets) == "abc"
