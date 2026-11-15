# dsh-print3d-mcp

[中文](README.md) · [English](README.en.md)

把本机 Print3D MCP 服务挂到 [DeepSeek Harness](https://github.com/deepseek-ai) 上的配置文件型 bundle。只含一个 patch，不含任何运行代码。

## 安装

```bash
# 1) 先让 `print3d` 在 PATH 上（任选一种）
pipx install "git+https://github.com/ThinkArtWork/Print3D.git"   # 发布后；或
uvx --from git+https://github.com/ThinkArtWork/Print3D.git print3d capabilities
cd /path/to/Print3D && .venv/bin/pip install -e .        # 从源码

# 2) 安装 bundle：DSH 的 Plugins → Add plugin 选择本目录
#    或由 agent 调用 plugin_manager 的 action=install_bundle, target=<绝对路径>
```

若用虚拟环境且不想改 PATH，把 `cordis.patch.yml` 里的 `command: print3d` 换成
`/path/to/Print3D/.venv/bin/print3d` 绝对路径（DSH 不展开 `~`；也不建议用相对路径，
因为 DSH 启动进程时的工作目录不保证是仓库根目录）。

安装后在任意会话里应能看到并调用：

- `mcp__print3d__system_status` —— 返回 `writes_performed: false`
- `mcp__print3d__capabilities` —— 报告可用 provider / modeler / slicer
- `mcp__print3d__model_write` / `mcp__print3d__model_build` —— 写入并沙箱构建 build123d 脚本
- `mcp__print3d__model_preview` —— 四视图 PNG

## 说明

- `serverName: print3d` 决定工具前缀 `mcp__print3d__*`；
- `failOnStartupError: true`：Print3D 起不来时直接在插件列表里报错，而不是静默消失；
- 工作区默认 `~/.print3d`；需要别的位置时在 `config` 下加 `env: { PRINT3D_WORKSPACE: /abs/path }`；
- 该 bundle 只连本机 stdio 进程，不监听端口，不需要 token。
