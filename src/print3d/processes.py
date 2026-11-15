"""Cross-platform child-process control.

Every external program Print3D starts — a model script's interpreter, Blender,
OrcaSlicer, a local generator — goes through this module so three behaviours stay
the same on POSIX and on Windows:

* **Isolation.**  The child runs in its own group, so a timeout can kill the
  whole tree it created instead of only the process we can see.  POSIX uses
  ``setsid`` + ``killpg``.  Windows has no sessions, so the child joins a Job
  Object carrying ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and a timeout calls
  ``TerminateJobObject``; when a job object cannot be created the fallback is
  ``taskkill /T /F``.
* **Decoding.**  Pipes are decoded as UTF-8 with replacement instead of the
  console code page, so one path outside the ANSI range cannot raise
  ``UnicodeDecodeError`` halfway through a slice.
* **Launching.**  Windows cannot exec a shebang script or a ``.cmd`` shim
  directly.  :func:`resolve_command` honours a ``#!`` line, runs ``.py`` files
  with the current interpreter and routes ``.cmd``/``.bat`` wrappers through
  ``cmd.exe``, so a configured path behaves the same on every platform.

The Job Object handle lives on the ``Popen`` object and is closed by the
:func:`child` context manager, so a finished call cannot leave an orphan behind.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"

#: ``creationflags`` bits used on Windows (see ``CreateProcess``).
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_CMD_SUFFIXES = (".cmd", ".bat")
_PY_SUFFIXES = (".py", ".pyw")
_SHEBANG_LIMIT = 512
_HANDLE = ctypes.c_void_p


# --------------------------------------------------------------- launching ----
def resolve_command(command: Sequence[str]) -> list[str]:
    """Return the argv that actually starts ``command`` on this platform.

    On POSIX the list is returned unchanged.  On Windows the first element is
    inspected: a native executable is used as is, a file with a ``#!`` line is
    started through the interpreter that line names, a ``.py`` file through the
    running interpreter, and a ``.cmd``/``.bat`` wrapper through ``cmd.exe``.
    Anything whose program cannot be located is passed through unchanged so the
    caller still receives the operating system's own "not found" error.
    """

    argv = [str(part) for part in command]
    if not IS_WINDOWS or not argv:
        return argv

    program = _locate(argv[0])
    if program is None or _is_native_binary(program):
        return argv
    first_line = _first_line(program)
    if first_line is not None and first_line.startswith(b"#!"):
        # A POSIX-style shim: honour its interpreter when Windows can run it,
        # otherwise treat the file as the Python script it almost always is.
        interpreter = _shebang(program) or [sys.executable]
        return [*interpreter, str(program), *argv[1:]]
    suffix = program.suffix.lower()
    if suffix in _CMD_SUFFIXES:
        comspec = os.environ.get("COMSPEC") or _system32("cmd.exe")
        return [comspec, "/d", "/c", str(program), *argv[1:]]
    if suffix in _PY_SUFFIXES:
        return [sys.executable, str(program), *argv[1:]]
    return argv


def _locate(program: str) -> Path | None:
    """Resolve ``program`` to an existing file, keeping PATH lookups intact."""

    candidate = Path(program).expanduser()
    if candidate.is_file():
        return candidate
    found = shutil.which(program)
    return Path(found) if found else None


def split_command_text(text: str) -> list[str]:
    """Split a configured command *string* the way this platform expects.

    POSIX shells split with ``shlex``.  A Windows command line is not a POSIX
    one — backslashes are path separators, not escapes — so POSIX splitting would
    turn ``C:\\Python313\\python.exe`` into ``CPython313python.exe``.  There,
    quotes are removed from each token and everything else is kept verbatim.

    Prefer an argv list in configuration; this is only for convenience strings.
    """

    if not IS_WINDOWS:
        return shlex.split(text)
    argv: list[str] = []
    for token in shlex.split(text, posix=False):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        if token:
            argv.append(token)
    return argv


def _is_native_binary(path: Path) -> bool:
    """True for a Windows PE image, which ``CreateProcess`` starts directly."""

    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return True  # unreadable: let Popen report the real problem


def _system32(name: str) -> str:
    """Absolute path to a Windows system tool, with a PATH fallback.

    Resolving ``taskkill`` through ``PATH`` would let anything earlier on the
    path win; ``SystemRoot\\System32`` is where these tools actually live.
    """

    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(root) / "System32" / name
    return str(candidate) if candidate.is_file() else name


def _shebang(path: Path) -> list[str] | None:
    """Parse a usable ``#!`` interpreter argv, or ``None``.

    On Windows a bare name such as ``python3`` resolves to the Store's
    0-byte App Execution Alias, and a POSIX path such as ``/usr/bin/env`` cannot
    run at all.  Both are rejected so the caller can fall back to the running
    interpreter instead of failing with exit code 9009.
    """

    first = _first_line(path)
    if first is None or not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").split()
    if parts and Path(parts[0]).name.lower() == "env":
        parts = parts[1:]
        while parts and parts[0].startswith("-"):  # env -S python3 -u
            parts = parts[1:]
    if not parts or parts[0].startswith("/"):
        return None
    located = _locate(parts[0])
    if located is None or not _is_usable_program(located):
        return None
    return parts


def _first_line(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            return handle.readline(_SHEBANG_LIMIT)
    except OSError:
        return None


def _is_usable_program(path: Path) -> bool:
    """False for an App Execution Alias: a 0-byte stub that only opens the Store."""

    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# -------------------------------------------------------------- job object ----
class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_API: Any = None
_JOB_API_LOADED = False


def _job_api() -> Any:
    """Load the kernel32 entry points used for Job Objects, once.

    Returns ``None`` on POSIX or when kernel32 cannot be used, which makes every
    caller fall back to the portable path instead of failing.
    """

    global _JOB_API, _JOB_API_LOADED
    if _JOB_API_LOADED:
        return _JOB_API
    _JOB_API_LOADED = True
    if not IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    try:  # pragma: no cover - Windows only
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = _HANDLE
        kernel32.CreateJobObjectW.argtypes = [_HANDLE, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            _HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [_HANDLE, ctypes.c_uint]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [_HANDLE]
        kernel32.LimitInformation = _ExtendedLimitInformation
    except Exception:  # noqa: BLE001 - any loader problem means "no job objects"
        return None
    _JOB_API = kernel32
    return _JOB_API


def open_job() -> Any:
    """Create a kill-on-close job object, or ``None`` when unavailable."""

    api = _job_api()
    if api is None:  # pragma: no cover - Windows only
        return None
    handle = api.CreateJobObjectW(None, None)
    if not handle:  # pragma: no cover - Windows only
        return None
    try:  # pragma: no cover - Windows only
        info = api.LimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = api.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except Exception:  # noqa: BLE001 - pragma: no cover
        ok = 0
    if not ok:  # pragma: no cover - Windows only
        _close_handle(handle)
        return None
    return handle


def _attach_job(process: subprocess.Popen[str]) -> Any:
    """Put ``process`` into a fresh kill-on-close job; ``None`` if impossible."""

    api = _job_api()
    if api is None:  # pragma: no cover - Windows only
        return None
    handle = open_job()
    if not handle:  # pragma: no cover - Windows only
        return None
    try:  # pragma: no cover - Windows only
        raw = int(process._handle)  # type: ignore[attr-defined]
        if api.AssignProcessToJobObject(handle, _HANDLE(raw)):
            return handle
    except Exception:  # noqa: BLE001 - pragma: no cover
        pass
    _close_handle(handle)
    return None


def _close_handle(handle: Any) -> None:
    api = _job_api()
    if api is None or not handle:  # pragma: no cover - Windows only
        return
    with contextlib.suppress(Exception):
        api.CloseHandle(handle)


def _release_job(process: subprocess.Popen[str]) -> None:
    """Close the process's job handle; kill-on-close reaps what is left."""

    handle = getattr(process, "_print3d_job", None)
    if not handle:  # pragma: no cover - Windows only
        return
    process._print3d_job = None  # type: ignore[attr-defined]
    _close_handle(handle)


