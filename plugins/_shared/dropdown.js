/**
 * DriveCat 自绘下拉组件 v1 — 对齐主仓下拉框视觉（浮层卡片 + 选项 hover 高亮）
 *
 * 用法：
 *   DriveCatDropdown.enhance(document.querySelector('select'))
 *
 * 原生 <select> 被隐藏但仍是唯一数据源，现有业务逻辑零改动：
 *   - 选项/选中项变化通过 MutationObserver 自动同步到自绘 UI
 *   - 用户选择后回写 select.value 并依次派发 input、change 事件，
 *     业务侧无论是监听 input、change 还是直接读 value 都不受影响
 *
 * 样式自包含（注入 <style>），颜色全部走宿主注入的 --dc-* 变量。
 */
;(function () {
  'use strict'

  var CSS = ''
    + '.dc-select{position:relative;width:100%}'
    + 'select.dc-select-native{display:none!important}'
    + '.dc-select-toggle{display:flex;align-items:center;justify-content:space-between;gap:8px;'
    + 'width:100%;padding:8px 12px;border:1px solid var(--dc-border);border-radius:6px;'
    + 'background:var(--dc-bg-input,var(--dc-bg-card));color:var(--dc-text-primary);'
    + 'font-size:13px;font-family:inherit;text-align:left;cursor:pointer;outline:none;'
    + 'transition:border-color .2s}'
    + '.dc-select-toggle:hover{border-color:var(--dc-border-hover,var(--dc-border))}'
    + '.dc-select.open .dc-select-toggle{border-color:var(--dc-primary)}'
    + '.dc-select-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.dc-select-chevron{flex-shrink:0;color:var(--dc-text-tertiary);transition:transform .15s}'
    + '.dc-select.open .dc-select-chevron{transform:rotate(180deg)}'
    + '.dc-select-menu{display:none;position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:20;'
    + 'background:var(--dc-bg-card);border:1px solid var(--dc-border);border-radius:8px;'
    + 'box-shadow:0 8px 24px rgba(0,0,0,.18),0 2px 6px rgba(0,0,0,.08);'
    + 'padding:4px;max-height:220px;overflow-y:auto}'
    + '.dc-select.open .dc-select-menu{display:block}'
    + '.dc-select-option{padding:6px 10px;border-radius:4px;font-size:13px;'
    + 'color:var(--dc-text-primary);cursor:pointer;'
    + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.dc-select-option:hover{background:var(--dc-bg-hover,var(--dc-bg-elevated))}'
    + '.dc-select-option.selected{color:var(--dc-primary);font-weight:600}'
    /* 浅色：toggle 白底（与插件 input 的浅色规则一致） */
    + '[data-theme="light"] .dc-select-toggle{background:var(--dc-bg-card);'
    + 'border-color:var(--dc-border-hover,rgba(0,0,0,.14))}'
    /* 移动端触摸目标 */
    + '@media (max-width:600px){'
    + '.dc-select-toggle{padding:12px 14px;font-size:15px;min-height:44px}'
    + '.dc-select-option{padding:10px 12px;font-size:15px}'
    + '.dc-select-menu{max-height:40vh}'
    + '}'

  var CHEVRON = '<svg class="dc-select-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'

  var styleInjected = false
  function injectStyle() {
    if (styleInjected) return
    styleInjected = true
    var el = document.createElement('style')
    el.textContent = CSS
    document.head.appendChild(el)
  }

  function closeAll() {
    document.querySelectorAll('.dc-select.open').forEach(function (w) {
      w.classList.remove('open')
    })
  }

  // 点击组件外任意处关闭浮层（capture 阶段，先于业务点击处理）
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.dc-select.open').forEach(function (w) {
      if (!w.contains(e.target)) w.classList.remove('open')
    })
  }, true)
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAll()
  })

  function enhance(sel) {
    if (!sel || sel.tagName !== 'SELECT') return
    injectStyle()
    // 幂等：已包装过只刷新（renderRules 重建 DOM 后是全新元素，不会走到这）
    if (sel._dcSelect) { sel._dcSelect.refresh(); return }

    var wrapper = document.createElement('div')
    wrapper.className = 'dc-select'
    sel.parentNode.insertBefore(wrapper, sel)
    wrapper.appendChild(sel)
    sel.classList.add('dc-select-native')

    var toggle = document.createElement('button')
    toggle.type = 'button'
    toggle.className = 'dc-select-toggle'
    var label = document.createElement('span')
    label.className = 'dc-select-label'
    toggle.appendChild(label)
    toggle.insertAdjacentHTML('beforeend', CHEVRON)

    var menu = document.createElement('div')
    menu.className = 'dc-select-menu'

    wrapper.appendChild(toggle)
    wrapper.appendChild(menu)

    function refresh() {
      menu.innerHTML = ''
      Array.prototype.forEach.call(sel.options, function (opt, i) {
        // 空值 option 是占位符（select 的约定俗成）：不进浮层列表，
        // 仅在未选择时作为封闭态 label 显示
        if (opt.value === '') return
        var item = document.createElement('div')
        item.className = 'dc-select-option' + (i === sel.selectedIndex ? ' selected' : '')
        item.textContent = opt.textContent
        item.setAttribute('data-idx', i)
        menu.appendChild(item)
      })
      var cur = sel.options[sel.selectedIndex] || sel.options[0]
      label.textContent = cur ? cur.textContent : ''
    }

    toggle.addEventListener('click', function () {
      var willOpen = !wrapper.classList.contains('open')
      closeAll()
      if (willOpen) wrapper.classList.add('open')
    })

    menu.addEventListener('click', function (e) {
      var item = e.target.closest('.dc-select-option')
      if (!item) return
      sel.selectedIndex = parseInt(item.getAttribute('data-idx'), 10)
      // 现有绑定里监听 input 和 change 的都有，都派发
      sel.dispatchEvent(new Event('input', { bubbles: true }))
      sel.dispatchEvent(new Event('change', { bubbles: true }))
      wrapper.classList.remove('open')
    })

    // 业务代码直接改 select 值后派发事件时同步 UI
    sel.addEventListener('change', refresh)
    sel.addEventListener('input', refresh)

    // 选项动态增删（innerHTML 重写 / appendChild）时自动刷新
    new MutationObserver(refresh).observe(sel, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['selected', 'disabled'],
    })

    sel._dcSelect = { refresh: refresh }
    refresh()
  }

  window.DriveCatDropdown = { enhance: enhance }
})()
