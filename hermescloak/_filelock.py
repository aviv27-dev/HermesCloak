"""Tiny cross-process advisory file lock (POSIX flock / Windows msvcrt), stdlib only.

Used so several processes on one HERMES_HOME (gateway, cron, CLI, subagents) never mint the
same token for different values. If locking is unavailable the lock degrades to a no-op —
in-process consistency is still guaranteed by the vault's own thread lock."""
from __future__ import annotations

import os
import time


class FileLock:
    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fh = None
        self.acquired = False

    def __enter__(self) -> "FileLock":
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self._fh = open(self.path, "a+b")
        except OSError:
            self._fh = None
            return self
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return self
            except (BlockingIOError, PermissionError, OSError):
                if time.monotonic() >= deadline:
                    return self          # degrade: proceed unlocked rather than hang the agent
                time.sleep(0.01)

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None
