"""Untrusted attachment parsing. This module never performs network or file I/O."""
from dataclasses import dataclass
import gzip
import hashlib
import io
import ipaddress
import json
import multiprocessing
import re
import stat
import zipfile
from pathlib import PurePosixPath
from defusedxml import ElementTree as ET


class Rejected(ValueError):
    """Stable rejection code; never includes attachment contents."""


@dataclass(frozen=True)
class Limits:
    compressed: int = 10 * 1024 * 1024
    expanded: int = 50 * 1024 * 1024
    entries: int = 16
    ratio: int = 100
    depth: int = 32
    records: int = 100_000
    seconds: float = 30


def domain(value):
    value = value.strip().lower().rstrip(".")
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise Rejected("invalid_domain")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
        raise Rejected("invalid_domain")
    return value


def _text(node, path, maximum=1024):
    items = node.findall(path)
    if len(items) != 1 or items[0].text is None or not items[0].text.strip():
        raise Rejected("missing_or_duplicate_field")
    result = items[0].text.strip()
    if len(result) > maximum:
        raise Rejected("field_too_long")
    return result


def _number(node, path, minimum=0, maximum=2**63-1):
    value = _text(node, path, 20)
    if not value.isascii() or not value.isdigit():
        raise Rejected("invalid_number")
    result = int(value)
    if not minimum <= result <= maximum:
        raise Rejected("invalid_number")
    return result


def _choice(node, path, choices):
    value = _text(node, path)
    if value not in choices:
        raise Rejected("invalid_policy_or_result")
    return value


def _xml(payload, allowlist, limits):
    depth = 0
    count = 0
    root = None
    namespace = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(payload), events=("start", "end"), forbid_dtd=True, forbid_entities=True, forbid_external=True):
            if event == "start":
                depth += 1
                if root is None:
                    root = elem
                    namespace = elem.tag.split("}", 1)[0][1:] if elem.tag.startswith("{") else ""
                    if namespace not in {"", "http://dmarc.org/dmarc-xml/0.1", "urn:ietf:params:xml:ns:dmarc-2.0"}:
                        raise Rejected("unsupported_xml_namespace")
                # Normalize only the report namespace. Foreign extensions cannot
                # impersonate core fields, and namespaced records retain limits.
                prefix = "{" + namespace + "}" if namespace else ""
                if prefix and elem.tag.startswith(prefix):
                    elem.tag = elem.tag[len(prefix):]
                if depth > limits.depth:
                    raise Rejected("xml_depth")
                if elem.tag == "record":
                    count += 1
                    if count > limits.records:
                        raise Rejected("record_limit")
            else:
                depth -= 1
    except Rejected:
        raise
    except Exception:
        raise Rejected("unsafe_or_malformed_xml") from None
    if root is None or root.tag != "feedback" or not root.findall("record"):
        raise Rejected("invalid_aggregate")
    report_domain = domain(_text(root, "policy_published/domain"))
    if report_domain not in allowlist:
        raise Rejected("domain_not_allowed")
    begin = _number(root, "report_metadata/date_range/begin", maximum=253402300799)
    end = _number(root, "report_metadata/date_range/end", maximum=253402300799)
    if end <= begin:
        raise Rejected("invalid_period")
    policy = {"p": _choice(root, "policy_published/p", {"none", "quarantine", "reject"})}
    for key, choices in (("sp", {"none", "quarantine", "reject"}), ("adkim", {"r", "s"}), ("aspf", {"r", "s"})):
        if root.find("policy_published/" + key) is not None:
            policy[key] = _choice(root, "policy_published/" + key, choices)
    if root.find("policy_published/pct") is not None:
        policy["pct"] = _number(root, "policy_published/pct", maximum=100)
    rows = []
    for record in root.findall("record"):
        try:
            ip = str(ipaddress.ip_address(_text(record, "row/source_ip", 45)))
        except ValueError:
            raise Rejected("invalid_source_ip") from None
        row = {
            "source_ip": ip, "count": _number(record, "row/count", minimum=1),
            "disposition": _choice(record, "row/policy_evaluated/disposition", {"none", "pass", "quarantine", "reject"}),
            "dkim": _choice(record, "row/policy_evaluated/dkim", {"pass", "fail"}),
            "spf": _choice(record, "row/policy_evaluated/spf", {"pass", "fail"}),
            "header_from": domain(_text(record, "identifiers/header_from")),
        }
        if row["header_from"] != report_domain and not row["header_from"].endswith("." + report_domain):
            raise Rejected("header_domain_mismatch")
        rows.append(row)
    # A canonical multiset makes record order irrelevant without dropping duplicates.
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    report = {
        "reporter": _text(root, "report_metadata/org_name", 255).casefold(),
        "report_id": _text(root, "report_metadata/report_id", 255),
        "domain": report_domain, "begin": begin, "end": end,
        "policy": policy, "rows": rows,
    }
    # Include all XML content, including authentication results we do not query.
    # Different evidence must never silently collapse to the same report.
    report["fingerprint"] = hashlib.sha256(payload).hexdigest()
    return report


