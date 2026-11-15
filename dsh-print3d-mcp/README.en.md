# dsh-print3d-mcp

[中文](README.md) · [English](README.en.md)

A configuration-only bundle that connects the local Print3D MCP server to [DeepSeek Harness](https://github.com/deepseek-ai). It contains one patch and no runtime code.

## Install

```bash
# 1) Put `print3d` on PATH first (pick one)
pipx install "git+https://github.com/ThinkArtWork/Print3D.git"   # after release; or
uvx --from git+https://github.com/ThinkArtWork/Print3D.git print3d capabilities
cd /path/to/Print3D && .venv/bin/pip install -e .        # from source

# 2) Install the bundle: DSH → Plugins → Add plugin, select this directory
#    or have an agent call plugin_manager with action=install_bundle, target=<absolute path>
```

If you use a virtualenv and do not want to change PATH, replace `command: print3d` in
`cordis.patch.yml` with the absolute path `/path/to/Print3D/.venv/bin/print3d` (DSH does not
expand `~`; a relative path is not recommended because DSH's working directory is not guaranteed
to be the repository root).

After installation, any session should expose and be able to call:

- `mcp__print3d__system_status` — returns `writes_performed: false`
- `mcp__print3d__capabilities` — reports the available provider / modeler / slicer
- `mcp__print3d__model_write` / `mcp__print3d__model_build` — write and sandbox-build a build123d script
- `mcp__print3d__model_preview` — four-view PNG

## Notes

- `serverName: print3d` sets the tool prefix `mcp__print3d__*`;
- `failOnStartupError: true` — if Print3D cannot start, the plugin list shows the error instead of failing silently;
- the default workspace is `~/.print3d`; to use another location, add `env: { PRINT3D_WORKSPACE: /abs/path }` under `config`;
- the bundle only talks to a local stdio process: no port, no token.
