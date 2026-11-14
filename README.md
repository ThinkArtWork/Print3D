# XPrint

**XPrint 是一个自托管的 3D 打印 MCP 服务。** 它把「文字/图片 → 参数化建模脚本 → 构建 → 渲染 → 切片 → 发送到打印机」做成一组可组合的 MCP 工具，接入 DSH、Codex、Claude Desktop、Cursor 等任意 MCP 宿主。

> 状态：**M0 + 建模闭环已可运行**（2026-11-14）。已有 MCP stdio/HTTP 服务、配置与密钥管理、路径沙箱、任务状态机、资产版本库、沙箱化 CAD 执行器、概念图生成、四视图预览渲染，以及 **14 个工具**（含 Blender bpy 与本地生成器两条额外路径）。切片与打印适配器在 M2 之后接入。

## 文档

- [设计文档](docs/DESIGN.md) —— 架构、模块、工具面、数据模型、安全边界
- [开发计划](docs/PLAN.md) —— 里程碑、验收标准、风险

## 快速开始

需要 Python ≥ 3.11（本机验证于 3.14）与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone <this-repo> XPrint && cd XPrint

# 安装（含开发依赖；build123d 是核心依赖）
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

把 XPrint 接到 DSH 有两种方式，推荐第一种（不需要安装任何本地插件包）：

**方式 A：官方 `mcp-manager`（推荐）**。DSH 自带 `@deepseek-ai/dsh-mcp-manager`，在 `Settings → MCP` 里加一条 stdio 服务器即可（命令必须是**绝对路径**，DSH 不会展开 `~`）:

```json
{
  "xprint": {
    "transport": "stdio",
    "command": "/absolute/path/to/XPrint/.venv/bin/xprint",
    "args": ["serve", "--stdio"],
    "env": { "XPRINT_WORKSPACE": "/absolute/path/to/XPrint/.xprint" },
    "enabled": true
  }
}
```

如果本机 DSH 设置里没有 MCP 页（0.2.2-rc.3 的已知情况），直接写 profile 的 `cordis.patch.yml`：`id` 必须是 **`mcp-manager`**（不是 README 里旧的 `mcp`），profile 里是**整表替换**继承来的 servers，改完重启 DSH。

**方式 B：自定义 bundle**。在 **Plugins → Add plugin** 里选择本仓库的 `dsh-xprint-mcp/` 目录（或让 agent 调 `plugin_manager` 的 `install_bundle`），细节见 [dsh-xprint-mcp/README.md](dsh-xprint-mcp/README.md)。

之后在会话里直接调用 `mcp__xprint__system_status`、`mcp__xprint__capabilities` 等工具。

## 两条建模路径

XPrint **不自己接任何 LLM**。它把建模闭环的原语暴露成 MCP 工具，由**你正在用的宿主模型**驱动；每条路径都以真实几何检查收尾，而不是凭文字想象。

### 路径一：文字 → 模型（直接写脚本）

```text
你 → 宿主模型写 build123d 脚本
   → model_write(script, confirm="WRITE <name>")     # 静态审查，不执行
   → model_build(asset_id, confirm="BUILD <asset_id>")# 沙箱执行，返回真实几何
   → model_preview(asset_id)                          # 四视图 PNG 回给模型
   → 改脚本 → model_write 追加 v002 → 再 build → 再 preview
```

`model_build` 返回 `watertight`、`boundary_edges`、`nonmanifold_edges`、`bbox_mm`、`volume_mm3`、`solids`、`is_valid`，并保存 `model.stl` / `model.step`。`model_preview` 把 STL 渲染成 ISO/FRONT/TOP/RIGHT 四视图，**以图片内容返回**，所以宿主模型能真的「看见」比例、孔位和缺失特征。

### 路径二：文字/图片 → 概念图 → 模型（视觉参考）

```text
image_generate(prompt, confirm="IMAGE <name>")        # OpenAI 兼容 images API
reference_import(path, confirm="IMPORT <name>")       # 或导入用户上传的草图/照片
   → model_write(script, reference=<image_path>, confirm="WRITE <name>")
   → model_build → model_preview                       # 预览会同时回概念图和渲染图
```

概念图（reference）会复制到该版本的 `reference.<ext>`，`params.json` 记录来源、提示词与 sha256；`model_preview` 把**概念图和渲染图并排返回**，模型据此对比「要的」和「做出来的」再改。

### 多轮对话

