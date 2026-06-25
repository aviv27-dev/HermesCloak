import sqlite3
from hermescloak.adapter.sql_source import SqlEntitySource, DbGazetteerSource
from hermescloak.recognizers.gazetteer import GazetteerRecognizer


def _make_db():
    # one shared in-memory DB; connect() returns a new handle each call
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE parties (name TEXT, kind TEXT)")
    conn.executemany("INSERT INTO parties VALUES (?,?)",
                     [("שירה לוי", "לקוח"), ("דני כהן", None), ("", "לקוח")])
    conn.commit()
    return conn


class _Conn:
    """Wraps a shared sqlite connection so .close() is a no-op (keeps the in-memory DB alive)."""
    def __init__(self, real): self._real = real
    def cursor(self): return self._real.cursor()
    def close(self): pass


def test_sql_entity_source_yields_rows_and_default_type():
    db = _make_db()
    src = SqlEntitySource(lambda: _Conn(db), "SELECT name, kind FROM parties")
    rows = list(src.names())
    assert ("שירה לוי", "לקוח") in rows
    assert ("דני כהן", "לקוח") in rows     # NULL kind -> default_type
    assert all(name for name, _ in rows)   # empty name skipped
    assert len(rows) == 2

def test_aglo_os_source_feeds_gazetteer():
    db = _make_db()
    src = DbGazetteerSource(lambda: _Conn(db), "SELECT name, kind FROM parties")
    rec = GazetteerRecognizer(src)
    spans = rec.recognize("פגשתי את שירה לוי")
    assert len(spans) == 1 and spans[0].entity_type == "לקוח"
