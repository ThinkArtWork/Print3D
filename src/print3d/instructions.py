"""The MCP `instructions` string shown to clients at connect time."""

INSTRUCTIONS = """\
3DPrint is a self-hosted bridge between MCP agents and the 3D-printing tools on
this machine.

Safety contract:
- Read-only tools (capabilities, system_status, job_status, model_show,
  asset_list) run without confirmation.
- Every tool that writes files or talks to a paid service requires the exact
  `confirm` string documented in its schema.  A wrong string is rejected with
  code `confirmation_mismatch`, nothing is written.
- 3DPrint never sends arbitrary G-code to a printer; slicing output is inspected
  and confirmed first.

Start with `capabilities` to learn which providers, CAD backends, image
providers, slicers and printers are available on this host.

Two modelling paths, both stored as versioned assets:

1. Text -> concept image -> model:
   `image_generate` turns the idea into a concept image and stores it as a
   reference.  Write a build123d script informed by that image, call
   `model_write` with `reference=<image_path>`, then `model_build` to execute it
   in a sandbox and read real geometry checks.  `model_preview` renders the
   built STL back to you as an image (next to the concept image), so you can
   compare intent with the result and revise.

2. Text -> model (no image step):
   write the script directly, call `model_write`, `model_build`, then
   `model_preview` to look at the result.  Repeat until the part is watertight
   and matches the request.

`model_show` returns a stored revision's script and provenance.  Built artifacts
(model.stl, model.step) live in the workspace; previews are returned inline as
PNG content.

Every successful build also writes a self-contained interactive viewer
(`viewer.html`) next to `model.stl` and returns `viewer_file` — `model_build`,
`blender_run`, and the `generate_local` / `generate_cloud` jobs all do this.
As soon as a model exists, call `present` with that `viewer_file` so the user
gets a one-click 3D preview in the conversation; do not wait to be asked.
`model_preview` also echoes `viewer_file` when it is already there, and
`model_view` can regenerate it for older revisions.

Two extra engines reuse the same asset + preview loop:

- `blender_run` executes a bpy scene script with Blender in background mode,
  exports model.stl/model.glb and returns the same geometry checks.  Use it for
  text-to-3D when Blender's modelling tools fit better than build123d; for
  image-to-3D, look at the attached image yourself and write the scene.
- `generate_local` runs a user-installed local generator (configured under
  providers.local.<id>) as a background job; poll `job_status`, then preview the
  produced asset.  3DPrint downloads no weights and calls no cloud API.

Both accept `image=` for image-driven work; assets remember the reference.

Once the model is accepted, slice it for a configured target: `slice_plan` is a
read-only dry run that prints the exact confirm string; `slice_run` produces
G-code asynchronously (poll `job_status`, then `slice_inspect`).  To print,
`send` uploads the G-code to a configured printer and starts it by default;
`printer_status` and `printer_cancel` cover the live state, while
`printer_list`/`printer_save`/`printer_remove` manage the printer list.
"""
