"""Fault-injection suite: the vault and the live adapter must survive concurrent processes,
crashes mid-write, corruption, missing/wrong keys, broken config, a hung NER service and log
growth — without ever leaking (sending a detected value) or losing a token mapping silently."""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

import hermescloak.adapter.hermes_live as live
from hermescloak.durable_vault import MAGIC, DurableVault, generate_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- vault

def test_concurrent_processes_never_share_a_token(tmp_path):
    path = str(tmp_path / "v.json")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from hermescloak.durable_vault import DurableVault\n"
        "v = DurableVault(%r)\n"
        "for i in range(40): v.tokenize(f'p{sys.argv[1]}-value-{i}', 'T')\n"
    ) % (ROOT, path)
    procs = [subprocess.Popen([sys.executable, "-c", code, str(n)]) for n in range(6)]
    assert all(p.wait(60) == 0 for p in procs)
    fresh = DurableVault(path)
    t2r = fresh._token_to_real
    assert len(t2r) == 240                                   # nothing lost
    assert len(set(t2r.values())) == 240                     # one value per token
    assert fresh.conflicts == 0


def test_token_minted_by_other_process_is_restorable(tmp_path):
    a = DurableVault(str(tmp_path / "v.json"))
    b = DurableVault(str(tmp_path / "v.json"))
    tok = a.tokenize("only-in-a", "T")
    assert b.restore_token(tok) == "only-in-a"               # b re-reads on a miss
    tok2 = b.tokenize("only-in-b", "T")
    assert tok2 != tok


def test_corrupt_main_file_recovers_from_backup(tmp_path):
    p = str(tmp_path / "v.json")
    v = DurableVault(p)
    t1 = v.tokenize("first", "T")
    v.tokenize("second", "T")                                # .bak now holds {first}
    with open(p, "w") as f:
        f.write("{ truncated garbage")                       # crash mid-write / disk error
    r = DurableVault(p)
    assert r.restore_token(t1) == "first"
    assert "vault_corrupt_quarantined" in r.events and "vault_restored_from_backup" in r.events
    assert any(n.startswith("v.json.corrupt-") for n in os.listdir(tmp_path))
    assert json.load(open(p))["token_to_real"]               # main file re-established


def test_corrupt_without_backup_starts_empty_without_raising(tmp_path):
    p = tmp_path / "v.json"
    p.write_text("not json")
    v = DurableVault(str(p))
    assert v.is_empty() and v.tokenize("x", "T")


def test_encrypted_at_rest(tmp_path):
    key = generate_key()
    p = str(tmp_path / "v.json")
    v = DurableVault(p, key=key)
    tok = v.tokenize("secret-person", "לקוח")
    raw = open(p, "rb").read()
    assert raw.startswith(MAGIC) and b"secret-person" not in raw and "לקוח".encode() not in raw
    assert DurableVault(p, key=key).restore_token(tok) == "secret-person"


@pytest.mark.parametrize("key", [None, "wrong"])
def test_encrypted_vault_without_right_key_is_never_overwritten(tmp_path, key):
    p = str(tmp_path / "v.json")
    DurableVault(p, key=generate_key()).tokenize("a", "T")
    before = open(p, "rb").read()
    v = DurableVault(p, key=generate_key() if key == "wrong" else None)
    assert v.memory_only and "vault_locked" in v.events
    v.tokenize("b", "T")
    v.save()
    assert open(p, "rb").read() == before


def test_expiry_starts_new_generation(tmp_path):
    p = str(tmp_path / "v.json")
    v = DurableVault(p, ttl_seconds=60)
    v.tokenize("a", "T")
    old = os.path.getmtime(p) - 3600
    os.utime(p, (old, old))
    v2 = DurableVault(p, ttl_seconds=60)
    assert v2.is_empty() and v2.generation != v.generation


# ---------------------------------------------------------------- live adapter fixtures

