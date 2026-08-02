"""Where to look, and when.

A radius filter is the wrong model. An airliner at 38,000 ft that is 5 km away sits
62 degrees up in the sky; a helicopter at 1,000 ft that is 5 km away sits 3 degrees up,
behind a tree. What matters is the elevation angle, the bearing to look, and when the
aircraft will be closest.

Ported from reference/overhead.mjs in the skyglance-mac repo, which passes 11
known-answer tests. tests/test_geometry.py carries those same cases across, so this
port is held to the answers the original produces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

R_EARTH_KM = 6371.0
FT_TO_KM = 0.0003048
KT_TO_KMS = 0.000514444

#: Past roughly this slant range an airliner (~40 m) stops being resolvable to the
#: naked eye in clear air.
NAKED_EYE_LIMIT_KM = 50.0

#: Elevation at or above which something is "overhead" rather than merely visible.
OVERHEAD_ELEVATION_DEG = 60.0

_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    return 2 * R_EARTH_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing from observer to target. 0 = north, 90 = east."""
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(d_lon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(bearing: float) -> str:
    """Bearing to a 16-point compass label."""
    return _COMPASS[round(bearing / 22.5) % 16]


@dataclass(frozen=True)
class ClosestApproach:
    seconds_away: float
    approaching: bool
    ground_km: float
    altitude_ft: int
    elevation_deg: float
    bearing_deg: float


def closest_approach(
    obs_lat: float,
    obs_lon: float,
    lat: float,
    lon: float,
    ground_speed_kt: Optional[float],
    track_deg: Optional[float],
    altitude_ft: Optional[float] = None,
    vertical_rate_fpm: Optional[float] = None,
) -> Optional[ClosestApproach]:
    """Closest point of approach along the aircraft's current heading.

    Flat local approximation — good to well under a percent inside ~100 km.

    Straight-line extrapolation is excellent to 30s and worthless by 240s (measured:
    77% of aircraft more than 2 km from prediction at 240s). Callers must not present
    this beyond ~90 seconds; see PREDICTION_HORIZON_S in server.py.
    """
    if ground_speed_kt is None or track_deg is None:
        return None

    # Local east/north offsets in km.
    east = haversine_km(obs_lat, obs_lon, obs_lat, lon) * (-1 if lon < obs_lon else 1)
    north = haversine_km(obs_lat, obs_lon, lat, obs_lon) * (-1 if lat < obs_lat else 1)

    speed_kms = ground_speed_kt * KT_TO_KMS
    v_east = speed_kms * math.sin(math.radians(track_deg))
    v_north = speed_kms * math.cos(math.radians(track_deg))
    v_sq = v_east ** 2 + v_north ** 2
    if v_sq == 0:
        return None

    # Minimise |p + v*t| over t.
    t = -(east * v_east + north * v_north) / v_sq
    c_east = east + v_east * t
    c_north = north + v_north * t
    ground_km = math.hypot(c_east, c_north)

    # baro_rate is ft/min; t is seconds.
    alt_at_cpa = max(0.0, (altitude_ft or 0) + ((vertical_rate_fpm or 0) / 60) * t)

    return ClosestApproach(
        seconds_away=t,
        approaching=t > 0,
        ground_km=ground_km,
        altitude_ft=round(alt_at_cpa),
        elevation_deg=90.0 if ground_km == 0 else math.degrees(
            math.atan2(alt_at_cpa * FT_TO_KM, ground_km)),
        bearing_deg=(math.degrees(math.atan2(c_east, c_north)) + 360) % 360,
    )


@dataclass(frozen=True)
class Sighting:
    """One aircraft, as seen from one place on the ground."""
    ground_km: float
    slant_km: float
    elevation_deg: float
    bearing_deg: float
    look_direction: str
    overhead: bool
    naked_eye_plausible: bool
    cpa: Optional[ClosestApproach]


def describe(
    obs_lat: float,
    obs_lon: float,
    lat: float,
    lon: float,
    altitude_ft: Optional[float],
    ground_speed_kt: Optional[float] = None,
    track_deg: Optional[float] = None,
    vertical_rate_fpm: Optional[float] = None,
) -> Sighting:
    """Everything needed to decide whether to walk outside and look up."""
    ground_km = haversine_km(obs_lat, obs_lon, lat, lon)
    # alt_baro can read slightly negative near sea level in low pressure, which would
    # otherwise produce a negative elevation angle.
    alt_km = max(0.0, altitude_ft or 0) * FT_TO_KM
    elevation = 90.0 if ground_km == 0 else math.degrees(math.atan2(alt_km, ground_km))
    slant_km = math.hypot(ground_km, alt_km)
    bearing = bearing_deg(obs_lat, obs_lon, lat, lon)

    return Sighting(
        ground_km=ground_km,
        slant_km=slant_km,
        elevation_deg=elevation,
        bearing_deg=bearing,
        look_direction=compass(bearing),
        overhead=elevation >= OVERHEAD_ELEVATION_DEG,
        naked_eye_plausible=slant_km <= NAKED_EYE_LIMIT_KM,
        cpa=closest_approach(obs_lat, obs_lon, lat, lon, ground_speed_kt, track_deg,
                             altitude_ft, vertical_rate_fpm),
    )


def rank_key(s: Sighting) -> float:
    """Rank by what a person actually cares about: what's about to be overhead."""
    if s.overhead:
        return 0.0
    if s.cpa and s.cpa.approaching and s.cpa.elevation_deg >= 40:
        return 1 + s.cpa.seconds_away / 10000
    return 2 + (90 - s.elevation_deg) / 90
