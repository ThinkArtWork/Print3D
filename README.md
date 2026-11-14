# 3DPrint

**3DPrint 是一个自托管的 3D 打印 MCP 服务。** 它把「文字/图片 → 参数化建模脚本 → 构建 → 渲染 → 切片 → 发送到打印机」做成一组可组合的 MCP 工具，接入 DSH、Codex、Claude Desktop、Cursor 等任意 MCP 宿主。

> 状态：**M0 + 建模闭环 + 切片 + 发送已可运行**（2026-11-14）。已有 MCP stdio/HTTP 服务、配置与密钥管理、路径沙箱、任务状态机、资产版本库、沙箱化 CAD 执行器、STEP 导入与 SVG/DXF 工程图纸、概念图生成、四视图预览渲染、OrcaSlicer 切片、Moonraker/OctoPrint/Bambu LAN 发送，以及 **29 个工具**；已在 Snapmaker U1 真机上完成切片、上传与真实打印（25 层 / 325 s / 662 mm），含 Blender bpy、本地生成器、云端生成三条额外路径。生成后可在对话流里直接看四视图 PNG，或用 `model_view` 导出**可旋转/缩放的交互式 HTML 查看器**。

## 文档

- [设计文档](docs/DESIGN.md) —— 架构、模块、工具面、数据模型、安全边界
- [开发计划](docs/PLAN.md) —— 里程碑、验收标准、风险

## 快速开始

