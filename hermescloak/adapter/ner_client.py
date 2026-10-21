"""Adapter-side client for the shared NER service.

FAIL-SOFT by design: if the service is unreachable, disabled, or errors, recognize() returns
[] so NER simply contributes nothing — an agent is NEVER broken by NER being off. This is the
"easy disconnect": stop the service (or flip the control file to 'off') and NER vanishes with
no agent restart and no error.

The model is freed from RAM via the service's /unload (or by stopping the service)."""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from hermescloak.service import auth
from hermescloak.span import Span

# no proxy (a corporate proxy must never see unmasked text) and no redirects
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      type("_NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                           {"redirect_request": lambda *a, **k: None}))


class NerServiceRecognizer:
    # CIRCUIT BREAKER: a hung/down service used to cost `timeout` seconds on EVERY new message.
    # After a failure NER is skipped for `cooldown` seconds, then one probe is let through.
    def __init__(self, base_url: str = "http://127.0.0.1:8011",
                 timeout: float = 5.0, control_file: str | None = None,
                 cooldown: float = 30.0, on_state_change=None, token: str | None = None,
                 on_auth_failed=None) -> None:
        self._url = base_url.rstrip("/")
        self._token = token
        self._on_auth_failed = on_auth_failed
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
            token = self._token or auth.read_token()
            if not token:
                raise PermissionError("no-token")
            # the service must prove it knows the token BEFORE it gets any text: a process squatting
            # on the port while the service is down learns nothing
            nonce = secrets.token_hex(16)
            try:
                with _OPENER.open(self._url + "/hello?nonce=" + nonce, timeout=self._timeout) as resp:
                    got = json.loads(resp.read().decode("utf-8")).get("proof", "")
            except urllib.error.HTTPError as e:      # 404: a pre-0.3 service that has no /hello
                raise PermissionError("old-service" if e.code == 404 else f"http-{e.code}") from e
            if not auth.same(got, auth.proof(token, nonce)):
                raise PermissionError("bad-proof")
            req = urllib.request.Request(
                self._url + "/recognize",
                data=json.dumps({"text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json", auth.HEADER: token},
            )
            with _OPENER.open(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            spans = [Span(s["start"], s["end"], s["entity_type"], s["text"])
                     for s in data.get("spans", [])]
            self._set_up(True)
            return spans
        except Exception as exc:
            if isinstance(exc, PermissionError) and self._on_auth_failed is not None:
                try:                              # not "down": NER is OFF until this is fixed — say so
                    self._on_auth_failed(str(exc))
                except Exception:
                    pass
            with self._lock:
                self._open_until = time.monotonic() + self._cooldown
            self._set_up(False)
            return []  # fail-soft: NER off / unavailable -> no spans, agent unaffected
