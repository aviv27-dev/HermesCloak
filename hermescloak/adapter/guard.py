"""CloakGuard — the fail-open wrapper that the live hermes seams call.

Failure policy (project decision): FAIL-OPEN and SILENT during the turn. If sanitize
cannot run, the ORIGINAL (unfiltered) messages go to the cloud — availability wins — and
an alert is BUFFERED, not raised. After the turn, `flush_alerts()` dispatches the buffered
events to the configured sinks (audit log + deferred user alert)."""
from hermescloak.adapter.alerts import AlertEvent
from hermescloak.adapter.session_registry import SessionRegistry


class CloakGuard:
    def __init__(self, registry: SessionRegistry, alerters: list | None = None) -> None:
        self._registry = registry
        self._alerters = list(alerters or [])
        self._pending: list[AlertEvent] = []

    @property
    def pending(self) -> list[AlertEvent]:
        return list(self._pending)

    def sanitize_outbound(self, messages: list[dict], session_id: str) -> list[dict]:
        try:
            return self._registry.get_or_create(session_id).sanitize_outbound(messages)
        except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
            self._pending.append(AlertEvent("unfiltered_sent", session_id, repr(exc)))
            return messages  # unfiltered, by design

    def restore_inbound(self, response: dict, session_id: str) -> dict:
        try:
            restored, report = self._registry.get_or_create(session_id).restore_inbound(response)
            if report.leftover:
                self._pending.append(
                    AlertEvent("leftover_token", session_id, ",".join(report.leftover)))
            return restored
        except Exception as exc:  # noqa: BLE001
            self._pending.append(AlertEvent("restore_error", session_id, repr(exc)))
            return response

    def flush_alerts(self) -> list[AlertEvent]:
        events, self._pending = self._pending, []
        for ev in events:
            for sink in self._alerters:
                sink.send(ev)
        return events
