"""Maps a hermes session_id to its own Engine (and therefore its own per-session vault).

Reset on a new session so the vault never grows unbounded and tokens never cross sessions."""
from hermescloak.engine import Engine
from hermescloak.entities import EntitySource
from hermescloak.profile import Profile


class SessionRegistry:
    def __init__(self, profile: Profile, entity_source: EntitySource | None = None,
                 extra_recognizers: list | None = None) -> None:
        self._profile = profile
        self._entity_source = entity_source
        self._extra = extra_recognizers
        self._engines: dict[str, Engine] = {}

    def get_or_create(self, session_id: str) -> Engine:
        eng = self._engines.get(session_id)
        if eng is None:
            eng = Engine(self._profile, self._entity_source, self._extra)
            self._engines[session_id] = eng
        return eng

    def reset(self, session_id: str) -> None:
        self._engines.pop(session_id, None)

    def active_sessions(self) -> int:
        return len(self._engines)
