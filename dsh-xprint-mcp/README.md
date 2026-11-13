# dsh-xprint-mcp

把本机 XPrint MCP 服务挂到 [DeepSeek Harness](https://github.com/deepseek-ai) 上的配置文件型 bundle。只含一个 patch，不含任何运行代码。

## 安装

```bash
# 1) 先让 `xprint` 在 PATH 上（任选一种）
pipx install /path/to/XPrint          # 或
cd /path/to/XPrint && .venv/bin/pip install -e .

# 2) 安装 bundle：DSH 的 Plugins → Add plugin 选择本目录
#    或由 agent 调用 plugin_manager 的 action=install_bundle, target=<绝对路径>
```

若用虚拟环境且不想改 PATH，把 `cordis.patch.yml` 里的 `command: xprint` 换成
`/path/to/XPrint/.venv/bin/xprint` 绝对路径（DSH 不展开 `~`）。

安装后在任意会话里应能看到并调用：

- `mcp__xprint__system_status` —— 返回 `writes_performed: false`
- `mcp__xprint__capabilities` —— 报告可用 provider / modeler / slicer
- `mcp__xprint__model_generate` —— 让 Kimi 生成 build123d 脚本（需要 `MOONSHOT_API_KEY`）

## 说明

- `serverName: xprint` 决定工具前缀 `mcp__xprint__*`；
- `failOnStartupError: true`：XPrint 起不来时直接在插件列表里报错，而不是静默消失；
- 工作区默认 `~/.xprint`；需要别的位置时在 `config` 下加 `env: { XPRINT_WORKSPACE: /abs/path }`；
- 该 bundle 只连本机 stdio 进程，不监听端口，不需要 token。
