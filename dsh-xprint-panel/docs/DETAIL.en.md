# XPrint panel

The **XPrint** icon in the sidebar opens a read-only view of the modelling
workspace. It does not replace the `mcp__xprint__*` tools in chat; it visualises
the same workspace data:

- **Model list**: assets from `asset_list`, newest first, with revision counts.
- **Latest render**: selecting a model calls `model_preview` and shows the
  rendered `model.stl`; when the revision carries a concept image, the concept
  is shown first, then the render.
- **Status**: version, workspace path and job counts from `system_status`.

## Data flow

```
DSH host process
  └─ Host half of this plugin: spawn `xprint serve --http --host 127.0.0.1 --port 8765`
        └─ XPrint MCP over HTTP (same workspace as the stdio server used in chat)
              └─ Browser half: fetch /mcp, initialize → tools/call
```

The browser half only uses MCP `initialize`, `notifications/initialized` and
`tools/call`. It never reads the filesystem directly and never stores printer
credentials.

## Read-only boundary

The panel never writes assets, starts a slice or a print. Every write still has
to come from the model in chat, with the exact `confirm` string. That is
XPrint's safety boundary: the UI only looks.

## When it fails

If port 8765 is already taken (for example by a manually started
`xprint serve --http`), the plugin does not start a second server and the panel
connects to the existing one. If the binary moved, the panel shows an offline
card with a copyable start command; the `command` in `cordis.patch.yml`
overrides it.
