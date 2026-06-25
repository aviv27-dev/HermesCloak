import threading
from hermescloak.service.ner_service import serve
from hermescloak.adapter.ner_client import NerServiceRecognizer
from hermescloak.span import Span


class FakeRec:
    def recognize(self, text):
        return [Span(0, 3, "לקוח", text[:3])] if text else []


def _start():
    httpd, _ = serve(host="127.0.0.1", port=0, factory=lambda: FakeRec())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_client_roundtrip_against_service():
    httpd, port = _start()
    try:
        c = NerServiceRecognizer(f"http://127.0.0.1:{port}")
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
        c = NerServiceRecognizer(f"http://127.0.0.1:{port}", control_file=str(ctrl))
        assert c.recognize("שירה לוי") == []     # disabled live, no restart
        ctrl.write_text("on", encoding="utf-8")
        assert c.recognize("שירה לוי")            # re-enabled live
    finally:
        httpd.shutdown()
