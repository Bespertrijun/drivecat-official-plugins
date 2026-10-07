/**
 * qB 种子清理插件设置页。
 *
 * 配置接口：
 *   GET  /qb-cleanup/config
 *   POST /qb-cleanup/config
 *   POST /qb-cleanup/test
 */
;(function () {
  'use strict'

  var state = {
    passwordSet: false,
    saving: false,
    testing: false,
  }

  function $(id) {
    return document.getElementById(id)
  }

  function setStatus(text, kind) {
    var el = $('status')
    el.textContent = text || ''
    el.className = 'status' + (kind ? ' ' + kind : '')
  }

  function makeMappingRow(mapping) {
    mapping = mapping || {}

    var row = document.createElement('div')
    row.className = 'mapping-row'

    var local = document.createElement('input')
    local.type = 'text'
    local.className = 'mapping-local'
    local.placeholder = '/downloads'
    local.value = mapping.local_path || ''
    local.autocomplete = 'off'
    local.spellcheck = false
    local.setAttribute('aria-label', 'DriveCat 路径')

    var qb = document.createElement('input')
    qb.type = 'text'
    qb.className = 'mapping-qb'
    qb.placeholder = '/data'
    qb.value = mapping.qb_path || ''
    qb.autocomplete = 'off'
    qb.spellcheck = false
    qb.setAttribute('aria-label', 'qB 路径')

    var remove = document.createElement('button')
    remove.type = 'button'
    remove.className = 'btn-remove'
    remove.textContent = '删除'
    remove.setAttribute('aria-label', '删除此路径映射')
    remove.addEventListener('click', function () {
      row.remove()
      if (!$('mapping-list').children.length) addMappingRow()
      DriveCat.resize()
    })

    row.appendChild(local)
    row.appendChild(qb)
    row.appendChild(remove)
    return row
  }

  function addMappingRow(mapping) {
    $('mapping-list').appendChild(makeMappingRow(mapping))
    DriveCat.resize()
  }

  function renderMappings(mappings) {
    var list = $('mapping-list')
    list.innerHTML = ''
    if (Array.isArray(mappings) && mappings.length) {
      mappings.forEach(addMappingRow)
    } else {
      addMappingRow()
    }
  }

  function collectMappings() {
    var result = []
    var invalid = false
    $('mapping-list').querySelectorAll('.mapping-row').forEach(function (row) {
      var local = row.querySelector('.mapping-local').value.trim()
      var qb = row.querySelector('.mapping-qb').value.trim()
      if (!local && !qb) return
      if (!local || !qb) {
        invalid = true
        return
      }
      result.push({ local_path: local, qb_path: qb })
    })
    if (invalid) throw new Error('每条路径映射都需要填写两端路径')
    return result
  }

  function collect() {
    var timeout = parseInt($('timeout-seconds').value, 10)
    if (!isFinite(timeout)) timeout = 5
    timeout = Math.max(1, Math.min(10, timeout))

    return {
      enabled: $('enabled').checked,
      base_url: $('base-url').value.trim(),
      username: $('username').value.trim(),
      // 空密码以 null 发送，后端据此保留已保存的 secret。
      password: $('password').value ? $('password').value : null,
      timeout_seconds: timeout,
      path_mappings: collectMappings(),
      watcher_ids: Array.from(document.querySelectorAll('#watcher-list input:checked')).map(function (el) { return Number(el.value) }),
      poll_seconds: Number($('poll-seconds').value),
      quiet_seconds: Number($('quiet-seconds').value),
    }
  }

  function fill(config) {
    config = config || {}
    state.passwordSet = !!config.password_set
    $('enabled').checked = !!config.enabled
    $('base-url').value = config.base_url || ''
    $('username').value = config.username || ''
    $('password').value = ''
    $('password').placeholder = state.passwordSet ? '已设置，留空保持不变' : '请输入 qB 密码'
    $('password-hint').textContent = state.passwordSet
      ? '已保存密码；留空表示保持不变。'
      : '尚未保存密码，请填写后保存。'

    var timeout = parseInt(config.timeout_seconds, 10)
    $('timeout-seconds').value = isFinite(timeout) ? Math.max(1, Math.min(10, timeout)) : 5
    $('poll-seconds').value = config.poll_seconds || 10
    $('quiet-seconds').value = config.quiet_seconds || 60
    state.watcherIds = config.watcher_ids || []
    document.querySelectorAll('#watcher-list input').forEach(function (el) { el.checked = state.watcherIds.indexOf(Number(el.value)) >= 0 })
    renderMappings(config.path_mappings)
  }

  function setBusy(buttonId, busy) {
    $(buttonId).disabled = busy
    $('btn-add-mapping').disabled = state.saving || state.testing
    $('mapping-list').querySelectorAll('input, .btn-remove').forEach(function (el) {
      el.disabled = state.saving || state.testing
    })
  }

  function save() {
    var config
    try {
      config = collect()
    } catch (e) {
      setStatus(e.message, 'err')
      return
    }

    state.saving = true
    setBusy('btn-save', true)
    setBusy('btn-test', true)
    setStatus('保存中...', 'pending')
    DriveCat.api('POST', '/qb-cleanup/config', config)
      .then(function (res) {
        if (res && res.config) fill(res.config)
        setStatus('已保存', 'ok')
        DriveCat.toast('qB 清理设置已保存', 'success')
      })
      .catch(function (e) {
        setStatus('保存失败：' + e.message, 'err')
        DriveCat.toast('qB 清理设置保存失败', 'error')
      })
      .finally(function () {
        state.saving = false
        setBusy('btn-save', false)
        setBusy('btn-test', false)
      })
  }

  function test() {
    var config
    try {
      config = collect()
    } catch (e) {
      setStatus(e.message, 'err')
      return
    }
    if (!config.base_url) {
      setStatus('请先填写 qB 地址', 'err')
      $('base-url').focus()
      return
    }

    state.testing = true
    setBusy('btn-test', true)
    setBusy('btn-save', true)
    setStatus('连接测试中...', 'pending')
    DriveCat.api('POST', '/qb-cleanup/test', config)
      .then(function (res) {
        if (res && res.ok) {
          setStatus('连接成功' + (res.version ? '：' + res.version : ''), 'ok')
          DriveCat.toast('qB 连接成功', 'success')
        } else {
          setStatus('连接失败：' + ((res && res.error) || '未知错误'), 'err')
        }
      })
      .catch(function (e) {
        setStatus('连接失败：' + e.message, 'err')
      })
      .finally(function () {
        state.testing = false
        setBusy('btn-test', false)
        setBusy('btn-save', false)
      })
  }

  var offset = 0
  var labels = { waiting_download: '等待下载完成', waiting_upload: '等待上传完成', quiet: '安静期', deleting: '确认删除结果', needs_inspection: '需要检查', completed: '已完成', cancelled: '已取消', out_of_scope: '已不在所选范围' }
  function refreshStatus() {
    return DriveCat.api('GET', '/qb-cleanup/status?limit=100&offset=' + offset).then(function (res) {
      var retry = res.retry || {}
      $('monitor-summary').textContent = '数据库 ' + (res.bytes / 1024).toFixed(1) + ' KB · ' + Object.keys(res.counts || {}).map(function (k) { return (labels[k] || k) + ' ' + res.counts[k] }).join(' / ') +
        (res.error ? ' · ' + res.error : '') + (retry.error ? ' · ' + (retry.paused ? '需要检查：' : '等待重试：') + retry.error + (retry.next_retry_at ? '，下次 ' + new Date(retry.next_retry_at * 1000).toLocaleString() : '') : '')
      var list = $('task-list')
      list.replaceChildren()
      ;(res.rows || []).forEach(function (row) {
        var item = document.createElement('div')
        item.className = 'task-row'
        var title = document.createElement('strong')
        title.textContent = row.name + ' · ' + (labels[row.status] || row.status) + (row.status === 'quiet' ? ' ' + Math.ceil(row.remaining_seconds) + ' 秒' : '')
        var detail = document.createElement('p')
        detail.textContent = row.reason + ' · 关联任务 ' + row.task_count + ' · 上次检查 ' + new Date((row.last_observed_at || row.updated) * 1000).toLocaleString()
        item.append(title, detail)
        if (row.status !== 'completed' && row.status !== 'cancelled') {
          var cancel = document.createElement('button')
          cancel.type = 'button'; cancel.className = 'btn btn-secondary'; cancel.textContent = '取消此代际清理'
          cancel.addEventListener('click', function () { action(cancel, '/cancel/' + row.key) })
          item.appendChild(cancel)
        }
        list.appendChild(item)
      })
      ;(res.diagnostics || []).forEach(function (row) {
        var detail = document.createElement('p'); detail.textContent = row.hash + '：' + row.reason; list.appendChild(detail)
      })
      if (!list.children.length) list.textContent = '暂无清理记录'
      $('btn-prev').disabled = offset === 0
      $('btn-next').disabled = (res.rows || []).length < 100
      DriveCat.resize()
    }).catch(function (e) { $('monitor-summary').textContent = '读取状态失败：' + e.message })
  }
  function action(button, path) {
    button.disabled = true
    return DriveCat.api('POST', '/qb-cleanup' + path).then(function (res) {
      if (path === '/mapping/check') {
        $('mapping-result').textContent = res.ok ? (res.partial ? '仅显示部分样本；未命中不代表完整扫描结果。\n' : '') + (res.samples || []).map(function (s) { return s.local_path + ' → ' + (s.qb_path || '无映射') + (s.matched ? ' ✓' : ' 未命中') }).join('\n') || '暂无所选 Watcher 的任务样本' : res.error
      }
      return refreshStatus()
    }).catch(function (e) { setStatus(e.message, 'err') }).finally(function () { button.disabled = false; DriveCat.resize() })
  }
  function loadWatchers() {
    return DriveCat.api('GET', '/qb-cleanup/watchers').then(function (res) {
      var list = $('watcher-list'); list.replaceChildren()
      ;(res.watchers || []).forEach(function (w) {
        var label = document.createElement('label'); label.className = 'watcher-choice'
        var box = document.createElement('input'); box.type = 'checkbox'; box.value = w.id
        box.checked = (state.watcherIds || []).indexOf(w.id) >= 0
        var text = document.createElement('span')
        text.textContent = w.name + ' · ' + w.local_path + (!w.is_enabled || w.post_action !== 'delete' ? '（暂不符合：需启用且后处理为删除）' : '')
        label.append(box, text); list.appendChild(label)
      })
      if (!list.children.length) list.textContent = '尚无 Watcher 规则'
      DriveCat.resize()
    }).catch(function (e) { $('watcher-list').textContent = '规则读取失败：' + e.message; $('btn-save').disabled = true })
  }

  var App = {
    load: function () {
      setStatus('加载中...', 'pending')
      DriveCat.api('GET', '/qb-cleanup/config')
        .then(function (res) {
          fill(res && res.config)
          loadWatchers()
          setStatus('', '')
          DriveCat.resize()
        })
        .catch(function (e) {
          renderMappings([])
          setStatus('加载配置失败：' + e.message, 'err')
          DriveCat.resize()
        })
    },
    save: save,
    test: test,
    addMapping: function () { addMappingRow() },
  }

  window.App = App

  DriveCat.onInit(function () {
    $('settings-form').addEventListener('submit', function (e) {
      e.preventDefault()
      save()
    })
    $('btn-add-mapping').addEventListener('click', function () { addMappingRow() })
    $('btn-test').addEventListener('click', test)
    $('btn-check').addEventListener('click', function () { action(this, '/check') })
    $('btn-clear').addEventListener('click', function () { action(this, '/history/clear') })
    $('btn-mapping').addEventListener('click', function () { action(this, '/mapping/check') })
    $('btn-prev').addEventListener('click', function () { offset = Math.max(0, offset - 100); refreshStatus() })
    $('btn-next').addEventListener('click', function () { offset += 100; refreshStatus() })
    refreshStatus()
    setInterval(function () { if (!document.hidden) refreshStatus() }, 10000)
    App.load()
  })
})()
