# XPrint

**XPrint 是一个自托管的 3D 打印 MCP 服务。** 它把「文字/图片 → 参数化建模脚本 → 构建 → 渲染 → 切片 → 发送到打印机」做成一组可组合的 MCP 工具，接入 DSH、Codex、Claude Desktop、Cursor 等任意 MCP 宿主。

> 状态：**M0 已可运行**（2026-11-13）。已有 MCP stdio/HTTP 服务、配置与密钥管理、路径沙箱、任务状态机、Kimi 建模器、资产版本库，以及 5 个工具。切片与打印适配器在 M2 之后接入。

## 文档

- [设计文档](docs/DESIGN.md) —— 架构、模块、工具面、数据模型、安全边界
- [开发计划](docs/PLAN.md) —— 里程碑、验收标准、风险

## 快速开始

需要 Python ≥ 3.11（本机验证于 3.14）与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone <this-repo> XPrint && cd XPrint

# 安装（含开发依赖）
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 初始化工作区 ~/.xprint（生成 config.json 目录骨架，不写密钥）
.venv/bin/xprint init

# 看看这台机器能做什么：provider 有没有 key、CAD/渲染/切片是否可用
.venv/bin/xprint capabilities

# 以 MCP stdio 方式启动（宿主拉起的标准姿势）
.venv/bin/xprint serve --stdio

# 或者本地 HTTP（默认 127.0.0.1:8765；非回环地址强制要求 Bearer token）
.venv/bin/xprint serve --http --port 8765
```

工作区默认 `~/.xprint`，可用 `--workspace` 或环境变量 `XPRINT_WORKSPACE` 覆盖（测试/多实例时建议覆盖）。`xprint config` 打印生效配置，密钥永远不出现。

把 XPrint 接到 DSH：在 **Plugins → Add plugin** 里选择本仓库的 `dsh-xprint-mcp/` 目录（或让 agent 调 `plugin_manager` 的 `install_bundle`）。细节见 [dsh-xprint-mcp/README.md](dsh-xprint-mcp/README.md)。

之后在会话里直接调用 `mcp__xprint__system_status`、`mcp__xprint__capabilities` 等工具。

## Kimi 建模

Kimi（Moonshot）的 API 是**文本/图片 → 文本**，没有 3D 网格生成接口。它的「建模」能力来自直接写 CAD 脚本。因此 XPrint 把 Kimi 接成 **modeler**：Kimi 生成 build123d 参数化脚本，XPrint 做静态安全审查、存成版本化资产，再由 CAD 后端构建。这样留下的是可编辑、可改参数、可复现的源码，而不是一坨网格。

```bash
export MOONSHOT_API_KEY=sk-...     # 也可以写进 ~/.xprint/secrets.json（0600）
.venv/bin/xprint capabilities      # modelers[].ready 应为 true
```

MCP 调用 `model_generate`：

```json
{
  "prompt": "一个 80×40×12 mm 的桌面线缆夹，夹口 6 mm，底部带 3M 胶槽",
  "name": "cable-clip",
  "modeler": "kimi",
  "confirm": "MODEL cable-clip kimi"
}
```

返回资产 `a_...`、版本 `v001`，以及：

- `script_path`：`assets/a_.../v001/model.py`（相对工作区）
- `params.json`：提示词、模型名、token 用量、审查结果
- `checks`：静态策略结果与告警

脚本**不会**在 `model_generate` 里执行。审查不过（比如出现 `subprocess`）会返回 `invalid_argument` 且不落盘。

也可以直接当 Python 库用：

```python
from xprint.modelers import KimiModeler

