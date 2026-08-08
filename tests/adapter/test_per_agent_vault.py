"""Per-agent persistent vault: tokens minted before a gateway restart must still be
EGRESS-restorable after it — in v2 the durable vault exists for exactly one consumer,
the egress restore path (no restore-on-return exists). A restart that lost the map
would strand ⟦tokens⟧ in outbound actions forever."""
import importlib
import os


def _fresh_adapter(home):
    os.environ["HERMES_HOME"] = home
    ck = os.path.join(home, "cloak")
    os.makedirs(ck, exist_ok=True)
    with open(os.path.join(ck, "MODE"), "w") as f:
        f.write("enforce")
    with open(os.path.join(ck, "gazetteer.txt"), "w", encoding="utf-8") as f:
        f.write("דנה כהן\tלקוח\n")
    import hermescloak.adapter.hermes_live as hl
    importlib.reload(hl)
    return hl


class _Agent:
    def __init__(self, sid):
        self.session_id = sid


def test_egress_restore_survives_restart(tmp_path):
    home = str(tmp_path / "hermes")
    hl = _fresh_adapter(home)

    # session A: tokenize a message with PII
    a = _Agent("conversation-A")
    out = hl.cloak_sanitize_outbound(a, [{"role": "user", "content": 'דנה כהן, ת"ז 100000009'}])
    sent = out[-1]["content"]
    assert "דנה כהן" not in sent and "100000009" not in sent     # tokenized for the cloud

    # SIMULATE GATEWAY RESTART: wipe ALL in-memory state (engines + the per-agent vault cache)
    hl._ENGINES.clear()
    hl._AGENT_VAULTS.clear()
    hl._TRANSPORT_ENGINES.clear()

    # An outbound ACTION (email body, API call) later carries session A's tokens.
    # The on-disk vault must restore them — this is the only restore path in v2.
    from hermescloak.egress import restore_content
    restored, leftover = restore_content(sent, home)
    assert "דנה כהן" in restored and "100000009" in restored
    assert "⟦" not in restored
    assert leftover == []


def test_same_value_same_token_across_sessions(tmp_path):
    home = str(tmp_path / "hermes2")
    hl = _fresh_adapter(home)
    a = hl.cloak_sanitize_outbound(_Agent("s1"), [{"role": "user", "content": "ת\"ז 100000009"}])[-1]["content"]
    hl._ENGINES.clear(); hl._AGENT_VAULTS.clear()             # restart
    b = hl.cloak_sanitize_outbound(_Agent("s2"), [{"role": "user", "content": "שוב ת\"ז 100000009"}])[-1]["content"]
    # the same real value gets the SAME token across the restart+new session (shared
    # per-agent vault) — coreference for the model AND prompt-cache prefix stability
    import re
    ta = re.findall(r"⟦[^⟧]+⟧", a); tb = re.findall(r"⟦[^⟧]+⟧", b)
    assert ta and tb and ta[0] == tb[0]
