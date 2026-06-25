import json
import threading
import urllib.request
from hermescloak.service.ner_service import serve
from hermescloak.span import Span


class FakeRec:
    def recognize(self, text):
        return [Span(0, 3, "לקוח", text[:3])] if text else []


def _start():
    httpd, holder = serve(host="127.0.0.1", port=0, factory=lambda: FakeRec())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, holder, httpd.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def _post(port, path, obj=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(obj or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_health_recognize_and_unload_frees_model():
    httpd, holder, port = _start()
    try:
        assert _get(port, "/health")["status"] == "ok"
        assert _get(port, "/health")["loaded"] is False        # not loaded until used
        out = _post(port, "/recognize", {"text": "שירה לוי"})
        assert out["spans"][0]["entity_type"] == "לקוח"
        assert _get(port, "/health")["loaded"] is True          # lazy-loaded on first recognize
        assert _post(port, "/unload")["loaded"] is False        # freed from RAM (easy off)
    finally:
        httpd.shutdown()


def test_one_holder_serves_repeated_calls():
    httpd, holder, port = _start()
    try:
        _post(port, "/load")
        assert holder.loaded
        a = _post(port, "/recognize", {"text": "דנה לוי"})
        b = _post(port, "/recognize", {"text": "דני כהן"})
        assert a["spans"] and b["spans"]                        # same warm model, many calls
    finally:
        httpd.shutdown()
