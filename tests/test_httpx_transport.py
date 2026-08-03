"""The transport chokepoint: mask outbound LLM traffic, restore inbound.

These exercise the path that the three source seams do NOT cover — hermes reaches
cloud models through ~120 auxiliary call sites (title generation, context
compression, vision, web-extract…) that never pass a seam. Nothing here imports or
simulates hermes: the point is that a bare httpx call is protected on its own.
"""
import json
import os
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
    """Fake provider: records the wire body, echoes the last message back."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            body = self.rfile.read(n).decode("utf-8")
            seen["body"] = body
            payload = json.loads(body)
            echoed = payload["messages"][-1]["content"]
            resp = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": f"Noted: {echoed}"}}]},
                ensure_ascii=False).encode("utf-8")
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
    assert "⟦" in seen["body"]


def test_response_is_restored_for_the_caller(cloak_home, echo_server):
    url, _ = echo_server
    reply = _chat(url).json()["choices"][0]["message"]["content"]
    assert MAIL in reply and PHONE in reply
    assert "⟦" not in reply          # a token must never reach the caller / disk


def test_tool_schemas_are_not_rewritten(cloak_home, echo_server):
    """Masking a tool description or enum would silently break tool-calling."""
    url, seen = echo_server
    tools = [{"type": "function",
              "function": {"name": "send_email", "description": "Send mail to an address"}}]
    _chat(url, tools=tools)
    assert json.loads(seen["body"])["tools"] == tools


def test_off_mode_is_passthrough(cloak_home, echo_server):
    url, seen = echo_server
    (cloak_home / "cloak" / "MODE").write_text("off", encoding="utf-8")
    from hermescloak.adapter import hermes_live
    hermes_live._mode_cache.clear()
    _chat(url)
    assert MAIL in seen["body"]      # untouched when not enforcing


def test_non_chat_payloads_are_untouched(cloak_home, echo_server):
    """A non-LLM API that happens to POST JSON must not be rewritten."""
    url, seen = echo_server
    body = {"messages": ["just", "strings"], "to": MAIL}
    try:
        httpx.post(f"{url}/webhook", json=body, timeout=10)
    except Exception:
        pass                          # the echo handler expects chat shape; we only
    assert MAIL in seen["body"]       # care that the body went out unmodified


def test_install_is_idempotent():
    httpx_llm._INSTALLED = False
    assert httpx_llm.install() is True
    assert httpx_llm.install() is False
