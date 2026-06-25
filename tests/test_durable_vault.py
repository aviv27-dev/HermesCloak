"""DurableVault: survives restart/recreation, keeps numbering, TTL, clear, golden round-trip."""
import json
import os
import time

import pytest

from hermescloak.durable_vault import DurableVault
from hermescloak.engine import Engine
from hermescloak.profile import Profile
from hermescloak.entities import CallableSource


def test_survives_restart_token_still_restorable(tmp_path):
    p = tmp_path / "v.json"
    v1 = DurableVault(str(p))
    tok = v1.tokenize("דנה כהן", "לקוח")
    v1.save()
    assert os.path.exists(p)
    # simulate process restart: brand-new instance, same path
    v2 = DurableVault(str(p))
    assert v2.restore_token(tok) == "דנה כהן"          # the bug this fixes
    assert not v2.is_empty()


def test_numbering_continues_after_reload(tmp_path):
    p = tmp_path / "v.json"
    v1 = DurableVault(str(p)); v1.tokenize("a@b.co", "מייל"); v1.save()
    v2 = DurableVault(str(p))
    t2 = v2.tokenize("c@d.co", "מייל"); v2.save()
    assert t2 == "⟦מייל_2⟧"                            # not reset to _1
    # and the same value keeps its token across reload
    assert v2.tokenize("a@b.co", "מייל") == "⟦מייל_1⟧"


def test_same_value_stable_token_across_restart(tmp_path):
    p = tmp_path / "v.json"
    v1 = DurableVault(str(p)); tok = v1.tokenize("050-1234567", "טלפון"); v1.save()
    v2 = DurableVault(str(p))
    assert v2.tokenize("050-1234567", "טלפון") == tok  # idempotent across restart


def test_ttl_expiry_starts_fresh(tmp_path):
    p = tmp_path / "v.json"
    v1 = DurableVault(str(p)); v1.tokenize("x", "לקוח"); v1.save()
    os.utime(p, (time.time() - 10_000, time.time() - 10_000))   # age the file
    v2 = DurableVault(str(p), ttl_seconds=3600)
    assert v2.is_empty()                                # expired → fresh
    assert not os.path.exists(p)                        # and removed


def test_clear_deletes_file(tmp_path):
    p = tmp_path / "v.json"
    v = DurableVault(str(p)); v.tokenize("y", "לקוח"); v.save()
    assert os.path.exists(p)
    v.clear()
    assert v.is_empty() and not os.path.exists(p)


def test_corrupt_file_starts_empty_not_crash(tmp_path):
    p = tmp_path / "v.json"
    p.write_text("{ this is not json", encoding="utf-8")
    v = DurableVault(str(p))                            # must not raise
    assert v.is_empty()


def test_file_permissions_0600(tmp_path):
    if os.name != "posix":
        pytest.skip("posix perms only")
    p = tmp_path / "v.json"
    v = DurableVault(str(p)); v.tokenize("z", "לקוח"); v.save()
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_sweep_expired(tmp_path):
    d = tmp_path / "vaults"; d.mkdir()
    old = d / "old.json"; old.write_text("{}", encoding="utf-8")
    new = d / "new.json"; new.write_text("{}", encoding="utf-8")
    os.utime(old, (time.time() - 10_000, time.time() - 10_000))
    removed = DurableVault.sweep_expired(str(d), ttl_seconds=3600)
    assert removed == 1 and not old.exists() and new.exists()


def test_engine_with_durable_vault_roundtrip_across_restart(tmp_path):
    """End-to-end: tokenize via Engine, 'restart', restore via a fresh Engine — no leftover."""
    p = tmp_path / "sess.json"
    prof = Profile(name="t")
    gaz = CallableSource(lambda: [("שירה לוי", "לקוח")])
    e1 = Engine(prof, entity_source=gaz, vault=DurableVault(str(p)))
    out = e1.sanitize_outbound([{"role": "user", "content": "שלח מייל לשירה לוי a@b.co.il"}])
    sent = out[-1]["content"]
    assert "שירה לוי" not in sent and "a@b.co.il" not in sent   # tokenized
    # restart: new engine, same vault file; model echoes the tokens
    e2 = Engine(prof, entity_source=gaz, vault=DurableVault(str(p)))
    restored, report = e2.restore_inbound({"content": sent, "tool_calls": []})
    assert "שירה לוי" in restored["content"] and "a@b.co.il" in restored["content"]
    assert report.leftover == []                        # the leftover bug is gone
