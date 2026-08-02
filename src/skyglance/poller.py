"""Background polling, so history accumulates without you asking.

Without this, the sightings log only ever contains what you happened to ask about, and
"have I seen this tail before?" can only answer for aircraft you already looked at.

Two things make a background loop inside an MCP server safe to ship:

1. **A single-instance lock.** Several Claude sessions means several server processes.
   Without a lock they would all poll, multiplying load on volunteer services that owe
   us nothing. Only one process polls; the others still read the same database.
2. **A conservative interval.** 60 seconds is one request per source per minute against
   a published limit of about one per second.

Honest limitation: a pass is inferred from snapshots taken a minute apart, so a fast
low pass between two polls is missed entirely. poller_status() reports the interval so
this is visible rather than hidden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .feeds import FeedClient
from .geometry import describe
from .store import Observation, Store

log = logging.getLogger("skyglance.poller")

DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 30.0


def lock_path() -> Path:
    root = Path(os.environ.get("SKYGLANCE_HOME_DIR", Path.home() / ".skyglance"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "poller.lock"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class SingleInstanceLock:
    """A pid-stamped lockfile that a dead holder cannot keep forever.

    Written with O_EXCL so the create-or-fail decision is atomic. If the file exists but
    names a pid that is gone (hard kill, crash), it is reclaimed — otherwise polling
    would stop permanently after one bad exit, which is a worse failure than a race.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or lock_path()
        self.acquired = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "started_at": time.time()}, f)
                self.acquired = True
                return True
            except FileExistsError:
                if not self._reclaim_if_stale():
                    return False
        return False

    def _reclaim_if_stale(self) -> bool:
        try:
            data = json.loads(self.path.read_text())
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable or truncated lock — a crash mid-write. Safe to clear.
            self.path.unlink(missing_ok=True)
            return True
        if _process_alive(pid):
            return False
        log.info("reclaiming stale poller lock from dead pid %s", pid)
        self.path.unlink(missing_ok=True)
        return True

    def holder(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


class Poller:
    def __init__(self, feeds: FeedClient, store: Store, lat: float, lon: float,
                 radius_nm: int = 60, interval_s: float = DEFAULT_INTERVAL_S,
                 lock: Optional[SingleInstanceLock] = None) -> None:
        self.feeds = feeds
        self.store = store
        self.lat = lat
        self.lon = lon
        self.radius_nm = radius_nm
        self.interval_s = max(MIN_INTERVAL_S, interval_s)
        self.lock = lock or SingleInstanceLock()
        self._task: Optional[asyncio.Task] = None
        self.started_at: Optional[float] = None
        self.polls = 0
        self.last_poll_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.passes_opened = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return True
        if not self.lock.acquire():
            log.info("another skyglance process is already polling; not starting a second")
            return False
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.lock.release()

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 — a bad poll must not kill the loop
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("poll failed: %s", self.last_error)
                await asyncio.sleep(self.interval_s)
        finally:
            self.lock.release()

    async def poll_once(self) -> dict:
        snapshot = await self.feeds.snapshot(self.lat, self.lon, self.radius_nm)
        self.polls += 1
        self.last_poll_at = time.time()

        # A stale snapshot is the same aircraft again; folding it in would inflate
        # observation counts and paper over an outage.
        if snapshot.stale or snapshot.error:
            self.last_error = snapshot.error or "stale snapshot"
            return {"skipped": True, "reason": self.last_error}

        self.last_error = None
        observations = []
        for a in snapshot.aircraft:
            if a.on_ground or a.lat is None or a.lon is None:
                continue
            s = describe(self.lat, self.lon, a.lat, a.lon, a.altitude_ft)
            observations.append(Observation(
                hex=a.hex, callsign=a.callsign, registration=a.registration,
                type_code=a.type_code, ground_km=s.ground_km,
                altitude_ft=a.altitude_ft, elevation_deg=s.elevation_deg,
                bearing_deg=s.bearing_deg, is_military=a.is_military))

        result = self.store.record_snapshot(observations)
        self.passes_opened += result["new_passes"]
        return result

    def status(self) -> dict:
        holder = self.lock.holder()
        return {
            "running": self.running,
            "this_process_is_polling": self.lock.acquired,
            "lock_holder": holder,
            "interval_seconds": self.interval_s,
            "home": {"lat": self.lat, "lon": self.lon, "radius_nm": self.radius_nm},
            "started_at": self.started_at,
            "polls_completed": self.polls,
            "last_poll_at": self.last_poll_at,
            "new_passes_this_session": self.passes_opened,
            "last_error": self.last_error,
            "caveat": (f"Passes are inferred from snapshots {self.interval_s:.0f}s apart, "
                       "so a fast low pass between polls is not recorded."),
        }
