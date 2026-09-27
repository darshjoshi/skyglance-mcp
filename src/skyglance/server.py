"""SkyGlance — the MCP server.

Thin by design: every tool here validates input, calls into feeds/geometry/enrich/store,
and formats. No logic lives in this file that isn't about presentation.

stdio transport note: stdout IS the JSON-RPC channel. Every diagnostic in this package
goes to stderr via logging. Printing to stdout corrupts the stream.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import airport_data, airports, traces, weather
from . import world as world_mod
from .enrich import Enricher, is_valid_callsign, is_valid_hex, is_valid_registration
from .feeds import FeedClient, Snapshot
from .geometry import describe, haversine_km, rank_key
from .interest import score as interest_score
from .poller import DEFAULT_INTERVAL_S, Poller
from .store import Store
from .traces import TraceClient
from .world import WorldView

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
# httpx logs every request at INFO, which turns the poller into a line a minute of noise
# in the client's MCP log for no diagnostic value. Failures still surface: feeds.py logs
# its own warnings and feed_health() reports per-source errors.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("skyglance")

#: Straight-line extrapolation is excellent to 30s, good to 60s, and worthless by 240s,
#: where 77% of aircraft end up more than 2 km from the prediction. Beyond this we say
#: "inbound" and refuse to give a countdown. This is measured, not a guess — see
#: docs/OVERHEAD-DETECTION.md in the skyglance-mac repo.
PREDICTION_HORIZON_S = 90

MAX_RADIUS_NM = 250

mcp = FastMCP(
    "SkyGlance",
    instructions="""SkyGlance — what is flying above you, and what you've seen before.

LOCATION: most tools take lat/lon. If omitted they use SKYGLANCE_HOME_LAT/LON. If
neither is set, ask the user where they are rather than guessing a city.

TOOL ROUTING:
- "what's flying over me" / "anything overhead" -> whats_overhead
- "what's about to fly over" -> coming_overhead (only promises 90 seconds ahead)
- "what's that plane" / a hex, registration or callsign -> identify_aircraft
- "where is flight XX123" -> track_flight
- "any military aircraft" -> military_aircraft
- "any emergencies" -> emergencies
- "any A380s / 747s up" -> find_by_type
- "anything interesting" -> interesting_nearby
- "can I see anything tonight" -> viewing_conditions
- "have I seen this plane before" -> sighting_history / is_this_new
- "what's my closest ever" / records -> my_records
- "how many have I seen" -> spotting_stats
- "is the tracker running" -> poller_status
- "are the feeds up" -> feed_health

READING THE OUTPUT: elevation_deg is how far up the sky to look (90 = straight up,
below 15 = on the horizon and usually behind buildings). look_direction is the compass
point to face. naked_eye_plausible false means it is too far to see regardless.

HONESTY RULES:
- Never invent a countdown beyond 90 seconds. The tools deliberately withhold one.
- Coverage is geographic. A near-empty result over rural areas or oceans means no
  volunteer receiver is nearby, NOT that the sky is empty. Say so.
- Altitudes are barometric; speeds are ground speed, not airspeed.

ATTRIBUTION: every position result carries an `attribution` field. adsb.lol data is
ODbL and photo credits are a condition of use, so pass them through when you show the
data.""",
)


def _tool(title: str):
    """Register a tool with a display title and read-only annotations.

    Every SkyGlance tool reads public flight data or the local history; none changes
    anything outside this machine. The only writes are local lookup caches, so
    readOnlyHint is honest, and Claude can run the tools without a per-call prompt.
    """
    return mcp.tool(title=title, annotations=ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))


_feeds: Optional[FeedClient] = None
_store: Optional[Store] = None
_enricher: Optional[Enricher] = None
_tracer: Optional[TraceClient] = None
_world: Optional[WorldView] = None
_poller: Optional[Poller] = None
_poller_started = False


def feeds() -> FeedClient:
    global _feeds
    if _feeds is None:
        _feeds = FeedClient()
    return _feeds


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def enricher() -> Enricher:
    global _enricher
    if _enricher is None:
        _enricher = Enricher(store())
    return _enricher


def tracer() -> TraceClient:
    global _tracer
    if _tracer is None:
        _tracer = TraceClient()
    return _tracer


def world() -> WorldView:
    global _world
    if _world is None:
        _world = WorldView(feeds())
    return _world


def home() -> Optional[tuple[float, float]]:
    try:
        lat = float(os.environ["SKYGLANCE_HOME_LAT"])
        lon = float(os.environ["SKYGLANCE_HOME_LON"])
    except (KeyError, ValueError):
        return None
    return lat, lon


def resolve_location(lat: Optional[float], lon: Optional[float]) -> tuple[float, float]:
    if lat is not None and lon is not None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"latitude/longitude out of range: {lat}, {lon}")
        return lat, lon
    configured = home()
    if configured is None:
        raise ValueError(
            "No location given and SKYGLANCE_HOME_LAT/SKYGLANCE_HOME_LON are not set. "
            "Ask the user where they are, then pass lat and lon.")
    return configured


def _maybe_start_poller() -> None:
    """Start background polling once, if a home location is configured."""
    global _poller, _poller_started
    if _poller_started or os.environ.get("SKYGLANCE_POLL") == "0":
        return
    _poller_started = True
    configured = home()
    if configured is None:
        return
    interval = float(os.environ.get("SKYGLANCE_POLL_INTERVAL", DEFAULT_INTERVAL_S))
    _poller = Poller(feeds(), store(), configured[0], configured[1],
                     interval_s=interval)
    if _poller.start():
        log.info("background poller started at %.2f,%.2f every %.0fs",
                 configured[0], configured[1], _poller.interval_s)


# ── Formatting ───────────────────────────────────────────────────────────────


def _snapshot_meta(snap: Snapshot) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "sources": snap.contributing_sources,
        "attribution": snap.attributions,
        "age_seconds": round(time.time() - snap.captured_at, 1),
    }
    if snap.degraded:
        meta["degraded"] = True
    if snap.stale:
        meta["stale"] = True
        meta["note"] = "All sources failed; this is the last good snapshot."
    if snap.error:
        meta["error"] = snap.error
    return meta


def _climb_state(rate_fpm: Optional[float]) -> Optional[str]:
    """Plain-language vertical state, so the model doesn't have to pick a threshold."""
    if rate_fpm is None:
        return None
    if rate_fpm >= 400:
        return "climbing"
    if rate_fpm <= -400:
        return "descending"
    return "level"


