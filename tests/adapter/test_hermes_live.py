import json
import hermescloak.adapter.hermes_live as live


class _FakeAgent:
    def __init__(self, sid):
        self.session_id = sid


def _setup(tmp_path, monkeypatch, mode):
    live._ENGINES.clear()
    live._TRANSPORT_ENGINES.clear()
    live._AGENT_VAULTS.clear()
    live._mode_cache.clear()
    home = tmp_path / "home"
    cloak = home / "cloak"
    cloak.mkdir(parents=True)
    (cloak / "MODE").write_text(mode, encoding="utf-8")
    (cloak / "gazetteer.txt").write_text("שירה לוי\tלקוח\n", encoding="utf-8")
    (cloak / "profile.yaml").write_text("profile: example\nlanguages: [he, en]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return cloak


def test_off_is_passthrough(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "off")
    msgs = [{"role": "user", "content": "שירה לוי בטלפון 050-1234567"}]
    assert live.cloak_sanitize_outbound(_FakeAgent("s1"), msgs) is msgs  # untouched


def test_shadow_sends_original_but_logs_detection(tmp_path, monkeypatch):
    cloak = _setup(tmp_path, monkeypatch, "shadow")
    msgs = [{"role": "user", "content": "שירה לוי בטלפון 050-1234567"}]
    out = live.cloak_sanitize_outbound(_FakeAgent("s1"), msgs)
    assert out is msgs                                  # ZERO behaviour change (original sent)
    audit = (cloak / "audit.log").read_text(encoding="utf-8")
    # audit logs counts/types only, never the real values
    assert "שירה לוי" not in audit and "050-1234567" not in audit
    rec = json.loads(audit.splitlines()[-1])
    assert rec["kind"] == "shadow_detect"
    summary = json.loads(rec["detail"])
    assert summary.get("לקוח") == 1 and summary.get("טלפון") == 1


def test_enforce_tokenizes_outbound(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    agent = _FakeAgent("s1")
    out = live.cloak_sanitize_outbound(agent, [{"role": "user", "content": "מייל לשירה לוי a@b.co.il"}])
    blob = " ".join(m["content"] for m in out)
    assert "שירה לוי" not in blob and "a@b.co.il" not in blob   # no real values to cloud
    assert "⟦" in blob


def test_no_restore_on_return_functions_exist():
    """v2 contract: restore-on-return is GONE. A hermes checkout still carrying the
    old B/C seams must degrade to a no-op via its own except-ImportError, which only
    works if these names really are absent — a reintroduction would silently revive
    the restore path, so pin their absence."""
    assert not hasattr(live, "cloak_restore_inbound")
    assert not hasattr(live, "cloak_filter_stream_delta")
    assert not hasattr(live, "cloak_restore_text_for")


def test_engine_for_home_is_mode_independent_singleton(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "shadow")
    a = live._engine_for_home()
    b = live._engine_for_home()
    assert a is b                                       # one engine per HERMES_HOME
    assert live.transport_engine() is None              # transport acts only in enforce


def test_enforce_failopen_on_engine_error(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    monkeypatch.setattr(live, "_engine_for", lambda agent: (_ for _ in ()).throw(RuntimeError("boom")))
    msgs = [{"role": "user", "content": "שירה לוי"}]
    assert live.cloak_sanitize_outbound(_FakeAgent("s1"), msgs) is msgs   # fail-open -> original
