"""One snapshot of every aircraft the network can see, shared and cached.

adsb.lol's point query has no radius cap in practice: a 12,000 nm radius returns roughly
6,000 aircraft worldwide in about 1.3 seconds. That single fact is what makes fleet views
("every British Airways flight airborne"), global counts, and cross-airport comparisons
possible without a commercial feed — none of which any per-circle query can answer.

It is also a ~3.7 MB response from volunteers paying their own bandwidth, so this module
exists to make sure we ask for it rarely and share the result:

  - **One fetch per MIN_SWEEP_INTERVAL_S at most**, process-wide.
  - **Concurrent callers share one in-flight request** rather than each starting their
    own. This is the single-flight rule the reference implementation learned the hard
    way, where six simultaneous callers produced five empty maps.
  - Callers get a cached snapshot in between, with its age attached so staleness is
    visible rather than hidden.

Never reach for a sweep to answer something a 60 nm circle can answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .feeds import Aircraft, FeedClient

log = logging.getLogger("skyglance.world")

#: Radius large enough to cover the planet from any centre.
GLOBAL_RADIUS_NM = 12000

#: Minimum seconds between real fetches. Roughly the load of one person with a map open.
MIN_SWEEP_INTERVAL_S = 60.0

#: Beyond this a cached sweep is too old to answer "right now" questions with.
MAX_SERVE_AGE_S = 300.0


@dataclass
class WorldSnapshot:
    aircraft: list[Aircraft]
    captured_at: float
    error: Optional[str] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.captured_at


class WorldView:
    def __init__(self, feeds: FeedClient) -> None:
        self._feeds = feeds
        self._cached: Optional[WorldSnapshot] = None
        self._lock = asyncio.Lock()
        self._inflight: Optional[asyncio.Task] = None

    def cached(self) -> Optional[WorldSnapshot]:
        return self._cached

    async def sweep(self, force: bool = False) -> WorldSnapshot:
        """A global snapshot, fetched at most once per MIN_SWEEP_INTERVAL_S."""
        now = time.time()
        cached = self._cached
        if cached and not force and (now - cached.captured_at) < MIN_SWEEP_INTERVAL_S:
            return cached

        async with self._lock:
            # Re-check: another caller may have refreshed while we waited for the lock.
            cached = self._cached
            now = time.time()
            if cached and not force and (now - cached.captured_at) < MIN_SWEEP_INTERVAL_S:
                return cached

            result = await self._feeds.global_query(
                f"point/0/0/{GLOBAL_RADIUS_NM}")
            if result.error:
                if cached and cached.age_seconds < MAX_SERVE_AGE_S:
                    log.info("global sweep failed, serving cache: %s", result.error)
                    return cached
                return WorldSnapshot([], time.time(), error=result.error)

            snapshot = WorldSnapshot(
                [a for a in result.aircraft if a.lat is not None and a.lon is not None],
                time.time())
            self._cached = snapshot
            log.info("global sweep: %d aircraft", len(snapshot.aircraft))
            return snapshot


def callsign_prefix(aircraft: Aircraft) -> Optional[str]:
    """The ICAO airline code at the front of a callsign: BAW117 -> BAW.

    Airline callsigns are three letters followed by a flight identifier. General
    aviation uses the registration as the callsign (N520JB), which has no such prefix,
    so anything that doesn't match the shape returns None rather than a bad guess.
    """
    cs = (aircraft.callsign or "").strip().upper()
    if len(cs) < 4:
        return None
    head = cs[:3]
    if not head.isalpha():
        return None
    # The remainder of an airline callsign always begins with a digit.
    return head if cs[3].isdigit() else None
