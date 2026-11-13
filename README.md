# XPrint

**XPrint 是一个自托管的 3D 打印 MCP 服务。** 它把「图片/文字生成 3D 模型 → 编辑 → 渲染 → 切片 → 发送到打印机」做成一组可组合的 MCP 工具，接入 DSH、Codex、Claude Desktop、Cursor 等任意 MCP 宿主。

> 状态：**设计阶段**。本目录目前只有设计文档与开发计划，还没有可运行代码。

## 文档

- [设计文档](docs/DESIGN.md) —— 架构、模块、工具面、数据模型、安全边界
- [开发计划](docs/PLAN.md) —— 里程碑、验收标准、风险

## 原则

1. **MCP 先行**：先把工具做对，聊天界面交给宿主；需要时再加自带的 Web 控制台。
2. **适配器化**：云端/本地生成器、CAD、渲染器、切片器、打印机全部是注册表里的可插拔适配器，新增厂商不改核心。
3. **本地优先**：配置、密钥、产物、日志都在本机；云端只在显式配置后启用。
4. **安全门禁**：只读工具可直接执行，写操作必须带精确确认串；不开放任意 G-code 注入。
5. **许可干净**：XPrint 原创代码 MIT；OrcaSlicer/Bambu Studio/Blender 等一律外部进程调用，不链接、不打包、不复制其源码与素材。

## 非目标

- 不做完整的切片器 GUI，不复制 Orca/Bambu 的几百个参数；
- 不托管模型，不做多租户 SaaS；
- 不绑定某一个打印机品牌或某一家云厂商。

## 目录规划（M0 创建）

```
XPrint/
  pyproject.toml
  src/xprint/
    server.py          # MCP 入口（stdio / streamable HTTP）
    registry.py        # 适配器注册表
    jobs.py            # 任务与产物
    config.py          # 配置与密钥
    providers/         # 生成器：cloud/、local/
    cad/               # text-to-cad、导入导出、参数编辑
    render/            # Blender(bpy) / Rhino
    slicing/           # 切片器适配器 + profile 解析 + 审计
    printers/          # Moonraker / PrusaLink / OctoPrint / Bambu LAN
  docs/
  tests/
```
