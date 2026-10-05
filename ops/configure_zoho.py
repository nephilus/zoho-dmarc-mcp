"""One-time, loopback-only Zoho OAuth code exchange into a Kubernetes Secret."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import subprocess
import threading
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

ACCOUNTS = {"com": "https://accounts.zoho.com", "eu": "https://accounts.zoho.eu", "in": "https://accounts.zoho.in", "com.au": "https://accounts.zoho.com.au", "jp": "https://accounts.zoho.jp", "ca": "https://accounts.zohocloud.ca"}


def exchange_and_store(context, namespace, region, client_id, client_secret, code):
    body = urlencode({"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret, "code": code}).encode()
    request = Request(ACCOUNTS[region] + "/oauth/v2/token", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read(65537))
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not 16 <= len(refresh) <= 512:
        return False
    document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": "zoho-oauth", "namespace": namespace},
                "stringData": {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh}}
    result = subprocess.run(["kubectl", "--context", context, "create", "-f", "-"], input=json.dumps(document).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="zoho-dmarc")
    parser.add_argument("--namespace", default="zoho-dmarc")
    parser.add_argument("--region", choices=ACCOUNTS, default="com")
    args = parser.parse_args()
    path = "/setup/" + secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def reply(self, status, body):
            content = body.encode()
            self.send_response(status)
            for name, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(content)), "Cache-Control": "no-store", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(content)

        def allowed(self):
            return self.path == path and self.headers.get("Host") == authority

        def do_GET(self):
            if not self.allowed():
                return self.reply(404, "Not found")
            self.reply(200, f'''<!doctype html><html><head><meta charset="utf-8"><title>Zoho OAuth setup</title><style>body{{font:16px system-ui;max-width:650px;margin:50px auto;padding:24px}}input{{display:block;width:95%;padding:12px;margin:8px 0 20px}}button{{padding:12px}}</style></head><body><h1>Zoho read-only collector setup</h1><p>Use a Self Client for your mailbox owner, with only ZohoMail.accounts.READ and ZohoMail.messages.READ. Generate a fresh authorization code immediately before submitting.</p><form method="post" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label>Client ID<input name="client_id" required maxlength="512"></label><label>Client secret<input type="password" name="client_secret" required maxlength="512" autocomplete="new-password"></label><label>Authorization code<input type="password" name="code" required maxlength="512" autocomplete="new-password"></label><button>Create collector Secret</button></form><p>The code is exchanged in memory. Only the client credentials and refresh token are stored in Kubernetes. No token is displayed or saved in a file.</p></body></html>''')

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") != origin:
                return self.reply(403, "Request rejected")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                    raise ValueError()
                fields = parse_qs(self.rfile.read(length).decode(), strict_parsing=True, max_num_fields=4)
                if any(len(value) != 1 for value in fields.values()) or not hmac.compare_digest(fields["csrf"][0], csrf):
                    raise ValueError()
                values = [fields[key][0] for key in ("client_id", "client_secret", "code")]
                if any(not 16 <= len(value) <= 512 or any(ord(char)<33 or ord(char)>126 for char in value) for value in values):
                    raise ValueError()
                success = exchange_and_store(args.context, args.namespace, args.region, *values)
                del values, fields
            except Exception:
                return self.reply(400, "Setup failed. Check client, region and code expiry; generate a fresh code before retrying. Credentials were not logged.")
            if not success:
                return self.reply(409, "Setup failed. Check code expiry or whether zoho-oauth already exists. No existing Secret was changed.")
            self.reply(200, "<h1>Zoho collector Secret created</h1><p>Close this page and return to Codex.</p>")
            print("Zoho collector Secret created; credential values were not logged.", flush=True)
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