gen = KimiModeler(api_key="sk-...", reasoning_effort="high").generate(
    "a 20 mm calibration cube", image=None
)
print(gen.code)          # ```python 代码块已提取
print(gen.usage)         # {"prompt_tokens": ..., "total_tokens": ...}
```

配置项（`~/.xprint/config.json`）：

```json
{
  "modelers": {
    "kimi": {
      "enabled": true,
      "base_url": "https://api.moonshot.cn/v1",
      "model": "kimi-k3.1-pro",
      "reasoning_effort": "high",
      "timeout_s": 180,
      "api_key_env": "MOONSHOT_API_KEY"
    }
  }
}
```

可选模型：`kimi-k3.1-pro`（默认）、`kimi-k3.1-pro-highspeed`、`kimi-k3`。`reasoning_effort` 取 `low|high|max`。

## 工具（M0）

| 工具 | 写操作 | 说明 |
| --- | --- | --- |
| `capabilities` | 否 | provider / modeler / CAD / 渲染 / 切片 / 打印机能力，永不返回密钥 |
| `system_status` | 否 | 版本、Python、工作区、任务计数；固定 `writes_performed: false` |
| `job_status` | 否 | 按 `job_id` 查询任务；未知 id 返回 `job_not_found` |
| `asset_list` | 否 | 列出资产与版本数 |
| `model_generate` | 是 | 调 LLM 生成 build123d 脚本并存版本；`confirm` 必须精确为 `MODEL <name> <modeler>` |

错误统一为 `{"ok": false, "error": {"code", "message", "hint", "details"}}`。`hint` 是给人和 agent 的下一步提示，`details` 里可能有被拒绝的行号。

## 安全边界

1. **只读工具免费**：`capabilities`、`system_status`、`job_status`、`asset_list` 不需要确认，且明确返回 `writes_performed: false`。
2. **写操作必须精确确认**：确认串逐字符比较，错了返回 `confirmation_mismatch` 且不写任何文件。
3. **路径沙箱**：所有用户路径经 `safe_path()` 解析符号链接后校验，逃出工作区一律 `path_outside_workspace`。
4. **脚本策略**：LLM 产出的脚本先过 AST 审查（禁 `os/pathlib/subprocess/socket/shutil/ctypes/requests/urllib/http/multiprocessing`、禁 `eval/exec/open`…），再谈构建。这是纵深防御，不是沙箱——执行仍在外部进程。
5. **不开放任意 G-code**：切片产物先检查再发送，`printer_*` 只接受切片器产物。
6. **密钥**：只从环境变量或 `~/.xprint/secrets.json`（0600）读取；日志与工具输出统一脱敏。

## 开发

```bash
.venv/bin/python -m pytest -q          # 单元 + 内存 MCP + respx 模拟 HTTP
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
```

测试覆盖：配置优先级与 BOM、路径逃逸与符号链接、任务状态机非法迁移、脚本策略、资产版本 v001→v002、Kimi 客户端（respx 模拟，含 401/429/超时/图片）、MCP stdio 握手与 HTTP Bearer 鉴权。

## 目录

```
XPrint/
  pyproject.toml
  src/xprint/
    server.py          # MCP 入口（stdio / streamable HTTP）+ 工具注册
    cli.py             # xprint init|capabilities|status|config|tools|serve|job
    config.py          # config.json / secrets / 环境变量优先级
    workspace.py       # ~/.xprint 布局 + safe_path 沙箱
    jobs.py            # 任务状态机与 job.json
    assets.py          # 资产 manifest + vNNN 版本
    registry.py        # 适配器注册表
    capabilities.py    # 能力探测（只读）
    modelers.py        # LLM 建模器客户端（Kimi）
    modelers_policy.py # 提示词契约
    script_policy.py   # build123d 脚本静态审查
    errors.py          # 统一错误码
  dsh-xprint-mcp/      # DSH 配置文件型 bundle
  tests/
  docs/
```

## 路线图

- **M0（完成）**：脚手架、配置/密钥、路径沙箱、任务状态机、MCP stdio+HTTP、5 个工具、Kimi 建模器、DSH bundle。
- **M1**：CAD 适配器（text-to-cad / build123d）与 `model_build`，预览渲染。
- **M2**：云端图生 3D（Hunyuan/Tripo）异步任务、导入导出。
- **M3**：切片器适配（Orca/Bambu 外部进程，profile 运行时读取）与产物审计。
- **M4**：Moonraker / PrusaLink 打印机适配，`send` + 审批流。

详细验收标准见 [docs/PLAN.md](docs/PLAN.md)。
