"""Live integration shim for hermes-agent. The HermesCloak hermes plugin
(``hermescloak.hermes_plugin``) calls these; so do the legacy source-insertion seams.

Scoped + safe by design:
  * MODE from `$HERMES_HOME/cloak/MODE` ∈ {absent|off|shadow|enforce}. Absent/off → passthrough,
    so any agent without the cloak dir is unaffected (scopes the deployment to one agent).
  * shadow → run detection to PROVE it works (audit logs counts/types only, NEVER real PII) but
    SEND THE ORIGINAL outbound and restore nothing (zero behaviour change).
  * enforce → tokenize the outbound copy; restore the response (content + tool-call args) in place.
  * FAIL-OPEN: any exception → return the original + buffer an audit line; never raise into hermes.

Duck-typed on the passed objects (no hermes import) so it stays testable and import-safe.
The audit log never contains real PII — only event kinds, counts, and entity types."""
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from hermescloak.adapter.alerts import AlertEvent, FileAuditAlerter
from hermescloak.engine import Engine
from hermescloak.entities import StaticFileSource
from hermescloak.profile import Profile

_LOCK = threading.Lock()
# One Engine per (home, session). Bounded LRU: a gateway that runs for weeks sees thousands of
# sessions, and each Engine holds a content cache — the old plain dict grew without limit.
_ENGINES: "OrderedDict[tuple[str, str], Engine]" = OrderedDict()
_MAX_ENGINES = 128


