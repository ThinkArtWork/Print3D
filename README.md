<div align="center">

# Print3D

**自托管 3D 打印 MCP 服务 —— 从一句话到真正打印出来的零件。**

MIT · Python 3.11+ · macOS / Linux · [使用手册](docs/MANUAL.md) · [English](README.en.md)

</div>

![120 × 80 × 22 mm 三格收纳盘](docs/images/parts-tray.png)

<p align="center"><sub>120 × 80 × 22 mm 三格收纳盘 · build123d 脚本生成 · <code>model_preview</code> 渲染</sub></p>

## 功能

| 能力 | 说明 |
| --- | --- |
| **参数化建模** | 用 build123d 写脚本，输出 STL + STEP，返回水密性、壁厚、包围盒等真实几何检查 |
| **多种输入** | 文字直接建模（默认，不需要图片）、概念图、参考图/草图、STL/GLB/OBJ、STEP、Blender bpy 脚本 |
| **模型生成** | 云端：腾讯混元 3D（`hunyuan`）、Tripo AI（`tripo`）；本地部署：`generate_local`；本机 Blender：`blender_run` 执行 bpy 脚本生成 mesh |
| **资产与版本** | 每个资产保留多个 revision、脚本、参考图和检查结果；四视图 PNG 和可旋转的 HTML 查看器 |
| **工程图** | 从 STEP 投影第三角四视图，导出 SVG / DXF，带隐藏线 |
| **可打印性** | 悬垂、支撑体积、薄壁、热床接触、0–100 评分，以及 24 向摆放排序 |
| **切片** | 调用 OrcaSlicer CLI，可覆盖层高、填充、墙数、温度、流量、压力提前、线宽、风扇 |
| **打印** | Moonraker/Klipper、OctoPrint、Bambu LAN、PrusaLink、通用 HTTP；支持多工具头和多材料槽 |
| **安全** | 工作区路径沙箱、脚本静态审查 + 子进程资源限额、写操作确认串、密钥脱敏 |

## 安装

需要 Python 3.11+、git、macOS 或 Linux。推荐安装方式[uv](https://docs.astral.sh/uv/) 。还需要安装两个本机工具(切片和 Blender)：

| 依赖 | 用途 | 不装会怎样 |
| --- | --- | --- |
| [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) | `slice_plan`、`slice_run`、发送打印 | 仍可建模、预览、导出，但不能切片和上机 |
| [Blender](https://www.blender.org/) | `blender_run`、`mesh_fit` 的 bpy 路径 | 仍可走 build123d、云端/本地生成，但不能用 Blender 路径 |

### 交给 Agent 安装（推荐新手）

不用自己逐条敲命令。复制下面这段（GitHub 代码块右上角有复制按钮）交给能执行命令的 Agent：

```text
你是我的安装助手。请在这台 macOS/Linux 机器上安装 Print3D MCP 服务：

仓库：https://github.com/ThinkArtWork/Print3D
依赖：Python 3.11+、git、uv；Windows 不支持。

1. 检查系统和依赖；缺 uv 就按官方方式安装；Python 版本不够就告诉我，不要擅自改系统 Python。
2. 克隆仓库到合适目录，执行 uv sync 和 uv run print3d init。
3. 运行 uv run print3d capabilities，报告 CAD、渲染、切片器、打印机状态。
4. 检查 OrcaSlicer 和 Blender 是否安装；缺失只报告，不要自动下载大文件。
5. 根据我的 Agent（DSH / Claude Desktop / Cursor）生成 stdio 配置；command 使用仓库内 .venv/bin/print3d 的绝对路径，写入前先给我确认。
6. 不要索要 API key，核心功能不需要。最后给我一条测试提示词。
```

### 手动安装

```bash
git clone https://github.com/ThinkArtWork/Print3D.git
cd Print3D
uv sync
uv run print3d init
```

`print3d init` 会创建 `~/.print3d/` 工作区。然后把 Print3D 配置到 Agent（DSH、Claude Desktop、Cursor 等）：

```json
{
  "mcpServers": {
    "print3d": {
      "command": "/绝对路径/Print3D/.venv/bin/print3d",
      "args": ["serve", "--stdio"]
    }
  }
}
```

`command` 必须是绝对路径。核心建模、预览和导出不需要任何 API key；只有概念图、云端生成等可选路径需要：

```bash
export TRIPO_API_KEY=...          # Tripo AI
export HUNYUAN3D_API_KEY=...      # 腾讯混元 3D
export OPENAI_API_KEY=...         # 概念图 / images provider
```

本地部署模型不需要云端 key，在 `providers.local` 里配置命令即可。Agent 接入、HTTP 模式、密钥管理和排错见[使用手册](docs/MANUAL.md)。

## 支持什么

- **输入**：一句话/尺寸约束、概念图、参考图与手绘草图、现有 STL/GLB/OBJ、STEP 文件、Blender bpy 脚本。
- **建模与生成**：build123d 参数化 CAD；腾讯混元 3D、Tripo AI 云端生成；TripoSR、Hunyuan3D、ComfyUI 等本地部署模型（只要能写出 STL）；本机 Blender 通过 `blender_run` 执行 bpy 脚本生成 mesh。
- **复杂造型**：有机雕刻、角色和复杂曲面可走 Blender bpy（`blender_run`）或 mesh（`mesh_fit`）路径；这类工作流比参数化建模更复杂，效果取决于 Agent 和 Blender 脚本。
- **资产能力**：版本管理、四视图预览、交互式 HTML 查看器、SVG/DXF 工程图、STL/STEP/脚本导出。
- **可打印性**：悬垂与支撑估计、薄壁检查、热床接触、摆放评分和 24 向排序。
- **切片与打印**：OrcaSlicer；Moonraker/Klipper（含 Snapmaker U1 实机验证）、OctoPrint、Bambu LAN、PrusaLink、通用 HTTP；多工具头/多材料槽。
- **平台与 Agent**：macOS / Linux；Python 3.11+（CI 覆盖 3.11 / 3.13）；MCP stdio 与 streamable HTTP；DSH、Claude Desktop、Cursor 等 Agent。

## 不支持什么

- **Windows**：依赖 POSIX 进程组和资源限制，当前不可用。
- **自带大模型或聊天界面**：Print3D 是 MCP server，理解需求、写脚本、看图由 Agent 完成。
- **托管的云端建模平台**：它是单用户、自托管服务；HTTP 模式不应直接暴露到公网。
- **保证打印成功**：几何检查和可打印性评分是决策辅助，不是结构仿真或认证。
- **Bambu LAN / PrusaLink 的真机验证**：适配器已有模拟测试，但尚未在对应硬件上完整验证。
- **正式发行版**：项目仍是 Alpha；PyPI 与 GitHub Release 尚未发布，当前以源码安装为主。

## License

MIT © 2026 ThinkArtWork
