"""Live aircraft positions, merged from community ADS-B feeds (adsb.lol and adsb.fi).

Ported from app/Sources/OverheadKit/FeedClient.swift and reference/server.mjs in the
skyglance-mac repo. The four decisions that matter, all inherited from that work:

1. Merge, don't fail over. Querying every source every time and unioning the results
   buys both redundancy and more aircraft (measured +17.3% over Newark, 2026-08-02, with
   airplanes.live as a third source; see SOURCES for why it's gone).
2. Circuit breakers. Three strikes opens a source for 30 seconds. These are volunteer
   services with no funding and no SLA; hammering a sick one is rude and pointless.
3. Reserve a rate slot and wait, don't skip. Rate limits are per-source and global, so
   skipping starves a second concurrent query instead of merely delaying it.
4. Stale data beats an error. A 40-second-old aircraft is more useful than a spinner.

OpenSky is deliberately absent, as is airplanes.live since it closed its API. It contributes real coverage but its licence requires a
written agreement for operational use, and this package is redistributed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("skyglance.feeds")

USER_AGENT = "skyglance-mcp/0.2 (+https://github.com/darshjoshi/skyglance-mcp)"

FAILURES_BEFORE_OPEN = 3
BREAKER_COOLDOWN_S = 30.0
MAX_QUEUE_WAIT_S = 4.0
REQUEST_TIMEOUT_S = 8.0


def coarsen(value: float) -> float:
    """Round a coordinate to 2 decimal places — roughly 1.1 km.

    Full precision would hand each volunteer feed operator a rooftop-accurate record of
    where this machine is and when it is awake, for no gain in what the user sees. Only
    the *query centre* is coarsened; bearing and elevation are computed locally from the
    true coordinate, so what you're told to look at stays exact.

    This is called at the last moment before a URL is built, so there is no code path
    that reaches a feed with an exact position. tests/test_feeds.py asserts that.
    """
    return round(value, 2)


@dataclass(frozen=True)
class FeedSource:
    name: str
    minimum_interval: float
    build_url: Callable[[float, float, int], str]
    attribution: str


def _adsb_lol(lat: float, lon: float, radius: int) -> str:
    return f"https://api.adsb.lol/v2/point/{coarsen(lat)}/{coarsen(lon)}/{radius}"


def _adsb_fi(lat: float, lon: float, radius: int) -> str:
    return (f"https://opendata.adsb.fi/api/v2/lat/{coarsen(lat)}"
            f"/lon/{coarsen(lon)}/dist/{radius}")


SOURCES: tuple[FeedSource, ...] = (
    FeedSource("adsb.lol", 1.0, _adsb_lol, "adsb.lol (ODbL 1.0)"),
    FeedSource("adsb.fi", 1.0, _adsb_fi, "adsb.fi"),
)
# airplanes.live was a third source until September 2026, when its API began answering
# every request with HTTP 403 and "please contact us" first. Querying a service that has
# asked to be asked is not on; re-add it only with their agreement.

#: Global endpoints on adsb.lol, which serve the whole world rather than a circle.
#: Verified live 2026-08-02: /mil -> 81, /type/A388 -> 44, /registration/N520JB -> 1.
GLOBAL_BASE = "https://api.adsb.lol/v2"

#: Whole-world queries are far heavier than a 60nm circle, and adsb.lol answers them
#: with HTTP 420 when pushed. Observed in testing: four in quick succession was enough.
GLOBAL_MIN_INTERVAL_S = 1.5


@dataclass
class SourceHealth:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    open_until: float = 0.0
    last_latency: Optional[float] = None
    last_error: Optional[str] = None

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self.open_until


@dataclass
class Aircraft:
    """One aircraft, normalised across the feeds' shared readsb JSON."""
    hex: str
    callsign: Optional[str] = None
    registration: Optional[str] = None
    type_code: Optional[str] = None
    description: Optional[str] = None
    operator: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude_ft: Optional[float] = None
    on_ground: bool = False
    ground_speed_kt: Optional[float] = None
    track_deg: Optional[float] = None
    vertical_rate_fpm: Optional[float] = None
    squawk: Optional[str] = None
    emergency: Optional[str] = None
    seen_pos_s: Optional[float] = None
    category: Optional[str] = None
    db_flags: int = 0
    #: Build year, carried by some feeds for some aircraft. FR24 charges for this
    #: under "aircraft age"; it is present here for free, just sparsely populated.
    year: Optional[str] = None
    sources: list[str] = field(default_factory=list)

    @property
    def is_military(self) -> bool:
        return bool(self.db_flags & 1)

    @property
    def has_emergency(self) -> bool:
        if self.emergency and self.emergency != "none":
            return True
        return self.squawk in ("7500", "7600", "7700")


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def parse_aircraft(raw: dict, source: str) -> Optional[Aircraft]:
    """Normalise one feed record. Returns None if it has no usable identity."""
    hex_id = _clean(raw.get("hex"))
    if not hex_id:
        return None

    # alt_baro is a number in flight and the literal string "ground" on the ground.
    # Decoding it as a float fails on the ground — the classic ADS-B parsing bug.
    alt_raw = raw.get("alt_baro")
    on_ground = alt_raw == "ground"
    altitude = None if on_ground else (alt_raw if isinstance(alt_raw, (int, float)) else None)

    return Aircraft(
        hex=hex_id.lower(),
        callsign=_clean(raw.get("flight")),
        registration=_clean(raw.get("r")),
        type_code=_clean(raw.get("t")),
        description=_clean(raw.get("desc")),
        operator=_clean(raw.get("ownOp")),
        lat=raw.get("lat"),
        lon=raw.get("lon"),
        altitude_ft=float(altitude) if altitude is not None else (0.0 if on_ground else None),
        on_ground=on_ground,
        ground_speed_kt=raw.get("gs"),
        track_deg=raw.get("track"),
        vertical_rate_fpm=raw.get("baro_rate"),
        squawk=_clean(raw.get("squawk")),
        emergency=_clean(raw.get("emergency")),
        seen_pos_s=raw.get("seen_pos"),
        category=_clean(raw.get("category")),
        db_flags=raw.get("dbFlags") or 0,
        year=_clean(str(raw["year"])) if raw.get("year") else None,
        sources=[source],
    )


