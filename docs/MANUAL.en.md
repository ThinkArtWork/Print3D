# Print3D Manual

[中文](MANUAL.md) · [English](MANUAL.en.md)

> This is an operator's manual for users: from installation and MCP host wiring, through modelling, preview, slicing and sending a print, to where to look first when something goes wrong.
>
> - To get the project overview first: see "Quick start" and "Tools" in the [README](../README.en.md).
> - If you just want it running: use the agent install prompt in §1.2, or install manually per §1.3.
> - Need every slicing parameter and printer field: see §8 and §9; troubleshooting is in §12.
>
> Commands below are run from the repository root; `print3d` means `.venv/bin/print3d` inside the repository virtualenv (`.venv\Scripts\print3d.exe` on Windows) — `uv run print3d` works too.

## Table of contents

1. [Installation and initialisation](#1-installation-and-initialisation)
2. [Connecting an MCP host](#2-connecting-an-mcp-host)
3. [Your first part](#3-your-first-part)
4. [Assets, revisions and jobs](#4-assets-revisions-and-jobs)
5. [Choosing a modelling path](#5-choosing-a-modelling-path)
6. [Preview, drawings and export](#6-preview-drawings-and-export)
7. [Printability check and orientation](#7-printability-check-and-orientation)
8. [Slicing](#8-slicing)
9. [Printers and sending a print](#9-printers-and-sending-a-print)
10. [Configuration and secrets](#10-configuration-and-secrets)
11. [Security boundaries](#11-security-boundaries)
12. [Troubleshooting](#12-troubleshooting)
13. [CLI quick reference](#13-cli-quick-reference)
14. [Common recipes](#14-common-recipes)
15. [Upgrade, backup and uninstall](#15-upgrade-backup-and-uninstall)
16. [More documentation](#16-more-documentation)

---

## 1. Installation and initialisation

### 1.1 What you need

| Item | Requirement | Notes |
| --- | --- | --- |
| Python | 3.11+ | 3.11 / 3.12 / 3.13 all work |
| System | macOS / Linux / Windows 10–11 x64 | Subprocess isolation on Windows is a Job Object plus a wall-clock timeout, without POSIX `rlimit`; see §11 |
| [uv](https://docs.astral.sh/uv/) | Recommended | Your own venv + `pip` works too |
| Disk | About 250 MB and up | build123d / OCP are the bulk of it |
| Blender (optional) | 3.x+ | Required by `blender_run`, `mesh_fit` |
| OrcaSlicer (optional) | 2.x | Required by `slice_*`; uses the CLI you already installed |

**No API key is required** for parametric modelling, preview, printability checks and export. Keys only affect optional paths such as concept images and cloud generation.

### 1.2 Let an agent install it (recommended for newcomers)

You do not have to type every command yourself. Copy the block below (GitHub has a copy button in the top-right corner of a code block) and hand it to an agent that can run commands:

```text
You are my installation assistant. Install the Print3D MCP server on this macOS / Linux / Windows machine:

Repository: https://github.com/ThinkArtWork/Print3D
Dependencies: Python 3.11+, git, uv.

1. Check the system and dependencies; if uv is missing, install it the official way; if Python is too old, tell me — do not change the system Python on your own.
2. Clone the repository to a suitable directory, then run uv sync and uv run print3d init.
3. Run uv run print3d capabilities and report CAD, rendering, slicer and printer status.
4. Check whether OrcaSlicer and Blender are installed; if missing, only report it — do not download large files automatically.
5. Generate a stdio config for my MCP host (DSH / Claude Desktop / Cursor); set command to the absolute path of print3d inside the repository virtualenv (.venv/bin/print3d on macOS/Linux, .venv\Scripts\print3d.exe on Windows), and show it to me for confirmation before writing.
6. Do not ask for API keys; core features do not need one. Finally, give me a test prompt.
```

### 1.3 Installing from source

```bash
git clone https://github.com/ThinkArtWork/Print3D.git
cd Print3D
uv sync
uv run print3d version
```

Windows uses the same commands; only the entry point moves to `.venv\Scripts\` (PowerShell):

```powershell
git clone https://github.com/ThinkArtWork/Print3D.git
cd Print3D
uv sync
.\.venv\Scripts\print3d.exe version
```

`uv sync` creates `.venv` and installs the runtime dependencies. To run lint, add the dev dependencies:

```bash
uv sync --extra dev
uv run ruff check src
```

> On Windows, an OrcaSlicer `--help` that prints nothing does not break slicing: Print3D only trusts the help text when there is some, and otherwise treats the CLI flags as unverified and carries on.<br>
> Windows needs 64-bit Python; `cadquery-ocp-novtk` has no win_arm64 wheel, so Windows on ARM is not supported yet.

> Print3D has not been published to PyPI yet. Until the first GitHub Release appears, do not use `pipx install print3d`; installing from source is the only reliable way. Once a release is out, you can also `uv tool install .` or `pipx install .` to put the command on your PATH.

### 1.4 Initialising the workspace

```bash
uv run print3d init
uv run print3d capabilities
```

The default workspace is `~/.print3d`. `init` creates the directory skeleton and `config.json`, **writes no secrets**, and does not overwrite existing configuration; add `--force` when you really want it rewritten.

```bash
# Multiple instances / tests: use another workspace
uv run print3d init --workspace /path/to/print3d-workspace

# Or use an environment variable
export PRINT3D_WORKSPACE=/path/to/print3d-workspace
```

The workspace then contains:

```text
<workspace>/
  assets/         # one directory per asset; revisions live in v001/, v002/…
  cache/          # rebuildable cache such as preview images
  config.json     # non-sensitive configuration
  exports/        # default destination for asset_export (created on first export)
  jobs/           # background job records, job.json
  logs/
  profiles/       # slicing profiles (e.g. your U1 preset)
  references/     # generated concept images
  secrets.json    # optional; permissions must be 0600
  slices/         # slicing output: plate_1.gcode, statistics.json
```

`print3d config` prints the merged effective configuration; for secrets it only ever shows "present or not", never the value. Precedence is:

```text
CLI flags > environment variables > <workspace>/config.json > built-in defaults
```

---

## 2. Connecting an MCP host

Print3D is a standard MCP server and has no UI of its own. A host (DSH, Claude Desktop, Codex, Cursor, …) launches it, and then the host's model calls the tools.

### 2.1 stdio (most common)

Add an entry to the host's MCP configuration:

```json
{
  "mcpServers": {
    "print3d": {
      "command": "/absolute/path/to/Print3D/.venv/bin/print3d",
      "args": ["serve", "--stdio"],
      "env": {
        "PRINT3D_WORKSPACE": "/absolute/path/to/.print3d"
      }
    }
  }
}
```

Two things people trip over:

- **The command must be an absolute path.** The host will not `cd` into the repository for you, and will not expand `~`. On Windows, point `command` at the repository's `.venv\Scripts\print3d.exe` and write backslashes as `\\` in the JSON.
- If you want the workspace to follow the project, point `PRINT3D_WORKSPACE` at the project's `.print3d`; otherwise `~/.print3d` is used (`%USERPROFILE%\.print3d` on Windows).

### 2.2 DSH

In DSH, add a stdio server under `Settings → MCP` and put the absolute path above in the command field. If your DSH version has no MCP settings page, edit the profile's `cordis.patch.yml` directly, using `mcp-manager` as the entry `id`, then restart DSH.

### 2.3 streamable HTTP

```bash
# listen on loopback only
uv run print3d serve --http

# access from another machine: set a token first
export PRINT3D_TOKEN="$(openssl rand -hex 32)"
uv run print3d serve --http --host 0.0.0.0 --port 8765
```

The default address is `http://127.0.0.1:8765/mcp`. Rules:

- When binding a non-loopback address, **the server refuses to start without a token**;
- Clients use `Authorization: Bearer <token>`;
- Do not expose the port to the public internet. For cross-machine access, put it behind Tailscale / an SSH tunnel / a reverse proxy.

### 2.4 Verifying the connection

Once connected, have the host call these two read-only tools:

```text
capabilities()      # are CAD / render / slicing / provider / printer ready
system_status()     # version, workspace, job counts; writes_performed: false
```

In `capabilities`, look mainly at:

- `cad.available: true`: build123d is available;
- `providers.cloud[].ready`: whether cloud generation has a key configured;
- `targets`: whether slicing targets resolve to presets;
- `printers`: whether printers are configured.

---

## 3. Your first part

Give the host a sentence that is specific enough, for example:

> Make a 120 × 80 × 22 mm three-compartment tray, 2.4 mm walls, 3 mm floor, R8 corners, and output STL and STEP.

The host will usually follow this chain:

```text
capabilities() → model_write(script, confirm="WRITE <name>")
→ model_build(asset_id, confirm="BUILD <asset_id>")
→ model_preview(asset_id) → printability_check(asset_id, target="u1")
→ slice_plan(asset_id, "u1") → slice_run(..., confirm="SLICE <asset_id> u1")
→ printer_status("u1") → send(..., confirm="PRINT u1 <slice_id>")
```

You do not need to remember every step, but you do need to know what each one does, especially `confirm`:

- `model_write` only saves the script (and the reference image); it does not execute geometry;
- `model_build` executes in a subprocess and returns real geometry checks;
- `model_preview` only writes the preview cache; it does not change the asset;
- `slice_plan` is a dry run and writes no files;
- `send` is the only tool that actually makes the printer move; by default it uploads and starts immediately.

### 3.1 A minimal script

```python
from build123d import *

PARAMS = {"size": 40, "thickness": 10, "hole": 8}


def build_part():
    with BuildPart() as part:
        Box(PARAMS["size"], PARAMS["size"], PARAMS["thickness"],
            align=(Align.CENTER, Align.CENTER, Align.MIN))
        with Locations((0, 0, PARAMS["thickness"])):
            Hole(radius=PARAMS["hole"] / 2, depth=PARAMS["thickness"])
    return part.part
```

This script gives you a 40 × 40 × 10 mm block with an 8 mm through-hole in the centre. `model_build` returns something like:

```json
{
  "ok": true,
  "checks": {
    "watertight": true,
    "bbox_mm": [40.0, 40.0, 10.0],
    "volume_cm3": 15.497,
    "solids": 1,
    "faces": 7,
    "triangles": 520,
    "boundary_edges": 0,
    "nonmanifold_edges": 0
  }
}
```

How to read it (everything is under `checks`):

1. `solids == 1`: one solid, not loose pieces;
2. `watertight == true` and `boundary_edges == 0`: closed;
3. `bbox_mm` / `volume_cm3`: whether size and volume match expectations;
4. `nonmanifold_edges == 0`: no non-manifold edges.

If any one of them is off, fix the script before slicing; do not gamble in the slicer.

### 3.2 Using the CLI when you have no host

To check a script quickly, you can bypass the asset store and run it directly:

```bash
uv run print3d cad run model.py --workdir /tmp/print3d-build
```

It writes `model.stl` / `model.step` into `/tmp/print3d-build` and prints the same `checks`. Good for tuning geometry; it creates no asset and does not show up in `asset_list`.

---

## 4. Assets, revisions and jobs

### 4.1 Common identifiers

| ID | Form | Source | Purpose |
| --- | --- | --- | --- |
| `asset_id` | `a_…` | `model_write` / `blender_run` / `generate_*` / `mesh_fit` | One model asset |
| `revision` | `v001`, `v002`… | Appended by each `model_write` | A snapshot of an asset |
| `job_id` | `j_…` | Cloud generation, local generation, slicing, sending | Background job |
| `slice_id` | `s_…` | `slice_run` | The output of one slicing run |

`asset_list()` lists assets and their latest revision; `model_show(asset_id, revision=...)` reads the script of a given revision and lists each revision's prompt, reference image, geometry checks, drawing and viewer under `revisions`.

### 4.2 How revisions are created

- The first `model_write(script, name="parts-tray", confirm="WRITE parts-tray")` creates the asset and `v001`;
- Pass `asset_id` and write again to get `v002`; old revisions are kept as they are;
- `model_build` only builds the given revision; it does not create a new one;
- To go back to an old revision: read the old script with `model_show` and `model_write` it as a new revision (history preserved), or just look at the old geometry with `model_view(..., revision="v001")`.

Do not mix modelling backends within one asset: `model_build` creates `build123d` assets, `blender_run` creates `blender` assets, and `generate_*` creates `cloud` / `local` assets. Mixing them raises a type error.

### 4.3 Async jobs

Cloud generation, local generation, slicing and sending are all background jobs:

```text
generate_cloud(...) / generate_local(...) / slice_run(...) / send(...)
  → returns a job_id immediately
  → poll job_status(job_id), or CLI: print3d job show <job_id>
  → on success, take result (asset_id / slice_id / upload result)
```

`job_status` returns `state`, `progress`, `message`, `result` and any error. When a job fails, do not look at `state` alone: `error.message` and `error.hint` usually already say what to do next.

---

## 5. Choosing a modelling path

| Your input | Recommended path | Entry point | Output |
| --- | --- | --- | --- |
| Text description with explicit dimensions | build123d parametric script | `model_write` → `model_build` | STL + STEP + viewer |
| Text description, concept image first | Concept image + build123d | `image_generate` → `model_write(reference=…)` | Same, with the concept image as reference |
| Sketch / photo / existing concept image | Reference image + build123d | `reference_import` → `model_write(reference=…)` | Same, with a reference |
| Organic shapes, complex chamfers, arrays | Blender bpy script | `blender_run` | STL + GLB + viewer |
| Existing mesh (STL/GLB/OBJ) that needs real dimensions | Mesh calibration | `mesh_fit` | Calibrated STL + GLB |
| Existing STEP, one feature to change | STEP input + build123d | `model_write` → `model_build(input=…)` | New revision of STL + STEP |
| A generation model already on this machine | Local command adapter | `generate_local` | STL asset |
| Willing to call a cloud API | Tripo AI / Tencent Hunyuan 3D | `generate_cloud` | STL asset (can be sized automatically) |

**A text request has two complete paths: by default it goes straight into build123d, with no image generated first; alternatively you can run `image_generate` for a concept image and then model from that image as the `reference`.** The first suits parts whose dimensions and structure are clear; the second suits settling the shape direction first. When you already have a sketch or a photo, use `reference_import`. An image is not a prerequisite, but it is a legitimate optional input.

How to choose: **dimensions first → build123d; shape first → Blender or a generation model; a generated model must land in millimetres via `mesh_fit` or `size_mm`.**

### 5.1 build123d: text → parametric model

The script contract (`model_build` runs static checks first):

1. The first line must be `from build123d import *`;
2. Define `def build_part()` returning **one** solid (`Part` / `Solid`);
3. Put parameters in `PARAMS`, in millimetres;
4. Put the bottom face of the part at `z=0` so it sits on the bed;
5. Only `build123d` and `math` may be imported;
6. No attribute access starting with `_`, and no dynamic entry points such as `eval` / `exec` / `open` / `getattr` / `__import__`;
7. Scripts are capped at 64 KiB.

Static analysis is defence in depth; the real isolation is the subprocess: its own process group, CPU/file/handle limits, a timeout SIGKILL and an environment allowlist. Do not treat it as a multi-tenant sandbox — it assumes the person running the script is you.

On Windows that chain becomes: the child joins a Job Object (closed, it kills the whole tree), pipes are decoded as UTF-8, and a timeout calls `TerminateJobObject`; there are **no** CPU-time, file-size or handle limits, only the wall-clock budget. The `sandbox` field of `print3d capabilities` reports what this machine actually enforces — do not assume parity with macOS / Linux.

**Editing an existing STEP:**

```text
asset_id = model_write(script, name="bracket-v2", confirm="WRITE bracket-v2")
model_build(asset_id, input="/path/to/bracket.step", confirm=f"BUILD {asset_id}")
```

In the script, read the STEP through the module global `INPUT_STEP`:

```python
from build123d import *

def build_part():
    base = import_step(INPUT_STEP)
    # …cut holes / add bosses on base…
    return base
```

`input=` copies the STEP into the current revision and records its sha256, original filename and size; the revision stays rebuildable afterwards even if the original file is moved.

**Iterating:** call `model_write` to append a new revision for every script change, then `model_build`; do not overwrite old revisions. `model_show` shows each revision's prompt and geometry, which makes comparison easy.

### 5.2 Image → reference → model

```text
reference_import(path="/path/sketch.jpg", name="lamp-base", confirm="IMPORT lamp-base")
# returns image_path (a workspace-relative path such as references/ref_.../image.png)
# or: image_generate(prompt="…", name="lamp-base", confirm="IMAGE lamp-base")
  → model_write(script, reference="<image_path>", confirm="WRITE lamp-base")
  → model_build → model_preview        # preview returns the concept image and the render side by side
```

- `reference_import` reads only from the path you give and stores a copy in the workspace `references/`; `image_generate` needs an OpenAI-compatible images API key. The `reference` of `model_write` must be a path **inside the workspace** (`image_path` is), and passing an external absolute path directly is rejected with `path_outside_workspace`.
- Print3D **does not ship a vision model**. It is the host model that looks at the image; it reads proportions, hole positions and structure from the image, while Print3D stores the image on the revision and returns it side by side in the preview for comparison.
- What the image cannot show — the back, internal structure, exact dimensions — has to be added in the prompt, or you should switch to a drawing/STEP.

### 5.3 Blender: organic shapes and bpy scripts

```text
blender_run(script, name="organic-shell", confirm="BLENDER organic-shell")
  → Blender runs the bpy script in background mode
  → exports all MESH objects as model.stl + model.glb
  → same geometry checks, preview and viewer
```

- Suits chamfers, booleans, arrays, modifiers and Blender's own modelling tools; build123d is not involved.
- Only `MESH` objects are exported. Remember to delete helper objects used for boolean cuts, empties, cameras and lights, or they end up in the STL as well.
- Image input: `blender_run(image="/path/ref.png", ...)`; the script receives the same path after `--` in `sys.argv`.
- Append a revision with `blender_run(asset_id=<id>, confirm="BLENDER <asset_id>")`.

### 5.4 mesh_fit: generated mesh → real dimensions

Image-to-3D returns a shape, not dimensions. `mesh_fit` cleans up an existing mesh, scales it, centres it on the bed, decimates it and exports it:

```text
mesh_fit(
  asset_id="a_…",                 # or source="/path/model.stl"
  size_mm=[116, 101, 44],         # known XYZ dimensions: exact scaling
  name="product-shell",
  max_triangles=145000,
  confirm="FIT product-shell",
)
```

When you do not know all three axes and only the longest edge, use proportional mode:

```text
mesh_fit(source="/path/model.stl", max_dimension_mm=120, name="display-piece",
         confirm="FIT display-piece")
```

Details:

- `size_mm` and `max_dimension_mm` are mutually exclusive; passing neither raises an argument error;
- Internally it merges duplicate vertices, fills boundary holes and recomputes normals; after scaling it centres in XY, drops the bottom face to `z=0`, then decimates;
- `max_triangles` defaults to 145,000 so that the four-view preview renders in full; previews above 150,000 are sampled down to a wireframe — that is not a geometry limit;
- Dimensions come from vendor specs or your own measurement; never use the bbox of a generated mesh as the design dimension.

### 5.5 Cloud generation: Tripo AI / Tencent Hunyuan 3D

A cloud provider is an async job: submit → poll → download → convert to STL → create asset → preview. Nothing has to run on your machine.

```text
generate_cloud(provider="tripo", prompt="…", name="lamp-shade",
               confirm="CLOUD lamp-shade")
  → returns a job_id immediately
  → job_status(job_id)
  → resulting asset: model.stl, geometry checks, viewer
```

Optional `size_mm=[x,y,z]` or `max_dimension_mm`: after generation the same mesh fit runs automatically, landing directly in millimetres.

Configuration and caveats:

- Tripo uses `TRIPO_API_KEY`; Hunyuan uses `HUNYUAN3D_API_KEY` (OpenAI-compatible mode), and the older Tencent Cloud TC3 mode is supported too.
- Cloud meshes usually carry no physical units; the default `scale` of 1000 only aligns with vendor export habits; **adjust it against the generated bbox**.
- Each generation consumes provider quota; `usage` records fields such as `credits_consumed`.
- The prompt and reference image leave your machine and go to the corresponding provider; do not use the cloud path for sensitive images.
- On failure, check `job.error.message` and `task_id`; poll timeout is controlled by the provider's configured `timeout_s`.

### 5.6 Self-hosted models: any command that can write STL

You can run TripoSR, Hunyuan3D, ComfyUI or similar models/services locally; Print3D downloads no weights and is not tied to a framework — it only invokes the command you configure. Any local command that reads a prompt/image and writes an STL can be plugged in:

```json
{
  "providers": {
    "local": {
      "triposr": {
        "adapter": "triposr",
        "mode": "image",
        "command": ["/opt/triposr/.venv/bin/python", "/opt/triposr/run.py"],
        "args": ["--image", "{image}", "--out", "{outdir}", "--name", "{name}"],
        "timeout_s": 600
      }
    }
  }
}
```

```text
generate_local(provider="triposr", image="/path/ref.png", name="scan-fix",
               confirm="LOCAL scan-fix")
  → background job; command runs in a clean env with cwd=<outdir>
  → must write <outdir>/mesh.stl (or print {"mesh": "/path/model.stl"} to stdout)
  → Print3D creates the asset, runs geometry checks, and attaches the reference
```

Placeholders: `{prompt}`, `{image}`, `{outdir}`, `{name}`, `{mode}`. `command` / `args` may be lists or strings; strings are split with `shlex` and never go through a shell. This is the "you trust local commands" boundary: do not configure scripts of unknown origin.

---

## 6. Preview, drawings and export

### 6.1 Four-view preview

`model_preview(asset_id, revision="")` renders the STL into ISO / FRONT / TOP / RIGHT views and returns them to the host as image content. It only writes `cache/previews/` and does not change the asset, so it needs no `confirm`.

When a revision has a reference, the preview returns the concept image and the render side by side, which makes it easy to compare "what you asked for" with "what came out". If the preview is lost or you want a different size, just run it again; it is not a deliverable.

### 6.2 Interactive viewer

On a successful build (`model_build`, `blender_run`, `mesh_fit`, `generate_local` / `generate_cloud`) a viewer is generated automatically:

```text
assets/<asset_id>/viewer.html
assets/<asset_id>/<model-name>-viewer.html   # hard link with the same name; the tab title shows the model name
```

One asset has exactly one viewer, with **every built revision** embedded and a dropdown in the top-right corner to switch. Once open it supports rotate, pan, zoom, double-click to reset, spin, screenshot and STL export. It is a single file with no CDN and no server, so you can send it to someone directly.

To refresh it by hand or pick a default revision:

```text
model_view(asset_id, revision="v001", confirm="VIEW <asset_id>")
```

### 6.3 Drawings

```text
model_drawing(asset_id, confirm="DRAWING <asset_id>")
  → drawing.svg / drawing.dxf
```

Third-angle four views (front/top/right/iso); the orthographic views carry dashed hidden lines, while the isometric view omits them by convention; 1:1 real millimetres, with view labels. Good for showing a conventional drawing to a supplier or a colleague; the geometry still comes from the STEP.

### 6.4 Export

```text
asset_export(asset_id, artifact="stl", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="step", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="script", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="svg" | "dxf", confirm="EXPORT <asset_id>")
```

`artifact` only accepts `stl`, `step`, `script`, `svg`, `dxf`; the default destination is the workspace `exports/`. `out_path` must also resolve inside the workspace — export cannot be used to overwrite files outside it.

G-code does not go through `asset_export`: slicing output lives at `slices/<slice_id>/plate_1.gcode` and statistics at `slices/<slice_id>/statistics.json` (whether statistics are written depends on the slicer).

---

## 7. Printability check and orientation

Two read-only tools; no `confirm` needed, and no asset changes:

```text
printability_check(asset_id, target="u1", revision="")
orientation_plan(asset_id, target="u1", limit=3)
```

`printability_check` reports:

- bbox / volume;
- overhang area above 45° and its share;
- support volume estimate;
- bed contact area and whether it exceeds the target bed;
- thin-wall estimate (ray casting; 5th-percentile minimum, share of area below 1.2 mm);
- a 0–100 score plus actionable issues / hints.

`orientation_plan` enumerates 24 axis-aligned orientations (6 down axes × 4 planar spins), ranks them by overhang, support estimate, height and bed contact, and returns the recommended rotation matrix, `down_axis`, `spin_deg` and candidate metrics. It is a recommendation and **never rewrites the asset**; apply the recommended rotation in your slicer or script, then re-run the check.

The score is a geometric heuristic, not FEM, and does not replace a real slice. Thin walls are a ray-sampling estimate, and support volume is estimated as "overhang area × height × 0.25"; the slicer's figure is authoritative for the final support.

Recommended order:

```text
model_build → printability_check → (if problems) orientation_plan → edit script/reorient
→ printability_check → slice_plan
```

---

## 8. Slicing

The slicer is an external process: Print3D only invokes the OrcaSlicer CLI you installed; it does not link against or distribute GPL code, and does not modify your vendor presets.

### 8.1 Configuring a slicing target

Add a target under `targets` in `<workspace>/config.json`:

```json
{
  "targets": {
    "u1": {
      "printer_model": "Snapmaker U1",
      "slicer": "orca",
      "slicer_path": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
      "machine": "/path/to/profiles/u1/machine.json",
      "process": "/path/to/profiles/u1/process.json",
      "filament": ["/path/to/profiles/u1/filament.json"],
      "overrides": {"layer_height": "0.20"},
      "timeout_s": 900
    }
  }
}
```

Key fields:

| Field | Purpose |
| --- | --- |
| `printer_model` | Must match the printer's configured `model`; `send` cross-checks it |
| `slicer` / `slicer_path` | Slicer type and absolute CLI path |
| `machine` / `process` / `filament` | Orca preset paths; flattened single-file profiles also work |
| `overrides` | Fixed process overrides, such as layer height |
| `timeout_s` | Timeout for the external slicing process |

Paths must be absolute, and the files must exist and be readable. `slice_capabilities()` tells you whether each target is `ready`, along with the slicer version and bed size.

### 8.2 Dry run first

```text
slice_capabilities()
slice_plan(asset_id="a_…", target="u1", material="PETG", layer_height=0.24)
```

`slice_plan` writes no files and returns: the resolved presets, the command that would run, geometry checks, a parameter summary, and the **exact confirmation string** `slice_run` needs (of the form `SLICE <asset_id> u1`).

### 8.3 Running and inspecting

```text
slice_run(asset_id="a_…", target="u1",
          confirm="SLICE a_… u1",
          material="PETG", layer_height=0.24,
          infill="gyroid", infill_density=30, strength="strong")
  → job_id
slice_inspect(slice_id="s_…")
```

`slice_inspect` returns the layer count, estimated time, filament, `printer_model`, the G-code path and warnings. When slicing fails, look at the job's stderr and the `slice_inspect` warnings first, then at the preset paths / slicer version.

### 8.4 Common parameters

| Parameter | Values / meaning |
| --- | --- |
| `material` | PLA / PLA+ / PETG / ABS / ASA / TPU / PC / PA and so on; nozzle and bed temperatures are matched automatically |
| `layer_height` | 0.05–1.0 mm |
| `infill` | grid / gyroid / honeycomb / cubic / concentric / triangle / line / rectilinear / zigzag / crosshatch / lightning / adaptivecubic / supportcubic |
| `infill_density` | 0–100%; `-1` inherits the preset |
| `strength` | `light` / `normal` / `strong`, mapped to wall count and top/bottom layer count; `walls`, `top_shells` and `bottom_shells` can also be passed individually |
| `bed_temp` / `nozzle_temp` | Explicitly override the material's default temperatures |
| `build_plate` | `textured_pei` / `smooth_pei` / `cool_plate` / `engineering_plate` / `supertack` / `textured_cool` |
| `flow_ratio` | 0.5–1.5, written to `filament_flow_ratio` |
| `pressure_advance` | 0–0.2, written to `pressure_advance` and enabled |
| `line_width` / `outer_wall_line_width` / `inner_wall_line_width` / `top_surface_line_width` | Line widths in mm; read flow and line width together |
| `arc_fitting` | Whether to emit G2/G3 arcs; enable it when Klipper supports it — rounder holes, smaller files |
| `fan_speed` / `aux_fan_speed` / `fan_first_layers` | Part fan, auxiliary/chamber fan, and turning the fan off for the first layers |
| `printer` + `slot` | Bind a printer and a filament slot; the slot's material/colour is picked up automatically, and a material conflict is rejected |

Material temperatures are mapped automatically from `material`; for fine-tuning, override them explicitly with `bed_temp` / `nozzle_temp`. For multi-toolhead and filament slot configuration on the U1, see §9.3.

### 8.5 Common slicing problems

- **`slice_capabilities` shows a target that is not ready**: nine times out of ten a `machine` / `process` / `filament` path is wrong, or the slicer path is. Confirm each file's existence with absolute paths first.
- **Slicer segfault / preset load failure**: a vendor-customised Orca build may not be compatible with the headless CLI; switch to upstream OrcaSlicer, or flatten the profile (remove `inherits`, expand vendor placeholders).
- **Sliced output exceeds the bed**: `slice_plan` checks the bed; look at the bbox with `printability_check` first, then consider scaling or a different orientation.
- **G-code is empty / no statistics**: check `warnings` in `slice_inspect` and the job stderr; some slicers do not write `statistics.json`, in which case the layer count and filament are parsed from the G-code header.

---

## 9. Printers and sending a print

**Hardware-verified machine: Snapmaker U1** (Moonraker, 4 toolheads, textured PEI). It is the example below; on other Klipper / Moonraker machines just change `base_url` and `model`, and on multi-toolhead machines configure `toolheads` and `slots` for the actual number of heads.

### 9.1 Configuring a printer

`printers` is an array, one object per machine. Moonraker / Klipper example:

```json
{
  "printers": [
    {
      "id": "u1",
      "adapter": "moonraker",
      "base_url": "http://192.168.1.50:7125",
      "model": "Snapmaker U1",
      "toolheads": 4,
      "slots": [
        {"material": "PLA", "color": "#8C9099", "name": "Matte Grey"},
        {"material": "PLA", "color": "#000000", "name": "Matte Black"},
        {"material": "PETG", "color": "#FFFFFF"}
      ],
      "build_plate": "textured_pei",
      "api_key_env": "MOONRAKER_API_KEY",
      "timeout_s": 30
    }
  ]
}
```

You can also write it with a tool instead of editing by hand:

```text
printer_save(printer_id="u1", adapter="moonraker", base_url="http://192.168.1.50:7125",
             model="Snapmaker U1", toolheads=4, build_plate="textured_pei",
             confirm="SAVE PRINTER u1")
```

`printer_save` upserts by `printer_id`; `printer_remove(printer_id="u1", confirm="REMOVE PRINTER u1")` deletes.

### 9.2 Adapters

| Adapter | Fits | Notes |
| --- | --- | --- |
| `moonraker` | Klipper / Snapmaker U1 and so on | `/server/files/upload` + `/printer/print/start`; supports arbitrary G-code calibration commands and JSON calibration factors |
| `octoprint` | OctoPrint | `/api/files/local` + `/api/job` |
| `bambu_lan` | Bambu LAN mode | MQTT/TLS + implicit FTPS + 3MF; community protocol implementation, **not yet verified on hardware** |
| `prusalink` | PrusaLink | HTTP Digest; the password is the one on the printer screen, not an API key |
| `http` | Unknown / in-house printers | Endpoints, field paths and state names all live in configuration; no code changes needed |

Bambu LAN needs `serial` and `access_code_env`; PrusaLink's `username` defaults to `maker`, and the password goes in secrets or `password_env`.

### 9.3 Toolheads and filament slots

- `toolheads` accepts 1–9; multi-nozzle machines number by physical toolhead, single-nozzle AMS by slot.
- `slots` describes each slot's `material` / `color` (`#RRGGBB`) / `name`; the optional `toolhead` field explicitly declares which physical head a slot belongs to.
- When slicing, pass `printer="u1", slot=1`: the slot's material is picked up automatically, `Tn` is generated, and a material conflict fails immediately.
- `send` checks materials slot by slot against the `Tn` actually used in the G-code; with no `slots` it warns but still checks the `toolheads` limit.

### 9.4 Sending a print

```text
printer_status("u1")                       # confirm idle / model first
send(slice_id="s_…", printer_id="u1", confirm="PRINT u1 s_…")
send(..., start=false)                     # upload only, do not start
printer_cancel("u1", confirm="CANCEL PRINT u1")
```

`send`'s preconditions: slicing succeeded, the G-code exists, the printer is idle, the slicing target's `printer_model` matches the printer's `model`, and the filament slots match. If any one fails it refuses outright and does not retry automatically.

`start=true` by default, so printing starts right after upload. A real print is an irreversible physical action, so the first time:

1. `send(..., start=false)` to upload only;
2. confirm the filename and preview on the printer screen;
3. then start from the screen, or call `send(..., start=true)` again.

### 9.5 Automatic flow calibration

`send(..., flow_calibrate=True)` runs the printer configuration's `flow_calibration.enable_gcode` before starting. For a Snapmaker U1, for example, it is configured as:

```text
SET_PRINT_PREFERENCES FLOW_CALIBRATE=1
```

The printer then runs `SM_PRINT_FLOW_CALIBRATE` inside `PRINT_START`. Generic Klipper can substitute its own macro. The command can only be set while idle; the firmware rejects it during a print. If it is not configured, or the adapter does not support it, you get an explicit error rather than a silent skip.

With multiple colours it de-duplicates by **physical toolhead**: multi-toolhead machines calibrate each head they use, while single-nozzle multi-colour calibrates once. `trim_unused_tools` + `tool_macros` can strip preheat/prime/calibration lines for unused toolheads from the start G-code, but only macro names listed in the configuration are matched.

---

## 10. Configuration and secrets

### 10.1 Precedence

```text
CLI flags > environment variables > <workspace>/config.json > defaults
```

Common environment variables:

| Variable | Purpose |
| --- | --- |
| `PRINT3D_WORKSPACE` | Workspace path, defaults to `~/.print3d` |
| `PRINT3D_CONFIG` | Explicit `config.json` |
| `PRINT3D_TOKEN` | HTTP Bearer token |
| `TRIPO_API_KEY` / `HUNYUAN3D_API_KEY` / `OPENAI_API_KEY` | provider keys |

Provider key resolution order: the variable named by `api_key_env` → `PRINT3D_<ID>_API_KEY` → `<ID>_API_KEY` → `secrets.json`.

### 10.2 config.json structure

```json
{
  "server": {"host": "127.0.0.1", "port": 8765, "token_env": "PRINT3D_TOKEN"},
  "providers": {
    "cloud": {"tripo": {"adapter": "tripo", "api_key_env": "TRIPO_API_KEY"}},
    "local": {}
  },
  "images": {"openai": {"adapter": "openai", "api_key_env": "OPENAI_API_KEY"}},
  "cad": {"backend": "text2cad", "python": null, "timeout_s": 120},
  "render": {"backend": "blender_cli", "blender_path": null, "timeout_s": 300},
  "targets": {},
  "printers": []
}
```

Secrets are **not written into config.json**. `print3d config` reports only `has_key`, and logs and tool output are redacted.

### 10.3 secrets.json

```json
{
  "providers": {
    "tripo": "tripo_api_key_here",
    "hunyuan": "hunyuan_api_key_here",
    "openai": "openai_api_key_here"
  },
  "server": {"token": "optional_http_bearer_token"}
}
```

```bash
chmod 600 ~/.print3d/secrets.json
```

Overly broad permissions trigger a `secrets_insecure` warning. Keeping keys in environment variables is preferable, especially on shared machines and in CI.

### 10.4 Multiple instances

Use different `--workspace` / `PRINT3D_WORKSPACE` values to isolate assets, jobs and configuration. Do not share one workspace between two instances: job queues, slice directories and `config.json` will all trip over each other.

---

## 11. Security boundaries

1. **Read-only tools are free**: `capabilities`, `system_status`, `job_status`, `asset_list`, `model_show`, `printability_check`, `orientation_plan` need no confirmation and return `writes_performed: false`; `model_preview` only writes the preview cache.
2. **Writes require exact confirmation**: confirmation strings are compared character by character; a mismatch returns `confirmation_mismatch` and writes no files. Do not assemble `confirm` from memory — copy it from `slice_plan` or a tool result.
3. **Path sandbox**: except for `reference_import` reading an image you specify, every user-supplied path is resolved (symlinks included) and validated; an escape returns `path_outside_workspace`. `asset_export`'s `out_path` cannot write outside the workspace either.
4. **Script policy + subprocess isolation**: build123d scripts pass an AST allowlist first, then run in a separate process group with CPU/file/handle limits, a timeout SIGKILL and a clean environment. bpy scripts are reviewed and then run the same way. Static review is defence in depth, not a multi-tenant sandbox. On Windows the child runs in a Job Object and a timeout kills the whole tree, but there are no CPU/file-size/handle limits — `capabilities.sandbox` is the authoritative answer.
5. **Bounded downloads**: cloud model downloads accept only http(s), limit the number of redirects and count bytes as they stream; concept image URLs have size limits too. HTTP mode compares Bearer tokens in constant time.
6. **No arbitrary G-code**: `send` accepts only slicer output, checks before uploading, and does not retry automatically.
7. **Secrets never reach the logs**: read only from environment variables or `secrets.json`, with uniform redaction on output.
8. **Local by default**: apart from explicitly calling cloud generation, modelling, rendering and slicing all happen on your machine; sending goes only to your printer.

**It is not a service for untrusted users.** Do not expose HTTP mode to the public internet; on shared machines keep workspace and secrets permissions tight; cloud generation sends the prompt/image to a third party, so use the local path for sensitive content.

The `sandbox` field of `print3d capabilities` is exactly what this machine enforces (`wall_clock_timeout`, `tree_kill`, `kill_method`, `cpu_time_limit`, `file_size_limit`, `open_file_limit`). The last three are `false` on Windows — that is a fact about the platform, not a setting; if you need strict quotas, run model builds on macOS/Linux.

---

## 12. Troubleshooting

Every failed tool returns one envelope:

```json
{"ok": false, "error": {"code": "…", "message": "…", "hint": "…", "details": {}}}
```

**Read `error.hint` first** — it is usually the next step. Common error codes:

| code | Meaning | What to do |
| --- | --- | --- |
| `config_missing` | `config.json` not found | `print3d init`, or check `PRINT3D_WORKSPACE` / `--workspace` |
| `config_invalid` | Configuration JSON / field is invalid | `print3d config` to see the parse result, then fix the field per the hint |
| `secrets_insecure` | `secrets.json` permissions are too broad | `chmod 600 <workspace>/secrets.json` |
| `path_invalid` | Path has the wrong form | Use an absolute path or a path relative to the workspace |
| `path_outside_workspace` | Path escapes the workspace | Put the file inside the workspace, or use `reference_import` |
| `not_found` | Asset / revision / file does not exist | `asset_list` / `model_show` first, to confirm the id and revision |
| `invalid_argument` | Argument missing or conflicting | Check `details`; a common case is `size_mm` and `max_dimension_mm` given together |
| `confirmation_mismatch` | Confirmation string does not match | Copy the exact string from `slice_plan` or a result; mind spaces and case |
| `adapter_unknown` / `adapter_unavailable` | Adapter not registered / environment unavailable | `capabilities` to see what is available; check the Blender and Orca paths |
| `job_not_found` | Job id does not exist | `print3d job list` to see recent ids |
| `job_invalid_state` | Job state does not allow the operation | Wait for the job to finish, or submit it again |
| `internal_error` | Unexpected exception | Check the message and the host's MCP logs; reproduce geometry problems with `print3d cad run` |

### 12.1 Symptom → action

| Symptom | Check first |
| --- | --- |
| The host cannot see the tools | Whether the command is an absolute path, whether `args` contains `serve --stdio`, whether the host was restarted; run `print3d tools` by hand first |
| CAD unavailable in `capabilities` | Whether `.venv` has build123d installed; the interpreter reported by `print3d --version` and `capabilities.cad` |
| Script static review fails | Whether the first line is `from build123d import *`; whether a `_` attribute, `eval`/`open`/`getattr` is used; whether a module outside the allowlist is imported |
| Build fails with no visible reason | The build result has `stdout` / `stderr` / `exit_code`; reproduce in `/tmp` with `print3d cad run` first |
| `watertight: false` / `boundary_edges > 0` | An open shell or unmerged solids; check booleans, `fillet` and imported meshes |
| `solids > 1` | Several non-touching solids; merge them with `union`, or split them into several assets |
| The model is not on the bed | Whether the bbox exceeds the target bed; whether the part's bottom face is at `z=0` |
| Cloud provider not ready | Whether the key is in an environment variable / `secrets.json`; the source reported by `capabilities.providers.cloud` |
| Cloud generation comes out the wrong size | Check `scale`; use `size_mm` or `max_dimension_mm` for an automatic fit |
| Local generation job fails | Whether the command is executable, whether the placeholders are right, whether `<outdir>/mesh.stl` was written; check the job's stderr |
| Slicing target not ready | Whether the preset paths exist, whether the slicer CLI is executable; the details from `slice_capabilities` |
| Slicing times out | Raise `targets.<id>.timeout_s`; for complex models try a test slice with a coarser layer height / less support first |
| The slicer crashes | Switch to upstream OrcaSlicer; vendor-customised builds may not support the headless CLI; flatten the profile |
| The printer is unreachable | `printer_status` to see the error; whether `base_url` is right, whether the machine is on the same subnet, firewall |
| `send` refuses: not idle | The printer is printing / paused / in error; clear the state on the screen first |
| `send` refuses: model mismatch | The printer's `model` and the target's `printer_model` must match |
| `send` refuses: material mismatch | Check the `slots` materials; pass `printer` + `slot` when slicing, or fix the slot configuration |
| HTTP 401 | `Authorization: Bearer`; whether the token matches `PRINT3D_TOKEN` / `secrets.server.token` |
| The viewer does not update | Regenerate it with `model_view(asset_id, confirm="VIEW <asset_id>")`; make sure you opened the asset-level `viewer.html` |
| A script reports a strange `ModuleNotFoundError` | Run it from a clean directory; the script's own directory goes on `sys.path` and can shadow standard library modules |

### 12.2 Logs and evidence

- `<workspace>/jobs/<job_id>/job.json`: job state, progress, result, errors;
- `<workspace>/slices/<slice_id>/`: `plate_1.gcode`, `slice.json`, optionally `statistics.json`;
- `<workspace>/assets/<asset_id>/vNNN/`: `model.py`, `model.stl`, `model.step`, `params.json`, reference;
- `<workspace>/logs/`: service logs (if configured).

When troubleshooting, keep the job and asset directories around; do not delete them in a hurry — they are reproducible evidence.

---

## 13. CLI quick reference

| Command | Purpose |
| --- | --- |
| `print3d init [--workspace DIR] [--force]` | Create the workspace and default configuration |
| `print3d capabilities` | Capability report (JSON) |
| `print3d status` | System status (JSON) |
| `print3d config` | Effective configuration (secrets redacted) |
| `print3d tools` | Registered MCP tools (JSON) |
| `print3d serve [--stdio \| --http] [--host H] [--port P]` | Start the MCP server |
| `print3d job list` / `print3d job show <job_id>` | Inspect jobs |
| `print3d cad run <script.py> [--workdir DIR] [--timeout S] [--python PATH]` | Run a build123d script locally |
| `print3d image <prompt> --name NAME [--size 1024x1024] [--out PATH]` | Generate a concept image |
| `print3d preview <asset_id> [--revision v001] [--views iso,front] [--out PATH]` | Render the four views |
| `print3d view <asset_id> [--revision v001] [--open]` | Generate / open the interactive viewer |
| `print3d version` | Version |

Every subcommand accepts `--workspace` and `--config`; put them after the subcommand, for example:

```bash
print3d preview a_20261114T000000Z_abcdef12 --revision v001 --workspace /tmp/ws
```

---

## 14. Common recipes

### 14.1 Change one hole in an existing STEP

```text
asset_id = model_write(script_with_INPUT_STEP, name="bracket-v2", confirm="WRITE bracket-v2")
→ model_build(asset_id, input="/path/bracket.step", confirm="BUILD <asset_id>")
→ model_preview(asset_id)
→ asset_export(asset_id, artifact="step", confirm="EXPORT <asset_id>")
```

In the script, read the source file with `import_step(INPUT_STEP)` and export only the new revision; the original STEP is untouched.

### 14.2 Image-to-3D where dimensions must be exact

```text
generate_cloud(provider="tripo", image="/path/ref.png", name="shell",
               size_mm=[116, 101, 44], confirm="CLOUD shell")
→ job_status(job_id)
→ printability_check(asset_id, target="u1")
```

`size_mm` runs the same mesh fit automatically after download, landing directly in three-axis millimetres. Without three-axis data, scale proportionally with `max_dimension_mm`, then fine-tune with `mesh_fit`.

### 14.3 Reference modelling from an image

```text
reference_import(path="/path/sketch.jpg", name="lamp-base", confirm="IMPORT lamp-base")
→ returns image_path (a workspace-relative path)
→ model_write(script, reference="<image_path>", confirm="WRITE lamp-base")
→ model_build → model_preview
```

The host model looks at the image; you supply the dimensions and the back-side structure in the prompt.

### 14.4 Dry run first, then send

```text
printability_check(asset_id, target="u1")
→ orientation_plan(asset_id, target="u1")
→ slice_plan(asset_id, "u1", material="PLA")
→ slice_run(..., confirm="SLICE <asset_id> u1")
→ slice_inspect(slice_id)
→ printer_status("u1")
→ send(slice_id, printer_id="u1", confirm="PRINT u1 <slice_id>", start=false)   # upload only
→ send(slice_id, printer_id="u1", confirm="PRINT u1 <slice_id>", start=true)    # start after confirmation
```

### 14.5 Slice only, no printing

```text
slice_run(...)
→ slice_inspect(slice_id)       # get slices/<slice_id>/plate_1.gcode
```

The G-code stays in the workspace; you can copy it out yourself or have the slicer review it. Print3D does not offer arbitrary G-code upload — it sends only output it sliced itself.

---

## 15. Upgrade, backup and uninstall

### 15.1 Upgrade

```bash
cd Print3D
git pull
uv sync --extra dev
uv run python -m pytest -q
```

Restart the host so that it relaunches `print3d serve --stdio`. If you updated Print3D itself, run `uv sync` again, then restart.

### 15.2 Backup

What needs backing up:

- `<workspace>/assets/`: models, scripts, STEP, references, viewers;
- `<workspace>/config.json`: targets / printers configuration;
- `<workspace>/secrets.json`: keys (store them encrypted separately; never commit them to git);
- `<workspace>/profiles/`: custom slicing presets.

`cache/` and `jobs/` can be rebuilt or discarded; keep the G-code under `slices/` as needed.

### 15.3 Uninstall

```bash
# source install: delete the repo and virtualenv
rm -rf /path/to/Print3D

# workspace (delete only after confirming you no longer need the assets)
rm -rf ~/.print3d
```

If a key has ever been in a shared environment or a log, **rotate** it in the provider console; deleting the file is not remediation.

---

## 16. More documentation

- [README](../README.en.md) — project overview, features, quick start
- [Manual (中文)](MANUAL.md) — the Chinese version of this manual