@pytest.fixture
def home(tmp_path, monkeypatch):
    live._ENGINES.clear()
    live._ENGINE_SIG.clear()
    h = tmp_path / "home"
    (h / "cloak").mkdir(parents=True)
    (h / "cloak" / "MODE").write_text("enforce", encoding="utf-8")
    (h / "cloak" / "gazetteer.txt").write_text("שירה לוי\tלקוח\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(h))
    for k in ("HERMESCLOAK_VAULT_KEY", "HERMESCLOAK_VAULT_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    return h / "cloak"


def _send(content, sid="s1"):
    req = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": content}]}
    return live.cloak_sanitize_request(req, sid, "chat_completions")


def _audit(cloak):
    p = cloak / "audit.log"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class _TC:
    def __init__(self, arguments):
        self.arguments = arguments
        self.function = self


class _NR:
    def __init__(self, content, tool_calls=None, reasoning=None):
        self.content, self.tool_calls, self.reasoning = content, tool_calls or [], reasoning


# ---------------------------------------------------------------- replay

def test_replay_keeps_history_byte_identical(home):
    _send("שירה לוי")
    # the model writes its own phone-shaped number: re-tokenizing it would EDIT this old turn
    model_text = "פנה ל-⟦לקוח_1⟧, היא זמינה ב-050-7654321"
    model_args = '{"to":"⟦לקוח_1⟧"}'
    nr = live.cloak_restore_normalized(_NR(model_text, [_TC(model_args)]))
    restored, restored_args = nr.content, nr.tool_calls[0].arguments
    assert "שירה לוי" in restored
    req = {"messages": [
        {"role": "user", "content": "שירה לוי"},
        {"role": "assistant", "content": restored,
         "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": restored_args}}]}]}
    out = live.cloak_sanitize_request(req, "s1", "chat_completions")
    assert out["messages"][-1]["content"] == model_text                     # exact model bytes
    assert out["messages"][-1]["tool_calls"][0]["function"]["arguments"] == model_args
    assert json.loads(_audit(home)[-1]["detail"])["replayed"] == 2


def test_replay_ignored_after_vault_reset(home):
    _send("שירה לוי")
    restored = live.cloak_restore_normalized(_NR("hi ⟦לקוח_1⟧")).content
    live._AGENT_VAULTS[live._home()].clear()                                # new generation
    out = live.cloak_sanitize_request({"messages": [{"role": "assistant", "content": restored}]},
                                      "s1", "chat_completions")
    assert out["messages"][-1]["content"] == "hi ⟦לקוח_1⟧"                 # re-tokenized fresh
    assert "שירה לוי" not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------- tolerant restore + audits

def test_tolerant_restore_and_mangled_leftover(home):
    _send("שירה לוי")
    nr = live.cloak_restore_normalized(_NR("א: ⟦לקוח 1⟧ ב: [לקוח_1] ג: ⟦לקוח 9⟧"))
    assert nr.content == "א: שירה לוי ב: שירה לוי ג: ⟦לקוח 9⟧"
    left = [e for e in _audit(home) if e["kind"] == "leftover_token"]
    assert left and "⟦לקוח_9⟧" in left[-1]["detail"]


def test_tolerant_restore_can_be_disabled(home):
    (home / "profile.yaml").write_text("profile: x\ntolerant_restore: false\n", encoding="utf-8")
    _send("שירה לוי")
    assert live.cloak_restore_normalized(_NR("[לקוח_1]")).content == "[לקוח_1]"


def test_audits_new_pii_and_leaked_original(home):
    _send("שירה לוי 050-1234567")
    live.cloak_restore_normalized(_NR("call 050-1234567 or new@x.io, ⟦לקוח_1⟧"))
    kinds = {e["kind"]: e["detail"] for e in _audit(home)}
    assert "טלפון" in kinds["leaked_original"]          # a masked value came back verbatim
    assert "מייל" in kinds["new_pii"]                   # the model introduced an address
    assert "050-1234567" not in json.dumps(_audit(home), ensure_ascii=False)   # counts only


