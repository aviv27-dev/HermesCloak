"""Shared local NER service — ONE model in memory, serving ALL agent profiles.

Design goals (per project decision):
  * one warm model copy (not per-agent) so a 4-core / 8GB box can host it;
  * profile-agnostic — any agent's adapter calls the same /recognize;
  * EASY OFF: `POST /unload` frees the model from RAM; stopping the process frees everything;
  * fail-soft from the client side (see adapter/ner_client.py) so turning NER off never
    breaks an agent.

Stdlib only. The model (transformers/torch via the [ner] extra) is imported lazily by the
default factory, so importing this module is cheap and dependency-free."""
import gc
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def default_factory():
    from hermescloak.recognizers.ner import HebrewNerRecognizer
    return HebrewNerRecognizer()


class NerEngineHolder:
    """Holds (at most) one recognizer. load()/unload() control the RAM footprint."""

    def __init__(self, factory=default_factory) -> None:
        self._factory = factory
        self._rec = None
        self._lock = threading.Lock()

    def load(self) -> bool:
        with self._lock:
            if self._rec is None:
                self._rec = self._factory()
        return True

    def unload(self) -> bool:
        with self._lock:
            self._rec = None
        gc.collect()  # release the model from memory
        return True

    @property
    def loaded(self) -> bool:
        return self._rec is not None

    def recognize(self, text: str) -> list[dict]:
        with self._lock:
            if self._rec is None:
                self._rec = self._factory()
            rec = self._rec
        return [{"start": s.start, "end": s.end, "entity_type": s.entity_type, "text": s.text}
                for s in rec.recognize(text)]


def make_handler(holder: NerEngineHolder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence access logs
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "loaded": holder.loaded})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                data = {}
            if self.path == "/recognize":
                self._send(200, {"spans": holder.recognize(data.get("text", ""))})
            elif self.path == "/load":
                holder.load()
                self._send(200, {"loaded": holder.loaded})
            elif self.path == "/unload":
                holder.unload()
                self._send(200, {"loaded": holder.loaded})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8011, factory=default_factory):
    """Build (httpd, holder). Caller runs httpd.serve_forever()."""
    holder = NerEngineHolder(factory)
    httpd = ThreadingHTTPServer((host, port), make_handler(holder))
    return httpd, holder


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="HermesCloak shared NER service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    args = ap.parse_args()
    httpd, _ = serve(args.host, args.port)
    print(f"cloak-ner serving on {args.host}:{args.port} (POST /recognize, /load, /unload; GET /health)")
    httpd.serve_forever()
