"""One-time loopback form: submit credentials directly to a Kubernetes Secret.

Only the local user should open the generated URL. No keys are saved to disk,
logged, placed in argv, or echoed to the browser. The form expires after 20 min.
"""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import secrets
import subprocess
import threading
from urllib.parse import parse_qs


def apply_secret(context: str, namespace: str, tunnel_id: str, api_key: str) -> bool:
    document = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "openai-tunnel", "namespace": namespace},
        "stringData": {"tunnel_id": tunnel_id, "api_key": api_key},
    }
    # Create rather than client-side apply: do not duplicate secrets into the
    # last-applied-configuration annotation; do not overwrite existing access.
    result = subprocess.run(
        ["kubectl", "--context", context, "create", "-f", "-"],
        input=json.dumps(document).encode(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=False,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="zoho-dmarc")
    parser.add_argument("--namespace", default="zoho-dmarc")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    path = "/setup/" + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def reply(self, status: int, body: str):
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # Preserve Origin on same-origin native form POSTs. no-referrer
            # would force Origin: null and fail the strict origin check below.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            return self.path == path and self.headers.get("Host") == authority

        def do_GET(self):
            if not self.authorized():
                self.reply(404, "Not found")
                return
            self.reply(200, f'''<!doctype html><html><head><meta charset="utf-8"><title>Tunnel setup</title>
            <style>body{{font:16px system-ui;max-width:640px;margin:60px auto;padding:24px}}label,input{{display:block}}input{{width:95%;padding:12px;margin:8px 0 24px}}button{{padding:12px 20px}}</style></head>
            <body><h1>Connect your private tunnel</h1><p>Creates the openai-tunnel Secret in namespace {args.namespace} on context {args.context}. The key stays in memory until Kubernetes receives it.</p>
            <form method="post" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
            <label>Tunnel ID<input name="tunnel_id" required pattern="tunnel_[0-9a-f]{{32}}" autocomplete="off"></label>
            <label>Runtime API key<input type="password" name="api_key" required minlength="32" maxlength="512" autocomplete="new-password"></label>
            <button type="submit">Create Kubernetes Secret</button></form><p>Use a runtime key with Tunnels Read + Use. Do not use an admin key.</p></body></html>''')

        def do_POST(self):
            if not self.authorized() or self.headers.get("Origin") != origin:
                self.reply(403, "Request rejected")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                    raise ValueError()
                fields = parse_qs(self.rfile.read(length).decode(), strict_parsing=True, max_num_fields=3)
                if any(len(value) != 1 for value in fields.values()):
                    raise ValueError()
                tunnel_id = fields["tunnel_id"][0]
                api_key = fields["api_key"][0]
                if not hmac.compare_digest(fields["csrf"][0], csrf):
                    raise ValueError()
                if not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id) or not re.fullmatch(r"[!-~]{32,512}", api_key):
                    raise ValueError()
                succeeded = apply_secret(args.context, args.namespace, tunnel_id, api_key)
                del api_key, fields
            except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                self.reply(400, "Setup failed. Check local cluster connectivity and input; no credential details are displayed.")
                return
            if not succeeded:
                self.reply(409, "Secret creation failed. Check cluster access or whether openai-tunnel already exists. No existing secret was changed.")
                return
            self.reply(200, "<h1>Tunnel Secret created</h1><p>You can close this form. Return to Codex to continue the connectivity proof.</p>")
            print("Tunnel Secret created; credential values were not logged.", flush=True)
            threading.Thread(target=server.shutdown, daemon=True).start()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    authority = f"127.0.0.1:{server.server_port}"
    origin = "http://" + authority
    timer = threading.Timer(1200, server.shutdown)
    timer.daemon = True
    timer.start()
    print(origin + path, flush=True)
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