def test_secrets_are_masked_and_restored_into_tool_args(home):
    key = "sk-proj-" + "A1b2C3d4" * 4
    out = _send(f"use key {key}")
    assert key not in json.dumps(out)
    tok = out["messages"][1]["content"].split()[-1]
    nr = live.cloak_restore_normalized(_NR(None, [_TC(json.dumps({"key": tok}))]))
    assert json.loads(nr.tool_calls[0].arguments) == {"key": key}


# ---------------------------------------------------------------- config self-healing

def test_broken_profile_keeps_last_good(home):
    (home / "profile.yaml").write_text("profile: x\nnever_mask: ['שירה לוי']\n", encoding="utf-8")
    assert "שירה לוי" in json.dumps(_send("שירה לוי"), ensure_ascii=False)   # never_mask honoured
    (home / "profile.yaml").write_text("profile: [unclosed\n", encoding="utf-8")
    assert "שירה לוי" in json.dumps(_send("שירה לוי", "s2"), ensure_ascii=False)  # last-good kept
    assert any(e["kind"] == "config_error" for e in _audit(home))


def test_unreadable_gazetteer_keeps_last_good_names(home):
    assert "שירה לוי" not in json.dumps(_send("שירה לוי"), ensure_ascii=False)
    g = home / "gazetteer.txt"
    g.unlink()
    g.mkdir()                                            # now unreadable as a file
    out = _send("שירה לוי", "s-new")
    assert "שירה לוי" not in json.dumps(out, ensure_ascii=False)
    assert any(e["kind"] == "config_error" for e in _audit(home))


def test_gazetteer_edit_applies_to_live_session(home):
    assert "דוד כץ" in json.dumps(_send("דוד כץ"), ensure_ascii=False)
    time.sleep(0.01)
    (home / "gazetteer.txt").write_text("שירה לוי\tלקוח\nדוד כץ\tלקוח\n", encoding="utf-8")
    assert "דוד כץ" not in json.dumps(_send("דוד כץ"), ensure_ascii=False)   # same session id


def test_unreadable_key_file_never_writes_plaintext(home, tmp_path):
    (home / "vault_key_file").write_text(str(tmp_path / "missing.key"), encoding="utf-8")
    _send("שירה לוי")
    vdir = home / "vaults"
    assert not vdir.exists() or not [f for f in vdir.iterdir() if f.suffix == ".json"]
    assert any(e["kind"] in ("vault_key_unreadable", "vault_key_invalid") for e in _audit(home))


def test_encrypted_vault_end_to_end_with_egress(home, tmp_path):
    keyf = tmp_path / "k"
    keyf.write_text(generate_key(), encoding="utf-8")
    (home / "vault_key_file").write_text(str(keyf), encoding="utf-8")
    out = _send("שירה לוי")
    tok = out["messages"][1]["content"]
    files = [f for f in (home / "vaults").iterdir() if f.suffix == ".json"]
    assert files and open(files[0], "rb").read().startswith(MAGIC)
    from hermescloak.egress import restore_content
    assert restore_content(tok, str(home.parent))[0] == "שירה לוי"


# ---------------------------------------------------------------- NER breaker, audit rotation, self-test

def test_hung_ner_service_costs_one_timeout_not_one_per_message(home):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)                                        # accepts, never answers
    (home / "ner_url").write_text(f"http://127.0.0.1:{srv.getsockname()[1]}", encoding="utf-8")
    from hermescloak.adapter.ner_client import NerServiceRecognizer
    orig = NerServiceRecognizer.__init__

    def fast(self, *a, **k):
        k["timeout"] = 0.3
        orig(self, *a, **k)

    NerServiceRecognizer.__init__ = fast
    try:
        t0 = time.monotonic()
        for i in range(8):
            _send(f"message {i} שירה לוי")
        elapsed = time.monotonic() - t0
    finally:
        NerServiceRecognizer.__init__ = orig
        srv.close()
    assert elapsed < 1.5, elapsed                        # was 8 × timeout before the breaker
    assert any(e["kind"] == "ner_down" for e in _audit(home))


