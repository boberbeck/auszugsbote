"""Betriebsdaten (kein Geheimnis): abgelegte Auszüge, Läufe, Protokoll, Web-Login."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS statements (
  id INTEGER PRIMARY KEY,
  bank_id TEXT NOT NULL,
  iban TEXT NOT NULL,
  kind TEXT NOT NULL,             -- pdf | camt
  year INTEGER NOT NULL,
  number INTEGER NOT NULL,        -- Auszugsnummer; bei camt: Monat
  method TEXT,                    -- HKEKP | HKEKA | HKCAZ | demo
  relpath TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  period_start TEXT,
  period_end TEXT,
  fetched REAL NOT NULL,
  run_id INTEGER,
  UNIQUE (bank_id, iban, kind, year, number)
);
CREATE INDEX IF NOT EXISTS statements_sha ON statements(sha256);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL,          -- zeitplan | manuell | test
  started REAL NOT NULL,
  finished REAL,
  status TEXT NOT NULL,           -- laeuft | ok | teilweise | fehler | abgebrochen
  summary TEXT
);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  run_id INTEGER,
  bank_id TEXT,
  level TEXT NOT NULL,            -- info | warn | error
  message TEXT NOT NULL
);
"""


@dataclass
class StatementRow:
    id: int
    bank_id: str
    iban: str
    kind: str
    year: int
    number: int
    method: str | None
    relpath: str
    sha256: str
    size: int
    period_start: str | None
    period_end: str | None
    fetched: float
    run_id: int | None


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- Auszüge --------------------------------------------------------------
    def has_statement(self, bank_id: str, iban: str, kind: str, year: int, number: int) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM statements WHERE bank_id=? AND iban=? AND kind=? AND year=? AND number=?",
                (bank_id, iban, kind, year, number),
            ).fetchone() is not None

    def has_sha(self, sha256: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM statements WHERE sha256=?", (sha256,)).fetchone() is not None

    def add_statement(self, **kw) -> None:
        kw.setdefault("fetched", time.time())
        cols = ",".join(kw)
        with self._lock, self._db() as db:
            db.execute(f"INSERT INTO statements({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))

    def statements(self, limit: int = 200, bank_id: str | None = None, year: int | None = None) -> list[StatementRow]:
        q, args = "SELECT * FROM statements WHERE 1=1", []
        if bank_id:
            q += " AND bank_id=?"
            args.append(bank_id)
        if year:
            q += " AND year=?"
            args.append(year)
        q += " ORDER BY fetched DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._db() as db:
            return [StatementRow(**dict(r)) for r in db.execute(q, args)]

    def statement_count(self, since: float | None = None) -> int:
        with self._db() as db:
            if since:
                return db.execute("SELECT COUNT(*) FROM statements WHERE fetched>=?", (since,)).fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM statements").fetchone()[0]

    def last_statement(self, bank_id: str, iban: str) -> StatementRow | None:
        with self._db() as db:
            r = db.execute(
                "SELECT * FROM statements WHERE bank_id=? AND iban=? ORDER BY year DESC, number DESC LIMIT 1",
                (bank_id, iban),
            ).fetchone()
        return StatementRow(**dict(r)) if r else None

    def years(self) -> list[int]:
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT year FROM statements ORDER BY year DESC")]

    # -- Läufe & Protokoll ------------------------------------------------------
    def start_run(self, trigger: str) -> int:
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT INTO runs(trigger, started, status) VALUES (?, ?, 'laeuft')", (trigger, time.time())
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, summary: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE runs SET finished=?, status=?, summary=? WHERE id=?", (time.time(), status, summary, run_id)
            )

    def runs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._db() as db:
            return list(db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def last_run(self) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()

    def mark_interrupted_runs(self) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE runs SET status='abgebrochen', finished=?, summary='Neustart während des Laufs' "
                "WHERE status='laeuft'", (time.time(),)
            )

    def log(self, level: str, message: str, run_id: int | None = None, bank_id: str | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO log(ts, run_id, bank_id, level, message) VALUES (?, ?, ?, ?, ?)",
                (time.time(), run_id, bank_id, level, message),
            )

    def logs(self, limit: int = 300, run_id: int | None = None) -> list[sqlite3.Row]:
        with self._db() as db:
            if run_id:
                return list(db.execute("SELECT * FROM log WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)))
            return list(db.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)))
