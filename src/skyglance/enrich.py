"""Turning a hex code into an aircraft: type, owner, route, photograph.

Three volunteer services, tried in order and cached forever:

    adsbdb.com      hex -> aircraft, callsign -> route with airport coordinates
    hexdb.io        the same, as a fallback when adsbdb misses or is down
    planespotters   registration -> photograph + photographer credit

Ported from app/Sources/OverheadKit/Enrichment.swift. Two rules carried across:

1. **Cache misses too.** Hex -> aircraft type never changes, so it's one lookup per
   aircraft for all time. Caching negative results matters just as much, or unknown
   aircraft get re-looked-up on every single frame.
2. **Validate every identifier before it enters a URL path.** These values arrive as
   unvalidated JSON from a third-party feed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

import httpx

from .feeds import USER_AGENT
from .store import Store

log = logging.getLogger("skyglance.enrich")

REQUEST_TIMEOUT_S = 8.0

#: Half a second between calls to any one host: enough to stop a burst, small enough
#: not to be felt on a lookup a human is actively waiting for. Keyed by host because
#: these are unrelated services that must not queue behind one another.
MIN_HOST_INTERVAL_S = 0.5
MAX_HOST_WAIT_S = 3.0


def is_well_formed(value: str, extra: str = "") -> bool:
    """ASCII alphanumerics plus `extra`, 1-16 characters.

    Without this, a callsign of `../../v0/aircraft/x` walks the URL to a different
    endpoint, and one containing `?` or `#` rewrites the request. Both were verified
    to work against the original Swift implementation. The scheme and host are fixed
    so this is not SSRF to another domain, but a compromised upstream should not get
    to choose our paths, and malformed values should not become permanent cache keys.

    Rejecting is safe: the caller returns nothing and the aircraft simply shows no
    extra detail. 16 is generous; the longest real value is an 8-character callsign.
    """
    if not 1 <= len(value) <= 16:
        return False
    return all(c.isascii() and (c.isalnum() or c in extra) for c in value)


# adsb.lol prefixes non-ICAO (TIS-B) targets with `~`, so excluding it would silently
# drop enrichment for a whole class of aircraft.
def is_valid_hex(v: str) -> bool:
    return is_well_formed(v, "~")


def is_valid_callsign(v: str) -> bool:
    return is_well_formed(v)


def is_valid_registration(v: str) -> bool:
    # Registrations carry hyphens: G-EUPT, VH-OQA.
    return is_well_formed(v, "-")


@dataclass
class AircraftIdentity:
    registration: Optional[str] = None
    type_code: Optional[str] = None
    type_name: Optional[str] = None
    manufacturer: Optional[str] = None
    owner: Optional[str] = None
    country: Optional[str] = None
    source: Optional[str] = None
    #: adsbdb reports this as mode_s. Carrying it lets a registration resolve to a hex
    #: without a live lookup, which matters for parked aircraft.
    hex: Optional[str] = None


@dataclass
class Airport:
    icao: Optional[str] = None
    iata: Optional[str] = None
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class Route:
    airline: Optional[str] = None
    origin: Optional[Airport] = None
    destination: Optional[Airport] = None

    @property
    def complete(self) -> bool:
        """A half-route reads as broken, so only surface both ends or neither."""
        return self.origin is not None and self.destination is not None


@dataclass
class Photo:
    thumbnail_url: str
    photographer: str
    link: Optional[str] = None


class Enricher:
    def __init__(self, store: Store, client: Optional[httpx.AsyncClient] = None) -> None:
        self._store = store
        self._client = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, host_key: str, url: str) -> Optional[dict]:
        async with self._lock:
            now = time.monotonic()
            slot = max(self._next_slot.get(host_key, now), now)
            wait = slot - now
            if wait > MAX_HOST_WAIT_S:
                return None
            self._next_slot[host_key] = slot + MIN_HOST_INTERVAL_S
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await self._client.get(url)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("enrichment %s failed: %s", host_key, exc)
            return None

    # ── Aircraft identity ────────────────────────────────────────────────────

    async def aircraft(self, hex_id: str) -> Optional[AircraftIdentity]:
        if not is_valid_hex(hex_id):
            return None
        key = hex_id.lower()

        cached = self._store.get_enrichment("aircraft", key)
        if cached is not None:
            return AircraftIdentity(**cached) if cached else None

        identity = await self._adsbdb_aircraft(key) or await self._hexdb_aircraft(key)
        # Store the miss too, or unknown aircraft are retried on every frame.
        self._store.put_enrichment("aircraft", key, asdict(identity) if identity else {})
        return identity

    async def aircraft_by_registration(self, registration: str) -> Optional[AircraftIdentity]:
        """adsbdb resolves a registration directly, so identifying a parked aircraft
        does not require catching it airborne first.

        Without this, `identify_aircraft(registration=...)` silently returned nothing
        whenever the aircraft happened to be on the ground — which for any given
        airframe is most of the time.
        """
        if not is_valid_registration(registration):
            return None
        key = registration.upper()

        cached = self._store.get_enrichment("aircraft_reg", key)
        if cached is not None:
            return AircraftIdentity(**cached) if cached else None

        identity = await self._adsbdb_aircraft(key)
        self._store.put_enrichment("aircraft_reg", key,
                                   asdict(identity) if identity else {})
        return identity

    async def _adsbdb_aircraft(self, hex_id: str) -> Optional[AircraftIdentity]:
        data = await self._get("adsbdb", f"https://api.adsbdb.com/v0/aircraft/{hex_id}")
        try:
            ac = data["response"]["aircraft"]  # type: ignore[index]
        except (TypeError, KeyError):
            return None
        if not isinstance(ac, dict):
            return None
        return AircraftIdentity(
            registration=ac.get("registration"),
            type_code=ac.get("icao_type"),
            type_name=ac.get("type"),
            manufacturer=ac.get("manufacturer"),
            owner=ac.get("registered_owner"),
            country=ac.get("registered_owner_country_name"),
            source="adsbdb",
            hex=(ac.get("mode_s") or "").lower() or None,
        )

    async def _hexdb_aircraft(self, hex_id: str) -> Optional[AircraftIdentity]:
        data = await self._get("hexdb", f"https://hexdb.io/api/v1/aircraft/{hex_id}")
        if not isinstance(data, dict) or not data.get("Registration"):
            return None
        return AircraftIdentity(
            registration=data.get("Registration"),
            type_code=data.get("ICAOTypeCode"),
            type_name=data.get("Type"),
            manufacturer=data.get("Manufacturer"),
            owner=data.get("RegisteredOwners"),
            source="hexdb",
        )

    # ── Route ────────────────────────────────────────────────────────────────

    async def route(self, callsign: str) -> Optional[Route]:
        if not is_valid_callsign(callsign):
            return None
        key = callsign.upper()

        cached = self._store.get_enrichment("route", key)
        if cached is not None:
            return _route_from_dict(cached) if cached else None

        data = await self._get("adsbdb", f"https://api.adsbdb.com/v0/callsign/{key}")
        route: Optional[Route] = None
        try:
            fr = data["response"]["flightroute"]  # type: ignore[index]
            route = Route(
                airline=(fr.get("airline") or {}).get("name"),
                origin=_airport_from_dict(fr.get("origin")),
                destination=_airport_from_dict(fr.get("destination")),
            )
        except (TypeError, KeyError):
            route = None

        self._store.put_enrichment("route", key, _route_to_dict(route) if route else {})
        return route

    # ── Photo ────────────────────────────────────────────────────────────────

    async def photo(self, registration: str) -> Optional[Photo]:
        if not is_valid_registration(registration):
            return None
        key = registration.upper()

        cached = self._store.get_enrichment("photo", key)
        if cached is not None:
            return Photo(**cached) if cached else None

        data = await self._get(
            "planespotters", f"https://api.planespotters.net/pub/photos/reg/{key}")
        photo: Optional[Photo] = None
        try:
            first = data["photos"][0]  # type: ignore[index]
            thumb = (first.get("thumbnail_large") or first.get("thumbnail") or {}).get("src")
            if thumb:
                # Attribution is a condition of use, so the credit travels with the URL.
                photo = Photo(thumbnail_url=thumb,
                              photographer=(first.get("photographer") or "unknown").strip(),
                              link=first.get("link"))
        except (TypeError, KeyError, IndexError):
            photo = None

        self._store.put_enrichment("photo", key, asdict(photo) if photo else {})
        return photo


def _airport_from_dict(d: Optional[dict]) -> Optional[Airport]:
    if not isinstance(d, dict):
        return None
    return Airport(icao=d.get("icao_code"), iata=d.get("iata_code"),
                   name=d.get("name"), lat=d.get("latitude"), lon=d.get("longitude"))


def _route_to_dict(r: Route) -> dict:
    return {"airline": r.airline,
            "origin": asdict(r.origin) if r.origin else None,
            "destination": asdict(r.destination) if r.destination else None}


def _route_from_dict(d: dict) -> Route:
    return Route(airline=d.get("airline"),
                 origin=Airport(**d["origin"]) if d.get("origin") else None,
                 destination=Airport(**d["destination"]) if d.get("destination") else None)