def test_audit_log_rotates(home, monkeypatch):
    monkeypatch.setattr(live, "_AUDIT_MAX_BYTES", 2000)
    for i in range(200):
        live._audit("probe", "x" * 50)
    names = os.listdir(home)
    assert "audit.log.1" in names and os.path.getsize(home / "audit.log") < 4000
    assert "audit.log.4" not in names


def test_self_test_passes():
    assert live.self_test() == (True, "ok")


def test_concurrent_sessions_in_one_process(home):
    errors = []

    def worker(n):
        try:
            for i in range(30):
                out = _send(f"client{n}-{i}@firm.org", sid=f"s{n}")
                assert f"client{n}-{i}@firm.org" not in json.dumps(out)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    v = live._AGENT_VAULTS[live._home()]
    assert len(set(v._token_to_real)) == len(set(v._token_to_real.values())) == 240


# ---------------------------------------------------------------- journal

def test_new_values_are_O1_on_a_large_vault(tmp_path):
    v = DurableVault(str(tmp_path / "v.json"))
    with v._lock:
        for i in range(20000):
            v._real_to_token[f"old-{i}"] = f"⟦T_{i}⟧"
            v._token_to_real[f"⟦T_{i}⟧"] = f"old-{i}"
        v._counters["T"] = 20000
        v._dirty = True
    v.save()
    t0 = time.monotonic()
    for i in range(200):
        v.tokenize(f"new-{i}", "מייל")
    assert time.monotonic() - t0 < 2.0          # was ~4 s when every new value rewrote the file
    assert DurableVault(str(tmp_path / "v.json")).restore_token(v.tokenize("new-199", "מייל")) == "new-199"


def test_torn_journal_line_from_crash_is_ignored(tmp_path):
    p = str(tmp_path / "v.json")
    v = DurableVault(p)
    a = v.tokenize("a", "T")
    b = v.tokenize("b", "T")
    with open(p + ".journal", "ab") as f:
        f.write(b'{"t": "\xe2\x9f\xa6T_9\xe2\x9f\xa7", "r": "half-writ')     # power cut mid-append
    r = DurableVault(p)
    assert r.restore_token(a) == "a" and r.restore_token(b) == "b"
    c = r.tokenize("c", "T")
    assert c not in (a, b) and DurableVault(p).restore_token(c) == "c"


def test_corrupt_journal_line_is_skipped_not_fatal(tmp_path):
    p = str(tmp_path / "v.json")
    v = DurableVault(p)
    v.tokenize("a", "T")
    with open(p + ".journal", "ab") as f:
        f.write(b"garbage-line\n")
    good = DurableVault(p)
    tok = good.tokenize("z", "T")
    r = DurableVault(p)
    assert r.restore_token(tok) == "z" and "vault_journal_lines_skipped" in r.events


def test_compaction_keeps_everything_and_other_process_follows(tmp_path, monkeypatch):
    import hermescloak.durable_vault as dv
    monkeypatch.setattr(dv, "COMPACT_EVERY", 5)
    p = str(tmp_path / "v.json")
    a, b = DurableVault(p), DurableVault(p)
    toks = [a.tokenize(f"v{i}", "T") for i in range(23)]      # several compactions
    assert all(b.restore_token(t) == f"v{i}" for i, t in enumerate(toks))
    assert all(DurableVault(p).restore_token(t) == f"v{i}" for i, t in enumerate(toks))


