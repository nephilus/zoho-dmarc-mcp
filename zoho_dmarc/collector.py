"""Durable replay and collection scheduling; never retain raw attachments."""
import json
import hashlib
import os
from pathlib import Path
import signal
import time
from .config import Config, database
from .parser import parse_bounded, Rejected
from .storage import Writer
from .zoho import Zoho, APIError


class Collector:
    def __init__(self, config, writer, api):
        self.config, self.writer, self.api = config, writer, api

    def pending(self):
        # Iterate by key, not a fixed LIMIT that could skip a remaining backlog.
        cursor = 0
        while True:
            self.api._check_deadline()
            item = self.writer.db.execute("SELECT * FROM work WHERE state IN ('pending','retry') AND next_attempt<=? AND id>? ORDER BY id LIMIT 1", (int(time.time()), cursor)).fetchone()
            if item is None:
                return
            cursor = item["id"]
            try:
                raw = self.api.download(item["message"], item["attachment"])
                try:
                    reports = parse_bounded(raw, self.config.domains)
                finally:
                    del raw
                self.writer.ingest(item["id"], reports)
            except Rejected as exc:
                self.writer.fail(item["id"], str(exc), permanent=True)
            except APIError as exc:
                self.writer.fail(item["id"], str(exc), permanent=str(exc) == "compressed_limit")
                if str(exc) in {"scan_time_limit", "oauth_refresh_failed", "account_owner_mismatch"}:
                    raise

    def scan(self, kind):
        self.api.deadline = time.monotonic() + self.config.scan_seconds
        now = int(time.time())
        with self.writer.db:
            run = self.writer.db.execute("INSERT INTO collection_runs(started,kind) VALUES(?,?)", (now, kind)).lastrowid
        pages = messages = 0
        try:
            self.api.validate_owner()
            # Resume previously discovered work before starting another complete
            # metadata inventory. A large or interrupted inventory cannot starve
            # attachments already queued durably.
            self.pending()
            seen_ids = set()
            seen_pages = set()
            start = 1
            inventory = hashlib.sha256()
            cutoff = now - 7 * 86400 if kind == "poll" else 0
            while True:
                batch = self.api.messages(start)
                if not isinstance(batch, list):
                    raise APIError("invalid_api_response")
                if not batch:
                    break
                ids = tuple(str(item.get("messageId", "")) for item in batch)
                if not all(item.isdigit() for item in ids) or len(set(ids)) != len(ids) or ids in seen_pages:
                    raise APIError("unstable_pagination")
                seen_pages.add(ids)
                for message_id in ids:
                    inventory.update((message_id + "\n").encode())
                if seen_ids.intersection(ids):
                    raise APIError("unstable_pagination")
                seen_ids.update(ids)
                pages += 1
                messages += len(batch)
                for item, message_id in zip(batch, ids):
                    try:
                        received = int(item["receivedTime"]) // 1000
                    except (KeyError, ValueError, TypeError):
                        raise APIError("invalid_receipt_time") from None
                    if received < cutoff:
                        continue
                    for attachment in self.api.attachments(message_id):
                        attachment_id = str(attachment.get("attachmentId", ""))
                        if not attachment_id.isdigit():
                            raise APIError("invalid_attachment_id")
                        self.writer.enqueue(self.config.account, self.config.folder, message_id, attachment_id, received)
                with self.writer.db:
                    self.writer.db.execute("UPDATE collection_runs SET pages=?,messages=? WHERE id=?", (pages, messages, run))
                self.pending()
                # Always request the next page, including after short pages.
                start += len(batch)
            # Offset pagination has no snapshot guarantee. A second complete
            # inventory pass must agree before this scan can claim completion.
            check = hashlib.sha256()
            start = 1
            check_seen = set()
            while True:
                batch = self.api.messages(start)
                if not isinstance(batch, list):
                    raise APIError("invalid_api_response")
                if not batch:
                    break
                for item in batch:
                    message_id = str(item.get("messageId", ""))
                    if not message_id.isdigit() or message_id in check_seen:
                        raise APIError("unstable_pagination")
                    check_seen.add(message_id)
                    check.update((message_id + "\n").encode())
                start += len(batch)
            if inventory.digest() != check.digest():
                raise APIError("unstable_pagination")
            self.pending()
            unfinished = self.writer.db.execute("SELECT COUNT(*) FROM work WHERE state IN ('pending','retry')").fetchone()[0]
            with self.writer.db:
                self.writer.db.execute("UPDATE collection_runs SET finished=?,state=?,reason=? WHERE id=?", (int(time.time()), "incomplete" if unfinished else "complete", "attachment_retries_pending" if unfinished else None, run))
            self.writer.checkpoint("last_metadata_scan", {"at": int(time.time()), "kind": kind, "pages": pages, "messages": messages})
            if kind in {"backfill", "reconcile"}:
                self.writer.checkpoint("last_full_scan", int(time.time()))
        except APIError as exc:
            with self.writer.db:
                self.writer.db.execute("UPDATE collection_runs SET finished=?,reason=? WHERE id=?", (int(time.time()), str(exc), run))
        finally:
            self.api.deadline = float("inf")


def run():
    config = Config.load()
    writer = Writer(database())
    collector = Collector(config, writer, Zoho(config))
    stopping = False

    def stop(*args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    heartbeat = Path(database()).parent / "collector-heartbeat"
    try:
        while not stopping:
            checkpoint = writer.db.execute("SELECT value FROM checkpoints WHERE key='last_full_scan'").fetchone()
            full_at = json.loads(checkpoint[0]) if checkpoint else None
            kind = "backfill" if full_at is None else "reconcile" if time.time()-full_at >= 7*86400 else "poll"
            heartbeat.write_text(str(int(time.time())))
            collector.scan(kind)
            backup = writer.db.execute("SELECT value FROM checkpoints WHERE key='last_backup'").fetchone()
            if backup is None or time.time()-json.loads(backup[0])["at"] >= 86400:
                writer.backup(Path(database()).parent / "backups")
            for _ in range(3600):
                if stopping:
                    break
                heartbeat.write_text(str(int(time.time())))
                time.sleep(1)
    finally:
        writer.close()
