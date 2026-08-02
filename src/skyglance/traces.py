"""Where an aircraft has actually been — roughly the last day of its flight path.

adsb.lol publishes per-aircraft traces alongside its live API. A full trace for a busy
airliner is ~2,600 points covering ~21 hours and about 450 KB decompressed, which is far
too much to hand a model. So this module does the reduction:

    raw trace -> flight segments (takeoff to landing) -> a downsampled path per segment

What that buys, which live positions alone cannot answer: "where has this plane been
today", "what route did it fly", "how many legs has it done", "did it divert".

Honest limits, surfaced in the output rather than hidden:
  - Roughly 24 hours of history. This is not an archive; yesterday is already gone.
  - Coverage gaps look like straight lines. A track crossing the Atlantic will show a
    long jump because no receiver heard it, not because it teleported.
  - Only aircraft adsb.lol has seen. No trace means no coverage, not no flight.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .feeds import USER_AGENT
from .geometry import haversine_km

log = logging.getLogger("skyglance.traces")

TRACE_BASE = "https://globe.adsb.lol/data/traces"
REQUEST_TIMEOUT_S = 20.0

#: A separate host from api.adsb.lol, serving much larger payloads, so it gets its own
#: budget rather than sharing the live API's slot.
MIN_INTERVAL_S = 2.0
MAX_WAIT_S = 6.0

#: A gap longer than this with the aircraft airborne throughout means we lost reception,
#: not that it landed. Splitting there would invent a landing that never happened.
COVERAGE_GAP_S = 1800

#: Points per segment in the output. Enough to see the shape of a route; small enough
#: that a four-leg day stays readable.
PATH_SAMPLES = 20

#: Below this ground speed on the ground, the aircraft is parked rather than taxiing.
TAXI_SPEED_KT = 3.0


def trace_url(hex_id: str, recent: bool = False) -> str:
    """adsb.lol shards traces by the last two characters of the hex."""
    h = hex_id.lower().lstrip("~")
    kind = "recent" if recent else "full"
    return f"{TRACE_BASE}/{h[-2:]}/trace_{kind}_{h}.json"


@dataclass
class TracePoint:
    t: float             # absolute epoch seconds
    lat: float
    lon: float
    alt_ft: Optional[float]   # None when on the ground
    on_ground: bool
    ground_speed_kt: Optional[float]
    track_deg: Optional[float]


def _parse_points(payload: dict) -> list[TracePoint]:
    """Trace rows are positional: [dt, lat, lon, alt, gs, track, flags, vert_rate, ...]."""
    base = payload.get("timestamp")
    rows = payload.get("trace") or []
    if not isinstance(base, (int, float)):
        return []

    points: list[TracePoint] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        dt, lat, lon, alt = row[0], row[1], row[2], row[3]
        if not all(isinstance(v, (int, float)) for v in (dt, lat, lon)):
            continue
        on_ground = alt == "ground"
        points.append(TracePoint(
            t=base + dt,
            lat=float(lat),
            lon=float(lon),
            alt_ft=None if on_ground else (float(alt) if isinstance(alt, (int, float)) else None),
            on_ground=on_ground,
            ground_speed_kt=row[4] if isinstance(row[4], (int, float)) else None,
            track_deg=row[5] if len(row) > 5 and isinstance(row[5], (int, float)) else None,
        ))
    return points


def _segment(points: list[TracePoint]) -> list[list[TracePoint]]:
    """Split a trace into airborne legs.

    A leg ends when the aircraft is on the ground and stationary, or when reception is
    lost for longer than COVERAGE_GAP_S.
    """
    segments: list[list[TracePoint]] = []
    current: list[TracePoint] = []

    for i, p in enumerate(points):
        parked = p.on_ground and (p.ground_speed_kt or 0) < TAXI_SPEED_KT
        gap = i > 0 and (p.t - points[i - 1].t) > COVERAGE_GAP_S

        if parked or gap:
            if current:
                segments.append(current)
                current = []
            if parked:
                continue
        if not p.on_ground:
            current.append(p)

    if current:
        segments.append(current)
    # A handful of stray points is reception noise, not a flight.
    return [s for s in segments if len(s) >= 5]


def _downsample(points: list[TracePoint], n: int) -> list[TracePoint]:
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def _path_km(points: list[TracePoint]) -> float:
    return sum(haversine_km(a.lat, a.lon, b.lat, b.lon)
               for a, b in zip(points, points[1:]))


def summarise(payload: dict, max_segments: int = 6,
              path_samples: int = PATH_SAMPLES) -> dict[str, Any]:
    """Reduce a raw trace to something a model can actually read."""
    points = _parse_points(payload)
    if not points:
        return {"found": False,
                "note": "Trace exists but contained no usable positions."}

    segments = _segment(points)
    now = time.time()

    legs = []
    for seg in segments[-max_segments:]:
        sampled = _downsample(seg, path_samples)
        altitudes = [p.alt_ft for p in seg if p.alt_ft is not None]
        legs.append({
            "started_utc": _iso(seg[0].t),
            "ended_utc": _iso(seg[-1].t),
            "duration_minutes": round((seg[-1].t - seg[0].t) / 60),
            "from": {"lat": round(seg[0].lat, 4), "lon": round(seg[0].lon, 4)},
            "to": {"lat": round(seg[-1].lat, 4), "lon": round(seg[-1].lon, 4)},
            "max_altitude_ft": round(max(altitudes)) if altitudes else None,
            "ground_track_km": round(_path_km(seg)),
            "still_airborne": (now - seg[-1].t) < 600 and seg is segments[-1],
            "path": [{"lat": round(p.lat, 4), "lon": round(p.lon, 4),
                      "alt_ft": round(p.alt_ft) if p.alt_ft is not None else None,
                      "utc": _iso(p.t)} for p in sampled],
        })

    return {
        "found": True,
        "aircraft": {
            "hex": payload.get("icao"),
            "registration": payload.get("r"),
            "type": payload.get("t"),
            "description": payload.get("desc"),
            "operator": payload.get("ownOp"),
            "year": payload.get("year"),
        },
        "history_window": {
            "from_utc": _iso(points[0].t),
            "to_utc": _iso(points[-1].t),
            "hours": round((points[-1].t - points[0].t) / 3600, 1),
        },
        "legs_found": len(segments),
        "legs_returned": len(legs),
        "legs": legs,
        "limits": (
            "Roughly the last 24 hours only — this is not a flight archive. Each leg's "
            "path is downsampled, so it shows the shape of the route, not every point. "
            "Long straight jumps are gaps in volunteer receiver coverage (oceans "
            "especially), not the actual path flown."),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class TraceClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            # Traces are served gzipped and are large; asking for it explicitly took one
            # response from 450 KB to 68 KB on the wire.
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True)
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, hex_id: str, recent: bool = False) -> Optional[dict]:
        async with self._lock:
            now = time.monotonic()
            slot = max(self._next_slot, now)
            wait = slot - now
            if wait > MAX_WAIT_S:
                return None
            self._next_slot = slot + MIN_INTERVAL_S
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            response = await self._client.get(trace_url(hex_id, recent))
            if response.status_code != 200:
                return None
            return response.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("trace fetch failed for %s: %s", hex_id, exc)
            return None
