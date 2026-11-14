/**
 * Host half of the XPrint panel bundle.
 *
 * The browser panel talks MCP over loopback HTTP. This half starts a second,
 * read-mostly HTTP server for the panel on the same workspace, and stops it
 * when the plugin unloads. It also proxies `/api/xprint/mcp` under the DSH
 * origin, so the browser half never depends on CORS, a Content-Security-Policy
 * connect-src, or a fixed bridge port.
 *
 * A server that is already listening (manual start or a previous child) is
 * left alone: uvicorn exits with EADDRINUSE and the panel reconnects to the
 * existing one.
 */

import { spawn } from 'node:child_process'

const DEFAULT_COMMAND = '/Users/macadmin/XPrint/.venv/bin/xprint'
const DEFAULT_WORKSPACE = '/Users/macadmin/XPrint/.xprint'
const DEFAULT_CWD = '/Users/macadmin/XPrint'
const PORT = 8765
const BRIDGE_URL = `http://127.0.0.1:${PORT}/mcp`
const PROXY_PATH = '/api/xprint/mcp'
const FORWARDED_REQUEST_HEADERS = [
  'content-type',
  'accept',
  'mcp-session-id',
  'mcp-protocol-version',
  'last-event-id',
]
const FORWARDED_RESPONSE_HEADERS = ['content-type', 'mcp-session-id', 'cache-control']

export const inject = ['connection']

export function apply(ctx, config = {}) {
  const command = String(config.command || process.env.XPRINT_BIN || DEFAULT_COMMAND)
  const workspace = String(config.workspace || process.env.XPRINT_WORKSPACE || DEFAULT_WORKSPACE)
  const cwd = String(config.cwd || DEFAULT_CWD)
  let child = null

  ctx.effect(() => {
    const env = { ...process.env, XPRINT_WORKSPACE: workspace }
    // The panel binds loopback only; never let an inherited token turn on auth
    // that the browser half does not have.
    delete env.XPRINT_TOKEN
    try {
      child = spawn(command, ['serve', '--http', '--host', '127.0.0.1', '--port', String(PORT)], {
        cwd,
        env,
        stdio: ['ignore', 'ignore', 'pipe'],
      })
      child.on('error', () => {})
      child.stderr.on('data', () => {})
    } catch {
      child = null
    }
    return () => {
      if (child && !child.killed) {
        try {
          child.kill('SIGTERM')
        } catch {
          // Already gone.
        }
      }
      child = null
    }
  }, 'xprint-panel: loopback HTTP bridge')

  if (ctx.connection?.fetch) {
    ctx.connection.fetch.register({
      path: PROXY_PATH,
      methods: ['POST'],
      requestBody: 'buffered',
      fetch: async request => {
        const headers = new Headers()
        for (const name of FORWARDED_REQUEST_HEADERS) {
          const value = request.headers.get(name)
          if (value) headers.set(name, value)
        }
        let upstream
        try {
          upstream = await fetch(BRIDGE_URL, {
            method: 'POST',
            headers,
            body: await request.arrayBuffer(),
          })
        } catch (error) {
          return Response.json(
            {
              ok: false,
              error: {
                code: 'bridge_unreachable',
                message: `XPrint bridge is not reachable at ${BRIDGE_URL}`,
                detail: error instanceof Error ? error.message : String(error),
              },
            },
            { status: 503, headers: { 'cache-control': 'no-store' } },
          )
        }
        const responseHeaders = new Headers()
        for (const name of FORWARDED_RESPONSE_HEADERS) {
          const value = upstream.headers.get(name)
          if (value) responseHeaders.set(name, value)
        }
        responseHeaders.set('cache-control', 'no-store')
        return new Response(upstream.body, {
          status: upstream.status,
          headers: responseHeaders,
        })
      },
    })
  }
}
