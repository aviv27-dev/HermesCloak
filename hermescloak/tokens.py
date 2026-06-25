import re

OPEN, CLOSE = "⟦", "⟧"
# type = one-or-more non-underscore, non-bracket chars; n = digits
TOKEN_RE = re.compile(r"⟦([^_⟧]+)_(\d+)⟧")


def make_token(entity_type: str, n: int) -> str:
    # the type segment must not contain '_' or the bracket chars, or TOKEN_RE can't parse it back
    safe_type = entity_type.replace("_", "").replace(OPEN, "").replace(CLOSE, "")
    return f"{OPEN}{safe_type}_{n}{CLOSE}"


def find_tokens(text: str) -> list[str]:
    return [m.group(0) for m in TOKEN_RE.finditer(text)]