def _home() -> str:
    """The ACTIVE hermes home. Current hermes resolves it as context-local override (profiles,
    multi-tenant gateway) → $HERMES_HOME → platform default (%LOCALAPPDATA% on Windows), so
    reading only $HERMES_HOME/~/.hermes pointed at the wrong cloak dir under profiles."""
    try:
        from hermes_constants import get_hermes_home  # present when running inside hermes
        home = str(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    # normalized: the per-agent vault file is keyed by this string ("a/../b" ≠ "b" made 2 vaults)
    return os.path.realpath(os.path.expanduser(home))


def _cloak_dir() -> Path:
    return Path(_home()) / "cloak"


def _mode() -> str:
    try:
        return (_cloak_dir() / "MODE").read_text(encoding="utf-8").strip().lower()
    except Exception:
        return "off"


def _audit(kind: str, detail: str) -> None:
    try:
        FileAuditAlerter(str(_cloak_dir() / "audit.log")).send(AlertEvent(kind, _home(), detail))
    except Exception:
        pass


def _build_engine(session_id: str = "default") -> Engine:
    d = _cloak_dir()
    prof_path = d / "profile.yaml"
    profile = Profile.from_yaml(str(prof_path)) if prof_path.exists() else Profile(name="default")
    gaz = d / "gazetteer.txt"
    source = StaticFileSource(str(gaz)) if gaz.exists() else None
    extra: list = []
    ner_url_file = d / "ner_url"
    if ner_url_file.exists():
        url = ner_url_file.read_text(encoding="utf-8").strip()
        if url:                               # guard: empty ner_url → no NER (don't wire a dead client)
            from hermescloak.adapter.ner_client import NerServiceRecognizer
            extra.append(NerServiceRecognizer(url, control_file=str(d / "ner.ctl")))
    return Engine(profile, entity_source=source, extra_recognizers=extra or None,
                  vault=_agent_vault())


# ONE durable vault PER AGENT (keyed by HERMES_HOME), shared across all of that agent's
# sessions and reloaded on restart. This is what guarantees no broken deliverables: every
# token the agent ever issued stays restorable across sessions AND gateway restarts, so a
# restart mid-task can never leave an unrestorable ⟦token⟧ in an output. Opt out with
# "memory" in $HERMES_HOME/cloak/vault_mode (instant rollback). Fail-open to in-memory.
_AGENT_VAULTS: dict = {}
_VLOCK = threading.Lock()      # separate from _LOCK: _engine_for holds _LOCK when it calls here


def _agent_vault():
    home = _home()
    with _VLOCK:
        v = _AGENT_VAULTS.get(home)
        if v is None:
            try:
                d = _cloak_dir()
                mode_f = d / "vault_mode"
                if mode_f.exists() and mode_f.read_text(encoding="utf-8").strip().lower() == "memory":
                    from hermescloak.vault import Vault
                    v = Vault()                       # opt-out: in-memory
                else:
                    from hermescloak.durable_vault import DurableVault
                    vdir = d / "vaults"
                    DurableVault.sweep_expired(str(vdir))
                    v = DurableVault(DurableVault.path_for(str(vdir), home))  # per-AGENT, persistent
            except Exception:
                from hermescloak.vault import Vault
                v = Vault()                           # fail-open
            _AGENT_VAULTS[home] = v
        return v


def _engine_for_session(session_id) -> Engine:
    key = (_home(), str(session_id or "default"))
    with _LOCK:
        eng = _ENGINES.get(key)
        if eng is None:
            eng = _build_engine(key[1])               # shares the per-agent vault
            _ENGINES[key] = eng
            while len(_ENGINES) > _MAX_ENGINES:
                _ENGINES.popitem(last=False)
        else:
            _ENGINES.move_to_end(key)
        return eng


def _engine_for(agent) -> Engine:
    return _engine_for_session(getattr(agent, "session_id", None))


def mode() -> str:
    """Public: the live MODE (off | shadow | enforce) for the active hermes home."""
    m = _mode()
    return m if m in ("shadow", "enforce") else "off"


def _fail_closed() -> bool:
    try:
        p = _cloak_dir() / "profile.yaml"
        return p.exists() and Profile.from_yaml(str(p)).fail_mode == "closed"
    except Exception:
        return False


_REDACTED = "[HermesCloak: content withheld — PII filter failed (fail_mode: closed)]"


def cloak_sanitize_request(request: dict, session_id=None, api_mode: str = ""):
    """Tokenize a PROVIDER-SHAPED request (hermes ``llm_request`` middleware payload).

    Returns the request to send: a tokenized copy in enforce, the original in off/shadow.
    On an internal error: the original (fail_mode: open, default) or a copy whose text is
    replaced by a notice (fail_mode: closed) — never raises."""
    m = _mode()
    if m not in ("shadow", "enforce") or not isinstance(request, dict):
        return request
    from hermescloak.adapter import payload as _p
    try:
        eng = _engine_for_session(session_id)
        sanitized = _p.transform_request(request, eng.tokenize_text)
        if m == "shadow":
            _audit("shadow_detect", json.dumps(eng.vault.summary(), ensure_ascii=False))
            return request  # zero behaviour change — original goes to the cloud
        if eng.profile.token_instruction and not eng.vault.is_empty():
            from hermescloak.instruction import TOKEN_INSTRUCTION
            sanitized = _p.inject_instruction(sanitized, TOKEN_INSTRUCTION, api_mode)
        getattr(eng.vault, "save", lambda: None)()
        _audit("enforce_send", json.dumps(
            {"entities": eng.vault.summary(),
             "real_values_in_outbound": eng.vault.count_present(_p.request_text_blob(sanitized)),
             "api_mode": api_mode or "?"},
            ensure_ascii=False))
        return sanitized
    except Exception as exc:  # noqa: BLE001
        if m == "enforce" and _fail_closed():
            _audit("blocked_send", repr(exc))
            try:
                return _p.transform_request(request, lambda _s: _REDACTED)
            except Exception:
                return {**request, "messages": [{"role": "user", "content": _REDACTED}]}
        _audit("unfiltered_sent", repr(exc))
        return request


def _restore_args_string(raw, vault):
    """Restore tokens in a JSON-encoded tool-arguments string, incl. ``\\u27e6``-escaped ones."""
    from hermescloak.adapter.payload import transform_json_string, unescape_tokens
    from hermescloak.restorer import restore_text
    if not isinstance(raw, str) or not raw:
        return raw
    if "⟦" not in raw and "\\u27e6" not in raw.lower():
        return raw
    # json.loads decodes \\u27e6-escaped tokens; unparseable args fall back to text restore
    # after un-escaping the bracket pair.
    restore = lambda t: restore_text(unescape_tokens(t), vault)  # noqa: E731
    return transform_json_string(raw, restore)


def cloak_restore_normalized(response):
    """Restore real values IN PLACE on a hermes ``NormalizedResponse`` (any transport):
    content, reasoning, and every tool call's arguments — before hermes persists, displays,
    or executes anything. Enforce-only, fail-open, returns the same object."""
    if _mode() != "enforce" or response is None:
        return response
    try:
        from hermescloak.restorer import leftover_tokens, restore_text
        vault = _agent_vault()
        if vault.is_empty():
            return response
        surfaces: list[str] = []
        for attr in ("content", "reasoning"):
            v = getattr(response, attr, None)
            if isinstance(v, str) and "⟦" in v:
                v = restore_text(v, vault)
                setattr(response, attr, v)
            if isinstance(v, str):
                surfaces.append(v)
        for tc in list(getattr(response, "tool_calls", None) or []):
            fn = getattr(tc, "function", None) or tc
            if isinstance(fn, dict):
                args = fn.get("arguments")
                new = _restore_args_string(args, vault) if isinstance(args, str) else args
                if new is not args:
                    fn["arguments"] = new
            else:
                args = getattr(fn, "arguments", None)
                new = _restore_args_string(args, vault)
                if new is not args:
                    fn.arguments = new
            surfaces.append(str(new or ""))
        leftover = sorted({t for s in surfaces for t in leftover_tokens(s, vault)})
        _audit("enforce_restore", json.dumps({"restored": True, "leftover": len(leftover)},
                                              ensure_ascii=False))
        if leftover:
            _audit("leftover_token", ",".join(leftover))
    except Exception as exc:  # noqa: BLE001 — fail-open
        _audit("restore_error", repr(exc))
    return response


def cloak_restore_chat_response(response):
    """Restore an OpenAI-shaped ChatCompletion (auxiliary calls) in place: every
    ``choices[].message`` content + tool-call arguments. Enforce-only, fail-open."""
    if _mode() != "enforce" or response is None:
        return response
    try:
        choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
        for ch in choices or []:
            msg = ch.get("message") if isinstance(ch, dict) else getattr(ch, "message", None)
            if msg is None:
                continue
            if isinstance(msg, dict):
                cloak_restore_normalized(_DictView(msg))
            else:
                cloak_restore_normalized(msg)
    except Exception as exc:  # noqa: BLE001
        _audit("restore_error", repr(exc))
    return response


class _DictView:
    """Attribute view over a message dict so cloak_restore_normalized can treat it uniformly."""

    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name):
        return self._d.get(name)

    def __setattr__(self, name, value):
        self._d[name] = value


