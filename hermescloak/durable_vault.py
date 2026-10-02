"""Durable, per-agent Vault — survives restarts, crashes, corruption and multiple processes.

The in-memory Vault loses its token<->real map when the gateway restarts; any token issued
before then becomes unrestorable and leaks through (observed in the field: ⟦מזהה_6⟧ embedded
in a generated document). DurableVault persists the map and keeps it consistent:

  * WRITE-THROUGH, O(1): a new mapping is appended (fsync'd) to ``<file>.journal`` before its
    token is returned, so a crash can never leave a token in flight that no file can restore;
    the snapshot is compacted every ``COMPACT_EVERY`` entries (rewriting the whole file per new
    value was O(n²): 200 new names on a 20k-entry vault took 4 s).
  * CROSS-PROCESS: minting happens under an exclusive file lock after re-reading the file, so
    the gateway, cron jobs and CLI sessions sharing one HERMES_HOME never issue the same token
    for different values; a token minted by another process is picked up on demand.
  * CRASH/CORRUPTION SAFE: atomic replace, previous version kept as ``.bak``; an unreadable
    file is quarantined (``.corrupt-<ts>``) and the backup is loaded instead of starting empty.
  * ENCRYPTION AT REST (optional): with a Fernet key (``cryptography`` package) the file is
    encrypted. Keep the key OUTSIDE the cloak dir. If the file is encrypted and no usable key
    is available the vault goes memory-only and NEVER overwrites the encrypted file.
  * GENERATION: a random id per vault lifetime; caches keyed to tokens (the replay cache)
    discard entries from an older generation.
  * TTL: the file expires ``ttl_seconds`` after its last change; expired files (and their
    backups) are swept.

The file IS a PII store. It lives on the local machine, same trust boundary as the agent's own
conversation DB: 0600 + TTL + optional encryption.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from hermescloak._filelock import FileLock
from hermescloak.vault import Vault

DEFAULT_TTL_SECONDS = 24 * 3600
MAGIC = b"HCVAULT-FERNET-1\n"
COMPACT_EVERY = 256


def _now() -> float:
    return time.time()


class VaultLocked(Exception):
    """The vault file is encrypted and no usable key is available."""


def _fernet(key):
    if not key:
        return None
    from cryptography.fernet import Fernet   # optional dependency ([crypto] extra)
    return Fernet(key if isinstance(key, bytes) else key.encode("ascii"))


def generate_key() -> str:
    """A new Fernet key (urlsafe base64, 32 bytes) — store it OUTSIDE the cloak dir."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def read_payload(path: str, key=None) -> dict:
    """Read + (if needed) decrypt one vault file. Raises VaultLocked / ValueError / OSError."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(MAGIC):
        if not key:
            raise VaultLocked(path)
        try:
            f = _fernet(key)
        except ImportError as exc:
            raise VaultLocked(f"{path}: cryptography not installed") from exc
        try:
            raw = f.decrypt(raw[len(MAGIC):])
        except Exception as exc:  # InvalidToken (wrong key) or truncated ciphertext
            raise VaultLocked(f"{path}: cannot decrypt ({type(exc).__name__})") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("vault payload is not an object")
    return data


def _encrypt_line(key, obj: dict) -> bytes:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if key:
        return b"E:" + _fernet(key).encrypt(raw) + b"\n"
    return raw + b"\n"


def _decrypt_line(key, line: bytes) -> dict:
    if line.startswith(b"E:"):
        if not key:
            raise VaultLocked("journal")
        line = _fernet(key).decrypt(line[2:])
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict) or "t" not in obj or "r" not in obj:
        raise ValueError("bad journal entry")
    return obj


def _last_byte(fd: int, path: str, size: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, 1, size - 1)
    with open(path, "rb") as f:                       # Windows: no pread
        f.seek(size - 1)
        return f.read(1)


def read_journal(path: str, key=None, offset: int = 0) -> tuple[list[dict], int, int]:
    """(entries, new_offset, bad_lines) from ``path`` starting at byte ``offset``. Only complete
    (newline-terminated) lines are consumed; a torn last line (crash mid-append) is left for later
    and an undecodable line is skipped and counted."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset, 0
    entries, bad, consumed = [], 0, 0
    for line in data.split(b"\n")[:-1]:              # last element = incomplete tail (or b"")
        consumed += len(line) + 1
        if not line.strip():
            continue
        try:
            entries.append(_decrypt_line(key, line))
        except VaultLocked:
            raise
        except Exception:
            bad += 1
    return entries, offset + consumed, bad


