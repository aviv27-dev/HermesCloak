import threading
from hermescloak.service.ner_service import serve
from hermescloak.adapter.ner_client import NerServiceRecognizer
from hermescloak.span import Span


class FakeRec:
    def recognize(self, text):
        return [Span(0, 3, "לקוח", text[:3])] if text else []


TOKEN = "test-token"


def _start():
    httpd, _ = serve(host="127.0.0.1", port=0, factory=lambda: FakeRec(), token=TOKEN)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_client_roundtrip_against_service():
    httpd, port = _start()
    try:
        c = NerServiceRecognizer(f"http://127.0.0.1:{port}", token=TOKEN)
        spans = c.recognize("שירה לוי")
        assert spans and spans[0].entity_type == "לקוח"
    finally:
        httpd.shutdown()


def test_client_failsoft_when_service_down():
    c = NerServiceRecognizer("http://127.0.0.1:9", timeout=2.0)  # nothing listening
    assert c.recognize("שירה לוי") == []                         # NER off -> no spans, no error


def test_client_off_via_control_file(tmp_path):
    httpd, port = _start()
    ctrl = tmp_path / "ner.ctl"
    ctrl.write_text("off", encoding="utf-8")
    try:
        c = NerServiceRecognizer(f"http://127.0.0.1:{port}", control_file=str(ctrl), token=TOKEN)
        assert c.recognize("שירה לוי") == []     # disabled live, no restart
        ctrl.write_text("on", encoding="utf-8")
        assert c.recognize("שירה לוי")            # re-enabled live
    finally:
        httpd.shutdown()


def _post(port, path, body=b"{}", headers=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_service_rejects_requests_without_the_token_or_from_a_foreign_host():
    httpd, port = _start()
    try:
        js = {"Content-Type": "application/json"}
        assert _post(port, "/recognize", b'{"text":"x"}', js) == 403                      # no token
        assert _post(port, "/recognize", b'{"text":"x"}', {**js, "X-Cloak-Token": "nope"}) == 403
        assert _post(port, "/unload", b"{}", {"Content-Type": "text/plain", "X-Cloak-Token": TOKEN}) == 403
        assert _post(port, "/recognize", b'{"text":"x"}',
                     {**js, "X-Cloak-Token": TOKEN, "Host": "attacker.example:8011"}) == 403  # rebinding
        assert _post(port, "/recognize", b'{"text":"x"}', {**js, "X-Cloak-Token": TOKEN}) == 200
    finally:
        httpd.shutdown()


def test_client_never_sends_text_to_a_server_that_cannot_prove_the_token():
    httpd, port = _start()             # a squatter: knows a different token
    try:
        c = NerServiceRecognizer(f"http://127.0.0.1:{port}", token="the-real-token")
        assert c.recognize("שירה לוי") == []
    finally:
        httpd.shutdown()


def test_service_refuses_to_bind_beyond_localhost():
    import pytest
    with pytest.raises(ValueError):
        serve(host="0.0.0.0", port=0, factory=lambda: FakeRec(), token=TOKEN)
