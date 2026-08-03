"""fail_mode must actually do something.

It was parsed into Profile from the start but read by nothing, so a deployment that
set `fail_mode: closed` believed it had a blocking guarantee while the code
unconditionally failed open and forwarded unmasked content.
"""
import pytest

from hermescloak.adapter import hermes_live
from hermescloak.errors import CloakFailClosed


class _Agent:
    session_id = "s1"


@pytest.fixture
def cloak_home(tmp_path, monkeypatch):
    d = tmp_path / "cloak"
    d.mkdir()
    (d / "MODE").write_text("enforce", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hermes_live._ENGINES.clear()
    hermes_live._TRANSPORT_ENGINES.clear()
    hermes_live._AGENT_VAULTS.clear()
    hermes_live._mode_cache.clear()
    return tmp_path


def _profile(home, mode):
    (home / "cloak" / "profile.yaml").write_text(
        f"profile: test\nfail_mode: {mode}\n", encoding="utf-8")


def _break_masking(monkeypatch):
    def boom(agent):
        raise RuntimeError("detector exploded")
    monkeypatch.setattr(hermes_live, "_engine_for", boom)


def test_fail_closed_blocks_the_send(cloak_home, monkeypatch):
    _profile(cloak_home, "closed")
    _break_masking(monkeypatch)
    with pytest.raises(CloakFailClosed):
        hermes_live.cloak_sanitize_outbound(_Agent(), [{"role": "user", "content": "hi"}])


def test_fail_open_forwards_original(cloak_home, monkeypatch):
    _profile(cloak_home, "open")
    _break_masking(monkeypatch)
    msgs = [{"role": "user", "content": "hi"}]
    assert hermes_live.cloak_sanitize_outbound(_Agent(), msgs) is msgs


def test_missing_profile_defaults_to_open(cloak_home, monkeypatch):
    _break_masking(monkeypatch)
    msgs = [{"role": "user", "content": "hi"}]
    assert hermes_live.cloak_sanitize_outbound(_Agent(), msgs) is msgs
    assert hermes_live.fail_closed() is False
