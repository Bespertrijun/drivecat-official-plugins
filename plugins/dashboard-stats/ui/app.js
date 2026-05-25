;(function () {
  'use strict'

  // ── DOM refs ──
  var UI = {
    app:      document.getElementById('app'),
    loader:   document.getElementById('loader'),
    content:  document.getElementById('content'),
    main:     document.getElementById('main-area'),
    footer:   document.getElementById('footer-stats'),
    title:    document.getElementById('header-title'),
    icon:     document.getElementById('header-icon'),
  }

  // ── 内联 SVG 图标（无外部依赖） ──
  var ICONS = {
    cloud:
      '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/>' +
      '</svg>',
    drive:
      '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M7.71 3.5h8.58l5.21 9.02L17.21 21.5H6.79L2.5 12.52 7.71 3.5zm.86 1.5L4.21 12.52 7.43 18.5l4.07-7.05L8.57 5zM12 12.45L15.86 19h-7.72L12 12.45zM13.43 11.5l4.07-7.05h-5.43l4.07 7.05h-2.71z"/>' +
      '</svg>',
  }

  // ── 字节格式化 ──
  function formatBytes(n, decimals) {
    if (!n || n <= 0) return '0 B'
    var k = 1024
    var sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    var i = Math.min(sizes.length - 1, Math.floor(Math.log(n) / Math.log(k)))
    var v = n / Math.pow(k, i)
    var d = (decimals != null) ? decimals : (i === 0 ? 0 : (v >= 100 ? 0 : 1))
    return v.toFixed(d) + ' ' + sizes[i]
  }

  // ── 按 range 格式化 bucket key ──
  function formatLabel(iso, range) {
    var p = iso.split('-')
    if (range === 'year')  return p[0]                                    // '2026'
    if (range === 'month') return parseInt(p[1], 10) + '月'                // '5月'
    return parseInt(p[1], 10) + '/' + parseInt(p[2], 10)                  // '5/8'
  }

  // 全局状态
  var STATE = { range: 'day', quotaRange: 'day', selectedDrives: null, quotaData: null }
  var CACHE = { uploads: {}, quotas: {} }


  // ── 数字增长动画 ──
  function animateBytes(el, target, duration) {
    var start = 0
    var t0 = null
    function step(ts) {
      if (t0 == null) t0 = ts
      var p = Math.min(1, (ts - t0) / duration)
      // ease-out cubic
      var eased = 1 - Math.pow(1 - p, 3)
      el.textContent = formatBytes(Math.round(start + (target - start) * eased))
      if (p < 1) window.requestAnimationFrame(step)
    }
    window.requestAnimationFrame(step)
  }

  // ── 卡片分流 ──
  function resolveCard(ctx) {
    var qs = new URLSearchParams(window.location.search)
    var c = qs.get('card')
    if (c) return c
    var h = window.location.hash.replace(/^#/, '')
    if (h && /^card=/.test(h)) return h.split('=')[1]
    if (ctx && ctx.match && ctx.match.card) return ctx.match.card
    return null
  }

  // ── 上传卡渲染 ──
  function renderUploads(data) {
    UI.title.textContent = '上传统计'
    UI.icon.innerHTML = ICONS.cloud
    UI.footer.hidden = false
    UI.footer.classList.remove('stats-col')

    var range = data.range || STATE.range
    STATE.range = range
    var series = data.series || []

    // Range 切换条 + 图表容器 + tooltip 元素（独立于 series 长度，始终渲染）
    var toggleHtml =
      '<div class="range-toggle" role="tablist">' +
      ['day:日', 'month:月', 'year:年'].map(function (s) {
        var parts = s.split(':')
        var active = parts[0] === range ? ' active' : ''
        return '<button class="range-btn' + active + '" data-range="' + parts[0] + '" type="button">' + parts[1] + '</button>'
      }).join('') +
      '</div>'

    if (series.length < 2) {
      UI.main.innerHTML = toggleHtml + '<div class="flex-center">数据不足</div>'
      bindRangeToggle()
      return
    }

    // 坐标系
    var W = 400, H = 150
    var pad = { top: 14, right: 8, bottom: 18, left: 8 }
    var iw = W - pad.left - pad.right
    var ih = H - pad.top - pad.bottom

    var maxB = 0
    for (var i = 0; i < series.length; i++) {
      if (series[i].bytes > maxB) maxB = series[i].bytes
    }
    if (maxB === 0) maxB = 1

    var pts = series.map(function (d, idx) {
      var x = pad.left + idx * (iw / (series.length - 1))
      var y = pad.top + ih - (d.bytes / maxB) * ih
      return { x: x, y: y, d: d }
    })

    function pathD() {
      var s = 'M' + pts[0].x.toFixed(2) + ',' + pts[0].y.toFixed(2)
      for (var i = 1; i < pts.length; i++) {
        s += ' L' + pts[i].x.toFixed(2) + ',' + pts[i].y.toFixed(2)
      }
      return s
    }
    function areaD() {
      return pathD() +
        ' L' + pts[pts.length - 1].x.toFixed(2) + ',' + (pad.top + ih) +
        ' L' + pts[0].x.toFixed(2) + ',' + (pad.top + ih) + ' Z'
    }

    // X 轴 mini label：日 4 个 / 月 4 个 / 年所有 5 个
    var tickIdx
    if (range === 'year') {
      tickIdx = []
      for (var yi = 0; yi < pts.length; yi++) tickIdx.push(yi)
    } else if (range === 'month') {
      tickIdx = [0, 3, 7, 11]
    } else {
      tickIdx = [0, 7, 14, 21, 28]
    }
    var ticks = []
    for (var t = 0; t < tickIdx.length; t++) {
      var p = pts[tickIdx[t]]
      if (!p) continue
      ticks.push(
        '<text class="chart-label" x="' + p.x.toFixed(1) +
        '" y="' + (H - 4) + '" text-anchor="middle">' +
        formatLabel(p.d.date, range) + '</text>'
      )
    }

    var last = pts[pts.length - 1]

    // SVG（注意 stop-color 用 style 属性，CSS var 才能解析）
    UI.main.innerHTML =
      toggleHtml +
      '<div class="chart-container">' +
        '<svg class="chart-svg" viewBox="0 0 ' + W + ' ' + H +
        '" preserveAspectRatio="none" role="img" aria-label="上传趋势 - ' + range + '">' +
          '<defs>' +
            '<linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1">' +
              '<stop offset="0%"   style="stop-color: var(--dc-primary); stop-opacity: 0.28"/>' +
              '<stop offset="100%" style="stop-color: var(--dc-primary); stop-opacity: 0"/>' +
            '</linearGradient>' +
          '</defs>' +
          // 横轴 label
          ticks.join('') +
          // 面积
          '<path class="chart-area" d="' + areaD() + '" fill="url(#chartGradient)"/>' +
          // 折线
          '<path id="chart-path" class="chart-line" d="' + pathD() + '"/>' +
          // 末端高亮（带 pulse 圈）
          '<circle class="chart-marker-pulse" cx="' + last.x.toFixed(2) + '" cy="' + last.y.toFixed(2) + '" r="4"/>' +
          '<circle class="chart-marker"       cx="' + last.x.toFixed(2) + '" cy="' + last.y.toFixed(2) + '" r="3.5"/>' +
          // Tooltip 元素（初始隐藏）
          '<g id="tip" class="chart-tooltip" style="visibility: hidden">' +
            '<line  id="tip-line" class="tooltip-line" x1="0" y1="' + pad.top + '" x2="0" y2="' + (pad.top + ih) + '"/>' +
            '<rect  id="tip-bg"   class="tooltip-box" x="0" y="0" rx="4" ry="4" width="80" height="18"/>' +
            '<text  id="tip-text" class="tooltip-text" x="0" y="0" text-anchor="middle"></text>' +
          '</g>' +
          // 顶层透明矩形吃事件
          '<rect class="hit-area" x="0" y="0" width="' + W + '" height="' + H + '"/>' +
        '</svg>' +
      '</div>'

    // 折线进场动画（stroke-dashoffset）
    var line = document.getElementById('chart-path')
    var len = line.getTotalLength()
    line.style.strokeDasharray = len + ' ' + len
    line.style.strokeDashoffset = len
    // 强制 reflow
    void line.getBoundingClientRect()
    line.style.transition = 'stroke-dashoffset 0.8s ease-out'
    line.style.strokeDashoffset = '0'

    // Tooltip (mousemove on desktop, touch on mobile — matches host pattern)
    var svg = UI.main.querySelector('svg')
    var tip = document.getElementById('tip')
    var tipLine = document.getElementById('tip-line')
    var tipBg = document.getElementById('tip-bg')
    var tipText = document.getElementById('tip-text')

    function showTip(best) {
      tip.style.visibility = 'visible'
      tipLine.setAttribute('x1', best.x.toFixed(2))
      tipLine.setAttribute('x2', best.x.toFixed(2))
      var label = formatLabel(best.d.date, range) + ' · ' + formatBytes(best.d.bytes)
      tipText.textContent = label
      var w = Math.max(54, label.length * 6.4 + 14)
      var bx = Math.max(0, Math.min(W - w, best.x - w / 2))
      var by = Math.max(pad.top - 2, best.y - 22)
      tipBg.setAttribute('width', w)
      tipBg.setAttribute('x', bx)
      tipBg.setAttribute('y', by)
      tipText.setAttribute('x', bx + w / 2)
      tipText.setAttribute('y', by + 12)
    }

    function findNearestByClientX(clientX) {
      var rect = svg.getBoundingClientRect()
      var rx = (clientX - rect.left) * (W / rect.width)
      var best = pts[0], bestD = Math.abs(pts[0].x - rx)
      for (var i = 1; i < pts.length; i++) {
        var dd = Math.abs(pts[i].x - rx)
        if (dd < bestD) { best = pts[i]; bestD = dd }
      }
      return best
    }

    svg.addEventListener('mousemove', function (e) { showTip(findNearestByClientX(e.clientX)) })
    svg.addEventListener('mouseleave', function () { tip.style.visibility = 'hidden' })
    svg.addEventListener('touchstart', function (e) { e.preventDefault(); showTip(findNearestByClientX(e.touches[0].clientX)) }, { passive: false })
    svg.addEventListener('touchmove', function (e) { e.preventDefault(); showTip(findNearestByClientX(e.touches[0].clientX)) }, { passive: false })
    svg.addEventListener('touchend', function () { tip.style.visibility = 'hidden' })

    // 底部三档数字
    UI.footer.innerHTML =
      '<div class="stat-item"><span class="stat-value" data-val="day">0 B</span><span class="stat-label">今日</span></div>' +
      '<div class="stat-item"><span class="stat-value" data-val="week">0 B</span><span class="stat-label">本周</span></div>' +
      '<div class="stat-item"><span class="stat-value" data-val="month">0 B</span><span class="stat-label">本月</span></div>'

    animateBytes(UI.footer.querySelector('[data-val="day"]'),   data.totals.day   || 0, 800)
    animateBytes(UI.footer.querySelector('[data-val="week"]'),  data.totals.week  || 0, 800)
    animateBytes(UI.footer.querySelector('[data-val="month"]'), data.totals.month || 0, 800)

    bindRangeToggle()
    DriveCat.resize()
  }

  function bindRangeToggle() {
    var bar = UI.main.querySelector('.range-toggle')
    if (!bar) return
    bar.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.range-btn') : null
      if (!btn) return
      var r = btn.dataset.range
      if (!r || r === STATE.range) return
      STATE.range = r
      reloadUploads()
    })
  }

  function reloadUploads() {
    var r = STATE.range
    if (CACHE.uploads[r]) { renderUploads(CACHE.uploads[r]); return }
    DriveCat.api('GET', '/stats/uploads?range=' + r)
      .then(function (data) { CACHE.uploads[r] = data; renderUploads(data) })
      .catch(function (err) {
        console.error('[dashboard-stats]', err)
        showError('加载失败：' + (err && err.message ? err.message : err))
      })
  }

  // ── 配额卡渲染（柱状图） ──
  var BAR_COLORS = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#06b6d4']

  function renderQuotas(data) {
    UI.title.textContent = '网盘上传统计'
    UI.icon.innerHTML = ICONS.drive

    var range = data.range || STATE.quotaRange
    STATE.quotaRange = range
    STATE.quotaData = data

    var allDrives = data.drives || []
    if (allDrives.length === 0) {
      UI.main.innerHTML = '<div class="flex-center">暂无网盘数据</div>'
      UI.footer.hidden = true
      return
    }

    // 初始化选中集（默认全选）
    if (!STATE.selectedDrives) {
      STATE.selectedDrives = {}
      for (var k = 0; k < allDrives.length; k++) STATE.selectedDrives[allDrives[k].id] = true
    }

    // Range toggle
    var toggleHtml =
      '<div class="range-toggle" role="tablist">' +
      ['day:日', 'month:月', 'year:年'].map(function (s) {
        var parts = s.split(':')
        var active = parts[0] === range ? ' active' : ''
        return '<button class="range-btn' + active + '" data-range="' + parts[0] + '" type="button">' + parts[1] + '</button>'
      }).join('') +
      '</div>'

    // 网盘筛选芯片（保留原始索引以对应颜色）
    var chipsHtml = '<div class="drive-chips">'
    for (var ci = 0; ci < allDrives.length; ci++) {
      var sel = STATE.selectedDrives[allDrives[ci].id]
      var col = BAR_COLORS[ci % BAR_COLORS.length]
      chipsHtml += '<button class="drive-chip' + (sel ? ' active' : '') +
        '" data-id="' + allDrives[ci].id + '" data-idx="' + ci + '" type="button"' +
        ' style="--chip-color:' + col + '">' +
        '<span class="bar-legend-dot" style="background:' + col + '"></span>' +
        escapeHtml(allDrives[ci].name) + '</button>'
    }
    chipsHtml += '</div>'

    // 过滤出选中的网盘（保留原始索引以匹配颜色）
    var visibleDrives = []
    var visibleColors = []
    for (var vi = 0; vi < allDrives.length; vi++) {
      if (STATE.selectedDrives[allDrives[vi].id]) {
        visibleDrives.push(allDrives[vi])
        visibleColors.push(BAR_COLORS[vi % BAR_COLORS.length])
      }
    }

    if (visibleDrives.length === 0) {
      UI.main.innerHTML = toggleHtml + chipsHtml + '<div class="flex-center">请选择网盘</div>'
      UI.footer.hidden = true
      bindQuotaRangeToggle()
      bindDriveChips()
      return
    }

    // 坐标系
    var W = 400, H = 220
    var pad = { top: 10, right: 8, bottom: 18, left: 8 }
    var iw = W - pad.left - pad.right
    var ih = H - pad.top - pad.bottom

    var bucketCount = (visibleDrives[0].series || []).length
    var dc = visibleDrives.length
    if (bucketCount < 1) {
      UI.main.innerHTML = toggleHtml + chipsHtml + '<div class="flex-center">数据不足</div>'
      UI.footer.hidden = true
      bindQuotaRangeToggle()
      bindDriveChips()
      return
    }

    // 全局最大值
    var maxVal = 1
    for (var di = 0; di < dc; di++) {
      var s = visibleDrives[di].series || []
      for (var si = 0; si < s.length; si++) {
        if (s[si].bytes > maxVal) maxVal = s[si].bytes
      }
    }

    // 柱子尺寸
    var groupW = iw / bucketCount
    var barW = Math.max(2, (groupW * 0.7) / dc)
    var groupPad = (groupW - barW * dc) / 2

    // 绘制柱子（带 data 属性用于 tooltip）
    var barsSvg = ''
    for (var bi = 0; bi < bucketCount; bi++) {
      for (var dj = 0; dj < dc; dj++) {
        var seriesItem = (visibleDrives[dj].series || [])[bi]
        var bytes = seriesItem ? seriesItem.bytes : 0
        var dateStr = seriesItem ? seriesItem.date : ''
        var bh = Math.max(0, (bytes / maxVal) * ih)
        var bx = pad.left + bi * groupW + groupPad + dj * barW
        var by = pad.top + ih - bh
        barsSvg += '<rect class="bar-rect" x="' + bx.toFixed(2) + '" y="' + by.toFixed(2) +
          '" width="' + barW.toFixed(2) + '" height="' + bh.toFixed(2) +
          '" rx="1" fill="' + visibleColors[dj] + '" opacity="0.85"' +
          ' data-name="' + escapeHtml(visibleDrives[dj].name) + '"' +
          ' data-date="' + dateStr + '" data-bytes="' + bytes + '"/>'
      }
    }

    // X 轴 label
    var tickIdx
    if (range === 'year') {
      tickIdx = []
      for (var yi = 0; yi < bucketCount; yi++) tickIdx.push(yi)
    } else if (range === 'month') {
      tickIdx = [0, 3, 7, 11]
    } else {
      tickIdx = [0, 7, 14, 21, 28]
    }
    var ticks = ''
    for (var t = 0; t < tickIdx.length; t++) {
      var ti = tickIdx[t]
      if (ti >= bucketCount) continue
      var tx = pad.left + ti * groupW + groupW / 2
      var label = visibleDrives[0].series[ti] ? formatLabel(visibleDrives[0].series[ti].date, range) : ''
      ticks += '<text class="chart-label" x="' + tx.toFixed(1) + '" y="' + (H - 4) + '" text-anchor="middle">' + label + '</text>'
    }

    UI.main.innerHTML =
      toggleHtml + chipsHtml +
      '<div class="chart-container">' +
        '<svg class="chart-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
          ticks + barsSvg +
          '<g id="bar-tip" class="chart-tooltip" style="visibility:hidden">' +
            '<rect id="bar-tip-bg" class="tooltip-box" x="0" y="0" rx="4" ry="4" width="80" height="18"/>' +
            '<text id="bar-tip-text" class="tooltip-text" x="0" y="0" text-anchor="middle"></text>' +
          '</g>' +
        '</svg>' +
      '</div>'

    // Bar tooltip (mousemove on desktop, touch on mobile — matches host pattern)
    var svg = UI.main.querySelector('svg')
    var tipG = document.getElementById('bar-tip')
    var tipBg = document.getElementById('bar-tip-bg')
    var tipText = document.getElementById('bar-tip-text')

    function showBarTipByClientX(clientX) {
      var rect = svg.getBoundingClientRect()
      var rx = (clientX - rect.left) * (W / rect.width)
      var idx = Math.round((rx - pad.left - groupW / 2) / groupW)
      idx = Math.max(0, Math.min(bucketCount - 1, idx))

      var rects = svg.querySelectorAll('.bar-rect')
      for (var ri = 0; ri < rects.length; ri++) {
        rects[ri].setAttribute('opacity', Math.floor(ri / dc) === idx ? '1' : '0.85')
      }

      var tallest = null, mb = -1
      for (var dj = 0; dj < dc; dj++) {
        var item = (visibleDrives[dj].series || [])[idx]
        if (item && item.bytes > mb) { mb = item.bytes; tallest = { drive: visibleDrives[dj], item: item } }
      }
      if (!tallest || mb <= 0) { tipG.style.visibility = 'hidden'; return }
      var labelStr = tallest.drive.name + ' · ' + formatLabel(tallest.item.date, range) + ' · ' + formatBytes(tallest.item.bytes)
      tipText.textContent = labelStr
      var tw = Math.max(60, labelStr.length * 5.6 + 14)
      var cx = pad.left + idx * groupW + groupW / 2
      var tx = Math.max(0, Math.min(W - tw, cx - tw / 2))
      var bh = (mb / maxVal) * ih
      var ty = Math.max(0, pad.top + ih - bh - 22)
      tipBg.setAttribute('width', tw)
      tipBg.setAttribute('x', tx)
      tipBg.setAttribute('y', ty)
      tipText.setAttribute('x', tx + tw / 2)
      tipText.setAttribute('y', ty + 12)
      tipG.style.visibility = 'visible'
    }

    function resetBarOpacity() {
      var rects = svg.querySelectorAll('.bar-rect')
      for (var ri = 0; ri < rects.length; ri++) rects[ri].setAttribute('opacity', '0.85')
    }

    svg.addEventListener('mousemove', function (e) { showBarTipByClientX(e.clientX) })
    svg.addEventListener('mouseleave', function () { tipG.style.visibility = 'hidden'; resetBarOpacity() })
    svg.addEventListener('touchstart', function (e) { e.preventDefault(); showBarTipByClientX(e.touches[0].clientX) }, { passive: false })
    svg.addEventListener('touchmove', function (e) { e.preventDefault(); showBarTipByClientX(e.touches[0].clientX) }, { passive: false })
    svg.addEventListener('touchend', function () { tipG.style.visibility = 'hidden'; resetBarOpacity() })

    UI.footer.hidden = true

    bindQuotaRangeToggle()
    bindDriveChips()
    DriveCat.resize()
  }

  function bindDriveChips() {
    var wrap = UI.main.querySelector('.drive-chips')
    if (!wrap) return
    wrap.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.drive-chip') : null
      if (!btn) return
      var id = Number(btn.dataset.id)
      STATE.selectedDrives[id] = !STATE.selectedDrives[id]
      // 至少保留一个选中
      var any = false
      for (var k in STATE.selectedDrives) if (STATE.selectedDrives[k]) { any = true; break }
      if (!any) { STATE.selectedDrives[id] = true; return }
      renderQuotas(STATE.quotaData)
    })
  }

  function bindQuotaRangeToggle() {
    var bar = UI.main.querySelector('.range-toggle')
    if (!bar) return
    bar.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.range-btn') : null
      if (!btn) return
      var r = btn.dataset.range
      if (!r || r === STATE.quotaRange) return
      STATE.quotaRange = r
      reloadQuotas()
    })
  }

  function reloadQuotas() {
    var r = STATE.quotaRange
    if (CACHE.quotas[r]) { renderQuotas(CACHE.quotas[r]); return }
    DriveCat.api('GET', '/stats/quotas?range=' + r)
      .then(function (data) { CACHE.quotas[r] = data; renderQuotas(data) })
      .catch(function (err) {
        console.error('[dashboard-stats]', err)
        showError('加载失败：' + (err && err.message ? err.message : err))
      })
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
  }

  function showError(msg) {
    UI.loader.hidden = true
    UI.content.hidden = false
    UI.main.innerHTML = '<div class="flex-center error-text">' + escapeHtml(msg) + '</div>'
    UI.footer.hidden = true
  }

  // ── 主流程 ──
  function prefetchAll(card) {
    var base = card === 'quotas' ? '/stats/quotas' : '/stats/uploads'
    var cache = card === 'quotas' ? CACHE.quotas : CACHE.uploads
    ;['day', 'month', 'year'].forEach(function (r) {
      if (cache[r]) return
      DriveCat.api('GET', base + '?range=' + r)
        .then(function (data) { cache[r] = data })
        .catch(function () {})
    })
  }

  DriveCat.onInit(function (ctx) {
    var isWidget = ctx && ctx.position === 'dashboard.widget'
    if (!isWidget) {
      UI.loader.hidden = true
      UI.content.hidden = false
      document.querySelector('.card-head').hidden = true
      UI.main.innerHTML = '<div class="flex-center">该插件仅支持仪表盘小组件模式</div>'
      UI.footer.hidden = true
      DriveCat.resize()
      return
    }

    UI.app.classList.add('widget-mode')
    document.documentElement.style.height = 'auto'
    document.body.style.height = 'auto'

    var card = resolveCard(ctx) || 'uploads'
    var range = card === 'quotas' ? STATE.quotaRange : STATE.range
    var path = (card === 'quotas' ? '/stats/quotas' : '/stats/uploads') + '?range=' + range

    DriveCat.api('GET', path)
      .then(function (data) {
        var cache = card === 'quotas' ? CACHE.quotas : CACHE.uploads
        cache[range] = data
        UI.loader.hidden = true
        UI.content.hidden = false
        if (card === 'quotas') renderQuotas(data)
        else renderUploads(data)
        prefetchAll(card)
      })
      .catch(function (err) {
        console.error('[dashboard-stats]', err)
        showError('加载失败：' + (err && err.message ? err.message : err))
      })
  })
})()
