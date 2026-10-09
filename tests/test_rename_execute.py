"""RenameManager 执行路径测试：驱动返回值校验、worker 池、耗时字段。"""
import asyncio
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/rename'))

# 宿主运行时提供 loguru；测试环境未装时用标准 logging 顶替（与 qb 测试的 stub 模式一致）
_loguru_stub = types.ModuleType('loguru')
_loguru_stub.logger = logging.getLogger('test.rename')
sys.modules.setdefault('loguru', _loguru_stub)

from rename_engine import RuleSpec  # noqa: E402
from rename_manager import RenameManager  # noqa: E402

PREFIX_RULE = RuleSpec(type='insert', params={'text': 'X-', 'position': 0})


class FakeFile:
    def __init__(self, fid, name):
        self.id = fid
        self.name = name
        self.modified_at = None


class FakeDrive:
    """behavior: {file_id: (delay_sec, outcome)}；outcome 为 bool 或 Exception 实例。"""

    def __init__(self, count, behavior=None):
        self._files = [FakeFile(f'id{i}', f'f{i}.mkv') for i in range(count)]
        self._behavior = behavior or {}

    async def list_files(self, parent_id='0'):
        return list(self._files)

    async def rename(self, file_id, new_name):
        delay, outcome = self._behavior.get(file_id, (0, True))
        if delay:
            await asyncio.sleep(delay)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _collect(drive, **kw):
    async def _go():
        events = []
        async for ev in RenameManager.execute_stream(
            drive, '0', [PREFIX_RULE], pause_ms=0, **kw
        ):
            events.append(ev)
        return events
    return asyncio.run(_go())


def _progress(events):
    return [e for e in events if e['type'] == 'progress']


def _done(events):
    return next(e for e in events if e['type'] == 'done')


def test_falsy_return_counts_as_failed():
    """驱动吞异常返回 False 时必须记为 failed，不能谎报 success。"""
    drive = FakeDrive(3, {'id1': (0, False)})
    events = _collect(drive, concurrency=3)
    by_id = {e['file_id']: e for e in _progress(events)}
    assert by_id['id1']['status'] == 'failed'
    assert '驱动未确认成功' in by_id['id1']['error']
    assert by_id['id0']['status'] == 'success'
    done = _done(events)
    assert (done['success'], done['failed'], done['skipped']) == (2, 1, 0)


def test_exception_counts_as_failed():
    drive = FakeDrive(2, {'id0': (0, RuntimeError('boom'))})
    events = _collect(drive, concurrency=2)
    by_id = {e['file_id']: e for e in _progress(events)}
    assert by_id['id0']['status'] == 'failed'
    assert 'boom' in by_id['id0']['error']
    assert _done(events)['failed'] == 1


def test_worker_pool_has_no_batch_barrier():
    """id0 慢 0.3s：批次屏障下 id10~id19 必须排在它之后，worker 池下不用。"""
    drive = FakeDrive(20, {'id0': (0.3, True)})
    events = _collect(drive, concurrency=10)
    order = [e['file_id'] for e in _progress(events)]
    assert order.index('id19') < order.index('id0')


def test_done_event_carries_elapsed_and_rate():
    drive = FakeDrive(5, {f'id{i}': (0.05, True) for i in range(5)})
    events = _collect(drive, concurrency=2)
    done = _done(events)
    # 5 个任务 / 2 worker * 50ms ≈ 150ms，阈值放宽到 80ms
    assert done['elapsed_ms'] >= 80
    assert done['rate'] > 0


def test_empty_plan_stream_still_reports_done():
    drive = FakeDrive(0)
    done = _done(_collect(drive, concurrency=10))
    assert done['total'] == 0
    assert done['elapsed_ms'] >= 0
    assert done['rate'] == 0


def test_execute_non_stream_checks_return_and_counts():
    drive = FakeDrive(3, {'id2': (0, False)})
    result = asyncio.run(RenameManager.execute(
        drive, '0', [PREFIX_RULE], concurrency=3, pause_ms=0
    ))
    assert (result.success, result.failed, result.skipped) == (2, 1, 0)
