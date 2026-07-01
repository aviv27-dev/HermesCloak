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


def _audit_restored(home, url):
    """Positive proof the egress net fired on a real send (host only, no PII)."""
    try:
        import json as _j
        import time as _t
        from urllib.parse import urlparse
        from hermescloak.egress import _home
        log = os.path.join(_home(home), "cloak", "audit.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(_j.dumps({"kind": "egress_http_restored", "ts": _t.strftime("%F %T"),
                              "host": urlparse(url).netloc}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _restore_blob(d, home):
    """Restore a str/bytes body. Handles a LITERAL token, and a base64-of-text body
    (e.g. Graph MIME sendMail base64-encodes the whole calendar/email MIME, hiding the
    ⟦token⟧ from a plain scan). Returns (new_value_or_original, changed)."""
    raw = d if isinstance(d, str) else None
    if raw is None:
        try: raw = d.decode("utf-8")
        except Exception: raw = None
    # 1) literal token in the (text) body
    if raw is not None and "⟦" in raw:
        out = _restore(raw, home)
        return (out if isinstance(d, str) else out.encode("utf-8")), True
    # 2) base64-encoded TEXT body — decode, restore, re-encode (binary base64 fails the
    #    utf-8 decode and is skipped, so real binary attachments are left untouched/safe)
    try:
        import base64
        decoded = base64.b64decode(d, validate=True)
        text = decoded.decode("utf-8")
        if "⟦" in text:
            reenc = base64.b64encode(_restore(text, home).encode("utf-8"))
            return (reenc if isinstance(d, bytes) else reenc.decode("ascii")), True
    except Exception:
        pass
    return d, False


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
    if isinstance(d, (str, bytes)):
        new, ch = _restore_blob(d, home)
        if ch:
            kwargs["data"] = new; changed = True
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
                if _restore_body(kwargs, home):
                    _audit_restored(home, url)   # positive proof the net fired (no PII; host only)
        except Exception:
            pass                       # fail-open: never block a request
        return _orig(self, method, url, **kwargs)

    request.__hermescloak_egress__ = True
    if not getattr(_s.Session.request, "__hermescloak_egress__", False):
        _s.Session.request = request
    _INSTALLED = True
    return True
