"""Durable, per-session Vault — survives process restart / engine recreation.

The in-memory Vault loses its token<->real map when the gateway restarts or the
per-session engine is recreated; any token issued before then becomes unrestorable
and leaks through (observed in the field: ⟦מזהה_6⟧ embedded in a generated document).
DurableVault persists the map to a per-session file so the SAME session reloads its
full mapping (and keeps token numbering continuous) across restarts.

Lifecycle / safety:
  * One file per session under <dir>/<sha16(session_id)>.json, written atomically, 0600.
  * NOT deleted after each restore (the session continues and later replies still need
    it). Expires by TTL; expired files are swept + deleted. `clear()` deletes on demand
    (use at session end).
  * The file IS a PII store (token<->real). It lives only on the local machine, same
    trust boundary as the agent's own conversation DB. Encrypt-at-rest is an optional
    follow-up; for now rely on 0600 + TTL + clear().
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from hermescloak.vault import Vault

DEFAULT_TTL_SECONDS = 24 * 3600


def _now() -> float:
    return time.time()


class DurableVault(Vault):
    def __init__(self, path: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        super().__init__()
        self.path = str(path)
        self.ttl_seconds = ttl_seconds
        self._dirty = False
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            if self.ttl_seconds and (_now() - os.path.getmtime(self.path)) > self.ttl_seconds:
                self._delete_file()           # expired → start fresh
                return
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._real_to_token = dict(data.get("real_to_token", {}))
            self._token_to_real = dict(data.get("token_to_real", {}))
            self._counters = {k: int(v) for k, v in data.get("counters", {}).items()}
        except Exception:
            # corrupt/unreadable → start empty rather than crash (fail-open spirit)
            self._real_to_token, self._token_to_real, self._counters = {}, {}, {}

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            payload = {
                "ts": _now(),
                "real_to_token": self._real_to_token,
                "token_to_real": self._token_to_real,
                "counters": self._counters,
            }
            fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".vault-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass                      # best-effort (e.g. Windows)
                os.replace(tmp, self.path)    # atomic
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self._dirty = False
        except Exception:
            pass                              # never raise into the agent

    def _delete_file(self) -> None:
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    def clear(self) -> None:
        """Drop the mapping and delete the file (call at session/task end)."""
        self._real_to_token, self._token_to_real, self._counters = {}, {}, {}
        self._dirty = False
        self._delete_file()

    # ---- override the one mutator so new mappings mark the vault dirty ----
    def tokenize(self, real: str, entity_type: str) -> str:
        before = len(self._token_to_real)
        token = super().tokenize(real, entity_type)
        if len(self._token_to_real) != before:
            self._dirty = True
        return token

    # ---- maintenance ----
    @staticmethod
    def path_for(directory: str, session_id: str) -> str:
        h = hashlib.sha256((session_id or "default").encode("utf-8")).hexdigest()[:16]
        return os.path.join(directory, f"{h}.json")

    @staticmethod
    def sweep_expired(directory: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Delete vault files older than TTL. Returns count removed."""
        removed = 0
        try:
            p = Path(directory)
            if not p.is_dir():
                return 0
            cutoff = _now() - ttl_seconds
            for f in p.glob("*.json"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        except Exception:
            pass
        return removed
