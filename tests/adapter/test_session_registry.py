from hermescloak.adapter.session_registry import SessionRegistry
from hermescloak.entities import CallableSource
from hermescloak.profile import Profile

def reg():
    return SessionRegistry(Profile(name="t"),
                           CallableSource(lambda: [("שירה לוי", "לקוח")]))

def test_same_session_same_engine():
    r = reg()
    assert r.get_or_create("s1") is r.get_or_create("s1")

def test_different_sessions_isolated():
    r = reg()
    e1, e2 = r.get_or_create("s1"), r.get_or_create("s2")
    assert e1 is not e2
    e1.sanitize_outbound([{"role": "user", "content": "שירה לוי"}])
    assert not e1.vault.is_empty() and e2.vault.is_empty()  # vaults isolated

def test_reset_drops_engine():
    r = reg()
    r.get_or_create("s1")
    assert r.active_sessions() == 1
    r.reset("s1")
    assert r.active_sessions() == 0