同一资产的每次修改都是一次 `model_write`（带 `asset_id` 追加 `v002`、`v003`…），`prompt` 记录这一轮的要求；`model_show` 读回任意版本的脚本、参数和构建记录。这样「这一轮改哪里、哪些保留」有据可查，也方便回滚。

### 图片理解

XPrint 不内置视觉模型：上传的草图/照片由宿主模型（多模态）自己看，XPrint 负责把图片存成 reference、附到版本上、并在预览时回传。参考图看不到的背面、内部结构和精确尺寸，需要宿主模型在 `prompt` 里补充或用文字提问。

### 路径三：Blender（bpy 场景脚本）

适合有机形状、倒角/布尔/阵列和 Blender 自带的建模工具；不需要安装 build123d。

```text
宿主模型写 bpy 脚本（primitive_* / bmesh / 修改器）
   → blender_run(script, confirm="BLENDER <name>")   # 先过 bpy 静态审查
   → Blender 后台模式执行：-b --factory-startup --python-exit-code 1
   → 导出所有 MESH 对象为 model.stl + model.glb
   → 同样的 geometry checks（watertight/bbox/volume/边计数）
   → model_preview(asset_id)；改成新版本则传 asset_id
```

- 版本可追加：`blender_run(asset_id=<id>, confirm="BLENDER <asset_id>")` 存为 `v002`、`v003`…；资产 `kind` 为 `blender`，不能与 `model_build` 混用。
- 图片输入：`blender_run(image=<path>)` 把参考图复制到该版本；脚本可在 `sys.argv` 的 `--` 之后拿到同一路径，宿主模型也可以直接看 MCP 返回的图片内容。
- 实测（Blender 5.3.0，M1）：20 mm 立方体 + 直径 6 mm 通孔，整个 Blender 进程 0.9–1.1 s，四视图预览正常。
- 导出脚本只认 `MESH` 对象，**布尔切割用的辅助对象记得删除**，否则会一起导出（预览/bbox 会暴露这种残留）。

### 路径四：本地生成器（通用命令适配器）

不下载模型权重，也不绑定某个框架：任何能读提示词/图片、写出 STL 的本地命令都能接进来。

```json
// ~/.xprint/config.json —— providers.local.<id>
{
  "providers": {
    "local": {
      "triposr": {
        "adapter": "triposr",
        "mode": "image",                       // text | image | both
        "command": ["/opt/triposr/.venv/bin/python", "/opt/triposr/run.py"],
        "args": ["--image", "{image}", "--out", "{outdir}", "--name", "{name}"],
        "timeout_s": 600
      }
    }
  }
}
```

```text
generate_local(provider="triposr", prompt=..., image=..., confirm="LOCAL <name>")
   → 后台任务（job_id）
   → 命令在 clean env 中运行，cwd=<outdir>，可读 XPRINT_LOCAL_OUT
   → 必须写出 <outdir>/mesh.stl（或 stdout 打印 {"mesh": "/path/model.stl"}）
   → XPrint 建资产（kind=local）、算 geometry checks、附 reference
   → job_status 轮询 → model_preview(asset_id)
```

占位符：`{prompt}` `{image}` `{outdir}` `{name}` `{mode}`；`command`/`args` 可以是列表或 shell 字符串（字符串按 `shlex` 切分，不经过 shell）。`capabilities.providers.local` 会显示命令是否存在、是否启用以及 `mode`。

### 路径五：云端生成（Tripo / 混元 3D）

云 provider 是异步任务：提交 → 轮询 → 下载 → 转 STL → 建资产 → 预览。不需要本机能跑模型。

```json
// ~/.xprint/config.json —— providers.cloud.<id>
{
  "providers": {
    "cloud": {
      "tripo": {
        "adapter": "tripo",
        "base_url": "https://openapi.tripo3d.com/v3",
        "model": "v3.1-20260211",
        "api_key_env": "TRIPO_API_KEY",
        "scale": 1000,
        "timeout_s": 600
      },
      "hunyuan": {
        "adapter": "hunyuan",
        "region": "ap-guangzhou",
        "model": "3.0",
        "secret_id_env": "TENCENTCLOUD_SECRET_ID",
        "secret_key_env": "TENCENTCLOUD_SECRET_KEY",
        "scale": 1,
        "timeout_s": 600
      }
    }
  }
}
```

```text
generate_cloud(provider="tripo", prompt=..., image=..., confirm="CLOUD <name>")
   → 提交文生/图生任务，立即返回 job_id（真后台）
   → job_status 轮询：progress 5–95%，message 显示 provider 状态
   → success：下载 model_url → trimesh 转 STL（支持 GLB/OBJ/GLTF/FBX）
   → 存 model.stl（+ 原始 model.glb/obj），geometry checks，资产 kind=cloud
   → model_preview(asset_id)
```

