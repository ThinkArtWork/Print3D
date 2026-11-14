"""Tool registrars, one module per tool family."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..runtime import Runtime
from .analysis import register as register_analysis
from .catalog import register as register_catalog
from .generating import register as register_generating
from .images import register as register_images
from .models import register as register_models
from .printers import register as register_printers
from .slicing import register as register_slicing
from .system import register as register_system

REGISTRARS = (
    register_system,
    register_images,
    register_models,
    register_generating,
    register_analysis,
    register_slicing,
    register_printers,
    register_catalog,
)


def register_all(server: FastMCP, rt: Runtime) -> None:
    """Register every tool family on `server` (order only affects listing order)."""

    for register in REGISTRARS:
        register(server, rt)
