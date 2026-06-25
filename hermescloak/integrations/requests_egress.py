"""Automatic egress-restore at the HTTP layer (the `requests` library).

Agents often send mail / write to external systems by writing *ad-hoc scripts*
that POST to an API with `requests` — there is no single mailer to wrap. A model
working in token-space can even build a token from char codes inside such a
script, so the literal ⟦token⟧ only exists in the final HTTP body. This patch
restores ⟦tokens⟧ in the OUTBOUND request body right before it leaves, using the
on-disk per-agent vault — so it catches those tokens no matter which script
produced them.

Design / safety:
  * Wraps `requests.sessions.Session.request`. Acts ONLY when the body actually
    contains the token marker ⟦ (so virtually every request is untouched).
  * Restoring ⟦tokens⟧ in an outbound body is always safe: a token in an external
    request is, by definition, leaked internal state that should be real.
  * Fail-open: any error → the original request is sent unchanged.
  * Idempotent install(); kill-switch env HERMESCLOAK_EGRESS_OFF=1.
  * Binary `files=` attachments (docx/pdf) are NOT handled here — use the
    format-aware file wrappers for those.
"""
import json as _json
import os

_INSTALLED = False


def _restore(s, home):
    from hermescloak.egress import restore_for_send
    return restore_for_send(s, home, where="http")


def _restore_body(kwargs, home):
    """Restore ⟦tokens⟧ inside json=/data= bodies. Returns True if anything changed."""
    changed = False
    j = kwargs.get("json")
    if j is not None:
        dumped = _json.dumps(j, ensure_ascii=False)
        if "⟦" in dumped:
            kwargs["json"] = _json.loads(_restore(dumped, home))
            changed = True
    d = kwargs.get("data")
    if isinstance(d, str) and "⟦" in d:
        kwargs["data"] = _restore(d, home)
        changed = True
    elif isinstance(d, bytes):
        try:
            text = d.decode("utf-8")
            if "⟦" in text:
                kwargs["data"] = _restore(text, home).encode("utf-8")
                changed = True
        except Exception:
            pass
    elif isinstance(d, dict):
        for k, v in list(d.items()):
            if isinstance(v, str) and "⟦" in v:
                d[k] = _restore(v, home); changed = True
    return changed


def install(hermes_home: str | None = None) -> bool:
    """Monkey-patch requests so outbound bodies get egress-restored. Safe to call once."""
    global _INSTALLED
    if _INSTALLED or os.environ.get("HERMESCLOAK_EGRESS_OFF") == "1":
        return False
    try:
        import requests.sessions as _s
    except Exception:
        return False
    home = hermes_home or os.environ.get("HERMES_HOME")
    _orig = _s.Session.request

    def request(self, method, url, **kwargs):
        try:
            if (("json" in kwargs and kwargs["json"] is not None) or kwargs.get("data") is not None):
                _restore_body(kwargs, home)
        except Exception:
            pass                       # fail-open: never block a request
        return _orig(self, method, url, **kwargs)

    request.__hermescloak_egress__ = True
    if not getattr(_s.Session.request, "__hermescloak_egress__", False):
        _s.Session.request = request
    _INSTALLED = True
    return True
