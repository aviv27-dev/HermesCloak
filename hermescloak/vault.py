import threading

from hermescloak.tokens import make_token


class Vault:
    """Per-session, in-memory, bidirectional real<->token map. Never persisted."""

    def __init__(self) -> None:
        # keyed by VALUE only — a given real value ALWAYS maps to the same token,
        # whatever detector/type found it first. Prevents one value getting two tokens
        # (e.g. labeled ⟦חפ⟧ vs bare ⟦מזהה⟧) which would break the model's cross-referencing.
        self._real_to_token: dict[str, str] = {}              # real -> token
        self._token_to_real: dict[str, str] = {}              # token -> real
        self._counters: dict[str, int] = {}                   # type -> last n
        # ONE vault is shared by every session of an agent, and the gateway runs sessions on
        # several threads: without this lock two values could race to the same counter and be
        # issued the SAME token (→ the reply restores one client's value into another's text).
        self._lock = threading.RLock()

    def tokenize(self, real: str, entity_type: str) -> str:
        with self._lock:
            existing = self._real_to_token.get(real)
            if existing is not None:
                return existing  # first detection's token/type wins → one token per value
            n = self._counters.get(entity_type, 0) + 1
            token = make_token(entity_type, n)
            while token in self._token_to_real:   # never reuse a token (e.g. after a merge/reload)
                n += 1
                token = make_token(entity_type, n)
            self._counters[entity_type] = n
            self._real_to_token[real] = token
            self._token_to_real[token] = real
            return token

    def known_values(self) -> list[str]:
        """Real values held, longest first (so an overlap resolves to the longer value)."""
        with self._lock:
            n = len(self._real_to_token)
            cached = getattr(self, "_known_cache", None)
            if cached is None or cached[0] != n:
                cached = (n, sorted(self._real_to_token, key=len, reverse=True))
                self._known_cache = cached
            return cached[1]

    def restore_token(self, token: str) -> str | None:
        return self._token_to_real.get(token)

    def is_empty(self) -> bool:
        return not self._token_to_real

    def summary(self) -> dict[str, int]:
        """type -> count of distinct values seen. No PII (counts only) — safe to log."""
        return dict(self._counters)

    def count_present(self, text: str) -> int:
        """How many distinct real values still appear in `text` (the leak proof).
        Returns a COUNT only — never exposes the values themselves."""
        with self._lock:
            reals = set(self._token_to_real.values())
        return sum(1 for real in reals if real and real in text)
