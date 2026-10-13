"""Plugin-owned SQLite queue; completed files leave no permanent deduplication rows."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from onestrm_config import load_config, target_key


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / "state.sqlite3"
        with self.connect(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("不支持的 OneStrm 状态数据库版本")
            statements = [
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS targets (
                    key TEXT PRIMARY KEY, drive_id INTEGER NOT NULL, drive_name TEXT NOT NULL,
                    base_url TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""",
                """CREATE TABLE IF NOT EXISTS intervals (
                    id INTEGER PRIMARY KEY, target TEXT NOT NULL REFERENCES targets(key),
                    started REAL NOT NULL, ended REAL, watermark REAL NOT NULL,
                    round_end REAL, cursor_time REAL, cursor_id INTEGER NOT NULL DEFAULT 0,
                    next_check REAL NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL REFERENCES targets(key),
                    task_id INTEGER, remote_path TEXT NOT NULL, received REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '', UNIQUE(target, task_id))""",
                """CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL REFERENCES targets(key),
                    path TEXT NOT NULL, status TEXT NOT NULL, first_at REAL NOT NULL,
                    last_at REAL NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    last_request REAL, updated REAL NOT NULL, error TEXT NOT NULL DEFAULT '',
                    http_status INTEGER, manual INTEGER NOT NULL DEFAULT 0)""",
                "CREATE INDEX IF NOT EXISTS batch_ready ON batches(target, status, due)",
                "CREATE INDEX IF NOT EXISTS batch_directory ON batches(target, path, status)",
                """CREATE TABLE IF NOT EXISTS members (
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL, PRIMARY KEY(batch_id, task_id))""",
                "CREATE INDEX IF NOT EXISTS member_task ON members(task_id)",
                """CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL REFERENCES targets(key),
                    path TEXT NOT NULL, status TEXT NOT NULL, file_count INTEGER NOT NULL,
                    attempts INTEGER NOT NULL, last_request REAL, updated REAL NOT NULL,
                    error TEXT NOT NULL, http_status INTEGER)""",
                "CREATE INDEX IF NOT EXISTS history_updated ON history(updated)",
            ]
            for statement in statements:
                db.execute(statement)
            db.execute("PRAGMA user_version=1")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def meta(db, key, default=None):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def sync_config(self, db, config):
        """Replay an atomic config save if a crash interrupted its SQLite update."""
        previous = self.meta(db, "config", {})
        if previous.get("revision") == config["revision"]:
            return
        key = target_key(config)
        now = config["changed_at"]
        if key:
            db.execute("""INSERT INTO targets(key,drive_id,drive_name,base_url) VALUES (?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET drive_name=excluded.drive_name""",
                       (key, config["drive_id"], config["drive_name"], config["base_url"]))
            if previous.get("auth_revision") != config["auth_revision"]:
                db.execute("UPDATE targets SET paused=0,error='' WHERE key=?", (key,))
                db.execute("""UPDATE batches SET status='waiting',attempts=0,due=?,error=''
                    WHERE target=? AND status='auth'""", (now, key))
        active = key if config["enabled"] else ""
        if previous.get("active", "") != active:
            db.execute("UPDATE intervals SET ended=? WHERE ended IS NULL", (now,))
            if active:
                db.execute("INSERT INTO intervals(target,started,watermark) VALUES (?,?,?)", (active, now, now))
        # Credentials stay exclusively in config.json, never in queue/history.
        self.set_meta(db, "config", {"revision": config["revision"], "active": active,
                                     "auth_revision": config["auth_revision"]})

    def current(self, db):
        config = load_config(self.directory)
        self.sync_config(db, config)
        return config

    def capture(self, data, now=None):
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            config = self.current(db)
            if not config["enabled"] or data.get("drive_config_id") != config["drive_id"]:
                return
            task_id = data.get("upload_task_id")
            if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
                task_id = None
            path = data.get("remote_path")
            path = path if isinstance(path, str) else ""
            db.execute("""INSERT OR IGNORE INTO inbox(target,task_id,remote_path,received,error)
                VALUES (?,?,?,?,?)""", (target_key(config), task_id, path, now,
                                        "缺少上传任务 ID，待核查" if task_id is None else ""))

    def clues(self, key):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM inbox WHERE target=? AND error='' ORDER BY id LIMIT 500", (key,))]

    @staticmethod
    def eligible(db, key, completed):
        return db.execute("""SELECT 1 FROM intervals WHERE target=? AND started<=?
            AND (ended IS NULL OR ended>?) LIMIT 1""", (key, completed, completed)).fetchone() is not None

    @staticmethod
    def enqueue(db, key, task_id, path, now, quiet):
        # Deduplication is confined to unfinished batches. A successful batch deletes its members.
        if task_id is not None and db.execute("""SELECT 1 FROM members m JOIN batches b ON b.id=m.batch_id
            WHERE b.target=? AND m.task_id=? LIMIT 1""", (key, task_id)).fetchone():
            return
        row = db.execute("""SELECT * FROM batches WHERE target=? AND path=? AND status!='sending'
            AND manual=0 ORDER BY id LIMIT 1""", (key, path)).fetchone() if task_id is not None else None
        if row:
            batch_id = row["id"]
            # Reconciliation never resets a retry budget, failure or Retry-After.
            due = min(now + quiet, row["first_at"] + 60) if row["status"] == "waiting" else row["due"]
            db.execute("UPDATE batches SET last_at=?,updated=?,due=? WHERE id=?", (now, now, due, batch_id))
        else:
            cursor = db.execute("""INSERT INTO batches(target,path,status,first_at,last_at,due,updated,manual)
                VALUES (?,?,'waiting',?,?,?,?,?)""", (key, path, now, now, now + quiet, now, int(task_id is None)))
            batch_id = cursor.lastrowid
        if task_id is not None:
            db.execute("INSERT OR IGNORE INTO members VALUES (?,?)", (batch_id, task_id))
        return batch_id

    def consume_clues(self, key, results, now, quiet):
        with self.connect(write=True) as db:
            self.current(db)
            for clue, task, error in results:
                if not db.execute("SELECT 1 FROM inbox WHERE id=?", (clue["id"],)).fetchone():
                    continue
                if error:
                    db.execute("UPDATE inbox SET error=? WHERE id=?", (error, clue["id"]))
                else:
                    if task and self.eligible(db, key, task["completed_at"]):
                        self.enqueue(db, key, task["id"], task["path"], now, quiet)
                    db.execute("DELETE FROM inbox WHERE id=?", (clue["id"],))

    def begin_page(self, key, now):
        with self.connect(write=True) as db:
            self.current(db)
            row = db.execute("""SELECT * FROM intervals WHERE target=? AND next_check<=?
                AND (ended IS NULL OR watermark<ended) ORDER BY id LIMIT 1""", (key, now)).fetchone()
            if not row:
                return None
            row = dict(row)
            if row["round_end"] is None:
                upper = min(now - 5, row["ended"]) if row["ended"] is not None else now - 5
                if upper < row["watermark"]:
                    if now < row["watermark"]:
                        self.set_meta(db, "error", "完成时间水位晚于当前时间；等待时间恢复后再补查")
                    return None
                if upper <= row["started"]:
                    return None
                row.update(round_end=upper, cursor_time=max(row["started"], row["watermark"] - 120), cursor_id=0)
                db.execute("UPDATE intervals SET round_end=?,cursor_time=?,cursor_id=0 WHERE id=?",
                           (upper, row["cursor_time"], row["id"]))
            return row

    def commit_page(self, interval, tasks, count, cursor, now, quiet):
        with self.connect(write=True) as db:
            self.current(db)
            current = db.execute("SELECT * FROM intervals WHERE id=?", (interval["id"],)).fetchone()
            if not current or any(current[k] != interval[k] for k in ("round_end", "cursor_time", "cursor_id")):
                return  # Another invocation already committed this page.
            for task, error in tasks:
                if not self.eligible(db, interval["target"], task["completed_at"]):
                    continue
                if error:
                    db.execute("""INSERT OR IGNORE INTO inbox(target,task_id,remote_path,received,error)
                        VALUES (?,?,?,?,?)""", (interval["target"], task["id"], task.get("remote_path") or "", now, error))
                else:
                    self.enqueue(db, interval["target"], task["id"], task["path"], now, quiet)
            if count < 500:
                db.execute("""UPDATE intervals SET watermark=round_end,round_end=NULL,cursor_time=NULL,
                    cursor_id=0,next_check=? WHERE id=?""", (now + 60, interval["id"]))
            else:
                db.execute("UPDATE intervals SET cursor_time=?,cursor_id=? WHERE id=?", (*cursor, interval["id"]))

    def claim(self, now, manual_id=None):
        with self.connect(write=True) as db:
            config = self.current(db)
            key = target_key(config)
            # One request at a time, including explicit test scans and overlapping scheduler calls.
            if db.execute("SELECT 1 FROM batches WHERE status='sending' AND lease_until>?", (now,)).fetchone():
                return None
            db.execute("""UPDATE batches SET status=CASE WHEN attempts>=4 THEN 'exhausted' ELSE 'retry' END,
                lease='',lease_until=0,error='请求结果未知，租约已过期',updated=?
                WHERE status='sending' AND lease_until<=?""", (now, now))
            if not config["enabled"] and manual_id is None:
                return None
            target = db.execute("SELECT * FROM targets WHERE key=?", (key,)).fetchone()
            if not target or target["retry_at"] > now or (target["paused"] and manual_id is None):
                return None
            if manual_id is None:
                row = db.execute("""SELECT * FROM batches WHERE target=? AND status IN ('waiting','retry')
                    AND due<=? ORDER BY due,id LIMIT 1""", (key, now)).fetchone()
            else:
                row = db.execute("""SELECT * FROM batches WHERE id=? AND target=?
                    AND status IN ('waiting','retry')""", (manual_id, key)).fetchone()
            if not row:
                return None
            lease = uuid.uuid4().hex
            db.execute("""UPDATE batches SET status='sending',attempts=attempts+1,lease=?,lease_until=?,
                last_request=?,updated=? WHERE id=?""", (lease, now + config["timeout_seconds"] + 30, now, now, row["id"]))
            return {**dict(row), "lease": lease, "attempts": row["attempts"] + 1, "config": config}

    @staticmethod
    def archive(db, row, status, now, error="", http_status=None):
        count = db.execute("SELECT COUNT(*) FROM members WHERE batch_id=?", (row["id"],)).fetchone()[0]
        db.execute("""INSERT INTO history(target,path,status,file_count,attempts,last_request,updated,error,http_status)
            VALUES (?,?,?,?,?,?,?,?,?)""", (row["target"], row["path"], status, count,
                                            row["attempts"], row["last_request"], now, error, http_status))
        db.execute("DELETE FROM batches WHERE id=?", (row["id"],))

    def finish(self, claimed, result, now):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM batches WHERE id=? AND lease=? AND status='sending'",
                             (claimed["id"], claimed["lease"])).fetchone()
            if not row:
                return
            if result.ok:
                self.archive(db, row, "success", now, http_status=result.http_status)
                return
            status = "failed"
            due = now
            if result.kind == "auth":
                status = "auth"
                db.execute("UPDATE targets SET paused=1,error=? WHERE key=?", (result.error, row["target"]))
            elif result.retryable:
                status = "retry" if row["attempts"] < 4 else "exhausted"
                delay = (30, 120, 600)[min(row["attempts"] - 1, 2)]
                due = max(now + delay, result.retry_at or 0)
                if result.kind == "rate_limit":
                    db.execute("UPDATE targets SET retry_at=MAX(retry_at,?) WHERE key=?", (due, row["target"]))
            db.execute("""UPDATE batches SET status=?,due=?,lease='',lease_until=0,error=?,
                http_status=?,updated=? WHERE id=?""", (status, due, result.error, result.http_status, now, row["id"]))

    def retry(self, batch_id, now):
        with self.connect(write=True) as db:
            config = self.current(db)
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError("记录不存在")
            if row["target"] != target_key(config) or row["status"] == "sending":
                raise ValueError("请恢复对应配置，或等待当前请求结束")
            if row["status"] not in ("retry", "exhausted", "failed", "auth"):
                raise ValueError("该记录无需重试")
            target = db.execute("SELECT * FROM targets WHERE key=?", (row["target"],)).fetchone()
            if target["paused"]:
                raise ValueError("请先修改鉴权配置解除暂停")
            db.execute("UPDATE batches SET status='waiting',attempts=0,due=?,updated=?,error='' WHERE id=?",
                       (max(now, target["retry_at"]), now, batch_id))

    def cancel(self, kind, record_id, now):
        with self.connect(write=True) as db:
            if kind == "inbox":
                row = db.execute("SELECT * FROM inbox WHERE id=?", (record_id,)).fetchone()
                if not row:
                    raise KeyError("记录不存在")
                db.execute("""INSERT INTO history(target,path,status,file_count,attempts,updated,error)
                    VALUES (?,?,'cancelled',1,0,?,?)""", (row["target"], row["remote_path"], now, row["error"]))
                db.execute("DELETE FROM inbox WHERE id=?", (record_id,))
                return
            row = db.execute("SELECT * FROM batches WHERE id=?", (record_id,)).fetchone()
            if not row:
                raise KeyError("记录不存在")
            if row["status"] == "sending":
                raise ValueError("请求已经发出，请等待结果后操作")
            self.archive(db, row, "cancelled", now)

    def clear_history(self):
        with self.connect(write=True) as db:
            db.execute("DELETE FROM history")

    def maintain(self, now):
        with self.connect(write=True) as db:
            if now - self.meta(db, "last_prune", 0) < 86400:
                return
            db.execute("DELETE FROM history WHERE updated<?", (now - 30 * 86400,))
            db.execute("""DELETE FROM history WHERE id IN
                (SELECT id FROM history ORDER BY updated DESC,id DESC LIMIT -1 OFFSET 10000)""")
            self.set_meta(db, "last_prune", now)
        with self.connect() as db:
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
            busy = db.execute("SELECT 1 FROM batches WHERE status='sending'").fetchone()
            if not busy and self.path.stat().st_size > 32 * 1024 * 1024 and free > pages // 2:
                db.execute("VACUUM")

    def diagnostic(self, message):
        with self.connect(write=True) as db:
            self.set_meta(db, "error", message)

    def status(self, limit=50, offset=0):
        with self.connect(write=True) as db:
            config = self.current(db)
            key = target_key(config)
            # Separate histories from live work so a long history cannot hide a failed queue.
            queue = """SELECT 'batch' AS kind,b.id,b.target,b.path,b.status,
                (SELECT COUNT(*) FROM members m WHERE m.batch_id=b.id) AS file_count,
                b.attempts,b.last_request,b.updated,b.error,b.due,b.http_status FROM batches b
                UNION ALL SELECT 'inbox',id,target,remote_path,'inspection',1,0,NULL,received,error,0,NULL FROM inbox"""
            rows = [dict(r) for r in db.execute(f"SELECT * FROM ({queue}) ORDER BY updated DESC,kind,id LIMIT ? OFFSET ?", (limit, offset))]
            history = [dict(r) for r in db.execute("SELECT * FROM history ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?", (limit, offset))]
            targets = {r["key"]: dict(r) for r in db.execute("SELECT * FROM targets")}
            for row in rows + history:
                row["drive_name"] = targets[row["target"]]["drive_name"]
                row["drive_id"] = targets[row["target"]]["drive_id"]
                row["base_url"] = targets[row["target"]]["base_url"]
                row["config_changed"] = row["target"] != key
            return {"rows": rows, "history": history, "enabled": config["enabled"],
                    "paused": bool(targets.get(key, {}).get("paused")),
                    "target_error": targets.get(key, {}).get("error", ""),
                    "error": self.meta(db, "error", ""),
                    "pending_count": db.execute(f"SELECT COUNT(*) FROM ({queue})").fetchone()[0],
                    "history_count": db.execute("SELECT COUNT(*) FROM history").fetchone()[0],
                    "bytes": self.path.stat().st_size}
