# @local/xprint-panel

XPrint 的 DSH 客户端面板：左侧栏一个立方体图标，点开是本地模型库和最新渲染。

## 结构

| 文件 | 作用 |
| --- | --- |
| `index.js` | Host 半：随插件启动 `xprint serve --http --host 127.0.0.1 --port 8765`，并注册同源代理 `/api/xprint/mcp`，卸载时停掉。 |
| `client.js` | 浏览器半：注册 `sidebar.panellist` 图标和 `main` 面板，优先经同源 `/api/xprint/mcp` 调 `system_status` / `asset_list` / `model_preview`，旧 Host 才回退直连 8765。 |
| `cordis.patch.yml` | 把插件条目插入 profile；`command` / `workspace` / `cwd` 可改。 |
| `locale/en.json`, `locale/zh.json` | 界面文案与 Plugin Manager 的标题/描述/详情页。 |
| `icon.svg` | 侧栏与插件列表图标。 |

## 安装

插件管理器要求绝对路径：

```
plugin_manager install_bundle target: /Users/macadmin/XPrint/dsh-xprint-panel
```

或者 GUI 的 **Plugins → Add plugin**。安装是 profile 级操作，DSH 会把新的客户端
模块推给已打开的页面；如果图标没有立刻出现，刷新一次页面即可（只有替换已安装包
的代码时才需要重启）。

## 配置

`cordis.patch.yml` 的 `config` 支持三个键：

- `command`：`xprint` 可执行文件路径，默认仓库内的 `.venv/bin/xprint`。
- `workspace`：工作区路径，默认 `/Users/macadmin/XPrint/.xprint`。
- `cwd`：子进程工作目录，默认仓库根目录。

`workspace` 必须与会话里 `mcp-manager` 给 XPrint 的 `XPRINT_WORKSPACE` 一致，
否则面板和聊天工具会看到两份不同的资产。

## 注意

- 面板是只读的：写资产、切片、发送打印仍然走会话里的工具和 `confirm` 字符串。
- Host 半把 MCP 反向代理到 DSH 同源的 `/api/xprint/mcp`，因此浏览器不依赖
  CORS、CSP `connect-src` 或固定端口；桌面壳的页面 origin 是 `dsh-app://app`，
  相对路径同样走这个代理。该路由在 DSH 的认证栅栏内，只服务已登录页面。直连
  8765 只是 Host 半版本过旧或静态页面时的回退。
- 8765 端口只绑定 `127.0.0.1`；CORS 只放行 loopback origin（`127.0.0.1` /
  `localhost` / `[::1]`），不放宽到 `null`/`file://`。Host 半会删掉继承来的
  `XPRINT_TOKEN`，因为浏览器半没有凭证。要跨机器访问请自行加认证，不要直接
  把端口暴露到局域网。
- 如果 `secrets.json` 里配置了 `server.token`（或运行环境带 `XPRINT_TOKEN`），
  HTTP 端点会要求 Bearer 认证，而浏览器半没有凭证；把该 token 去掉或改用
  stdio/`mcp-manager` 路径。
- 如果 8765 已被占用，Host 半的 uvicorn 会退出，面板连已有的服务；这适合手动
  调试，但两个进程的工作区必须一致。
- 面板黑屏时：先看开发者工具 Console 是否有 `slot entry crashed`；Host 半已给
  面板加 `PanelBoundary`，正常会显示“XPrint 面板出错”而不是纯黑。
- 面板一直停在“正在启动”时：先确认 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 有进程，
  再请求一次 `curl -i -X POST http://127.0.0.1:<DSH 端口>/api/xprint/mcp`；返回
  401 表示代理路由已注册（未带登录 cookie），404 表示 Host 半还是旧版本，需要
  在 Plugins 页关/开一次插件或重启 DSH。

设计说明见 `docs/DESIGN.md` 的 §3.3，安装与排障概览见仓库根 `README.md`。