- 凭据：Tripo 用 `TRIPO_API_KEY`（Bearer）；混元用腾讯云 `TENCENTCLOUD_SECRET_ID`/`TENCENTCLOUD_SECRET_KEY`（TC3-HMAC-SHA256 签名，`region` 默认 `ap-guangzhou`，接口 `ai3d.tencentcloudapi.com`，Version `2025-05-13`）。`capabilities.providers.cloud` 只显示是否配置，不返回密钥。
- 尺度：Tripo 的 GLB 按米导出，`scale: 1000` 换成毫米；混元直接请求 `ResultFormat: STL`，`scale: 1`。转换后仍以 STL 的真实 bbox/volume 为准。
- 计费：每次生成消耗 provider 额度；Tripo 的 `credits_consumed` 会写进资产 `usage`。
- 失败：provider 的 `error_message` 与 `task_id` 进入 job 错误；轮询超时由 `timeout_s`（默认 600 s）控制。

## 工具

| 工具 | 写操作 | 说明 |
| --- | --- | --- |
| `capabilities` | 否 | provider / 图像 provider / CAD / 渲染 / 切片 / 打印机能力，永不返回密钥 |
| `system_status` | 否 | 版本、Python、工作区、任务计数；固定 `writes_performed: false` |
| `job_status` | 否 | 按 `job_id` 查询任务；未知 id 返回 `job_not_found` |
| `asset_list` | 否 | 列出资产与版本数 |
| `model_show` | 否 | 读回资产清单、指定版本源码、`params.json` 与 reference |
| `blender_run` | 是 | 用 Blender 后台模式执行 bpy 脚本，导出 STL/GLB 并检查；`confirm` 精确为 `BLENDER <name>` / `BLENDER <asset_id>` |
| `generate_local` | 是 | 后台运行 `providers.local.<id>` 命令生成 STL，建资产并返回 `job_id`；`confirm` 精确为 `LOCAL <name>` |
| `generate_cloud` | 是 | 后台提交 Tripo/混元 3D 任务，轮询后下载并转 STL 建资产；`confirm` 精确为 `CLOUD <name>` |
| `image_generate` | 是 | 文生概念图并存入 `references/`；`confirm` 精确为 `IMAGE <name>`，图片内联返回 |
| `reference_import` | 是 | 把用户已有的草图/照片导入为 reference；`confirm` 精确为 `IMPORT <name>` |
| `model_write` | 是 | 存入 build123d 脚本（新建或追加版本，可带 reference）；`confirm` 精确为 `WRITE <name>` / `WRITE <asset_id>` |
| `model_build` | 是 | 沙箱执行某版本，返回 geometry checks 并存 STL/STEP；`confirm` 精确为 `BUILD <asset_id>` |
| `model_preview` | 否* | 四视图 PNG（含概念图对比）内联返回；只写 `cache/previews/`，不改资产 |
| `asset_export` | 是 | 把 STL/STEP/脚本复制到用户可打开的位置；`confirm` 精确为 `EXPORT <asset_id>` |

错误统一为 `{"ok": false, "error": {"code", "message", "hint", "details"}}`。`hint` 是给人和 agent 的下一步提示，`details` 里可能有被拒绝的行号或缺失的产物名。

## 安全边界

1. **只读工具免费**：`capabilities`、`system_status`、`job_status`、`asset_list`、`model_show` 不需要确认，且明确返回 `writes_performed: false`；`model_preview` 只写预览缓存。
2. **写操作必须精确确认**：确认串逐字符比较，错了返回 `confirmation_mismatch` 且不写任何文件。
3. **路径沙箱**：除 `reference_import`（只读用户给的图片路径）和 `asset_export`（只写用户指定的目标）外，所有路径经 `safe_path()` 解析符号链接后校验，逃出工作区一律 `path_outside_workspace`。
4. **脚本策略 + 执行沙箱**：build123d 脚本先过 AST 审查（禁 `os/pathlib/subprocess/socket/shutil/ctypes/requests/urllib/http/multiprocessing`、禁 `eval/exec/open`…），再在 `cad_worker.py` 子进程里执行：RLIMIT_CPU/FSIZE/NOFILE、独立进程组、超时 SIGKILL、环境变量剔除密钥。bpy 脚本同样先过静态审查（同样禁网络/进程/文件模块，必须使用 `bpy`），再由 `blender -b --factory-startup` 子进程执行，使用独立的 Blender 配置目录、超时杀进程组。`generate_local` 运行的是用户自己配置的命令，环境变量剔除密钥并带超时；这是「用户信任本机命令」的显式边界。静态审查是纵深防御，不是沙箱；真正的隔离边界是子进程 + 资源限额。
5. **不开放任意 G-code**：切片产物先检查再发送，`printer_*` 只接受切片器产物。
6. **密钥**：只从环境变量或 `~/.xprint/secrets.json`（0600）读取；日志与工具输出统一脱敏。

