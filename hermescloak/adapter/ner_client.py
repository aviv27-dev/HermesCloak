"""Adapter-side client for the shared NER service.

FAIL-SOFT by design: if the service is unreachable, disabled, or errors, recognize() returns
[] so NER simply contributes nothing — an agent is NEVER broken by NER being off. This is the
"easy disconnect": stop the service (or flip the control file to 'off') and NER vanishes with
no agent restart and no error.

The model is freed from RAM via the service's /unload (or by stopping the service)."""
import json
import os
import threading
import time
import urllib.request
from hermescloak.span import Span


class NerServiceRecognizer:
    # CIRCUIT BREAKER: a hung/down service used to cost `timeout` seconds on EVERY new message.
    # After a failure NER is skipped for `cooldown` seconds, then one probe is let through.
    def __init__(self, base_url: str = "http://127.0.0.1:8011",
                 timeout: float = 5.0, control_file: str | None = None,
                 cooldown: float = 30.0, on_state_change=None) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout
        self._control_file = control_file  # optional live on/off without restart
        self._cooldown = cooldown
        self._open_until = 0.0
        self._up = True
        self._on_state_change = on_state_change
        self._lock = threading.Lock()

    def _set_up(self, up: bool) -> None:
        if up != self._up:
            self._up = up
            if self._on_state_change is not None:
                try:
                    self._on_state_change(up)
                except Exception:
                    pass

    def enabled(self) -> bool:
        if self._control_file and os.path.exists(self._control_file):
            try:
                with open(self._control_file, encoding="utf-8") as fh:
                    return fh.read().strip().lower() != "off"
            except Exception:
                return True
        return True

    def recognize(self, text: str) -> list[Span]:
        if not self.enabled():
            return []
        with self._lock:
            if time.monotonic() < self._open_until:
                return []            # breaker open: skip NER instead of waiting on a dead service
        try:
            req = urllib.request.Request(
                self._url + "/recognize",
                data=json.dumps({"text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            spans = [Span(s["start"], s["end"], s["entity_type"], s["text"])
                     for s in data.get("spans", [])]
            self._set_up(True)
            return spans
        except Exception:
            with self._lock:
                self._open_until = time.monotonic() + self._cooldown
            self._set_up(False)
            return []  # fail-soft: NER off / unavailable -> no spans, agent unaffected
