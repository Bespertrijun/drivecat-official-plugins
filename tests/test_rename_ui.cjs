const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')

function openPlugin(context) {
  const elements = new Map()
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set(id === 'step-1' ? ['active'] : [])
      elements.set(id, {
        id, style: {}, textContent: '', innerHTML: '',
        classList: {
          toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name) },
          contains(name) { return classes.has(name) },
        },
        addEventListener() {}, querySelectorAll() { return [] },
      })
    }
    return elements.get(id)
  }
  const requests = []
  const warnings = []
  let init, preview
  const sandbox = {
    window: { location: { origin: 'http://localhost' }, addEventListener() {} },
    document: {
      getElementById: element,
      createElement() { return element('temporary') },
      querySelectorAll(selector) {
        return selector === '.step-panel' ? [1, 2, 3].map(n => element('step-' + n)) : []
      },
    },
    DriveCat: {
      onInit(callback) { init = callback }, resize() {},
      toast(message) { warnings.push(message) },
      api(method, path, body) {
        requests.push({ path, body })
        return new Promise(() => {})
      },
    },
    fetch() { return new Promise(() => {}) },
    setTimeout(callback) { preview = callback }, clearTimeout() {},
  }
  vm.runInNewContext(readFileSync(join(__dirname, '../plugins/rename/ui/app.js'), 'utf8'), sandbox)
  init(context)
  return {
    element, warnings,
    app: sandbox.window.App,
    previewBody() {
      element('new-rule-type').value = 'case'
      sandbox.window.App.addRule()
      preview()
      return JSON.parse(JSON.stringify(requests.find(r => r.path === '/rename/preview').body))
    },
  }
}

test('directory context opens rules and previews the selected directory contents', () => {
  const ui = openPlugin({ drive_id: 7, parent_id: 'root', selected_file: { id: 'folder', is_dir: true } })
  assert.equal(ui.element('step-2').classList.contains('active'), true)
  assert.deepEqual(ui.warnings, [])
  const body = ui.previewBody()
  assert.equal(body.drive_config_id, 7)
  assert.equal(body.parent_id, 'folder')
  assert.equal(Object.hasOwn(body, 'file_ids'), false)
})

test('file context opens rules and limits the preview to the selected file', () => {
  const ui = openPlugin({ drive_id: 7, parent_id: 'folder', selected_file: { id: 'file', is_dir: false } })
  assert.equal(ui.element('step-2').classList.contains('active'), true)
  assert.deepEqual(ui.warnings, [])
  const body = ui.previewBody()
  assert.equal(body.parent_id, 'folder')
  assert.deepEqual(body.file_ids, ['file'])
})

test('standalone entry still requires a selection', () => {
  const ui = openPlugin({ drive_id: 7, parent_id: 'root', selected_file: null })
  ui.app.goStep(2)
  assert.equal(ui.element('step-1').classList.contains('active'), true)
  assert.deepEqual(ui.warnings, ['请至少选择一个文件或目录'])
})

test('context entry still requires a drive', () => {
  const ui = openPlugin({ selected_file: { id: 'folder', is_dir: true } })
  assert.equal(ui.element('step-1').classList.contains('active'), true)
  assert.deepEqual(ui.warnings, ['请先选择网盘'])
})
