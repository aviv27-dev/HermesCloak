"""ReplayCache — send the model's own earlier turns back to it byte-for-byte.

hermes persists assistant turns with REAL values (restored). On the next request the outbound
filter re-tokenizes that history. Usually that round-trips exactly (the vault is stable), but
not always: a value the model wrote itself that looks like PII gets a fresh token, a tolerant
restore of a mangled token re-tokenizes to the canonical form, a new gazetteer entry changes an
old turn. Any difference is an EDIT to an earlier turn: it restarts the provider's prompt cache
from that point (cost) and, on Claude with extended thinking, invalidates the signature of every
later thinking block (HTTP 400 on accounts that enforce it).

So when a reply is restored we remember ``sha256(restored text) → exact model text`` and, when
that restored text comes back in a later request, we send the remembered model text instead of
re-tokenizing it.

Safety:
  * Keys are hashes of the restored text; values are the MODEL's (tokenized) text — the file
    holds no value the cloud model didn't already see.
  * Entries carry the vault generation: after the vault expires/clears, old entries (whose
    tokens the new vault can't restore) are ignored.
  * A replayed value is only used if every token in it is still restorable.
  * Bounded LRU, persisted atomically, fail-open (a broken cache = plain re-tokenization).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import OrderedDict

MAX_ENTRIES = 4000
MAX_VALUE_CHARS = 200_000


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class ReplayCache:
    def __init__(self, path: str | None = None, max_entries: int = MAX_ENTRIES) -> None:
        self.path = path
        self.max_entries = max_entries
        self._d: "OrderedDict[str, tuple[str, str]]" = OrderedDict()   # hash -> (generation, model_text)
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("entries", []):
                if isinstance(k, str) and isinstance(v, list) and len(v) == 2:
                    self._d[k] = (str(v[0]), str(v[1]))
        except Exception:
            self._d.clear()                     # a corrupt cache only costs cache hits

    def remember(self, restored: str, model_text: str, generation: str) -> None:
        if not isinstance(restored, str) or not isinstance(model_text, str):
            return
        if restored == model_text or not restored or len(model_text) > MAX_VALUE_CHARS:
            return
        with self._lock:
            self._d[_h(restored)] = (generation, model_text)
            self._d.move_to_end(_h(restored))
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)
            self._dirty = True

    def lookup(self, restored: str, generation: str) -> str | None:
        if not isinstance(restored, str) or not restored:
            return None
        with self._lock:
            hit = self._d.get(_h(restored))
            if hit is None or hit[0] != generation:
                return None
            self._d.move_to_end(_h(restored))
            return hit[1]

    def __len__(self) -> int:
        return len(self._d)

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            with self._lock:
                entries = [[k, list(v)] for k, v in self._d.items()]
                self._dirty = False
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".replay-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"entries": entries}, f, ensure_ascii=False)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except Exception:
            self._dirty = True