class DurableVault(Vault):
    def __init__(self, path: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, key=None) -> None:
        super().__init__()
        self.path = str(path)
        self.journal_path = self.path + ".journal"
        self.ttl_seconds = ttl_seconds
        self._key = key
        self._dirty = False
        self._seen: tuple | None = None       # (mtime_ns, size) of the snapshot we merged
        self._joff = 0                         # bytes of the journal already merged
        self._jlines = 0                       # journal entries since the last compaction
        self._last_sync = 0.0
        self.generation: str = ""
        self.memory_only = False               # set when the file cannot be safely written
        self.conflicts = 0                     # token clashes found while merging other writers
        self.events: list[str] = []            # load/save incidents for the adapter's audit log
        if key:
            try:
                _fernet(key)
            except ImportError:
                self.memory_only = True
                self.events.append("vault_crypto_unavailable")
            except Exception:
                self.memory_only = True
                self.events.append("vault_key_invalid")
        if not self.memory_only:
            # every disk read happens under the cross-process lock: an unlocked read racing a
            # compaction could see an old snapshot + an already-emptied journal, believe it is
            # current, and later mint a token number another process already used
            with FileLock(self.path + ".lock"):
                self._load()
        else:
            self._load()
        if not self.generation:
            self.generation = uuid.uuid4().hex

    # ---------------------------------------------------------------- loading / merging
    @staticmethod
    def _stat_of(path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _stat(self):
        return self._stat_of(self.path)

    def _last_change(self) -> float | None:
        ts = []
        for p in (self.path, self.journal_path):
            st = self._stat_of(p)
            if st is not None and (p == self.path or st[1] > 0):   # an EMPTY journal is no activity
                ts.append(st[0] / 1e9)
        return max(ts) if ts else None

    def _load(self) -> None:
        paths = (self.path, self.path + ".bak", self.journal_path)
        if not any(os.path.exists(p) for p in paths):
            return
        last = self._last_change()
        if self.ttl_seconds and last is not None and (_now() - last) > self.ttl_seconds:
            self._delete_file()                  # expired → start fresh (new generation)
            return
        for candidate in (self.path, self.path + ".bak"):
            if not os.path.exists(candidate):
                continue
            try:
                data = read_payload(candidate, self._key)
            except VaultLocked:
                self.memory_only = True          # never overwrite what we cannot read
                self.events.append("vault_locked")
                return
            except Exception:
                if candidate == self.path:       # quarantine, then try the backup
                    try:
                        os.replace(self.path, f"{self.path}.corrupt-{int(_now())}")
                    except OSError:
                        pass
                    self.events.append("vault_corrupt_quarantined")
                continue
            self._merge(data.get("token_to_real") or {}, data.get("counters") or {}, data.get("generation"))
            if candidate != self.path:
                self.events.append("vault_restored_from_backup")
            self._seen = self._stat()
            break
        try:
            self._read_journal()
        except VaultLocked:
            self.memory_only = True
            self.events.append("vault_locked")
            return
        if "vault_restored_from_backup" in self.events and not self.memory_only:
            self._compact()                      # re-establish the main file (lock already held)

    def _merge(self, t2r: dict, counters: dict | None = None, generation=None) -> None:
        """Adopt another writer's mappings without ever breaking our own."""
        with self._lock:
            for tok, real in t2r.items():
                mine = self._token_to_real.get(tok)
                if mine is None:
                    self._token_to_real[tok] = real                      # restorable everywhere
                    self._real_to_token.setdefault(real, tok)
                elif mine != real:
                    self.conflicts += 1                                   # keep ours; count it
            for k, v in (counters or {}).items():
                try:
                    self._counters[k] = max(int(v), self._counters.get(k, 0))
                except (TypeError, ValueError):
                    pass
            if not self.generation and isinstance(generation, str):
                self.generation = generation

    def _read_journal(self) -> None:
        if self._joff > (self._stat_of(self.journal_path) or (0, 0))[1]:
            self._joff = 0                        # compacted (truncated) by another process
        entries, self._joff, bad = read_journal(self.journal_path, self._key, self._joff)
        for e in entries:
            self._merge({e["t"]: e["r"]}, {e.get("k", ""): e.get("n", 0)} if e.get("k") else None,
                        e.get("g"))
        self._jlines += len(entries)
        if bad:
            self.events.append("vault_journal_lines_skipped")

    def sync(self, force: bool = False) -> bool:
        """Merge whatever other processes wrote since we last looked (one or two stats when idle)."""
        if self.memory_only:
            return False
        with FileLock(self.path + ".lock"):
            return self._sync_locked(force)

    def _sync_locked(self, force: bool = False) -> bool:
        changed = False
        st = self._stat()
        if st is not None and (st != self._seen or force):
            try:
                data = read_payload(self.path, self._key)
                self._merge(data.get("token_to_real") or {}, data.get("counters") or {},
                            data.get("generation"))
                self._seen = st
                self._joff = 0                    # a new snapshot came with a fresh journal
                changed = True
            except Exception:
                pass
        jst = self._stat_of(self.journal_path)
        if jst is not None and jst[1] != self._joff:
            before = len(self._token_to_real)
            try:
                self._read_journal()
            except VaultLocked:
                return changed
            changed = changed or len(self._token_to_real) != before
        return changed

    # ---------------------------------------------------------------- writing
    def _payload_bytes(self) -> bytes:
        with self._lock:
            payload = {
                "ts": _now(),
                "generation": self.generation,
                "real_to_token": dict(self._real_to_token),
                "token_to_real": dict(self._token_to_real),
                "counters": dict(self._counters),
            }
            self._dirty = False                   # cleared under the lock: a racing mint re-dirties
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self._key:
            raw = MAGIC + _fernet(self._key).encrypt(raw)
        return raw

    def _compact(self) -> None:
        """Write a full snapshot (atomic, keeps .bak) and empty the journal. Caller holds the lock."""
        if self.memory_only:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            raw = self._payload_bytes()
            # the SAME snapshot is written twice (backup first, then main), each atomically: a
            # crash or corruption of either copy leaves the other complete, and the journal is
            # only emptied after both are on disk
            for target in (self.path + ".bak", self.path):
                fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".vault-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                        f.flush()
                        os.fsync(f.fileno())
                    try:
                        os.chmod(tmp, 0o600)
                    except OSError:
                        pass                      # best-effort (e.g. Windows)
                    os.replace(tmp, target)       # atomic
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            self._seen = self._stat()
            with open(self.journal_path, "wb"):   # snapshot holds everything now
                pass
            self._joff = 0
            self._jlines = 0
        except Exception:
            self._dirty = True                    # retry later; never raise into the agent
            self.events.append("vault_write_failed")

    def _append(self, token: str, real: str, entity_type: str, n: int) -> None:
        """Durably record one new mapping (O(1)). Caller holds the lock and has synced."""
        try:
            d = os.path.dirname(self.journal_path)
            if d:
                os.makedirs(d, exist_ok=True)
            line = _encrypt_line(self._key, {"t": token, "r": real, "k": entity_type, "n": n,
                                             "g": self.generation})
            fd = os.open(self.journal_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size and _last_byte(fd, self.journal_path, size) != b"\n":
                    # a crash left a torn last line: terminate it (it becomes one skipped bad
                    # line) so this entry is not glued onto it and lost too
                    os.write(fd, b"\n")
                    self._joff = max(self._joff, size + 1)
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._joff += len(line)
            self._jlines += 1
            if self._jlines >= COMPACT_EVERY or not os.path.exists(self.path):
                self._compact()
        except Exception:
            self._dirty = True
            self.events.append("vault_write_failed")

    def save(self) -> None:
        """Compact if a journal append failed earlier (retry) — the normal path is already durable."""
        if not self._dirty or self.memory_only:
            return
        with FileLock(self.path + ".lock"):
            self._sync_locked()
            self._compact()

    def _delete_file(self) -> None:
        for p in (self.path, self.path + ".bak", self.journal_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def clear(self) -> None:
        """Drop the mapping and delete the file (call at session/task end)."""
        with self._lock:
            self._real_to_token, self._token_to_real, self._counters = {}, {}, {}
            self._dirty = False
            self.generation = uuid.uuid4().hex
        self._joff = self._jlines = 0
        self._seen = None
        self._delete_file()

    # ---------------------------------------------------------------- vault API
    def tokenize(self, real: str, entity_type: str) -> str:
        with self._lock:
            existing = self._real_to_token.get(real)
        if existing is not None:
            return existing
        if self.memory_only:
            return super().tokenize(real, entity_type)
        # new value: mint under the cross-process lock against the freshest state, write through
        with FileLock(self.path + ".lock") as fl:
            if not fl.acquired:
                self.events.append("vault_lock_timeout")   # proceeding unlocked: audit it
            self._sync_locked()
            with self._lock:
                before = len(self._token_to_real)
                token = super().tokenize(real, entity_type)
                minted = len(self._token_to_real) != before
                n = self._counters.get(entity_type, 0)
            if minted:
                self._append(token, real, entity_type, n)
        return token

    def restore_token(self, token: str) -> str | None:
        real = super().restore_token(token)
        if real is None and not self.memory_only and _now() - self._last_sync > 0.5:
            self._last_sync = _now()               # maybe another process minted it
            if self.sync():
                real = super().restore_token(token)
        return real

    # ---------------------------------------------------------------- maintenance
    @staticmethod
    def path_for(directory: str, session_id: str) -> str:
        h = hashlib.sha256((session_id or "default").encode("utf-8")).hexdigest()[:16]
        return os.path.join(directory, f"{h}.json")

    @staticmethod
    def sweep_expired(directory: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Delete vault files (snapshot, journal, .bak, quarantined copies) older than TTL —
        a vault is expired only when BOTH its snapshot and journal are older than the TTL."""
        removed = 0
        try:
            p = Path(directory)
            if not p.is_dir():
                return 0
            cutoff = _now() - ttl_seconds
            for f in p.glob("*.json"):
                group = [f, Path(str(f) + ".journal"), Path(str(f) + ".bak")]
                times = [g.stat().st_mtime for g in group[:2]
                         if g.exists() and (g == f or g.stat().st_size > 0)]
                if times and max(times) < cutoff:
                    for g in group:
                        try:
                            g.unlink()
                            removed += 1
                        except OSError:
                            pass
            for f in p.glob("*.json.corrupt-*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        except Exception:
            pass
        return removed
