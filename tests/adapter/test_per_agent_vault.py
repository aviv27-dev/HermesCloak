"""Per-agent persistent vault via the live adapter: tokens stay restorable across
BOTH a gateway restart AND a different session — so no broken deliverables.
Reproduces the field failure (⟦מזהה_n⟧ left in an output) and proves it's fixed."""
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


def test_restore_survives_restart_and_new_session(tmp_path):
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

    # ... and the work continues in a DIFFERENT session B (compaction / new conversation).
    # The model's reply echoes session A's tokens; restoring must still yield real values.
    b = _Agent("conversation-B")

    class Fn:
        def __init__(s, args): s.arguments = args
    class Tc:
        def __init__(s, args): s.function = Fn(args)
    class Msg:
        def __init__(s, content, tcs): s.content = content; s.tool_calls = tcs

    reply = Msg(sent, [Tc('{"body": "' + sent.replace('"', '\\"') + '"}')])
    hl.cloak_restore_inbound(b, reply)

    assert "דנה כהן" in reply.content and "100000009" in reply.content   # restored across restart+session
    assert "⟦" not in reply.content                                       # no leftover token survives
    args = reply.tool_calls[0].function.arguments
    assert "100000009" in args and "⟦" not in args                        # tool-arg (deliverable) also clean


def test_same_value_same_token_across_sessions(tmp_path):
    home = str(tmp_path / "hermes2")
    hl = _fresh_adapter(home)
    a = hl.cloak_sanitize_outbound(_Agent("s1"), [{"role": "user", "content": "ת\"ז 100000009"}])[-1]["content"]
    hl._ENGINES.clear(); hl._AGENT_VAULTS.clear()             # restart
    b = hl.cloak_sanitize_outbound(_Agent("s2"), [{"role": "user", "content": "שוב ת\"ז 100000009"}])[-1]["content"]
    # the same real value gets the SAME token across the restart+new session (shared per-agent vault)
    import re
    ta = re.findall(r"⟦[^⟧]+⟧", a); tb = re.findall(r"⟦[^⟧]+⟧", b)
    assert ta and tb and ta[0] == tb[0]
