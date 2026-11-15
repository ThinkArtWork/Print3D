# Print3D 使用手册

[中文](MANUAL.md) · [English](MANUAL.en.md)

> 这是一份面向使用者的操作手册：从安装、接入 MCP 宿主，到建模、预览、切片、上机，以及出错时先查哪里。
>
> - 想先了解项目全貌：看 [README](../README.md) 的「快速开始」与「工具一览」。
> - 只想尽快跑起来：用 §1.2 的 Agent 安装提示词，或按 §1.3 手动安装。
> - 需要全部切片参数与打印机字段：见 §8、§9；排错见 §12。
>
> 文中命令默认在仓库根目录执行；`print3d` 指仓库虚拟环境里的 `.venv/bin/print3d`，用 `uv run print3d` 也可以。

## 目录

1. [安装与初始化](#1-安装与初始化)
2. [接入 MCP 宿主](#2-接入-mcp-宿主)
3. [第一个零件](#3-第一个零件)
4. [资产、版本与任务](#4-资产版本与任务)
5. [建模路径怎么选](#5-建模路径怎么选)
6. [预览、工程图与导出](#6-预览工程图与导出)
7. [可打印性检查与摆放](#7-可打印性检查与摆放)
8. [切片](#8-切片)
9. [打印机与上机](#9-打印机与上机)
10. [配置与密钥](#10-配置与密钥)
11. [安全边界](#11-安全边界)
12. [故障排查](#12-故障排查)
13. [命令行速查](#13-命令行速查)
14. [常见任务配方](#14-常见任务配方)
15. [升级、备份与卸载](#15-升级备份与卸载)
16. [更多文档](#16-更多文档)

---

## 1. 安装与初始化

### 1.1 需要什么

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.11+ | CI 覆盖 3.11 / 3.13 |
| 系统 | macOS / Linux | Windows 不可用：CAD/切片 worker 依赖 POSIX 进程组与 `resource` |
| [uv](https://docs.astral.sh/uv/) | 推荐 | 也可以用自己的 venv + `pip` |
| 磁盘 | 约 250 MB 起 | build123d / OCP 是主要体积 |
| Blender（可选） | 3.x+ | `blender_run`、`mesh_fit` 需要 |
| OrcaSlicer（可选） | 2.x | `slice_*` 需要；用你已安装的 CLI |

**不需要任何 API key**，也能完成参数化建模、预览、可打印性检查和导出。key 只影响概念图、云端生成等可选路径。

### 1.2 交给 Agent 安装（推荐新手）

不用自己逐条敲命令。复制下面这段（GitHub 代码块右上角有复制按钮）交给能执行命令的 Agent：

```text
你是我的安装助手。请在这台 macOS/Linux 机器上安装 Print3D MCP 服务：

仓库：https://github.com/ThinkArtWork/Print3D
依赖：Python 3.11+、git、uv；Windows 不支持。

1. 检查系统和依赖；缺 uv 就按官方方式安装；Python 版本不够就告诉我，不要擅自改系统 Python。
2. 克隆仓库到合适目录，执行 uv sync 和 uv run print3d init。
3. 运行 uv run print3d capabilities，报告 CAD、渲染、切片器、打印机状态。
4. 检查 OrcaSlicer 和 Blender 是否安装；缺失只报告，不要自动下载大文件。
5. 根据我的 MCP 宿主（DSH / Claude Desktop / Cursor）生成 stdio 配置；command 使用仓库内 .venv/bin/print3d 的绝对路径，写入前先给我确认。
6. 不要索要 API key，核心功能不需要。最后给我一条测试提示词。
```

### 1.3 从源码安装

```bash
git clone https://github.com/ThinkArtWork/Print3D.git
cd Print3D
uv sync
uv run print3d version
```

`uv sync` 会创建 `.venv` 并安装运行依赖。要跑测试和 lint，加上开发依赖：

```bash
uv sync --extra dev
uv run python -m pytest -q
```

> Print3D 尚未发布到 PyPI。在首个 GitHub Release 出现之前，不要用 `pipx install print3d`；源码安装是唯一可靠的方式。Release 发布后，也可以 `uv tool install .` 或 `pipx install .` 把命令装到 PATH。

### 1.4 初始化工作区

```bash
uv run print3d init
uv run print3d capabilities
```

默认工作区是 `~/.print3d`。`init` 创建目录骨架和 `config.json`，**不写任何密钥**，也不会覆盖已有配置；确实要重写时加 `--force`。

```bash
# 多实例 / 测试：换一个工作区
uv run print3d init --workspace /path/to/print3d-workspace

# 或者用环境变量
export PRINT3D_WORKSPACE=/path/to/print3d-workspace
```

工作区里会出现：

```text
<workspace>/
  assets/         # 每个资产一个目录，版本在 v001/、v002/…
  cache/          # 预览图等可重建缓存
  config.json     # 非敏感配置
  exports/        # asset_export 的默认落点（首次导出时创建）
  jobs/           # 后台任务记录 job.json
  logs/
  profiles/       # 切片 profile（如你的 U1 预设）
  references/     # 生成的概念图
  secrets.json    # 可选，权限必须是 0600
  slices/         # 切片产物 plate_1.gcode、statistics.json
```

`print3d config` 打印合并后的生效配置，密钥永远只显示「有没有」，不显示值。配置优先级是：

```text
命令行参数 > 环境变量 > <workspace>/config.json > 内置默认值
```

---

## 2. 接入 MCP 宿主

Print3D 是一个标准 MCP server，本身没有 UI。宿主（DSH、Claude Desktop、Codex、Cursor 等）拉起它，然后由宿主的模型调用工具。

### 2.1 stdio（最常用）

在宿主的 MCP 配置里加一条：

```json
{
  "mcpServers": {
    "print3d": {
      "command": "/absolute/path/to/Print3D/.venv/bin/print3d",
      "args": ["serve", "--stdio"],
      "env": {
        "PRINT3D_WORKSPACE": "/Users/you/.print3d"
      }
    }
  }
}
```

两个容易踩的点：

- **命令必须是绝对路径**。宿主不会替你 `cd` 到仓库，也不会展开 `~`。
- 如果希望工作区跟着项目走，把 `PRINT3D_WORKSPACE` 指到项目的 `.print3d`；否则默认用 `~/.print3d`。

### 2.2 DSH

DSH 有两条路，推荐第一条：

1. **mcp-manager**：在 `Settings → MCP` 里新增 stdio 服务器，命令填上面的绝对路径。若当前 DSH 版本没有 MCP 设置页，直接改 profile 的 `cordis.patch.yml`，条目 `id` 用 `mcp-manager`，改完重启 DSH。
2. **自定义 bundle**：安装仓库里的 [`dsh-print3d-mcp/`](../dsh-print3d-mcp/README.md)。它默认调用 PATH 上的 `print3d`；如果只用 `uv sync` 的虚拟环境，记得把 bundle 里的 `command` 改成绝对路径。

### 2.3 streamable HTTP

```bash
# 只监听本机
uv run print3d serve --http

# 从别的机器访问：必须先设置 token
export PRINT3D_TOKEN="$(openssl rand -hex 32)"
uv run print3d serve --http --host 0.0.0.0 --port 8765
```

默认地址是 `http://127.0.0.1:8765/mcp`。规则：

- 绑定非回环地址时，**没有 token 会直接拒绝启动**；
- 客户端用 `Authorization: Bearer <token>`；
- 不要把端口暴露到公网。需要跨机访问时，放在 Tailscale / SSH 隧道 / 反向代理后面。

### 2.4 验证连接

接入后先让宿主调用这两个只读工具：

```text
capabilities()      # CAD / 渲染 / 切片 / provider / 打印机是否就绪
system_status()     # 版本、工作区、任务计数；writes_performed: false
```

`capabilities` 里重点看：

- `cad.available: true`：build123d 可用；
- `providers.cloud[].ready`：云端生成是否配置了 key；
- `targets`：切片目标是否解析到预设；
- `printers`：打印机是否配置。

---

## 3. 第一个零件

给宿主一句足够具体的话，例如：

> 做一个 120 × 80 × 22 mm 的三格收纳盘，壁厚 2.4 mm，底厚 3 mm，四角 R8，输出 STL 和 STEP。

宿主通常会走这条链：

```text
capabilities() → model_write(script, confirm="WRITE <name>")
→ model_build(asset_id, confirm="BUILD <asset_id>")
→ model_preview(asset_id) → printability_check(asset_id, target="u1")
→ slice_plan(asset_id, "u1") → slice_run(..., confirm="SLICE <asset_id> u1")
→ printer_status("u1") → send(..., confirm="PRINT u1 <slice_id>")
```

你不需要记住每一步，但需要知道每一步在做什么，尤其是 `confirm`：

- `model_write` 只保存脚本（和参考图），不执行几何；
- `model_build` 在子进程里执行，返回真实几何检查；
- `model_preview` 只写预览缓存，不改资产；
- `slice_plan` 是 dry-run，不写文件；
- `send` 是唯一会真正让打印机动起来的工具，默认上传后立即开始。

### 3.1 一个最小脚本

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

这个脚本会得到一个 40 × 40 × 10 mm、中心 8 mm 通孔的方块。`model_build` 会返回类似：

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

判读顺序（都在 `checks` 里）：

1. `solids == 1`：一个实体，不是散件；
2. `watertight == true` 且 `boundary_edges == 0`：封闭；
3. `bbox_mm` / `volume_cm3`：尺寸和体量是否符合预期；
4. `nonmanifold_edges == 0`：没有非流形边。

任何一项不对，先改脚本再切片，不要在切片器里赌。

### 3.2 没有宿主时用 CLI

想快速验证脚本，可以绕过资产库直接跑：

```bash
uv run print3d cad run model.py --workdir /tmp/print3d-build
```

它会在 `/tmp/print3d-build` 写出 `model.stl` / `model.step`，并打印同样的 `checks`。适合调几何，不会创建资产，也不会出现在 `asset_list` 里。

---

## 4. 资产、版本与任务

### 4.1 常用标识

| ID | 形态 | 来源 | 用途 |
| --- | --- | --- | --- |
| `asset_id` | `a_…` | `model_write` / `blender_run` / `generate_*` / `mesh_fit` | 一个模型资产 |
| `revision` | `v001`、`v002`… | 每次 `model_write` 追加 | 资产的一个快照 |
| `job_id` | `j_…` | 云端生成、本地生成、切片、发送 | 后台任务 |
| `slice_id` | `s_…` | `slice_run` | 一次切片产物 |

`asset_list()` 看资产和最新版本；`model_show(asset_id, revision=...)` 读指定版本的脚本，并在 `revisions` 里列出每版的 prompt、参考图、几何检查、图纸和 viewer。

### 4.2 版本怎么产生

- 第一次 `model_write(script, name="parts-tray", confirm="WRITE parts-tray")` 创建资产和 `v001`；
- 传 `asset_id` 再写一次，得到 `v002`，旧版本原样保留；
- `model_build` 只构建指定版本，不创建新版本；
- 想回到旧版本：`model_show` 读出旧脚本，再 `model_write` 成一个新版本（保留历史），或直接用 `model_view(..., revision="v001")` 查看旧几何。

同一个资产不要混用建模后端：`model_build` 建的是 `build123d` 资产，`blender_run` 建的是 `blender` 资产，`generate_*` 建的是 `cloud` / `local` 资产。混用会报类型错误。

### 4.3 异步任务

云端生成、本地生成、切片、发送都是后台任务：

```text
generate_cloud(...) / generate_local(...) / slice_run(...) / send(...)
  → 立即返回 job_id
  → job_status(job_id) 轮询，或 CLI：print3d job show <job_id>
  → success 后拿 result（asset_id / slice_id / 上传结果）
```

`job_status` 返回 `state`、`progress`、`message`、`result` 和错误。任务失败时不要只看 `state`，`error.message` 和 `error.hint` 通常已经说明下一步。

---

## 5. 建模路径怎么选

| 你的输入 | 推荐路径 | 入口 | 产物 |
| --- | --- | --- | --- |
| 文字描述 + 明确尺寸 | build123d 参数化脚本 | `model_write` → `model_build` | STL + STEP + viewer |
| 文字描述，先要概念图 | 概念图 + build123d | `image_generate` → `model_write(reference=…)` | 同上，带概念图 reference |
| 草图 / 照片 / 已有概念图 | 参考图 + build123d | `reference_import` → `model_write(reference=…)` | 同上，带 reference |
| 有机形状、复杂倒角、阵列 | Blender bpy 脚本 | `blender_run` | STL + GLB + viewer |
| 已有网格（STL/GLB/OBJ），要真实尺寸 | 网格校准 | `mesh_fit` | 校准后的 STL + GLB |
| 已有 STEP，要改一个特征 | STEP 输入 + build123d | `model_write` → `model_build(input=…)` | 新版本 STL + STEP |
| 本机已有生成模型 | 本地命令适配器 | `generate_local` | STL 资产 |
| 愿意调用云端 API | Tripo AI / 腾讯混元 3D | `generate_cloud` | STL 资产（可自动定尺寸） |

**文字需求有两条完整路径：默认直接进入 build123d，不必先生成图片；也可以先 `image_generate` 出概念图，再把图片作为 `reference` 建模。** 前者适合尺寸、结构明确的零件；后者适合先定造型方向。手头已有草图/照片时用 `reference_import`。图片不是前置条件，但确实是可选的一类输入。

选择原则：**尺寸优先用 build123d，形状优先用 Blender/生成模型，生成模型必须 `mesh_fit` 或 `size_mm` 落到毫米。**

### 5.1 build123d：文字 → 参数化模型

脚本契约（`model_build` 会先做静态检查）：

1. 第一行必须是 `from build123d import *`；
2. 定义 `def build_part()`，返回**一个**实体（`Part` / `Solid`）；
3. 参数放 `PARAMS`，单位毫米；
4. 零件底面放在 `z=0`，方便贴床；
5. 只允许导入 `build123d` 和 `math`；
6. 不允许 `_` 开头的属性访问，也不允许 `eval` / `exec` / `open` / `getattr` / `__import__` 等动态入口；
7. 脚本上限 64 KiB。

静态检查是纵深防御，真正的隔离在子进程：独立进程组、CPU/文件/句柄限额、超时 SIGKILL、环境变量白名单。不要把它当成多租户沙箱——它假设运行脚本的人就是你。

**改已有 STEP：**

```text
asset_id = model_write(script, name="bracket-v2", confirm="WRITE bracket-v2")
model_build(asset_id, input="/path/to/bracket.step", confirm=f"BUILD {asset_id}")
```

脚本里用模块全局 `INPUT_STEP` 读取这个 STEP：

```python
from build123d import *

def build_part():
    base = import_step(INPUT_STEP)
    # …在 base 上切孔、加凸台…
    return base
```

`input=` 会把 STEP 复制进当前版本，记录 sha256、原文件名和大小；之后即使原文件被移动，版本仍然可重建。

**迭代：** 每次改脚本都 `model_write` 追加新版本，再 `model_build`，不要覆盖旧版本。`model_show` 能看到每版的 prompt 和几何，方便比较。

### 5.2 图片 → 参考 → 模型

```text
reference_import(path="/path/sketch.jpg", name="lamp-base", confirm="IMPORT lamp-base")
# 返回 image_path（工作区内的相对路径，如 references/ref_.../image.png）
# 或者：image_generate(prompt="…", name="lamp-base", confirm="IMAGE lamp-base")
  → model_write(script, reference="<image_path>", confirm="WRITE lamp-base")
  → model_build → model_preview        # 预览会把概念图和渲染图并排返回
```

- `reference_import` 只从你给的路径读取，副本存进工作区 `references/`；`image_generate` 需要 OpenAI 兼容的 images API key。`model_write` 的 `reference` 必须是**工作区内**路径（`image_path` 就是），直接传外部绝对路径会被 `path_outside_workspace` 拒绝。
- Print3D **不内置视觉模型**。看图的是宿主模型；它负责从图片里读比例、孔位和结构，Print3D 负责把图片存进版本、在预览时回传对比。
- 图片看不到的背面、内部结构和精确尺寸，必须在 prompt 里补，或者改用工程图/STEP。

### 5.3 Blender：有机形状和 bpy 脚本

```text
blender_run(script, name="organic-shell", confirm="BLENDER organic-shell")
  → Blender 后台模式执行 bpy 脚本
  → 导出所有 MESH 对象为 model.stl + model.glb
  → 同样的几何检查、预览、viewer
```

- 适合倒角、布尔、阵列、修改器和 Blender 自带建模工具；不需要 build123d。
- 只导出 `MESH` 对象。布尔切割用的辅助对象、空物体、相机、灯光记得删掉，否则会一起进 STL。
- 图片输入：`blender_run(image="/path/ref.png", ...)`；脚本在 `sys.argv` 的 `--` 之后拿到同一路径。
- 追加版本用 `blender_run(asset_id=<id>, confirm="BLENDER <asset_id>")`。

### 5.4 mesh_fit：生成网格 → 真实尺寸

图生 3D 返回的是形状，不是尺寸。`mesh_fit` 把已有网格清理、缩放、居中落地、减面并导出：

```text
mesh_fit(
  asset_id="a_…",                 # 或 source="/path/model.stl"
  size_mm=[116, 101, 44],         # 已知三轴尺寸：精确缩放
  name="product-shell",
  max_triangles=145000,
  confirm="FIT product-shell",
)
```

不知道三轴、只知道最长边时，用等比模式：

```text
mesh_fit(source="/path/model.stl", max_dimension_mm=120, name="display-piece",
         confirm="FIT display-piece")
```

细节：

- `size_mm` 和 `max_dimension_mm` 二选一，都不给会报参数错误；
- 内部会合并重复点、补边界洞、重算法线，缩放后 XY 居中、底面落到 `z=0`，再减面；
- `max_triangles` 默认 145,000，是为了让四视图预览完整渲染；超过 150,000 的预览会抽样成线框，不是几何上限；
- 尺寸来自厂商规格或你的测量，不要拿生成网格的 bbox 当设计尺寸。

### 5.5 云端生成：Tripo AI / 腾讯混元 3D

云端 provider 是异步任务：提交 → 轮询 → 下载 → 转 STL → 建资产 → 预览。不需要本机能跑模型。

```text
generate_cloud(provider="tripo", prompt="…", name="lamp-shade",
               confirm="CLOUD lamp-shade")
  → 立即返回 job_id
  → job_status(job_id)
  → 结果资产：model.stl、几何检查、viewer
```

可选 `size_mm=[x,y,z]` 或 `max_dimension_mm`：生成后自动走同一套 mesh fit，直接落到毫米。

配置与注意事项：

- Tripo 用 `TRIPO_API_KEY`；混元用 `HUNYUAN3D_API_KEY`（OpenAI 兼容模式），也支持旧的腾讯云 TC3 模式。
- 云端网格通常不带物理单位，`scale` 默认 1000 只是与厂商导出习惯对齐；**请按生成后的 bbox 调整**。
- 每次生成消耗 provider 额度，`usage` 会记录 `credits_consumed` 等字段。
- prompt 和参考图会离开本机，发给对应 provider；敏感图片不要用云端路径。
- 失败时看 `job.error.message` 和 `task_id`；轮询超时由 provider 配置的 `timeout_s` 控制。

### 5.6 本地部署模型：任何能写 STL 的命令

你可以在本机部署 TripoSR、Hunyuan3D、ComfyUI 等模型/服务；Print3D 不下载权重，也不绑定框架，只负责调用你配置的命令。任何能读提示词/图片、写出 STL 的本地命令都能接进来：

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
  → 后台任务，命令在 clean env 中运行，cwd=<outdir>
  → 必须写出 <outdir>/mesh.stl（或 stdout 打印 {"mesh": "/path/model.stl"}）
  → Print3D 建资产、算几何检查、附 reference
```

占位符：`{prompt}`、`{image}`、`{outdir}`、`{name}`、`{mode}`。`command` / `args` 可以是列表或字符串；字符串按 `shlex` 切分，不经过 shell。这是「用户信任本机命令」的边界，不要配置来路不明的脚本。

---

## 6. 预览、工程图与导出

### 6.1 四视图预览

`model_preview(asset_id, revision="")` 把 STL 渲染成 ISO / FRONT / TOP / RIGHT 四视图，以图片内容返回给宿主。它只写 `cache/previews/`，不改资产，所以不需要 `confirm`。

版本有 reference 时，预览会把概念图和渲染图并排返回，方便对比「要的」和「做出来的」。预览图丢失或想换尺寸时可以重跑；它不是交付物。

### 6.2 交互式 viewer

构建成功时（`model_build`、`blender_run`、`mesh_fit`、`generate_local` / `generate_cloud`）会自动生成：

```text
assets/<asset_id>/viewer.html
assets/<asset_id>/<模型名>-viewer.html   # 同名硬链接，标签页显示模型名
```

一个资产只有一个 viewer，里面内嵌**全部已构建版本**，右上角下拉切换。打开后支持旋转、平移、缩放、双击复位、自旋、截图、导出 STL。它是单文件、无 CDN、无服务端，可以直接发给别人。

需要手动刷新或指定默认版本时：

```text
model_view(asset_id, revision="v001", confirm="VIEW <asset_id>")
```

### 6.3 工程图

```text
model_drawing(asset_id, confirm="DRAWING <asset_id>")
  → drawing.svg / drawing.dxf
```

第三角四视图（front/top/right/iso），正交视图带虚线隐藏线，等轴测按惯例不画隐藏线，1:1 真实毫米，带视图标签。适合给供应商或同事看传统图纸；几何仍以 STEP 为准。

### 6.4 导出

```text
asset_export(asset_id, artifact="stl", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="step", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="script", confirm="EXPORT <asset_id>")
asset_export(asset_id, artifact="svg" | "dxf", confirm="EXPORT <asset_id>")
```

`artifact` 只接受 `stl`、`step`、`script`、`svg`、`dxf`；默认落到工作区 `exports/`。`out_path` 也必须解析到工作区内，不能借导出覆盖工作区外的文件。

G-code 不走 `asset_export`：切片产物在 `slices/<slice_id>/plate_1.gcode`，统计在 `slices/<slice_id>/statistics.json`（是否生成统计取决于切片器）。

---

## 7. 可打印性检查与摆放

两个只读工具，不需要 `confirm`，不改资产：

```text
printability_check(asset_id, target="u1", revision="")
orientation_plan(asset_id, target="u1", limit=3)
```

`printability_check` 给出：

- 包围盒 / 体积；
- >45° 悬垂面积与占比；
- 支撑体积估计；
- 热床接触面积、是否超出目标热床；
- 薄壁估计（射线法，5% 分位最小值、<1.2 mm 面积占比）；
- 0–100 分和可执行的 issue / hint。

`orientation_plan` 枚举 24 个轴向摆放（6 个朝下轴 × 4 个平面旋转），按悬垂、支撑估计、高度、热床接触排序，返回推荐旋转矩阵、`down_axis`、`spin_deg` 和候选指标。它是建议，**不会自动改写资产**；把推荐旋转应用到切片器或脚本里，再重跑检查。

评分是几何启发式，不是 FEM，也不替代真实切片。薄壁是射线采样估计，支撑体积按「悬垂面积 × 高度 × 0.25」估算，最终支撑量以切片器为准。

推荐顺序：

```text
model_build → printability_check → （有问题）orientation_plan → 改脚本/重摆
→ printability_check → slice_plan
```

---

## 8. 切片

切片器是外部进程：Print3D 只调用你已安装的 OrcaSlicer CLI，不链接、不分发 GPL 代码，也不改你的 vendor 预设。

### 8.1 配置切片目标

在 `<workspace>/config.json` 的 `targets` 下加一个目标：

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

关键字段：

| 字段 | 作用 |
| --- | --- |
| `printer_model` | 必须与打印机配置的 `model` 一致，`send` 会核对 |
| `slicer` / `slicer_path` | 切片器类型和 CLI 绝对路径 |
| `machine` / `process` / `filament` | Orca 预设路径；也可以是扁平化的单文件 profile |
| `overrides` | 固定的 process 覆盖，如层高 |
| `timeout_s` | 外部切片进程超时 |

路径必须是绝对路径，文件必须存在且可读。`slice_capabilities()` 会告诉你每个 target 是否 `ready`、slicer 版本、bed 尺寸。

### 8.2 先 dry-run

```text
slice_capabilities()
slice_plan(asset_id="a_…", target="u1", material="PETG", layer_height=0.24)
```

`slice_plan` 不写文件，返回：解析到的 presets、将执行的命令、几何检查、参数摘要，以及 `slice_run` 需要的**精确确认串**（形如 `SLICE <asset_id> u1`）。

### 8.3 运行与检查

```text
slice_run(asset_id="a_…", target="u1",
          confirm="SLICE a_… u1",
          material="PETG", layer_height=0.24,
          infill="gyroid", infill_density=30, strength="strong")
  → job_id
slice_inspect(slice_id="s_…")
```

`slice_inspect` 返回层数、预计时间、耗材、`printer_model`、G-code 路径和 warnings。切片失败时先看 job 的 stderr 和 `slice_inspect` 的 warnings，再查预设路径/切片器版本。

### 8.4 常用参数

| 参数 | 取值 / 说明 |
| --- | --- |
| `material` | PLA / PLA+ / PETG / ABS / ASA / TPU / PC / PA 等；自动匹配喷嘴与热床温度 |
| `layer_height` | 0.05–1.0 mm |
| `infill` | grid / gyroid / honeycomb / cubic / concentric / triangle / line / rectilinear / zigzag / crosshatch / lightning / adaptivecubic / supportcubic |
| `infill_density` | 0–100%；`-1` 表示沿用预设 |
| `strength` | `light` / `normal` / `strong`，映射墙数和顶/底层数；也可单独传 `walls`、`top_shells`、`bottom_shells` |
| `bed_temp` / `nozzle_temp` | 显式覆盖材料默认温度 |
| `build_plate` | `textured_pei` / `smooth_pei` / `cool_plate` / `engineering_plate` / `supertack` / `textured_cool` |
| `flow_ratio` | 0.5–1.5，写入 `filament_flow_ratio` |
| `pressure_advance` | 0–0.2，写入 `pressure_advance` 并启用 |
| `line_width` / `outer_wall_line_width` / `inner_wall_line_width` / `top_surface_line_width` | 线宽 mm；流量和线宽要一起看 |
| `arc_fitting` | 是否输出 G2/G3 圆弧；Klipper 支持时开启，圆孔更圆、文件更小 |
| `fan_speed` / `aux_fan_speed` / `fan_first_layers` | 模型风扇、辅助/腔体风扇、前几层关风扇 |
| `printer` + `slot` | 绑定打印机和耗材槽；自动取槽的材质/颜色，材质冲突会拒绝 |

材料温度会按 `material` 自动映射；需要精调时用 `bed_temp` / `nozzle_temp` 显式覆盖。U1 的多工具头与耗材槽配置见 §9.3。

### 8.5 切片常见问题

- **`slice_capabilities` 显示 target 不 ready**：九成是 `machine` / `process` / `filament` 路径写错，或切片器路径不对。先用绝对路径逐个确认文件存在。
- **切片器段错误 / 预设加载失败**：厂商定制版 Orca 可能不兼容无头 CLI；换上游 OrcaSlicer，或把 profile 扁平化（去掉 `inherits`、展开厂商占位符）。
- **切出来超出热床**：`slice_plan` 会检查 bed；先用 `printability_check` 看 bbox，再考虑缩放或换摆放。
- **G-code 是空的 / 没有统计**：看 `slice_inspect` 的 `warnings` 和 job stderr；有些切片器不写 `statistics.json`，层数和耗材会从 G-code 头解析。

---

## 9. 打印机与上机

**已实机验证的机型：Snapmaker U1**（Moonraker，4 工具头，textured PEI）。下面以它为例；其他 Klipper / Moonraker 机器换 `base_url` 和 `model` 即可，多工具头机器再按实际头数配置 `toolheads` 和 `slots`。

### 9.1 配置打印机

`printers` 是数组，每台一个对象。Moonraker / Klipper 示例：

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

也可以用工具写入，不必手改：

```text
printer_save(printer_id="u1", adapter="moonraker", base_url="http://192.168.1.50:7125",
             model="Snapmaker U1", toolheads=4, build_plate="textured_pei",
             confirm="SAVE PRINTER u1")
```

`printer_save` 按 `printer_id` upsert；`printer_remove(printer_id="u1", confirm="REMOVE PRINTER u1")` 删除。

### 9.2 适配器

| 适配器 | 适用 | 说明 |
| --- | --- | --- |
| `moonraker` | Klipper / Snapmaker U1 等 | `/server/files/upload` + `/printer/print/start`；支持任意 G-code 校准命令和 JSON 校准系数 |
| `octoprint` | OctoPrint | `/api/files/local` + `/api/job` |
| `bambu_lan` | 拓竹 LAN 模式 | MQTT/TLS + 隐式 FTPS + 3MF；社区协议实现，**尚未真机验证** |
| `prusalink` | PrusaLink | HTTP Digest；密码用打印机屏幕上的 password，不是 API key |
| `http` | 未知/自研打印机 | 端点、字段路径、状态名全部写在配置里，不用改代码 |

Bambu LAN 需要 `serial` 和 `access_code_env`；PrusaLink 的 `username` 默认 `maker`，密码放 secrets 或 `password_env`。

### 9.3 工具头与耗材槽

- `toolheads` 支持 1–9；多喷头机器按物理工具头编号，单喷头 AMS 按槽位编号。
- `slots` 描述每个槽的 `material` / `color`（`#RRGGBB`）/ `name`；可选的 `toolhead` 字段显式声明槽位属于哪个物理头。
- 切片时传 `printer="u1", slot=1`：自动取该槽材质，生成 `Tn`，材质冲突直接报错。
- `send` 会按 G-code 实际用到的 `Tn` 逐槽核对材质；没有 `slots` 时给 warning，但仍检查 `toolheads` 上限。

### 9.4 上机

```text
printer_status("u1")                       # 必须先确认 idle / 型号
send(slice_id="s_…", printer_id="u1", confirm="PRINT u1 s_…")
send(..., start=false)                     # 只上传，不启动
printer_cancel("u1", confirm="CANCEL PRINT u1")
```

`send` 的前置检查：切片成功、G-code 存在、打印机 idle、切片目标的 `printer_model` 与打印机 `model` 一致、耗材槽匹配。任一不满足直接拒绝，不做自动重试。

默认 `start=true`，上传后立即开始。真实打印是不可逆的物理动作，第一次建议：

1. `send(..., start=false)` 只上传；
2. 在打印机屏幕上确认文件名和预览；
3. 再从屏幕启动，或再次 `send(..., start=true)`。

### 9.5 自动流量校准

`send(..., flow_calibrate=True)` 会在启动前执行打印机配置里的 `flow_calibration.enable_gcode`。例如 Snapmaker U1 配的是：

```text
SET_PRINT_PREFERENCES FLOW_CALIBRATE=1
```

打印机随后在 `PRINT_START` 里跑 `SM_PRINT_FLOW_CALIBRATE`。通用 Klipper 可以换成自己的宏。该命令只能在空闲状态设置，打印中会被固件拒绝；未配置或适配器不支持时会明确报错，不会静默跳过。

多色时按**物理工具头**去重：多工具头机器分别校准用到的头；单喷嘴多色只校准一次。`trim_unused_tools` + `tool_macros` 可以裁掉起始 G-code 里未使用工具头的预热/进料/校准行，但只匹配配置里列出的宏名。

---

## 10. 配置与密钥

### 10.1 优先级

```text
CLI 参数 > 环境变量 > <workspace>/config.json > 默认值
```

常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `PRINT3D_WORKSPACE` | 工作区路径，默认 `~/.print3d` |
| `PRINT3D_CONFIG` | 显式指定 `config.json` |
| `PRINT3D_TOKEN` | HTTP Bearer token |
| `TRIPO_API_KEY` / `HUNYUAN3D_API_KEY` / `OPENAI_API_KEY` | provider key |

provider key 的解析顺序：`api_key_env` 指定的变量 → `PRINT3D_<ID>_API_KEY` → `<ID>_API_KEY` → `secrets.json`。

### 10.2 config.json 结构

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

密钥**不写进 config.json**。`print3d config` 只报告 `has_key`，日志和工具输出会脱敏。

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

权限过宽会触发 `secrets_insecure` warning。更推荐把 key 放环境变量，尤其是共享机器和 CI。

### 10.4 多实例

用不同的 `--workspace` / `PRINT3D_WORKSPACE` 隔离资产、任务和配置。两个实例不要共用同一个工作区：任务队列、切片目录和 `config.json` 都会互相踩。

---

## 11. 安全边界

1. **只读工具免费**：`capabilities`、`system_status`、`job_status`、`asset_list`、`model_show`、`printability_check`、`orientation_plan` 不需要确认，并返回 `writes_performed: false`；`model_preview` 只写预览缓存。
2. **写操作要精确确认**：确认串逐字符比较，错了返回 `confirmation_mismatch`，不写任何文件。不要凭印象拼 `confirm`，从 `slice_plan` / 工具返回里复制。
3. **路径沙箱**：除 `reference_import` 读取你指定的图片外，用户给的路径都会解析符号链接后校验；逃出工作区返回 `path_outside_workspace`。`asset_export` 的 `out_path` 也不能写到工作区外。
4. **脚本策略 + 子进程隔离**：build123d 脚本先过 AST 白名单，再在独立进程组里执行，带 CPU/文件/句柄限额、超时 SIGKILL 和干净环境变量。bpy 脚本同样先审后跑。静态审查是纵深防御，不是多租户沙箱。
5. **下载有上限**：云端模型下载只接受 http(s)、限制重定向次数、边下边计数；概念图 URL 也有大小上限。HTTP 模式用恒时比较 Bearer token。
6. **不开放任意 G-code**：`send` 只接受切片器产物，先检查再上传；不做自动重试。
7. **密钥不落日志**：只从环境变量或 `secrets.json` 读取，输出统一脱敏。
8. **默认本机**：除显式调用云端生成外，建模、渲染、切片都在本机完成；发送只到你的打印机。

**它不是面向不可信用户的服务。** HTTP 模式不要暴露公网；共享机器上把工作区和 secrets 的权限管好；云端生成会把 prompt/图片发给第三方，敏感内容走本地路径。

---

## 12. 故障排查

所有工具失败都返回统一信封：

```json
{"ok": false, "error": {"code": "…", "message": "…", "hint": "…", "details": {}}}
```

**先读 `error.hint`**，它通常就是下一步。常见错误码：

| code | 含义 | 处理 |
| --- | --- | --- |
| `config_missing` | 没找到 `config.json` | `print3d init`，或检查 `PRINT3D_WORKSPACE` / `--workspace` |
| `config_invalid` | 配置 JSON / 字段不合法 | `print3d config` 看解析结果，按 hint 改字段 |
| `secrets_insecure` | `secrets.json` 权限过宽 | `chmod 600 <workspace>/secrets.json` |
| `path_invalid` | 路径格式不对 | 用绝对路径或工作区内相对路径 |
| `path_outside_workspace` | 路径逃出工作区 | 把文件放进工作区，或改用 `reference_import` |
| `not_found` | 资产 / 版本 / 文件不存在 | 先 `asset_list` / `model_show`，确认 id 和 revision |
| `invalid_argument` | 参数缺失或冲突 | 看 `details`，常见是 `size_mm` 与 `max_dimension_mm` 同时给 |
| `confirmation_mismatch` | 确认串不匹配 | 从 `slice_plan` / 返回里复制精确串，注意空格和大小写 |
| `adapter_unknown` / `adapter_unavailable` | 适配器未注册 / 环境不可用 | `capabilities` 看可用项；Blender、Orca 路径是否配好 |
| `job_not_found` | job id 不存在 | `print3d job list` 看最近的 id |
| `job_invalid_state` | 任务状态不允许该操作 | 等任务结束，或重新提交 |
| `internal_error` | 未预期异常 | 看 message 和宿主 MCP 日志；用 `print3d cad run` 复现几何问题 |

### 12.1 症状 → 动作

| 症状 | 优先检查 |
| --- | --- |
| 宿主看不到工具 | 命令是否绝对路径、`args` 是否有 `serve --stdio`、宿主是否重启；先手动跑 `print3d tools` |
| `capabilities` 里 CAD 不可用 | `.venv` 是否装好 build123d；`print3d --version` 与 `capabilities.cad` 的 interpreter |
| 脚本静态审查报错 | 第一行是否 `from build123d import *`；是否用了 `_` 属性、`eval`/`open`/`getattr`；是否 import 了白名单外模块 |
| 构建失败但看不到原因 | 构建返回里有 `stdout` / `stderr` / `exit_code`；先在 `/tmp` 用 `print3d cad run` 复现 |
| `watertight: false` / `boundary_edges > 0` | 有开放壳体或未合并实体；检查布尔、`fillet`、导入网格 |
| `solids > 1` | 多个不接触实体；用 `union` 合并，或拆成多个资产 |
| 模型不在热床上 | bbox 是否超出 target bed；零件底面是否在 `z=0` |
| 云端 provider 不 ready | key 是否在环境变量 / `secrets.json`；`capabilities.providers.cloud` 的 source |
| 云端生成尺寸不对 | 检查 `scale`；用 `size_mm` 或 `max_dimension_mm` 自动 fit |
| 本地生成任务失败 | 命令是否可执行、占位符是否写对、是否写出 `<outdir>/mesh.stl`；看 job 的 stderr |
| 切片 target 不 ready | 预设路径是否存在、切片器 CLI 是否可执行；`slice_capabilities` 的 details |
| 切片超时 | 加大 `targets.<id>.timeout_s`；复杂模型先用较低层高/减少支撑试切 |
| 切片器崩溃 | 换上游 OrcaSlicer；厂商定制版可能不支持无头 CLI；profile 扁平化 |
| 打印机连不上 | `printer_status` 看错误；`base_url` 是否正确、机器是否在同一网段、防火墙 |
| `send` 拒绝：not idle | 打印机正在打印/暂停/报错；先处理屏幕上的状态 |
| `send` 拒绝：model mismatch | 打印机 `model` 与 target `printer_model` 必须一致 |
| `send` 拒绝：material mismatch | 检查 `slots` 材质；切片时用 `printer` + `slot`，或修正槽配置 |
| HTTP 401 | `Authorization: Bearer`；token 是否与 `PRINT3D_TOKEN` / `secrets.server.token` 一致 |
| viewer 不更新 | `model_view(asset_id, confirm="VIEW <asset_id>")` 重新生成；确认打开的是资产级 `viewer.html` |
| 脚本报奇怪的 `ModuleNotFoundError` | 从干净目录运行；脚本所在目录会进 `sys.path`，可能遮蔽标准库模块 |

### 12.2 日志和现场

- `<workspace>/jobs/<job_id>/job.json`：任务状态、进度、结果、错误；
- `<workspace>/slices/<slice_id>/`：`plate_1.gcode`、`slice.json`、可选 `statistics.json`；
- `<workspace>/assets/<asset_id>/vNNN/`：`model.py`、`model.stl`、`model.step`、`params.json`、reference；
- `<workspace>/logs/`：服务日志（如果配置了）。

排查时先保留 job/资产目录，不要急着删；它们是可复现的证据。

---

## 13. 命令行速查

| 命令 | 作用 |
| --- | --- |
| `print3d init [--workspace DIR] [--force]` | 创建工作区与默认配置 |
| `print3d capabilities` | 能力报告（JSON） |
| `print3d status` | 系统状态（JSON） |
| `print3d config` | 生效配置（密钥脱敏） |
| `print3d tools` | 已注册 MCP 工具（JSON） |
| `print3d serve [--stdio \| --http] [--host H] [--port P]` | 启动 MCP 服务 |
| `print3d job list` / `print3d job show <job_id>` | 查看任务 |
| `print3d cad run <script.py> [--workdir DIR] [--timeout S] [--python PATH]` | 本地执行 build123d 脚本 |
| `print3d image <prompt> --name NAME [--size 1024x1024] [--out PATH]` | 生成概念图 |
| `print3d preview <asset_id> [--revision v001] [--views iso,front] [--out PATH]` | 渲染四视图 |
| `print3d view <asset_id> [--revision v001] [--open]` | 生成/打开交互式 viewer |
| `print3d version` | 版本 |

所有子命令都接受 `--workspace` 和 `--config`；放在子命令后面，例如：

```bash
print3d preview a_20261114T000000Z_abcdef12 --revision v001 --workspace /tmp/ws
```

---

## 14. 常见任务配方

### 14.1 在已有 STEP 上改一个孔

```text
asset_id = model_write(script_with_INPUT_STEP, name="bracket-v2", confirm="WRITE bracket-v2")
→ model_build(asset_id, input="/path/bracket.step", confirm="BUILD <asset_id>")
→ model_preview(asset_id)
→ asset_export(asset_id, artifact="step", confirm="EXPORT <asset_id>")
```

脚本里用 `import_step(INPUT_STEP)` 读源文件，改完只导出新版本；原 STEP 不动。

### 14.2 图生 3D 但尺寸必须准确

```text
generate_cloud(provider="tripo", image="/path/ref.png", name="shell",
               size_mm=[116, 101, 44], confirm="CLOUD shell")
→ job_status(job_id)
→ printability_check(asset_id, target="u1")
```

`size_mm` 会在下载后自动走同一套 mesh fit，直接落到三轴毫米。没有三轴数据就用 `max_dimension_mm` 等比缩放，再用 `mesh_fit` 微调。

### 14.3 从图片做参考建模

```text
reference_import(path="/path/sketch.jpg", name="lamp-base", confirm="IMPORT lamp-base")
→ 返回 image_path（工作区内相对路径）
→ model_write(script, reference="<image_path>", confirm="WRITE lamp-base")
→ model_build → model_preview
```

宿主模型看图；你负责在 prompt 里补尺寸和背面结构。

### 14.4 先干跑，再上机

```text
printability_check(asset_id, target="u1")
→ orientation_plan(asset_id, target="u1")
→ slice_plan(asset_id, "u1", material="PLA")
→ slice_run(..., confirm="SLICE <asset_id> u1")
→ slice_inspect(slice_id)
→ printer_status("u1")
→ send(slice_id, printer_id="u1", confirm="PRINT u1 <slice_id>", start=false)   # 只上传
→ send(slice_id, printer_id="u1", confirm="PRINT u1 <slice_id>", start=true)    # 确认后启动
```

### 14.5 只切片，不打印

```text
slice_run(...)
→ slice_inspect(slice_id)       # 拿到 slices/<slice_id>/plate_1.gcode
```

G-code 留在工作区，可以自己拷走或让切片器复核。Print3D 不提供任意 G-code 上传，只发送自己切出来的产物。

---

## 15. 升级、备份与卸载

### 15.1 升级

```bash
cd Print3D
git pull
uv sync --extra dev
uv run python -m pytest -q
```

重启宿主，让它重新拉起 `print3d serve --stdio`。如果更新了 `dsh-print3d-mcp/` bundle，也要在 DSH 里重装/重载。

### 15.2 备份

需要备份的是：

- `<workspace>/assets/`：模型、脚本、STEP、reference、viewer；
- `<workspace>/config.json`：targets / printers 配置；
- `<workspace>/secrets.json`：key（单独加密保管，不要进 git）；
- `<workspace>/profiles/`：自定义切片预设。

`cache/` 和 `jobs/` 可以重建或丢弃；`slices/` 里的 G-code 视需要保留。

### 15.3 卸载

```bash
# 源码安装：删掉仓库和虚拟环境
rm -rf /path/to/Print3D

# 工作区（确认不再需要资产后再删）
rm -rf ~/.print3d
```

如果 key 曾进入过共享环境或日志，去 provider 控制台**轮换**，删文件不算处置。

---

## 16. 更多文档

- [README](../README.md) —— 项目概览、功能、快速开始、工具一览
- [Manual (English)](MANUAL.en.md) —— 本手册的英文版
- [DSH bundle](../dsh-print3d-mcp/README.md) —— 把 Print3D 装进 DeepSeek Harness
- [DSH bundle (English)](../dsh-print3d-mcp/README.en.md) —— 英文说明
