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
from urllib.parse import parse_qs, urlsplit

from hermescloak.service import auth

MAX_BODY = 2_000_000
_LOCAL_HOSTS = ("127.0.0.1", "localhost")


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


def make_handler(holder: NerEngineHolder, token: str):
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

        def _local_host(self) -> bool:
            # a DNS-rebinding page reaches us with ITS hostname in Host
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in _LOCAL_HOSTS

        def do_GET(self):
            if not self._local_host():
                return self._send(403, {"error": "forbidden"})
            url = urlsplit(self.path)
            if url.path == "/health":
                self._send(200, {"status": "ok", "loaded": holder.loaded})
            elif url.path == "/hello":       # prove we know the token before a client sends text
                nonce = (parse_qs(url.query).get("nonce") or [""])[0][:128]
                self._send(200, {"proof": auth.proof(token, nonce)} if nonce else {"error": "nonce"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if (not self._local_host()
                    or not (self.headers.get("Content-Type") or "").startswith("application/json")
                    or not auth.same(self.headers.get(auth.HEADER, ""), token)):
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "bad length"})
            try:
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            if self.path == "/recognize":
                text = data.get("text", "")
                self._send(200, {"spans": holder.recognize(text if isinstance(text, str) else "")})
            elif self.path == "/load":
                holder.load()
                self._send(200, {"loaded": holder.loaded})
            elif self.path == "/unload":
                holder.unload()
                self._send(200, {"loaded": holder.loaded})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8011, factory=default_factory, token: str | None = None):
    """Build (httpd, holder). Caller runs httpd.serve_forever(). ``token``: the shared secret
    (default: read or create the token file, see service/auth.py)."""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the NER service receives unmasked text: bind it to 127.0.0.1 only")
    holder = NerEngineHolder(factory)
    httpd = ThreadingHTTPServer((host, port), make_handler(holder, token or auth.ensure_token()))
    return httpd, holder


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="HermesCloak shared NER service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    args = ap.parse_args()
    httpd, _ = serve(args.host, args.port)
    print(f"cloak-ner serving on {args.host}:{args.port} (POST /recognize, /load, /unload; GET /health)"
          f" — clients authenticate with the token in {auth.token_path()}")
    httpd.serve_forever()