def _unavailable(reason: str) -> dict[str, Any]:
    """An answer we could not obtain — never the same as an answer of zero.

    Returning `count: 0` when the upstream is rate-limiting would let the model report
    "no military aircraft airborne" when the truth is "we did not get to ask". That
    exact failure happened in testing.
    """
    return {"available": False, "error": reason,
            "note": ("This is NOT a result of zero. The data source could not be "
                     "reached, so nothing is known either way. Say so plainly and "
                     "offer to retry in a few seconds.")}


def _coverage_note(count: int) -> Optional[str]:
    if count == 0:
        return ("No aircraft found. Free ADS-B coverage depends on a volunteer receiver "
                "being nearby — over oceans, Africa, central Asia and remote areas this "
                "means no data, not an empty sky.")
    if count < 5:
        return ("Very few aircraft. This may be thin receiver coverage rather than a "
                "quiet sky.")
    return None


def _view(a, obs_lat: float, obs_lon: float) -> Optional[dict[str, Any]]:
    if a.lat is None or a.lon is None:
        return None
    s = describe(obs_lat, obs_lon, a.lat, a.lon, a.altitude_ft,
                 a.ground_speed_kt, a.track_deg, a.vertical_rate_fpm)
    out: dict[str, Any] = {
        "hex": a.hex,
        "callsign": a.callsign,
        "registration": a.registration,
        "type": a.type_code,
        "operator": a.operator,
        "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None,
        "ground_speed_kt": a.ground_speed_kt,
        # Vertical rate is what separates a departure from an arrival from an
        # overflight. Omitting it made that undecidable for the model.
        "vertical_rate_fpm": a.vertical_rate_fpm,
        "climb_state": _climb_state(a.vertical_rate_fpm),
        "elevation_deg": round(s.elevation_deg, 1),
        "look_direction": s.look_direction,
        "bearing_deg": round(s.bearing_deg),
        "distance_km": round(s.ground_km, 1),
        "slant_km": round(s.slant_km, 1),
        "overhead": s.overhead,
        "naked_eye_plausible": s.naked_eye_plausible,
        "military": a.is_military,
        "build_year": a.year,
        "seen_by": a.sources,
    }
    if a.has_emergency:
        out["emergency"] = a.emergency or f"squawk {a.squawk}"
    out["_rank"] = rank_key(s)
    out["_cpa"] = s.cpa
    return out


def _strip(v: dict[str, Any]) -> dict[str, Any]:
    return {k: val for k, val in v.items() if not k.startswith("_")}


# ── Tools: overhead ──────────────────────────────────────────────────────────


@_tool("What's Overhead")
async def whats_overhead(lat: Optional[float] = None, lon: Optional[float] = None,
                         radius_nm: int = 60, limit: int = 25) -> dict:
    """What is in the sky above a location right now, and where to look.

    Returns aircraft ranked by what a person would actually notice: directly overhead
    first, then what is about to be, then the rest by how high in the sky they sit.
    Includes viewing conditions, because telling someone to look up at an overcast sky
    is useless.
    """
    obs_lat, obs_lon = resolve_location(lat, lon)
    radius = max(1, min(MAX_RADIUS_NM, radius_nm))
    _maybe_start_poller()

    snap, conditions = await asyncio.gather(
        feeds().snapshot(obs_lat, obs_lon, radius),
        weather.current(obs_lat, obs_lon),
    )

    views = [v for v in (_view(a, obs_lat, obs_lon)
                         for a in snap.aircraft if not a.on_ground) if v]
    views.sort(key=lambda v: v["_rank"])

    overhead = [_strip(v) for v in views if v["overhead"]]
    rest = [_strip(v) for v in views if not v["overhead"]][:limit]

    result = {
        "observer": {"lat": obs_lat, "lon": obs_lon, "radius_nm": radius},
        "airborne_count": len(views),
        "overhead_now": overhead,
        "others": rest,
        "viewing_conditions": {
            "summary": conditions.summary,
            "cloud_cover_pct": conditions.cloud_cover_pct,
            "visibility_km": conditions.visibility_km,
            "daylight": conditions.is_daylight,
        },
        **_snapshot_meta(snap),
    }
    note = _coverage_note(len(views))
    if note:
        result["coverage_note"] = note
    return result


@_tool("Coming Overhead")
async def coming_overhead(lat: Optional[float] = None,
                          lon: Optional[float] = None) -> dict:
    """What is about to pass overhead, within the next 90 seconds.

    Deliberately refuses to predict further out. Straight-line extrapolation from track
    and ground speed is accurate to about 0.1 km at 30 seconds but misses by a median
    6 km at 4 minutes, because aircraft on approach turn constantly. Anything further
    away is reported as inbound with no countdown rather than a number that would be
    wrong most of the time.
    """
    obs_lat, obs_lon = resolve_location(lat, lon)
    _maybe_start_poller()
    snap = await feeds().snapshot(obs_lat, obs_lon, 60)

    imminent, inbound = [], []
    for a in snap.aircraft:
        if a.on_ground:
            continue
        v = _view(a, obs_lat, obs_lon)
        if not v:
            continue
        cpa = v["_cpa"]
        if cpa is None or not cpa.approaching or cpa.elevation_deg < 40:
            continue
        entry = _strip(v) | {
            "passing_within_km": round(cpa.ground_km, 1),
            "altitude_at_pass_ft": cpa.altitude_ft,
            "elevation_at_pass_deg": round(cpa.elevation_deg),
            "pass_direction": v["look_direction"],
        }
        if cpa.seconds_away <= PREDICTION_HORIZON_S:
            entry["seconds_away"] = round(cpa.seconds_away)
            imminent.append(entry)
        else:
            entry["timing"] = "inbound — beyond the 90s window we can predict honestly"
            inbound.append(entry)

    imminent.sort(key=lambda e: e["seconds_away"])
    inbound.sort(key=lambda e: e["passing_within_km"])

    return {
        "observer": {"lat": obs_lat, "lon": obs_lon},
        "overhead_within_90s": imminent,
        "inbound_no_countdown": inbound[:10],
        "prediction_note": (
            "Countdowns are only given inside 90 seconds. Measured straight-line error "
            "is 0.11 km at 30s but 5.98 km median at 240s, so longer predictions are "
            "withheld rather than guessed."),
        **_snapshot_meta(snap),
    }


