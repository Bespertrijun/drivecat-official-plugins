"""Plugin-owned durable state. No host database writes or shared connections."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class State:
    def __init__(self, directory):
        self.path = Path(directory) / 'state.sqlite3'
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError('不支持的状态数据库版本')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS candidates (
                key TEXT PRIMARY KEY, status TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS candidate_status ON candidates(status, updated)')
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA busy_timeout=5000')
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT data FROM candidates WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, row):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO candidates VALUES (?,?,?,?)',
                       (row['key'], row['status'], row['updated'], json.dumps(row, ensure_ascii=False)))

    def rows(self, limit=100, offset=0, status=None):
        with self.connect() as db:
            where, args = ('WHERE status=?', [status]) if status else ('', [])
            rows = db.execute(f'SELECT data FROM candidates {where} ORDER BY updated DESC, key LIMIT ? OFFSET ?',
                              [*args, limit, offset]).fetchall()
            return [json.loads(row[0]) for row in rows]

    def active(self):
        offset = 0
        while True:
            with self.connect() as db:
                rows = db.execute("SELECT data FROM candidates WHERE status NOT IN ('completed','cancelled') ORDER BY key LIMIT 100 OFFSET ?", (offset,)).fetchall()
            if not rows:
                return
            for row in rows:
                yield json.loads(row[0])
            offset += len(rows)

    def clear_completed(self):
        with self.connect() as db:
            db.execute("DELETE FROM candidates WHERE status='completed'")

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("DELETE FROM candidates WHERE status='completed' AND updated < ?", (now - 30*86400,))
            db.execute("DELETE FROM candidates WHERE key IN (SELECT key FROM candidates WHERE status='completed' ORDER BY updated DESC LIMIT -1 OFFSET 10000)")
            # Inactive snapshots are disposable; cancellation tombstones are not.
            db.execute("DELETE FROM candidates WHERE status='out_of_scope' AND updated < ?", (now - 7*86400,))

        # Reclaim large idle databases only after retention cleanup, never on every poll.
        with self.connect() as db:
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            free = db.execute('PRAGMA freelist_count').fetchone()[0]
            if self.path.stat().st_size > 32 * 1024 * 1024 and free > pages // 2:
                db.execute('VACUUM')

    def stats(self):
        with self.connect() as db:
            counts = dict(db.execute('SELECT status, COUNT(*) FROM candidates GROUP BY status').fetchall())
        return {'bytes': self.path.stat().st_size, 'counts': counts}
