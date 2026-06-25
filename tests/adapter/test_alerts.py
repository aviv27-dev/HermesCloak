import json
from hermescloak.adapter.alerts import AlertEvent, CollectingAlerter, FileAuditAlerter, CallbackAlerter

def test_collecting_alerter():
    a = CollectingAlerter()
    a.send(AlertEvent("leftover_token", "s1", "⟦x_1⟧"))
    assert a.events[0].kind == "leftover_token"

def test_file_audit_alerter_writes_jsonl(tmp_path):
    p = tmp_path / "audit.log"
    a = FileAuditAlerter(str(p))
    a.send(AlertEvent("unfiltered_sent", "s1", "boom"))
    a.send(AlertEvent("leftover_token", "s2", "⟦y_2⟧"))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec == {"kind": "unfiltered_sent", "session_id": "s1", "detail": "boom"}

def test_callback_alerter_formats_message():
    seen = []
    a = CallbackAlerter(seen.append)
    a.send(AlertEvent("leftover_token", "s9", "⟦z_3⟧"))
    assert "leftover_token" in seen[0] and "s9" in seen[0]
