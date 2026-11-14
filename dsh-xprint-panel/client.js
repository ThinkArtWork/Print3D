/**
 * Browser half of the XPrint panel bundle.
 *
 * Registers a global sidebar entry named `xprint` and the keyed `main` panel
 * that entry opens. The panel speaks MCP over the loopback HTTP server that
 * the Host half starts; it never renders printer or CAD code itself.
 */

window.__ModuleLoader__.load({
  id: '@local/xprint-panel',
  factory(require) {
    const React = require('react')
    const { useCallback, useEffect, useRef, useState } = React
    const h = React.createElement

    const NS = 'xprint'
    const ENDPOINT = 'http://127.0.0.1:8765/mcp'
    const PROTOCOL = '2025-06-18'
    const PANEL_ID = 'xprint'

    const en = {
      panel: 'XPrint',
      title: 'XPrint',
      subtitle: 'Local 3D-printing bridge',
      connected: 'Connected',
      connecting: 'Connecting to XPrint…',
      refresh: 'Refresh',
      offline: 'XPrint is not reachable',
      offlineBody: 'The panel talks to the local XPrint server over MCP. DSH starts it with this plugin; if the port is busy or the binary moved, start it yourself in a terminal:',
      retry: 'Retry',
      models: 'Models',
      noModels: 'No models yet',
      noModelsBody: 'Ask the agent to call model_write, then model_build. The built model appears here.',
      selectHint: 'Select a model to see its latest render.',
      concept: 'Concept image',
      render: 'Rendered model',
      workspace: 'Workspace',
      jobs: 'Jobs',
      assets: 'Assets',
      running: 'running',
      succeeded: 'succeeded',
      revision: 'Revision',
      updated: 'Updated',
      views: 'Views',
      cached: 'Cached',
      reference: 'Concept reference',
      previewFailed: 'Could not render this revision',
      previewLoading: 'Rendering preview…',
      endpoint: 'Endpoint',
      footer: 'Read-only view. Modelling and printing run through the mcp__xprint__ tools in chat.',
      loadFailed: 'Could not load XPrint data',
      unknownError: 'Unknown error',
      latest: 'latest',
    }

    const zh = {
      panel: 'XPrint',
      title: 'XPrint',
      subtitle: '本地 3D 打印桥',
      connected: '已连接',
      connecting: '正在连接 XPrint…',
      refresh: '刷新',
      offline: '无法连接 XPrint',
      offlineBody: '面板通过 MCP 连接本机的 XPrint 服务。DSH 启动时会随插件拉起；如果端口被占用或可执行文件已移动，请在终端手动启动：',
      retry: '重试',
      models: '模型',
      noModels: '还没有模型',
      noModelsBody: '在会话里让模型调用 model_write，再调用 model_build，构建结果就会出现在这里。',
      selectHint: '选择一个模型查看最新渲染。',
      concept: '概念图',
      render: '模型渲染',
      workspace: '工作区',
      jobs: '任务',
      assets: '资产',
      running: '运行中',
      succeeded: '已完成',
      revision: '版本',
      updated: '更新于',
      views: '视图',
      cached: '已缓存',
      reference: '概念参考',
      previewFailed: '这个版本无法渲染',
      previewLoading: '正在渲染预览…',
      endpoint: '端点',
      footer: '只读视图。建模与打印在会话中通过 mcp__xprint__ 工具完成。',
      loadFailed: '无法读取 XPrint 数据',
      unknownError: '未知错误',
      latest: '最新',
    }

    let sessionId = null
    let nextId = 1
    let sessionPromise = null

    function messageOf(error) {
      if (!error) return ''
      return error.message || String(error)
    }

    function parseMessages(text) {
      const trimmed = text.trim()
      if (!trimmed) return []
      if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try {
          const value = JSON.parse(trimmed)
          return Array.isArray(value) ? value : [value]
        } catch {
          return []
        }
      }
      const messages = []
      for (const block of trimmed.split(/\n\n+/)) {
        const data = block
          .split('\n')
          .filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart())
          .join('\n')
        if (!data) continue
        try {
          messages.push(JSON.parse(data))
        } catch {
          // Keep scanning: one malformed event must not hide later replies.
        }
      }
      return messages
    }

    async function post(body) {
      const headers = {
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
      }
      if (sessionId) headers['mcp-session-id'] = sessionId
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 60000)
      let response
      try {
        response = await fetch(ENDPOINT, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: controller.signal,
        })
      } catch (error) {
        if (error && error.name === 'AbortError') {
          throw new Error(`XPrint did not answer within 60s (${ENDPOINT})`)
        }
        throw new Error(`Cannot reach XPrint at ${ENDPOINT}`)
      } finally {
        clearTimeout(timer)
      }
      const header = response.headers.get('mcp-session-id')
      if (header) sessionId = header
      const text = await response.text()
      if (!response.ok) {
        const error = new Error(`XPrint returned HTTP ${response.status}`)
        error.status = response.status
        error.body = text.slice(0, 300)
        throw error
      }
      return { text, messages: parseMessages(text) }
    }

    async function request(method, params) {
      const id = nextId++
      const body = { jsonrpc: '2.0', id, method }
      if (params !== undefined) body.params = params
      const { messages } = await post(body)
      const reply = messages.find(item => item && item.id === id && (item.result !== undefined || item.error !== undefined))
      if (!reply) throw new Error(`XPrint sent no reply to ${method}`)
      if (reply.error) throw new Error(reply.error.message || `XPrint rejected ${method}`)
      return reply.result
    }

    async function notify(method, params) {
      const body = { jsonrpc: '2.0', method }
      if (params !== undefined) body.params = params
      await post(body)
    }

    async function ensureSession() {
      if (sessionId) return
      if (!sessionPromise) {
        sessionPromise = (async () => {
          await request('initialize', {
            protocolVersion: PROTOCOL,
            capabilities: {},
            clientInfo: { name: 'xprint-panel', version: '0.1.0' },
          })
          if (!sessionId) throw new Error('XPrint did not return an MCP session id')
          await notify('notifications/initialized')
        })().finally(() => {
          sessionPromise = null
        })
      }
      return sessionPromise
    }

    async function callToolRaw(name, args) {
      await ensureSession()
      const send = () => request('tools/call', { name, arguments: args || {} })
      let result
      try {
        result = await send()
      } catch (error) {
        if (error && (error.status === 400 || error.status === 404) && sessionId) {
          sessionId = null
          await ensureSession()
          result = await send()
        } else {
          throw error
        }
      }
      if (result && result.isError) {
        const block = (result.content || []).find(item => item.type === 'text')
        throw new Error(block ? block.text : `${name} failed`)
      }
      return result || {}
    }

    function payloadOf(result) {
      if (result && result.structuredContent) return result.structuredContent
      const block = result && (result.content || []).find(item => item.type === 'text')
      if (!block) return {}
      try {
        return JSON.parse(block.text)
      } catch {
        return { text: block.text }
      }
    }

    async function callTool(name, args) {
      return payloadOf(await callToolRaw(name, args))
    }

    function imagesOf(result) {
      return (result && result.content ? result.content : [])
        .filter(item => item.type === 'image')
        .map(item => `data:${item.mimeType || 'image/png'};base64,${item.data}`)
    }

    function when(value) {
      if (!value) return ''
      const date = new Date(value)
      return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
    }

    function CubeIcon({ size = 20 }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.6,
          strokeLinejoin: 'round',
          strokeLinecap: 'round',
          'aria-hidden': true,
          style: { display: 'block', flex: 'none' },
        },
        h('path', { d: 'M12 2.8 21 7.4v9.2L12 21.2 3 16.6V7.4Z' }),
        h('path', { d: 'M3 7.4 12 12l9-4.6M12 12v9.2' }),
      )
    }

    function PanelIcon({ size = 18 }) {
      return h(CubeIcon, { size })
    }

    const styles = {
      root: {
        display: 'flex',
        height: '100%',
        minHeight: 0,
        background: 'var(--dsw-alias-bg-base)',
        color: 'var(--dsw-alias-label-primary)',
        fontFamily: 'inherit',
        overflow: 'hidden',
      },
      aside: {
        width: 336,
        minWidth: 280,
        maxWidth: 420,
        borderRight: '1px solid var(--dsw-alias-border-l2)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      },
      head: {
        padding: '14px 16px 12px',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      },
      iconBubble: {
        width: 32,
        height: 32,
        borderRadius: 9,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--dsw-alias-button-primary-fill)',
        color: 'var(--dsw-alias-label-primary-foreground)',
        flex: 'none',
      },
      title: { fontSize: 15, fontWeight: 600, lineHeight: '20px' },
      subtitle: { fontSize: 12, color: 'var(--dsw-alias-label-tertiary)', lineHeight: '16px' },
      grow: { flex: 1, minWidth: 0 },
      button: {
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-secondary)',
        borderRadius: 7,
        padding: '4px 10px',
        fontSize: 12,
        cursor: 'pointer',
        fontFamily: 'inherit',
      },
      statusRow: {
        display: 'flex',
        alignItems: 'center',
        gap: 7,
        padding: '0 16px 12px',
        fontSize: 12,
        color: 'var(--dsw-alias-label-secondary)',
      },
      dot: { width: 7, height: 7, borderRadius: 999, flex: 'none' },
      stats: {
        margin: '0 16px 12px',
        padding: '10px 12px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 9,
        display: 'flex',
        flexDirection: 'column',
        gap: 7,
        fontSize: 12,
      },
      statRow: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 },
      statLabel: { color: 'var(--dsw-alias-label-tertiary)', flex: 'none' },
      mono: {
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 11,
        color: 'var(--dsw-alias-label-secondary)',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        direction: 'rtl',
        textAlign: 'left',
      },
      listHead: {
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '4px 16px 6px',
        fontSize: 12,
        color: 'var(--dsw-alias-label-tertiary)',
      },
      list: { flex: 1, minHeight: 0, overflow: 'auto', padding: '0 8px 8px' },
      row: {
        display: 'flex',
        flexDirection: 'column',
        gap: 3,
        width: '100%',
        textAlign: 'left',
        border: '1px solid transparent',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        borderRadius: 8,
        padding: '8px 10px',
        cursor: 'pointer',
        fontFamily: 'inherit',
        fontSize: 13,
      },
      rowActive: {
        background: 'var(--dsw-alias-interactive-bg-active)',
        borderColor: 'var(--dsw-alias-border-l2)',
      },
      rowName: { fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      rowMeta: { fontSize: 11, color: 'var(--dsw-alias-label-tertiary)' },
      foot: {
        padding: '10px 16px 14px',
        fontSize: 11,
        lineHeight: '16px',
        color: 'var(--dsw-alias-label-tertiary)',
        borderTop: '1px solid var(--dsw-alias-border-l2)',
      },
      main: { flex: 1, minWidth: 0, minHeight: 0, overflow: 'auto', padding: '18px 20px 24px' },
      empty: {
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        color: 'var(--dsw-alias-label-tertiary)',
        textAlign: 'center',
        padding: 24,
      },
      emptyTitle: { color: 'var(--dsw-alias-label-secondary)', fontSize: 14, fontWeight: 500 },
      emptyBody: { fontSize: 12, lineHeight: '18px', maxWidth: 380 },
      detailHead: { display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 16 },
      detailTitle: { fontSize: 18, fontWeight: 600, lineHeight: '24px' },
      detailId: {
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 11,
        color: 'var(--dsw-alias-label-tertiary)',
        marginTop: 2,
      },
      card: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 10,
        background: 'var(--dsw-alias-bg-layer-1)',
        padding: 12,
      },
      image: {
        display: 'block',
        width: '100%',
        maxHeight: '58vh',
        objectFit: 'contain',
        borderRadius: 8,
        background: 'var(--dsw-alias-bg-layer-2)',
      },
      caption: { fontSize: 11, color: 'var(--dsw-alias-label-tertiary)', margin: '6px 0 12px' },
      metaGrid: {
        display: 'grid',
        gridTemplateColumns: 'max-content 1fr',
        gap: '6px 14px',
        fontSize: 12,
        marginTop: 14,
      },
      metaKey: { color: 'var(--dsw-alias-label-tertiary)' },
      metaValue: {
        color: 'var(--dsw-alias-label-secondary)',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      },
      offlineWrap: {
        height: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      },
      offlineCard: {
        maxWidth: 520,
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 12,
        background: 'var(--dsw-alias-bg-layer-1)',
        padding: 20,
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        alignItems: 'flex-start',
      },
      offlineTitle: { fontSize: 15, fontWeight: 600 },
      offlineBody: { fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-label-secondary)' },
      code: {
        display: 'block',
        width: '100%',
        boxSizing: 'border-box',
        margin: '2px 0 4px',
        padding: '8px 10px',
        borderRadius: 7,
        background: 'var(--dsw-alias-markdown-code-block)',
        color: 'var(--dsw-alias-label-secondary)',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 11,
        overflowX: 'auto',
        whiteSpace: 'pre',
      },
      errorText: { fontSize: 12, color: 'var(--dsw-alias-label-error)', lineHeight: '18px' },
    }

    const hoverCss = `
      .xp-row:hover { background: var(--dsw-alias-interactive-bg-hover); }
      .xp-btn:hover { background: var(--dsw-alias-interactive-bg-hover); color: var(--dsw-alias-label-primary); }
      .xp-btn:focus-visible, .xp-row:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }
    `

    function XPrintPanel({ t }) {
      const [phase, setPhase] = useState('connecting')
      const [status, setStatus] = useState(null)
      const [assets, setAssets] = useState([])
      const [error, setError] = useState('')
      const [busy, setBusy] = useState(false)
      const [selected, setSelected] = useState(null)
      const [detail, setDetail] = useState(null)
      const [preview, setPreview] = useState(null)
      const [previewError, setPreviewError] = useState('')
      const [previewLoading, setPreviewLoading] = useState(false)
      const retryTimer = useRef(null)
      const retries = useRef(0)

      const refresh = useCallback(async () => {
        if (retryTimer.current) {
          clearTimeout(retryTimer.current)
          retryTimer.current = null
        }
        setBusy(true)
        setError('')
        try {
          const [nextStatus, listing] = await Promise.all([
            callTool('system_status'),
            callTool('asset_list', { limit: 50 }),
          ])
          setStatus(nextStatus)
          setAssets(Array.isArray(listing.assets) ? listing.assets : [])
          setPhase('ready')
          retries.current = 0
        } catch (cause) {
          setPhase('error')
          setError(messageOf(cause))
          // The Host half starts the bridge alongside the page; give it a few
          // seconds to bind before settling on the offline card.
          if (retries.current < 8) {
            retries.current += 1
            retryTimer.current = setTimeout(() => {
              retryTimer.current = null
              refresh()
            }, 3000)
          }
        } finally {
          setBusy(false)
        }
      }, [])

      useEffect(() => {
        refresh()
        return () => {
          if (retryTimer.current) clearTimeout(retryTimer.current)
        }
      }, [refresh])

      const openAsset = useCallback(async asset => {
        setSelected(asset)
        setDetail(null)
        setPreview(null)
        setPreviewError('')
        setPreviewLoading(true)
        try {
          const result = await callToolRaw('model_preview', { asset_id: asset.asset_id })
          const payload = payloadOf(result)
          if (payload.ok === false) {
            const failure = payload.error || {}
            setPreviewError(
              failure.message
                ? `${failure.message}${failure.hint ? ` — ${failure.hint}` : ''}`
                : t('previewFailed'),
            )
          } else {
            setDetail(payload)
            setPreview(imagesOf(result))
          }
        } catch (cause) {
          setPreviewError(messageOf(cause))
        } finally {
          setPreviewLoading(false)
        }
      }, [t])

      const dotColor = phase === 'ready'
        ? 'var(--dsw-alias-state-success-primary)'
        : phase === 'connecting'
          ? 'var(--dsw-alias-state-warn-primary)'
          : 'var(--dsw-alias-state-error-primary)'

      if (phase === 'error') {
        return h(
          'div',
          { style: styles.root },
          h('style', null, hoverCss),
          h(
            'div',
            { style: styles.offlineWrap },
            h(
              'div',
              { style: styles.offlineCard },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 9 } },
                h('span', { style: { color: 'var(--dsw-alias-state-error-primary)' } }, h(CubeIcon, { size: 22 })),
                h('div', { style: styles.offlineTitle }, t('offline'))),
              h('div', { style: styles.offlineBody }, t('offlineBody')),
              h('code', { style: styles.code }, 'xprint serve --http --host 127.0.0.1 --port 8765'),
              h('div', { style: styles.offlineBody },
                t('endpoint'), ': ', h('span', { style: styles.mono }, ENDPOINT)),
              error ? h('div', { style: styles.errorText }, error) : null,
              h('button', {
                type: 'button',
                className: 'xp-btn',
                style: { ...styles.button, marginTop: 4, color: 'var(--dsw-alias-label-primary)' },
                onClick: refresh,
                disabled: busy,
              }, busy ? t('connecting') : t('retry')),
            ),
          ),
        )
      }

      return h(
        'div',
        { style: styles.root },
        h('style', null, hoverCss),
        h(
          'aside',
          { style: styles.aside },
          h(
            'div',
            { style: styles.head },
            h('div', { style: styles.iconBubble }, h(CubeIcon, { size: 19 })),
            h('div', { style: styles.grow },
              h('div', { style: styles.title }, t('title')),
              h('div', { style: styles.subtitle }, t('subtitle'))),
            h('button', {
              type: 'button',
              className: 'xp-btn',
              style: styles.button,
              onClick: refresh,
              disabled: busy,
              title: t('refresh'),
            }, busy ? '…' : '↻'),
          ),
          h(
            'div',
            { style: styles.statusRow },
            h('span', { style: { ...styles.dot, background: dotColor } }),
            h('span', null, phase === 'ready' ? t('connected') : t('connecting')),
            status ? h('span', { style: { color: 'var(--dsw-alias-label-tertiary)' } }, ` · v${status.version || ''}`) : null,
          ),
          status
            ? h(
              'div',
              { style: styles.stats },
              h('div', { style: styles.statRow },
                h('span', { style: styles.statLabel }, t('workspace')),
                h('span', { style: styles.mono, title: status.workspace && status.workspace.root }, (status.workspace && status.workspace.root) || '—')),
              h('div', { style: styles.statRow },
                h('span', { style: styles.statLabel }, t('jobs')),
                h('span', null, `${status.jobs ? status.jobs.running : 0} ${t('running')} · ${status.jobs ? status.jobs.succeeded : 0} ${t('succeeded')}`)),
              h('div', { style: styles.statRow },
                h('span', { style: styles.statLabel }, t('assets')),
                h('span', null, String(assets.length))),
            )
            : null,
          h('div', { style: styles.listHead },
            h('span', null, t('models')),
            h('span', { style: { ...styles.grow, textAlign: 'right' } }, String(assets.length))),
          h(
            'div',
            { style: styles.list },
            assets.length === 0
              ? h('div', { style: { ...styles.emptyBody, padding: '18px 10px' } }, t('noModelsBody'))
              : assets.map(asset => h(
                'button',
                {
                  key: asset.asset_id,
                  type: 'button',
                  className: 'xp-row',
                  style: {
                    ...styles.row,
                    ...(selected && selected.asset_id === asset.asset_id ? styles.rowActive : {}),
                  },
                  onClick: () => openAsset(asset),
                },
                h('span', { style: styles.rowName }, asset.name || asset.asset_id),
                h('span', { style: styles.rowMeta },
                  `${asset.revisions} rev · ${when(asset.updated_at)}`),
              )),
          ),
          h('div', { style: styles.foot }, t('footer')),
        ),
        h(
          'section',
          { style: styles.main },
          selected
            ? h(
              'div',
              null,
              h(
                'div',
                { style: styles.detailHead },
                h('div', { style: styles.grow },
                  h('div', { style: styles.detailTitle }, selected.name || selected.asset_id),
                  h('div', { style: styles.detailId }, selected.asset_id)),
                h('button', {
                  type: 'button',
                  className: 'xp-btn',
                  style: styles.button,
                  onClick: () => openAsset(selected),
                  disabled: previewLoading,
                }, t('refresh')),
              ),
              previewError
                ? h('div', { style: { ...styles.card, ...styles.errorText } },
                  h('div', { style: { fontWeight: 600, marginBottom: 4 } }, t('previewFailed')),
                  previewError)
                : previewLoading
                  ? h('div', { style: { ...styles.card, ...styles.emptyBody } }, t('previewLoading'))
                  : preview && preview.length > 0
                    ? h('div', null,
                      preview.map((src, index) => h('div', { key: index },
                        h('img', {
                          src,
                          alt: index === 0 && preview.length > 1 ? t('concept') : t('render'),
                          style: styles.image,
                        }),
                        h('div', { style: styles.caption },
                          preview.length > 1
                            ? (index === 0 ? t('concept') : t('render'))
                            : t('render')))))
                    : h('div', { style: { ...styles.card, ...styles.emptyBody } }, t('selectHint')),
              detail
                ? h(
                  'div',
                  { style: styles.metaGrid },
                  h('span', { style: styles.metaKey }, t('revision')),
                  h('span', { style: styles.metaValue }, String(detail.revision || '—')),
                  h('span', { style: styles.metaKey }, t('views')),
                  h('span', { style: styles.metaValue }, String(detail.views || '—')),
                  h('span', { style: styles.metaKey }, t('cached')),
                  h('span', { style: styles.metaValue }, detail.cached ? 'yes' : 'no'),
                  detail.reference
                    ? [h('span', { key: 'rk', style: styles.metaKey }, t('reference')),
                      h('span', { key: 'rv', style: styles.metaValue }, String(detail.reference))]
                    : null,
                )
                : null,
            )
            : h(
              'div',
              { style: styles.empty },
              h('div', { style: { color: 'var(--dsw-alias-label-tertiary)' } }, h(CubeIcon, { size: 40 })),
              h('div', { style: styles.emptyTitle }, assets.length === 0 ? t('noModels') : t('selectHint')),
              assets.length === 0 ? h('div', { style: styles.emptyBody }, t('noModelsBody')) : null,
            ),
        ),
      )
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { en, zh }), 'xprint-panel: dictionaries')
        const t = ctx.locale.bind(NS)
        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: PANEL_ID,
          locale: NS,
        }, () => h(XPrintPanel, { t })))
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: PANEL_ID,
          order: 45,
          locale: NS,
          label: () => t('panel'),
        }, PanelIcon))
      },
    }
  },
})