@_tool("Nearest Aircraft")
async def nearest_aircraft(lat: Optional[float] = None, lon: Optional[float] = None,
                           count: int = 5) -> dict:
    """The closest aircraft right now, by slant range, whether or not they're visible."""
    obs_lat, obs_lon = resolve_location(lat, lon)
    snap = await feeds().snapshot(obs_lat, obs_lon, 60)
    views = [v for v in (_view(a, obs_lat, obs_lon)
                         for a in snap.aircraft if not a.on_ground) if v]
    views.sort(key=lambda v: v["slant_km"])
    return {
        "observer": {"lat": obs_lat, "lon": obs_lon},
        "nearest": [_strip(v) for v in views[:max(1, min(50, count))]],
        **_snapshot_meta(snap),
    }


# ── Tools: identify ──────────────────────────────────────────────────────────


@_tool("Identify Aircraft")
async def identify_aircraft(hex_id: Optional[str] = None,
                            registration: Optional[str] = None,
                            callsign: Optional[str] = None) -> dict:
    """Full identity for one aircraft: type, operator, route, and a photograph.

    Give any one of an ICAO hex, a registration, or a callsign. Results are cached
    permanently, since an airframe's type and owner do not change.
    """
    e = enricher()
    result: dict[str, Any] = {}

    if hex_id and not is_valid_hex(hex_id):
        return {"error": f"malformed hex: {hex_id!r}"}
    if registration and not is_valid_registration(registration):
        return {"error": f"malformed registration: {registration!r}"}
    if callsign and not is_valid_callsign(callsign):
        return {"error": f"malformed callsign: {callsign!r}"}

    # A registration or callsign alone still needs a hex, so find the live aircraft.
    if not hex_id and (registration or callsign):
        path = (f"registration/{registration.upper()}" if registration
                else f"callsign/{callsign.upper()}")
        live = await feeds().global_query(path)
        if live.error:
            result["currently_airborne"] = None
            result["live_lookup_error"] = live.error
        elif live.aircraft:
            first = live.aircraft[0]
            hex_id = first.hex
            result["currently_airborne"] = True
            result["position"] = {"lat": first.lat, "lon": first.lon,
                                  "altitude_ft": first.altitude_ft}
            callsign = callsign or first.callsign
        else:
            result["currently_airborne"] = False

    if hex_id:
        identity = await e.aircraft(hex_id)
        if identity:
            result["aircraft"] = {k: v for k, v in vars(identity).items() if v}
            registration = registration or identity.registration
    elif registration:
        # Not airborne, but a registration is enough on its own — which is the common
        # case, since any given airframe is parked most of the time.
        identity = await e.aircraft_by_registration(registration)
        if identity:
            result["aircraft"] = {k: v for k, v in vars(identity).items() if v}

    if callsign:
        route = await e.route(callsign)
        if route and route.complete:
            result["route"] = {
                "airline": route.airline,
                "origin": vars(route.origin),
                "destination": vars(route.destination),
            }
        elif route:
            result["route_note"] = "Only one end of the route is known; withheld."

    if registration:
        photo = await e.photo(registration)
        if photo:
            result["photo"] = {
                "url": photo.thumbnail_url,
                "photographer": photo.photographer,
                "credit_required": True,
            }
        if hex_id:
            result["seen_before"] = not store().is_first_sighting(hex_id)

    if not result.get("aircraft") and not result.get("route"):
        result["note"] = ("No further details found. Coverage of the free aircraft "
                          "databases is good but not complete.")
    result["attribution"] = ["adsbdb.com", "hexdb.io", "planespotters.net"]
    return result


@_tool("Track Flight")
async def track_flight(callsign: Optional[str] = None,
                       registration: Optional[str] = None) -> dict:
    """Where a specific flight is right now, anywhere in the world.

    Searches the whole feed rather than a circle. Returns nothing if the flight is not
    currently airborne or is outside receiver coverage — the two are indistinguishable.
    """
    if callsign and not is_valid_callsign(callsign):
        return {"error": f"malformed callsign: {callsign!r}"}
    if registration and not is_valid_registration(registration):
        return {"error": f"malformed registration: {registration!r}"}
    if not callsign and not registration:
        return {"error": "give a callsign or a registration"}

    path = (f"callsign/{callsign.upper()}" if callsign
            else f"registration/{registration.upper()}")
    found = await feeds().global_query(path)
    if found.error:
        return _unavailable(found.error)
    if not found.aircraft:
        return {"found": False,
                "note": ("Not currently airborne, or outside ADS-B receiver coverage. "
                         "These two cannot be told apart from this data.")}

    a = found.aircraft[0]
    out: dict[str, Any] = {
        "found": True,
        "hex": a.hex, "callsign": a.callsign, "registration": a.registration,
        "type": a.type_code,
        "position": {"lat": a.lat, "lon": a.lon},
        "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None,
        "ground_speed_kt": a.ground_speed_kt,
        "track_deg": a.track_deg,
        "on_ground": a.on_ground,
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }
    configured = home()
    if configured and a.lat is not None and a.lon is not None:
        s = describe(configured[0], configured[1], a.lat, a.lon, a.altitude_ft)
        out["from_home"] = {"distance_km": round(s.ground_km, 1),
                            "bearing": s.look_direction,
                            "elevation_deg": round(s.elevation_deg, 1)}
    if a.callsign:
        route = await enricher().route(a.callsign)
        if route and route.complete:
            out["route"] = {"airline": route.airline,
                            "origin": vars(route.origin),
                            "destination": vars(route.destination)}
            estimate = _estimate_arrival(a, route)
            if estimate:
                out["arrival_estimate"] = estimate
    if a.year:
        out["build_year"] = a.year
    return out


