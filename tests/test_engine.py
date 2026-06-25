import copy
from hermescloak.entities import CallableSource
from hermescloak.engine import Engine
from hermescloak.profile import Profile

def make_engine():
    return Engine(
        profile=Profile(name="t", never_mask=["בית המשפט"]),
        entity_source=CallableSource(lambda: [("שירה לוי", "לקוח")]),
    )

def test_sanitize_outbound_tokenizes_copy_and_keeps_original():
    eng = make_engine()
    messages = [{"role": "user", "content": "שירה לוי בטלפון 050-1234567"}]
    original = copy.deepcopy(messages)
    out = eng.sanitize_outbound(messages)
    assert messages == original                      # canonical untouched
    body = out[-1]["content"]
    assert "שירה לוי" not in body and "050-1234567" not in body
    assert "⟦לקוח_1⟧" in body

def test_sanitize_injects_instruction_as_system():
    eng = make_engine()
    out = eng.sanitize_outbound([{"role": "user", "content": "שירה לוי"}])
    assert out[0]["role"] == "system" and "⟦" in out[0]["content"]

def test_no_instruction_when_nothing_tokenized():
    eng = make_engine()
    out = eng.sanitize_outbound([{"role": "user", "content": "שלום עולם"}])
    assert all(m["role"] != "system" for m in out)

def test_instruction_merges_into_existing_system_message():
    eng = make_engine()
    out = eng.sanitize_outbound([
        {"role": "system", "content": "אתה עוזר משפטי."},
        {"role": "user", "content": "שירה לוי"},
    ])
    systems = [m for m in out if m["role"] == "system"]
    assert len(systems) == 1                                  # merged, not duplicated
    assert "⟦" in systems[0]["content"] and "אתה עוזר משפטי." in systems[0]["content"]

def test_restore_inbound_text_and_tool_args():
    eng = make_engine()
    eng.sanitize_outbound([{"role": "user", "content": "מייל לשירה לוי a@b.co.il"}])
    response = {
        "content": "אשלח ל⟦לקוח_1⟧",
        "tool_calls": [{"function": {"name": "send_email",
                                     "arguments": {"to": "⟦מייל_1⟧", "body": "⟦לקוח_1⟧"}}}],
    }
    restored, report = eng.restore_inbound(response)
    assert restored["content"] == "אשלח לשירה לוי"
    assert restored["tool_calls"][0]["function"]["arguments"] == {"to": "a@b.co.il", "body": "שירה לוי"}
    assert report.leftover == []

def test_content_cache_runs_detection_once_per_unique_content():
    calls = {"n": 0}
    class Counting:
        def recognize(self, text):
            calls["n"] += 1
            return []
    eng = Engine(profile=Profile(name="t"), extra_recognizers=[Counting()])
    eng.sanitize_outbound([{"role": "user", "content": "אותו טקסט"}])
    eng.sanitize_outbound([{"role": "user", "content": "אותו טקסט"}])  # re-tokenized next call
    assert calls["n"] == 1   # detection (incl. would-be NER) ran ONCE, not twice

def test_restore_inbound_flags_leftover():
    eng = make_engine()
    eng.sanitize_outbound([{"role": "user", "content": "שירה לוי"}])
    restored, report = eng.restore_inbound({"content": "⟦לקוח_1⟧ ו⟦טלפון_9⟧", "tool_calls": []})
    assert "⟦טלפון_9⟧" in report.leftover
