import json
import time
import httpx
import pytest
from zoho_dmarc.collector import Collector
from zoho_dmarc.config import Config
from zoho_dmarc.storage import Writer
from zoho_dmarc.queries import Queries
from zoho_dmarc.zoho import Zoho, APIError
from test_reports import XML

CONFIG = Config("owner@example.test", "1", "2", ("example.test",))


def response(data):
    return httpx.Response(200, json={"status": {"code": 200}, "data": data})


def test_get_only_refresh_retry_delayed_mail_and_replay(tmp_path, monkeypatch):
    for key in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "synthetic")
    calls = []
    downloads = 0
    refreshes = 0
    throttle = True

    def handle(request):
        nonlocal downloads, refreshes, throttle
        calls.append((request.method, request.url.path))
        if request.url.path == "/oauth/v2/token":
            refreshes += 1
            return httpx.Response(200, json={"access_token": "synthetic-access", "expires_in": 3600})
        assert request.method == "GET"
        if request.url.path == "/api/accounts":
            return response([{"accountId": "1", "primaryEmailAddress": CONFIG.owner}])
        if request.url.path.endswith("messages/view"):
            start = request.url.params["start"]
            if start == "1":
                return response([{"messageId": "3", "receivedTime": str(int(time.time()*1000))}])
            return response([])
        if request.url.path.endswith("attachmentinfo"):
            if throttle:
                throttle = False
                return httpx.Response(429, headers={"Retry-After": "0"})
            return response({"attachments": [{"attachmentId": "4"}]})
        if request.url.path.endswith("attachments/4"):
            downloads += 1
            if downloads == 1:
                return httpx.Response(401)
            return httpx.Response(200, content=XML)
        raise AssertionError(request.url.path)

    writer = Writer(tmp_path/"history.sqlite")
    try:
        api = Zoho(CONFIG, httpx.Client(transport=httpx.MockTransport(handle)))
        collector = Collector(CONFIG, writer, api)
        collector.scan("backfill")
        assert Queries(writer.path).health()["status"] == "healthy"
        assert refreshes == 2
        collector.scan("poll")
        assert downloads == 2  # completed work is replay-safe
        assert writer.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert not any(method != "GET" and path != "/oauth/v2/token" for method, path in calls)
    finally:
        writer.close()


def test_interrupted_scan_keeps_work_and_retry(tmp_path):
    class Interrupted:
        deadline = float("inf")
        _check_deadline = lambda self: None
        validate_owner = lambda self: None
        attachments = lambda self, message: [{"attachmentId": "4"}]
        def messages(self, start):
            if start == 1:
                return [{"messageId": "3", "receivedTime": str(int(time.time()*1000))}]
            raise APIError("network_failure")
    writer = Writer(tmp_path/"history.sqlite")
    try:
        Collector(CONFIG, writer, Interrupted()).scan("backfill")
        assert writer.db.execute("SELECT state FROM work").fetchone()[0] == "pending"
        assert Queries(writer.path).health()["status"] == "incomplete"
        assert writer.db.execute("SELECT COUNT(*) FROM checkpoints WHERE key='last_full_scan'").fetchone()[0] == 0
    finally:
        writer.close()


def test_never_initialized_stale_empty_and_owner_rejection(tmp_path):
    writer = Writer(tmp_path/"history.sqlite")
    try:
        query = Queries(writer.path)
        assert query.health()["status"] == "never_initialized"
        with writer.db:
            writer.db.execute("INSERT INTO collection_runs(started,finished,kind,state) VALUES(0,1,'backfill','complete')")
        assert query.health()["status"] == "stale"
        assert query.summary("2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z")["totals"]["messages"] == 0
        api = Zoho(CONFIG, httpx.Client(transport=httpx.MockTransport(lambda req: response([{"accountId": "1", "primaryEmailAddress": "other@example.test"}]))))
        api.token, api.expires = "synthetic", time.monotonic()+100
        with pytest.raises(APIError, match="account_owner_mismatch"):
            api.validate_owner()
    finally:
        writer.close()
