import json
import hermescloak.adapter.hermes_live as live


# --- fakes mirroring hermes' NormalizedResponse / tool_call shapes ---
class _FakeFn:
    def __init__(self, arguments):
        self.arguments = arguments

class _FakeTC:
    def __init__(self, arguments):
        self.function = _FakeFn(arguments)

class _FakeMsg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

class _FakeAgent:
    def __init__(self, sid):
        self.session_id = sid


def _setup(tmp_path, monkeypatch, mode):
    live._ENGINES.clear()
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


def test_enforce_tokenizes_outbound_and_restores_inbound(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    agent = _FakeAgent("s1")
    out = live.cloak_sanitize_outbound(agent, [{"role": "user", "content": "מייל לשירה לוי a@b.co.il"}])
    blob = " ".join(m["content"] for m in out)
    assert "שירה לוי" not in blob and "a@b.co.il" not in blob   # no real values to cloud
    # model echoes tokens back; tool-call args are a JSON STRING (hermes shape)
    msg = _FakeMsg("אשלח ל⟦לקוח_1⟧", [_FakeTC(json.dumps({"to": "⟦מייל_1⟧"}))])
    restored = live.cloak_restore_inbound(agent, msg)
    assert restored.content == "אשלח לשירה לוי"
    assert json.loads(restored.tool_calls[0].function.arguments) == {"to": "a@b.co.il"}


def test_cloak_restore_text_for_rehydrates_outgoing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    agent = _FakeAgent("s1")
    live.cloak_sanitize_outbound(agent, [{"role": "user", "content": "מייל לשירה לוי"}])
    assert live.cloak_restore_text_for(agent, "אשלח ל⟦לקוח_1⟧") == "אשלח לשירה לוי"

def test_cloak_restore_text_for_noop_when_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "off")
    assert live.cloak_restore_text_for(_FakeAgent("s1"), "x ⟦לקוח_1⟧") == "x ⟦לקוח_1⟧"

def test_stream_delta_restored_across_split_tokens(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    agent = _FakeAgent("s1")
    live.cloak_sanitize_outbound(agent, [{"role": "user", "content": "מייל לשירה לוי"}])
    state = {}
    # the token ⟦לקוח_1⟧ arrives split across three deltas
    out = ""
    for chunk in ["התשובה: ", "⟦לקו", "ח_1⟧", " סוף"]:
        out += live.cloak_filter_stream_delta(agent, state, chunk)
    assert out == "התשובה: שירה לוי סוף"        # reassembled + restored, no token leaked
    assert "⟦" not in out

def test_stream_delta_noop_when_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "off")
    assert live.cloak_filter_stream_delta(_FakeAgent("s1"), {}, "x ⟦לקוח_1⟧") == "x ⟦לקוח_1⟧"

def test_enforce_failopen_on_engine_error(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    monkeypatch.setattr(live, "_engine_for", lambda agent: (_ for _ in ()).throw(RuntimeError("boom")))
    msgs = [{"role": "user", "content": "שירה לוי"}]
    assert live.cloak_sanitize_outbound(_FakeAgent("s1"), msgs) is msgs   # fail-open -> original
