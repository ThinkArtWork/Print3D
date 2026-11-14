"""Static review for bpy scripts before Blender executes them.

Blender's Python is a full interpreter: the review below is defence in depth,
not a sandbox.  The real boundary is the background Blender process plus the
resource limits and environment scrubbing in :mod:`print3d.blender`.

Imports are an allowlist.  ``sys`` is allowed because the runner passes input
paths through ``sys.argv``, but the module-escape attributes (``sys.modules``,
``sys.path``, ...) are rejected by :func:`print3d.script_policy.access_issues`.
"""

from __future__ import annotations

import ast

from .script_policy import MAX_SCRIPT_BYTES, Issue, access_issues

ALLOWED_MODULES = frozenset(
    {
        "bmesh",
        "bpy",
        "functools",
        "itertools",
        "json",
        "math",
        "mathutils",
        "random",
        "re",
        "sys",
    }
)


def check_blender_script(code: str) -> list[Issue]:
    """Return structural and policy issues; an empty list means "may run"."""

    if not isinstance(code, str) or not code.strip():
        return [Issue("error", "The script is empty.", None)]
    if len(code.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return [
            Issue(
                "error",
                f"The script is larger than {MAX_SCRIPT_BYTES // 1024} KiB.",
                None,
            )
        ]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [Issue("error", f"Python syntax error: {exc.msg}", exc.lineno)]

    issues = access_issues(
        tree,
        allowed_modules=ALLOWED_MODULES,
        allowed_dunders=frozenset({"__doc__", "__name__"}),
        import_message="importing {module} is not allowed in a bpy script",
    )

    if "bpy" not in code:
        issues.append(Issue("error", "The script does not use bpy; it cannot build a scene.", None))
    if not any(word in code for word in ("primitive_", "bmesh", "from_pydata", "mesh.from_pydata")):
        issues.append(
            Issue(
                "warning",
                "No mesh creation call found; Blender will export an empty scene unless the script builds geometry.",
                None,
            )
        )
    return issues


__all__ = ["ALLOWED_MODULES", "check_blender_script"]
