"""Deletion authorization tests: clocks and external systems are controlled."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/qb-cleanup'))
from qb_cleanup import QbError
from qb_monitor import Monitor
from qb_state import State

HASH = 'a' * 40
CONFIG = dict(enabled=True, base_url='http://qb', username='u', password='p',
              timeout_seconds=5, watcher_ids=[1], poll_seconds=10, quiet_seconds=60,
              path_mappings=[dict(local_path='/local', qb_path='/data')])


def task(id=1, **kw):
    return dict(dict(id=id, watch_rule_id=1, upload_target_id=1, local_path='/local/a.mkv',
                     file_size=100, file_mtime=150, status='success', created_at=200,
                     origin_type='watcher'), **kw)


class Client:
    def __init__(self):
        self.torrents = [dict(hash=HASH, added_on=100, save_path='/data', progress=1,
                              amount_left=0, state='stalledUP', name='demo')]
        self.files = [dict(name='a.mkv', size=100), dict(name='extra.nfo', size=10)]
        self.removed = []
        self.error = None
        self.on_info = None

    def login(self):
        if self.error:
            raise self.error

    def json(self, endpoint, query=None):
        if self.error:
            raise self.error
        if endpoint == 'torrents/info':
            if self.on_info:
                self.on_info()
            return copy.deepcopy(self.torrents)
        return copy.deepcopy(self.files)

    def remove(self, hashes):
        self.removed += hashes
        self.torrents = [t for t in self.torrents if t['hash'] not in hashes]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(self.tmp.name)
        self.config = copy.deepcopy(CONFIG)
        self.client = Client()
        self.data = dict(watchers=[dict(id=1, is_enabled=True, post_action='delete', local_path='/local')],
                         targets=[dict(id=1, watch_rule_id=1, is_enabled=True)], tasks=[task()])
        self.now = 1000
        self.mono = 0
        self.monitor = self.make_monitor()

    def make_monitor(self):
        return Monitor(self.state, lambda: copy.deepcopy(self.data), lambda: self.config,
                       client_factory=lambda c: self.client, wall=lambda: self.now,
                       monotonic=lambda: self.mono)

    def tick(self, seconds=10, **kw):
        self.now += seconds
        self.mono += seconds
        self.monitor.run(**kw)

    def wait(self, seconds=60):
        for _ in range(seconds // 10):
            self.tick()

    def test_quiet_period_allows_unwatched_leftovers(self):
        self.tick(0)
        self.wait(50)
        self.tick(9)
        self.assertEqual(self.client.removed, [])
        self.tick(1)
        self.assertEqual(self.client.removed, [HASH])
        self.assertEqual(self.state.rows()[0]['status'], 'completed')

    def test_no_tasks_or_invalid_watcher_never_delete(self):
        for modify in [lambda: self.data.update(tasks=[]),
                       lambda: self.data['watchers'][0].update(post_action='keep'),
                       lambda: self.config.update(watcher_ids=[2]),
                       lambda: self.data['watchers'][0].update(is_enabled=False)]:
            with self.subTest(modify=modify):
                modify()
                self.tick(0)
                self.wait()
                self.assertEqual(self.client.removed, [])

    def test_non_success_states_block(self):
        for status in ['pending', 'running', 'failed', 'retry', 'cancelled', 'unknown']:
            self.data['tasks'][0]['status'] = status
            self.tick()
            self.wait()
            self.assertEqual(self.client.removed, [])

    def test_missing_target_blocks(self):
        self.data['targets'].append(dict(id=2, watch_rule_id=1, is_enabled=True))
        self.tick(0)
        self.wait()
        self.assertEqual(self.client.removed, [])
        self.data['tasks'].append(task(2, upload_target_id=2))
        self.tick()
        self.wait()
        self.assertEqual(self.client.removed, [HASH])

    def test_new_already_successful_task_resets(self):
        self.tick(0)
        self.wait(50)
        self.data['tasks'].append(task(2))
        self.tick()
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_new_pending_task_resets_until_success(self):
        self.tick(0)
        self.wait(20)
        self.data['tasks'].append(task(2, status='pending'))
        self.tick()
        self.wait()
        self.data['tasks'][1]['status'] = 'success'
        self.tick()
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_downloading_or_checking_blocks(self):
        for status in ['downloading', 'checkingUP', 'moving', 'unknown']:
            self.client.torrents[0]['state'] = status
            self.tick()
            self.wait()
            self.assertEqual(self.client.removed, [])

    def test_restart_and_wall_clock_jump(self):
        self.tick(0)
        self.wait(50)
        self.monitor = self.make_monitor()
        self.now += 100000
        self.tick()
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_connection_outage_retry_is_persistent_and_resets_quiet(self):
        self.tick(0)
        self.wait(50)
        self.client.error = QbError('offline')
        self.tick()
        retry = self.state.meta('retry')
        self.assertGreater(retry['next_retry_at'], self.now)
        self.monitor = self.make_monitor()
        self.client.error = None
        self.tick()
        self.assertEqual(self.client.removed, [])
        self.tick(force=True)
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_final_recheck_new_task(self):
        self.tick(0)
        self.wait(50)
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.data['tasks'].append(task(2, status='pending'))
            return copy.deepcopy(self.data)
        self.monitor.read_host = read
        self.tick()
        self.assertEqual(self.client.removed, [])

    def test_old_generation_or_conflicting_versions_block(self):
        self.data['tasks'][0]['created_at'] = 90
        self.tick(0)
        self.wait()
        self.assertEqual(self.client.removed, [])
        self.data['tasks'][0]['created_at'] = 200
        self.data['tasks'].append(task(2, file_mtime=160))
        self.tick()
        self.wait()
        self.assertEqual(self.client.removed, [])

    def test_shared_file_torrents_independently_complete(self):
        self.client.torrents.append({**self.client.torrents[0], 'hash': 'b'*40})
        self.tick(0)
        self.wait()
        self.assertEqual(set(self.client.removed), {HASH, 'b'*40})

    def test_lost_delete_response_confirmed_on_recovery(self):
        remove = self.client.remove
        def uncertain(hashes):
            remove(hashes)
            raise QbError('response lost')
        self.client.remove = uncertain
        self.tick(0)
        self.wait()
        self.assertEqual(self.state.rows()[0]['status'], 'deleting')
        self.monitor = self.make_monitor()
        self.tick(force=True)
        self.assertEqual(self.state.rows()[0]['status'], 'completed')
        self.assertEqual(self.client.removed, [HASH])

    def test_cancel_persists_and_clearing_history_does_not_unlock(self):
        self.tick(0)
        key = self.state.rows()[0]['key']
        self.monitor.cancel(key)
        self.state.clear_completed()
        self.monitor = self.make_monitor()
        self.wait(120)
        self.assertEqual(self.client.removed, [])

    def test_storage_failure_prevents_delete(self):
        self.tick(0)
        self.wait(50)
        put = self.state.put
        def fail(row):
            if row['status'] == 'deleting':
                raise OSError('disk full')
            put(row)
        self.state.put = fail
        self.tick()
        self.assertEqual(self.client.removed, [])
        self.assertTrue(self.monitor.error)

    def test_db_failure_resets_observation(self):
        self.tick(0)
        self.wait(50)
        read = self.monitor.read_host
        def fail():
            raise PermissionError('denied')
        self.monitor.read_host = fail
        self.tick()
        self.monitor.read_host = read
        self.tick(force=True)
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_permanent_auth_error_waits_for_manual_retry(self):
        self.client.error = QbError('bad credentials', permanent=True)
        self.tick(0)
        self.assertTrue(self.state.meta('retry')['paused'])
        self.client.error = None
        self.wait(120)
        self.assertEqual(self.client.removed, [])
        self.tick(force=True)
        self.wait()
        self.assertEqual(self.client.removed, [HASH])

    def test_observation_gap_restarts_full_wait(self):
        self.tick(0)
        self.wait(50)
        self.tick(120)
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_stop_prevents_new_network_operations(self):
        self.tick(0)
        self.monitor.stopped.set()
        self.wait(120)
        self.assertEqual(self.client.removed, [])

    def test_disable_enable_restarts(self):
        self.tick(0)
        self.wait(50)
        self.config['enabled'] = False
        self.tick()
        self.config['enabled'] = True
        self.tick()
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_configuration_and_file_list_change_restart(self):
        self.tick(0)
        self.wait(50)
        self.config['quiet_seconds'] = 70
        self.tick()
        self.wait(60)
        self.assertEqual(self.client.removed, [])
        self.client.files.append(dict(name='another.nfo', size=15))
        self.tick()
        self.wait(60)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_readded_torrent_does_not_reuse_old_success(self):
        self.tick(0)
        self.wait(50)
        self.client.torrents[0]['added_on'] = 300
        self.tick()
        self.wait(120)
        self.assertEqual(self.client.removed, [])

    def test_history_disappearance_is_not_success(self):
        self.data['tasks'].append(task(2, status='failed'))
        self.tick(0)
        self.data['tasks'].pop()
        self.tick()
        self.wait(120)
        self.assertEqual(self.client.removed, [])
        self.assertTrue(self.state.rows()[0]['evidence_lost'])

    def test_mapping_aliases_block(self):
        self.config['path_mappings'].append(dict(local_path='/local/alias', qb_path='/data'))
        self.data['tasks'].append(task(2, local_path='/local/alias/a.mkv'))
        self.tick(0)
        self.wait()
        self.assertEqual(self.client.removed, [])

    def test_wall_clock_jump_without_restart_does_not_skip_wait(self):
        self.tick(0)
        self.now += 999999
        self.tick()
        self.assertEqual(self.client.removed, [])
        self.wait(50)
        self.assertEqual(self.client.removed, [HASH])

    def test_confirmed_retry_does_not_delete_readded_generation(self):
        def uncertain(hashes):
            self.client.removed += hashes
            self.client.torrents[0]['added_on'] = 300
            raise QbError('response lost')
        self.client.remove = uncertain
        self.tick(0)
        self.wait()
        self.tick(force=True)
        self.wait(120)
        self.assertEqual(self.client.removed, [HASH])

    def test_corrupt_database_is_not_rebuilt(self):
        self.state.path.write_bytes(b'corrupt state')
        self.tick()
        self.assertIn('数据库', self.monitor.error)
        self.assertEqual(self.client.removed, [])
        with self.assertRaises(Exception):
            State(self.tmp.name)
        self.assertEqual(self.state.path.read_bytes(), b'corrupt state')

    def test_completed_retention_preserves_pending_and_cancelled(self):
        self.tick(0)
        row = self.state.rows()[0]
        self.state.put({**row, 'key': 'complete', 'status': 'completed', 'updated': -40*86400})
        self.state.put({**row, 'key': 'cancel', 'status': 'cancelled', 'updated': -40*86400})
        self.state.prune(self.now)
        self.assertIsNone(self.state.get('complete'))
        self.assertIsNotNone(self.state.get('cancel'))
        self.assertIsNotNone(self.state.get(row['key']))

    def test_live_peer_availability_does_not_reset_file_identity(self):
        self.tick(0)
        for i in range(6):
            self.client.files[0]['availability'] = i + 1
            self.tick()
        self.assertEqual(self.client.removed, [HASH])

    def test_batch_cursor_rotates_candidates_without_starvation(self):
        self.monitor.BATCH = 1
        self.client.torrents = [{**self.client.torrents[0], 'hash': format(i, '040x')} for i in range(1, 5)]
        self.tick(0)
        self.wait(120)
        self.assertEqual(len(self.client.removed), 4)

    def test_concurrent_check_does_not_overlap(self):
        import threading
        entered = threading.Event()
        released = threading.Event()
        read = self.monitor.read_host
        calls = []
        def blocked():
            calls.append(1)
            entered.set()
            released.wait(5)
            return read()
        self.monitor.read_host = blocked
        thread = threading.Thread(target=self.monitor.run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.monitor.run(force=True)
            self.assertEqual(len(calls), 1)
        finally:
            released.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_watcher_temporarily_disabled_does_not_mean_history_lost(self):
        self.tick(0)
        self.wait(50)
        self.data['watchers'][0]['is_enabled'] = False
        self.tick()
        self.data['watchers'][0]['is_enabled'] = True
        self.tick()
        self.wait(50)
        self.assertEqual(self.client.removed, [])
        self.tick()
        self.assertEqual(self.client.removed, [HASH])


if __name__ == '__main__':
    unittest.main()
