#!/usr/bin/env python3
"""GitHub webhook listener for continuous deployment.

Stdlib only — no pip dependencies. Survives broken venvs so it can
accept the push that fixes them.

Reads GITHUB_WEBHOOK_SECRET from the environment (set via
/etc/futarchy-webhook.env in the systemd unit).

Listens on 127.0.0.1:9000.  Caddy reverse-proxies /hooks/* here.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND = "127.0.0.1"
PORT = 9000
DEPLOY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy.sh")


def verify_signature(secret: bytes, payload: bytes, signature: str) -> bool:
    """Validate GitHub HMAC-SHA256 signature."""
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


class WebhookHandler(BaseHTTPRequestHandler):
    secret: bytes  # set on class before serving

    def do_POST(self):
        if self.path != "/hooks/github":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(content_length)

        signature = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(self.secret, payload, signature):
            self.send_error(403, "Invalid signature")
            return

        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        ref = body.get("ref", "")
        if ref != "refs/heads/main":
            self._respond(200, {"status": "ignored", "ref": ref})
            return

        # Fire deploy in background — don't block the HTTP response.
        subprocess.Popen(
            ["bash", DEPLOY_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._respond(200, {"status": "deploying"})

    def _respond(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Silence per-request log lines; deploy.sh logs to journal instead.
    def log_message(self, format, *args):
        pass


def main():
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET not set", file=sys.stderr)
        sys.exit(1)

    WebhookHandler.secret = secret.encode()

    server = HTTPServer((BIND, PORT), WebhookHandler)
    print(f"Listening on {BIND}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
