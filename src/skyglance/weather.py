"""Can you actually see it? Free, no API key, and half the product.

Telling someone to look up at an overcast sky is a broken experience. Open-Meteo gives
cloud cover, visibility and daylight; this one filter is the difference between a useful
answer and a spammy one.

Ported from the conditions block in reference/overhead.mjs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from .feeds import USER_AGENT, coarsen

log = logging.getLogger("skyglance.weather")

#: Above this cloud cover, assume aircraft at cruise are hidden.
OVERCAST_THRESHOLD = 80
BROKEN_THRESHOLD = 40

#: Below this altitude an aircraft is likely under the cloud deck and still visible
#: even when the sky is reported overcast.
UNDER_THE_DECK_FT = 4000


@dataclass(frozen=True)
class Conditions:
    cloud_cover_pct: Optional[int]
    visibility_km: Optional[float]
    is_daylight: Optional[bool]
    summary: str
    #: Extras that make this comparable to the weather layers FR24 sells: precipitation
    #: right now, and wind, which is what actually decides runway direction.
    precipitation_mm: Optional[float] = None
    wind_speed_kt: Optional[float] = None
    wind_direction_deg: Optional[int] = None
    temperature_c: Optional[float] = None
    cloud_base_low_pct: Optional[int] = None

    @property
    def permits_viewing(self) -> bool:
        if self.cloud_cover_pct is None:
            return True
        return self.cloud_cover_pct <= OVERCAST_THRESHOLD

    def permits_viewing_at(self, altitude_ft: Optional[float]) -> bool:
        """Overcast hides cruise traffic but not an airliner on final approach."""
        if self.permits_viewing:
            return True
        return altitude_ft is not None and altitude_ft <= UNDER_THE_DECK_FT


UNKNOWN = Conditions(None, None, None, "conditions unavailable")


async def current(lat: float, lon: float,
                  client: Optional[httpx.AsyncClient] = None) -> Conditions:
    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=6.0, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    try:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": coarsen(lat), "longitude": coarsen(lon),
                    "current": ("cloud_cover,visibility,is_day,precipitation,"
                                "wind_speed_10m,wind_direction_10m,temperature_2m,"
                                "cloud_cover_low"),
                    "wind_speed_unit": "kn"})
        if response.status_code != 200:
            return UNKNOWN
        cur = response.json().get("current") or {}
    except Exception as exc:  # noqa: BLE001 — weather is a nicety, never a failure
        log.debug("weather lookup failed: %s", exc)
        return UNKNOWN
    finally:
        if owns_client:
            await client.aclose()

    cloud = cur.get("cloud_cover")
    visibility_m = cur.get("visibility")
    is_day = cur.get("is_day")

    if cloud is None:
        return UNKNOWN
    if cloud > OVERCAST_THRESHOLD:
        seeing = "overcast — you probably won't see anything above the deck"
    elif cloud > BROKEN_THRESHOLD:
        seeing = "broken cloud — hit and miss"
    else:
        seeing = "clear enough to spot aircraft"
    if is_day == 0:
        seeing += "; dark, so only landing lights on low aircraft"

    precipitation = cur.get("precipitation")
    if isinstance(precipitation, (int, float)) and precipitation > 0:
        seeing += f"; {precipitation} mm of precipitation falling"

    vis_km = round(visibility_m / 1000, 1) if isinstance(visibility_m, (int, float)) else None

    def _num(key):
        v = cur.get(key)
        return v if isinstance(v, (int, float)) else None

    wind_dir = _num("wind_direction_10m")
    return Conditions(
        cloud_cover_pct=int(cloud), visibility_km=vis_km,
        is_daylight=bool(is_day) if is_day is not None else None,
        summary=seeing,
        precipitation_mm=_num("precipitation"),
        wind_speed_kt=_num("wind_speed_10m"),
        wind_direction_deg=int(wind_dir) if wind_dir is not None else None,
        temperature_c=_num("temperature_2m"),
        cloud_base_low_pct=int(_num("cloud_cover_low")) if _num("cloud_cover_low") is not None else None,
    )
