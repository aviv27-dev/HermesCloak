"""Transport-layer masking for LLM traffic — the provider-agnostic chokepoint.

The three source seams cover ONE call path (``build_api_kwargs``). hermes-agent also
reaches cloud models through ~120 auxiliary call sites (``agent/auxiliary_client.py``
→ title generation, context compression, vision, web-extract, approval, MCP…), plus
``trajectory_compressor`` calling ``chat.completions.create()`` directly. None of those
pass a seam, so on a stock install a conversation's history goes to the compression
model and every session's first exchange goes to the title model — in cleartext.

Seaming each call site is a losing race: every hermes release adds more. Instead this
patches the one place they all converge — ``httpx``, which the OpenAI SDK, the
Anthropic SDK and the native Gemini adapter all use. A new call site added by a future
hermes version is covered the moment it sends a request, with no new seam.

Direction of travel:
  * OUTBOUND — mask, but TARGETED. Only message-bearing subtrees (``messages``,
    ``system``, ``contents``, ``systemInstruction``, ``input``) are rewritten. Tool
    schemas, model names and sampling params are left byte-identical, because masking
    a tool's description or an enum value would silently break tool-calling.
  * INBOUND — restore, and BLANKET. Every string in a non-streaming JSON response is
    rehydrated. Unlike masking this needs no shape knowledge: a ⟦token⟧ appearing
    anywhere in a reply is by definition internal state that must become real again
    before it is persisted. Without this, a masked title request would persist
    "meeting with ⟦לקוח_1⟧" as the session title — a token on disk, which is exactly
    what the design forbids.

Streaming responses are left alone: the main chat path streams and is already restored
by seams B and C, and buffering an SSE body here would break incremental delivery. The
auxiliary calls that motivate this module are non-streaming, so they are covered.

Safety:
  * Acts only in MODE=enforce, and only on payloads that look like chat requests.
  * Fail-open everywhere: any error → the original request goes out unchanged.
  * Idempotent install(); kill-switch env HERMESCLOAK_HTTPX_OFF=1.
"""
import json as _json
import os

_INSTALLED = False

# Keys whose STRING values are message text worth masking.
_TEXT_KEYS = frozenset({"text", "content", "prompt", "arguments"})
# Keys we descend through to reach that text (Gemini parts, OpenAI tool-call args).
_RECURSE_KEYS = frozenset({"parts", "tool_calls", "function", "input"})
# Top-level request fields that carry conversation content.
_PAYLOAD_ROOTS = ("messages", "system", "contents", "systemInstruction", "input")


def _is_chat_payload(data) -> bool:
    """Conservative: only rewrite bodies that really look like a chat completion.

    Deliberately strict. A false positive would send ⟦tokens⟧ to a non-LLM API that
    happened to use a "messages" field, corrupting real outbound data.
    """
    if not isinstance(data, dict):
        return False
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict) and "role" in msgs[0]:
        return True
    if isinstance(data.get("contents"), list):                      # Gemini native
        return True
    if isinstance(data.get("input"), list) and "model" in data:     # OpenAI Responses
        return True
    return False


