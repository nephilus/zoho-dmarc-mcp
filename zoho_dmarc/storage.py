"""Single-writer SQLite history. Readers open a genuinely read-only connection."""
import contextlib
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS reports(
 id INTEGER PRIMARY KEY, reporter TEXT NOT NULL, report_id TEXT NOT NULL,
 domain TEXT NOT NULL, begin INTEGER NOT NULL, end INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, policy TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0,
 UNIQUE(reporter,report_id,domain,begin,end,fingerprint));
CREATE INDEX IF NOT EXISTS report_period ON reports(begin,end);
CREATE TABLE IF NOT EXISTS aggregate_rows(
 id INTEGER PRIMARY KEY, report INTEGER NOT NULL REFERENCES reports(id),
 source_ip TEXT NOT NULL, count INTEGER NOT NULL, disposition TEXT NOT NULL,
 dkim TEXT NOT NULL, spf TEXT NOT NULL, header_from TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS row_report ON aggregate_rows(report);
CREATE TABLE IF NOT EXISTS work(
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, folder TEXT NOT NULL,
 message TEXT NOT NULL, attachment TEXT NOT NULL, received INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt INTEGER NOT NULL DEFAULT 0, reason TEXT, updated INTEGER NOT NULL,
 UNIQUE(account,folder,message,attachment));
CREATE TABLE IF NOT EXISTS provenance(
 work INTEGER NOT NULL REFERENCES work(id), report INTEGER NOT NULL REFERENCES reports(id),
 UNIQUE(work,report));
CREATE TABLE IF NOT EXISTS collection_runs(
 id INTEGER PRIMARY KEY, started INTEGER NOT NULL, finished INTEGER,
 kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'incomplete',
 pages INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0, reason TEXT);
CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.path) + ".lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("writer_already_running") from None
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        if self.db.execute("SELECT version FROM schema_version").fetchall()[0][0] != 1:
            self.close()
            raise RuntimeError("unsupported_schema")
        # Keep this connection open for the collector lifetime so WAL and SHM
        # exist before the reader's read-only volume becomes ready.
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()

    def checkpoint(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def enqueue(self, account, folder, message, attachment, received):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO work(account,folder,message,attachment,received,updated) VALUES(?,?,?,?,?,?)", (account, folder, message, attachment, received, int(time.time())))

    def ingest(self, work_id, reports):
        with self.db:
            for report in reports:
                key = tuple(report[name] for name in ("reporter", "report_id", "domain", "begin", "end"))
                self.db.execute("INSERT OR IGNORE INTO reports(reporter,report_id,domain,begin,end,fingerprint,policy) VALUES(?,?,?,?,?,?,?)", (*key, report["fingerprint"], json.dumps(report["policy"])))
                report_id = self.db.execute("SELECT id FROM reports WHERE reporter=? AND report_id=? AND domain=? AND begin=? AND end=? AND fingerprint=?", (*key, report["fingerprint"])).fetchone()[0]
                if not self.db.execute("SELECT 1 FROM aggregate_rows WHERE report=? LIMIT 1", (report_id,)).fetchone():
                    self.db.executemany("INSERT INTO aggregate_rows(report,source_ip,count,disposition,dkim,spf,header_from) VALUES(?,?,?,?,?,?,?)", ((report_id, *(row[name] for name in ("source_ip", "count", "disposition", "dkim", "spf", "header_from"))) for row in report["rows"]))
                variants = self.db.execute("SELECT COUNT(*) FROM reports WHERE reporter=? AND report_id=? AND domain=? AND begin=? AND end=?", key).fetchone()[0]
                if variants > 1:
                    self.db.execute("UPDATE reports SET conflict=1 WHERE reporter=? AND report_id=? AND domain=? AND begin=? AND end=?", key)
                self.db.execute("INSERT OR IGNORE INTO provenance VALUES(?,?)", (work_id, report_id))
            self.db.execute("UPDATE work SET state='complete',reason=NULL,updated=? WHERE id=?", (int(time.time()), work_id))

    def fail(self, work_id, reason, permanent=False):
        with self.db:
            attempts = self.db.execute("SELECT attempts FROM work WHERE id=?", (work_id,)).fetchone()[0] + 1
            self.db.execute("UPDATE work SET state=?,attempts=?,next_attempt=?,reason=?,updated=? WHERE id=?", ("rejected" if permanent else "retry", attempts, int(time.time()) + min(86400, 60 * 2**min(attempts, 10)), reason, int(time.time()), work_id))

    def backup(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = time.strftime("dmarc-%Y%m%dT%H%M%SZ.sqlite", time.gmtime())
        target = directory / name
        staging = directory / (name + ".partial")
        with sqlite3.connect(staging) as backup:
            self.db.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup_integrity_failed")
        staging.replace(target)
        digest = file_hash(target)
        (directory / (name + ".sha256")).write_text(digest + "\n")
        self.checkpoint("last_backup", {"at": int(time.time()), "file": name, "sha256": digest})
        return target


@contextlib.contextmanager
def reader(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        db.close()
