"""HermesCloak as a hermes-agent plugin (replaces the source-insertion seams).

Old hermes-agent had no extension point, so HermesCloak patched three files in the agent
checkout — and every hermes update overwrote them, silently switching the privacy layer off.
Current hermes-agent has a plugin system, so HermesCloak now loads as a plugin and
NOTHING in the hermes checkout is modified:

  OUTBOUND   ``llm_request`` middleware (official API): tokenize the final provider request
             (every transport: chat-completions / anthropic / codex-responses / bedrock).
  INBOUND    wrap ``Transport.normalize_response`` for every registered transport: restore
             content + reasoning + tool-call arguments on the NormalizedResponse, before hermes
             persists, displays or executes anything.
  STREAMING  wrap ``AIAgent._fire_stream_delta`` (the single emit point every consumer —
             gateway, TUI, API server, CLI, TTS — sits behind) + flush at stream end.
  AUXILIARY  wrap the auxiliary client's completion funnel (context compression, session
             titles, vision, approval ...) — those calls carried the raw conversation to a
             cloud model and were never covered by the old seams.

The wraps are runtime patches applied when the plugin registers; each one checks that its
target still exists and the result is written to ``$HERMES_HOME/cloak/audit.log`` as
``plugin_active`` (with any ``missing`` targets) so an incompatible hermes update is loud.
``python install/apply_hooks.py --verify --hermes-root <checkout>`` checks the same targets
statically. Every wrap is MODE-scoped and fail-open (see hermes_live).

Enable:  ``pip install -e /path/to/HermesCloak`` into hermes' venv, then
         ``hermes plugins enable hermescloak`` (or add it to ``plugins.enabled`` in config.yaml).
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import threading

logger = logging.getLogger("hermescloak")

_MARK = "__hermescloak__"
_PATCH_LOCK = threading.Lock()
_STATUS: dict[str, str] = {}


def _live():
    from hermescloak.adapter import hermes_live
    return hermes_live


# ---------------------------------------------------------------- outbound (middleware)

def llm_request_middleware(request=None, session_id="", api_mode="", **_kw):
    """hermes ``llm_request`` middleware: return ``{"request": tokenized}`` or None (unchanged)."""
    if not isinstance(request, dict):
        return None
    new = _live().cloak_sanitize_request(request, session_id=session_id or None, api_mode=api_mode or "")
    if new is request:
        return None
    return {"request": new, "source": "hermescloak", "reason": "pii-pseudonymization"}


# ---------------------------------------------------------------- inbound (transports)

def _wrap_normalize(cls) -> bool:
    fn = cls.__dict__.get("normalize_response") or getattr(cls, "normalize_response", None)
    if fn is None:
        return False
    if getattr(fn, _MARK, False):
        return True

    @functools.wraps(fn)
    def normalize_response(self, response, **kwargs):
        result = fn(self, response, **kwargs)
        return _live().cloak_restore_normalized(result)

    setattr(normalize_response, _MARK, True)
    cls.normalize_response = normalize_response
    return True


def _patch_transports() -> str:
    import agent.transports as tr
    tr._discover_transports()
    classes = list(getattr(tr, "_REGISTRY", {}).values())
    if not classes:
        return "missing: agent.transports._REGISTRY empty"
    wrapped = [c.__name__ for c in classes if _wrap_normalize(c)]
    # transports registered later (provider plugins) get wrapped on registration
    reg = tr.register_transport
    if not getattr(reg, _MARK, False):
        @functools.wraps(reg)
        def register_transport(api_mode, transport_cls):
            try:
                _wrap_normalize(transport_cls)
            except Exception:
                logger.debug("hermescloak: could not wrap transport %r", transport_cls, exc_info=True)
            return reg(api_mode, transport_cls)
        setattr(register_transport, _MARK, True)
        tr.register_transport = register_transport
    return "ok: " + ",".join(sorted(wrapped))


# ---------------------------------------------------------------- streaming

_STATE_ATTR = "_hermescloak_stream_state"


def _stream_state(agent) -> dict:
    st = agent.__dict__.get(_STATE_ATTR)
    if st is None:
        st = {}
        agent.__dict__[_STATE_ATTR] = st
    return st


def _patch_streaming() -> str:
    from agent.stream_delivery import StreamDeliveryMixin as M
    for name in ("_fire_stream_delta", "_emit_stream_end", "_reset_stream_delivery_tracking"):
        if not callable(getattr(M, name, None)):
            return f"missing: StreamDeliveryMixin.{name}"
    fire = M._fire_stream_delta
    if getattr(fire, _MARK, False):
        return "ok"
    end, reset = M._emit_stream_end, M._reset_stream_delivery_tracking

    @functools.wraps(fire)
    def _fire_stream_delta(self, text, *a, **k):
        if isinstance(text, str) and text:
            try:
                out = _live().cloak_filter_stream_delta(self, _stream_state(self), text)
            except Exception:
                out = text
            if not out:
                return None          # held back: an open ⟦ waiting for its ⟧
            text = out
        return fire(self, text, *a, **k)

    @functools.wraps(end)
    def _emit_stream_end(self, *a, **k):
        try:
            tail = _live().cloak_flush_stream(self, _stream_state(self))
            if tail:
                fire(self, tail)
        except Exception:
            pass
        return end(self, *a, **k)

    @functools.wraps(reset)
    def _reset_stream_delivery_tracking(self, *a, **k):
        _stream_state(self).pop("buf", None)   # a new attempt never inherits a stale partial token
        return reset(self, *a, **k)

    for f in (_fire_stream_delta, _emit_stream_end, _reset_stream_delivery_tracking):
        setattr(f, _MARK, True)
    M._fire_stream_delta = _fire_stream_delta
    M._emit_stream_end = _emit_stream_end
    M._reset_stream_delivery_tracking = _reset_stream_delivery_tracking
    return "ok"


# ---------------------------------------------------------------- auxiliary calls

def _aux_session_id() -> str:
    try:
        from agent.auxiliary_hooks import _parent_turn_identity
        return _parent_turn_identity().get("session_id") or "aux"
    except Exception:
        return "aux"


def _patch_auxiliary() -> str:
    import agent.auxiliary_client as ac
    sync_fn = getattr(ac, "_relay_sync_completion", None)
    async_fn = getattr(ac, "_relay_async_completion", None)
    if not callable(sync_fn) or not inspect.iscoroutinefunction(async_fn):
        return "missing: auxiliary_client._relay_sync_completion/_relay_async_completion"
    if getattr(sync_fn, _MARK, False):
        return "ok"

    def _sanitize(kwargs):
        if not isinstance(kwargs, dict):
            return kwargs
        return _live().cloak_sanitize_request(kwargs, session_id=_aux_session_id(),
                                              api_mode="chat_completions")

    @functools.wraps(sync_fn)
    def _relay_sync_completion(client, kwargs, *a, **k):
        resp = sync_fn(client, _sanitize(kwargs), *a, **k)
        return _live().cloak_restore_chat_response(resp)

    @functools.wraps(async_fn)
    async def _relay_async_completion(client, kwargs, *a, **k):
        resp = await async_fn(client, _sanitize(kwargs), *a, **k)
        return _live().cloak_restore_chat_response(resp)

    setattr(_relay_sync_completion, _MARK, True)
    setattr(_relay_async_completion, _MARK, True)
    ac._relay_sync_completion = _relay_sync_completion
    ac._relay_async_completion = _relay_async_completion
    return "ok"


# ---------------------------------------------------------------- registration

PATCHES = {
    "inbound": _patch_transports,
    "streaming": _patch_streaming,
    "auxiliary": _patch_auxiliary,
}


def install_runtime_patches() -> dict[str, str]:
    """Apply every runtime wrap (idempotent). Returns {name: "ok..." | "missing: ..." | "error: ..."}."""
    with _PATCH_LOCK:
        for name, fn in PATCHES.items():
            try:
                _STATUS[name] = fn()
            except Exception as exc:  # noqa: BLE001 — a missing target must never break hermes
                _STATUS[name] = f"error: {exc!r}"
        return dict(_STATUS)


def status() -> dict[str, str]:
    return dict(_STATUS)


def register(ctx) -> None:
    """hermes plugin entry point."""
    ctx.register_middleware("llm_request", llm_request_middleware)
    st = install_runtime_patches()
    st["outbound"] = "ok: llm_request middleware"
    missing = {k: v for k, v in st.items() if not v.startswith("ok")}
    live = _live()
    live._audit("plugin_active", json.dumps({"mode": live.mode(), "patches": st}, ensure_ascii=False))
    if missing:
        live._audit("seam_missing", json.dumps(missing, ensure_ascii=False))
        logger.warning("HermesCloak: some interception points are unavailable in this hermes "
                       "version — coverage is reduced: %s", missing)