def _walk(node, fn):
    """Rewrite text under known text keys; leave every other key untouched."""
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [_walk(x, fn) for x in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out[k] = _walk(v, fn) if (k in _TEXT_KEYS or k in _RECURSE_KEYS) else v
        return out
    return node


def _mask_payload(data, mask):
    """Return (new_data, changed) with conversation content masked."""
    out = dict(data)
    changed = False
    for root in _PAYLOAD_ROOTS:
        if root in out:
            new = _walk(out[root], mask)
            if new != out[root]:
                out[root] = new
                changed = True
    return out, changed


def _restore_all(obj, restore):
    """Blanket-restore every string — safe in this direction, no shape knowledge needed."""
    if isinstance(obj, str):
        return restore(obj)
    if isinstance(obj, list):
        return [_restore_all(x, restore) for x in obj]
    if isinstance(obj, dict):
        return {k: _restore_all(v, restore) for k, v in obj.items()}
    return obj


def _engine():
    """The per-HERMES_HOME engine, or None when not enforcing."""
    try:
        from hermescloak.adapter.hermes_live import transport_engine
        return transport_engine()
    except Exception:
        return None


def _audit(kind: str, detail: dict) -> None:
    try:
        import time
        from hermescloak.adapter.hermes_live import _cloak_dir
        rec = {"kind": kind, "ts": time.strftime("%F %T"), **detail}
        with open(_cloak_dir() / "audit.log", "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _mask_request(request):
    """Return a masked copy of `request`, or the original when nothing applies."""
    try:
        body = request.content
        if not body or b'"' not in body:
            return request
        eng = _engine()
        if eng is None:
            return request
        data = _json.loads(body)
        if not _is_chat_payload(data):
            return request
        masked, changed = _mask_payload(data, eng.mask_text)
        if not changed:
            return request
        new_bytes = _json.dumps(masked, ensure_ascii=False).encode("utf-8")

        import httpx
        headers = dict(request.headers)
        headers.pop("content-length", None)          # httpx recomputes it
        new_req = httpx.Request(request.method, request.url, headers=headers,
                                content=new_bytes, extensions=request.extensions)
        from urllib.parse import urlparse
        _audit("transport_masked", {"host": urlparse(str(request.url)).netloc,
                                    "entities": eng.vault.summary(),
                                    "real_values_in_outbound":
                                        eng.vault.count_present(new_bytes.decode("utf-8", "replace"))})
        return new_req
    except Exception as exc:
        # Unlike the source seams (whose try/except lives in hermes and swallows), this
        # is our own frame — raising here genuinely stops the request leaving the box,
        # so fail-closed is real at the transport layer without patching hermes at all.
        try:
            from hermescloak.adapter.hermes_live import fail_closed
            closed = fail_closed()
        except Exception:
            closed = False                            # can't read the policy → fail open
        if closed:
            from hermescloak.errors import CloakFailClosed
            _audit("fail_closed_block", {"error": repr(exc)})
            raise CloakFailClosed(f"masking failed, send blocked: {exc!r}") from exc
        return request                                # fail-open


def _restore_response(response) -> None:
    """Rehydrate ⟦tokens⟧ in an already-read JSON response, in place."""
    try:
        if "json" not in response.headers.get("content-type", ""):
            return
        raw = response.content
        if b"\xe2\x9f\xa6" not in raw:                # UTF-8 for ⟦ — cheap guard
            return
        eng = _engine()
        if eng is None:
            return
        from hermescloak.restorer import restore_text
        data = _json.loads(raw)
        restored = _restore_all(data, lambda s: restore_text(s, eng.vault))
        new_bytes = _json.dumps(restored, ensure_ascii=False).encode("utf-8")
        response._content = new_bytes                 # response is fully read here
        response.headers["content-length"] = str(len(new_bytes))
        _audit("transport_restored", {"bytes": len(new_bytes)})
    except Exception:
        pass                                          # fail-open


def install(hermes_home: str | None = None) -> bool:
    """Patch httpx so LLM traffic is masked outbound and restored inbound."""
    global _INSTALLED
    if _INSTALLED or os.environ.get("HERMESCLOAK_HTTPX_OFF") == "1":
        return False
    try:
        import httpx
    except Exception:
        return False
    if hermes_home:
        os.environ.setdefault("HERMES_HOME", hermes_home)

    if not getattr(httpx.Client.send, "__hermescloak_llm__", False):
        _orig_send = httpx.Client.send

        def send(self, request, **kwargs):
            request = _mask_request(request)
            response = _orig_send(self, request, **kwargs)
            if not kwargs.get("stream"):
                _restore_response(response)
            return response

        send.__hermescloak_llm__ = True
        httpx.Client.send = send

    if not getattr(httpx.AsyncClient.send, "__hermescloak_llm__", False):
        _orig_asend = httpx.AsyncClient.send

        async def asend(self, request, **kwargs):
            request = _mask_request(request)
            response = await _orig_asend(self, request, **kwargs)
            if not kwargs.get("stream"):
                _restore_response(response)
            return response

        asend.__hermescloak_llm__ = True
        httpx.AsyncClient.send = asend

    _INSTALLED = True
    return True
