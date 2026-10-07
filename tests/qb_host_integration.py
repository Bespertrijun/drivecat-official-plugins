"""Run separately: python tests/qb_host_integration.py /path/to/DriveCat/backend.

Uses real host models/DbProxy and SQLAlchemyJobStore; only temporary databases.
"""
import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, sys.argv[1])
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models.watch import WatchRule, UploadTask, UploadTarget
from app.plugin.base import PluginContext
from app.core.scheduler import init_scheduler



async def run():
    with tempfile.TemporaryDirectory() as directory:
        # Distribution builds inject version from the tag; emulate that packaging step only.
        package = Path(directory) / 'plugin'
        shutil.copytree(ROOT / 'plugins/qb-cleanup', package)
        manifest = json.loads((package / 'manifest.json').read_text())
        (package / 'manifest.json').write_text(json.dumps({**manifest, 'version': '0.0.0'}))
        spec = importlib.util.spec_from_file_location('qb_host_integration_plugin', package / 'main.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        engine = create_engine('sqlite:///' + directory + '/host.sqlite3')
        for model in [WatchRule, UploadTarget, UploadTask]:
            model.__table__.create(engine)
        factory = sessionmaker(bind=engine)
        with factory() as db:
            db.add(WatchRule(id=1, name='demo', local_path='/downloads', post_action='delete', is_enabled=True))
            db.add(UploadTarget(id=1, watch_rule_id=1, is_enabled=True, remote_path='/'))
            for i in range(1, 1003):
                db.add(UploadTask(id=i, watch_rule_id=1, upload_target_id=1, local_path=f'/downloads/{i}.mkv',
                                  file_size=100, file_mtime=100, status='success', origin_type='watcher',
                                  remote_path='/', created_at=datetime.now(timezone.utc)))
            db.commit()
        statements = []
        @event.listens_for(engine, 'before_cursor_execute')
        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)
        context = PluginContext('qb-integration', hooks=MagicMock(), permissions=['db.read','fs.read','fs.write'],
                                db_factory=factory, app=FastAPI(), plugin_data_dir=directory)
        scheduler = init_scheduler('sqlite:///' + directory + '/jobs.sqlite3')
        scheduler.start(paused=True)
        plugin = module.QbCleanupPlugin()
        await plugin.on_load(context)
        snapshot = module.host_snapshot(context, [1])
        assert len(snapshot['tasks']) == 1002, 'pagination lost records'
        assert snapshot['tasks'][0]['created_at'] > 0
        assert len(snapshot['targets']) == 1
        assert all(not s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for s in statements), statements
        db = context.get_db()
        try:
            try:
                db.commit()
                raise AssertionError('host DB write unexpectedly permitted')
            except PermissionError:
                pass
        finally:
            db.close()
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        # Reading the job back from the persistent store exercises serialization too.
        job = scheduler._jobstores['default'].lookup_job(jobs[0].id)
        assert job is not None
        plugin.monitor.run = MagicMock()
        await job.func()
        plugin.monitor.run.assert_called_once()
        plugin.monitor.run.reset_mock()
        await plugin.on_unload()
        context.unregister_jobs()
        await job.func()
        plugin.monitor.run.assert_not_called()
        assert not scheduler.get_jobs()
        await plugin.on_load(context)
        assert len(scheduler.get_jobs()) == 1
        await plugin.on_unload()
        context.unregister_jobs()
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        engine.dispose()
    print('PASS: real host imports, 1002-row pagination, read-only DbProxy, persistent scheduling, unload/reload')


asyncio.run(run())
