"""Optional real Chromium + DevRT smoke. Uses installed websockets, no new deps.
Run: python tests/qb_ui_smoke.py /path/to/chrome
"""
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import websockets

ROOT = Path(__file__).resolve().parents[1]


async def run():
    with tempfile.TemporaryDirectory(prefix='qb-ui-') as temporary:
        directory = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server_code = '''import sys
from pathlib import Path
from devrt import server
import uvicorn
server.DATA_DIR = Path(sys.argv[1])
uvicorn.run(server.create_app(Path('plugins/qb-cleanup').resolve()), host='127.0.0.1', port=int(sys.argv[2]), log_level='error')
'''
        with (directory / 'server.log').open('w') as log:
            server = subprocess.Popen([sys.executable, '-c', server_code, str(directory / 'data'), str(port)], cwd=ROOT, stdout=log, stderr=log)
            browser = None
            try:
                for _ in range(100):
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/', timeout=1):
                            break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError((directory / 'server.log').read_text())
                        await asyncio.sleep(.1)
                else:
                    raise RuntimeError('DevRT did not start')
                profile = directory / 'browser'
                browser = subprocess.Popen([sys.argv[1], '--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0',
                                            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                                            '--user-data-dir=' + str(profile), 'about:blank'], stdout=subprocess.DEVNULL, stderr=log)
                for _ in range(100):
                    if (profile / 'DevToolsActivePort').exists():
                        break
                    await asyncio.sleep(.1)
                debug_port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
                for _ in range(100):
                    with urlopen(f'http://127.0.0.1:{debug_port}/json/list') as response:
                        targets = json.load(response)
                    if targets:
                        target = targets[0]
                        break
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError('Chromium did not create a page')
                async with websockets.connect(target['webSocketDebuggerUrl']) as ws:
                    sequence = 0
                    async def command(method, params=None):
                        nonlocal sequence
                        sequence += 1
                        await ws.send(json.dumps(dict(id=sequence, method=method, params=params or {})))
                        while True:
                            reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
                            if reply.get('id') == sequence:
                                if 'error' in reply:
                                    raise RuntimeError(reply['error'])
                                return reply.get('result', {})
                    async def evaluate(expression):
                        result = await command('Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True))
                        if 'exceptionDetails' in result:
                            raise AssertionError(result['exceptionDetails'])
                        return result.get('result', {}).get('value')
                    async def wait(expression):
                        for _ in range(100):
                            if await evaluate(expression):
                                return
                            await asyncio.sleep(.1)
                        raise AssertionError('UI timed out: ' + expression)
                    await command('Page.navigate', {'url': f'http://127.0.0.1:{port}/'})
                    await wait("!!document.querySelector('#plugin-iframe')?.contentDocument?.querySelector('#watcher-list input')")
                    await evaluate("window.d = document.querySelector('#plugin-iframe').contentDocument; window.w = document.querySelector('#plugin-iframe').contentWindow; true")
                    assert await evaluate("d.querySelectorAll('#watcher-list input').length") == 2
                    assert await evaluate("d.querySelector('#quiet-seconds').value") == '60'
                    await evaluate("d.querySelector('#watcher-list input').checked=true; d.querySelector('#quiet-seconds').value=75; d.querySelector('#base-url').value='http://127.0.0.1:1'; d.querySelector('#password').value='ui-secret'; d.querySelector('.mapping-local').value='/downloads'; d.querySelector('.mapping-qb').value='/data'; d.querySelector('#btn-save').click(); true")
                    await wait("d.querySelector('#status').textContent === '已保存'")
                    assert await evaluate("d.querySelector('#password').value") == ''
                    assert await evaluate("d.querySelector('#password').placeholder.includes('已设置')")
                    config = await evaluate("w.DriveCat.api('GET','/qb-cleanup/config')")
                    assert config['config']['quiet_seconds'] == 75
                    assert config['config']['watcher_ids'] == [1]
                    assert 'password' not in config['config']
                    await evaluate("d.querySelector('#quiet-seconds').value=0; d.querySelector('#btn-save').click(); true")
                    await wait("d.querySelector('#status').textContent.includes('保存失败')")
                    await evaluate("d.querySelector('#quiet-seconds').value=75; d.querySelector('#btn-test').click(); true")
                    await wait("d.querySelector('#status').textContent.includes('连接失败')")
                    await evaluate("d.querySelector('#btn-check').click(); true")
                    await wait("!d.querySelector('#btn-check').disabled")
                    await evaluate("d.querySelector('#btn-clear').click(); true")
                    await wait("!d.querySelector('#btn-clear').disabled")
                    await command('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=True))
                    assert await evaluate("d.documentElement.scrollWidth <= d.documentElement.clientWidth + 1")
                    print('PASS: Chromium iframe/SDK, watcher list, save/redaction, quiet validation, failed connection, check/history actions, mobile width')
            finally:
                for process in (browser, server):
                    if process and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()


asyncio.run(run())
