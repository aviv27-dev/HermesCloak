from hermescloak.adapter.guard import CloakGuard
from hermescloak.adapter.session_registry import SessionRegistry
from hermescloak.adapter.alerts import CollectingAlerter
from hermescloak.entities import CallableSource
from hermescloak.profile import Profile


# --- fakes to force failures ---
class _BoomEngine:
    def sanitize_outbound(self, messages):
        raise RuntimeError("sanitize boom")
    def restore_inbound(self, response):
        raise RuntimeError("restore boom")

class _BoomRegistry:
    def get_or_create(self, session_id):
        return _BoomEngine()


def real_guard(alerter):
    reg = SessionRegistry(Profile(name="t"), CallableSource(lambda: [("שירה לוי", "לקוח")]))
    return CloakGuard(reg, alerters=[alerter])


def test_normal_sanitize_tokenizes():
    g = real_guard(CollectingAlerter())
    out = g.sanitize_outbound([{"role": "user", "content": "שירה לוי"}], "s1")
    assert "⟦לקוח_1⟧" in out[-1]["content"]
    assert g.pending == []

def test_sanitize_failure_is_failopen_and_silent_until_flush():
    alerter = CollectingAlerter()
    g = CloakGuard(_BoomRegistry(), alerters=[alerter])
    original = [{"role": "user", "content": "שירה לוי"}]
    out = g.sanitize_outbound(original, "s1")
    assert out == original                       # FAIL-OPEN: unfiltered original returned
    assert len(alerter.events) == 0              # SILENT during the turn
    assert g.pending[0].kind == "unfiltered_sent"
    g.flush_alerts()
    assert alerter.events[0].kind == "unfiltered_sent"   # alert only AFTER flush

def test_restore_leftover_token_buffers_alert():
    g = real_guard(CollectingAlerter())
    g.sanitize_outbound([{"role": "user", "content": "שירה לוי"}], "s1")
    out = g.restore_inbound({"content": "⟦לקוח_1⟧ ⟦טלפון_9⟧", "tool_calls": []}, "s1")
    assert "שירה לוי" in out["content"]          # known token restored
    assert g.pending[0].kind == "leftover_token" and "⟦טלפון_9⟧" in g.pending[0].detail

def test_restore_exception_is_failopen():
    g = CloakGuard(_BoomRegistry(), alerters=[CollectingAlerter()])
    resp = {"content": "⟦לקוח_1⟧", "tool_calls": []}
    out = g.restore_inbound(resp, "s1")
    assert out == resp                            # returns original on failure
    assert g.pending[0].kind == "restore_error"

def test_flush_clears_pending():
    g = CloakGuard(_BoomRegistry(), alerters=[CollectingAlerter()])
    g.sanitize_outbound([{"role": "user", "content": "x"}], "s1")
    g.flush_alerts()
    assert g.pending == []
