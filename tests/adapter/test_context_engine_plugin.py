"""CloakContextEngine — masking through hermes's supported select_context() hook.

These tests need a real hermes-agent checkout on sys.path (the engine subclasses
hermes's ContextCompressor). They skip cleanly where none exists (CI without
hermes); on a deployment machine they run against the installed version, which is
exactly the compatibility signal we want after an upgrade.
"""
import copy
import json
import os
from pathlib import Path

import pytest

_HERMES_ROOT = os.environ.get(
    "HERMES_AGENT_ROOT", str(Path.home() / ".hermes" / "hermes-agent"))

if not (Path(_HERMES_ROOT) / "agent" / "context_compressor.py").exists():
    pytest.skip("no hermes-agent checkout to test against", allow_module_level=True)

import sys                                                    # noqa: E402
if _HERMES_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_ROOT)

from hermescloak.adapter.context_engine import build_cloak_engine  # noqa: E402

MAIL = "dana@example.org"
PHONE = "050-1234567"
TEXT = f"Email {MAIL}, call {PHONE}"


@pytest.fixture
def cloak_home(tmp_path, monkeypatch):
    d = tmp_path / "cloak"
    d.mkdir()
    (d / "MODE").write_text("enforce", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermescloak.adapter import hermes_live
    hermes_live._TRANSPORT_ENGINES.clear()
    hermes_live._AGENT_VAULTS.clear()
    hermes_live._mode_cache.clear()
    return tmp_path


def test_engine_builds_and_is_a_compressor():
    eng = build_cloak_engine()
    assert eng is not None and eng.name == "cloak"
    from agent.context_compressor import ContextCompressor
    assert isinstance(eng, ContextCompressor)         # compression behaviour inherited


def test_select_context_masks_request_only(cloak_home):
    eng = build_cloak_engine()
    msgs = [{"role": "user", "content": TEXT}]
    out = eng.select_context([dict(m) for m in msgs])
    blob = json.dumps(out, ensure_ascii=False)
    assert MAIL not in blob and PHONE not in blob and "⟦" in blob
    assert msgs[0]["content"] == TEXT                 # input list untouched


def test_select_context_handles_multimodal(cloak_home):
    eng = build_cloak_engine()
    out = eng.select_context([{"role": "user",
                               "content": [{"type": "text", "text": TEXT}]}])
    blob = json.dumps(out, ensure_ascii=False)
    assert MAIL not in blob and PHONE not in blob


def test_off_and_shadow_return_none(cloak_home):
    from hermescloak.adapter import hermes_live
    eng = build_cloak_engine()
    for mode in ("off", "shadow"):
        (cloak_home / "cloak" / "MODE").write_text(mode, encoding="utf-8")
        hermes_live._mode_cache.clear()
        # None = "leave the request unchanged" per the select_context contract
        assert eng.select_context([{"role": "user", "content": TEXT}]) is None


def test_deepcopy_contract(cloak_home):
    """The host deep-copies plugin engines per agent (#42449). All cloak state is
    module-level, so the copy must both succeed and still mask."""
    eng = copy.deepcopy(build_cloak_engine())
    out = eng.select_context([{"role": "user", "content": TEXT}])
    assert out is not None and MAIL not in json.dumps(out, ensure_ascii=False)


def test_failure_falls_open_not_raises(cloak_home, monkeypatch):
    """select_context must NEVER raise into the host loop (its contract catches, but
    we fail open ourselves so behaviour is deterministic, not contract-dependent)."""
    from hermescloak.adapter import hermes_live
    monkeypatch.setattr(hermes_live, "_engine_for_home",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    eng = build_cloak_engine()
    assert eng.select_context([{"role": "user", "content": TEXT}]) is None
