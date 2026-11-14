"""Static review for bpy scripts before Blender executes them.

Blender's Python is a full interpreter: the review below is defence in depth,
not a sandbox.  The real boundary is the background Blender process plus the
resource limits and environment scrubbing in :mod:`print3d.blender`.
"""

from __future__ import annotations

import ast

from .script_policy import MAX_SCRIPT_BYTES, Issue

BANNED_MODULES = {
    "ctypes",
    "ftplib",
    "http",
    "multiprocessing",
    "os",
    "pathlib",
    "requests",
    "shutil",
    "signal",
    "socket",
    "subprocess",
    "telnetlib",
    "urllib",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "input", "breakpoint", "open"}
BANNED_ATTR_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("shutil", "rmtree"),
    ("pathlib", "Path"),
}
ALLOWED_MODULES = {
    "bpy",
    "bmesh",
    "mathutils",
    "math",
    "sys",
    "random",
    "itertools",
    "functools",
    "json",
    "re",
}


def check_blender_script(code: str) -> list[Issue]:
    """Return structural and policy issues; an empty list means "may run"."""

    issues: list[Issue] = []
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

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    issues.append(
                        Issue(
                            "error",
                            f"Importing {root!r} is not allowed in a bpy script.",
                            node.lineno,
                        )
                    )
                elif root not in ALLOWED_MODULES:
                    issues.append(
                        Issue(
                            "warning",
                            f"{root!r} is unusual in a bpy script; keep it minimal.",
                            node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                issues.append(
                    Issue(
                        "error", f"Importing {root!r} is not allowed in a bpy script.", node.lineno
                    )
                )
            elif root and root not in ALLOWED_MODULES:
                issues.append(
                    Issue(
                        "warning",
                        f"{root!r} is unusual in a bpy script; keep it minimal.",
                        node.lineno,
                    )
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                issues.append(Issue("error", f"Calling {func.id}() is not allowed.", node.lineno))
            elif isinstance(func, ast.Attribute):
                owner = func.value.id if isinstance(func.value, ast.Name) else None
                if owner and (owner, func.attr) in BANNED_ATTR_CALLS:
                    issues.append(
                        Issue(
                            "error", f"Calling {owner}.{func.attr}() is not allowed.", node.lineno
                        )
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
