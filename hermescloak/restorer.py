import re
from typing import Any
from hermescloak.tokens import TOKEN_RE, find_tokens, make_token
from hermescloak.vault import Vault

# A token as models sometimes rewrite it: other bracket styles (⟨ ⟩ [ ] [[ ]] 【 】), spaces
# inside the brackets, a space or hyphen instead of the underscore. Only restored when the
# canonical ⟦TYPE_N⟧ it normalizes to EXISTS in the vault — so ordinary bracketed text
# ("[note 1]") is never touched.
# Variant for tool ARGUMENTS (code, paths, queries): only the unambiguous bracket styles —
# "[NAME 1]" can be ordinary code there (array indexing), "⟦…⟧" / "⟨…⟩" never are.
_LOOSE_STRICT_RE = re.compile(
    r"(?:⟦|⟨)\s*([^\s\d_⟦⟧⟨⟩【】\[\]][^\d_⟦⟧⟨⟩【】\[\]\n]{0,24}?)\s*[_\- ]\s*(\d{1,6})\s*(?:⟧|⟩)"
)
_LOOSE_RE = re.compile(
    r"(?:⟦|⟨|【|\[\[?)\s*([^\s\d_⟦⟧⟨⟩【】\[\]][^\d_⟦⟧⟨⟩【】\[\]\n]{0,24}?)\s*[_\- ]\s*(\d{1,6})\s*(?:⟧|⟩|】|\]\]?)"
)


def restore_text(text: str, vault: Vault, tolerant=False) -> str:
    """``tolerant``: False | True (all bracket styles) | "strict" (⟦⟧/⟨⟩ only — tool args)."""
    def _sub(m):
        real = vault.restore_token(m.group(0))
        return real if real is not None else m.group(0)
    out = TOKEN_RE.sub(_sub, text)
    if tolerant and any(c in out for c in "⟦⟨【["):
        def _loose(m):
            real = vault.restore_token(make_token(m.group(1).strip(), int(m.group(2))))
            return real if real is not None else m.group(0)
        out = (_LOOSE_STRICT_RE if tolerant == "strict" else _LOOSE_RE).sub(_loose, out)
    return out


def restore_json(obj: Any, vault: Vault, tolerant: bool = False) -> Any:
    if isinstance(obj, str):
        return restore_text(obj, vault, tolerant)
    if isinstance(obj, list):
        return [restore_json(x, vault, tolerant) for x in obj]
    if isinstance(obj, dict):
        return {k: restore_json(v, vault, tolerant) for k, v in obj.items()}
    return obj


def leftover_tokens(text: str, vault: Vault, tolerant: bool = False) -> list[str]:
    """Tokens still present that the vault CANNOT restore (the fail-safe signal). With
    ``tolerant``, mangled forms of a type the vault issued (e.g. ``⟦לקוח 9⟧``) count too."""
    out = [t for t in find_tokens(text) if vault.restore_token(t) is None]
    if tolerant:
        # token type segments are sanitized ("CREDIT_CARD" → "CREDITCARD"): compare like with like
        types = {make_token(t, 0)[1:-3] for t in vault.summary()}
        for m in (_LOOSE_STRICT_RE if tolerant == "strict" else _LOOSE_RE).finditer(text):
            canon = make_token(m.group(1).strip(), int(m.group(2)))
            if canon[1:canon.rindex("_")] in types and canon not in out and vault.restore_token(canon) is None:
                out.append(canon)
    return out
