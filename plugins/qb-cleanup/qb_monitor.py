"""Poll watcher evidence and authorize record-only deletion after a quiet period."""
import hashlib
import json
import math
import random
import threading
import time
from collections import defaultdict
from pathlib import PurePosixPath

from qb_cleanup import QbClient, QbError, absolute_path, mapped_path, torrent_files, valid_hash


TASK_FIELDS = ('id', 'watch_rule_id', 'upload_target_id', 'local_path', 'file_size',
               'file_mtime', 'status', 'created_at', 'origin_type')
STABLE_STATES = {'uploading', 'stalledUP', 'pausedUP', 'stoppedUP', 'queuedUP', 'forcedUP'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identity(instance, torrent):
    added = torrent.get('added_on')
    if not valid_hash(torrent.get('hash')) or not isinstance(added, (int, float)) or added <= 0:
        raise ValueError('种子 hash 或添加时间缺失，无法确认代际')
    return fingerprint([instance, torrent['hash'], added, absolute_path(torrent.get('save_path'))])


def evaluate(config, host, torrent, files):
    """One authorization function for normal polling and final recheck."""
    paths = torrent_files(torrent, files)
    selected = set(config['watcher_ids'])
    watchers = {w['id']: w for w in host['watchers'] if w['id'] in selected}
    eligible = {wid: w for wid, w in watchers.items() if w['is_enabled'] and w['post_action'] == 'delete'}
    targets = [t for t in host['targets'] if t['watch_rule_id'] in eligible and t['is_enabled']]
    tasks = []
    sources = defaultdict(set)
    for task in host['tasks']:
        if task['watch_rule_id'] not in eligible or task.get('origin_type') != 'watcher':
            continue
        try:
            local = absolute_path(task['local_path'])
            if not PurePosixPath(local).is_relative_to(absolute_path(eligible[task['watch_rule_id']]['local_path'])):
                continue
            target = mapped_path(local, config['path_mappings'])
        except ValueError:
            continue
        if target in paths:
            tasks.append({field: task.get(field) for field in TASK_FIELDS})
            sources[target].add(local)
    tasks.sort(key=lambda t: t['id'])
    file_identity = sorted(({field: f.get(field) for field in ('name', 'size', 'index', 'priority')} for f in files), key=lambda f: f['name'])
    digest = fingerprint([config, sorted(watchers.values(), key=lambda w: w['id']),
                          sorted(targets, key=lambda t: t['id']), tasks, file_identity,
                          torrent.get('added_on'), torrent.get('save_path')])
    result = dict(fingerprint=digest, tasks=tasks, task_count=len(tasks), status='needs_inspection', reason='')
    def verdict(status, reason):
        return {**result, 'status': status, 'reason': reason}
    if not tasks:
        return verdict('needs_inspection', '没有可关联的所选 watcher 上传任务')
    if any(len(values) > 1 for values in sources.values()):
        return verdict('needs_inspection', '多个本地路径映射至同一 qB 文件，关联有歧义')
    versions = defaultdict(set)
    groups = defaultdict(list)
    for task in tasks:
        if (not isinstance(task['created_at'], (int, float)) or task['created_at'] < torrent['added_on']
                or not isinstance(task['file_mtime'], (int, float)) or task['file_mtime'] <= 0):
            return verdict('needs_inspection', '任务时间不足以证明属于当前种子代际')
        target = mapped_path(task['local_path'], config['path_mappings'])
        if task['file_size'] != paths[target]:
            return verdict('needs_inspection', '任务文件大小与 qB 文件不一致')
        version = (task['file_size'], task['file_mtime'])
        versions[(task['watch_rule_id'], task['local_path'])].add(version)
        groups[(task['watch_rule_id'], task['local_path'], *version)].append(task)
    if any(len(v) != 1 for v in versions.values()):
        return verdict('needs_inspection', '同路径存在多个文件版本，无法确认替代关系')
    if any(task['status'] != 'success' for task in tasks):
        return verdict('waiting_upload', '关联上传任务尚未全部成功（含失败、取消及重试）')
    for (watcher, *_), group in groups.items():
        required = {t['id'] for t in targets if t['watch_rule_id'] == watcher}
        actual = {t['upload_target_id'] for t in group}
        if not required or not required.issubset(actual) or None in actual:
            return verdict('waiting_upload', '当前文件版本的有效上传目标尚未全部成功')
    if (torrent.get('progress') != 1 or torrent.get('amount_left') != 0
            or torrent.get('state') not in STABLE_STATES):
        return verdict('waiting_download', 'qB 尚未下载完成，或正在下载、移动、校验')
    return verdict('quiet', '关联任务全部成功，等待安静期')


class Monitor:
    BATCH = 40

    def __init__(self, state, read_host, get_config, client_factory=QbClient,
                 wall=time.time, monotonic=time.monotonic):
        self.state, self.read_host, self.get_config = state, read_host, get_config
        self.client_factory, self.wall, self.monotonic = client_factory, wall, monotonic
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.quiet = {}
        self.error = ''
        self.last_run = None
        self.client = None
        self.client_config = None
        self.observation_interval = 10

    def invalidate(self):
        self.quiet.clear()
        self.client = None
        self.last_run = None

    def cancel(self, key):
        with self.lock:
            row = self.state.get(key)
            if not row:
                raise KeyError(key)
            row.update(status='cancelled', reason='用户取消当前种子代际', tasks=[], updated=self.wall(), quiet_since=None)
            self.state.put(row)
            self.quiet.pop(key, None)

    def _save(self, row, status, reason):
        row.update(status=status, reason=reason, updated=self.wall(), quiet_since=None)
        if status == 'completed':
            row['tasks'] = []
        self.quiet.pop(row['key'], None)
        self.state.put(row)

    def _observe(self, row, result, config, host):
        key = row['key']
        prior_ids = {t['id'] for t in row.get('tasks', []) if t['watch_rule_id'] in config['watcher_ids']}
        current_ids = {t['id'] for t in host['tasks']}
        if row.get('evidence_lost') or not prior_ids.issubset(current_ids):
            result = {**result, 'status': 'needs_inspection', 'reason': '曾关联的任务历史已缺失，不能自动授权'}
            row['evidence_lost'] = True
        row.update(result, updated=self.wall(), last_observed_at=self.wall())
        if result['status'] != 'quiet':
            self.quiet.pop(key, None)
            row['quiet_since'] = None
        else:
            previous = self.quiet.get(key)
            now = self.monotonic()
            if (previous is None or previous[0] != result['fingerprint']
                    or now - previous[2] > max(max(previous[3], self.observation_interval) * 3, 30)):
                previous = (result['fingerprint'], now, now, self.observation_interval)
                row['quiet_since'] = self.wall()
            self.quiet[key] = (previous[0], previous[1], now, self.observation_interval)
            row['remaining_seconds'] = max(0, config['quiet_seconds'] - (now - previous[1]))
        self.state.put(row)
        return result['status'] == 'quiet' and row['remaining_seconds'] == 0

    def _retry(self, exc):
        self.quiet.clear()
        self.client = None
        retry = self.state.meta('retry', {})
        attempts = retry.get('attempts', 0) + 1
        permanent = isinstance(exc, QbError) and exc.permanent
        delay = [30, 60, 300, 900][min(attempts-1, 3)] * random.uniform(1, 1.1)
        self.state.set_meta('retry', dict(attempts=attempts, error=str(exc) if isinstance(exc, QbError) else '宿主查询或状态存储失败，请检查权限及磁盘',
                                        paused=permanent, next_retry_at=None if permanent else self.wall()+delay))

    def run(self, force=False):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.stopped.is_set():
                return
            config = self.get_config()
            if not config['enabled']:
                self.invalidate()
                return
            if not force and self.last_run is not None and self.monotonic()-self.last_run < config['poll_seconds']:
                return
            if self.last_run is not None and self.monotonic()-self.last_run > max(config['poll_seconds'] * 3, 30):
                self.quiet.clear()
            self.last_run = self.monotonic()
            binding = fingerprint(config)
            if self.state.meta('config') != binding:
                self.invalidate()
                self.state.set_meta('config', binding)
                self.state.set_meta('retry', {})
            retry = self.state.meta('retry', {})
            if not force and (retry.get('paused') or (retry.get('next_retry_at') or 0) > self.wall()):
                return
            self._poll(config)
            self.state.set_meta('retry', {})
            self.error = ''
            if self.wall() - self.state.meta('last_prune', 0) > 86400:
                self.state.prune(self.wall())
                self.state.set_meta('last_prune', self.wall())
        except Exception as exc:
            self.quiet.clear()
            self.error = '检查失败：' + (str(exc) if isinstance(exc, QbError) else type(exc).__name__)
            try:
                self._retry(exc)
            except Exception:
                self.error = '状态数据库不可写或已损坏，已停止删除；请检查磁盘及数据库'
        finally:
            self.lock.release()

    def _poll(self, config):
        binding = fingerprint(config)
        instance = fingerprint(config['base_url'])
        if self.client is None or self.client_config != binding:
            self.client = self.client_factory(config)
            self.client.login()
            self.client_config = binding
        client = self.client
        torrents = client.json('torrents/info')
        by_hash = {t['hash']: t for t in torrents if valid_hash(t.get('hash'))}
        # Reconcile uncertain deletes before considering any new authorization.
        pending = list(self.state.active())
        for row in pending:
            if row['instance'] != instance:
                if row['status'] not in ('out_of_scope', 'deleting'):
                    self._save(row, 'out_of_scope', 'qB 实例已改变；七天后清理旧快照')
                continue
            current = by_hash.get(row['hash'])
            if row['status'] == 'deleting':
                if current is None or identity(instance, current) != row['key']:
                    self._save(row, 'completed', '已确认原种子代际不存在')
                else:
                    self._save(row, 'needs_inspection', '删除结果未确认，重新完整观察')
            elif current is not None and identity(instance, current) != row['key']:
                self._save(row, 'completed', '种子代际或保存路径已改变，旧观察结束')
            elif current is None:
                self._save(row, 'completed', '种子已不在 qB 中')
        host = self.read_host()
        ordered = sorted(by_hash)
        self.observation_interval = config['poll_seconds'] * max(1, math.ceil(len(ordered) / self.BATCH))
        cursor = self.state.meta('cursor', '')
        ordered = [h for h in ordered if h > cursor] + [h for h in ordered if h <= cursor]
        diagnostics = []
        for hash_value in ordered[:self.BATCH]:
            if self.stopped.is_set():
                return
            torrent = by_hash[hash_value]
            try:
                key = identity(instance, torrent)
            except ValueError as exc:
                diagnostics.append({'hash': hash_value, 'reason': str(exc)})
                continue
            old = self.state.get(key)
            if old and old['status'] in ('cancelled', 'completed'):
                continue
            files = client.json('torrents/files', {'hash': hash_value})
            try:
                result = evaluate(config, host, torrent, files)
            except (ValueError, TypeError, KeyError):
                diagnostics.append({'hash': hash_value, 'reason': '种子清单或任务字段无效'})
                self.quiet.pop(key, None)
                continue
            row = old or dict(key=key, hash=hash_value, instance=instance, name=torrent.get('name', hash_value),
                              added_on=torrent['added_on'], tasks=[])
            if not result['tasks'] and old and old.get('tasks') and not any(t['watch_rule_id'] in config['watcher_ids'] for t in old['tasks']):
                if old['status'] != 'out_of_scope':
                    self._save(row, 'out_of_scope', '已不属于所选 watcher；七天后清理快照')
                continue
            ready = self._observe(row, result, config, host)
            if ready:
                self._delete_if_unchanged(row, config, client)
        if ordered:
            self.state.set_meta('cursor', ordered[min(len(ordered), self.BATCH)-1])
        self.state.set_meta('diagnostics', diagnostics[:100])

    def _delete_if_unchanged(self, row, config, client):
        # Refresh qB first, host last: both calls are required; neither is atomic with deletion.
        torrents = client.json('torrents/info', {'hashes': row['hash']})
        current = next((t for t in torrents if t.get('hash') == row['hash']), None)
        if current is None:
            self._save(row, 'completed', '种子已不存在')
            return
        if identity(row['instance'], current) != row['key']:
            self._save(row, 'completed', '原种子代际已不存在')
            return
        files = client.json('torrents/files', {'hash': row['hash']})
        host = self.read_host()
        fresh_config = self.get_config()
        if self.stopped.is_set() or fresh_config != config or not fresh_config['enabled']:
            self.quiet.pop(row['key'], None)
            return
        result = evaluate(config, host, current, files)
        if not self._observe(row, result, config, host):
            return
        # Storage failure here must prevent the network mutation.
        self._save(row, 'deleting', '已记录删除意图，等待 qB 确认')
        if self.stopped.is_set():
            return
        client.remove([row['hash']])
        remaining = client.json('torrents/info', {'hashes': row['hash']})
        if not any(t.get('hash') == row['hash'] and identity(row['instance'], t) == row['key'] for t in remaining):
            self._save(row, 'completed', '种子任务已移除，下载数据保留')

    def status(self, limit=100, offset=0):
        with self.lock:
            rows = self.state.rows(limit, offset)
            for row in rows:
                row.pop('tasks', None)
                clock = self.quiet.get(row['key'])
                if row['status'] == 'quiet':
                    row['remaining_seconds'] = max(0, self.get_config()['quiet_seconds']-(self.monotonic()-clock[1])) if clock else self.get_config()['quiet_seconds']
                    if not clock:
                        row['reason'] = '等待恢复连续观察，安静期将重新开始'
            return dict(rows=rows, retry=self.state.meta('retry', {}), diagnostics=self.state.meta('diagnostics', []),
                        error=self.error, **self.state.stats())
