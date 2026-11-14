/**
 * Host half of the XPrint panel bundle.
 *
 * The browser panel talks MCP over loopback HTTP, while the chat tools use the
 * stdio server configured in the mcp-manager. This half starts a second,
 * read-mostly HTTP server for the panel, on the same workspace, and stops it
 * when the plugin unloads. A server that is already listening (manual start or
 * a previous child) is left alone: uvicorn exits with EADDRINUSE and the panel
 * reconnects to the existing one.
 */

import { spawn } from 'node:child_process'

const DEFAULT_COMMAND = '/Users/macadmin/XPrint/.venv/bin/xprint'
const DEFAULT_WORKSPACE = '/Users/macadmin/XPrint/.xprint'
const DEFAULT_CWD = '/Users/macadmin/XPrint'
const PORT = 8765

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
}
