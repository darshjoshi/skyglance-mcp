"""Merge behaviour, breakers, and the two security properties the README claims.

The privacy and identifier tests exist because both are promises made to users, and a
promise without a test is a hope.
"""

import asyncio
import re

import httpx
import pytest

from skyglance import feeds
from skyglance.enrich import is_valid_callsign, is_valid_hex, is_valid_registration
from skyglance.feeds import Aircraft, FeedClient, coarsen, parse_aircraft


class TestParsing:
    def test_alt_baro_ground_string_does_not_crash(self):
        """The classic ADS-B parsing bug: alt_baro is "ground" on the ground."""
        a = parse_aircraft({"hex": "abc123", "alt_baro": "ground"}, "t")
        assert a is not None
        assert a.on_ground is True
        assert a.altitude_ft == 0.0

    def test_numeric_altitude(self):
        a = parse_aircraft({"hex": "abc123", "alt_baro": 32975}, "t")
        assert a.on_ground is False
        assert a.altitude_ft == 32975.0

    def test_callsign_is_stripped(self):
        """Callsigns are space-padded to 8 characters on the wire."""
        assert parse_aircraft({"hex": "a", "flight": "JBU87   "}, "t").callsign == "JBU87"

    def test_empty_callsign_becomes_none(self):
        assert parse_aircraft({"hex": "a", "flight": "        "}, "t").callsign is None

    def test_record_without_hex_is_dropped(self):
        assert parse_aircraft({"flight": "TEST"}, "t") is None

    def test_military_flag(self):
        assert parse_aircraft({"hex": "a", "dbFlags": 1}, "t").is_military is True
        assert parse_aircraft({"hex": "a", "dbFlags": 0}, "t").is_military is False

    def test_emergency_detection(self):
        assert parse_aircraft({"hex": "a", "squawk": "7700"}, "t").has_emergency is True
        assert parse_aircraft({"hex": "a", "emergency": "none"}, "t").has_emergency is False
        assert parse_aircraft({"hex": "a", "squawk": "1200"}, "t").has_emergency is False


class TestPrivacy:
    """No code path may reach a feed with an exact position."""

    def test_coarsen_rounds_to_about_a_kilometre(self):
        assert coarsen(40.689247) == 40.69
        assert coarsen(-74.174537) == -74.17

    @pytest.mark.parametrize("source", feeds.SOURCES, ids=lambda s: s.name)
    def test_every_source_url_carries_only_coarsened_coordinates(self, source):
        exact_lat, exact_lon = 40.6892494, -74.1745371
        url = source.build_url(exact_lat, exact_lon, 60)
        # The full-precision values must not appear anywhere in the URL.
        assert "40.6892494" not in url
        assert "74.1745371" not in url
        # And every numeric coordinate present must be 2dp or fewer.
        for match in re.findall(r"-?\d+\.\d+", url):
            decimals = len(match.split(".")[1])
            assert decimals <= 2, f"{source.name} leaked {match} ({decimals}dp) in {url}"

    def test_coarsening_is_applied_at_url_build_not_by_the_caller(self):
        """Callers pass exact coordinates; the source coarsens. That's the contract."""
        url = feeds.SOURCES[0].build_url(51.4700001, -0.4500001, 60)
        assert "51.47" in url and "51.4700001" not in url


class TestIdentifierValidation:
    """Both of these were verified exploitable against the original implementation."""

    @pytest.mark.parametrize("evil", [
        "../../v0/aircraft/x",
        "..%2f..%2fx",
        "abc?foo=1",
        "abc#frag",
        "abc/def",
        "a" * 17,
        "",
        "abc def",
    ])
    def test_path_traversal_and_query_injection_rejected(self, evil):
        assert is_valid_hex(evil) is False
        assert is_valid_callsign(evil) is False
        assert is_valid_registration(evil) is False

    def test_legitimate_values_accepted(self):
        assert is_valid_hex("a689b8")
        assert is_valid_hex("~abc123")       # adsb.lol prefixes TIS-B targets with ~
        assert is_valid_callsign("JBU87")
        assert is_valid_registration("N520JB")
        assert is_valid_registration("G-EUPT")   # registrations carry hyphens

    def test_hyphen_not_allowed_where_it_is_not_valid(self):
        assert is_valid_callsign("JBU-87") is False


class TestMerge:
    @staticmethod
    def _ac(hex_id, source, **kw):
        base = dict(hex=hex_id, lat=40.0, lon=-74.0, sources=[source])
        base.update(kw)
        return Aircraft(**base)

    def test_union_exceeds_any_single_source(self):
        merged = FeedClient._merge([
            ("s1", [self._ac("a", "s1"), self._ac("b", "s1")]),
            ("s2", [self._ac("b", "s2"), self._ac("c", "s2")]),
        ])
        assert {a.hex for a in merged} == {"a", "b", "c"}

    def test_freshest_position_wins(self):
        merged = FeedClient._merge([
            ("s1", [self._ac("a", "s1", seen_pos_s=30.0, altitude_ft=1000.0)]),
            ("s2", [self._ac("a", "s2", seen_pos_s=2.0, altitude_ft=2000.0)]),
        ])
        assert merged[0].altitude_ft == 2000.0

    def test_missing_fields_are_backfilled_from_the_other_source(self):
        """airplanes.live carries desc/ownOp that the others often omit."""
        merged = FeedClient._merge([
            ("s1", [self._ac("a", "s1", seen_pos_s=1.0, operator=None, type_code="B738")]),
            ("s2", [self._ac("a", "s2", seen_pos_s=9.0, operator="JetBlue", type_code=None)]),
        ])
        assert merged[0].type_code == "B738"      # from the fresher record
        assert merged[0].operator == "JetBlue"    # backfilled from the staler one

    def test_records_which_sources_saw_it(self):
        merged = FeedClient._merge([
            ("s1", [self._ac("a", "s1")]),
            ("s2", [self._ac("a", "s2")]),
        ])
        assert merged[0].sources == ["s1", "s2"]

    def test_positionless_records_dropped(self):
        merged = FeedClient._merge([("s1", [self._ac("a", "s1", lat=None, lon=None)])])
        assert merged == []


