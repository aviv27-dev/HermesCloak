"""The Jev residual check inside the live request path: off by default; on → one call per request
with the MASKED text; `jev_residual` audited (counts only); `jev_action: block` withholds; a dead
service never blocks the turn (fail-open + `jev_unavailable`)."""
import json

import pytest

import hermescloak.adapter.hermes_live as live
from hermescloak import decide as D
from tests.test_decide import _Fake

REQ = lambda: {"model": "m", "messages": [{"role": "user", "content": "דנה לוי, טלפון 050-1234567, שאלה."}]}  # noqa: E731


def _setup(tmp_path, monkeypatch, profile_yaml, fake=None, key="k-test"):
    home = tmp_path / "hh"
    cloak = home / "cloak"
    cloak.mkdir(parents=True)
    (cloak / "MODE").write_text("enforce", encoding="utf-8")
    (cloak / "gazetteer.txt").write_text("דנה לוי\tלקוח\n", encoding="utf-8")
    (cloak / "profile.yaml").write_text("profile: t\n" + profile_yaml, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    if fake is not None:
        monkeypatch.setenv("HERMESCLOAK_JEV_URL", fake.url)
    monkeypatch.setattr(D, "_shared", None)         # a fresh breaker per test
    live._ENGINES.clear()
    live._ENGINE_SIG.clear()
    return cloak


def _audit(cloak):
    return [json.loads(l) for l in (cloak / "audit.log").read_text(encoding="utf-8").splitlines()]


def test_off_by_default_no_call(tmp_path, monkeypatch):
    f = _Fake()
    try:
        cloak = _setup(tmp_path, monkeypatch, "", fake=f)
        live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
        assert f.calls == [] and not any(e["kind"].startswith("jev") for e in _audit(cloak))
    finally:
        f.close()


def test_on_sends_only_masked_text_and_audits_probabilities(tmp_path, monkeypatch):
    f = _Fake(answers={"person": {"type": "noul", "noul": 0.12}, "contact": {"type": "noul", "noul": 0.05},
                       "identifier": {"type": "noul", "noul": 0.81}})
    try:
        cloak = _setup(tmp_path, monkeypatch, "jev_check: true\njev_min_confidence: 0.7\n", fake=f)
        out = live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
        assert "דנה לוי" not in out["messages"][0]["content"]                 # still the normal masking
        assert len(f.calls) == 1
        sent = json.dumps(f.calls[0]["body"]["state"], ensure_ascii=False)
        assert "דנה לוי" not in sent and "050-1234567" not in sent and "⟦לקוח_1⟧" in sent
        assert set(f.calls[0]["body"]["questions"]) == {"person", "contact", "identifier"}
        ev = [e for e in _audit(cloak) if e["kind"] == "jev_residual"]
        assert len(ev) == 1
        d = json.loads(ev[0]["detail"])
        assert d["hits"] == {"identifier": 0.81} and d["probs"]["person"] == 0.12
        assert "דנה" not in ev[0]["detail"]
    finally:
        f.close()


def test_block_action_withholds_the_request_on_a_hit(tmp_path, monkeypatch):
    f = _Fake(answers={"person": {"type": "noul", "noul": 0.95}})
    try:
        cloak = _setup(tmp_path, monkeypatch, "jev_check: true\njev_action: block\n", fake=f)
        out = live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
        assert out["messages"][0]["content"] == live._REDACTED_JEV
        blocked = [e for e in _audit(cloak) if e["kind"] == "blocked_send"]
        assert blocked and json.loads(blocked[0]["detail"])["reason"] == "jev_residual"
    finally:
        f.close()


def test_audit_action_never_changes_the_request_even_on_a_hit(tmp_path, monkeypatch):
    f = _Fake(answers={"person": {"type": "noul", "noul": 0.95}})
    try:
        _setup(tmp_path, monkeypatch, "jev_check: true\n", fake=f)
        out = live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
        assert "⟦לקוח_1⟧" in out["messages"][0]["content"] and live._REDACTED_JEV not in json.dumps(out)
    finally:
        f.close()


def test_dead_service_is_fail_open_and_audited(tmp_path, monkeypatch):
    f = _Fake()
    f.close()                                       # port now refuses connections
    cloak = _setup(tmp_path, monkeypatch, "jev_check: true\njev_action: block\n", fake=f)
    out = live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
    assert "⟦לקוח_1⟧" in out["messages"][0]["content"]
    assert any(e["kind"] == "jev_unavailable" for e in _audit(cloak))


def test_no_key_is_skipped_quietly(tmp_path, monkeypatch):
    cloak = _setup(tmp_path, monkeypatch, "jev_check: true\n", key="")
    out = live.cloak_sanitize_request(REQ(), "s1", "chat_completions")
    assert "⟦לקוח_1⟧" in out["messages"][0]["content"]
    kinds = [e["kind"] for e in _audit(cloak)]
    assert "jev_unavailable" in kinds and "jev_residual" not in kinds


def test_profile_keys_load_from_yaml(tmp_path):
    from hermescloak.profile import Profile
    p = tmp_path / "p.yaml"
    p.write_text("profile: x\njev_check: true\njev_action: block\njev_min_confidence: 0.6\n"
                 "never_mask_domains: [aviv-law.co.il]\n", encoding="utf-8")
    prof = Profile.from_yaml(str(p))
    assert prof.jev_check and prof.jev_action == "block" and prof.jev_min_confidence == 0.6
    assert prof.never_mask_domains == ["aviv-law.co.il"]
    assert Profile(name="d").never_mask_domains == ["gov.il", "muni.il", "knesset.il", "idf.il"]
