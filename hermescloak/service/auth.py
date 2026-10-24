"""Shared secret between the NER service and its clients.

The service and every agent on the machine read one random token from a 0600 file (default
``~/.config/hermescloak/ner.token``, or $HERMESCLOAK_NER_TOKEN_FILE). Both directions are checked:
  * the client sends the token with each request, so a web page (CSRF, DNS rebinding) or another
    local user cannot drive the service;
  * before sending any text, the client asks the service to prove it knows the token (an HMAC of a
    fresh nonce), so a process squatting on the port while the service is down never receives
    unmasked text."""
import hashlib
import hmac
import os
import secrets

HEADER = "X-Cloak-Token"


def token_path() -> str:
    return os.environ.get("HERMESCLOAK_NER_TOKEN_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "hermescloak", "ner.token")


def read_token(path: str | None = None) -> str | None:
    try:
        with open(path or token_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def ensure_token(path: str | None = None) -> str:
    """The service side: read the token, creating it (0600, dir 0700) on first start."""
    path = path or token_path()
    tok = read_token(path)
    if tok:
        return tok
    from hermescloak.fsutil import private_dir
    private_dir(os.path.dirname(path))
    tok = secrets.token_urlsafe(32)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    try:
        os.link(tmp, path)                 # atomic, and never replaces a token another start wrote
    except FileExistsError:
        pass
    finally:
        os.remove(tmp)
    return read_token(path) or tok


def proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode("utf-8"), ("hermescloak-ner\0" + nonce).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))
