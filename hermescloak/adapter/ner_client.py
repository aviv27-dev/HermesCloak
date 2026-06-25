"""Adapter-side client for the shared NER service.

FAIL-SOFT by design: if the service is unreachable, disabled, or errors, recognize() returns
[] so NER simply contributes nothing — an agent is NEVER broken by NER being off. This is the
"easy disconnect": stop the service (or flip the control file to 'off') and NER vanishes with
no agent restart and no error.

The model is freed from RAM via the service's /unload (or by stopping the service)."""
import json
import os
import urllib.request
from hermescloak.span import Span


class NerServiceRecognizer:
    def __init__(self, base_url: str = "http://127.0.0.1:8011",
                 timeout: float = 5.0, control_file: str | None = None) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout
        self._control_file = control_file  # optional live on/off without restart

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
        try:
            req = urllib.request.Request(
                self._url + "/recognize",
                data=json.dumps({"text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [Span(s["start"], s["end"], s["entity_type"], s["text"])
                    for s in data.get("spans", [])]
        except Exception:
            return []  # fail-soft: NER off / unavailable -> no spans, agent unaffected
