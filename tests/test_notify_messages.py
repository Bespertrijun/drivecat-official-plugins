import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/notify'))
from notify_messages import format_event  # noqa: E402

GB = 1024 ** 3


def fields_of(message):
    """去掉「时间」字段（值随时间变化），其余按顺序返回。"""
    return [(label, value) for label, value in message.fields if label != '时间']


def field(message, label):
    values = [value for lbl, value in message.fields if lbl == label]
    return values


def old_host_sync_payload(**overrides):
    """旧宿主 after_sync 载荷：目标为均衡组时 target_drive_* 为 None，无新增键。"""
    data = {
        'batch_id': 42,
        'kind': 'sync_run',
        'name': '动漫迁移',
        'sync_rule_id': 7,
        'source_drive_id': 1,
        'source_drive_name': '115me',
        'target_drive_id': None,
        'target_drive_name': None,
        'source_paths': ['/anime'],
        'target_path': '/动漫',
        'total_files': 497,
        'success_count': 228,
        'failed_count': 0,
        'skipped_count': 0,
        'cancelled_count': 0,
        'deferred_count': 269,
        'skipped_existing': 0,
        'skipped_unsupported': 0,
        'total_bytes': int(512.39 * GB),
        'success_bytes': int(199.99 * GB),
    }
    data.update(overrides)
    return data


def new_host_sync_payload(**overrides):
    data = old_host_sync_payload(
        target_type='balance_group',
        balance_rule_id=3,
        balance_rule_name='XX',
        target_drive_names=['A', 'B'],
        deferred_bytes=int(312.40 * GB),
        deferred_reasons=['bytes'],
        byte_budget=200 * GB,
        deadline_at='2026-11-11T06:00:00',
    )
    data.update(overrides)
    return data


