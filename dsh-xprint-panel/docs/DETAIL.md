# XPrint 面板

左侧栏的 **XPrint** 图标打开一个只读的建模工作区视图。它不替代会话里的
`mcp__xprint__*` 工具，只是把同一份工作区数据可视化出来：

- **模型列表**：`asset_list` 返回的资产，按更新时间排序，显示版本数。
- **最新渲染**：选中模型后调用 `model_preview`，显示 `model.stl` 的渲染图；
  如果该版本带概念图，会先显示概念图，再显示渲染图。
- **运行状态**：`system_status` 的版本、工作区路径、任务计数。

## 数据流

```
DSH 主机进程
  └─ 本插件的 Host 半：spawn `xprint serve --http --host 127.0.0.1 --port 8765`
        └─ XPrint MCP over HTTP（与会话里的 stdio 服务共用同一个工作区）
              └─ 浏览器半：fetch /mcp，initialize → tools/call
```

浏览器半只使用 MCP 的 `initialize` / `notifications/initialized` /
`tools/call`，不直接读文件系统，也不保留打印机凭据。

## 只读边界

面板不会写入资产、不会启动切片或打印。所有写操作仍然必须由会话里的模型调用
对应工具，并带上精确的 `confirm` 字符串。这是 XPrint 的安全边界：UI 只负责看。

## 失败时

如果 8765 端口已被占用（例如手动起过一个 `xprint serve --http`），插件不会
重复启动服务，面板会直接连上已有的那个。如果二进制路径变了，面板会显示离线
卡片，里面给出可复制的启动命令；也可以用 `cordis.patch.yml` 里的 `command`
配置覆盖。