def _estimate_arrival(a, route) -> Optional[dict[str, Any]]:
    """Rough time to destination from current position and ground speed.

    This is arithmetic on live data, NOT a scheduled or published arrival time. It
    assumes the aircraft flies straight to the field at its current speed — no descent
    profile, no holding, no vectoring, no runway change. On a long final it will read
    early by several minutes. Labelled clearly so it can never be mistaken for an STA.
    """
    dest = route.destination
    if dest is None or dest.lat is None or dest.lon is None:
        return None
    if a.lat is None or a.lon is None or not a.ground_speed_kt:
        return None
    if a.ground_speed_kt < 50:
        return None

    remaining_km = haversine_km(a.lat, a.lon, dest.lat, dest.lon)
    speed_kmh = a.ground_speed_kt * 1.852
    minutes = round(remaining_km / speed_kmh * 60)
    return {
        "distance_to_destination_km": round(remaining_km),
        "minutes_remaining_estimate": minutes,
        "basis": "current position and ground speed",
        "caveat": ("An estimate computed from live position and speed — NOT a scheduled "
                   "or published arrival time. It ignores descent, holding, vectoring "
                   "and taxi, so it reads early, typically by several minutes."),
    }


# ── Tools: spotting ──────────────────────────────────────────────────────────


def _global_result(aircraft: list, obs: Optional[tuple[float, float]],
                   near_km: Optional[float]) -> list[dict]:
    rows = []
    for a in aircraft:
        row = {"hex": a.hex, "callsign": a.callsign, "registration": a.registration,
               "type": a.type_code, "operator": a.operator,
               "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None,
               "position": {"lat": a.lat, "lon": a.lon}}
        if obs and a.lat is not None and a.lon is not None:
            s = describe(obs[0], obs[1], a.lat, a.lon, a.altitude_ft)
            if near_km is not None and s.ground_km > near_km:
                continue
            row["distance_km"] = round(s.ground_km, 1)
            row["look_direction"] = s.look_direction
            row["elevation_deg"] = round(s.elevation_deg, 1)
        rows.append(row)
    rows.sort(key=lambda r: r.get("distance_km", math.inf))
    return rows


@_tool("Military Aircraft")
async def military_aircraft(near_me: bool = False, within_km: float = 400,
                            lat: Optional[float] = None,
                            lon: Optional[float] = None) -> dict:
    """Military aircraft currently airborne, worldwide or near a location.

    Set near_me true to filter to within_km of your location.
    """
    obs = None
    if near_me:
        obs = resolve_location(lat, lon)
    result = await feeds().global_query("mil")
    if result.error:
        return _unavailable(result.error)
    rows = _global_result(result.aircraft, obs, within_km if near_me else None)
    return {"count": len(rows), "aircraft": rows[:60],
            "scope": f"within {within_km:.0f} km" if near_me else "worldwide",
            "attribution": ["adsb.lol (ODbL 1.0)"]}


