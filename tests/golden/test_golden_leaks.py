import pytest
from hermescloak.engine import Engine
from hermescloak.entities import CallableSource
from hermescloak.profile import Profile
from tests.golden.corpus import CASES, GAZETTEER

def _engine():
    return Engine(profile=Profile(name="golden"),
                  entity_source=CallableSource(lambda: GAZETTEER))

@pytest.mark.parametrize("messages,real_values", CASES)
def test_no_real_value_leaks_outbound(messages, real_values):
    out = _engine().sanitize_outbound(messages)
    blob = " ".join(m.get("content", "") for m in out)
    for val in real_values:
        assert val not in blob, f"LEAK: {val!r} reached the outbound (cloud) payload"

@pytest.mark.parametrize("messages,real_values", CASES)
def test_roundtrip_restore_text(messages, real_values):
    eng = _engine()
    out = eng.sanitize_outbound(messages)
    echoed = {"content": " ".join(m.get("content", "") for m in out), "tool_calls": []}
    restored, report = eng.restore_inbound(echoed)
    assert report.leftover == []
    for val in real_values:
        assert val in restored["content"], f"restore lost {val!r}"

def test_roundtrip_restore_tool_args():
    eng = _engine()
    eng.sanitize_outbound([{"role": "user", "content": "שלח ל-John Doe מייל a@b.co.il"}])
    resp = {"content": "", "tool_calls": [
        {"function": {"name": "send_email",
                      "arguments": {"to": "a@b.co.il", "body": "עבור ⟦לקוח_1⟧"}}}]}
    restored, report = eng.restore_inbound(resp)
    assert report.leftover == []
    assert "John Doe" in restored["tool_calls"][0]["function"]["arguments"]["body"]
