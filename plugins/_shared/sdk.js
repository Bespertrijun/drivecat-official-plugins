/**
 * DriveCat Plugin SDK v1
 *
 * 用法：在插件 UI 的 HTML 中引入此脚本，然后调用 DriveCat.onInit(callback)。
 *
 *   <script src="../_shared/sdk.js"></script>
 *   <script>
 *     DriveCat.onInit((ctx) => {
 *       // ctx.drive_id, ctx.parent_id, ctx.selected_file, ctx.plugin_id ...
 *     })
 *   </script>
 *
 * SDK 会自动处理：
 *   - host.init 握手（token + cssVars 注入）
 *   - DriveCat.api(method, path, body) — 带鉴权的插件 API 调用
 *   - DriveCat.toast(message, type) — 宿主 toast 通知
 *   - DriveCat.resize() — 通知宿主调整 iframe 高度
 *   - DriveCat.close() — 通知宿主关闭插件
 */
;(function () {
  'use strict'

  let _token = ''
  let _context = {}
  let _initCallback = null
  const API_BASE = window.location.origin

  // ── 监听宿主 host.init ──
  window.addEventListener('message', function (e) {
    if (!e.data || e.data.protocol !== 'drivecat.plugin.v1') return
    if (e.data.type === 'host.init') {
      _token = e.data.payload.token
      _context = e.data.payload.context || {}

      // 注入宿主 CSS 变量
      var cssVars = e.data.payload.cssVars
      if (cssVars) {
        var root = document.documentElement
        for (var key in cssVars) {
          if (cssVars.hasOwnProperty(key)) {
            root.style.setProperty(key, cssVars[key])
          }
        }
      }

      if (typeof _initCallback === 'function') {
        _initCallback(_context)
      }
    }
  })

  // ── 公开 API ──
  window.DriveCat = {
    /**
     * 注册初始化回调。host.init 完成后会调用 callback(context)。
     */
    onInit: function (callback) {
      _initCallback = callback
    },

    /**
     * 获取当前上下文。
     */
    getContext: function () {
      return _context
    },

    /**
     * 发起插件 API 请求。路径自动添加 /api/plugins/{plugin_id} 前缀。
     * @param {string} method - HTTP 方法
     * @param {string} path - 相对路径，如 /rename/preview
     * @param {object} [body] - 请求体
     * @returns {Promise<any>}
     */
    api: function (method, path, body) {
      var pluginId = _context.plugin_id || ''
      return fetch(API_BASE + '/api/plugins/' + pluginId + path, {
        method: method,
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + _token,
        },
        body: body ? JSON.stringify(body) : undefined,
      }).then(function (r) {
        if (!r.ok) throw new Error('API ' + r.status)
        return r.json()
      })
    },

    /**
     * 在宿主显示 toast 通知。
     * @param {string} message
     * @param {'info'|'success'|'error'|'warning'} [type='info']
     */
    toast: function (message, type) {
      window.parent.postMessage(
        {
          protocol: 'drivecat.plugin.v1',
          type: 'plugin.toast',
          payload: { message: message, type: type || 'info' },
        },
        '*'
      )
    },

    /**
     * 通知宿主调整 iframe 高度。
     * @param {number} [maxHeight=700] - 最大高度
     */
    resize: function (maxHeight) {
      var h = document.documentElement.scrollHeight
      var max = maxHeight || 700
      window.parent.postMessage(
        {
          protocol: 'drivecat.plugin.v1',
          type: 'plugin.resize',
          payload: { height: Math.min(h + 20, max) },
        },
        '*'
      )
    },

    /**
     * 通知宿主关闭插件面板。
     */
    close: function () {
      window.parent.postMessage(
        {
          protocol: 'drivecat.plugin.v1',
          type: 'plugin.close',
        },
        '*'
      )
    },
  }
})()
