// Real Chromium smoke test using Node's built-in CDP WebSocket; no added dependencies.
// CHROME_BIN=/path/to/chrome node tests/onestrm_ui_smoke.cjs
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { createServer } = require('node:http')
const { readFileSync, writeFileSync, mkdtempSync, rmSync, existsSync } = require('node:fs')
const { tmpdir, homedir } = require('node:os')
const { join, resolve } = require('node:path')

const root = resolve(__dirname, '..')
const chromePath = process.env.CHROME_BIN || join(homedir(), '.cache/ms-playwright/chromium-1228/chrome-linux64/chrome')
assert(existsSync(chromePath), 'Set CHROME_BIN to an installed Chromium executable')
const profile = mkdtempSync(join(tmpdir(), 'onestrm-ui-'))
let chrome, socket, seq = 0
const pending = new Map(), calls = [], errors = []
let config = { enabled: true, drive_id: 1, drive_name: '115 A', base_url: 'http://onestrm:8080',
  token_set: true, auth_mode: 'bearer', timeout_seconds: 10, quiet_seconds: 10 }
const record = { kind: 'batch', id: 1, drive_id: 1, drive_name: '115 A', path: '/影视/<img src=x onerror=alert(1)>',
  status: 'exhausted', file_count: 10, attempts: 4, updated: 1000, error: 'OneStrm HTTP 503', last_request: 999 }
const status = { enabled: true, paused: false, rows: [record], history: [{ ...record, id: 2, status: 'success', error: '' }],
  pending_count: 1, history_count: 1, bytes: 8192, target_error: '', error: '' }
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://local').pathname
    const files = { '/ui/index.html': 'plugins/onestrm/ui/index.html', '/ui/app.js': 'plugins/onestrm/ui/app.js',
      '/ui/style.css': 'plugins/onestrm/ui/style.css', '/_shared/sdk.js': 'plugins/_shared/sdk.js' }
    if (files[path]) {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'text/html')
      res.end(readFileSync(join(root, files[path]))); return
    }
    if (path === '/favicon.ico') { res.writeHead(204); res.end(); return }
    let body = ''
    for await (const part of req) body += part
    body = body ? JSON.parse(body) : null
    calls.push({ path, method: req.method, body, auth: req.headers.authorization })
    assert.equal(req.headers.authorization, 'Bearer host-session')
    const route = path.replace('/api/plugins/onestrm-test/onestrm', '')
    let result
    if (route === '/config' && req.method === 'GET') result = { config }
    else if (route === '/drives') result = { drives: [{ id: 1, name: '115 A', is_active: true }, { id: 2, name: '115 B', is_active: false }] }
    else if (route === '/status') result = status
    else if (route === '/config') {
      config = { ...config, ...body, token_set: !body.clear_token }
      delete config.token
      result = { ok: true, config }
    } else if (route === '/test-connection') result = { ok: true, message: 'TCP 连接成功；尚未验证鉴权或扫描接口' }
    else if (route === '/test-scan') result = { ok: true, message: '通知成功（HTTP 已接收）；STRM 生成请在 OneStrm 确认' }
    else if (route.startsWith('/retry/')) { status.rows[0].status = 'waiting'; result = { ok: true } }
    else if (route.startsWith('/cancel/')) { status.rows = []; status.pending_count = 0; result = { ok: true } }
    else if (route === '/history/clear') { status.history = []; status.history_count = 0; result = { ok: true } }
    else { res.writeHead(404); res.end(); return }
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(result))
  } catch (error) { errors.push(String(error)); res.writeHead(500); res.end() }
})

function command(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++seq
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)) }, 10000)
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const data = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (data.exceptionDetails) throw new Error(JSON.stringify(data.exceptionDetails))
  return data.result.value
}
async function until(expression) {
  return evaluate(`(async () => { const end = Date.now() + 5000; while (!(${expression})) { if (Date.now() > end) throw Error('UI wait timed out'); await new Promise(r => setTimeout(r, 30)); } return true; })()`)
}

