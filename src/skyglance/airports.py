"""Airport lookup, and what is moving around one right now.

There is no free "departure board" — no ADS-B feed knows a schedule, a gate, or which
airport a flight left. What the data *does* support is direct observation: query the
circle around an airport and classify each aircraft by how it is moving.

    on the ground     alt_baro == "ground"
    departing         low, close, climbing hard
    arriving          low, close, descending
    overflying        high, or not descending toward the field

That is genuinely useful and genuinely different from a departure board, and the
distinction is stated in the output so nobody mistakes one for the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .enrich import is_well_formed
from .feeds import USER_AGENT, Aircraft
from .geometry import haversine_km

log = logging.getLogger("skyglance.airports")

AIRPORT_ENDPOINT = "https://api.adsb.lol/api/0/airport/{icao}"
AIRLINE_ENDPOINT = "https://api.adsbdb.com/v0/airline/{code}"
REQUEST_TIMEOUT_S = 10.0

#: Within this range of the field, a climbing or descending aircraft is plausibly using
#: it. Beyond it, low traffic is more likely transiting to a neighbouring airport — a
#: real risk in metro areas like New York with three majors inside 30 km.
NEAR_FIELD_KM = 30.0

#: Above this, an aircraft is passing over rather than using the airport.
PATTERN_CEILING_FT = 10000.0

#: Climb and descent rates that distinguish a departure or approach from level flight.
CLIMB_FPM = 400.0
DESCENT_FPM = -400.0


def is_valid_icao(code: str) -> bool:
    """ICAO airport codes are four alphanumerics: KEWR, EGLL, VABB."""
    return len(code) == 4 and is_well_formed(code)


@dataclass(frozen=True)
class AirportRef:
    icao: str
    iata: Optional[str]
    name: Optional[str]
    location: Optional[str]
    country: Optional[str]
    lat: float
    lon: float
    elevation_ft: Optional[float]


async def lookup(icao: str, client: Optional[httpx.AsyncClient] = None
                 ) -> Optional[AirportRef]:
    """Resolve an ICAO code to a position. Airports don't move, so callers cache forever."""
    if not is_valid_icao(icao):
        return None
    owns = client is None
    client = client or httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT},
        follow_redirects=False)
    try:
        response = await client.get(AIRPORT_ENDPOINT.format(icao=icao.upper()))
        if response.status_code != 200:
            return None
        d = response.json()
        if not isinstance(d, dict) or d.get("lat") is None:
            return None
        return AirportRef(
            icao=d.get("icao") or icao.upper(),
            iata=d.get("iata"),
            name=d.get("name"),
            location=d.get("location"),
            country=d.get("countryiso2"),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            elevation_ft=d.get("alt_feet"),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("airport lookup failed for %s: %s", icao, exc)
        return None
    finally:
        if owns:
            await client.aclose()


async def airline(code: str, client: Optional[httpx.AsyncClient] = None
                  ) -> Optional[dict[str, Any]]:
    """ICAO airline code (JBU, BAW, UAE) to name, IATA code and radio callsign."""
    if not is_well_formed(code):
        return None
    owns = client is None
    client = client or httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT},
        follow_redirects=False)
    try:
        response = await client.get(AIRLINE_ENDPOINT.format(code=code.upper()))
        if response.status_code != 200:
            return None
        payload = (response.json() or {}).get("response")
        if isinstance(payload, list) and payload:
            return payload[0]
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("airline lookup failed for %s: %s", code, exc)
        return None
    finally:
        if owns:
            await client.aclose()


def classify(aircraft: Aircraft, field: AirportRef) -> tuple[str, float]:
    """Bucket one aircraft relative to the field. Returns (bucket, distance_km)."""
    if aircraft.lat is None or aircraft.lon is None:
        return "unknown", float("inf")
    distance = haversine_km(field.lat, field.lon, aircraft.lat, aircraft.lon)

    if aircraft.on_ground:
        return "on_ground", distance

    altitude = aircraft.altitude_ft or 0
    rate = aircraft.vertical_rate_fpm

    if distance > NEAR_FIELD_KM or altitude > PATTERN_CEILING_FT:
        return "overflying", distance
    if rate is None:
        return "nearby_level", distance
    if rate >= CLIMB_FPM:
        return "departing", distance
    if rate <= DESCENT_FPM:
        return "arriving", distance
    return "nearby_level", distance


def describe_activity(aircraft_list: list[Aircraft], field: AirportRef,
                      limit: int = 15) -> dict[str, Any]:
    buckets: dict[str, list[dict]] = {
        "departing": [], "arriving": [], "on_ground": [],
        "nearby_level": [], "overflying": [],
    }

    for a in aircraft_list:
        bucket, distance = classify(a, field)
        if bucket == "unknown":
            continue
        buckets[bucket].append({
            "callsign": a.callsign,
            "registration": a.registration,
            "type": a.type_code,
            "operator": a.operator,
            "hex": a.hex,
            "altitude_ft": round(a.altitude_ft) if a.altitude_ft is not None else None,
            "vertical_rate_fpm": a.vertical_rate_fpm,
            "ground_speed_kt": a.ground_speed_kt,
            "distance_km": round(distance, 1),
        })

    for rows in buckets.values():
        rows.sort(key=lambda r: r["distance_km"])

    return {
        "airport": {
            "icao": field.icao, "iata": field.iata, "name": field.name,
            "location": field.location, "country": field.country,
            "lat": field.lat, "lon": field.lon,
            "elevation_ft": field.elevation_ft,
        },
        "counts": {k: len(v) for k, v in buckets.items()},
        "departing": buckets["departing"][:limit],
        "arriving": buckets["arriving"][:limit],
        "on_ground": buckets["on_ground"][:limit],
        "overflying": buckets["overflying"][:limit],
        "how_this_is_derived": (
            "Observed movement, NOT a departure board. Aircraft are bucketed by where "
            "they are and how they are climbing or descending — nothing here knows a "
            "schedule, a gate, or which airport a flight is actually bound for. "
            f"'Departing' means climbing above {CLIMB_FPM:.0f} fpm within "
            f"{NEAR_FIELD_KM:.0f} km and below {PATTERN_CEILING_FT:.0f} ft; a nearby "
            "airport's traffic can be misattributed in a busy metro area."),
        "attribution": ["adsb.lol (ODbL 1.0)"],
    }