@_tool("Emergencies")
async def emergencies() -> dict:
    """Aircraft squawking an emergency code, worldwide.

    7700 general emergency, 7600 radio failure, 7500 hijack. Usually zero — that is the
    normal and desirable answer, not a failure.
    """
    codes = {"7700": "general emergency", "7600": "radio failure", "7500": "unlawful interference"}
    # Sequential, not gathered: these are three whole-world queries against one service,
    # and firing them at once is what earned an HTTP 420 in testing. The rate limiter
    # would space them anyway; doing it in order keeps the intent obvious.
    results = [await feeds().global_query(f"squawk/{c}") for c in codes]
    if all(r.error for r in results):
        return _unavailable(results[0].error or "adsb.lol unavailable")
    out = []
    for code, result in zip(codes, results):
        for a in result.aircraft:
            out.append({"squawk": code, "meaning": codes[code], "hex": a.hex,
                        "callsign": a.callsign, "registration": a.registration,
                        "type": a.type_code,
                        "position": {"lat": a.lat, "lon": a.lon},
                        "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None})
    response: dict[str, Any] = {
        "count": len(out),
        "aircraft": out,
        "note": ("Zero is the usual result. A squawk can also be set briefly by mistake, "
                 "so treat a single hit as unconfirmed."),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }
    unchecked = [c for c, r in zip(codes, results) if r.error]
    if unchecked:
        response["partial"] = (f"Could not check {', '.join(unchecked)} — "
                               f"{results[0].error}. Zero here is not conclusive.")
    return response


@_tool("Find by Aircraft Type")
async def find_by_type(type_code: str, near_me: bool = False, within_km: float = 400,
                       lat: Optional[float] = None, lon: Optional[float] = None) -> dict:
    """Every airborne aircraft of one ICAO type code, worldwide or nearby.

    Examples: A388 (A380), B748 (747-8), A124 (An-124), C17. Set near_me to filter.
    """
    code = type_code.strip().upper()
    if not is_valid_callsign(code):
        return {"error": f"malformed type code: {type_code!r}"}
    obs = resolve_location(lat, lon) if near_me else None
    result = await feeds().global_query(f"type/{code}")
    if result.error:
        return _unavailable(result.error)
    rows = _global_result(result.aircraft, obs, within_km if near_me else None)
    return {"type": code, "count": len(rows), "aircraft": rows[:60],
            "scope": f"within {within_km:.0f} km" if near_me else "worldwide",
            "attribution": ["adsb.lol (ODbL 1.0)"]}


@_tool("Interesting Nearby")
async def interesting_nearby(lat: Optional[float] = None, lon: Optional[float] = None,
                             radius_nm: int = 60, minimum_score: float = 45) -> dict:
    """Only the aircraft worth walking outside for.

    Scores each aircraft as emergency, rare type, military, large-and-low, or a close
    low helicopter, and returns those above the threshold. Thresholds are deliberately
    harsh: under an approach path a generous filter fires constantly.
    """
    obs_lat, obs_lon = resolve_location(lat, lon)
    snap, conditions = await asyncio.gather(
        feeds().snapshot(obs_lat, obs_lon, max(1, min(MAX_RADIUS_NM, radius_nm))),
        weather.current(obs_lat, obs_lon),
    )

    found = []
    for a in snap.aircraft:
        if a.on_ground:
            continue
        v = _view(a, obs_lat, obs_lon)
        if not v:
            continue
        scored = interest_score(
            type_code=a.type_code, category=a.category, altitude_ft=a.altitude_ft,
            slant_km=v["slant_km"], is_military=a.is_military,
            has_emergency=a.has_emergency)
        if scored is None or scored.score < minimum_score:
            continue
        entry = _strip(v) | {
            "why": scored.reason,
            "interest_score": round(scored.score),
            "category": scored.category,
            "visible_now": conditions.permits_viewing_at(a.altitude_ft),
        }
        if store().is_first_sighting(a.hex):
            entry["first_ever_sighting"] = True
        found.append(entry)

    found.sort(key=lambda e: -e["interest_score"])
    return {
        "observer": {"lat": obs_lat, "lon": obs_lon},
        "count": len(found),
        "aircraft": found,
        "viewing_conditions": conditions.summary,
        **_snapshot_meta(snap),
    }


@_tool("Track History")
async def track_history(hex_id: Optional[str] = None,
                        registration: Optional[str] = None,
                        callsign: Optional[str] = None) -> dict:
    """Where an aircraft has actually flown over roughly the last day.

    Returns each airborne leg — when it started and ended, how long, how far, the
    highest altitude reached, and a downsampled path. This is what answers "where has
    this plane been today", "what route did it fly", and "how many legs has it done".

    Not a flight archive: roughly 24 hours only, and long straight jumps in a path are
    gaps in volunteer receiver coverage rather than the route actually flown.
    """
    if hex_id and not is_valid_hex(hex_id):
        return {"error": f"malformed hex: {hex_id!r}"}
    if registration and not is_valid_registration(registration):
        return {"error": f"malformed registration: {registration!r}"}
    if callsign and not is_valid_callsign(callsign):
        return {"error": f"malformed callsign: {callsign!r}"}

    if not hex_id:
        if registration:
            identity = await enricher().aircraft_by_registration(registration)
            # adsbdb returns the hex as mode_s; fall back to a live lookup if it misses.
            hex_id = await _hex_for(registration=registration, identity=identity)
        elif callsign:
            hex_id = await _hex_for(callsign=callsign)
        else:
            return {"error": "give a hex, registration, or callsign"}

    if not hex_id:
        return {"found": False,
                "note": ("Could not resolve that to an aircraft. A callsign only "
                         "resolves while the flight is airborne; a registration needs "
                         "to be known to adsbdb.")}

    payload = await tracer().fetch(hex_id)
    if payload is None:
        return {"found": False, "hex": hex_id,
                "note": ("No trace available. Either adsb.lol has not seen this "
                         "aircraft in the last day, or it flies outside volunteer "
                         "receiver coverage. This is not evidence it did not fly.")}
    return traces.summarise(payload)


async def _hex_for(registration: Optional[str] = None,
                   callsign: Optional[str] = None,
                   identity: Optional[Any] = None) -> Optional[str]:
    """Resolve a registration or callsign to an ICAO hex, database first, live second."""
    if identity is not None and getattr(identity, "hex", None):
        return identity.hex
    path = (f"registration/{registration.upper()}" if registration
            else f"callsign/{callsign.upper()}")
    live = await feeds().global_query(path)
    if live.error or not live.aircraft:
        return None
    return live.aircraft[0].hex


@_tool("Airport Activity")
async def airport_activity(icao: str, radius_nm: int = 20) -> dict:
    """What is moving around an airport right now, observed rather than scheduled.

    Buckets nearby aircraft into departing, arriving, on the ground, and overflying,
    based on position and climb or descent rate.

    This is NOT a departure board. Nothing in this data knows a schedule, a gate, or
    which airport a flight is bound for — a jet climbing out near the field is inferred
    to be departing it. In a metro area with several airports close together, a
    neighbour's traffic can be misattributed.
    """
    code = icao.strip().upper()
    if not airports.is_valid_icao(code):
        return {"error": f"not an ICAO airport code: {icao!r}. Use four letters, "
                         f"like KEWR, EGLL or VABB."}

    cached = store().get_enrichment("airport", code)
    if cached:
        field = airports.AirportRef(**cached)
    elif cached == {}:
        return {"error": f"unknown airport: {code}"}
    else:
        field = await airports.lookup(code)
        store().put_enrichment("airport", code, vars(field) if field else {})
        if field is None:
            return {"error": f"unknown airport: {code}"}

    snap = await feeds().snapshot(field.lat, field.lon,
                                 max(1, min(MAX_RADIUS_NM, radius_nm)))
    activity = airports.describe_activity(snap.aircraft, field)
    activity |= _snapshot_meta(snap)
    note = _coverage_note(len(snap.aircraft))
    if note:
        activity["coverage_note"] = note
    return activity


@_tool("Airline Info")
async def airline_info(code: str) -> dict:
    """Look up an airline by its ICAO code — the prefix on a callsign.

    JBU is JetBlue, BAW is British Airways, UAE is Emirates. Returns the airline name,
    its IATA code, country, and the radio callsign crews actually say.
    """
    cleaned = code.strip().upper()
    result = await airports.airline(cleaned)
    if not result:
        return {"found": False,
                "note": (f"No airline found for {cleaned!r}. Callsign prefixes are ICAO "
                         f"codes (three letters, like JBU), not IATA codes (two, like B6).")}
    return {"found": True, **result, "attribution": ["adsbdb.com"]}


@_tool("Privacy-Blocked Aircraft")
async def privacy_blocked_aircraft(near_me: bool = False, within_km: float = 400,
                                   lat: Optional[float] = None,
                                   lon: Optional[float] = None) -> dict:
    """Aircraft using privacy programmes that hide their identity on most trackers.

    LADD (Limiting Aircraft Data Displayed) is an opt-out US owners can request, and PIA
    assigns a rotating temporary hex address. These are visible here because adsb.lol
    publishes unfiltered community data. Positions are real; the identity is obscured
    by design, so do not expect a registration or owner.
    """
    obs = resolve_location(lat, lon) if near_me else None
    ladd, pia = await feeds().global_query("ladd"), await feeds().global_query("pia")
    if ladd.error and pia.error:
        return _unavailable(ladd.error or "adsb.lol unavailable")

    rows = _global_result(ladd.aircraft, obs, within_km if near_me else None)
    for r in rows:
        r["programme"] = "LADD"
    pia_rows = _global_result(pia.aircraft, obs, within_km if near_me else None)
    for r in pia_rows:
        r["programme"] = "PIA"
    rows.extend(pia_rows)
    rows.sort(key=lambda r: r.get("distance_km", math.inf))

    return {"count": len(rows), "aircraft": rows[:40],
            "scope": f"within {within_km:.0f} km" if near_me else "worldwide",
            "note": ("Identity is deliberately obscured on these aircraft. Speculating "
                     "about who is aboard is not something this data supports."),
            "attribution": ["adsb.lol (ODbL 1.0)"]}


@_tool("Airline Fleet View")
async def fleet_view(airline: str, limit: int = 60) -> dict:
    """Every flight an airline currently has airborne, worldwide.

    Give the ICAO airline code — the three-letter prefix on a callsign. BAW is British
    Airways, UAE is Emirates, DAL is Delta, JBU is JetBlue. Use `airline_info` if you
    only have the two-letter IATA code.

    Reads one shared global snapshot rather than querying per aircraft, so it is cheap
    to ask repeatedly. Only aircraft within volunteer receiver coverage appear — a
    long-haul fleet will be under-counted while its aircraft are over oceans.
    """
    code = airline.strip().upper()
    if not is_valid_callsign(code) or len(code) != 3 or not code.isalpha():
        return {"error": f"expected a three-letter ICAO airline code, got {airline!r}. "
                         f"BAW, UAE, DAL, JBU. Use airline_info to convert from IATA."}

    snapshot = await world().sweep()
    if snapshot.error:
        return _unavailable(snapshot.error)

    matches = [a for a in snapshot.aircraft if world_mod.callsign_prefix(a) == code]
    matches.sort(key=lambda a: -(a.altitude_ft or 0))

    airborne = [a for a in matches if not a.on_ground]
    rows = [{
        "callsign": a.callsign, "registration": a.registration, "type": a.type_code,
        "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None,
        "ground_speed_kt": a.ground_speed_kt,
        "climb_state": _climb_state(a.vertical_rate_fpm),
        "on_ground": a.on_ground,
        "position": {"lat": a.lat, "lon": a.lon},
    } for a in matches[:limit]]

    by_type: dict[str, int] = {}
    for a in matches:
        if a.type_code:
            by_type[a.type_code] = by_type.get(a.type_code, 0) + 1

    details = await airports.airline(code)
    return {
        "airline": details or {"icao": code},
        "total_seen": len(matches),
        "airborne": len(airborne),
        "on_ground": len(matches) - len(airborne),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "aircraft": rows,
        "snapshot_age_seconds": round(snapshot.age_seconds),
        "coverage_caveat": (
            "Counts only aircraft within volunteer ADS-B coverage. Long-haul fleets are "
            "under-counted while over oceans, and this is not the airline's full fleet — "
            "only what is transmitting and being heard right now."),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }


@_tool("Search Aircraft")
async def search_aircraft(airline: Optional[str] = None, type_code: Optional[str] = None,
                          min_altitude_ft: Optional[float] = None,
                          max_altitude_ft: Optional[float] = None,
                          min_speed_kt: Optional[float] = None,
                          max_speed_kt: Optional[float] = None,
                          military_only: bool = False,
                          near_me: bool = False, within_km: float = 400,
                          lat: Optional[float] = None, lon: Optional[float] = None,
                          limit: int = 40) -> dict:
    """Find airborne aircraft matching any combination of filters.

    Filter by airline (ICAO code), aircraft type, altitude band, speed band, or military
    status — worldwide, or within a distance of a location. This is the general search:
    "any 747s above 40,000 feet", "Lufthansa aircraft below 10,000 feet near me",
    "anything doing over 500 knots".
    """
    snapshot = await world().sweep()
    if snapshot.error:
        return _unavailable(snapshot.error)

    obs = resolve_location(lat, lon) if near_me else None
    code = airline.strip().upper() if airline else None
    wanted_type = type_code.strip().upper() if type_code else None

    matches = []
    for a in snapshot.aircraft:
        if a.on_ground:
            continue
        if code and world_mod.callsign_prefix(a) != code:
            continue
        if wanted_type and (a.type_code or "").upper() != wanted_type:
            continue
        if military_only and not a.is_military:
            continue
        altitude = a.altitude_ft
        if min_altitude_ft is not None and (altitude is None or altitude < min_altitude_ft):
            continue
        if max_altitude_ft is not None and (altitude is None or altitude > max_altitude_ft):
            continue
        speed = a.ground_speed_kt
        if min_speed_kt is not None and (speed is None or speed < min_speed_kt):
            continue
        if max_speed_kt is not None and (speed is None or speed > max_speed_kt):
            continue

        row = {
            "callsign": a.callsign, "registration": a.registration,
            "type": a.type_code, "operator": a.operator, "year": a.year,
            "altitude_ft": round(altitude) if altitude is not None else None,
            "ground_speed_kt": speed,
            "climb_state": _climb_state(a.vertical_rate_fpm),
            "military": a.is_military,
            "position": {"lat": a.lat, "lon": a.lon},
        }
        if obs and a.lat is not None and a.lon is not None:
            s = describe(obs[0], obs[1], a.lat, a.lon, altitude)
            if s.ground_km > within_km:
                continue
            row["distance_km"] = round(s.ground_km, 1)
            row["look_direction"] = s.look_direction
            row["elevation_deg"] = round(s.elevation_deg, 1)
        matches.append(row)

    matches.sort(key=lambda r: r.get("distance_km", -(r["altitude_ft"] or 0)))
    return {
        "matched": len(matches),
        "returned": min(len(matches), limit),
        "filters": {k: v for k, v in {
            "airline": code, "type": wanted_type,
            "min_altitude_ft": min_altitude_ft, "max_altitude_ft": max_altitude_ft,
            "min_speed_kt": min_speed_kt, "max_speed_kt": max_speed_kt,
            "military_only": military_only or None,
            "within_km": within_km if near_me else None}.items() if v is not None},
        "aircraft": matches[:limit],
        "snapshot_age_seconds": round(snapshot.age_seconds),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }


@_tool("Global Stats")
async def global_stats() -> dict:
    """How much is flying right now, worldwide: totals, busiest types and airlines.

    A single shared snapshot of everything volunteer receivers can hear. Note this is
    always an undercount of real global traffic — oceans, Africa and central Asia have
    little to no receiver coverage, so those aircraft are simply absent.
    """
    snapshot = await world().sweep()
    if snapshot.error:
        return _unavailable(snapshot.error)

    airborne = [a for a in snapshot.aircraft if not a.on_ground]
    by_type: dict[str, int] = {}
    by_airline: dict[str, int] = {}
    bands = {"below_10k": 0, "10k_to_30k": 0, "above_30k": 0}

    for a in airborne:
        if a.type_code:
            by_type[a.type_code] = by_type.get(a.type_code, 0) + 1
        prefix = world_mod.callsign_prefix(a)
        if prefix:
            by_airline[prefix] = by_airline.get(prefix, 0) + 1
        altitude = a.altitude_ft or 0
        if altitude < 10000:
            bands["below_10k"] += 1
        elif altitude < 30000:
            bands["10k_to_30k"] += 1
        else:
            bands["above_30k"] += 1

    top = lambda d, n: dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])  # noqa: E731
    return {
        "airborne": len(airborne),
        "on_ground": len(snapshot.aircraft) - len(airborne),
        "total_tracked": len(snapshot.aircraft),
        "military_airborne": sum(1 for a in airborne if a.is_military),
        "altitude_bands": bands,
        "busiest_types": top(by_type, 15),
        "busiest_airlines": top(by_airline, 15),
        "snapshot_age_seconds": round(snapshot.age_seconds),
        "undercount_warning": (
            "This counts only aircraft heard by volunteer receivers. Real global traffic "
            "is substantially higher — oceanic, African and central Asian airspace is "
            "largely uncovered. Treat this as 'what the network can see', not a census."),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }


