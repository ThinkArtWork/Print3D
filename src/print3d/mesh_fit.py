"""Turn an existing mesh into a calibrated, printable, shareable asset.

This is the finishing half of image-to-3D: the generated mesh is the shape
truth, and the caller supplies the size truth. Two sizing modes are supported:

* ``size_mm=(x, y, z)`` — exact product envelope, non-uniform scale.
* ``max_dimension_mm`` — uniform scale so the longest side matches; this is the
  general fallback when only a rough display size is known and proportions must
  be preserved.

The script imports STL/GLB/OBJ, repairs the surface, scales, seats the part on
``z=0``, and reduces the triangle count so ``model_preview`` renders the whole
mesh instead of a subsample.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

SUPPORTED_SUFFIXES = {".stl", ".glb", ".gltf", ".obj"}
DEFAULT_MAX_TRIANGLES = 145_000
PREVIEW_TRIANGLES = 150_000
VIEWER_TRIANGLES = 400_000
MAX_SIZE_MM = 10_000.0


def target_spec(
    size_mm: Sequence[float] | None,
    max_dimension_mm: float = 0.0,
) -> tuple[tuple[float, float, float] | None, float]:
    """Validate the two sizing modes: exact XYZ or uniform longest side."""

    has_size = bool(size_mm)
    has_uniform = float(max_dimension_mm or 0) > 0
    if has_size and has_uniform:
        raise ValueError("Pass either size_mm or max_dimension_mm, not both")
    if has_size:
        assert size_mm is not None
        if len(size_mm) != 3:
            raise ValueError("size_mm must contain exactly three values: [x, y, z]")
        exact = tuple(float(v) for v in size_mm)
        if min(exact) <= 0 or max(exact) > MAX_SIZE_MM:
            raise ValueError("size_mm values must be greater than 0 and no more than 10000 mm")
        return exact, 0.0
    if has_uniform:
        uniform = float(max_dimension_mm)
        if uniform > MAX_SIZE_MM:
            raise ValueError("max_dimension_mm must be no more than 10000 mm")
        return None, uniform
    raise ValueError("Pass size_mm=[x, y, z] or max_dimension_mm=<longest side>")


def fit_script(
    source: str | Path,
    size_mm: Sequence[float] | None,
    max_triangles: int = DEFAULT_MAX_TRIANGLES,
    *,
    max_dimension_mm: float = 0.0,
) -> str:
    """Build a self-contained bpy script for one mesh-fit revision."""

    source_path = str(Path(source).expanduser().resolve())
    exact, uniform = target_spec(size_mm, max_dimension_mm)
    if not 1_000 <= int(max_triangles) <= VIEWER_TRIANGLES:
        raise ValueError(f"max_triangles must be between 1000 and {VIEWER_TRIANGLES}")
    target = exact or (0.0, 0.0, 0.0)

    # fmt: off
    return f'''import bpy
import bmesh
from mathutils import Vector

SOURCE = {source_path!r}
TARGET = Vector(({float(target[0])!r}, {float(target[1])!r}, {float(target[2])!r}))
UNIFORM_MM = {float(uniform)!r}
MAX_TRIS = {int(max_triangles)}
SUFFIX = SOURCE.lower().rsplit('.', 1)[-1]

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

if SUFFIX == 'stl':
    bpy.ops.wm.stl_import(filepath=SOURCE)
elif SUFFIX in ('glb', 'gltf'):
    bpy.ops.import_scene.gltf(filepath=SOURCE)
elif SUFFIX == 'obj':
    bpy.ops.wm.obj_import(filepath=SOURCE)
else:
    raise RuntimeError('Unsupported mesh format: ' + SOURCE)

meshes = [ob for ob in bpy.context.scene.objects if ob.type == 'MESH']
if not meshes:
    raise RuntimeError('No mesh object found in ' + SOURCE)
for ob in meshes:
    ob.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.object
obj.select_set(True)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def repair(dist):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=dist)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    holes = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    if holes:
        bmesh.ops.holes_fill(bm, edges=holes, sides=0)
    bm.to_mesh(obj.data)
    obj.data.update()
    bm.free()


def bounds():
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    low = Vector((
        min(v.x for v in corners),
        min(v.y for v in corners),
        min(v.z for v in corners),
    ))
    high = Vector((
        max(v.x for v in corners),
        max(v.y for v in corners),
        max(v.z for v in corners),
    ))
    return low, high


repair(0.01)
low, high = bounds()
extent = high - low
if min(extent.x, extent.y, extent.z) <= 1e-9:
    raise RuntimeError('Degenerate mesh extent: ' + str(tuple(extent)))

if UNIFORM_MM > 0.0:
    factor = UNIFORM_MM / max(extent.x, extent.y, extent.z)
    target = extent * factor
else:
    target = TARGET
obj.scale = (target.x / extent.x, target.y / extent.y, target.z / extent.z)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

low, high = bounds()
obj.location.x -= (low.x + high.x) / 2.0
obj.location.y -= (low.y + high.y) / 2.0
obj.location.z -= low.z
bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)

if len(obj.data.polygons) > MAX_TRIS:
    decimate = obj.modifiers.new('fit_reduce', 'DECIMATE')
    decimate.ratio = MAX_TRIS / float(len(obj.data.polygons))
    bpy.ops.object.modifier_apply(modifier=decimate.name)

triangulate = obj.modifiers.new('fit_triangulate', 'TRIANGULATE')
bpy.ops.object.modifier_apply(modifier=triangulate.name)
repair(0.005)

# Decimation can shave a few microns off the envelope; pin it back exactly.
low, high = bounds()
extent = high - low
obj.scale = (target.x / extent.x, target.y / extent.y, target.z / extent.z)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
low, high = bounds()
obj.location.x -= (low.x + high.x) / 2.0
obj.location.y -= (low.y + high.y) / 2.0
obj.location.z -= low.z
bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)

for polygon in obj.data.polygons:
    polygon.use_smooth = True

low, high = bounds()
print('FIT_SOURCE', SOURCE)
print('FIT_MODE', 'uniform' if UNIFORM_MM > 0.0 else 'exact')
print('FIT_TARGET', tuple(round(v, 3) for v in target))
print('FIT_BBOX', tuple(round(v, 3) for v in (high - low)))
print('FIT_MIN', tuple(round(v, 3) for v in low))
print('FIT_VERTICES', len(obj.data.vertices), 'FIT_POLYGONS', len(obj.data.polygons))
'''
    # fmt: on


__all__ = [
    "DEFAULT_MAX_TRIANGLES",
    "PREVIEW_TRIANGLES",
    "SUPPORTED_SUFFIXES",
    "VIEWER_TRIANGLES",
    "fit_script",
    "target_spec",
]