def test_egress_sees_journal_only_tokens(tmp_path):
    from hermescloak.egress import restore_content
    vdir = tmp_path / "cloak" / "vaults"
    v = DurableVault(DurableVault.path_for(str(vdir), str(tmp_path)))
    v.tokenize("first", "T")
    tok = v.tokenize("journal-only", "T")                      # not compacted yet
    assert restore_content(tok, str(tmp_path))[0] == "journal-only"


def test_chaos_kill9_concurrent_writers(tmp_path):
    """4 processes mint through the live adapter while being SIGKILLed at random. Every token a
    process handed out (logged only after its request 'left') must restore to its own value and
    no token may ever stand for two values."""
    import random
    import signal
    if os.name == "nt":
        pytest.skip("SIGKILL chaos is POSIX-only")
    home = tmp_path / "home"
    (home / "cloak").mkdir(parents=True)
    (home / "cloak" / "MODE").write_text("enforce")
    worker = (
        "import os, sys, json, random\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "os.environ['HERMES_HOME'] = sys.argv[1]\n"
        "import hermescloak.adapter.hermes_live as live\n"
        "f = open(sys.argv[2], 'a')\n"
        "while True:\n"
        "    vals = [f'w{sys.argv[3]}-{random.randrange(10**9)}@corp.io' for _ in range(3)]\n"
        "    out = live.cloak_sanitize_request({'messages': [{'role': 'user', 'content': ' '.join(vals)}]}, 's', '')\n"
        "    for v, t in zip(vals, out['messages'][-1]['content'].split()):\n"
        "        f.write(json.dumps([t, v]) + '\\n')\n"
        "    f.flush()\n")
    procs = {}

    def start(i):
        procs[i] = subprocess.Popen([sys.executable, "-c", worker, str(home), str(tmp_path / f"{i}.jsonl"), str(i)])

    for i in range(4):
        start(i)
    end = time.time() + 5
    while time.time() < end:
        time.sleep(random.uniform(0.05, 0.3))
        i = random.randrange(4)
        procs[i].send_signal(signal.SIGKILL)
        procs[i].wait()
        start(i)
    for p in procs.values():
        p.send_signal(signal.SIGKILL)
        p.wait()
    vault_file = next((home / "cloak" / "vaults").glob("*.json"))
    v = DurableVault(str(vault_file))
    issued, owners = [], {}
    for i in range(4):
        for line in open(tmp_path / f"{i}.jsonl", encoding="utf-8"):
            try:
                issued.append(json.loads(line))
            except ValueError:
                pass                                   # the worker's OWN log line torn by the kill
    for tok, real in issued:
        owners.setdefault(tok, set()).add(real)
    assert len(issued) > 100
    assert not [t for t, r in issued if v.restore_token(t) != r]
    assert all(len(rs) == 1 for rs in owners.values())


def test_shadow_mode_never_writes_a_vault(home):
    (home / "MODE").write_text("shadow", encoding="utf-8")
    req = {"messages": [{"role": "user", "content": "שירה לוי 050-1234567"}]}
    assert live.cloak_sanitize_request(req, "s1", "") is req
    vdir = home / "vaults"
    assert not vdir.exists() or not [f for f in vdir.iterdir() if not f.name.endswith(".lock")]
    shadow = [e for e in _audit(home) if e["kind"] == "shadow_detect"]
    assert shadow and json.loads(shadow[-1]["detail"]) == {"לקוח": 1, "טלפון": 1}


# ---------------------------------------------------------------- review regressions

def test_keyless_process_never_reuses_a_keyed_process_token(tmp_path):
    p = str(tmp_path / "v.json")
    key = generate_key()
    DurableVault(p).tokenize("plain-first", "T")             # plaintext vault exists
    a = DurableVault(p, key=key)
    ta = a.tokenize("bob", "T")                               # encrypted journal line
    b = DurableVault(p)                                       # no key in this process
    tb = b.tokenize("carol", "T")
    assert tb != ta and b.memory_only
    assert DurableVault(p, key=key).restore_token(ta) == "bob"
    w = DurableVault(p, key=generate_key())                   # wrong key: same guarantee
    assert w.memory_only and w.tokenize("dave", "T") != ta
    assert DurableVault(p, key=key).restore_token(ta) == "bob"


