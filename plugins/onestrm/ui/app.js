/* Reuse the shared DriveCat SDK for authentication, theme and iframe sizing. */
;(function () {
  'use strict'
  const $ = id => document.getElementById(id)
  const labels = { waiting: '等待发送', sending: '请求中', retry: '等待重试', success: '通知成功',
    auth: '鉴权异常', failed: '需要处理', exhausted: '重试耗尽', inspection: '待核查', cancelled: '已取消' }
  let offset = 0, refreshing = false, initialized = false, dirty = false
  const api = (method, path, body) => DriveCat.api(method, '/onestrm' + path, body)
  const date = value => value ? new Date(value * 1000).toLocaleString() : '—'

  function message(text, ok) {
    $('message').textContent = text
    $('message').className = ok ? 'ok' : 'error'
    DriveCat.resize()
  }

  function collect() {
    return { enabled: $('enabled').checked, drive_id: Number($('drive-id').value) || null,
      base_url: $('base-url').value.trim(), token: $('token').value || null, clear_token: $('clear-token').checked,
      auth_mode: $('auth-mode').value, timeout_seconds: Number($('timeout-seconds').value),
      quiet_seconds: Number($('quiet-seconds').value) }
  }

  function fill(config, drives) {
    if (drives) {
      $('drive-id').replaceChildren(new Option('请选择网盘', ''))
      drives.forEach(d => {
        const option = new Option(d.name + (d.is_active ? '' : '（已禁用）'), String(d.id))
        option.disabled = !d.is_active
        $('drive-id').appendChild(option)
      })
    }
    if (config.drive_id && !Array.from($('drive-id').options).some(o => o.value === String(config.drive_id))) {
      const missing = new Option((config.drive_name || '原网盘') + '（不可用）', String(config.drive_id))
      missing.disabled = true
      $('drive-id').appendChild(missing)
    }
    $('drive-id').value = config.drive_id || ''
    $('enabled').checked = config.enabled
    $('base-url').value = config.base_url || ''
    $('token').value = ''
    $('token').placeholder = config.token_set ? '已保存，留空保持不变' : '请输入 OneStrm Token'
    $('clear-token').checked = false
    $('auth-mode').value = config.auth_mode
    $('timeout-seconds').value = config.timeout_seconds
    $('quiet-seconds').value = config.quiet_seconds
    dirty = false
  }

  async function action(button, task) {
    if (button.disabled) return
    button.disabled = true
    try { await task() } catch (error) { message('操作失败：' + error.message + '，请检查设置或当前记录状态', false) }
    finally { button.disabled = false; DriveCat.resize() }
  }

  function render(container, rows, live) {
    container.replaceChildren()
    rows.forEach(row => {
      const item = document.createElement('div'); item.className = 'record'
      const title = document.createElement('strong')
      title.textContent = row.path + ' · ' + (live && row.config_changed ? '配置已变更 · ' : '') +
        (row.kind === 'inbox' && !row.error ? '等待核对' : labels[row.status] || row.status)
      const detail = document.createElement('p')
      detail.textContent = (row.drive_name || '115') + ' (#' + row.drive_id + ') · ' +
        (row.file_count ? row.file_count + ' 个文件' : '手动扫描') + ' · 重试 ' + Math.max(0, row.attempts - 1) +
        ' 次 · 最近请求 ' + date(row.last_request) + ' · 更新 ' + date(row.updated)
      const error = document.createElement('p')
      error.textContent = row.error + (row.status === 'retry' ? ' · 下次尝试 ' + date(row.due) : '')
      item.append(title, detail, error)
      if (live && row.config_changed) {
        const target = document.createElement('p'); target.textContent = '原服务：' + row.base_url; item.appendChild(target)
      }
      function button(label, path) {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = label
        b.addEventListener('click', () => action(b, async () => { await api('POST', path); await refresh() }))
        item.appendChild(b)
      }
      if (live && row.status !== 'sending') {
        if (!row.config_changed && ['retry', 'exhausted', 'failed', 'inspection'].includes(row.status)) {
          button(row.kind === 'inbox' ? '重新核查' : '重试', '/retry/' + row.kind + '/' + row.id)
        }
        button('取消本批', '/cancel/' + row.kind + '/' + row.id)
      }
      container.appendChild(item)
    })
  }

  async function refresh() {
    if (refreshing) return
    refreshing = true
    try {
      const data = await api('GET', '/status?limit=50&offset=' + offset)
      $('summary').textContent = (data.enabled ? '自动通知已启用' : '自动通知已关闭') +
        (data.paused ? ' · 鉴权异常，发送已暂停' : '') + ' · 待处理 ' + data.pending_count +
        ' · 历史 ' + data.history_count + ' · 存储 ' + (data.bytes / 1024).toFixed(1) + ' KB'
      $('diagnostic').textContent = [data.target_error, data.error].filter(Boolean).join('；')
      render($('queue'), data.rows, true); render($('history'), data.history, false)
      $('previous').disabled = offset === 0
      $('next').disabled = offset + 50 >= Math.max(data.pending_count, data.history_count)
      $('page').textContent = '第 ' + (offset / 50 + 1) + ' 页'
      DriveCat.resize()
    } finally { refreshing = false }
  }

  $('settings-form').addEventListener('input', () => { dirty = true })
  $('settings-form').addEventListener('change', () => { dirty = true })
  $('settings-form').addEventListener('submit', event => {
    event.preventDefault()
    action($('save'), async () => {
      const result = await api('POST', '/config', collect())
      fill(result.config); message('设置已保存', true); await refresh()
    })
  })
  $('test-connection').addEventListener('click', () => action($('test-connection'), async () => {
    const body = collect(); body.enabled = false
    const result = await api('POST', '/test-connection', body)
    message(result.message, result.ok)
  }))
  $('test-scan').addEventListener('click', () => action($('test-scan'), async () => {
    if (dirty) { message('请先保存设置，再提交测试扫描', false); return }
    const path = $('scan-path').value
    if (!path.startsWith('/')) { message('请填写云端绝对目录，例如 /影视/某剧', false); return }
    const result = await api('POST', '/test-scan', { path })
    message(result.message, result.ok || result.queued); await refresh()
  }))
  $('refresh').addEventListener('click', () => action($('refresh'), refresh))
  $('clear-history').addEventListener('click', () => action($('clear-history'), async () => {
    await api('POST', '/history/clear'); offset = 0; await refresh()
  }))
  $('previous').addEventListener('click', () => { offset = Math.max(0, offset - 50); refresh().catch(e => message(e.message, false)) })
  $('next').addEventListener('click', () => { offset += 50; refresh().catch(e => message(e.message, false)) })
  DriveCat.onInit(async () => {
    if (initialized) return
    initialized = true
    try {
      const config = await api('GET', '/config')
      const drives = await api('GET', '/drives')
      fill(config.config, drives.drives); await refresh()
      const timer = setInterval(() => refresh().catch(e => message('状态读取失败：' + e.message, false)), 5000)
      window.addEventListener('pagehide', () => clearInterval(timer), { once: true })
    } catch (error) { initialized = false; message('加载失败：' + error.message, false) }
  })
})()