;(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  chrome = spawn(chromePath, ['--headless', '--no-sandbox', '--disable-dev-shm-usage', '--remote-debugging-port=0',
    '--user-data-dir=' + profile, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] })
  const endpoint = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Chromium startup timed out')), 10000)
    chrome.once('error', reject)
    chrome.stderr.on('data', chunk => {
      const match = chunk.toString().match(/DevTools listening on (ws:\/\/[^\s]+)/)
      if (match) { clearTimeout(timer); resolve(match[1]) }
    })
  })
  const origin = endpoint.replace(/^ws:/, 'http:').split('/devtools')[0]
  const pages = await (await fetch(origin + '/json/list')).json()
  socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  socket.addEventListener('message', ({ data }) => {
    const event = JSON.parse(data)
    if (event.id && pending.has(event.id)) {
      const request = pending.get(event.id); clearTimeout(request.timer); pending.delete(event.id)
      event.error ? request.reject(new Error(event.error.message)) : request.resolve(event.result)
    } else if (event.method === 'Runtime.exceptionThrown') errors.push(JSON.stringify(event.params))
  })
  await command('Runtime.enable')
  await command('Page.enable')
  await command('Emulation.setDeviceMetricsOverride', { width: 1040, height: 1500, deviceScaleFactor: 1, mobile: false })
  await command('Page.addScriptToEvaluateOnNewDocument', { source: `window.addEventListener('DOMContentLoaded', () => window.postMessage({ protocol: 'drivecat.plugin.v1', type: 'host.init', payload: { token: 'host-session', context: { plugin_id: 'onestrm-test' }, theme: 'light', cssVars: { '--dc-primary': '#123456' } } }, '*'));` })
  await command('Page.navigate', { url: 'http://127.0.0.1:' + server.address().port + '/ui/index.html' })
  await until(`document.getElementById('summary') && document.getElementById('summary').textContent.includes('待处理 1')`)
  if (process.env.ONESTRM_SCREENSHOT) {
    const shot = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
    writeFileSync(process.env.ONESTRM_SCREENSHOT, Buffer.from(shot.data, 'base64'))
  }
  assert.equal(await evaluate(`document.getElementById('token').value`), '')
  assert.equal(await evaluate(`document.querySelectorAll('#queue img').length`), 0)
  assert.equal(await evaluate(`document.querySelector('#drive-id option[value="2"]').disabled`), true)
  assert.equal(await evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--dc-primary').trim()`), '#123456')
  await evaluate(`document.getElementById('test-connection').click()`)
  await until(`document.getElementById('message').textContent.includes('TCP 连接成功')`)
  assert(!calls.some(c => c.path.endsWith('/test-scan')))
  await evaluate(`document.getElementById('base-url').dispatchEvent(new Event('input', { bubbles: true })); document.getElementById('scan-path').value='/影视/测试'; document.getElementById('test-scan').click()`)
  await until(`document.getElementById('message').textContent.includes('请先保存')`)
  assert(!calls.some(c => c.path.endsWith('/test-scan')))
  await evaluate(`document.getElementById('save').click()`)
  await until(`document.getElementById('message').textContent === '设置已保存'`)
  assert.equal(calls.find(c => c.path.endsWith('/config') && c.method === 'POST').body.token, null)
  await evaluate(`document.getElementById('test-scan').click()`)
  await until(`document.getElementById('message').textContent.includes('STRM 生成请在 OneStrm 确认')`)
  assert.deepEqual(calls.find(c => c.path.endsWith('/test-scan')).body, { path: '/影视/测试' })
  await evaluate(`Array.from(document.querySelectorAll('#queue button')).find(b => b.textContent === '重试').click()`)
  await until(`document.getElementById('queue').textContent.includes('等待发送')`)
  await evaluate(`Array.from(document.querySelectorAll('#queue button')).find(b => b.textContent === '取消本批').click()`)
  await until(`document.getElementById('queue').children.length === 0`)
  await evaluate(`document.getElementById('clear-history').click()`)
  await until(`document.getElementById('history').children.length === 0`)
  await command('Emulation.setDeviceMetricsOverride', { width: 375, height: 812, deviceScaleFactor: 1, mobile: true })
  await evaluate(`document.documentElement.setAttribute('data-theme', 'dark'); document.documentElement.style.setProperty('--dc-bg-card','#15191f'); document.documentElement.style.setProperty('--dc-text-primary','#e6e6e6')`)
  assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true)
  assert.deepEqual(errors, [])
  console.log('PASS: Chromium SDK init, theme, mobile layout, secret masking, save, connection/scan distinction, retry/cancel/history, XSS text rendering')
})().catch(async error => {
  console.error(error)
  if (socket && socket.readyState === 1) {
    console.error(await evaluate(`({url:location.href,ready:document.readyState,message:document.getElementById('message')?.textContent,sdk:typeof DriveCat,summary:document.getElementById('summary')?.textContent})`))
    console.error({ calls, errors })
  }
  process.exitCode = 1
}).finally(async () => {
  if (socket) socket.close()
  if (chrome) {
    const exited = new Promise(resolve => chrome.once('exit', resolve))
    chrome.kill(); await exited
  }
  await new Promise(resolve => server.close(resolve))
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
})
