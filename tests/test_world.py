"""The global sweep. This is the one request that costs a volunteer service real money
(~3.7 MB), so the caching and single-flight behaviour is the thing under test."""

import asyncio

import httpx
import pytest

from skyglance import world as world_mod
from skyglance.feeds import Aircraft, FeedClient
from skyglance.world import WorldView, callsign_prefix


def _client(counter: dict, aircraft_count: int = 3):
    def handler(request):
        counter["fetches"] = counter.get("fetches", 0) + 1
        return httpx.Response(200, json={"ac": [
            {"hex": f"a{i:05x}", "lat": 40.0 + i, "lon": -74.0, "alt_baro": 30000,
             "flight": f"BAW{i}  "} for i in range(aircraft_count)]})
    return FeedClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestSweepCaching:
    async def test_first_call_fetches(self):
        counter = {}
        view = WorldView(_client(counter))
        snapshot = await view.sweep()
        assert len(snapshot.aircraft) == 3
        assert counter["fetches"] == 1

    async def test_second_call_within_the_window_uses_cache(self):
        """The whole point: never hit a volunteer service twice in a minute for this."""
        counter = {}
        view = WorldView(_client(counter))
        await view.sweep()
        await view.sweep()
        assert counter["fetches"] == 1

    async def test_force_bypasses_the_cache(self):
        counter = {}
        view = WorldView(_client(counter))
        await view.sweep()
        await view.sweep(force=True)
        assert counter["fetches"] == 2

    async def test_concurrent_callers_share_one_fetch(self):
        """Six simultaneous callers must not become six 3.7 MB requests."""
        counter = {}
        view = WorldView(_client(counter))
        await asyncio.gather(*(view.sweep() for _ in range(6)))
        assert counter["fetches"] == 1

    async def test_expired_cache_refetches(self):
        counter = {}
        view = WorldView(_client(counter))
        snapshot = await view.sweep()
        snapshot.captured_at -= world_mod.MIN_SWEEP_INTERVAL_S + 1
        await view.sweep()
        assert counter["fetches"] == 2

    async def test_minimum_interval_is_not_trivially_small(self):
        assert world_mod.MIN_SWEEP_INTERVAL_S >= 30


class TestSweepFailure:
    async def test_failure_with_no_cache_reports_error(self):
        client = FeedClient(httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))))
        snapshot = await WorldView(client).sweep()
        assert snapshot.aircraft == []
        assert snapshot.error is not None

    async def test_failure_falls_back_to_a_recent_cache(self):
        state = {"fail": False}

        def handler(request):
            if state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"ac": [
                {"hex": "abc123", "lat": 40.0, "lon": -74.0, "alt_baro": 30000}]})

        view = WorldView(FeedClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler))))
        good = await view.sweep()
        good.captured_at -= world_mod.MIN_SWEEP_INTERVAL_S + 1
        state["fail"] = True
        stale = await view.sweep()
        assert len(stale.aircraft) == 1

    async def test_positionless_aircraft_are_dropped(self):
        client = FeedClient(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ac": [
                {"hex": "a1", "lat": 40.0, "lon": -74.0},
                {"hex": "a2"}]}))))
        snapshot = await WorldView(client).sweep()
        assert [a.hex for a in snapshot.aircraft] == ["a1"]


class TestCallsignPrefix:
    @pytest.mark.parametrize("callsign,expected", [
        ("BAW117", "BAW"),
        ("UAE2VL", "UAE"),
        ("JBU87", "JBU"),
        ("DLH4RL", "DLH"),
    ])
    def test_airline_callsigns(self, callsign, expected):
        assert callsign_prefix(Aircraft(hex="x", callsign=callsign)) == expected

    @pytest.mark.parametrize("callsign", [
        "N520JB",    # general aviation uses the registration
        "G-EUPT",
        "BAW",       # too short
        "",
        None,
        "12345",
    ])
    def test_non_airline_callsigns_return_none(self, callsign):
        assert callsign_prefix(Aircraft(hex="x", callsign=callsign)) is None

    def test_lowercase_and_padding_handled(self):
        assert callsign_prefix(Aircraft(hex="x", callsign="baw117  ")) == "BAW"
