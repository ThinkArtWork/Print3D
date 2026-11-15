"""Read a Windows file's DACL without extra dependencies.

POSIX protects ``secrets.json`` with mode 0600 and warns when other users can
read it.  Windows has no mode bits, so the equivalent question is "does the DACL
grant read access to a group wider than the signed-in account?" — which is what
this module answers.  It only *reads* the ACL: nothing here changes permissions.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: Groups whose read access means "anyone on this machine can read the file".
WIDE_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "INTERACTIVE",
    "S-1-2-1": "CONSOLE LOGON",
}

#: ACCESS_MASK bits that let a trustee read the file:
#: FILE_READ_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES | GENERIC_READ | GENERIC_ALL
_READ_MASK = 0x0001 | 0x0008 | 0x0080 | 0x80000000 | 0x10000000

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0x00


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


_ADVAPI: object | None = None
_ADVAPI_LOADED = False


def _advapi():
    """Load the advapi32 entry points once; ``None`` when unavailable."""

    global _ADVAPI, _ADVAPI_LOADED
    if _ADVAPI_LOADED:
        return _ADVAPI
    _ADVAPI_LOADED = True
    if not IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    try:  # pragma: no cover - Windows only
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.GetNamedSecurityInfoW.restype = ctypes.c_uint32
        advapi.GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi.GetAclInformation.restype = ctypes.c_int
        advapi.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        advapi.GetAce.restype = ctypes.c_int
        advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        advapi.ConvertSidToStringSidW.restype = ctypes.c_int
        advapi.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
    except Exception:  # noqa: BLE001 - any loader problem means "cannot inspect"
        return None
    _ADVAPI = advapi
    return _ADVAPI


def _local_free(pointer) -> None:
    """Free a buffer allocated by the Win32 security APIs (LocalAlloc)."""

    with contextlib.suppress(Exception):
        ctypes.windll.kernel32.LocalFree(pointer)


def wide_readers(path: str | os.PathLike[str]) -> list[str]:
    """Return the names of broad groups that can read ``path``.

    Empty means only the owner, SYSTEM and the administrators can read it — the
    Windows equivalent of a 0600 file.  Failures (missing file, no DACL, an API
    the runtime cannot load) also return an empty list: this is a warning aid,
    not a security boundary.
    """

    if not IS_WINDOWS:  # pragma: no cover - Windows only
        return []
    advapi = _advapi()
    if advapi is None:  # pragma: no cover - Windows only
        return []
    target = str(Path(path))
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    try:  # pragma: no cover - Windows only
        status = advapi.GetNamedSecurityInfoW(
            target,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0 or not dacl:
            return []
        size = _AclSizeInformation()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(size), ctypes.sizeof(size), _SIZE_INFORMATION
        ):
            return []
        found: list[str] = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                continue
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                continue  # deny ACEs and object ACEs are not what we ask about
            allowed = ctypes.cast(ace, ctypes.POINTER(_AccessAllowedAce)).contents
            if not allowed.Mask & _READ_MASK:
                continue
            sid_pointer = ctypes.c_void_p(
                ace.value + _AccessAllowedAce.SidStart.offset  # type: ignore[attr-defined]
            )
            text = ctypes.c_wchar_p()
            if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                continue
            try:
                name = WIDE_SIDS.get(text.value or "")
            finally:
                _local_free(text)
            if name and name not in found:
                found.append(name)
        return found
    finally:  # pragma: no cover - Windows only
        if descriptor:
            _local_free(descriptor)


__all__ = ["IS_WINDOWS", "WIDE_SIDS", "wide_readers"]
