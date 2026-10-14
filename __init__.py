"""hermes-agent directory-plugin entry: this repository IS the plugin.

hermes imports this file when the plugin is installed with ``hermes plugins install
aviv27-dev/HermesCloak`` (or copied to ``~/.hermes/plugins/hermescloak``) and enabled. Hermes' package
manager installs the ``hermescloak`` package from this repository's pyproject.toml into the managed
runtime; when it has not (a manual copy, ``--no-deps``), the package is imported from this checkout.

FAIL-SAFE LOAD. hermes treats a plugin that fails to import as "not loaded" and carries on — which,
for a privacy filter, means requests leave unfiltered while ``plugins.enabled`` still lists it. So if
the real plugin cannot be imported or registered, this registers a tiny stand-in instead (stdlib
only): with ``$HERMES_HOME/cloak/MODE`` = ``enforce`` every model request — main turn and auxiliary
calls (titles, compression, vision) — is WITHHELD (its text replaced by a notice) and ``plugin_load_failed`` is written to the audit log; with ``off`` / ``shadow``
it changes nothing — exactly what those modes promise.
"""
import json as _json
import os as _os
import sys as _sys
import time as _time

_NOTICE = ("[HermesCloak: request withheld — the privacy plugin failed to load and MODE is enforce. "
           "Run `python install/apply_hooks.py --verify`, or `echo off > $HERMES_HOME/cloak/MODE` "
           "to send unfiltered.]")


def _impl():
    try:
        from hermescloak.hermes_plugin import register as _register
    except ImportError:
        here = _os.path.dirname(_os.path.abspath(__file__))
        if here not in _sys.path:
            _sys.path.insert(0, here)
        from hermescloak.hermes_plugin import register as _register
    return _register


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return _os.environ.get("HERMES_HOME") or _os.path.join(_os.path.expanduser("~"), ".hermes")


def _mode() -> str:
    try:
        with open(_os.path.join(_home(), "cloak", "MODE"), encoding="utf-8") as fh:
            return fh.read().strip().lower()
    except OSError:
        return "off"


def _audit(kind: str, detail: dict) -> None:
    try:
        d = _os.path.join(_home(), "cloak")
        _os.makedirs(d, exist_ok=True)
        line = {"ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "session_id": _home(),
                "detail": _json.dumps(detail, ensure_ascii=False)}
        with open(_os.path.join(d, "audit.log"), "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _withheld(request: dict, api_mode: str) -> dict:
    """The request with every piece of conversation text replaced by the notice (same wire shape)."""
    out = dict(request)
    if api_mode == "bedrock_converse":
        msg = {"role": "user", "content": [{"text": _NOTICE}]}
    else:
        msg = {"role": "user", "content": _NOTICE}
    if "input" in out:
        out["input"] = [msg]
    if "messages" in out or "input" not in out:
        out["messages"] = [msg]
    for k in ("system", "instructions"):
        if k in out:
            out[k] = _NOTICE if isinstance(out[k], str) else [{"text": _NOTICE}] if api_mode == "bedrock_converse" \
                else [{"type": "text", "text": _NOTICE}]
    for k in ("tools", "tool_choice"):
        out.pop(k, None)
    return out


def _register_failsafe(ctx, exc: BaseException) -> None:
    reason = f"{type(exc).__name__}: {exc}"[:300]
    _audit("plugin_load_failed", {"error": reason, "mode": _mode(), "action": "withhold in enforce"})

    def llm_request_failsafe(request=None, api_mode="", **_kw):
        if not isinstance(request, dict) or _mode() != "enforce":
            return None
        _audit("blocked_send", {"reason": "plugin_load_failed", "error": reason})
        return {"request": _withheld(request, api_mode or ""), "source": "hermescloak",
                "reason": "privacy plugin failed to load (fail-closed in enforce)"}

    ctx.register_middleware("llm_request", llm_request_failsafe)
    _failsafe_auxiliary(reason)


def _failsafe_auxiliary(reason: str) -> None:
    """Auxiliary calls (session titles, compression, vision …) bypass the llm_request middleware —
    the real plugin wraps their funnel; the stand-in must too, or the title call leaks."""
    try:
        import inspect
        import agent.auxiliary_client as ac
    except Exception:
        return
    sync_fn, async_fn = getattr(ac, "_relay_sync_completion", None), getattr(ac, "_relay_async_completion", None)
    if getattr(sync_fn, "_hermescloak_failsafe", False):
        return

    def _guard(kwargs):
        if isinstance(kwargs, dict) and _mode() == "enforce":
            _audit("blocked_send", {"reason": "plugin_load_failed", "path": "auxiliary", "error": reason})
            return _withheld(kwargs, "chat_completions")
        return kwargs

    if callable(sync_fn):
        def _relay_sync_completion(client, kwargs, *a, **k):
            return sync_fn(client, _guard(kwargs), *a, **k)
        _relay_sync_completion._hermescloak_failsafe = True
        ac._relay_sync_completion = _relay_sync_completion
    if inspect.iscoroutinefunction(async_fn):
        async def _relay_async_completion(client, kwargs, *a, **k):
            return await async_fn(client, _guard(kwargs), *a, **k)
        ac._relay_async_completion = _relay_async_completion


def register(ctx):
    """hermes plugin entry point."""
    try:
        real = _impl()
    except Exception as exc:  # noqa: BLE001 — a missing dependency, a broken install …
        _register_failsafe(ctx, exc)
        return
    try:
        real(ctx)
    except Exception as exc:  # noqa: BLE001
        _register_failsafe(ctx, exc)