@_tool("Busiest Airports")
async def busiest_airports(limit: int = 15, country: Optional[str] = None) -> dict:
    """Which major airports have the most traffic around them right now.

    Counts aircraft on the ground or manoeuvring near each of ~1,150 major airports,
    from one shared global snapshot. Optionally filter to a two-letter country code
    such as US, GB or IN.

    This measures observed activity, not scheduled movements, and it favours airports in
    well-covered regions — a busy airport with no nearby receivers will look quiet.
    """
    snapshot = await world().sweep()
    if snapshot.error:
        return _unavailable(snapshot.error)

    fields = airport_data.MAJOR_AIRPORTS
    if country:
        code = country.strip().upper()
        fields = tuple(f for f in fields if f.country == code)
        if not fields:
            return {"error": f"no major airports known for country {code!r}"}

    # Bucket aircraft into a coarse grid first, so this is not 6,000 x 1,150 comparisons.
    grid: dict[tuple[int, int], list] = {}
    for a in snapshot.aircraft:
        if a.lat is None or a.lon is None:
            continue
        if (a.altitude_ft or 0) > airports.PATTERN_CEILING_FT and not a.on_ground:
            continue
        grid.setdefault((round(a.lat), round(a.lon)), []).append(a)

    # Assign each aircraft to its NEAREST airport only. Counting it for every field
    # within range double-counts across neighbours — San Francisco and Oakland are 20 km
    # apart, and a shared radius gave them identical totals, which is plainly wrong.
    tally: dict[str, dict[str, int]] = {}
    for cell, aircraft in grid.items():
        candidates = [f for f in fields
                      if abs(round(f.lat) - cell[0]) <= 1 and abs(round(f.lon) - cell[1]) <= 1]
        if not candidates:
            continue
        for a in aircraft:
            best, best_km = None, airports.NEAR_FIELD_KM
            for f in candidates:
                km = haversine_km(f.lat, f.lon, a.lat, a.lon)
                if km < best_km:
                    best, best_km = f, km
            if best is None:
                continue
            row = tally.setdefault(best.icao, {"on_ground": 0, "airborne_nearby": 0})
            row["on_ground" if a.on_ground else "airborne_nearby"] += 1

    by_icao = {f.icao: f for f in fields}
    counted = [{"icao": icao, "iata": by_icao[icao].iata, "name": by_icao[icao].name,
                "country": by_icao[icao].country, **counts,
                "total": counts["on_ground"] + counts["airborne_nearby"]}
               for icao, counts in tally.items()]
    counted.sort(key=lambda r: -r["total"])

    return {
        "airports_ranked": len(counted),
        "airports": counted[:limit],
        "snapshot_age_seconds": round(snapshot.age_seconds),
        "method": (
            f"Each aircraft is counted once, at its nearest major airport within "
            f"{airports.NEAR_FIELD_KM:.0f} km, either on the ground or below "
            f"{airports.PATTERN_CEILING_FT:.0f} ft. Observed activity, not scheduled "
            "movements, and biased toward regions with dense volunteer receiver "
            "coverage — a busy airport with no receivers nearby will look quiet."),
        "attribution": ["adsb.lol (ODbL 1.0)", "OurAirports (public domain)"],
    }