## 开发

```bash
.venv/bin/python -m pytest -q          # 单元 + 内存 MCP + respx 模拟 HTTP
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
```

测试覆盖：配置优先级与 BOM、路径逃逸与符号链接、任务状态机非法迁移、脚本策略、资产版本 v001→v002 与 reference、CAD 子进程（真实 build123d 立方体：watertight/bbox/volume、超时杀进程）、概念图生成（respx 模拟，含 401/429/超时/base64/URL 下载）、四视图渲染（二进制/ASCII STL、像素级校验）、bpy 策略与 Blender 执行（假 Blender 覆盖成功/失败/超时/无导出；真机立方体）、本地生成器（命令模板、STL 发现、超时、环境变量）、后台任务执行器、`model_write`/`model_build`/`model_preview`/`asset_export`/`reference_import`/`blender_run`/`generate_local`/`generate_cloud`、MCP stdio 握手与 HTTP Bearer 鉴权。

## 目录

```
XPrint/
  pyproject.toml
  src/xprint/
    server.py          # MCP 入口（stdio / streamable HTTP）+ 14 个工具
    cli.py             # xprint init|capabilities|status|config|tools|serve|job|cad|image|preview
    config.py          # config.json / secrets / 环境变量优先级
    workspace.py       # ~/.xprint 布局 + safe_path 沙箱
    jobs.py            # 任务状态机与 job.json
    assets.py          # 资产 manifest + vNNN 版本 + reference
    images.py          # 概念图生成（OpenAI 兼容）+ reference 存储
    preview.py         # numpy/Pillow 四视图渲染
    cad.py             # 父进程侧 CAD 执行器（超时/环境清理）
    cad_worker.py      # 子进程入口（rlimit + build123d + STL 检查）
    registry.py        # 适配器注册表
    capabilities.py    # 能力探测（只读）
    script_policy.py   # build123d 脚本静态审查
    errors.py          # 统一错误码
  dsh-xprint-mcp/      # DSH 配置文件型 bundle
  tests/
  docs/
```

## 功能与路线图

| 能力 | 现状 |
| --- | --- |
| 对话式创作 | ✅ 宿主模型驱动 `model_write`/`model_build`/`model_preview`；每轮是同一资产的新版本 |
| 图片理解 | ✅ 多模态宿主模型看图 + `reference_import`/`image_generate` 存 reference，预览时回传对比 |
| Blender 文/图生 3D | ✅ `blender_run`（bpy 脚本 → STL/GLB + 四视图）；图生 3D 由宿主模型看图写场景 |
| 本地模型文/图生 3D | ✅ `generate_local` 通用命令适配器（TripoSR / Hunyuan3D / ComfyUI 等，以本机命令为准） |
| 云端文/图生 3D（Tripo/混元） | ✅ `generate_cloud` 异步任务 + 下载 + 转 STL + 预览（需配置 key） |
| 导出模型文件 | ✅ `asset_export`（STL/STEP/脚本） |
| 切片 + 发送打印机 | ⏳ 未实现：M2 切片适配、M3 打印机 CRUD + 发送 |

- **M0（完成）**：脚手架、配置/密钥、路径沙箱、任务状态机、MCP stdio+HTTP、资产版本库。
- **M0.5（完成，2026-11-14）**：沙箱化 build123d 执行器、概念图生成、四视图预览、两条建模闭环、导出、11 个工具。
- **M0.6（完成，2026-11-14）**：Blender bpy 路径（`blender_run`）与本地生成器适配器（`generate_local` + 后台任务），13 个工具。
- **M1（完成，2026-11-14）**：云端文/图生 3D（Tripo V3、混元 3D Pro）异步任务、下载与 STL 转换，14 个工具。
- **M2**：切片器适配（Orca/Bambu 外部进程，profile 运行时读取）与产物审计。
- **M3**：打印机 CRUD（Moonraker / PrusaLink）、`send` + 审批流。
