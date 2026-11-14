"""Static checks for model scripts before Print3D stores or runs them.

An LLM-written ``build123d`` script is still untrusted code.  Print3D does not
execute it in-process, but it does store it and may hand it to the CAD backend,
so a cheap static gate runs first:

* parse errors and oversized scripts are hard failures;
* imports are an **allowlist** (``build123d`` and ``math``) — a denylist would
  let a script reach the same capability through a module we forgot to list;
* attribute access to anything underscore-prefixed is a hard failure, which
  closes the ``().__class__.__base__.__subclasses__()`` route around the import
  allowlist;
* ``__builtins__`` and the usual dynamic-evaluation helpers are hard failures;
* a missing ``build_part`` or missing ``build123d`` import is a hard failure.

This is defence in depth, not a sandbox: the CAD backend still runs scripts as
an external process with its own resource limits and a scrubbed environment.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

MAX_SCRIPT_BYTES = 64 * 1024

#: Model scripts may only import these top-level modules.
ALLOWED_MODULES = frozenset({"build123d", "math"})

#: Names that are never allowed, whether called or merely referenced.
BANNED_NAMES = frozenset(
    {
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "vars",
        "__import__",
    }
)

#: Dotted attribute chains that must not be reached (module-escape routes).
BANNED_DOTTED = frozenset(
    {
        "os.remove",
        "os.rmdir",
        "os.spawnl",
        "os.spawnv",
        "os.system",
        "pathlib.Path",
        "shutil.rmtree",
        "sys.builtins",
        "sys.meta_path",
        "sys.modules",
        "sys.path",
        "sys.path_hooks",
    }
)

#: Dunder globals a script may read; everything else underscore-looking is out.
ALLOWED_DUNDERS = frozenset({"__doc__", "__name__"})


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    message: str
    line: int | None = None

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"severity": self.severity, "message": self.message}
        if self.line is not None:
            data["line"] = self.line
        return data


def dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for an Attribute/Name chain, else None."""
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def access_issues(
    tree: ast.Module,
    *,
    allowed_modules: frozenset[str] = ALLOWED_MODULES,
    allowed_dunders: frozenset[str] = ALLOWED_DUNDERS,
    import_message: str = "imports in a model script are limited to {module}",
) -> list[Issue]:
    """Return import/attribute/name violations for an already-parsed module."""

    issues: list[Issue] = []
    allowed = ", ".join(sorted(allowed_modules))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed_modules:
                    issues.append(
                        Issue(
                            "error",
                            import_message.format(module=f"{alias.name!r} (allowed: {allowed})"),
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in allowed_modules:
                issues.append(
                    Issue(
                        "error",
                        import_message.format(
                            module=f"{(node.module or '')!r} (allowed: {allowed})"
                        ),
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                issues.append(
                    Issue(
                        "error", f"{node.id} is not available to a model script", line=node.lineno
                    )
                )
            elif node.id.startswith("__") and node.id not in allowed_dunders:
                issues.append(
                    Issue("error", f"dunder name {node.id} is not allowed", line=node.lineno)
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                issues.append(
                    Issue(
                        "error",
                        f"attribute access to {node.attr!r} is not allowed",
                        line=node.lineno,
                    )
                )
                continue
            name = dotted(node)
            if name in BANNED_DOTTED:
                issues.append(Issue("error", f"call to {name}() is not allowed", line=node.lineno))
    return issues


def check_script(code: str) -> list[Issue]:
    """Return every policy violation found in ``code`` (empty means usable)."""

    if not code or not code.strip():
        return [Issue("error", "script is empty")]
    if len(code.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return [Issue("error", f"script exceeds {MAX_SCRIPT_BYTES} bytes")]

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            Issue("error", f"script does not parse: {exc.msg}", line=exc.lineno),
        ]

    issues = access_issues(tree)
    has_build123d = False
    has_build_part = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            has_build123d |= any(a.name.split(".")[0] == "build123d" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            has_build123d |= (node.module or "").split(".")[0] == "build123d"
        elif isinstance(node, ast.FunctionDef) and node.name == "build_part":
            has_build_part = True

    if not has_build_part:
        issues.append(Issue("error", "script must define build_part()"))
    if not has_build123d:
        issues.append(Issue("error", "script must import build123d"))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.severity == "error"]


def summarise(issues: list[Issue]) -> dict[str, object]:
    return {
        "ok": not errors(issues),
        "errors": [i.as_dict() for i in errors(issues)],
        "warnings": [i.as_dict() for i in issues if i.severity == "warning"],
    }


__all__ = [
    "ALLOWED_DUNDERS",
    "ALLOWED_MODULES",
    "BANNED_DOTTED",
    "BANNED_NAMES",
    "Issue",
    "MAX_SCRIPT_BYTES",
    "access_issues",
    "check_script",
    "errors",
    "summarise",
]
