"""Walk a provider-shaped LLM request/response and transform only its human text.

The old seam tokenized ``msg["content"]`` only when it was a plain string, so PII in
multi-part content (``[{"type": "text", ...}]``), in prior assistant tool-call ARGUMENTS,
in Anthropic ``tool_use.input`` / ``tool_result`` blocks, and in Responses-API ``input``
items reached the cloud untouched. hermes-agent's ``llm_request`` middleware hands us the
final provider kwargs, so this walker understands every wire shape hermes speaks:

  chat_completions  messages[].content (str | parts), tool_calls[].function.arguments (JSON str)
  anthropic         system (str | blocks), messages[].content blocks: text / tool_use.input /
                    tool_result.content
  codex_responses   instructions, input[] items: message.content[].text,
                    function_call.arguments, function_call_output.output
  bedrock converse  system[].text, messages[].content[]: text / toolUse.input / toolResult.content

Never touched: ids, roles, names, signatures/encrypted reasoning (signed — editing them is
an HTTP 400), images/files, cache_control, tool schemas, and anything outside the
conversation keys. Pure functions; no hermes import.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

TextFn = Callable[[str], str]

# Top-level request keys that carry conversation text. Everything else (model, tools,
# tool_choice, extra_body, headers, ...) is passed through by reference.
REQUEST_TEXT_KEYS = ("messages", "system", "input", "instructions", "prompt")

# Blocks that are signed, opaque or binary — copied verbatim.
_SKIP_TYPES = frozenset({
    "thinking", "redacted_thinking", "reasoning", "image", "image_url", "input_image",
    "input_audio", "audio", "file", "input_file", "document", "image_generation_call",
})
# Keys whose values are identifiers / protocol state, never human text.
_SKIP_KEYS = frozenset({
    "role", "type", "id", "name", "tool_call_id", "tool_use_id", "call_id", "toolUseId",
    "signature", "encrypted_content", "cache_control", "status", "reasoning_details",
    "image_url", "source", "url", "file_id", "detail", "model", "extra_content",
    "response_item_id", "index", "format", "mime_type", "media_type",
})
# Keys whose string value is human text.
_TEXT_KEYS = frozenset({
    "content", "text", "output", "instructions", "system", "reasoning_content", "refusal", "prompt",
})
# Keys whose dict value is tool-call arguments: every string leaf is user data.
_ARGS_DICT_KEYS = frozenset({"input", "arguments", "json", "args"})

# A token's brackets as they appear inside a JSON string written with ensure_ascii.
_ESCAPED_TOKEN = re.compile(r"\\u27e6((?:[^\\\"]|\\u[0-9a-fA-F]{4}){1,40}?)\\u27e7", re.IGNORECASE)


def _walk(node: Any, fn: TextFn, mode: str) -> Any:
    """mode: 'struct' (dict keys decide), 'text' (strings are text), 'args' (every leaf)."""
    if isinstance(node, str):
        return fn(node) if mode in ("text", "args") and node else node
    if isinstance(node, list):
        out = [_walk(x, fn, mode) for x in node]
        return node if all(a is b for a, b in zip(out, node)) else out
    if isinstance(node, dict):
        if mode == "args":
            out = {k: _walk(v, fn, "args") for k, v in node.items()}
        else:
            if node.get("type") in _SKIP_TYPES:
                return node
            out = {}
            for k, v in node.items():
                if k in _SKIP_KEYS:
                    out[k] = v
                elif k == "arguments" and isinstance(v, str):
                    out[k] = transform_json_string(v, fn)
                elif k in _ARGS_DICT_KEYS and isinstance(v, dict):
                    out[k] = _walk(v, fn, "args")
                elif k in _TEXT_KEYS and isinstance(v, str):
                    out[k] = fn(v) if v else v
                elif isinstance(v, (dict, list)):
                    out[k] = _walk(v, fn, "text" if k in _TEXT_KEYS or k == "input" else "struct")
                else:
                    out[k] = v
        return node if all(out[k] is node[k] for k in node) else out
    return node


def transform_json_string(raw: str, fn: TextFn) -> str:
    """Apply ``fn`` to every string leaf of a JSON-encoded string (tool-call arguments).
    Returns the ORIGINAL string when nothing changed, so replayed history stays
    byte-identical (provider prompt caches key on it). Unparseable → treat as text."""
    if not raw:
        return raw
    try:
        obj = json.loads(raw)
    except Exception:
        out = fn(raw)
        return out
    new = _walk(obj, fn, "args")
    if new is obj:
        return raw
    return json.dumps(new, ensure_ascii=False)


def transform_request(request: dict, fn: TextFn) -> dict:
    """Return a shallow copy of a provider request with ``fn`` applied to its text.
    Containers that did not change are shared, not copied."""
    out = dict(request)
    for key in REQUEST_TEXT_KEYS:
        if key not in out:
            continue
        v = out[key]
        if isinstance(v, str):
            out[key] = fn(v) if v else v
        elif isinstance(v, (list, dict)):
            out[key] = _walk(v, fn, "text")
    return out


def request_text_blob(request: dict) -> str:
    """All conversation text of a request as one string (for the leak-count audit)."""
    parts: list[str] = []

    def _collect(s: str) -> str:
        parts.append(s)
        return s

    transform_request(request, _collect)
    return "\n".join(parts)


def unescape_tokens(raw: str) -> str:
    """``\\u27e6TYPE_1\\u27e7`` (a token inside ensure_ascii JSON) → ``⟦TYPE_1⟧``."""
    if "\\u27e6" not in raw and "\\u27E6" not in raw:
        return raw
    def _sub(m):
        inner = re.sub(r"\\u([0-9a-fA-F]{4})", lambda u: chr(int(u.group(1), 16)), m.group(1))
        return "⟦" + inner + "⟧"
    return _ESCAPED_TOKEN.sub(_sub, raw)


def inject_instruction(request: dict, instruction: str, api_mode: str = "") -> dict:
    """Append the token-handling instruction to the request's system prompt, in whichever
    slot this wire format uses. APPENDED (not prepended) so the cached prompt prefix stays
    valid. Returns a shallow copy; idempotent."""
    out = dict(request)
    if isinstance(out.get("instructions"), str):                       # Responses API
        if instruction not in out["instructions"]:
            out["instructions"] = (out["instructions"] + "\n\n" + instruction).strip()
        return out
    sysv = out.get("system")
    if isinstance(sysv, str):                                            # Anthropic (string)
        if instruction not in sysv:
            out["system"] = (sysv + "\n\n" + instruction).strip()
        return out
    if isinstance(sysv, list):                                           # Anthropic / Bedrock blocks
        if any(isinstance(b, dict) and b.get("text") == instruction for b in sysv):
            return out
        typed = any(isinstance(b, dict) and "type" in b for b in sysv)
        out["system"] = list(sysv) + [{"type": "text", "text": instruction} if typed
                                      else {"text": instruction}]
        return out
    if sysv is None and api_mode in ("anthropic_messages", "bedrock_converse"):
        # these wires carry the system prompt OUTSIDE messages; a role:system message is a 400
        out["system"] = instruction if api_mode == "anthropic_messages" else [{"text": instruction}]
        return out
    msgs = out.get("messages")
    if isinstance(msgs, list):                                           # chat completions
        new = list(msgs)
        for i, m in enumerate(new):
            if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                c = m.get("content")
                if isinstance(c, str):
                    if instruction not in c:
                        new[i] = {**m, "content": (c + "\n\n" + instruction).strip()}
                    out["messages"] = new
                    return out
                if isinstance(c, list):
                    if not any(isinstance(p, dict) and p.get("text") == instruction for p in c):
                        new[i] = {**m, "content": list(c) + [{"type": "text", "text": instruction}]}
                    out["messages"] = new
                    return out
        if out.get("system") is None and "input" not in out:
            new.insert(0, {"role": "system", "content": instruction})
            out["messages"] = new
        return out
    if isinstance(out.get("input"), list):                               # Responses without instructions
        out["instructions"] = instruction
    return out
