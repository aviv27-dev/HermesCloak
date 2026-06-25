"""Tiny stdlib bridge server so an HTML page can exercise the HermesCloak core.

The core Engine is a plain Python library with no HTTP layer; this thin server (stdlib
`http.server`, same style as hermescloak/service/ner_service.py) wraps it with three JSON
endpoints and serves demo/index.html, so the sanitize -> restore lifecycle can be driven
from a browser. NOT a production surface — a demo/test harness only.

Run from the repo root:

    python -m demo.test_server

then open http://127.0.0.1:8765
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermescloak import Engine

# examples.py owns the gazetteer + NER wiring + the 100 QA cases. Import works both
# as `python -m demo.test_server` (relative) and other launchers (absolute fallback).
try:
    from .examples import EXAMPLES, build_engine, examples_meta, run_example
except ImportError:  # pragma: no cover
    from demo.examples import EXAMPLES, build_engine, examples_meta, run_example

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")
_EX_BY_ID = {e["id"]: e for e in EXAMPLES}


# Per-session Engine cache: sanitize then restore must share one Vault so tokens round-trip
# (conceptually the same role as hermescloak/adapter/session_registry.py). build_engine()
# wires the example profile + demo gazetteer + the live Hebrew NER (fail-open if down).
_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def _engine_for(session: str) -> Engine:
    with _lock:
        eng = _engines.get(session)
        if eng is None:
            eng = build_engine()
            _engines[session] = eng
        return eng


def _reset(session: str) -> None:
    with _lock:
        _engines.pop(session, None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence access logs
        pass

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json(404, {"error": "index.html not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(INDEX_PATH)
        elif self.path == "/health":
            self._send_json(200, {"status": "ok", "sessions": len(_engines)})
        elif self.path == "/api/examples":
            # browsable list of the 100 QA cases (metadata only; run live via /api/run_example)
            self._send_json(200, {"examples": examples_meta(), "count": len(EXAMPLES)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        data = self._read_json()
        session = str(data.get("session") or "default")

        if self.path == "/api/sanitize":
            messages = data.get("messages") or []
            eng = _engine_for(session)
            outbound = eng.sanitize_outbound(messages)
            self._send_json(200, {
                "messages": outbound,
                "vault": eng.vault.summary(),  # type -> count only (no PII)
            })

        elif self.path == "/api/restore":
            response = data.get("response") or {}
            eng = _engine_for(session)
            restored, report = eng.restore_inbound(response)
            self._send_json(200, {
                "response": restored,
                "leftover": report.leftover,
                "restored_any": report.restored_any,
            })

        elif self.path == "/api/reset":
            _reset(session)
            self._send_json(200, {"ok": True})

        elif self.path == "/api/run_example":
            # run one of the 100 QA cases LIVE against a fresh engine; return verdict
            ex = _EX_BY_ID.get(int(data.get("id") or 0))
            if not ex:
                self._send_json(404, {"error": "unknown example id"})
                return
            self._send_json(200, run_example(ex))

        else:
            self._send_json(404, {"error": "not found"})


def main():
    import argparse
    ap = argparse.ArgumentParser(description="HermesCloak browser demo/test server")
    ap.add_argument("--host", default="127.0.0.1")
    # 8770: the original default 8765 is taken by the Tamar agent service on the
    # AGLO host; 8770 is free here. Override with --port for other environments.
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HermesCloak demo serving on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
