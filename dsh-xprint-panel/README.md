# @local/xprint-panel

XPrint 的 DSH 客户端面板：左侧栏一个立方体图标，点开是本地模型库和最新渲染。

## 结构

| 文件 | 作用 |
| --- | --- |
| `index.js` | Host 半：随插件启动 `xprint serve --http --host 127.0.0.1 --port 8765`，卸载时停掉。 |
| `client.js` | 浏览器半：注册 `sidebar.panellist` 图标和 `main` 面板，通过 MCP over HTTP 调 `system_status` / `asset_list` / `model_preview`。 |
| `cordis.patch.yml` | 把插件条目插入 profile；`command` / `workspace` / `cwd` 可改。 |
| `locale/en.json`, `locale/zh.json` | 界面文案与 Plugin Manager 的标题/描述/详情页。 |
| `icon.svg` | 侧栏与插件列表图标。 |

## 安装

插件管理器要求绝对路径：

```
plugin_manager install_bundle target: /Users/macadmin/XPrint/dsh-xprint-panel
```

或者 GUI 的 **Plugins → Add plugin**。安装是 profile 级操作，装完后需要重启
DSH（或让本次运行重新加载 profile）才能在左侧栏看到图标。

## 配置

`cordis.patch.yml` 的 `config` 支持三个键：

- `command`：`xprint` 可执行文件路径，默认仓库内的 `.venv/bin/xprint`。
- `workspace`：工作区路径，默认 `/Users/macadmin/XPrint/.xprint`。
- `cwd`：子进程工作目录，默认仓库根目录。

`workspace` 必须与会话里 `mcp-manager` 给 XPrint 的 `XPRINT_WORKSPACE` 一致，
否则面板和聊天工具会看到两份不同的资产。

## 注意

- 面板是只读的：写资产、切片、发送打印仍然走会话里的工具和 `confirm` 字符串。
- 8765 端口只绑定 `127.0.0.1`；Host 半会删掉继承来的 `XPRINT_TOKEN`，因为浏览器
  半没有凭证。要跨机器访问请自行加认证，不要直接把端口暴露到局域网。
- 如果 8765 已被占用，Host 半的 uvicorn 会退出，面板连已有的服务；这适合手动
  调试，但两个进程的工作区必须一致。
