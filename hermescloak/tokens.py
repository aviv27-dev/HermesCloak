import re

OPEN, CLOSE = "⟦", "⟧"
# type = 1–40 chars without underscore/brackets/newline; n = 1–9 digits. Both are bounded: the old
# unbounded "[^_⟧]+" went quadratic on a run of "⟦" in hostile text (20 s on 50k chars), and it runs
# on every request.
TOKEN_RE = re.compile(r"⟦([^_⟦⟧\n]{1,40})_(\d{1,9})⟧")


def make_token(entity_type: str, n: int) -> str:
    # the type segment must not contain '_' or the bracket chars, or TOKEN_RE can't parse it back
    safe_type = entity_type.replace("_", "").replace(OPEN, "").replace(CLOSE, "")
    return f"{OPEN}{safe_type}_{n}{CLOSE}"


def find_tokens(text: str) -> list[str]:
    return [m.group(0) for m in TOKEN_RE.finditer(text)]
