import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ops.configure_tunnel import apply_secret


def test_secret_is_only_sent_on_stdin_and_does_not_use_apply_annotations():
    key = "synthetic-not-a-real-key-1234567890"
    with patch("ops.configure_tunnel.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0)
        assert apply_secret("test-context", "test-namespace", "tunnel_" + "0" * 32, key)
    call = run.call_args
    assert key not in " ".join(call.args[0])
    assert call.args[0] == ["kubectl", "--context", "test-context", "create", "-f", "-"]
    doc = json.loads(call.kwargs["input"])
    assert doc["metadata"] == {"name": "openai-tunnel", "namespace": "test-namespace"}
    assert doc["stringData"]["api_key"] == key
    assert call.kwargs["stdout"] == subprocess.PIPE
    assert call.kwargs["stderr"] == subprocess.PIPE


def test_secret_creation_failure_is_reported_without_error_output():
    with patch("ops.configure_tunnel.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, stderr=b"synthetic secret")
        assert not apply_secret("test", "test", "tunnel_" + "0" * 32, "synthetic-key")


def test_loopback_form_rejects_cross_origin_and_creates_secret_without_echo(tmp_path):
    fake = tmp_path / "kubectl"
    fake.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
    fake.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "ops/configure_tunnel.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:")
        with urlopen(url, timeout=5) as response:
            form = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Frame-Options"] == "DENY"
            # no-referrer makes browsers send Origin: null on form POSTs,
            # which correctly fails this server's strict origin validation.
            assert response.headers["Referrer-Policy"] == "same-origin"
        csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
        key = "synthetic-not-a-real-key-1234567890"
        body = urlencode({"csrf": csrf, "tunnel_id": "tunnel_" + "0" * 32, "api_key": key}).encode()
        request = Request(url, data=body, headers={"Origin": "https://untrusted.example"})
        try:
            urlopen(request, timeout=5)
            raise AssertionError("Cross-origin submission accepted")
        except HTTPError as error:
            assert error.code == 403
            assert key not in error.read().decode()
        origin = url.split("/setup/")[0]
        request = Request(url, data=body, headers={"Origin": "null"})
        try:
            urlopen(request, timeout=5)
            raise AssertionError("Opaque-origin submission accepted")
        except HTTPError as error:
            assert error.code == 403
        request = Request(url, data=body, headers={"Origin": origin})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert key not in response.read().decode()
        stdout, stderr = process.communicate(timeout=5)
        assert key not in stdout + stderr
        assert "Tunnel Secret created" in stdout
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