class SyncRunDeferredTest(unittest.TestCase):
    def test_user_scenario_new_host(self):
        msg = format_event('after_sync', new_host_sync_payload())
        self.assertEqual(msg.emoji, '🔄')
        self.assertEqual(msg.title, '同步完成 · 已达流量上限')
        self.assertEqual(msg.level, 'success')
        self.assertEqual(fields_of(msg), [
            ('规则', '动漫迁移'),
            ('目标', '/动漫'),
            ('网盘', '115me → 均衡组「XX」（实际写入：A、B）'),
            ('文件', '成功 228 · 失败 0 · 顺延 269 · 共 497'),
            ('大小', '199.99 GB（本轮上限 200.00 GB）'),
            ('说明', '剩余 269 个文件（312.40 GB）顺延到下一轮'),
        ])
        self.assertEqual(msg.fields[-1][0], '时间')

    def test_user_scenario_old_host(self):
        msg = format_event('after_sync', old_host_sync_payload())
        self.assertEqual(msg.title, '同步完成 · 已达本轮上限')
        self.assertEqual(msg.level, 'success')
        self.assertEqual(fields_of(msg), [
            ('规则', '动漫迁移'),
            ('目标', '/动漫'),
            ('网盘', '115me → 均衡组'),
            ('文件', '成功 228 · 失败 0 · 顺延 269 · 共 497'),
            ('大小', '199.99 GB'),
            ('说明', '剩余 269 个文件顺延到下一轮'),
        ])

    def test_deadline_only_title(self):
        msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=['deadline']))
        self.assertEqual(msg.title, '同步完成 · 已达时长上限')

    def test_both_reasons_title(self):
        msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=['bytes', 'deadline']))
        self.assertEqual(msg.title, '同步完成 · 已达流量 / 时长上限')

    def test_empty_reasons_title(self):
        msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=[]))
        self.assertEqual(msg.title, '同步完成 · 已达本轮上限')

    def test_no_deferred_regression(self):
        data = new_host_sync_payload(
            total_files=228, deferred_count=0, deferred_bytes=0, deferred_reasons=[],
        )
        msg = format_event('after_sync', data)
        self.assertEqual(msg.title, '同步完成')
        self.assertEqual(field(msg, '说明'), [])
        self.assertEqual(field(msg, '文件'), ['成功 228 · 失败 0 · 共 228'])
        # 未顺延时不提示本轮上限
        self.assertEqual(field(msg, '大小'), ['199.99 GB'])

    def test_deferred_bytes_zero_omits_size(self):
        msg = format_event('after_sync', new_host_sync_payload(deferred_bytes=0))
        self.assertEqual(field(msg, '说明'), ['剩余 269 个文件顺延到下一轮'])

    def test_on_error_sync_run_failed_deferred_aborted(self):
        data = new_host_sync_payload(
            source='sync_run',
            message='同步有文件失败',
            aborted=True,
            error='网盘限流',
            failed_count=5,
            success_count=223,
        )
        msg = format_event('on_error', data)
        self.assertEqual(msg.emoji, '⚠️')
        self.assertEqual(msg.title, '同步异常 · 已达流量上限')
        self.assertEqual(msg.level, 'warning')
        self.assertEqual(field(msg, '文件'), ['成功 223 · 失败 5 · 顺延 269 · 共 497'])
        self.assertEqual(field(msg, '说明'), [
            '剩余 269 个文件（312.40 GB）顺延到下一轮',
            '同步中途出错（网盘限流），仅统计已提交的文件',
        ])
        labels = [label for label, _ in msg.fields]
        self.assertLess(labels.index('大小'), labels.index('说明'))

    def test_on_error_without_success_is_error_level(self):
        data = old_host_sync_payload(source='sync_run', success_count=0, failed_count=228,
                                     success_bytes=0)
        msg = format_event('on_error', data)
        self.assertEqual(msg.title, '同步异常 · 已达本轮上限')
        self.assertEqual(msg.level, 'error')

    def test_target_drive_names_truncated_after_three(self):
        data = new_host_sync_payload(target_drive_names=['A', 'B', 'C', 'D', 'E'])
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '网盘'), ['115me → 均衡组「XX」（实际写入：A、B、C 等 5 个）'])

    def test_target_type_balance_group_without_rule_name(self):
        data = new_host_sync_payload(balance_rule_name=None, target_drive_names=['A'])
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '网盘'), ['115me → 均衡组（实际写入：A）'])

    def test_stale_target_drive_with_balance_group_shows_group(self):
        # 规则从单盘改成均衡组后，宿主仍可能带着旧网盘的 id / 名称
        data = new_host_sync_payload(target_drive_id=5, target_drive_name='旧单盘')
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '网盘'), ['115me → 均衡组「XX」（实际写入：A、B）'])

    def test_stale_target_drive_with_unnamed_balance_group(self):
        data = new_host_sync_payload(target_drive_id=5, target_drive_name='旧单盘',
                                     balance_rule_name=None, target_drive_names=[])
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '网盘'), ['115me → 均衡组'])

    def test_explicit_drive_target_without_drive_is_question_mark(self):
        data = old_host_sync_payload(target_type='drive')
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '网盘'), ['115me → ?'])

    def test_deadline_only_hides_byte_cap(self):
        msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=['deadline']))
        self.assertEqual(field(msg, '大小'), ['199.99 GB'])

    def test_both_reasons_show_byte_cap(self):
        data = new_host_sync_payload(deferred_reasons=['bytes', 'deadline'])
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '大小'), ['199.99 GB（本轮上限 200.00 GB）'])

    def test_missing_reasons_with_budget_show_byte_cap(self):
        data = old_host_sync_payload(byte_budget=200 * GB)
        msg = format_event('after_sync', data)
        self.assertEqual(field(msg, '大小'), ['199.99 GB（本轮上限 200.00 GB）'])

    def test_malformed_reasons_do_not_crash(self):
        for reasons in ([['bytes']], [{'r': 1}, None], 'bytes', 5):
            with self.subTest(reasons=reasons):
                msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=reasons))
                self.assertEqual(msg.title, '同步完成 · 已达本轮上限')
        msg = format_event('after_sync', new_host_sync_payload(deferred_reasons=[['x'], 'bytes']))
        self.assertEqual(msg.title, '同步完成 · 已达流量上限')

    def test_long_balance_rule_name_is_shortened(self):
        data = new_host_sync_payload(balance_rule_name='组' * 50, target_drive_names=[])
        value = field(format_event('after_sync', data), '网盘')[0]
        self.assertEqual(value, '115me → 均衡组「' + '组' * 29 + '…」')