def terminate_tree(process: subprocess.Popen[str]) -> None:
    """Kill ``process`` and everything it started, as hard as the OS allows."""

    if IS_WINDOWS:  # pragma: no cover - Windows only
        handle = getattr(process, "_print3d_job", None)
        api = _job_api()
        if handle and api is not None:
            with contextlib.suppress(Exception):
                api.TerminateJobObject(handle, 1)
            return
        _taskkill(process.pid)
        with contextlib.suppress(OSError, AttributeError):
            process.kill()
        return

    with contextlib.suppress(ProcessLookupError, PermissionError, AttributeError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(OSError, AttributeError):
        process.kill()


def _taskkill(pid: int) -> None:  # pragma: no cover - Windows only
    """Fallback tree kill for when no job object was created."""

    if not pid:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [_system32("taskkill.exe"), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )


# ------------------------------------------------------------------ launch ----
@contextlib.contextmanager
def child(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> Iterator[subprocess.Popen[str]]:
    """Start ``command`` isolated from ours, and clean up when the block ends.

    The pipes are text mode UTF-8 with ``errors="replace"``.  That is a two-sided
    contract: a Python child started through :func:`print3d.cad.clean_env` is told
    to write UTF-8, and a native tool that prints in the ANSI code page shows
    replacement characters instead of raising ``UnicodeDecodeError`` in the middle
    of a slice.  ``OSError`` from a failed launch propagates to the caller, which
    already words that failure for the tool response.
    """

    argv = resolve_command(command)
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if IS_WINDOWS:  # pragma: no cover - Windows only
        options["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True

    process = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        **options,
    )
    try:
        if IS_WINDOWS:  # pragma: no cover - Windows only
            handle = _attach_job(process)
            if handle:
                process._print3d_job = handle  # type: ignore[attr-defined]
        yield process
    finally:
        if IS_WINDOWS:  # pragma: no cover - Windows only
            _release_job(process)


# --------------------------------------------------------------- reporting ----
def run_capture(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a short-lived command and capture its output as UTF-8 text.

    Same launcher shim and decoding rules as :func:`child`, without a job object:
    this is for probes that are expected to finish on their own (``--help``, a
    version string).  A non-zero exit code is returned, not raised — the caller
    decides what the text means.
    """

    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "check": False,
        "timeout": timeout,
        "env": env,
    }
    if IS_WINDOWS:  # pragma: no cover - Windows only
        options["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(
        resolve_command(command),
        cwd=str(cwd) if cwd is not None else None,
        **options,
    )


def describe_limits() -> dict[str, Any]:
    """What this platform can actually enforce around generated code.

    ``capabilities`` reports this verbatim so an agent never promises a sandbox
    the machine cannot provide: Windows has a wall-clock budget and a tree kill,
    but no ``RLIMIT_CPU``/``RLIMIT_FSIZE`` equivalent.
    """

    if IS_WINDOWS:  # pragma: no cover - Windows only
        job_objects = _job_api() is not None
        return {
            "platform": "windows",
            "wall_clock_timeout": True,
            "tree_kill": True,
            "kill_method": "job object" if job_objects else "taskkill /T /F",
            "cpu_time_limit": False,
            "file_size_limit": False,
            "open_file_limit": False,
            "notes": [
                "Windows has no resource module: CPU seconds, file size and open "
                "file counts are bounded only by the wall-clock timeout.",
                "Generated code still runs in a separate interpreter with a "
                "scrubbed environment, but the POSIX rlimits have no equivalent.",
            ],
        }
    return {
        "platform": "posix",
        "wall_clock_timeout": True,
        "tree_kill": True,
        "kill_method": "setsid + killpg(SIGKILL)",
        "cpu_time_limit": True,
        "file_size_limit": True,
        "open_file_limit": True,
        "notes": [
            "Generated code runs in its own session with RLIMIT_CPU, RLIMIT_FSIZE "
            "and RLIMIT_NOFILE applied before the user script is imported.",
        ],
    }


__all__ = [
    "IS_WINDOWS",
    "child",
    "describe_limits",
    "open_job",
    "resolve_command",
    "run_capture",
    "split_command_text",
    "terminate_tree",
]
