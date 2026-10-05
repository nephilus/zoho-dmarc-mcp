"""Synthetic CI fixture only; mounted as a ConfigMap, never in the runtime image."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from pathlib import Path
import sys

if __name__ == "__main__" and sys.argv[1] == "collector":
    from zoho_dmarc import config
    config.REGIONS["com"] = ("http://127.0.0.1:9000", "http://127.0.0.1:9000")
    from zoho_dmarc.collector import run
    run()
elif __name__ == "__main__":
    XML = Path("/fixtures/report.xml").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, data):
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def do_POST(self):
            if self.path == "/oauth/v2/token":
                self.reply({"access_token": "synthetic", "expires_in": 3600})
            else:
                self.send_error(405)
        def do_GET(self):
            from urllib.parse import urlsplit, parse_qs
            url = urlsplit(self.path)
            if url.path in {"/healthz", "/readyz"}:
                self.reply({"status": "fixture"})
                return
            if url.path == "/api/accounts":
                data = [{"accountId": "1", "primaryEmailAddress": "owner@example.test"}]
            elif url.path.endswith("messages/view"):
                data = [{"messageId": "3", "receivedTime": str(int(time.time()*1000))}] if parse_qs(url.query)["start"] == ["1"] else []
            elif url.path.endswith("attachmentinfo"):
                data = {"attachments": [{"attachmentId": "4"}]}
            elif url.path.endswith("attachments/4"):
                self.send_response(200)
                self.send_header("Content-Length", str(len(XML)))
                self.end_headers()
                self.wfile.write(XML)
                return
            else:
                self.send_error(404)
                return
            self.reply({"status": {"code": 200}, "data": data})
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 9000), Handler).serve_forever()
