"""Static checks for model scripts before XPrint stores or runs them.

An LLM-written ``build123d`` script is still untrusted code.  XPrint does not
execute it in-process, but it does store it and may hand it to the CAD backend,
so the cheap static gate runs first:

* parse errors and oversized scripts are hard failures;
* imports of process/network/file-shuffling modules are hard failures;
* calls to ``eval``/``exec``/``os.system`` and friends are hard failures;
* a missing ``build_part`` or missing ``build123d`` import is a hard failure;
* anything merely suspicious (e.g. ``open``) is a warning for the operator.

This is defence in depth, not a sandbox: the CAD backend must still run scripts
as an external process with its own limits.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

MAX_SCRIPT_BYTES = 64 * 1024

BANNED_MODULES = frozenset(
    {
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
)

BANNED_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint", "input"})

BANNED_ATTR_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "os.spawnl",
        "os.spawnv",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "shutil.rmtree",
        "pathlib.Path.unlink",
        "pathlib.Path.write_text",
    }
)


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


def _dotted(node: ast.AST) -> str | None:
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


def check_script(code: str) -> list[Issue]:
    """Return every policy violation found in ``code`` (empty means usable)."""

    issues: list[Issue] = []
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

    has_build123d = False
    has_build_part = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "build123d":
                    has_build123d = True
                if root in BANNED_MODULES:
                    issues.append(
                        Issue(
                            "error",
                            f"import of {root!r} is not allowed in a model script",
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "build123d":
                has_build123d = True
            if root in BANNED_MODULES:
                issues.append(
                    Issue(
                        "error",
                        f"import of {root!r} is not allowed in a model script",
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.FunctionDef) and node.name == "build_part":
            has_build_part = True
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
                issues.append(
                    Issue("error", f"call to {node.func.id}() is not allowed", line=node.lineno)
                )
            elif name in BANNED_ATTR_CALLS:
                issues.append(Issue("error", f"call to {name}() is not allowed", line=node.lineno))
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                issues.append(
                    Issue(
                        "warning",
                        "script calls open(); export through build123d helpers instead",
                        line=node.lineno,
                    )
                )

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


__all__ = ["Issue", "MAX_SCRIPT_BYTES", "check_script", "errors", "summarise"]