需要 Python ≥ 3.11（本机验证于 3.14）与 [uv](https://docs.astral.sh/uv/)。

### 安装：GitHub Release（首发渠道）

3DPrint 尚未发布到 PyPI。首发走 GitHub Release：既能直接跑，也能装 wheel。

```bash
# 直接从 git 仓库跑，不安装
uvx --from git+https://github.com/<you>/3DPrint.git 3dprint capabilities

# 或者装进一个隔离环境
pipx install "git+https://github.com/<you>/3DPrint.git"
```

每个 Release 会附 `3dprint-<version>-py3-none-any.whl`，也可以用
`pipx install https://github.com/<you>/3DPrint/releases/download/v0.1.0/3dprint-0.1.0-py3-none-any.whl`。
打包、打标签与发版步骤见 [docs/RELEASE.md](docs/RELEASE.md)。

> 平台：macOS / Linux。切片与 CAD worker 用 `os.killpg` + POSIX `resource` 限额，
> Windows 上不可用。

### 安装：从源码开发

```bash
git clone <this-repo> 3DPrint && cd 3DPrint

# 安装（含开发依赖；build123d 是核心依赖）
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 初始化工作区 ~/.3dprint（生成 config.json 目录骨架，不写密钥）
.venv/bin/3dprint init

# 看看这台机器能做什么：provider 有没有 key、CAD/渲染/切片是否可用
.venv/bin/3dprint capabilities

# 以 MCP stdio 方式启动（宿主拉起的标准姿势）
.venv/bin/3dprint serve --stdio

# 或者本地 HTTP（默认 127.0.0.1:8765；非回环地址强制要求 Bearer token）
.venv/bin/3dprint serve --http --port 8765
```

工作区默认 `~/.3dprint`，可用 `--workspace` 或环境变量 `PRINT3D_WORKSPACE` 覆盖（测试/多实例时建议覆盖）。`3dprint config` 打印生效配置，密钥永远不出现。

把 3DPrint 接到 DSH 有两种方式，推荐第一种（不需要安装任何本地插件包）：

**方式 A：官方 `mcp-manager`（推荐）**。DSH 自带 `@deepseek-ai/dsh-mcp-manager`，在 `Settings → MCP` 里加一条 stdio 服务器即可（命令必须是**绝对路径**，DSH 不会展开 `~`）:

```json
{
  "3dprint": {
    "transport": "stdio",
    "command": "/absolute/path/to/3DPrint/.venv/bin/3dprint",
    "args": ["serve", "--stdio"],
    "env": { "PRINT3D_WORKSPACE": "/absolute/path/to/3DPrint/.3dprint" },
    "enabled": true
  }
}
```

如果本机 DSH 设置里没有 MCP 页（0.2.2-rc.3 的已知情况），直接写 profile 的 `cordis.patch.yml`：`id` 必须是 **`mcp-manager`**（不是 README 里旧的 `mcp`），profile 里是**整表替换**继承来的 servers，改完重启 DSH。

**方式 B：自定义 bundle**。在 **Plugins → Add plugin** 里选择本仓库的 `dsh-3dprint-mcp/` 目录（或让 agent 调 `plugin_manager` 的 `install_bundle`），细节见 [dsh-3dprint-mcp/README.md](dsh-3dprint-mcp/README.md)。

之后在会话里直接调用 `mcp__3dprint__system_status`、`mcp__3dprint__capabilities` 等工具。

### 交互式 3D 查看

**每个资产只有一个查看器文件**：`assets/<asset_id>/viewer.html`。它把该资产**所有已构建版本**的几何一起内嵌，右上角有版本下拉框，默认打开最新一版。同时生成同名硬链接 `<模型名>-viewer.html`（如 `neo-product-116-imagefit-viewer.html`），浏览器标签和文件卡会显示模型名；`viewer.html` 仍保留用于兼容旧链接。所以改到第 10 版，对话里仍然只有一个文件卡，不会堆出一排 `viewer.html`。

- 构建成功时（`model_build`、`blender_run`、`generate_local` / `generate_cloud`）自动重写这个文件并返回 `viewer_file`；宿主执行 `present(viewer_file)` 即可；
- 旧资产或需要手动刷新时调用 `model_view(asset_id, confirm="VIEW <asset_id>")`；传 `revision="v001"` 可让查看器默认打开该版本；
- 命令行：`3dprint view <asset_id> [--revision v001] [--open]`。
- 旧版本目录里遗留的 `vNNN/viewer.html` 会自动改成 526 字节的跳转页，指向资产级查看器；聊天里早先的旧卡片点开也能落到带下拉框的新文件。

几何以 base64 内嵌，单文件、无 CDN、无服务端。打开后：

- 右上角下拉框切换历史版本；
- 左键拖动旋转，右键 / Shift 拖动平移，滚轮缩放，触控板双指捏合；
- 双击复位，`自旋` 开关，`截图` 导出当前版本 PNG，`导出 STL` 把当前版本几何直接下载为二进制 STL（可在切片软件里复核）；
- HUD 显示当前版本的三角面数、包围盒 mm、体积 cm³。

为什么不做侧栏面板：2026-11-14 曾实现并安装 `dsh-3dprint-panel/`（DSH 左侧栏只读面板）。实测后认为价值不足——对话流里已有 `model_preview` 的四视图 PNG，面板只是同一数据的第二视图，却多一个 8765 进程和一套 CORS / 凭据边界。面板已卸载、源码已删除（历史见 [PLAN §17](docs/PLAN.md)）；3D 交互改由这个自包含 HTML 承担，不需要 DSH 客户端插件。

## 两条建模路径

3DPrint **不自己接任何 LLM**。它把建模闭环的原语暴露成 MCP 工具，由**你正在用的宿主模型**驱动；每条路径都以真实几何检查收尾，而不是凭文字想象。

### 路径一：文字 → 模型（直接写脚本）

```text
你 → 宿主模型写 build123d 脚本（改已有零件：脚本里 import_step(INPUT_STEP)）
   → model_write(script, confirm="WRITE <name>")     # 静态审查，不执行
   → model_build(asset_id, input="part.step",        # 可选：把 STEP 挂到本版本
                 confirm="BUILD <asset_id>")          # 沙箱执行，返回真实几何
   → model_preview(asset_id)                          # 四视图 PNG 回给模型
   → model_drawing(asset_id, confirm="DRAWING <id>")  # 需要图纸时：SVG/DXF
   → 改脚本 → model_write 追加 v002 → 再 build → 再 preview
```

`model_build` 返回 `watertight`、`boundary_edges`、`nonmanifold_edges`、`bbox_mm`、`volume_mm3`、`solids`、`is_valid`，并保存 `model.stl` / `model.step`，同时**自动生成 `viewer.html` 并在返回里给出 `viewer_file`**——宿主把它交给 `present`，对话里就直接出现可点击的 3D 预览，不需要再手动调用 `model_view`。`model_preview` 把 STL 渲染成 ISO/FRONT/TOP/RIGHT 四视图，**以图片内容返回**，所以宿主模型能真的「看见」比例、孔位和缺失特征。

`model_build` 也可以吃**别人的 STEP**：`input="part.step"` 会把文件复制成该版本的 `input.step`（记 sha256、原始文件名与字节数），脚本里用模块全局 `INPUT_STEP` 读取——适合「在已有零件上改一个特征」，而不是从零重画，几何精度与源文件一致（实测 `tourbox-neo.step` → bbox 135.885 × 111.714 × 32.5 mm，218.866 cm³）。需要给同事或供应商一份传统图纸时，`model_drawing` 把 `model.step` 投影成**第三角四视图合图**（front/top/right/iso，正交视图带虚线隐藏线、等轴测按惯例不画隐藏线、视图标签、1:1 真实毫米），写成 `drawing.svg` / `drawing.dxf`，再用 `asset_export(artifact="svg")` 或 `("dxf")` 取走；图纸同样随版本保存，`model_show` 会列出来。

### 路径二：文字/图片 → 概念图 → 模型（视觉参考）

```text
image_generate(prompt, confirm="IMAGE <name>")        # OpenAI 兼容 images API
reference_import(path, confirm="IMPORT <name>")       # 或导入用户上传的草图/照片
   → model_write(script, reference=<image_path>, confirm="WRITE <name>")
   → model_build → model_preview                       # 预览会同时回概念图和渲染图
   → present(viewer_file)                              # 可旋转/缩放的 3D 预览
```

概念图（reference）会复制到该版本的 `reference.<ext>`，`params.json` 记录来源、提示词与 sha256；`model_preview` 把**概念图和渲染图并排返回**，模型据此对比「要的」和「做出来的」再改。

### 多轮对话

同一资产的每次修改都是一次 `model_write`（带 `asset_id` 追加 `v002`、`v003`…），`prompt` 记录这一轮的要求；`model_show` 读回任意版本的脚本、参数和构建记录。这样「这一轮改哪里、哪些保留」有据可查，也方便回滚。

#### 查看历史版本

```text
asset_list()                                   # 每个资产带 latest_revision
   → model_show(asset_id)                      # revisions[]：prompt / model / bbox / volume / 是否构建 / viewer
   → model_preview(asset_id, revision="v001")  # 该版本的四视图 PNG（构建过才有）
   → model_view(asset_id, revision="v001",
                confirm="VIEW <asset_id>")     # 该版本的交互式 viewer.html
```

`model_show` 的每个 revision 里现在会带 `prompt`、`model`、`bbox_mm`、`volume_mm3`、`built` 和 `viewer`（已生成时给出路径）。旧版本若还没生成 viewer，用 `model_view(..., revision=...)` 补生成即可。

命令行同样支持：

```bash
3dprint view <asset_id> --revision v001 --open
3dprint preview <asset_id> --revision v001 --out /tmp/v001.png
```

在对话里直接说「看一下 v001」即可，宿主模型会调用 `model_show` 列出历史，再对该版本 `model_preview` / `model_view` 并把文件 `present` 出来。

### 图片理解

3DPrint 不内置视觉模型：上传的草图/照片由宿主模型（多模态）自己看，3DPrint 负责把图片存成 reference、附到版本上、并在预览时回传。参考图看不到的背面、内部结构和精确尺寸，需要宿主模型在 `prompt` 里补充或用文字提问。

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

### 路径三点五：mesh_fit（图生网格 → 真实尺寸成品）

图生 3D 返回的是“形状”，不是可交付的尺寸。真实产品的长宽高应查厂商规格；`mesh_fit` 把已经生成的 STL/GLB/OBJ 作为形状真相，一次完成清理、缩放、居中落地、减面和导出，不重新凭照片摆控件。

```text
mesh_fit(
  asset_id="a_...",                  # 或 source="/path/model.stl"
  size_mm=[116, 101, 44],            # 已知真实外形：三轴精确校准
  name="neo-product-116",
  max_triangles=145000,
  image="/path/neo.jpg",             # 可选：把参考图附到版本
  confirm="FIT neo-product-116",
)
```

只要求“最长边”而不知道三轴尺寸时，改用等比模式（不会拉歪比例）：

```text
mesh_fit(
  source="/path/model.stl",
  max_dimension_mm=120,              # 与 size_mm 二选一
  name="product-display",
  confirm="FIT product-display",
)
```

- 输入二选一：`source=` 是 STL/GLB/GLTF/OBJ 路径；`asset_id=` 读取该资产最新版（或 `revision=` 指定版）的 `model.stl`。
- 尺寸二选一：`size_mm=[x,y,z]` 做三轴精确缩放；`max_dimension_mm=<最长边>` 按最长边等比缩放，保持图片比例。两者都省略会报参数错误。
- 内部在 Blender 中合并重复点、补边界洞、重算法线，缩放后 XY 居中、底面落在 `z=0`，再 decimate 到 `max_triangles`，最后三角化并再清理一次。
- 返回与 `blender_run` 相同的 geometry checks、STL/GLB 和带模型名的 viewer；`max_triangles` 默认 145,000，是为了让 `model_preview` 完整渲染（超过 150,000 会被抽样成线框），不是几何上限。
- 这是这次 TourBox Neo 正确模型的固化路径：混元网格保留轮廓，官方 116×101×44 mm 校准尺寸，145k 面导出，得到 watertight 成品。

### 路径四：本地生成器（通用命令适配器）

不下载模型权重，也不绑定某个框架：任何能读提示词/图片、写出 STL 的本地命令都能接进来。

```json
// ~/.3dprint/config.json —— providers.local.<id>
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
   → 命令在 clean env 中运行，cwd=<outdir>，可读 PRINT3D_LOCAL_OUT
   → 必须写出 <outdir>/mesh.stl（或 stdout 打印 {"mesh": "/path/model.stl"}）
   → 3DPrint 建资产（kind=local）、算 geometry checks、附 reference
   → job_status 轮询 → model_preview(asset_id)
```

占位符：`{prompt}` `{image}` `{outdir}` `{name}` `{mode}`；`command`/`args` 可以是列表或 shell 字符串（字符串按 `shlex` 切分，不经过 shell）。`capabilities.providers.local` 会显示命令是否存在、是否启用以及 `mode`。

### 路径五：云端生成（Tripo / 混元 3D）

云 provider 是异步任务：提交 → 轮询 → 下载 → 转 STL → 建资产 → 预览。不需要本机能跑模型。

```json
// ~/.3dprint/config.json —— providers.cloud.<id>
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
        "adapter": "hunyuan_openai",
        "base_url": "https://api.ai3d.cloud.tencent.com",
        "model": "3.0",
        "api_key_env": "HUNYUAN3D_API_KEY",
        "scale": 1000,
        "face_count": 200000,
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
   → 可选 size_mm=[x,y,z] 或 max_dimension_mm=<最长边>：自动执行同一套 mesh fit
   → 存 model.stl（+ 原始 source.stl/model.glb/obj），geometry checks，资产 kind=cloud
   → model_preview(asset_id)
```

- 凭据：Tripo 用 `TRIPO_API_KEY`（Bearer）。混元默认走 OpenAI 兼容模式（`adapter: hunyuan_openai`），`Authorization` 直接放 API Key，环境变量 `HUNYUAN3D_API_KEY`；接口为 `POST /v1/ai3d/submit` 与 `POST /v1/ai3d/query`，请求提交 `ImageBase64`/`Prompt`，查询按 `JobId` 返回 `Status` 与 `ResultFile3Ds`。旧的腾讯云 TC3 模式仍保留：把 `adapter` 改为 `hunyuan` 并配置 `TENCENTCLOUD_SECRET_ID`/`TENCENTCLOUD_SECRET_KEY` 即可。`capabilities.providers.cloud` 只显示是否配置，不返回密钥。
- 尺度：云端网格不带真实物理尺寸。Tripo GLB 按米导出，默认 `scale: 1000`；混元接口返回的 STL 也可能被厂商归一化（实测 bbox 远小于目标），默认同样用 `1000` 只是与 Tripo 对齐。请按生成后的 bbox 调整 `scale`，不要把它当成设计尺寸。
- 计费：每次生成消耗 provider 额度；Tripo 的 `credits_consumed`、混元的 `ResultCreditConsumed` 会写进资产 `usage`。
- 失败：provider 的 `error_message` 与 `task_id` 进入 job 错误；轮询超时由 `timeout_s`（默认 600 s）控制。

### 路径六：切片（OrcaSlicer CLI）

切片器是外部进程（GPL，只调用不链接），profile 从用户已安装的切片器读取，3DPrint 不分发。

```json
// ~/.3dprint/config.json —— targets.<name>
{
  "targets": {
    "a1": {
      "printer_model": "Bambu Lab A1",
      "slicer": "orca",
      "slicer_path": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
      "machine": "/Users/me/Library/Application Support/OrcaSlicer/system/BBL/machine/Bambu Lab A1 0.4 nozzle.json",
      "process": "/Users/me/Library/Application Support/OrcaSlicer/system/BBL/process/0.20mm Standard @BBL A1.json",
      "filament": ["/Users/me/Library/Application Support/OrcaSlicer/system/BBL/filament/Bambu PLA Basic @BBL A1.json"],
      "overrides": {"layer_height": "0.16"},
      "timeout_s": 900
    }
  }
}
```

```text
slice_capabilities()                     → 每个 target 是否 ready、slicer 版本、bed
slice_plan(asset_id, "a1")               → presets / command / geometry checks / confirm 串（只读）
slice_run(asset_id, "a1", confirm="SLICE <asset_id> a1")
   → 后台 job：外部 OrcaSlicer → plate_1.gcode + statistics.json
slice_inspect(slice_id)                  → 层数、预计时间、耗材、printer_model、warnings
```

切片时可以直接告诉 3DPrint 工艺要求，系统把它们写进本次切片的 process/filament 私有副本：

```text
slice_plan(asset_id, "a1", material="PETG", layer_height=0.24,
           infill="gyroid", infill_density=30, strength="strong")
slice_run(asset_id, "a1", confirm="SLICE <asset_id> a1",
          material="PETG", layer_height=0.24, infill="gyroid",
          infill_density=30, strength="strong")
```

| 参数 | 说明 |
| --- | --- |
| `material` | PLA / PLA+ / PETG / ABS / ASA / TPU / PC / PA / NYLON；自动匹配喷嘴与热床温度（PLA 210/60、PETG 240/80、ABS 260/100、TPU 225/45…） |
| `layer_height` | 层高 mm，0.05–1.0 |
| `infill` | grid / gyroid / honeycomb / cubic / concentric / triangle / line / rectilinear / zigzag / crosshatch / lightning / adaptivecubic / supportcubic |
| `infill_density` | 填充密度 %，0–100；`-1` 表示沿用预设 |
| `strength` | `light` / `normal` / `strong`，映射墙数 2/3/4、顶/底层 3/4/5；也可单独传 `walls`、`top_shells`、`bottom_shells` |
| `bed_temp` / `nozzle_temp` | 显式覆盖材料默认温度 |
| `build_plate` | 打印板类型：`textured_pei`（默认纹理 PEI）/ `smooth_pei`（光面 PEI，Orca 里叫 High Temp Plate）/ `cool_plate` / `engineering_plate` / `supertack` / `textured_cool`；选择后 Orca 用对应的热床温度键，温度按材料自动调整 |
| `flow_ratio` | 挤出流量倍率 0.5–1.5，写入 `filament_flow_ratio`；预设 Generic PLA 是 0.98，Matte/高速料可试 1.00–1.05 |
| `pressure_advance` | 压力提前 K 值 0–0.2，写入 `pressure_advance` 并启用；可先用 `printer_status` 读取打印机已校准的值（U1 的 `flow_calibration.factors.extruder`） |
| `line_width` | 全局线宽 mm（0.2–1.0），写入 `line_width`；不改则沿用预设 |
| `outer_wall_line_width` / `inner_wall_line_width` | 外/内墙线宽 mm；外墙偏宽会让转角鼓包，0.4 喷嘴可先试 0.40–0.42 |
| `top_surface_line_width` | 顶面线宽 mm；顶面不平整时先把它降到 0.40 左右，再配合流量微调 |
| `arc_fitting` | 是否输出 G2/G3 圆弧（默认 false）；Klipper 固件支持时开启，圆孔更圆、文件更小 |
| `fan_speed` | 模型冷却风扇上限 0–100%；写入 `fan_max_speed`/`fan_min_speed`。U1 预设默认 100%，小件 PLA 可先降到 60 |
| `aux_fan_speed` | 辅助/腔体风扇 0–100%，写入 `additional_cooling_fan_speed`；U1 预设默认 70%，可与 `fan_speed` 一起降 |
| `fan_first_layers` | 前几层关闭模型风扇（默认 1）；提高可改善首层附着，但小件首层会更软 |
| `printer` + `slot` | 绑定某台打印机的耗材槽（多喷头按工具头、单喷头 AMS 按槽位）：自动取该槽的材质/颜色，材质冲突直接拒绝；`slot=1` 表示 G-code 用 `T1` |

- 流量：`flow_ratio` 直接改 `filament_flow_ratio`；`pressure_advance` 改 `pressure_advance` 并置 `enable_pressure_advance=1`。`printer_status` 会尽量读取打印机已存的校准系数（Snapmaker 的 `flow_calibrator.json`，按工具头给出 K 值），可把它作为 `pressure_advance` 传入。
- 实测调参（U1，PLA Matte，0.2 mm，30×20×6 小板）：`flow_ratio=1.03` 时外墙出现挤出鼓包、顶面不平，孔壁/R3 圆角正常；把流量降到 `1.00`、外墙/顶面线宽从 0.42 降到 0.40，并开启 `arc_fitting`，作为下一轮验证组合。线宽和流量要一起看：有效挤出量 ≈ 流量 × 线宽。
- 冷却：U1 的 Generic PLA 预设把模型风扇设成 100%、辅助风扇 70%，从第 1 层后全开。30×20×3 mm 小板的实测外壁中段有轻微内凹，优先怀疑冷却过强/两侧不均：先试 `fan_speed=60`、`aux_fan_speed=30`，保留 `overhang_fan_speed=100` 处理孔顶和悬垂；若仍内凹，再检查外壁线宽/PA 与机械（皮带张力、Z 轴）。
- 打印板温度：`textured_pei` / `smooth_pei` 按材料温度；`cool_plate`、`textured_cool` 各降 15 °C，`engineering_plate` 升 10 °C，`supertack` 降 5 °C。对应到 Orca 的 `curr_bed_type` 和 `*_plate_temp` 键，不再把所有板型的温度键写成同一个值。
- `overrides` 与上述参数通过复制 vendor 预设到私有 datadir、改写 process/filament JSON 实现；用户安装的 profile 不动。独立扁平 profile（如 U1）则复制到本次切片工作目录的 `profiles/` 后改写。
- `slice_plan` 只预览 `profile_overrides`，不写文件；`slice_run` 的 `slice_inspect` 记录里保留本次 `options`。
- G-code 头与 `*.statistics.json` 双源读取；与目标 `printer_model` 不符、超出 bed 都会写 warning。
- 实测（OrcaSlicer 2.5.0 + Bambu A1，0.16 mm）：20×20×10 带孔方块 → 62 层、16m01s、2.31 g、332 KB G-code。
- Snapmaker U1：厂商 Snapmaker Orca 01.10.01.50 无头加载 U1 预设即段错误，因此 U1 target 改用上游 OrcaSlicer 2.5.0 + 扁平化并清理过条件表达式的 U1 profile（见 `targets.u1`）；真机已完成 25 层小件打印。

### 路径七：发送到打印机（Moonraker / OctoPrint / Bambu LAN / PrusaLink / 自定义 HTTP）

切片完成后，3DPrint 通过打印机自己的 HTTP API 上传并启动，不经过厂商云。

```json
// config.json —— printers 是数组
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

```text
printer_list()                                      → 配置了哪些打印机、是否有 key（不联网）
printer_status("u1")                                → 实时状态 / 文件名 / 进度 / 温度
printer_save("u1", confirm="SAVE PRINTER u1", ...)  → 新增或修改（按 id upsert）
printer_remove("u1", confirm="REMOVE PRINTER u1")
send(slice_id, "u1", confirm="PRINT u1 <slice_id>", flow_calibrate=True)
                                                     → 上传 G-code 并启动；先让打印机自校准流量
printer_cancel("u1", confirm="CANCEL PRINT u1")
```

- 适配器：Moonraker（Klipper，`/server/files/upload` + `/printer/print/start`）、OctoPrint（`/api/files/local` + `/api/job`）、Bambu LAN（MQTT/TLS `8883` + 隐式 FTPS `990` + 3MF 封装，社区协议实现，**尚未真机验证**）、PrusaLink（HTTP Digest + `/api/v1/files/...`；v1 不可用时回退旧版 `/api/printer`）以及声明式 `http`（端点、字段路径、状态名全部写在 `config.json`，适配未知打印机不用改代码）。厂商云未接入。
- PrusaLink 用的是打印机屏幕上显示的 **password**，不是 API key；`username` 默认 `maker`，密码放 secrets 的 `providers.<id>` 或 `password_env`。
- 自定义 `http` 示例（`printer_save(adapter="http", http={...})` 或直接写 config.json）：

```json
{
  "id": "vendorp",
  "adapter": "http",
  "base_url": "http://192.168.1.60",
  "enabled": true,
  "http": {
    "auth": {"type": "header", "header": "X-Api-Key"},
    "status": {
      "path": "/api/state",
      "map": {"state": "state", "progress": "job.percent", "filename": "job.file",
              "bed_temp": "temps.bed", "nozzle_temp": "temps.nozzle"},
      "states": {"IDLE": "idle", "RUN": "printing", "PAUSE": "paused"}
    },
    "upload": {"path": "/api/files/{filename}", "method": "PUT"},
    "start": {"path": "/api/files/{filename}/start", "method": "POST", "body": {"start": true}},
    "cancel": {"path": "/api/job/stop", "method": "POST"}
  }
}
```
- 工具头与耗材：`toolheads` 支持 1–9（拓竹 X3D 类机型最多 9 个）。`slots` 按 G-code 工具顺序描述每个槽的 `material` / `color`（`#RRGGBB`）/ `name`；多喷头按工具头编号，单喷头 AMS（如拓竹 AMS）按槽位编号。
- 切片时传 `printer="u1", slot=1`：自动取 1 号槽的材质，生成 `T1`，并把该槽材质写进 slice 记录；材质与请求冲突会直接报错。`send` 再按 G-code 实际用到的 `Tn` 逐槽核对材质，不匹配就拒绝上传；没有 `slots` 时给出“无法核对材质”的 warning，但仍会检查 `toolheads` 上限。
- 打印板：`printer_save(build_plate="pei")` 存到打印机配置，之后切片不传 `build_plate` 也会自动使用该板型；也可每次切片单独覆盖。U1 固件（Klipper `print_task_config` / `_DETECT_PLATE`）只报告耗材和探测偏移，**不上报板型**，所以首次需要用户选一次；Snapmaker U1 默认纹理 PEI。
- 真机（2026-11-14）：Snapmaker U1，Moonraker 2.1.0，`http://192.168.1.50:7125`，4 × 0.4 mm 工具头；`printer_save` 接入后 `printer_status` 为 `standby`；OrcaSlicer 2.5.0 切片 25 层 / 311 s / 1.99 g，`send(start=false)` 上传 277,876 B 成功；随后 `send(start=true)` 真实打印完成：`complete`，25/25 层，325 s，662 mm 耗材。
- 已知限制：Snapmaker Orca 2.4.1 的 CLI 加载 U1 预设会段错误（GUI 不受影响）；当前用 upstream OrcaSlicer 2.5.0 + 扁平化 U1 预设（去掉 `inherits`，并把 `{if chamber_cooling_mode}` 这类厂商占位符展开成固定分支）绕开。预设放在工作区 `profiles/u1/`，目标配置指向它们。
- 发送前检查：slice 必须成功、G-code 存在、打印机 idle、`printer_model` 与切片目标一致，任一不满足直接拒绝。
- 自动流量校准：`send(..., flow_calibrate=True)` 会在启动前执行打印机配置里的 `flow_calibration.enable_gcode`。Snapmaker U1 配的是 `SET_PRINT_PREFERENCES FLOW_CALIBRATE=1`，打印机随后在 `PRINT_START` 执行 `SM_PRINT_FLOW_CALIBRATE`；通用 Klipper 可换成自己的宏（如 `FLOW_CALIBRATE ENABLE=1`）。该命令只能在**空闲**状态设置，打印中会被固件拒绝；未配置或适配器不支持 G-code 命令时会给出明确错误，不会静默跳过。

#### 多厂家 / 多型号适配

切片参数（`flow_ratio`、`pressure_advance`、`build_plate`、`slots` 匹配）本身与打印机品牌无关，都是写进 Orca 预设的通用键；真正与厂商相关的是“能不能发校准命令、能不能读校准系数、能不能自动识别板型”。这三件事由打印机的 `capabilities` 配置决定，而不是写死在适配器里：

```json
{
  "id": "voron",
  "adapter": "moonraker",
  "base_url": "http://voron.local:7125",
  "model": "Voron 2.4",
  "build_plate": "textured_pei",
  "build_plates": ["textured_pei", "cool_plate"],
  "plate_keys": ["bed_type"],
  "flow_calibration": {
    "enable_gcode": "FLOW_CALIBRATE ENABLE=1",
    "disable_gcode": "FLOW_CALIBRATE ENABLE=0",
    "factors_url": "/server/files/config/calibration.json",
    "factors_key": "pa.factor",
    "extruder_keys": ["extruder", "extruder1"],
    "trim_unused_tools": true,
    "tool_macros": ["PRINT_EXTRUDER_PREHEAT", "PRINT_AUTO_FEED", "PRINT_FLOW_CALIBRATE"]
  }
}
```

`trim_unused_tools` + `tool_macros` 用于多头机器：切片完成后，3DPrint 扫描起始 G-code，把 `EXTRUDER=n` 且 n 不在本次使用工具头里的预热/进料/校准行删掉。例如 U1 只用到 T0 时，G-code 里只保留 T0 的预热/进料/校准行。注意：U1 的启动总时长主要花在床网、喷嘴清洁和预热上；实测裁剪前后总启动都约 5.5 分钟，因此这项优化在 U1 上收益有限，但在“每个头都要实际进料校准”的机器上会明显。它是可选的文本级后处理，只匹配配置里列出的宏名，不会改动模型路径或挤出段；不配置则保持厂商原始行为。

多色语义：校准按**物理工具头**去重。
- 多工具头机器（U1，`toolheads` > 1）：打印用到 T0/T1/T2 三种颜色，就分别校准这 3 个物理头。
- 单喷嘴多色（AMS/换料，`toolheads: 1`）：无论用 1 种还是 3 种颜色，只校准该喷嘴一次；即使切片 G-code 里出现 T0/T1/T2，也不会对同一物理头重复校准。`slots` 里的 `toolhead` 字段可显式声明“哪个槽位属于哪个物理头”，不填时按单头自动归并。
- 这不是 3DPrint 自创的规则：Orca/Bambu 在单色打印启动时校准，在多色打印中改用每种耗材已保存的 K 值，在换料时切换（[OrcaSlicer 文案](https://github.com/OrcaSlicer/OrcaSlicer/blob/3b58217b47fdbea46db7abc2c6b16c3b3f4364a2/localization/i18n/en/OrcaSlicer_en.po#27)、[Bambu 流量动力学校准](https://www.orcaslicer.com/wiki/calibration/adaptive_pressure_advance_calib)）。因此同一喷嘴按颜色重复跑启动校准既慢又没有额外收益。

| 适配器 / 机型 | 切片参数 | 槽位匹配 | 板型选择 | 自动读板型 | 校准命令 | 读校准系数 |
| --- | --- | --- | --- | --- | --- | --- |
| Moonraker / Klipper（含 U1） | ✅ | ✅（配置 `slots`） | ✅（`build_plates` 限制） | 视固件字段（`plate_keys`）；U1 无 | ✅ 任意 G-code（`enable_gcode`） | ✅ 任意 JSON（`factors_url`） |
| OctoPrint / Klipper | ✅ | ✅ | ✅ | 视固件字段 | ✅ 转 `/api/printer/command` | 需自备 HTTP 可读的 JSON |
| Bambu LAN | ✅ | ✅（按工具头） | ✅（Orca 预设） | 未实现 | 暂未实现（Bambu 自有校准流程不在 LAN API 中） | 暂未实现 |
| PrusaLink | ✅ | ✅ | ✅ | 未实现 | ❌（无公开 G-code 端点） | ❌ |
| 声明式 HTTP | ✅ | ✅ | ✅ | 未实现 | 需适配器扩展 | 需适配器扩展 |

- `printer_status` 的 `capabilities` 会直接报告 `flow_calibration.supported` / `factors_configured` / `trim_unused_tools`、`build_plates`、`build_plate_detection`；`printer_list` 报告配置层面的 `flow_calibration_configured`，不需要联网。
- 按需校准是“起始宏裁剪”，不改变固件里的 `flow_calib_extruders` 全局开关；如果厂商宏名字不同，把实际宏名填进 `tool_macros` 即可，未列出的宏不会被碰。
- `build_plates` 为空表示接受切片器认识的所有板型；非空时 `slice_plan`/`slice_run` 会拒绝不在列表里的板型，并列出支持的取值。
- 板型 id 统一用 3DPrint 的 `textured_pei` / `smooth_pei` / `cool_plate` / `engineering_plate` / `supertack` / `textured_cool`，由切片适配器映射到 Orca 的 `curr_bed_type`；换切片器时只需在切片适配器里换映射，不改工具参数。
- 自动读板型依赖固件暴露字段；目前只有厂商返回 `build_plate` / `bed_type` 等 JSON 字段时才会生效，U1 不返回，所以仍需首次手选。
- 打印是不可逆的物理动作：`send` 默认上传后立即开始，必须精确确认；不做自动重试。
- 实测（Moonraker 本地 stub + 真实 Orca 切片）：20 mm 立方体 → 359,515 B G-code → 上传 → 启动 → 状态 `printing` → `printer_cancel` → `cancelled`；PrusaLink 与声明式 `http` 由单元测试覆盖（Digest 重试、上传头、启动/取消路径、状态映射），尚无对应真机。

## 打印前检查与摆放建议

两个只读工具，不需要 `confirm`，也不修改资产；用来在切片前回答“这个件好不好打、该怎么摆”：

- `printability_check(asset_id, target="", revision="")`：包围盒 / 体积、>45° 悬垂面积与占比、支撑体积估计、热床接触面积、薄壁估计（射线法，5% 分位最小值与 <1.2 mm 面积占比）、是否超出目标热床，给出 0–100 分和可执行的 issue/hint。
- `orientation_plan(asset_id, target="", revision="", limit=5)`：枚举 24 个轴向摆放（6 个朝下轴 × 4 个平面旋转），按悬垂、支撑估计、高度、热床接触计算代价并排序，返回推荐摆放的 3×3 旋转矩阵、`down_axis`、`spin_deg` 和每个候选的指标；当前摆放也会给出分数，便于比较。

```text
printability_check(asset_id="a_2026...", target="u1")
orientation_plan(asset_id="a_2026...", target="u1", limit=3)
```

评分是几何启发式，不是 FEM，也不替代真实切片：薄壁由射线采样估计，支撑体积按“悬垂面积 × 高度 × 0.25”估算；最终支撑量仍以切片器为准。典型用法是 `model_build` → `printability_check` → 有问题就 `orientation_plan` → 改脚本/重摆 → `slice_plan`。

## 工具

| 工具 | 写操作 | 说明 |
| --- | --- | --- |
| `capabilities` | 否 | provider / 图像 provider / CAD / 渲染 / 切片 / 打印机能力，永不返回密钥 |
| `system_status` | 否 | 版本、Python、工作区、任务计数；固定 `writes_performed: false` |
| `job_status` | 否 | 按 `job_id` 查询任务；未知 id 返回 `job_not_found` |
| `asset_list` | 否 | 列出资产与版本数 |
| `model_show` | 否 | 读回资产清单、指定版本源码、`params.json` 与 reference |
| `blender_run` | 是 | 用 Blender 后台模式执行 bpy 脚本，导出 STL/GLB 并检查；`confirm` 精确为 `BLENDER <name>` / `BLENDER <asset_id>` |
| `mesh_fit` | 是 | 把已有 STL/GLB/OBJ（或资产版本）按真实 `size_mm` 或 `max_dimension_mm` 校准，清理、居中落地、减面、导出 STL/GLB 和命名 viewer；`confirm` 精确为 `FIT <name>` |
| `generate_local` | 是 | 后台运行 `providers.local.<id>` 命令生成 STL，建资产并返回 `job_id`；`confirm` 精确为 `LOCAL <name>` |
| `generate_cloud` | 是 | 后台提交 Tripo/混元 3D 任务，轮询后下载并转 STL 建资产；可传 `size_mm`/`max_dimension_mm` 自动校准后建资产；`confirm` 精确为 `CLOUD <name>` |
| `image_generate` | 是 | 文生概念图并存入 `references/`；`confirm` 精确为 `IMAGE <name>`，图片内联返回 |
| `reference_import` | 是 | 把用户已有的草图/照片导入为 reference；`confirm` 精确为 `IMPORT <name>` |
| `model_write` | 是 | 存入 build123d 脚本（新建或追加版本，可带 reference）；`confirm` 精确为 `WRITE <name>` / `WRITE <asset_id>` |
| `model_build` | 是 | 沙箱执行某版本；`input=<step>` 时把 STEP 复制成版本内 `input.step` 并暴露为脚本全局 `INPUT_STEP`；返回 geometry checks，存 STL/STEP 并自动生成 `viewer.html`；`confirm` 精确为 `BUILD <asset_id>` |
| `model_drawing` | 是 | 把已构建的 `model.step` 投影成第三角四视图 SVG/DXF 图纸（虚线隐藏线、视图标签、1:1 毫米），存 `drawing.svg`/`drawing.dxf`；`confirm` 精确为 `DRAWING <asset_id>` |
| `model_preview` | 否* | 四视图 PNG（含概念图对比）内联返回；只写 `cache/previews/`，不改资产 |
| `model_view` | 是 | 把资产的全部已构建版本合并成一个交互式 3D 查看器（下拉切换版本，WebGL 旋转/缩放/平移/自旋/截图）；`confirm` 精确为 `VIEW <asset_id>` |
| `printability_check` | 否 | 只读几何可打印性报告：悬垂、支撑估计、薄壁、热床接触/尺寸、0–100 分与 issues |
| `orientation_plan` | 否 | 只读摆放排序：24 个轴向旋转的悬垂/支撑/高度/接触代价，返回推荐旋转与候选指标 |
| `asset_export` | 是 | 把 STL/STEP/脚本/SVG/DXF 图纸复制到用户可打开的位置；`confirm` 精确为 `EXPORT <asset_id>` |
| `slice_capabilities` | 否 | 列出 targets、slicer 版本、预设是否解析、bed |
| `slice_plan` | 否 | 切片 dry-run：presets、命令、checks、确认串；不写文件 |
| `slice_run` | 是 | 外部 OrcaSlicer 后台切片，G-code/统计存 `slices/<slice_id>/`；`confirm` 精确为 `SLICE <asset_id> <target>` |
| `slice_inspect` | 否 | 读切片记录、G-code 元数据与 warnings |
| `printer_list` | 否 | 配置的打印机、适配器、是否有 API key；不联网 |
| `printer_status` | 否 | 经适配器查询实时状态、文件名、进度、温度 |
| `printer_save` | 是 | 新增/修改打印机（config.json）；`confirm` 精确为 `SAVE PRINTER <id>`。`bambu_lan` 需 `serial` 与 `access_code_env` |
| `printer_remove` | 是 | 删除打印机；`confirm` 精确为 `REMOVE PRINTER <id>` |
| `send` | 是 | 上传切片 G-code 并启动打印（异步 job）；`confirm` 精确为 `PRINT <printer_id> <slice_id>` |
| `printer_cancel` | 是 | 取消当前打印；`confirm` 精确为 `CANCEL PRINT <printer_id>` |

错误统一为 `{"ok": false, "error": {"code", "message", "hint", "details"}}`。`hint` 是给人和 agent 的下一步提示，`details` 里可能有被拒绝的行号或缺失的产物名。

## 安全边界

1. **只读工具免费**：`capabilities`、`system_status`、`job_status`、`asset_list`、`model_show`、`printability_check`、`orientation_plan` 不需要确认，且明确返回 `writes_performed: false`；`model_preview` 只写预览缓存。
2. **写操作必须精确确认**：确认串逐字符比较，错了返回 `confirmation_mismatch` 且不写任何文件。
3. **路径沙箱**：除 `reference_import`（只读用户给的图片路径）外，所有用户给的路径经 `safe_path()` 解析符号链接后校验，逃出工作区一律 `path_outside_workspace`；`asset_export` 的 `out_path` 同样限制在工作区内，不能借导出覆盖 `~/.ssh/authorized_keys` 之类的文件。
4. **脚本策略 + 执行沙箱**：build123d 脚本先过 AST 审查，**导入是白名单**（`build123d`、`math`），`_` 开头的属性访问一律拒绝（封 `().__class__.__base__.__subclasses__()` 逃逸），`eval/exec/open/getattr/__builtins__` 等动态入口直接报错；再在 `cad_worker.py` 子进程里执行：RLIMIT_CPU/FSIZE/NOFILE、独立进程组、超时 SIGKILL。子进程拿到的是**环境变量白名单**（PATH/HOME/TMPDIR/语言/动态库/fontconfig），provider 密钥无论叫什么名字都进不去。bpy 脚本同样先过静态审查（`bpy/bmesh/mathutils/math/sys/...` 白名单，`sys.modules` 等逃逸路径拒绝），再由 `blender -b --factory-startup` 子进程执行。`generate_local` 运行的是用户自己配置的命令，环境变量同样白名单过滤并带超时；这是「用户信任本机命令」的显式边界。静态审查是纵深防御，不是沙箱；真正的隔离边界是子进程 + 资源限额。
5. **出站下载有上限**：云端模型下载只接受 http(s)、最多 3 次重定向、边下边计数（默认 150 MiB 上限），概念图 URL 下载 12 MiB 上限；HTTP 模式用 `hmac.compare_digest` 比较 Bearer token。
6. **不开放任意 G-code**：切片产物先检查再发送，`printer_*` 只接受切片器产物。
7. **密钥**：只从环境变量或 `~/.3dprint/secrets.json`（0600）读取；日志与工具输出统一脱敏。
8. **打印前检查**：`send` 要求切片成功、G-code 存在、打印机 idle 且型号匹配；上传后立即启动是默认行为，没有自动重试，取消走 `printer_cancel`。

## 开发

```bash
.venv/bin/python -m pytest -q          # 单元 + 内存 MCP + respx 模拟 HTTP
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
```

测试覆盖：配置优先级与 BOM、路径逃逸与符号链接、任务状态机非法迁移、脚本策略、资产版本 v001→v002 与 reference、CAD 子进程（真实 build123d 立方体：watertight/bbox/volume、超时杀进程）、概念图生成（respx 模拟，含 401/429/超时/base64/URL 下载）、四视图渲染（二进制/ASCII STL、像素级校验）、交互式查看器（几何内嵌、标题转义、面数上限）、bpy 策略与 Blender 执行（假 Blender 覆盖成功/失败/超时/无导出；真机立方体）、本地生成器（命令模板、STL 发现、超时、环境变量）、后台任务执行器、`model_write`/`model_build`/`model_preview`/`model_view`/`asset_export`/`reference_import`/`blender_run`/`mesh_fit`/`generate_local`/`generate_cloud`、打印前检查（薄壁/悬垂/支撑估计/热床/24 向摆放排序）、MCP stdio 握手与 HTTP Bearer 鉴权、打印机适配器（respx 模拟 Moonraker/OctoPrint：状态映射、上传、启动、取消、401、不可达）与 `printer_*`/`send` 工具（忙碌拒绝、型号不匹配、确认串、异步上传启动）。

## 目录

```
3DPrint/
  pyproject.toml
  src/print3d/
    server.py          # MCP 入口（stdio / streamable HTTP + Bearer 鉴权），只负责装配
    runtime.py         # Runtime（workspace/config/registry/assets）+ 工具错误信封
    instructions.py    # MCP instructions 文本
    tools/             # 29 个工具，按领域分模块注册
      system.py        #   capabilities / system_status
      images.py        #   reference_import / image_generate
      models.py        #   model_write / model_build / model_drawing / model_preview / model_view
      generating.py    #   blender_run / mesh_fit / generate_cloud / generate_local
      analysis.py      #   printability_check / orientation_plan
      slicing.py       #   slice_capabilities / slice_plan / slice_run / slice_inspect
      printers.py      #   printer_list / printer_status / printer_save / printer_remove / send / printer_cancel
      catalog.py       #   asset_list / asset_export / model_show / job_status
      common.py        #   共享的切片参数/错误/查看器助手
    cli.py             # 3dprint init|capabilities|status|config|tools|serve|job|cad|view|image|preview
    config.py          # config.json / secrets / 环境变量优先级
    workspace.py       # ~/.3dprint 布局 + safe_path 沙箱
    jobs.py            # 任务状态机与 job.json
    assets.py          # 资产 manifest + vNNN 版本 + reference + STEP input/output
    images.py          # 概念图生成（OpenAI 兼容）+ reference 存储
    preview.py         # numpy/Pillow 四视图渲染
    cloud.py           # Tripo / 混元 3D 适配器 + 下载/转 STL
    local_mesh.py      # generate_local 命令适配器
    mesh_fit.py        # 网格校准到毫米尺寸（Blender）
    blender.py         # Blender 后台执行器
    blender_policy.py  # bpy 脚本静态审查
    slicers.py         # OrcaSlicer CLI 适配器
    printers.py        # Moonraker / OctoPrint / Bambu LAN / PrusaLink / HTTP 适配器
    drawing.py         # 工程图父进程执行器
    drawing_worker.py  # 第三角四视图 SVG/DXF 子进程
    cad.py             # 父进程侧 CAD 执行器（超时/环境白名单）
    cad_worker.py      # 子进程入口（rlimit + build123d + STL 检查）
    registry.py        # 适配器注册表
    capabilities.py    # 能力探测（只读）
    script_policy.py   # build123d 脚本静态审查（导入白名单）
    errors.py          # 统一错误码
  dsh-3dprint-mcp/      # DSH 配置文件型 bundle（stdio 工具）
  tests/
  docs/
```

## 功能与路线图

| 能力 | 现状 |
| --- | --- |
| 对话式创作 | ✅ 宿主模型驱动 `model_write`/`model_build`/`model_preview`；每轮是同一资产的新版本 |
| 图片理解 | ✅ 多模态宿主模型看图 + `reference_import`/`image_generate` 存 reference，预览时回传对比 |
| Blender 文/图生 3D | ✅ `blender_run`（bpy 脚本 → STL/GLB + 四视图）；图生 3D 由宿主模型看图写场景 |
| 图生网格校准 | ✅ `mesh_fit` 把已有网格校准到真实毫米尺寸或最长边、清理减面并导出成品；`generate_cloud` 可直接自动 fit |
| 本地模型文/图生 3D | ✅ `generate_local` 通用命令适配器（TripoSR / Hunyuan3D / ComfyUI 等，以本机命令为准） |
| 云端文/图生 3D（Tripo/混元） | ✅ `generate_cloud` 异步任务 + 下载 + 转 STL + 预览（需配置 key） |
| 导出模型文件 | ✅ `asset_export`（STL/STEP/脚本/SVG/DXF）；`model_drawing` 出第三角四视图工程图纸 |
| 切片（G-code） | ✅ `slice_*` 四件套，外部 OrcaSlicer，异步任务 + 统计 |
| 打印机管理 | ✅ `printer_save` / `printer_remove` / `printer_list`，config.json 按 id upsert；9 工具头 preflight |
| 切片后发送打印机 | ✅ Moonraker / OctoPrint / Bambu LAN / PrusaLink / 声明式 HTTP 适配器，`send`（上传+启动）与 `printer_cancel`；Snapmaker U1 真机打印完成（25 层 / 325 s）；Bambu 与 PrusaLink 未真机验证 |
| 交互式 3D 查看 | ✅ `model_view` 导出单文件 `viewer.html`：WebGL 旋转/缩放/平移/自旋/截图/导出 STL，无外部依赖 |
| 打印前检查与摆放 | ✅ `printability_check` / `orientation_plan`：薄壁、悬垂、支撑体积估计、热床接触、24 向摆放评分 |

- **M0（完成）**：脚手架、配置/密钥、路径沙箱、任务状态机、MCP stdio+HTTP、资产版本库。
- **M0.5（完成，2026-11-14）**：沙箱化 build123d 执行器、概念图生成、四视图预览、两条建模闭环、导出、11 个工具。
- **M0.6（完成，2026-11-14）**：Blender bpy 路径（`blender_run`）与本地生成器适配器（`generate_local` + 后台任务），13 个工具。
- **M1（完成，2026-11-14）**：云端文/图生 3D（Tripo V3、混元 3D Pro）异步任务、下载与 STL 转换，14 个工具。
- **M2（完成，2026-11-14）**：切片器适配（Orca 外部进程，profile 运行时读取，overrides 私有副本）、`slice_capabilities`/`slice_plan`/`slice_run`/`slice_inspect`、G-code 与 statistics 双源元数据，18 个工具。Snapmaker U1 因厂商 CLI 段错误暂不可用。
- **M3（完成，2026-11-14）**：打印机 CRUD、Moonraker/OctoPrint/Bambu LAN/PrusaLink/声明式 HTTP 适配器、`send` 上传+启动、`printer_cancel`，24 个工具；Snapmaker U1 真机完成切片→上传→打印（25 层 / 325 s / 662 mm）。Bambu 与 PrusaLink 只有单元测试。
- **M3.6（完成，2026-11-14）**：新增 `printability_check` 与 `orientation_plan` 两个只读几何工具（薄壁射线估计、悬垂/支撑估计、热床适配、24 向摆放排序），27 个工具。
- **M3.7（完成，2026-11-14）**：`model_build` 支持 `input=<step>`（STEP 复制进版本并暴露为 `INPUT_STEP`），新增 `model_drawing` 出第三角四视图 SVG/DXF 图纸（`asset_export` 可导出），28 个工具。原因：用户要求「改已有 STEP」与「出工程图」补上 FreeCAD 的两项实际用途。
- **M4（完成，2026-11-14）**：项目由 XPrint 更名为 **3DPrint**（发行名 `3DPrint`，导入名 `print3d`，CLI `3dprint`，环境变量 `PRINT3D_*`，工作区 `~/.3dprint`），`server.py` 拆成 `runtime.py` + `tools/`；安全加固（导入白名单、属性 `_` 禁令、子进程环境白名单、导出限定工作区、下载流式上限、恒时比较 Bearer token），29 个工具；工具 schema 与拆分前逐字节一致。
- **M3.5（已移除，2026-11-14）**：曾实现并安装 DSH 左侧栏只读面板；因与对话流里的 `model_preview` 重复、维护成本高，当日卸载并删除。交互式 3D 改由 `model_view` 的单文件 HTML 提供，25 个工具。
