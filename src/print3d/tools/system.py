"""MCP tools: system."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..capabilities import build_capabilities, build_system_status
from ..runtime import Runtime


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="capabilities",
        description=(
            "Report what this 3DPrint installation can do right now: cloud image-to-3D "
            "providers (key present or not), CAD/render backends, slicer profiles and "
            "printers, plus the adapters compiled in. Never returns API keys and never "
            "writes files."
        ),
    )
    def capabilities() -> dict[str, Any]:
        return build_capabilities(rt.loaded, rt.secrets, rt.registry)

    @server.tool(
        name="system_status",
        description=(
            "Report 3DPrint version, Python/platform, workspace paths, config/secrets "
            "presence and job counters. Read-only: writes_performed is always false."
        ),
    )
    def system_status() -> dict[str, Any]:
        return build_system_status(rt.loaded, rt.secrets, rt.workspace, rt.store, rt.registry)