def parse(data, allowlist, limits=Limits()):
    if not data or len(data) > limits.compressed:
        raise Rejected("compressed_limit")
    budget = min(limits.expanded, len(data) * limits.ratio)
    payloads = []
    try:
        if data.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > limits.entries:
                    raise Rejected("entry_limit")
                expanded = 0
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    mode = entry.external_attr >> 16
                    if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename or stat.S_ISLNK(mode):
                        raise Rejected("unsafe_archive_path")
                    if entry.is_dir() or not entry.filename.lower().endswith(".xml") or entry.flag_bits & 1:
                        raise Rejected("unsupported_archive_entry")
                    expanded += entry.file_size
                    if expanded > budget or entry.file_size > max(1, entry.compress_size) * limits.ratio:
                        raise Rejected("expansion_limit")
                    with archive.open(entry) as stream:
                        payload = stream.read(budget + 1)
                    if len(payload) != entry.file_size or len(payload) > budget:
                        raise Rejected("expansion_limit")
                    payloads.append(payload)
        elif data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                payload = stream.read(budget + 1)
            if len(payload) > budget:
                raise Rejected("expansion_limit")
            payloads.append(payload)
        else:
            if len(data) > limits.expanded:
                raise Rejected("expansion_limit")
            payloads.append(data)
    except Rejected:
        raise
    except Exception:
        raise Rejected("malformed_archive") from None
    if not payloads:
        raise Rejected("empty_archive")
    reports = []
    record_count = 0
    for payload in payloads:
        if payload.startswith((b"PK", b"\x1f\x8b")):
            raise Rejected("nested_archive")
        report = _xml(payload, allowlist, limits)
        record_count += len(report["rows"])
        if record_count > limits.records:
            raise Rejected("record_limit")
        reports.append(report)
    return reports


def _worker(pipe, data, allowlist, limits):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(limits.seconds)), max(1, int(limits.seconds)) + 1))
    try:
        pipe.send((True, parse(data, allowlist, limits)))
    except Rejected as exc:
        pipe.send((False, str(exc)))
    except Exception:
        pipe.send((False, "worker_failure"))
    finally:
        pipe.close()


def parse_bounded(data, allowlist, limits=Limits()):
    if len(data) > limits.compressed:
        raise Rejected("compressed_limit")
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(writer, data, allowlist, limits), daemon=True)
    process.start()
    writer.close()
    try:
        if not reader.poll(limits.seconds):
            raise Rejected("worker_timeout")
        try:
            ok, result = reader.recv()
        except EOFError:
            raise Rejected("worker_resource_limit") from None
        if not ok:
            raise Rejected(result)
        return result
    finally:
        reader.close()
        if process.is_alive():
            process.kill()
        process.join()
