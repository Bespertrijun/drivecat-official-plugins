const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')

function openPlugin(context, opts = {}) {
  const elements = new Map()
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set(id === 'step-1' ? ['active'] : [])
      elements.set(id, {
        id, style: {}, textContent: '', innerHTML: '',
        classList: {
          toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name) },
          contains(name) { return classes.has(name) },
          add(name) { classes.add(name) },
          remove(name) { classes.delete(name) },
        },
        addEventListener() {}, querySelectorAll() { return [] }, appendChild() {},
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
      getContext() { return {} },
      api(method, path, body) {
        if (method === 'GET' && path === '/rename/templates') {
          return Promise.resolve({ templates: opts.templates || [] })
        }
        requests.push({ path, body })
        return new Promise(() => {})
      },
    },
    fetch: opts.sse
      ? function () {
        const chunks = opts.sse.slice()
        return Promise.resolve({
          ok: true,
          body: {
            getReader() {
              return {
                read() {
                  const next = chunks.shift()
                  return Promise.resolve(next === undefined
                    ? { done: true }
                    : { done: false, value: Buffer.from(next) })
                },
              }
            },
          },
        })
      }
      : function () { return new Promise(() => {}) },
    setTimeout(callback) { preview = callback }, clearTimeout() {},
    setInterval() { return 0 }, clearInterval() {},
    TextDecoder: require('node:util').TextDecoder,
  }
  vm.runInNewContext(readFileSync(join(__dirname, '../plugins/rename/ui/app.js'), 'utf8'), sandbox)
  init(context)
  return {
    element, warnings, requests,
    app: sandbox.window.App,
    runPreview() { preview() },
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

test('digit-only regex replacement stays a string (backend re.sub would 500 on int)', async () => {
  const ui = openPlugin(
    { drive_id: 7, parent_id: 'root', selected_file: { id: 'folder', is_dir: true } },
    // 旧模板可能已把 replacement 存成数字，两种情况都必须发字符串
    { templates: [
      { name: 'legacy-int', rules: [{ type: 'regex', params: { pattern: 'x', replacement: 123456 } }] },
      { name: 'typed-str', rules: [{ type: 'regex', params: { pattern: 'x', replacement: '123456' } }] },
    ] },
  )
  await new Promise(r => setImmediate(r))  // flush loadTemplates
  for (const idx of ['0', '1']) {
    ui.requests.length = 0
    ui.element('template-select').value = idx
    ui.app.loadTemplate()
    ui.runPreview()
    const body = JSON.parse(JSON.stringify(ui.requests.find(r => r.path === '/rename/preview').body))
    assert.equal(body.rules[0].params.replacement, '123456')
  }
})


test('execute shows elapsed time and rate from the done event', async () => {
  const ui = openPlugin(
    { drive_id: 7, parent_id: 'root', selected_file: { id: 'folder', is_dir: true } },
    { sse: [
      'data: {"type":"start","total":2}\n\n',
      'data: {"type":"progress","index":0,"file_id":"a","original":"a.mkv","new":"b.mkv","status":"success"}\n\n',
      'data: {"type":"progress","index":1,"file_id":"b","original":"c.mkv","new":"d.mkv","status":"success"}\n\n',
      'data: {"type":"done","total":2,"success":2,"failed":0,"skipped":0,"elapsed_ms":42300,"rate":3.71}\n\n',
      'data: [DONE]\n\n',
    ] },
  )
  ui.element('new-rule-type').value = 'case'
  ui.app.addRule()
  ui.app.doExecute()
  for (let i = 0; i < 10 && !ui.element('run-summary').textContent; i++) {
    await new Promise(r => setImmediate(r))
  }
  const summary = ui.element('run-summary')
  assert.match(summary.textContent, /成功 2/)
  assert.match(summary.textContent, /耗时 42\.3 秒/)
  assert.match(summary.textContent, /3\.7 个\/秒/)
  assert.equal(ui.element('btn-execute').textContent, '✓ 已完成')
  assert.equal(ui.element('btn-execute').disabled, true)
})