@dataclass
class GlobalResult:
    """A whole-world query. `error` set means we could not ask, not that nothing flew."""
    aircraft: list[Aircraft]
    error: Optional[str] = None


@dataclass
class Snapshot:
    aircraft: list[Aircraft]
    captured_at: float
    contributing_sources: list[str]
    degraded: bool
    stale: bool
    error: Optional[str] = None

    @property
    def attributions(self) -> list[str]:
        by_name = {s.name: s.attribution for s in SOURCES}
        return [by_name[n] for n in self.contributing_sources if n in by_name]


class FeedClient:
    """Queries every source in parallel and unions the results."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        self._health = {s.name: SourceHealth() for s in SOURCES}
        self._next_slot: dict[str, float] = {}
        self._slot_lock = asyncio.Lock()
        self._last_good: Optional[Snapshot] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    def health_report(self) -> dict[str, SourceHealth]:
        return dict(self._health)

    async def snapshot(self, lat: float, lon: float, radius_nm: int = 60) -> Snapshot:
        results = await asyncio.gather(
            *(self._fetch(s, lat, lon, radius_nm) for s in SOURCES)
        )
        collected = [r for r in results if r is not None]

        if not collected:
            if self._last_good is not None:
                prior = self._last_good
                return Snapshot(prior.aircraft, prior.captured_at,
                                prior.contributing_sources, degraded=True, stale=True)
            # count 0 with no error is indistinguishable from "empty sky" — a real bug
            # found by fault injection in the reference implementation.
            return Snapshot([], time.time(), [], degraded=True, stale=False,
                            error="all sources unavailable")

        merged = self._merge(collected)
        snap = Snapshot(
            aircraft=merged,
            captured_at=time.time(),
            contributing_sources=sorted(name for name, _ in collected),
            degraded=len(collected) < 2,
            stale=False,
        )
        self._last_good = snap
        return snap

    @staticmethod
    def _merge(collected: list[tuple[str, list[Aircraft]]]) -> list[Aircraft]:
        """Union by ICAO hex. Freshest position wins; missing fields backfilled."""
        best: dict[str, Aircraft] = {}
        for name, aircraft in collected:
            for a in aircraft:
                if a.lat is None or a.lon is None:
                    continue
                incumbent = best.get(a.hex)
                if incumbent is None:
                    best[a.hex] = a
                    continue
                seen = set(incumbent.sources) | {name}
                incumbent_age = incumbent.seen_pos_s if incumbent.seen_pos_s is not None else float("inf")
                challenger_age = a.seen_pos_s if a.seen_pos_s is not None else float("inf")
                winner = a if challenger_age < incumbent_age else incumbent
                loser = incumbent if winner is a else a
                # Backfill: one feed often carries desc/ownOp that another omits.
                for attr in ("callsign", "registration", "type_code", "description",
                             "operator", "squawk", "category", "year"):
                    if getattr(winner, attr) is None and getattr(loser, attr) is not None:
                        setattr(winner, attr, getattr(loser, attr))
                if not winner.db_flags and loser.db_flags:
                    winner.db_flags = loser.db_flags
                winner.sources = sorted(seen)
                best[a.hex] = winner
        return list(best.values())

    async def _reserve_slot(self, source_name: str, minimum_interval: float) -> bool:
        """Wait for this source's next allowed request slot.

        Reserve and wait rather than skip, or a second concurrent query gets starved
        rather than merely delayed. Returns False if the queue is longer than we're
        willing to wait, in which case the caller must treat the source as unavailable
        rather than as having returned nothing.
        """
        async with self._slot_lock:
            now = time.monotonic()
            slot = max(self._next_slot.get(source_name, now), now)
            wait = slot - now
            if wait > MAX_QUEUE_WAIT_S:
                return False
            self._next_slot[source_name] = slot + minimum_interval
        if wait > 0:
            await asyncio.sleep(wait)
        return True

    async def _fetch(self, source: FeedSource, lat: float, lon: float,
                     radius: int) -> Optional[tuple[str, list[Aircraft]]]:
        if self._health[source.name].circuit_open:
            return None
        if not await self._reserve_slot(source.name, source.minimum_interval):
            return None
        return await self._get_aircraft(source.name, source.build_url(lat, lon, radius))

    async def _get_aircraft(self, source_name: str,
                            url: str) -> Optional[tuple[str, list[Aircraft]]]:
        state = self._health[source_name]
        started = time.monotonic()
        try:
            response = await self._client.get(url)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            # Under burst these services can return an HTML error page with a 200, so a
            # decode failure here is a normal, expected outcome rather than a surprise.
            payload = response.json()
            raw = payload.get("ac") or payload.get("aircraft") or []
            parsed = [a for a in (parse_aircraft(r, source_name) for r in raw) if a]
            state.successes += 1
            state.consecutive_failures = 0
            state.last_latency = time.monotonic() - started
            state.last_error = None
            return source_name, parsed
        except Exception as exc:  # noqa: BLE001 — any failure is just an unhealthy source
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.last_latency = time.monotonic() - started
            if state.consecutive_failures >= FAILURES_BEFORE_OPEN:
                state.open_until = time.monotonic() + BREAKER_COOLDOWN_S
                log.warning("circuit opened for %s after %d failures: %s",
                            source_name, state.consecutive_failures, state.last_error)
            return None

    async def global_query(self, path: str) -> GlobalResult:
        """One of adsb.lol's whole-world endpoints: mil, squawk/X, type/X, ...

        Goes through the same rate-slot reservation as a positional query. Skipping it
        was a real bug: three parallel squawk lookups plus a type lookup earned an
        HTTP 420 from adsb.lol, which opened the breaker, after which `military_aircraft`
        answered "0 aircraft" when the true answer was 81.

        Returns a GlobalResult rather than a bare list so that "the sky is empty" and
        "we could not ask" stay distinguishable — the same distinction the positional
        snapshot makes with its `error` field.

        `path` must be built from validated identifiers only — see enrich.is_well_formed.
        """
        if self._health["adsb.lol"].circuit_open:
            return GlobalResult([], "adsb.lol is rate-limiting or failing; "
                                    "backing off for up to 30 seconds")
        if not await self._reserve_slot("adsb.lol", GLOBAL_MIN_INTERVAL_S):
            return GlobalResult([], "too many queries queued for adsb.lol; try again "
                                    "in a moment")
        result = await self._get_aircraft("adsb.lol", f"{GLOBAL_BASE}/{path}")
        if result is None:
            state = self._health["adsb.lol"]
            return GlobalResult([], f"adsb.lol query failed: {state.last_error}")
        return GlobalResult(result[1], None)
