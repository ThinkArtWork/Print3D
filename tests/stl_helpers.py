"""Small binary STL fixtures shared by the Blender/local-generator tests."""

from __future__ import annotations

import struct

TETRA = (
    ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
    ((0.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 10.0)),
    ((0.0, 0.0, 0.0), (0.0, 0.0, 10.0), (10.0, 0.0, 0.0)),
    ((10.0, 0.0, 0.0), (0.0, 0.0, 10.0), (0.0, 10.0, 0.0)),
)


def binary_stl_bytes() -> bytes:
    payload = bytearray(b"print3d test mesh".ljust(80, b"\0"))
    payload += struct.pack("<I", len(TETRA))
    for a, b, c in TETRA:
        payload += struct.pack("<3f", 0.0, 0.0, 0.0)
        for vertex in (a, b, c):
            payload += struct.pack("<3f", *vertex)
        payload += struct.pack("<H", 0)
    return bytes(payload)


def write_stl(path) -> None:
    path.write_bytes(binary_stl_bytes())