class FolderBatchTest(unittest.TestCase):
    def folder_payload(self, **overrides):
        data = {
            'batch_id': 9,
            'kind': 'folder',
            'name': '剧集',
            'sync_rule_id': None,
            'source_drive_id': 1,
            'source_drive_name': 'A',
            'target_drive_id': 2,
            'target_drive_name': 'B',
            'source_paths': ['/tv'],
            'target_path': '/剧集',
            'total_files': 10,
            'success_count': 10,
            'failed_count': 0,
            'skipped_count': 0,
            'cancelled_count': 0,
            'deferred_count': 0,
            'skipped_existing': 0,
            'skipped_unsupported': 0,
            'total_bytes': 5 * GB,
            'success_bytes': 5 * GB,
        }
        data.update(overrides)
        return data

    def test_folder_batch_with_real_target_drive(self):
        msg = format_event('after_folder_transfer', self.folder_payload())
        self.assertEqual(msg.emoji, '📦')
        self.assertEqual(msg.title, '文件夹转存完成')
        self.assertEqual(msg.level, 'success')
        self.assertEqual(fields_of(msg), [
            ('名称', '剧集'),
            ('目标', '/剧集'),
            ('网盘', 'A → B'),
            ('文件', '成功 10 · 失败 0 · 共 10'),
            ('大小', '5.00 GB'),
        ])

    def test_folder_batch_without_target_drive_is_question_mark(self):
        data = self.folder_payload(target_drive_id=None, target_drive_name=None)
        msg = format_event('after_folder_transfer', data)
        self.assertEqual(field(msg, '网盘'), ['A → ?'])

    def test_folder_batch_ignores_target_drive_names_for_plain_drive(self):
        data = self.folder_payload(target_type='drive', target_drive_names=['B'])
        msg = format_event('after_folder_transfer', data)
        self.assertEqual(field(msg, '网盘'), ['A → B'])

    def test_folder_batch_deferred_title(self):
        data = self.folder_payload(total_files=10, success_count=4, deferred_count=6,
                                   deferred_reasons=['deadline'])
        msg = format_event('after_folder_transfer', data)
        self.assertEqual(msg.title, '文件夹转存完成 · 已达时长上限')
        self.assertEqual(field(msg, '说明'), ['剩余 6 个文件顺延到下一轮'])

    def test_skipped_count_vs_skipped_existing(self):
        data = self.folder_payload(total_files=10, success_count=6, failed_count=1,
                                   skipped_count=2, cancelled_count=1, skipped_existing=4)
        msg = format_event('after_folder_transfer', data)
        self.assertEqual(field(msg, '文件'),
                         ['成功 6 · 失败 1 · 跳过 2 · 取消 1 · 共 10（另跳过已存在 4）'])

    def test_skipped_existing_only(self):
        data = self.folder_payload(skipped_existing=3)
        msg = format_event('after_folder_transfer', data)
        self.assertEqual(field(msg, '文件'), ['成功 10 · 失败 0 · 共 10（另跳过已存在 3）'])


class TransferUnchangedTest(unittest.TestCase):
    def test_transfer_output_unchanged(self):
        data = {
            'rel_path': 'S01/E01.mkv',
            'sync_rule_id': 7,
            'origin_label': '动漫迁移',
            'source_path': '/anime/S01/E01.mkv',
            'target_path': '/动漫/S01/E01.mkv',
            'source_drive_name': '115me',
            'target_drive_name': 'A',
            'file_size': 1 * GB,
        }
        msg = format_event('after_transfer', data)
        self.assertEqual(msg.emoji, '✅')
        self.assertEqual(msg.title, '转存完成')
        self.assertEqual(msg.level, 'success')
        self.assertEqual(fields_of(msg), [
            ('同步', '动漫迁移'),
            ('文件', 'S01/E01.mkv'),
            ('路径', '/anime/S01/E01.mkv → /动漫/S01/E01.mkv'),
            ('网盘', '115me → A'),
            ('大小', '1.00 GB'),
        ])

    def test_transfer_without_target_drive_keeps_question_mark(self):
        data = {'filename': 'a.mkv', 'source_drive_name': '115me'}
        msg = format_event('after_transfer', data)
        self.assertEqual(field(msg, '网盘'), ['115me → ?'])


if __name__ == '__main__':
    unittest.main()
