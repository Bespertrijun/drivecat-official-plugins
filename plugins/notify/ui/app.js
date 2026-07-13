/**
 * 通知系统插件 UI 逻辑。
 *
 * 通过共享 SDK 与宿主通信：
 *   - 载入时 GET /notify/config 回填表单
 *   - 保存 POST /notify/config
 *   - 测试 POST /notify/test（用当前表单里的即时配置，无需先保存）
 */
;(function () {
  'use strict'

  var EVENTS = ['after_upload', 'after_sync', 'on_error', 'on_startup']

  function $(id) { return document.getElementById(id) }

  // 从表单收集配置对象
  function collect() {
    var events = {}
    EVENTS.forEach(function (k) { events[k] = $('ev-' + k).checked })
    return {
      channels: {
        telegram: {
          enabled: $('tg-enabled').checked,
          bot_token: $('tg-token').value.trim(),
          chat_id: $('tg-chat').value.trim(),
          parse_mode: $('tg-parse').value,
        },
      },
      events: events,
    }
  }

  // 用配置回填表单
  function fill(cfg) {
    cfg = cfg || {}
    var tg = (cfg.channels && cfg.channels.telegram) || {}
    $('tg-enabled').checked = !!tg.enabled
    $('tg-token').value = tg.bot_token || ''
    $('tg-chat').value = tg.chat_id || ''
    $('tg-parse').value = tg.parse_mode != null ? tg.parse_mode : 'HTML'

    var ev = cfg.events || {}
    EVENTS.forEach(function (k) { $('ev-' + k).checked = !!ev[k] })
    syncEnabledState()
  }

  // 根据开关淡化/激活 Telegram 正文
  function syncEnabledState() {
    $('tg-body').classList.toggle('disabled', !$('tg-enabled').checked)
  }

  function setStatus(id, text, kind) {
    var el = $(id)
    el.textContent = text || ''
    el.className = 'status' + (kind ? ' ' + kind : '')
  }

  var App = {
    load: function () {
      DriveCat.api('GET', '/notify/config')
        .then(function (res) {
          fill(res.config)
          DriveCat.resize()
        })
        .catch(function (e) {
          setStatus('save-status', '加载配置失败：' + e.message, 'err')
        })
    },

    save: function () {
      var btn = $('btn-save')
      btn.disabled = true
      setStatus('save-status', '保存中...', 'pending')
      DriveCat.api('POST', '/notify/config', collect())
        .then(function () {
          setStatus('save-status', '✓ 已保存', 'ok')
          DriveCat.toast('通知设置已保存', 'success')
        })
        .catch(function (e) {
          setStatus('save-status', '保存失败：' + e.message, 'err')
          DriveCat.toast('保存失败', 'error')
        })
        .finally(function () { btn.disabled = false })
    },

    test: function () {
      var btn = $('btn-test')
      var cfg = collect().channels.telegram
      if (!cfg.bot_token || !cfg.chat_id) {
        setStatus('tg-status', '请先填写 Bot Token 和 Chat ID', 'err')
        return
      }
      btn.disabled = true
      setStatus('tg-status', '发送中...', 'pending')
      DriveCat.api('POST', '/notify/test', { channel: 'telegram', telegram: cfg })
        .then(function (res) {
          if (res.ok) {
            setStatus('tg-status', '✓ 测试消息已发送，请查收', 'ok')
            DriveCat.toast('测试消息已发送', 'success')
          } else {
            setStatus('tg-status', '失败：' + (res.error || '未知错误'), 'err')
          }
        })
        .catch(function (e) {
          setStatus('tg-status', '失败：' + e.message, 'err')
        })
        .finally(function () { btn.disabled = false })
    },
  }

  window.App = App

  DriveCat.onInit(function () {
    $('tg-enabled').addEventListener('change', syncEnabledState)
    App.load()
  })
})()
