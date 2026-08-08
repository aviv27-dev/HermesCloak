"""Transport-layer hook on httpx — one patch, two jobs, one direction each.

httpx is where hermes traffic converges: the OpenAI SDK, the Anthropic SDK and the
native Gemini adapter all sit on it, and so do many model-written scripts. Patching
``Client.send`` / ``AsyncClient.send`` therefore covers call sites that no source
seam ever saw — and call sites that future hermes versions add.

Outbound, per request body:

  * **Chat payload → MASK.** The main loop is already masked by the cloak context
    engine (``select_context``), on which masking is idempotent — but hermes also
    reaches models from ~120 auxiliary call sites (title generation, context
    compression, vision, web-extract, approval, MCP…) that bypass the agent loop
    entirely. This hook is what masks those. Targeted rewrite: only message-bearing
    subtrees; tool schemas and params stay byte-identical.

  * **Non-chat payload carrying ⟦tokens⟧ → EGRESS-RESTORE.** Architecture v2 has no
    restore-on-return: the model works in token-space and its replies keep the
    tokens. The ONLY place a real value re-enters is an actual outbound action —
    sending the mail, posting to the API. A token in a request to a non-LLM host is
    by definition leaked internal state that must become real for the action to
    work, so it is restored here (mirroring the ``requests`` egress patch, which
    cannot see httpx traffic).

Inbound: **nothing.** v1 rehydrated JSON responses here; v2 deliberately does not —
no placeholders are replaced on return, anywhere.

Encoding trap: httpx serializes ``json=`` bodies with ``ensure_ascii=True``, so a
Hebrew token like ⟦לקוח_1⟧ arrives here as ``\\u27e6...`` escapes — a literal ``⟦``
byte-scan misses it. Both forms are handled.

Safety: masking acts only in MODE=enforce and only on chat-shaped payloads (a false
positive would corrupt a non-LLM request). Egress restore is mode-independent —
restoring a leaked token is always correct. Fail-open everywhere except a masking
failure under ``fail_mode: closed``, which raises in OUR frame and genuinely blocks
the request. Idempotent install(); kill-switch env HERMESCLOAK_HTTPX_OFF=1.
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

_TOKEN_MARKS = ("⟦", "\\u27e6")        # literal and JSON-escaped forms


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
    """Blanket-restore every string. Safe: this runs only on ACTION egress bodies."""
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


def _token_map() -> dict:
    """token -> real, merged from ALL on-disk vaults plus this process's engine.

    Disk first (cross-process/cross-session coverage — a model-written script can
    carry a token minted in an earlier gateway session); the live engine's vault
    then supplements with mappings from this very request cycle.
    """
    tm: dict = {}
    try:
        from hermescloak.egress import load_token_map
        tm.update(load_token_map())
    except Exception:
        pass
    try:
        from hermescloak.adapter.hermes_live import _engine_for_home
        tm.update(_engine_for_home().vault._token_to_real)
    except Exception:
        pass
    return tm


def _rebuild(request, new_bytes):
    import httpx
    headers = dict(request.headers)
    headers.pop("content-length", None)              # httpx recomputes it
    return httpx.Request(request.method, request.url, headers=headers,
                         content=new_bytes, extensions=request.extensions)


def _mask_chat(request, data):
    """Masked copy of a chat request, or the original when nothing applies."""
    eng = _engine()
    if eng is None:
        return request
    masked, changed = _mask_payload(data, eng.mask_text)
    if not changed:
        return request
    new_bytes = _json.dumps(masked, ensure_ascii=False).encode("utf-8")
    from urllib.parse import urlparse
    _audit("transport_masked", {"host": urlparse(str(request.url)).netloc,
                                "entities": eng.vault.summary(),
                                "real_values_in_outbound":
                                    eng.vault.count_present(new_bytes.decode("utf-8", "replace"))})
    return _rebuild(request, new_bytes)


def _egress_restore(request, text, data):
    """Restore ⟦tokens⟧ in a non-chat action body. Mode-independent."""
    tm = _token_map()
    if not tm:
        return request

    from hermescloak.tokens import TOKEN_RE, find_tokens

    def _sub(s):
        return TOKEN_RE.sub(lambda m: tm.get(m.group(0), m.group(0)), s)

    if data is not None:                              # JSON body (either escape form)
        restored = _restore_all(data, _sub)
        if restored == data:
            return request
        new_text = _json.dumps(restored, ensure_ascii=False)
    else:                                             # plain text body, literal tokens
        new_text = _sub(text)
        if new_text == text:
            return request

    leftover = [t for t in find_tokens(new_text) if t not in tm]
    from urllib.parse import urlparse
    _audit("egress_http_restored", {"host": urlparse(str(request.url)).netloc,
                                    "leftover": len(leftover)})
    if leftover:
        _audit("egress_leftover", {"where": "httpx", "tokens": leftover})
    return _rebuild(request, new_text.encode("utf-8"))


def _process_request(request):
    """Route one outbound request: chat → mask, token-bearing non-chat → restore."""
    try:
        body = request.content
        if not body:
            return request
        try:
            text = body.decode("utf-8")
        except Exception:
            return request                            # binary body — not ours
        data = None
        if text[:1] in ("{", "["):
            try:
                data = _json.loads(text)
            except Exception:
                data = None
        if isinstance(data, dict) and _is_chat_payload(data):
            return _mask_chat(request, data)
        if any(m in text for m in _TOKEN_MARKS):
            return _egress_restore(request, text, data)
        return request
    except Exception as exc:
        # Our own frame — raising here genuinely stops the request leaving the box,
        # so fail-closed is real at the transport layer without patching hermes.
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


def install(hermes_home: str | None = None) -> bool:
    """Patch httpx: mask LLM requests, egress-restore token-bearing action requests."""
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
            return _orig_send(self, _process_request(request), **kwargs)

        send.__hermescloak_llm__ = True
        httpx.Client.send = send

    if not getattr(httpx.AsyncClient.send, "__hermescloak_llm__", False):
        _orig_asend = httpx.AsyncClient.send

        async def asend(self, request, **kwargs):
            return await _orig_asend(self, _process_request(request), **kwargs)

        asend.__hermescloak_llm__ = True
        httpx.AsyncClient.send = asend

    _INSTALLED = True
    return True
