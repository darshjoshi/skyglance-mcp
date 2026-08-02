"""Local history — the part no live feed can answer.

Day 1 a live map is magic. Day 3, "a 737 to Chicago" is wallpaper. What makes this
worth opening again is what only *you* know: that you have never seen this tail before,
that this is the lowest anything has ever passed, that a type has never crossed your sky.

That is accumulated here, in SQLite (stdlib, no dependency), at ~/.skyglance/sky.db.

A "pass" is one continuous appearance of an aircraft near you. It closes when the
aircraft is missing from MISSED_POLLS_TO_CLOSE consecutive snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("skyglance.store")

#: An aircraft absent from this many consecutive polls has left; its pass closes.
MISSED_POLLS_TO_CLOSE = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft (
    hex           TEXT PRIMARY KEY,
    registration  TEXT,
    type_code     TEXT,
    type_name     TEXT,
    operator      TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS passes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    hex                TEXT NOT NULL,
    callsign           TEXT,
    registration       TEXT,
    type_code          TEXT,
    started_at         REAL NOT NULL,
    ended_at           REAL,
    closest_km         REAL NOT NULL,
    min_altitude_ft    REAL,
    max_elevation_deg  REAL NOT NULL,
    bearing_at_closest REAL,
    is_military        INTEGER NOT NULL DEFAULT 0,
    observations       INTEGER NOT NULL DEFAULT 1,
    open               INTEGER NOT NULL DEFAULT 1,
    missed_polls       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_passes_hex     ON passes(hex);
CREATE INDEX IF NOT EXISTS idx_passes_started ON passes(started_at);
CREATE INDEX IF NOT EXISTS idx_passes_open    ON passes(open);

CREATE TABLE IF NOT EXISTS enrichment (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def default_db_path() -> Path:
    root = Path(os.environ.get("SKYGLANCE_HOME_DIR", Path.home() / ".skyglance"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "sky.db"


@dataclass
class Observation:
    """One aircraft in one snapshot, already reduced to what history cares about."""
    hex: str
    callsign: Optional[str]
    registration: Optional[str]
    type_code: Optional[str]
    ground_km: float
    altitude_ft: Optional[float]
    elevation_deg: float
    bearing_deg: float
    is_military: bool


class Store:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL so the background poller writing never blocks a tool reading.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ── Enrichment cache ─────────────────────────────────────────────────────

    def get_enrichment(self, kind: str, key: str) -> Optional[dict]:
        """Returns the cached payload, `{}` for a cached miss, or None if not cached.

        The three-way return is the point: a cached miss must be distinguishable from
        never having looked, or unknown aircraft are retried forever.
        """
        row = self._db.execute(
            "SELECT payload FROM enrichment WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_enrichment(self, kind: str, key: str, payload: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO enrichment (kind, key, payload, fetched_at)"
            " VALUES (?,?,?,?)",
            (kind, key, json.dumps(payload), time.time()))
        self._db.commit()

    # ── Passes ───────────────────────────────────────────────────────────────

    def record_snapshot(self, observations: Iterable[Observation],
                        now: Optional[float] = None) -> dict[str, int]:
        """Fold one snapshot into history. Returns counts of what changed."""
        now = now if now is not None else time.time()
        seen: dict[str, Observation] = {o.hex: o for o in observations}
        opened = 0

        open_rows = {r["hex"]: r for r in self._db.execute(
            "SELECT * FROM passes WHERE open=1").fetchall()}

        for hex_id, obs in seen.items():
            self._upsert_aircraft(obs, now)
            row = open_rows.get(hex_id)
            if row is None:
                self._db.execute(
                    "INSERT INTO passes (hex, callsign, registration, type_code,"
                    " started_at, closest_km, min_altitude_ft, max_elevation_deg,"
                    " bearing_at_closest, is_military)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (hex_id, obs.callsign, obs.registration, obs.type_code, now,
                     obs.ground_km, obs.altitude_ft, obs.elevation_deg,
                     obs.bearing_deg, int(obs.is_military)))
                opened += 1
            else:
                closer = obs.ground_km < row["closest_km"]
                self._db.execute(
                    "UPDATE passes SET"
                    "  callsign=COALESCE(?, callsign),"
                    "  registration=COALESCE(?, registration),"
                    "  type_code=COALESCE(?, type_code),"
                    "  closest_km=MIN(closest_km, ?),"
                    "  min_altitude_ft=CASE WHEN ? IS NULL THEN min_altitude_ft"
                    "      ELSE MIN(COALESCE(min_altitude_ft, ?), ?) END,"
                    "  max_elevation_deg=MAX(max_elevation_deg, ?),"
                    "  bearing_at_closest=CASE WHEN ? THEN ? ELSE bearing_at_closest END,"
                    "  observations=observations+1, missed_polls=0"
                    " WHERE id=?",
                    (obs.callsign, obs.registration, obs.type_code,
                     obs.ground_km,
                     obs.altitude_ft, obs.altitude_ft, obs.altitude_ft,
                     obs.elevation_deg,
                     int(closer), obs.bearing_deg,
                     row["id"]))

        # Anything open but not in this snapshot has missed a poll.
        missing = [r for h, r in open_rows.items() if h not in seen]
        for row in missing:
            missed = row["missed_polls"] + 1
            if missed >= MISSED_POLLS_TO_CLOSE:
                self._db.execute("UPDATE passes SET open=0, ended_at=? WHERE id=?",
                                 (now, row["id"]))
            else:
                self._db.execute("UPDATE passes SET missed_polls=? WHERE id=?",
                                 (missed, row["id"]))

        self._db.commit()
        return {"seen": len(seen), "new_passes": opened, "absent": len(missing)}

    def _upsert_aircraft(self, obs: Observation, now: float) -> None:
        self._db.execute(
            "INSERT INTO aircraft (hex, registration, type_code, first_seen, last_seen)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(hex) DO UPDATE SET"
            "   registration=COALESCE(excluded.registration, registration),"
            "   type_code=COALESCE(excluded.type_code, type_code),"
            "   last_seen=excluded.last_seen",
            (obs.hex, obs.registration, obs.type_code, now, now))

    # ── Queries ──────────────────────────────────────────────────────────────

    def history_for(self, *, hex_id: Optional[str] = None,
                    registration: Optional[str] = None, limit: int = 50) -> list[dict]:
        if hex_id:
            rows = self._db.execute(
                "SELECT * FROM passes WHERE hex=? ORDER BY started_at DESC LIMIT ?",
                (hex_id.lower(), limit)).fetchall()
        elif registration:
            rows = self._db.execute(
                "SELECT * FROM passes WHERE UPPER(registration)=? "
                "ORDER BY started_at DESC LIMIT ?",
                (registration.upper(), limit)).fetchall()
        else:
            return []
        return [dict(r) for r in rows]

    def is_first_sighting(self, hex_id: str) -> bool:
        row = self._db.execute("SELECT COUNT(*) c FROM passes WHERE hex=?",
                               (hex_id.lower(),)).fetchone()
        return row["c"] == 0

    def type_seen_before(self, type_code: str) -> int:
        row = self._db.execute("SELECT COUNT(*) c FROM passes WHERE type_code=?",
                               (type_code.upper(),)).fetchone()
        return row["c"]

    def records(self) -> dict[str, Any]:
        def one(sql: str) -> Optional[dict]:
            row = self._db.execute(sql).fetchone()
            return dict(row) if row else None

        busiest = self._db.execute(
            "SELECT DATE(started_at,'unixepoch') d, COUNT(*) c FROM passes"
            " GROUP BY d ORDER BY c DESC LIMIT 1").fetchone()

        return {
            "closest_pass": one("SELECT * FROM passes ORDER BY closest_km ASC LIMIT 1"),
            "lowest_pass": one("SELECT * FROM passes WHERE min_altitude_ft IS NOT NULL"
                               " ORDER BY min_altitude_ft ASC LIMIT 1"),
            "highest_elevation": one("SELECT * FROM passes"
                                     " ORDER BY max_elevation_deg DESC LIMIT 1"),
            "busiest_day": dict(busiest) if busiest else None,
            "total_passes": self._db.execute(
                "SELECT COUNT(*) c FROM passes").fetchone()["c"],
            "distinct_aircraft": self._db.execute(
                "SELECT COUNT(*) c FROM aircraft").fetchone()["c"],
            "distinct_types": self._db.execute(
                "SELECT COUNT(DISTINCT type_code) c FROM passes"
                " WHERE type_code IS NOT NULL").fetchone()["c"],
        }

    def stats(self, since_epoch: Optional[float] = None) -> dict[str, Any]:
        where, params = ("WHERE started_at >= ?", (since_epoch,)) if since_epoch else ("", ())
        total = self._db.execute(
            f"SELECT COUNT(*) c FROM passes {where}", params).fetchone()["c"]
        top_types = [dict(r) for r in self._db.execute(
            f"SELECT type_code, COUNT(*) c FROM passes {where}"
            f"{' AND' if where else ' WHERE'} type_code IS NOT NULL"
            " GROUP BY type_code ORDER BY c DESC LIMIT 10", params).fetchall()]
        by_hour = [dict(r) for r in self._db.execute(
            f"SELECT STRFTIME('%H', started_at, 'unixepoch') h, COUNT(*) c"
            f" FROM passes {where} GROUP BY h ORDER BY c DESC LIMIT 5",
            params).fetchall()]
        military = self._db.execute(
            f"SELECT COUNT(*) c FROM passes {where}"
            f"{' AND' if where else ' WHERE'} is_military=1", params).fetchone()["c"]
        return {"passes": total, "military": military,
                "top_types": top_types, "busiest_hours_utc": by_hour}

    def counts(self) -> dict[str, int]:
        return {
            "passes": self._db.execute("SELECT COUNT(*) c FROM passes").fetchone()["c"],
            "open_passes": self._db.execute(
                "SELECT COUNT(*) c FROM passes WHERE open=1").fetchone()["c"],
            "aircraft": self._db.execute(
                "SELECT COUNT(*) c FROM aircraft").fetchone()["c"],
            "enrichment_cached": self._db.execute(
                "SELECT COUNT(*) c FROM enrichment").fetchone()["c"],
        }