# ── Tools: context ───────────────────────────────────────────────────────────


@_tool("Viewing Conditions")
async def viewing_conditions(lat: Optional[float] = None,
                             lon: Optional[float] = None) -> dict:
    """Cloud, visibility and daylight — whether it's worth looking up at all."""
    obs_lat, obs_lon = resolve_location(lat, lon)
    c = await weather.current(obs_lat, obs_lon)
    return {
        "summary": c.summary,
        "cloud_cover_pct": c.cloud_cover_pct,
        "low_cloud_pct": c.cloud_base_low_pct,
        "visibility_km": c.visibility_km,
        "daylight": c.is_daylight,
        "precipitation_mm": c.precipitation_mm,
        "wind_speed_kt": c.wind_speed_kt,
        "wind_direction_deg": c.wind_direction_deg,
        "temperature_c": c.temperature_c,
        "good_for_spotting": c.permits_viewing,
        "note": ("Even under overcast, aircraft below about 4,000 ft are usually under "
                 "the cloud deck and still visible."),
        "attribution": ["Open-Meteo"],
    }


@_tool("Feed Health")
async def feed_health() -> dict:
    """Per-source health: latency, failures, and whether a circuit breaker is open.

    These are volunteer-run services with no SLA. When one is down the others cover it,
    which is why three are queried rather than one.
    """
    report = {}
    for name, h in feeds().health_report().items():
        report[name] = {
            "successes": h.successes,
            "failures": h.failures,
            "consecutive_failures": h.consecutive_failures,
            "circuit_open": h.circuit_open,
            "last_latency_s": round(h.last_latency, 3) if h.last_latency else None,
            "last_error": h.last_error,
        }
    return {"sources": report,
            "note": "Merging three feeds also yields roughly 15% more aircraft than the "
                    "best single source, so a degraded source costs coverage, not just "
                    "redundancy."}


