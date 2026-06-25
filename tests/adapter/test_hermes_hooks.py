from hermescloak.adapter.guard import CloakGuard
from hermescloak.adapter.session_registry import SessionRegistry
from hermescloak.adapter.alerts import CollectingAlerter
from hermescloak.adapter.hermes_hooks import make_outbound_hook, make_inbound_hook
from hermescloak.entities import CallableSource
from hermescloak.profile import Profile


def _guard():
    reg = SessionRegistry(Profile(name="t"), CallableSource(lambda: [("שירה לוי", "לקוח")]))
    return CloakGuard(reg, alerters=[CollectingAlerter()])


def test_outbound_then_inbound_roundtrip_via_hooks():
    g = _guard()
    sid = "s1"
    out_hook = make_outbound_hook(g, lambda: sid)
    in_hook = make_inbound_hook(g, lambda: sid)

    outbound = out_hook([{"role": "user", "content": "מייל לשירה לוי"}])
    assert "שירה לוי" not in " ".join(m["content"] for m in outbound)

    restored = in_hook({"content": "אשלח ל⟦לקוח_1⟧", "tool_calls": []})
    assert restored["content"] == "אשלח לשירה לוי"
