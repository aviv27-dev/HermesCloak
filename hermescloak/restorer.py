from typing import Any
from hermescloak.tokens import TOKEN_RE, find_tokens
from hermescloak.vault import Vault


def restore_text(text: str, vault: Vault) -> str:
    def _sub(m):
        real = vault.restore_token(m.group(0))
        return real if real is not None else m.group(0)
    return TOKEN_RE.sub(_sub, text)


def restore_json(obj: Any, vault: Vault) -> Any:
    if isinstance(obj, str):
        return restore_text(obj, vault)
    if isinstance(obj, list):
        return [restore_json(x, vault) for x in obj]
    if isinstance(obj, dict):
        return {k: restore_json(v, vault) for k, v in obj.items()}
    return obj


def leftover_tokens(text: str, vault: Vault) -> list[str]:
    """Tokens still present that the vault CANNOT restore (the fail-safe signal)."""
    return [t for t in find_tokens(text) if vault.restore_token(t) is None]
