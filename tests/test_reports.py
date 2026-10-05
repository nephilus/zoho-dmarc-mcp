import gzip
import io
import json
import sqlite3
import zipfile
from dataclasses import replace
import pytest
from zoho_dmarc.parser import Limits, Rejected, parse, parse_bounded
from zoho_dmarc.queries import Queries
from zoho_dmarc.storage import Writer, reader

XML = b'''<?xml version="1.0"?><feedback><report_metadata><org_name>Example reporter</org_name><email>reports@example.test</email><report_id>abc</report_id><date_range><begin>1728000000</begin><end>1728086400</end></date_range></report_metadata><policy_published><domain>example.test</domain><adkim>r</adkim><aspf>r</aspf><p>none</p><pct>100</pct></policy_published><record><row><source_ip>192.0.2.1</source_ip><count>12</count><policy_evaluated><disposition>none</disposition><dkim>fail</dkim><spf>fail</spf></policy_evaluated></row><identifiers><header_from>example.test</header_from></identifiers><auth_results><dkim><domain>example.test</domain><result>fail</result></dkim><spf><domain>example.test</domain><result>fail</result></spf></auth_results></record></feedback>'''


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries:
            z.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("data", [XML, gzip.compress(XML), archive([("report.xml", XML)])])
def test_supported_formats_and_worker(data):
    assert parse_bounded(data, ("example.test",))[0]["rows"][0]["count"] == 12


@pytest.mark.parametrize("data,code", [
    (b'<!DOCTYPE feedback [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + XML.split(b"?>")[1], "unsafe_or_malformed_xml"),
    (archive([("../a.xml", XML)]), "unsafe_archive_path"),
    (archive([("a.zip", XML)]), "unsupported_archive_entry"),
    (archive([("a.xml", gzip.compress(XML))]), "nested_archive"),
    (gzip.compress(b"x"*100000), "expansion_limit"),
    (XML.replace(b"<count>12", b"<count>-1"), "invalid_number"),
    (XML.replace(b"192.0.2.1", b"bad-address"), "invalid_source_ip"),
    (XML.replace(b"<p>none", b"<p>invalid"), "invalid_policy_or_result"),
    (b"not XML", "unsafe_or_malformed_xml"),
])
def test_rejections(data, code):
    with pytest.raises(Rejected, match=code):
        parse(data, ("example.test",))


@pytest.mark.parametrize("limits,code", [
    (replace(Limits(), compressed=1), "compressed_limit"),
    (replace(Limits(), expanded=1), "expansion_limit"),
    (replace(Limits(), depth=2), "xml_depth"),
    (replace(Limits(), records=0), "record_limit"),
])
def test_limits(limits, code):
    with pytest.raises(Rejected, match=code):
        parse(XML, ("example.test",), limits)


def test_entry_limit_symlink_domain_timeout():
    with pytest.raises(Rejected, match="entry_limit"):
        parse(archive([(f"{i}.xml", XML) for i in range(17)]), ("example.test",))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        info = zipfile.ZipInfo("a.xml")
        info.external_attr = 0o120777 << 16
        z.writestr(info, XML)
    with pytest.raises(Rejected, match="unsafe_archive_path"):
        parse(stream.getvalue(), ("example.test",))
    with pytest.raises(Rejected, match="domain_not_allowed"):
        parse(XML, ("different.test",))
    with pytest.raises(Rejected, match="worker_timeout"):
        parse_bounded(XML, ("example.test",), replace(Limits(), seconds=0.001))


def enqueue(writer, attachment="1"):
    writer.enqueue("1", "2", "3", attachment, 1729000000)
    return writer.db.execute("SELECT id FROM work WHERE attachment=?", (attachment,)).fetchone()[0]


def test_replay_conflict_atomicity_readonly_and_backup(tmp_path):
    path = tmp_path / "dmarc.sqlite"
    writer = Writer(path)
    try:
        with pytest.raises(RuntimeError, match="writer_already_running"):
            Writer(path)
        work = enqueue(writer)
        report = parse(XML, ("example.test",))
        writer.ingest(work, report)
        writer.ingest(work, report)
        assert writer.db.execute("SELECT COUNT(*) FROM aggregate_rows").fetchone()[0] == 1
        query = Queries(path)
        result = query.summary("2024-10-04T01:00:00Z", "2024-10-04T02:00:00Z")
        assert result["totals"]["messages"] == 12  # no proration
        with reader(path) as db, pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM reports")
        broken = [{**report[0], "report_id": "different", "rows": [{**report[0]["rows"][0], "count": None}]}]
        second = enqueue(writer, "2")
        with pytest.raises(sqlite3.IntegrityError):
            writer.ingest(second, broken)
        assert writer.db.execute("SELECT state FROM work WHERE id=?", (second,)).fetchone()[0] == "pending"
        assert writer.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        writer.ingest(second, parse(XML.replace(b"<count>12", b"<count>13"), ("example.test",)))
        result = query.summary("2024-10-04T00:00:00Z", "2024-10-05T00:00:00Z")
        assert result["totals"]["messages"] == 0
        assert result["health"]["status"] == "conflict"
        backup = writer.backup(tmp_path / "backups")
        with reader(backup) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()
    restarted = Writer(path)
    assert Queries(path).health()["conflicting_logical_reports"] == 1
    restarted.close()


@pytest.mark.parametrize("start,end,size,cursor", [("2024-01-01", "2024-02-01T00:00:00Z", 100, 0), ("2024-01-01T00:00:00Z", "2025-02-01T00:00:00Z", 100, 0), ("2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", 101, 0), ("2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", 100, -1)])
def test_query_bounds(tmp_path, start, end, size, cursor):
    with pytest.raises(ValueError):
        Queries(tmp_path/"none").summary(start, end, size, cursor)


def test_response_budget_accounts_for_mcp_envelope():
    from zoho_dmarc.queries import bounded
    with pytest.raises(ValueError, match='response_limit'):
        bounded({'value':'x'*140000})
    assert bounded({'value':'x'*100})['value']=='x'*100


def test_policy_domain_subdomain_is_valid():
    data = XML.replace(b'<header_from>example.test', b'<header_from>mail.example.test')
    assert parse(data, ('example.test',))[0]['rows'][0]['header_from']=='mail.example.test'


def test_adjacent_inclusive_report_periods_have_no_false_gap(tmp_path):
    writer=Writer(tmp_path/'history.sqlite')
    try:
        first=XML.replace(b'<end>1728086400', b'<end>1728086399')
        second=XML.replace(b'<report_id>abc', b'<report_id>second').replace(b'<begin>1728000000', b'<begin>1728086400').replace(b'<end>1728086400', b'<end>1728172799')
        writer.ingest(enqueue(writer), parse(first, ('example.test',)))
        writer.ingest(enqueue(writer,'2'), parse(second, ('example.test',)))
        answer=Queries(writer.path).summary('2024-10-04T00:00:00Z','2024-10-06T00:00:00Z')
        assert answer['coverage']['observed_reporters'][0]['uncovered_periods']==[]
    finally:
        writer.close()
