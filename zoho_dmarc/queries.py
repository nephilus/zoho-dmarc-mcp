"""Bounded, parameterized read-only answers with collection context."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from .storage import reader


def utc(value):
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        raise ValueError("explicit_UTC_timestamp_required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo != timezone.utc:
        raise ValueError("explicit_UTC_timestamp_required")
    return int(result.timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def bounds(start, end, page_size, cursor):
    begin, finish = utc(start), utc(end)
    if finish <= begin or finish - begin > 365 * 86400:
        raise ValueError("window_must_be_positive_and_at_most_365_days")
    if not 1 <= page_size <= 100 or not 0 <= cursor <= 2**63-1:
        raise ValueError("invalid_pagination")
    return begin, finish


def bounded(value):
    # MCP SDK emits both structuredContent and a textual JSON representation.
    # Include conservative formatting/escaping plus JSON-RPC envelope headroom.
    wire = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True, indent=4)}], "structuredContent": value, "isError": False}
    if len(json.dumps(wire, ensure_ascii=True, indent=4).encode()) > 255 * 1024:
        raise ValueError("response_limit_use_smaller_page_or_window")
    return value


class Queries:
    def __init__(self, path, remediation=None):
        self.path = path
        self.remediation = utc(remediation) if remediation else None

    def _health(self, db):
        checkpoints = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM checkpoints")}
        latest = db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        completed = db.execute("SELECT MAX(finished) FROM collection_runs WHERE state='complete'").fetchone()[0]
        conflicts = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM reports WHERE conflict=1 GROUP BY reporter,report_id,domain,begin,end)").fetchone()[0]
        states = dict(db.execute("SELECT state,COUNT(*) FROM work GROUP BY state").fetchall())
        status = "never_initialized" if completed is None else "stale" if time.time() - completed > 7200 else "healthy"
        if latest and latest["state"] != "complete":
            status = "incomplete"
        if conflicts:
            status = "conflict"
        export = {"state": "unknown", "reason": "host_export_not_observed"}
        try:
            raw = json.loads((Path(self.path).parent / "backup-export.json").read_text())
            export = {key: raw[key] for key in ("state", "at", "file", "reason") if key in raw}
        except (OSError, ValueError):
            pass
        return {"status": status, "last_complete_collection": iso(completed), "collection_age_seconds": int(time.time()-completed) if completed else None,
                "latest_scan": dict(latest) if latest else None, "attachment_states": states,
                "conflicting_logical_reports": conflicts, "checkpoints": checkpoints,
                "backup_export": export, "coverage_note": "Only observed reports are known. Missing periods do not prove mail delivery failures."}

    def health(self):
        try:
            with reader(self.path) as db:
                version = db.execute("SELECT version FROM schema_version").fetchone()[0]
                if version != 1:
                    return {"status": "unsupported_schema"}
                return bounded(self._health(db))
        except sqlite3.Error:
            return {"status": "never_initialized", "reason": "database_unavailable"}

    def _coverage(self, db, begin, end):
        # Read a bounded number of periods. Explicit truncation prevents partial
        # coverage from being mistaken for completeness.
        periods = db.execute("SELECT reporter,begin,end,conflict FROM reports WHERE begin<? AND end>=? ORDER BY reporter,begin,id LIMIT 10001", (end, begin)).fetchall()
        observed = {}
        truncated = len(periods) > 10000
        for row in periods[:10000]:
            item = observed.setdefault(row["reporter"], {"periods": [], "conflict": False})
            item["conflict"] |= bool(row["conflict"])
            if not row["conflict"]:
                item["periods"].append((max(begin, row["begin"]), min(end, row["end"])))
        coverage = []
        for reporter, item in observed.items():
            position = begin
            gaps = []
            for start, finish in item["periods"]:
                if start > position:
                    gaps.append({"start": iso(position), "end": iso(start)})
                position = max(position, finish)
            if position < end:
                gaps.append({"start": iso(position), "end": iso(end)})
            coverage.append({"reporter": reporter, "uncovered_periods": gaps[:100], "gaps_truncated": len(gaps)>100, "has_conflicts": item["conflict"]})
        return {"observed_reporters": coverage, "coverage_truncated": truncated}

    def _context(self, db, begin, end):
        return {"health": self._health(db), "coverage": self._coverage(db, begin, end),
                "counting": "Whole reports overlapping the requested UTC window; counts are never prorated. Conflicting reports are excluded."}

    def _period(self, row):
        value = dict(row)
        value["begin"], value["end"] = iso(row["begin"]), iso(row["end"])
        if self.remediation is not None:
            value["remediation_phase"] = "pre_change" if row["end"] < self.remediation else "post_change" if row["begin"] >= self.remediation else "spanning"
        return value

    def summary(self, start, end, page_size=100, cursor=0):
        begin, finish = bounds(start, end, page_size, cursor)
        with reader(self.path) as db:
            totals = dict(db.execute("SELECT COALESCE(SUM(a.count),0) AS messages,COALESCE(SUM(CASE WHEN a.dkim='fail' AND a.spf='fail' THEN a.count ELSE 0 END),0) AS dmarc_failures,COUNT(DISTINCT r.id) AS reports FROM reports r JOIN aggregate_rows a ON a.report=r.id WHERE r.conflict=0 AND r.begin<? AND r.end>=?", (finish, begin)).fetchone())
            rows = db.execute("SELECT id,reporter,report_id,domain,begin,end,conflict FROM reports WHERE begin<? AND end>=? AND id>? ORDER BY id LIMIT ?", (finish, begin, cursor, page_size+1)).fetchall()
            return bounded({"totals": totals, "reports": [self._period(row) for row in rows[:page_size]], "next_cursor": rows[page_size-1]["id"] if len(rows)>page_size else None, **self._context(db, begin, finish)})

    def failures(self, start, end, page_size=100, cursor=0):
        begin, finish = bounds(start, end, page_size, cursor)
        with reader(self.path) as db:
            rows = db.execute("SELECT a.id AS row_id,r.id AS report_id,r.reporter,r.domain,r.begin,r.end,a.source_ip,a.count,a.disposition,a.dkim,a.spf FROM aggregate_rows a JOIN reports r ON a.report=r.id WHERE r.conflict=0 AND r.begin<? AND r.end>=? AND a.dkim='fail' AND a.spf='fail' AND a.id>? ORDER BY a.id LIMIT ?", (finish, begin, cursor, page_size+1)).fetchall()
            return bounded({"failures": [self._period(row) for row in rows[:page_size]], "next_cursor": rows[page_size-1]["row_id"] if len(rows)>page_size else None, **self._context(db, begin, finish)})

    def details(self, report_id, page_size=100, cursor=0):
        if not 1 <= page_size <= 100 or not 0 <= cursor <= 2**63-1 or not 1 <= report_id <= 2**63-1:
            raise ValueError("invalid_pagination_or_report")
        with reader(self.path) as db:
            report = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                return {"error": "report_not_found", "health": self._health(db)}
            rows = db.execute("SELECT * FROM aggregate_rows WHERE report=? AND id>? ORDER BY id LIMIT ?", (report_id, cursor, page_size+1)).fetchall()
            provenance = db.execute("SELECT w.id,w.account,w.folder,w.message,w.attachment,w.received FROM work w JOIN provenance p ON p.work=w.id WHERE p.report=? ORDER BY w.id LIMIT 101", (report_id,)).fetchall()
            return bounded({"report": self._period(report), "rows": [dict(row) for row in rows[:page_size]], "next_cursor": rows[page_size-1]["id"] if len(rows)>page_size else None,
                            "provenance": [dict(row) for row in provenance[:100]], "provenance_truncated": len(provenance)>100, **self._context(db, report["begin"], report["end"])})