class TestCircuitBreaker:
    async def test_opens_after_three_consecutive_failures(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(500))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        for _ in range(feeds.FAILURES_BEFORE_OPEN):
            await client._get_aircraft("adsb.lol", "https://example.invalid/x")
        assert client.health_report()["adsb.lol"].circuit_open is True
        await client.aclose()

    async def test_two_failures_do_not_open_it(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(500))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        for _ in range(feeds.FAILURES_BEFORE_OPEN - 1):
            await client._get_aircraft("adsb.lol", "https://example.invalid/x")
        assert client.health_report()["adsb.lol"].circuit_open is False
        await client.aclose()

    async def test_success_resets_the_failure_streak(self):
        responses = [httpx.Response(500), httpx.Response(500),
                     httpx.Response(200, json={"ac": []})]
        transport = httpx.MockTransport(lambda r: responses.pop(0))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        for _ in range(3):
            await client._get_aircraft("adsb.lol", "https://example.invalid/x")
        health = client.health_report()["adsb.lol"]
        assert health.consecutive_failures == 0
        assert health.circuit_open is False
        await client.aclose()

    async def test_html_error_page_with_200_is_treated_as_a_failure(self):
        """Under burst these services return HTML with a 200 status."""
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html>rate limited</html>"))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        result = await client._get_aircraft("adsb.lol", "https://example.invalid/x")
        assert result is None
        assert client.health_report()["adsb.lol"].failures == 1
        await client.aclose()


class TestSnapshotDegradation:
    async def test_total_outage_reports_an_error_not_an_empty_sky(self):
        """count 0 with no error is indistinguishable from "no aircraft here"."""
        transport = httpx.MockTransport(lambda r: httpx.Response(503))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        snap = await client.snapshot(40.69, -74.17)
        assert snap.aircraft == []
        assert snap.error == "all sources unavailable"
        assert snap.degraded is True
        await client.aclose()

    async def test_last_good_snapshot_served_when_everything_dies(self):
        state = {"fail": False}

        def handler(request):
            if state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"ac": [{"hex": "abc123", "lat": 40.0,
                                                     "lon": -74.0, "alt_baro": 3000}]})

        client = FeedClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        good = await client.snapshot(40.69, -74.17)
        assert len(good.aircraft) == 1

        state["fail"] = True
        stale = await client.snapshot(40.69, -74.17)
        assert len(stale.aircraft) == 1
        assert stale.stale is True
        assert stale.degraded is True
        await client.aclose()

    async def test_single_surviving_source_is_flagged_degraded(self):
        def handler(request):
            if "adsb.lol" in str(request.url):
                return httpx.Response(200, json={"ac": [{"hex": "a", "lat": 1.0, "lon": 1.0}]})
            return httpx.Response(503)

        client = FeedClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        snap = await client.snapshot(40.69, -74.17)
        assert snap.degraded is True
        assert snap.contributing_sources == ["adsb.lol"]
        await client.aclose()

    async def test_attribution_travels_with_the_data(self):
        """ODbL makes crediting adsb.lol a condition of use, not a courtesy."""
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ac": [{"hex": "a", "lat": 1.0, "lon": 1.0}]}))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        snap = await client.snapshot(40.69, -74.17)
        assert any("ODbL" in a for a in snap.attributions)
        await client.aclose()


class TestGlobalQueryRateLimiting:
    """Regression: whole-world queries once bypassed the rate limiter entirely.

    Four in quick succession earned an HTTP 420 from adsb.lol, which opened the circuit
    breaker, after which military_aircraft() answered "0 aircraft" when the true answer
    was 81. An unavailable source must never look like an empty sky.
    """

    async def test_global_query_reserves_a_rate_slot(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ac": []}))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        await client.global_query("mil")
        # Having taken a slot, the next one must be scheduled into the future.
        assert client._next_slot["adsb.lol"] > 0
        await client.aclose()

    async def test_open_breaker_reports_unavailable_not_empty(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(420))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        for _ in range(feeds.FAILURES_BEFORE_OPEN):
            await client._get_aircraft("adsb.lol", "https://example.invalid/x")
        result = await client.global_query("mil")
        assert result.aircraft == []
        assert result.error is not None, "an open breaker must not read as zero aircraft"
        await client.aclose()

    async def test_failed_query_reports_unavailable_not_empty(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(420))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        result = await client.global_query("type/A388")
        assert result.aircraft == []
        assert result.error is not None
        await client.aclose()

    async def test_genuine_empty_result_is_not_an_error(self):
        """Zero A380s airborne is a real, valid answer and must stay distinguishable."""
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ac": []}))
        client = FeedClient(httpx.AsyncClient(transport=transport))
        result = await client.global_query("type/A124")
        assert result.aircraft == []
        assert result.error is None
        await client.aclose()
