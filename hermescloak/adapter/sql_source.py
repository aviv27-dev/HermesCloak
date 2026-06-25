"""DB-backed EntitySource. The firm-specific gazetteer provider lives HERE (adapter layer),
never in the core. `connect` is a zero-arg callable returning a DBAPI connection, so the
gazetteer can be refreshed by reconnecting."""
from typing import Callable, Iterable


class SqlEntitySource:
    def __init__(self, connect: Callable[[], object], query: str, default_type: str = "לקוח") -> None:
        self._connect = connect
        self._query = query
        self._default_type = default_type

    def names(self) -> Iterable[tuple[str, str]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(self._query)
            for row in cur.fetchall():
                surface = row[0]
                if not surface:
                    continue
                etype = row[1] if len(row) > 1 and row[1] else self._default_type
                yield str(surface), str(etype)
        finally:
            conn.close()


def DbGazetteerSource(connect: Callable[[], object], query: str | None = None) -> SqlEntitySource:
    """Convenience wrapper: build a gazetteer EntitySource from your own CRM/database. The query is
    deployment-specific (the default is a placeholder) and should return (name, type) rows."""
    return SqlEntitySource(connect, query or "SELECT name, 'לקוח' FROM parties", default_type="לקוח")