def test_in_use_vault_is_not_swept_and_resurrects(tmp_path):
    p = str(tmp_path / "v.json")
    v = DurableVault(p, ttl_seconds=60)
    t1 = v.tokenize("v1", "T")
    old = time.time() - 3600
    for f in (p, p + ".journal"):
        if os.path.exists(f):
            os.utime(f, (old, old))
    v._last_touch = 0
    assert v.restore_token(t1) == "v1"                        # use refreshes the TTL
    assert DurableVault.sweep_expired(str(tmp_path), 60) == 0
    os.remove(p)                                              # deleted behind its back anyway
    v.tokenize("v2", "T")
    fresh = DurableVault(p, ttl_seconds=60)
    assert fresh.restore_token(t1) == "v1"
    assert "vault_resurrected" in v.events


def test_corrupt_encrypted_snapshot_falls_back_to_backup(tmp_path):
    p = str(tmp_path / "v.json")
    key = generate_key()
    v = DurableVault(p, key=key)
    tok = v.tokenize("x", "T")
    with open(p, "r+b") as f:                                 # corrupt the ciphertext
        f.seek(30)
        f.write(b"XXXXXXXX")
    r = DurableVault(p, key=key)
    assert not r.memory_only and r.restore_token(tok) == "x"
    assert "vault_restored_from_backup" in r.events


def test_leftover_detects_mangled_underscore_types():
    from hermescloak.restorer import leftover_tokens
    from hermescloak.vault import Vault
    v = Vault()
    v.tokenize("4111", "CREDIT_CARD")
    assert leftover_tokens("[CREDITCARD 9]", v, True) == ["⟦CREDITCARD_9⟧"]


def test_replay_cache_merges_across_processes(tmp_path):
    from hermescloak.replay import ReplayCache
    path = str(tmp_path / "replay.json")
    a, b = ReplayCache(path), ReplayCache(path)
    a.remember("real A", "tok A", "g")
    a.save()
    b.remember("real B", "tok B", "g")
    b.save()
    c = ReplayCache(path)
    assert c.lookup("real A", "g") == "tok A" and c.lookup("real B", "g") == "tok B"


def test_invalid_key_from_start_gets_private_range_and_generation(tmp_path):
    p = str(tmp_path / "v.json")
    d = DurableVault(p)
    d.tokenize("first", "T")
    m = DurableVault(p, key="!unreadable")                  # what an unreadable key file yields
    assert m.memory_only and "vault_key_invalid" in m.events
    tm = m.tokenize("secret-x", "T")
    td = d.tokenize("other-y", "T")
    assert tm != td and m.generation.startswith("mem-") and m.generation != d.generation
    assert DurableVault(p).restore_token(tm) is None         # disk never maps it to someone else


def test_cleared_vault_is_not_resurrected_by_a_live_process(tmp_path):
    p = str(tmp_path / "v.json")
    a, b = DurableVault(p), DurableVault(p)
    a.tokenize("pii@x.com", "T")
    b.sync()
    a.clear()                                                # deliberate deletion
    b.tokenize("new@x.com", "T")                             # B must drop the cleared map
    assert "vault_cleared_elsewhere" in b.events
    assert "pii@x.com" not in open(p, encoding="utf-8").read()
    assert not os.path.exists(p + ".cleared")                # a new lifetime began
    c = DurableVault(p)                                      # a third, stale process
    c._real_to_token["stale@x.com"] = "⟦T_77⟧"
    c._token_to_real["⟦T_77⟧"] = "stale@x.com"
    c.generation = "old-generation"
    c._seen = (0, 0, 0)
    c.sync()
    assert "vault_generation_changed" in c.events and c.restore_token("⟦T_77⟧") is None
