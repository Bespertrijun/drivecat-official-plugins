/**
 * 批量重命名插件 — 前端逻辑
 *
 * 入口模式：
 *   1. 独立入口 → 显示 Step 1（选网盘 + 选文件）
 *   2. 右键入口 → 跳过 Step 1，直接进 Step 2
 */
;(function () {
  'use strict'

  var API_BASE = window.location.origin
  var _token = ''

  // 捕获宿主注入的鉴权 token
  window.addEventListener('message', function (e) {
    if (e.data && e.data.protocol === 'drivecat.plugin.v1' && e.data.type === 'host.init') {
      _token = e.data.payload.token || ''
    }
  })

  // ── State ──
  var state = {
    // 模式: 'standalone' | 'contextmenu'
    mode: 'standalone',
    currentStep: 1,
    // Step 1
    driveId: 0,
    parentId: '0',
    selectedFileIds: [],  // 选中的文件 ID
    scopeMode: 'dir',     // 'dir' | 'files'
    breadcrumb: [{ id: '0', name: '根目录' }],
    files: [],
    // Step 2
    rules: [],
    templates: [],
    // Step 3
    concurrency: 10,
    pauseMs: 1000,
    executing: false,
  }

  // ── SDK Init ──
  DriveCat.onInit(function (ctx) {
    state.driveId = ctx.drive_id || 0
    state.parentId = ctx.parent_id || '0'

    var selected = ctx.selected_file
    if (selected) {
      // 右键入口
      state.mode = 'contextmenu'
      if (selected.is_dir) {
        state.parentId = selected.id
        state.scopeMode = 'dir'
      } else {
        state.scopeMode = 'files'
        state.selectedFileIds = [selected.id]
      }
      // 跳过 Step 1
      goStep(2)
    } else if (state.driveId) {
      // 有 driveId 但没有 selected_file（可能从插件页面进入但已有上下文）
      state.mode = 'standalone'
      loadDrives()
    } else {
      state.mode = 'standalone'
      loadDrives()
    }

    loadTemplates()
    DriveCat.resize(900)
  })

  // ══════════════════════════════════
  //  Step Navigation
  // ══════════════════════════════════

  function goStep(n) {
    // 从 Step 1 → 2 时需要校验
    if (n === 2 && state.currentStep === 1) {
      if (!state.driveId) {
        DriveCat.toast('请先选择网盘', 'warning')
        return
      }
      if (state.selectedFileIds.length === 0) {
        DriveCat.toast('请至少选择一个文件或目录', 'warning')
        return
      }
    }

    state.currentStep = n

    // 切面板
    document.querySelectorAll('.step-panel').forEach(function (el) {
      el.classList.toggle('active', el.id === 'step-' + n)
    })

    // 切导航高亮
    document.querySelectorAll('.steps-bar .step').forEach(function (el) {
      var s = parseInt(el.getAttribute('data-step'))
      el.classList.remove('active', 'done')
      if (s === n) el.classList.add('active')
      else if (s < n) el.classList.add('done')
    })

    // 进入 Step 2 时更新 scope 提示 + 触发预览
    if (n === 2) {
      updateScopeHint('step2-scope')
      if (state.rules.length === 0) {
        state.rules.push({ type: 'replace', params: {} })
      }
      renderRules()
      triggerPreview()
    }

    // 进入 Step 3 时更新提示并拉取最终预览
    if (n === 3) {
      updateScopeHint('step3-scope')
      doExecutionPreview()
    }

    DriveCat.resize(900)
  }

  function updateScopeHint(elementId) {
    var el = document.getElementById(elementId)
    if (!el) return
    if (state.scopeMode === 'files') {
      el.textContent = '目标：' + state.selectedFileIds.length + ' 个选中文件'
    } else {
      el.textContent = '目标：目录内所有文件'
    }
  }

  // ══════════════════════════════════
  //  Step 1: Drive & File Selection
  // ══════════════════════════════════

  function loadDrives() {
    fetchCore('/api/drives/').then(function (drives) {
      var sel = document.getElementById('drive-select')
      sel.innerHTML = '<option value="">— 请选择网盘 —</option>'
      drives.forEach(function (d) {
        var opt = document.createElement('option')
        opt.value = d.id
        opt.textContent = d.name + ' (' + d.drive_type + ')'
        if (d.id === state.driveId) opt.selected = true
        sel.appendChild(opt)
      })
      sel.onchange = function () {
        state.driveId = parseInt(this.value) || 0
        state.parentId = '0'
        state.breadcrumb = [{ id: '0', name: '根目录' }]
        state.selectedFileIds = []
        state.scopeMode = 'dir'
        if (state.driveId) browseFiles()
        else renderFileList([])
        updateSelectionInfo()
      }
      if (state.driveId) browseFiles()
    })
  }

  function browseFiles() {
    if (!state.driveId) return
    var url = '/api/drives/' + state.driveId + '/files?parent_id=' + encodeURIComponent(state.parentId)
    fetchCore(url).then(function (data) {
      state.files = data.files || data || []
      renderFileList(state.files)
      renderBreadcrumb()
      DriveCat.resize(900)
    }).catch(function () {
      renderFileList([])
    })
  }

  function renderFileList(files) {
    var container = document.getElementById('file-list')
    if (!files || files.length === 0) {
      container.innerHTML = '<div class="status-msg">空目录</div>'
      return
    }

    // 文件夹排前面
    var sorted = files.slice().sort(function (a, b) {
      if (a.is_dir && !b.is_dir) return -1
      if (!a.is_dir && b.is_dir) return 1
      return a.name.localeCompare(b.name)
    })

    var html = ''
    sorted.forEach(function (f) {
      var icon = f.is_dir ? '📁' : '📄'
      var size = f.is_dir ? '' : formatSize(f.size)
      var checked = state.selectedFileIds.indexOf(f.id) >= 0
      html += '<div class="file-item' + (checked ? ' selected' : '') + '" data-id="' + f.id + '" data-dir="' + f.is_dir + '">'
      html += '<input type="checkbox" class="file-check"' + (checked ? ' checked' : '') + '>'
      html += '<span class="file-icon">' + icon + '</span>'
      html += '<span class="file-name">' + esc(f.name) + '</span>'
      html += '<span class="file-size">' + size + '</span>'
      html += '</div>'
    })
    container.innerHTML = html

    // 绑定事件
    container.querySelectorAll('.file-item').forEach(function (el) {
      el.addEventListener('click', function (e) {
        var id = el.getAttribute('data-id')
        var isDir = el.getAttribute('data-dir') === 'true'

        var clickedCheckbox = (e.target.tagName === 'INPUT' && e.target.type === 'checkbox')

        if (isDir && !clickedCheckbox) {
          // 点击文件夹非选框区域 → 进入目录
          var file = sorted.find(function (f) { return f.id === id })
          state.parentId = id
          state.breadcrumb.push({ id: id, name: file ? file.name : id })
          browseFiles()
        } else {
          // 点击文件整行，或点击了文件夹的复选框 → 勾选/取消
          e.preventDefault()
          var idx = state.selectedFileIds.indexOf(id)
          if (idx >= 0) {
            state.selectedFileIds.splice(idx, 1)
          } else {
            state.selectedFileIds.push(id)
          }
          state.scopeMode = state.selectedFileIds.length > 0 ? 'files' : 'dir'
          renderFileList(state.files)
          updateSelectionInfo()
        }
      })
    })
  }

  function renderBreadcrumb() {
    var container = document.getElementById('breadcrumb')
    var html = ''
    state.breadcrumb.forEach(function (c, i) {
      if (i > 0) html += '<span class="sep">/</span>'
      html += '<span class="crumb" data-id="' + c.id + '">' + esc(c.name) + '</span>'
    })
    container.innerHTML = html

    container.querySelectorAll('.crumb').forEach(function (el, i) {
      el.addEventListener('click', function () {
        state.parentId = el.getAttribute('data-id')
        state.breadcrumb = state.breadcrumb.slice(0, i + 1)
        browseFiles()
      })
    })
  }

  function updateSelectionInfo() {
    var el = document.getElementById('selection-info')
    var text = document.getElementById('selection-text')
    if (state.selectedFileIds.length > 0) {
      el.style.display = 'flex'
      text.textContent = '已选择 ' + state.selectedFileIds.length + ' 个文件'
    } else {
      el.style.display = 'none'
    }
  }

  function clearSelection() {
    state.selectedFileIds = []
    state.scopeMode = 'dir'
    renderFileList(state.files)
    updateSelectionInfo()
  }

  // ══════════════════════════════════
  //  Step 2: Rules & Preview
  // ══════════════════════════════════

  var RULE_CONFIG = {
    replace: {
      label: '查找替换',
      fields: [
        { key: 'pattern', label: '查找', placeholder: '要替换的文本' },
        { key: 'replacement', label: '替换为', placeholder: '新文本' }
      ]
    },
    regex: {
      label: '正则替换',
      fields: [
        { key: 'pattern', label: '正则', placeholder: '例: \\[.*?\\]' },
        { key: 'replacement', label: '替换为', placeholder: '例: EP$1' },
        { key: 'flags', label: '标志', placeholder: 'i/m/s' }
      ]
    },
    insert: {
      label: '插入文本',
      fields: [
        { key: 'text', label: '文本', placeholder: '要插入的文本' },
        { key: 'position', label: '位置', placeholder: '0=开头 -1=末尾', type: 'number' }
      ]
    },
    delete: {
      label: '删除字符',
      fields: [
        { key: 'target', label: '目标', placeholder: '要删除的文本' }
      ]
    },
    sequence: {
      label: '添加序号',
      fields: [
        { key: 'start_num', label: '起始', type: 'number', placeholder: '1' },
        { key: 'step', label: '步长', type: 'number', placeholder: '1' },
        { key: 'padding', label: '补零位数', type: 'number', placeholder: '3' }
      ]
    },
    pad: {
      label: '数字补零',
      fields: [
        { key: 'target_digits', label: '目标位数', type: 'number', placeholder: '3' }
      ]
    },
    case: {
      label: '大小写转换',
      fields: [
        { key: 'case_type', label: '类型', options: ['lower', 'upper', 'title', 'capitalize', 'swap'] }
      ]
    },
    date: {
      label: '添加日期',
      fields: [
        { key: 'format', label: '格式', placeholder: '%Y%m%d' },
        { key: 'position', label: '位置', placeholder: '0=开头 -1=末尾', type: 'number' }
      ]
    }
  }

  function addRule() {
    var type = document.getElementById('new-rule-type').value
    state.rules.push({ type: type, params: {} })
    renderRules()
    triggerPreview()
  }

  function removeRule(idx) {
    state.rules.splice(idx, 1)
    renderRules()
    triggerPreview()
  }

  function renderRules() {
    var list = document.getElementById('rules-list')
    if (state.rules.length === 0) {
      list.innerHTML = '<div class="status-msg" style="padding:12px">暂无规则</div>'
      DriveCat.resize(900)
      return
    }

    var html = ''
    state.rules.forEach(function (rule, i) {
      var config = RULE_CONFIG[rule.type]
      html += '<div class="rule-card">'
      html += '<div class="rule-card-header">'
      html += '<span class="rule-card-title">' + config.label + '</span>'
      html += '<button class="btn-remove" data-idx="' + i + '">×</button>'
      html += '</div>'

      config.fields.forEach(function (f) {
        var val = rule.params[f.key] || ''
        html += '<div class="rule-field">'
        html += '<label>' + f.label + '</label>'
        if (f.options) {
          html += '<select data-rule="' + i + '" data-key="' + f.key + '">'
          f.options.forEach(function (o) {
            html += '<option value="' + o + '"' + (val === o ? ' selected' : '') + '>' + o + '</option>'
          })
          html += '</select>'
        } else {
          html += '<input type="' + (f.type || 'text') + '" placeholder="' + (f.placeholder || '') + '" value="' + esc(String(val)) + '" data-rule="' + i + '" data-key="' + f.key + '">'
        }
        html += '</div>'
      })
      html += '</div>'
    })
    list.innerHTML = html

    // 绑定事件
    list.querySelectorAll('.btn-remove').forEach(function (btn) {
      btn.addEventListener('click', function () { removeRule(parseInt(btn.getAttribute('data-idx'))) })
    })
    list.querySelectorAll('input, select').forEach(function (el) {
      el.addEventListener('input', function () {
        var ri = parseInt(el.getAttribute('data-rule'))
        var key = el.getAttribute('data-key')
        if (ri >= 0 && key) {
          state.rules[ri].params[key] = el.value
          triggerPreview()
        }
      })
    })
    DriveCat.resize(900)
  }

  var previewTimer = null
  function triggerPreview() {
    clearTimeout(previewTimer)
    previewTimer = setTimeout(doPreview, 400)
  }

  function doPreview() {
    var area = document.getElementById('preview-area')
    var ruleSpecs = getRuleSpecs()
    if (ruleSpecs.length === 0) {
      area.innerHTML = '<div class="status-msg">添加规则后自动预览</div>'
      return
    }

    area.style.opacity = '0.5'
    DriveCat.api('POST', '/rename/preview', buildRequestBody())
      .then(function (res) {
        var previews = res.previews || []
        if (previews.length === 0) {
          area.innerHTML = '<div class="status-msg">没有匹配的文件</div>'
          return
        }
        var html = ''
        previews.forEach(function (p) {
          var changed = p.changed || p.original_name !== p.new_name
          html += '<div class="preview-row">'
          html += '<span class="preview-old">' + esc(p.original_name) + '</span>'
          html += '<span class="preview-arrow">→</span>'
          html += '<span class="preview-new' + (changed ? ' changed' : '') + '">' + esc(p.new_name) + '</span>'
          html += '</div>'
        })
        area.innerHTML = html
      })
      .catch(function (e) {
        area.innerHTML = '<div class="status-msg" style="color:var(--dc-error)">预览失败: ' + e.message + '</div>'
      })
      .finally(function () {
        area.style.opacity = '1'
        DriveCat.resize(900)
      })
  }

  function doExecutionPreview() {
    var area = document.getElementById('execution-preview')
    area.innerHTML = '<div class="status-msg">正在加载最终预览...</div>'
    var ruleSpecs = getRuleSpecs()
    if (ruleSpecs.length === 0) {
      area.innerHTML = '<div class="status-msg">没有配置任何规则</div>'
      return
    }

    DriveCat.api('POST', '/rename/preview', buildRequestBody())
      .then(function (res) {
        var previews = res.previews || []
        var changedCount = previews.filter(function(p) { return p.changed || p.original_name !== p.new_name }).length
        var html = '<div class="status-msg" style="padding:12px; border-bottom:1px solid var(--dc-border); font-weight:600;">预计将重命名 ' + changedCount + ' 个文件</div>'
        
        previews.forEach(function (p) {
          var changed = p.changed || p.original_name !== p.new_name
          html += '<div class="preview-row">'
          html += '<span class="preview-old">' + esc(p.original_name) + '</span>'
          html += '<span class="preview-arrow">→</span>'
          html += '<span class="preview-new' + (changed ? ' changed' : '') + '">' + esc(p.new_name) + '</span>'
          html += '</div>'
        })
        area.innerHTML = html
      })
      .catch(function (e) {
        area.innerHTML = '<div class="status-msg" style="color:var(--dc-error)">预览加载失败: ' + e.message + '</div>'
      })
      .finally(function() {
        DriveCat.resize(900)
      })
  }

  // ── Templates ──

  function loadTemplates() {
    DriveCat.api('GET', '/rename/templates')
      .then(function (res) {
        state.templates = res.templates || []
        renderTemplateSelect()
      })
      .catch(function () { /* ignore */ })
  }

  function renderTemplateSelect() {
    var sel = document.getElementById('template-select')
    sel.innerHTML = '<option value="">— 选择模板 —</option>'
    state.templates.forEach(function (t, i) {
      var opt = document.createElement('option')
      opt.value = i
      opt.textContent = t.name
      sel.appendChild(opt)
    })
  }

  function loadTemplate() {
    var sel = document.getElementById('template-select')
    var idx = parseInt(sel.value)
    if (isNaN(idx) || !state.templates[idx]) {
      DriveCat.toast('请先选择一个模板', 'warning')
      return
    }
    state.rules = JSON.parse(JSON.stringify(state.templates[idx].rules))
    renderRules()
    triggerPreview()
    DriveCat.toast('已加载模板: ' + state.templates[idx].name, 'success')
  }

  function saveTemplate() {
    var ruleSpecs = getRuleSpecs()
    if (ruleSpecs.length === 0) {
      DriveCat.toast('请先添加规则', 'warning')
      return
    }
    var overlay = document.getElementById('modal-overlay')
    var input = document.getElementById('modal-input')
    input.value = ''
    overlay.style.display = 'flex'
    input.focus()
  }

  function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none'
  }

  function confirmModal() {
    var name = document.getElementById('modal-input').value
    if (!name || !name.trim()) {
      DriveCat.toast('请输入模板名称', 'warning')
      return
    }
    closeModal()

    var ruleSpecs = getRuleSpecs()
    DriveCat.api('POST', '/rename/templates', {
      name: name.trim(),
      rules: ruleSpecs,
    }).then(function () {
      DriveCat.toast('模板已保存', 'success')
      loadTemplates()
    }).catch(function () {
      DriveCat.toast('保存失败', 'error')
    })
  }

  // ══════════════════════════════════
  //  Step 3: Execute with SSE
  // ══════════════════════════════════

  function doExecute() {
    if (state.executing) return
    var ruleSpecs = getRuleSpecs()
    if (ruleSpecs.length === 0) {
      DriveCat.toast('请先配置规则', 'error')
      return
    }

    state.executing = true
    state.concurrency = parseInt(document.getElementById('concurrency').value) || 10
    state.pauseMs = parseInt(document.getElementById('pause-ms').value)
    if (isNaN(state.pauseMs) || state.pauseMs < 0) state.pauseMs = 1000

    document.getElementById('btn-execute').disabled = true
    document.getElementById('btn-execute').textContent = '执行中...'
    document.getElementById('btn-back-2').disabled = true
    document.getElementById('progress-section').style.display = 'block'
    document.getElementById('progress-log').innerHTML = ''
    document.getElementById('progress-fill').style.width = '0%'
    document.getElementById('progress-stats').textContent = '连接中...'

    var body = buildRequestBody()
    body.concurrency = state.concurrency
    body.pause_ms = state.pauseMs

    var pluginId = (DriveCat.getContext() || {}).plugin_id || ''
    var url = API_BASE + '/api/plugins/' + pluginId + '/rename/execute'

    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _token },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) {
        return response.text().then(function (text) {
          throw new Error('HTTP ' + response.status + ': ' + (text || response.statusText))
        })
      }
      var reader = response.body.getReader()
      var decoder = new TextDecoder()
      var buffer = ''
      var total = 0
      var done_count = 0
      var success = 0
      var failed = 0
      var finished = false

      function finishOnce(s, f, k) {
        if (finished) return
        finished = true
        finishExecution(s, f, k)
      }

      function pump() {
        return reader.read().then(function (result) {
          if (finished) return
          if (result.done) {
            finishOnce(success, failed, total - success - failed)
            return
          }
          buffer += decoder.decode(result.value, { stream: true })
          var lines = buffer.split('\n')
          buffer = lines.pop() // 留下不完整的行

          lines.forEach(function (line) {
            line = line.trim()
            if (!line.startsWith('data: ')) return
            var payload = line.slice(6)
            if (payload === '[DONE]') return

            try {
              var event = JSON.parse(payload)
              if (event.type === 'start') {
                total = event.total
                document.getElementById('progress-stats').textContent = '0 / ' + total
              } else if (event.type === 'progress') {
                done_count++
                if (event.status === 'success') success++
                if (event.status === 'failed') failed++
                var pct = Math.round((done_count / total) * 100)
                document.getElementById('progress-fill').style.width = pct + '%'
                document.getElementById('progress-stats').textContent = done_count + ' / ' + total + '  (' + pct + '%)'
                appendLog(event)
              } else if (event.type === 'done') {
                finishOnce(event.success, event.failed, event.skipped)
              }
            } catch (e) { /* ignore parse errors */ }
          })

          if (finished) return
          return pump()
        })
      }

      return pump()
    }).catch(function (e) {
      DriveCat.toast('执行失败: ' + e.message, 'error')
      resetExecutionUI()
    })
  }

  function resetExecutionUI() {
    state.executing = false
    document.getElementById('btn-execute').disabled = false
    document.getElementById('btn-execute').textContent = '🚀 开始重命名'
    document.getElementById('btn-back-2').disabled = false
  }

  function appendLog(event) {
    var log = document.getElementById('progress-log')
    var div = document.createElement('div')
    div.className = 'log-item'
    div.innerHTML = '<span>' + esc(event.original) + ' → ' + esc(event.new) + '</span>'
      + '<span class="log-status ' + event.status + '">' + event.status + '</span>'
    log.appendChild(div)
    log.scrollTop = log.scrollHeight
  }

  function finishExecution(success, failed, skipped) {
    resetExecutionUI()
    var msg = '完成！成功 ' + success
    if (failed > 0) msg += '，失败 ' + failed
    if (skipped > 0) msg += '，跳过 ' + skipped
    DriveCat.toast(msg, failed > 0 ? 'warning' : 'success')
  }

  // ══════════════════════════════════
  //  Helpers
  // ══════════════════════════════════

  function getRuleSpecs() {
    return state.rules.map(function (r) {
      var params = {}
      for (var k in r.params) params[k] = r.params[k]
      // 数字转换
      for (var k2 in params) {
        if (typeof params[k2] === 'string' && /^-?\d+$/.test(params[k2])) {
          params[k2] = parseInt(params[k2], 10)
        }
      }
      return { type: r.type, params: params }
    }).filter(function (r) {
      var hasVal = false
      for (var k in r.params) if (r.params[k] !== '') hasVal = true
      return hasVal || r.type === 'case' || r.type === 'sequence'
    })
  }

  function buildRequestBody() {
    var body = {
      drive_config_id: state.driveId,
      parent_id: state.parentId,
      rules: getRuleSpecs(),
    }
    if (state.scopeMode === 'files' && state.selectedFileIds.length > 0) {
      body.file_ids = state.selectedFileIds
    }
    return body
  }

  /** 调用 DriveCat 核心 API（不走插件前缀） */
  function fetchCore(path) {
    return fetch(API_BASE + path, {
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _token },
    }).then(function (r) {
      if (!r.ok) throw new Error('API ' + r.status)
      return r.json()
    })
  }

  function formatSize(bytes) {
    if (!bytes) return ''
    var units = ['B', 'KB', 'MB', 'GB']
    var i = 0
    var b = bytes
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++ }
    return b.toFixed(i > 0 ? 1 : 0) + ' ' + units[i]
  }

  function esc(s) {
    if (!s) return ''
    var d = document.createElement('div')
    d.textContent = s
    return d.innerHTML
  }

  // ── Expose to HTML onclick handlers ──
  window.App = {
    goStep: goStep,
    addRule: addRule,
    clearSelection: clearSelection,
    loadTemplate: loadTemplate,
    saveTemplate: saveTemplate,
    closeModal: closeModal,
    confirmModal: confirmModal,
    doExecute: doExecute,
  }

  // 绑定 Enter 键保存模板
  document.getElementById('modal-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      confirmModal()
    } else if (e.key === 'Escape') {
      closeModal()
    }
  })
})()
