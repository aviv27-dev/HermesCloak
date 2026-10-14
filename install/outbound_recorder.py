#!/usr/bin/env python3
"""Transparent recording proxy for an OpenAI-compatible model endpoint.

Point hermes' provider base URL at it (e.g. OLLAMA_BASE_URL=http://127.0.0.1:8790/v1); it
forwards every request unchanged (including the caller's Authorization header) to --upstream and
appends each request BODY to --log as one JSON line — exactly what left the machine for the
model. Streaming responses are passed through chunk by chunk.

  python install/outbound_recorder.py --upstream https://ollama.com/v1 --port 8790 --log out.jsonl
"""
import argparse
import json
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()


def make_handler(upstream: str, log_path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass

        def _forward(self, method, body=None):
            path = self.path[len("/v1"):] if self.path.startswith("/v1") else self.path
            req = urllib.request.Request(upstream.rstrip("/") + path, data=body, method=method)
            for h in ("Authorization", "Content-Type", "Accept"):
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            try:
                resp = opener.open(req, timeout=600)
                status, headers = resp.status, resp.headers
            except urllib.error.HTTPError as e:
                resp, status, headers = e, e.code, e.headers
            except Exception as e:  # noqa: BLE001
                msg = json.dumps({"error": {"message": f"recorder upstream error: {e}"}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def do_GET(self):
            self._forward("GET")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with _LOCK, open(log_path, "a", encoding="utf-8") as f:
                f.write(body.decode("utf-8", "replace").replace("\n", " ") + "\n")
            self._forward("POST", body)

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.upstream, a.log)).serve_forever()


if __name__ == "__main__":
    main()