def cloak_sanitize_outbound(agent, api_messages):
    mode = _mode()
    if mode not in ("shadow", "enforce"):
        return api_messages
    try:
        eng = _engine_for(agent)
        sanitized = eng.sanitize_outbound(api_messages)
        if mode == "shadow":
            _audit("shadow_detect", json.dumps(eng.vault.summary(), ensure_ascii=False))
            return api_messages  # zero behaviour change — original goes to the cloud
        # enforce: prove no detected real value reached the cloud-bound copy
        blob = " ".join(m.get("content", "") for m in sanitized if isinstance(m.get("content"), str))
        _audit("enforce_send", json.dumps(
            {"entities": eng.vault.summary(), "real_values_in_outbound": eng.vault.count_present(blob)},
            ensure_ascii=False))
        return sanitized
    except Exception as exc:  # noqa: BLE001 — fail-open
        _audit("unfiltered_sent", repr(exc))
        return api_messages


def cloak_restore_text_for(agent, text):
    """Last-mile restore: rehydrate any ⟦token⟧ in an outgoing user-facing string
    (e.g. the gateway's streamed/accumulated reply) via the session vault. Fail-open;
    only acts in enforce when a token is present, so it's cheap and safe on every send."""
    if _mode() != "enforce" or not isinstance(text, str) or "⟦" not in text:
        return text
    try:
        from hermescloak.restorer import restore_text
        return restore_text(text, _engine_for(agent).vault)
    except Exception:
        return text


_MAX_TOKEN_HOLD = 48  # never hold back more than a plausible token's length


def cloak_filter_stream_delta(agent, state, text):
    """Restore ⟦tokens⟧ in a streaming delta BEFORE it reaches the gateway consumer,
    so the accumulated/sent reply shows real values. Buffers an unmatched '⟦' until its
    closing '⟧' arrives (tokens can split across deltas). `state` is a per-turn dict.
    Fail-open: on any error, returns the original text."""
    if _mode() != "enforce":
        return text
    try:
        buf = state.get("buf", "") + (text or "")
        open_idx = buf.rfind("⟦")
        # hold back a trailing, still-open token (no ⟧ yet) only if it's short enough
        if open_idx != -1 and "⟧" not in buf[open_idx:] and (len(buf) - open_idx) <= _MAX_TOKEN_HOLD:
            emit, hold = buf[:open_idx], buf[open_idx:]
        else:
            emit, hold = buf, ""
        state["buf"] = hold
        if not emit:
            return ""
        from hermescloak.restorer import restore_text
        return restore_text(emit, _engine_for(agent).vault)
    except Exception:
        return text


def cloak_flush_stream(agent, state):
    """End of a stream: release (restored) any text still held back waiting for a ``⟧``.
    Without this, a reply ending in an unmatched ``⟦`` silently lost its tail."""
    try:
        held = state.pop("buf", "")
        if not held:
            return ""
        from hermescloak.restorer import restore_text
        return restore_text(held, _engine_for(agent).vault) if _mode() == "enforce" else held
    except Exception:
        return ""


def _arg_payload(raw):
    """(payload_for_restore, was_json_string)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw), True
        except Exception:
            return raw, False
    return raw, False


def cloak_restore_inbound(agent, assistant_message):
    if _mode() != "enforce":
        return assistant_message  # nothing was tokenized outbound in off/shadow
    try:
        eng = _engine_for(agent)
        tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
        payload_tcs, meta = [], []
        for tc in tool_calls:
            raw = getattr(getattr(tc, "function", None), "arguments", None)
            payload, was_json = _arg_payload(raw)
            payload_tcs.append({"function": {"arguments": payload}})
            meta.append(was_json)
        resp = {"content": getattr(assistant_message, "content", None), "tool_calls": payload_tcs}
        restored, report = eng.restore_inbound(resp)
        if isinstance(getattr(assistant_message, "content", None), str) or restored["content"] is not None:
            assistant_message.content = restored["content"]
        for i, tc in enumerate(tool_calls):
            val = restored["tool_calls"][i]["function"]["arguments"]
            if meta[i]:
                tc.function.arguments = json.dumps(val, ensure_ascii=False)
            else:
                tc.function.arguments = val
        _audit("enforce_restore", json.dumps(
            {"restored": report.restored_any, "leftover": len(report.leftover)}, ensure_ascii=False))
        if report.leftover:
            _audit("leftover_token", ",".join(report.leftover))
        return assistant_message
    except Exception as exc:  # noqa: BLE001 — fail-open
        _audit("restore_error", repr(exc))
        return assistant_message
