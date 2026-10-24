"""Hostile inputs must never make enforce send a real value: a lone UTF-16 surrogate, absurdly deep
nesting, an odd MODE file. All data is synthetic."""
import json

import hermescloak.adapter.hermes_live as live
from tests.adapter.test_hermes_live import _FakeAgent, _setup

SECRET = 'שירה לוי, ת"ז 000000018'


def _sent(msgs):
    return json.dumps(live.cloak_sanitize_outbound(_FakeAgent("s1"), msgs), ensure_ascii=False)


def test_lone_surrogate_in_history_still_masks(tmp_path, monkeypatch):
    cloak = _setup(tmp_path, monkeypatch, "enforce")
    out = _sent([{"role": "tool", "content": "truncated emoji \ud83d"}, {"role": "user", "content": SECRET}])
    assert "שירה לוי" not in out and "000000018" not in out
    assert "000000018" not in (cloak / "audit.log").read_text(encoding="utf-8", errors="replace")


def test_deeply_nested_tool_arguments_still_mask(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    deep = SECRET
    for _ in range(900):
        deep = [deep]
    msgs = [{"role": "user", "content": SECRET},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "x", "arguments": json.dumps({"a": deep})}}]}]
    out = _sent(msgs)
    assert "שירה לוי" not in out and "000000018" not in out


def test_mode_file_written_by_powershell_is_enforce(tmp_path, monkeypatch):
    cloak = _setup(tmp_path, monkeypatch, "enforce")
    (cloak / "MODE").write_bytes("enforce\r\n".encode("utf-16"))
    assert live.mode() == "enforce"
    (cloak / "MODE").write_bytes(b"\xef\xbb\xbfenforce\n")
    assert live.mode() == "enforce"
    (cloak / "MODE").write_text("enforse", encoding="utf-8")          # a typo fails closed
    assert live.mode() == "enforce"
    (cloak / "MODE").unlink()
    assert live.mode() == "off"


def test_cloak_files_are_private(tmp_path, monkeypatch):
    import os
    import stat
    cloak = _setup(tmp_path, monkeypatch, "enforce")
    os.chmod(cloak, 0o755)
    old = os.umask(0o022)
    try:
        _sent([{"role": "user", "content": SECRET}])
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(cloak / "audit.log").st_mode) == 0o600
    for p in (cloak / "vaults").iterdir():
        assert stat.S_IMODE(os.stat(p).st_mode) & 0o077 == 0, p
    assert stat.S_IMODE(os.stat(cloak / "vaults").st_mode) == 0o700


def test_mangled_token_never_writes_its_text_to_the_audit(tmp_path, monkeypatch):
    cloak = _setup(tmp_path, monkeypatch, "enforce")
    _sent([{"role": "user", "content": "מייל a@b.co.il"}])
    live.cloak_restore_normalized(type("R", (), {"content": "⟦⟦מייל_1⟧_7⟧", "tool_calls": []})())
    assert "a@b.co.il" not in (cloak / "audit.log").read_text(encoding="utf-8")


def test_chat_shaped_tool_call_arguments_are_restored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "enforce")
    _sent([{"role": "user", "content": "מייל a@b.co.il"}])
    resp = {"choices": [{"message": {"content": "ok", "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "send", "arguments": '{"to": "⟦מייל_1⟧"}'}}]}}]}
    live.cloak_restore_chat_response(resp)
    assert json.loads(resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"to": "a@b.co.il"}
