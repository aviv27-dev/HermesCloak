"""Alert sinks for the adapter.

Per project decision: DURING a turn the guard is silent (it only buffers events);
AFTER the turn `CloakGuard.flush_alerts()` dispatches them to every configured sink.
A local audit log is always written; a Telegram sink is the deferred user alert."""
import json
from dataclasses import dataclass
from typing import Protocol


@dataclass
class AlertEvent:
    kind: str            # "unfiltered_sent" | "leftover_token" | "restore_error"
    session_id: str
    detail: str = ""


class Alerter(Protocol):
    def send(self, event: AlertEvent) -> None: ...


class CollectingAlerter:
    """In-memory sink (tests / inspection)."""

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.events.append(event)


class FileAuditAlerter:
    """Always-on local audit log (JSONL). Not a user-facing alert."""

    def __init__(self, path: str) -> None:
        self._path = path

    def send(self, event: AlertEvent) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"kind": event.kind, "session_id": event.session_id, "detail": event.detail},
                ensure_ascii=False) + "\n")


class CallbackAlerter:
    """Deferred user alert via any callable (e.g. a Telegram-send closure).

    The callable receives a short human string. Wiring the actual Telegram send
    is the integrator's responsibility and lives outside the offline library."""

    def __init__(self, send_fn) -> None:
        self._send_fn = send_fn

    def send(self, event: AlertEvent) -> None:
        self._send_fn(f"[HermesCloak] {event.kind} (session {event.session_id}): {event.detail}")
