"""The v2 transport hook: mask chat requests, egress-restore action requests,
and — deliberately — restore NOTHING on return.

Nothing here imports or simulates hermes: the point is that a bare httpx call is
protected on its own, which is what covers auxiliary call sites (title generation,
context compression, vision…) and model-written scripts alike.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

httpx = pytest.importorskip("httpx")

from hermescloak.integrations import httpx_llm  # noqa: E402

MAIL = "dana@example.org"
PHONE = "050-1234567"
PROMPT = f"Client contact: {MAIL}, phone {PHONE}"


@pytest.fixture
def cloak_home(tmp_path, monkeypatch):
    d = tmp_path / "cloak"
    d.mkdir()
    (d / "MODE").write_text("enforce", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # engines/vaults are cached per HERMES_HOME; clear so each test starts clean
    from hermescloak.adapter import hermes_live
    hermes_live._TRANSPORT_ENGINES.clear()
    hermes_live._AGENT_VAULTS.clear()
    hermes_live._mode_cache.clear()
    return tmp_path


@pytest.fixture
def echo_server():
    """Fake endpoint: records the wire body; chat POSTs get a model-style echo that
    REPEATS the tokens (like a real model reply); other POSTs get {"ok": true}."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            body = self.rfile.read(n).decode("utf-8")
            seen["body"] = body
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            msgs = payload.get("messages") if isinstance(payload, dict) else None
            if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
                echoed = payload["messages"][-1]["content"]
                resp = json.dumps(
                    {"choices": [{"message": {"role": "assistant",
                                              "content": f"Noted: {echoed}"}}]},
                    ensure_ascii=False).encode("utf-8")
            else:
                resp = b'{"ok": true}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


@pytest.fixture(autouse=True)
def installed():
    httpx_llm._INSTALLED = False
    httpx_llm.install()
    yield


def _chat(url, **extra):
    body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": PROMPT}], **extra}
    return httpx.post(f"{url}/v1/chat/completions", json=body, timeout=10)


def test_pii_never_reaches_the_wire(cloak_home, echo_server):
    url, seen = echo_server
    _chat(url)
    assert MAIL not in seen["body"]
    assert PHONE not in seen["body"]
    assert "⟦" in seen["body"] or "\\u27e6" in seen["body"]


def test_response_is_NOT_restored(cloak_home, echo_server):
    """v2 contract: no placeholders are replaced on return. The caller sees the
    model's tokens verbatim — restore happens only at action egress."""
    url, _ = echo_server
    reply = _chat(url).json()["choices"][0]["message"]["content"]
    assert MAIL not in reply and PHONE not in reply
    assert "⟦" in reply


def test_tool_schemas_are_not_rewritten(cloak_home, echo_server):
    """Masking a tool description or enum would silently break tool-calling."""
    url, seen = echo_server
    tools = [{"type": "function",
              "function": {"name": "send_email", "description": "Send mail to an address"}}]
    _chat(url, tools=tools)
    assert json.loads(seen["body"])["tools"] == tools


def test_action_request_with_tokens_is_egress_restored(cloak_home, echo_server):
    """A non-chat request (the action itself — a sendMail body, an API POST) that
    carries ⟦tokens⟧ gets real values on the wire. This is the ONLY restore in v2."""
    url, seen = echo_server
    masked = _chat(url).json()["choices"][0]["message"]["content"]  # contains tokens
    assert "⟦" in masked
    # the "agent" now performs an action using the masked text — e.g. mails it
    httpx.post(f"{url}/v1/sendMail", json={"to": "court@example.gov", "body": masked},
               timeout=10)
    assert MAIL in seen["body"] and PHONE in seen["body"]   # real values in the ACTION
    assert "⟦" not in seen["body"] and "\\u27e6" not in seen["body"]


def test_escaped_tokens_are_found(cloak_home, echo_server):
    """httpx serializes json= with ensure_ascii=True → Hebrew tokens arrive as
    \\u27e6 escapes. The hook must catch that form, not just literal ⟦ bytes."""
    url, seen = echo_server
    _chat(url)                                              # mint tokens in the vault
    from hermescloak.adapter.hermes_live import _engine_for_home
    token = next(iter(_engine_for_home().vault._token_to_real))
    body_bytes = json.dumps({"note": f"re: {token}"}).encode()  # ensure_ascii → ⟦
    assert b"\\u27e6" in body_bytes and "⟦".encode() not in body_bytes
    httpx.post(f"{url}/api/notes", content=body_bytes,
               headers={"content-type": "application/json"}, timeout=10)
    assert "⟦" not in seen["body"] and "\\u27e6" not in seen["body"]
    assert MAIL in seen["body"] or PHONE in seen["body"]


def test_off_mode_no_masking_but_egress_still_restores(cloak_home, echo_server):
    url, seen = echo_server
    _chat(url)                                              # enforce: mint tokens
    from hermescloak.adapter.hermes_live import _engine_for_home
    token = next(iter(_engine_for_home().vault._token_to_real))
    (cloak_home / "cloak" / "MODE").write_text("off", encoding="utf-8")
    from hermescloak.adapter import hermes_live
    hermes_live._mode_cache.clear()
    _chat(url)
    assert MAIL in seen["body"]                             # masking off → PII flows
    # ... but a leaked token in an action is STILL restored (mode-independent):
    httpx.post(f"{url}/api/x", json={"v": token}, timeout=10)
    assert "⟦" not in seen["body"] and "\\u27e6" not in seen["body"]


def test_tokenless_non_chat_payloads_are_untouched(cloak_home, echo_server):
    url, seen = echo_server
    body = {"messages": ["just", "strings"], "to": MAIL}    # not chat-shaped, no tokens
    httpx.post(f"{url}/webhook", json=body, timeout=10)
    assert json.loads(seen["body"]) == body                 # byte-level passthrough


def test_install_is_idempotent():
    httpx_llm._INSTALLED = False
    assert httpx_llm.install() is True
    assert httpx_llm.install() is False