# ── Tools: history ───────────────────────────────────────────────────────────


@_tool("Sighting History")
async def sighting_history(registration: Optional[str] = None,
                           hex_id: Optional[str] = None, limit: int = 20) -> dict:
    """Every recorded pass of one aircraft over your location, most recent first."""
    if registration and not is_valid_registration(registration):
        return {"error": f"malformed registration: {registration!r}"}
    if hex_id and not is_valid_hex(hex_id):
        return {"error": f"malformed hex: {hex_id!r}"}
    if not registration and not hex_id:
        return {"error": "give a registration or a hex"}

    rows = store().history_for(hex_id=hex_id, registration=registration, limit=limit)
    return {
        "passes": len(rows),
        "history": rows,
        "note": ("History only covers what this machine has recorded — it starts when "
                 "you first ran SkyGlance, not when the aircraft first flew."),
    }


@_tool("Is This New?")
async def is_this_new(hex_id: Optional[str] = None, registration: Optional[str] = None,
                      type_code: Optional[str] = None) -> dict:
    """Have you seen this airframe, or this type, over your location before?"""
    s = store()
    out: dict[str, Any] = {}
    if hex_id:
        if not is_valid_hex(hex_id):
            return {"error": f"malformed hex: {hex_id!r}"}
        first = s.is_first_sighting(hex_id)
        out["airframe_is_new"] = first
        if not first:
            out["previous_passes"] = len(s.history_for(hex_id=hex_id, limit=1000))
    if registration:
        if not is_valid_registration(registration):
            return {"error": f"malformed registration: {registration!r}"}
        seen = s.history_for(registration=registration, limit=1000)
        out["registration_is_new"] = not seen
        out["registration_passes"] = len(seen)
    if type_code:
        count = s.type_seen_before(type_code)
        out["type_is_new"] = count == 0
        out["type_passes"] = count
    if not out:
        return {"error": "give a hex, registration, or type_code"}
    return out


@_tool("My Records")
async def my_records() -> dict:
    """Your personal extremes: closest, lowest, most directly overhead, busiest day."""
    r = store().records()
    if r["total_passes"] == 0:
        return {"note": ("Nothing recorded yet. Set SKYGLANCE_HOME_LAT and "
                         "SKYGLANCE_HOME_LON so the background poller can build history, "
                         "or call whats_overhead a few times.")}
    return r


@_tool("Spotting Stats")
async def spotting_stats(days: Optional[int] = None) -> dict:
    """How much you've seen: totals, most common types, busiest hours."""
    since = (time.time() - days * 86400) if days else None
    stats = store().stats(since_epoch=since)
    stats["window"] = f"last {days} days" if days else "all time"
    stats["totals"] = store().counts()
    return stats


@_tool("History Recorder Status")
async def poller_status() -> dict:
    """Whether background history recording is running, and what it has captured."""
    _maybe_start_poller()
    if _poller is None:
        configured = home()
        return {
            "running": False,
            "reason": ("SKYGLANCE_POLL is 0" if os.environ.get("SKYGLANCE_POLL") == "0"
                       else "SKYGLANCE_HOME_LAT/SKYGLANCE_HOME_LON are not set"),
            "home_configured": configured is not None,
            "database": str(store().path),
            "totals": store().counts(),
        }
    return _poller.status() | {"database": str(store().path),
                               "totals": store().counts()}


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="SkyGlance — live flight data MCP server")
    parser.add_argument("--http", action="store_true", help="run as an HTTP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    configured = home()
    # stderr, always: in stdio mode stdout is the JSON-RPC channel.
    print(f"SkyGlance starting ({'http' if args.http else 'stdio'})", file=sys.stderr)
    if configured:
        print(f"  home {configured[0]}, {configured[1]} — background history on",
              file=sys.stderr)
    else:
        print("  no SKYGLANCE_HOME_LAT/LON set — history recording off", file=sys.stderr)

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
