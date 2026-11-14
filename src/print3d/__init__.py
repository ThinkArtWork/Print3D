"""3DPrint — a self-hosted 3D printing MCP server.

The package is organised around a small core (config, workspace, jobs, registry)
and a set of pluggable adapters (providers, CAD, render, slicing, printers).
Core code only depends on the adapter protocols, never on a vendor module.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
