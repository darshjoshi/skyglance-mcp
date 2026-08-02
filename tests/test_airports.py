"""Airport classification. The risk is over-claiming: this observes movement, it does
not read a departure board, and the bucketing must not pretend otherwise."""

import httpx
import pytest

from skyglance import airports
from skyglance.airports import AirportRef, classify, describe_activity, is_valid_icao
from skyglance.feeds import Aircraft

EWR = AirportRef(icao="KEWR", iata="EWR", name="Newark Liberty International Airport",
                 location="Newark", country="US", lat=40.6925, lon=-74.1687,
                 elevation_ft=18.0)


def ac(hex_id="abc123", *, lat=40.6925, lon=-74.1687, alt=3000.0, rate=None,
       ground=False, **kw):
    return Aircraft(hex=hex_id, lat=lat, lon=lon, altitude_ft=alt, on_ground=ground,
                    vertical_rate_fpm=rate, **kw)


class TestIcaoValidation:
    @pytest.mark.parametrize("code", ["KEWR", "EGLL", "VABB", "RKSI"])
    def test_valid(self, code):
        assert is_valid_icao(code)

    @pytest.mark.parametrize("code", ["EWR", "TOOLONG", "", "KE-R", "../x", "KEW?"])
    def test_invalid(self, code):
        assert is_valid_icao(code) is False


class TestClassification:
    def test_on_ground(self):
        assert classify(ac(ground=True, alt=0), EWR)[0] == "on_ground"

    def test_climbing_near_field_is_departing(self):
        assert classify(ac(alt=2000, rate=1500), EWR)[0] == "departing"

    def test_descending_near_field_is_arriving(self):
        assert classify(ac(alt=2000, rate=-900), EWR)[0] == "arriving"

    def test_level_near_field_is_not_guessed(self):
        """Level flight near a field says nothing about intent, so don't invent it."""
        assert classify(ac(alt=2000, rate=50), EWR)[0] == "nearby_level"

    def test_missing_vertical_rate_is_not_guessed(self):
        assert classify(ac(alt=2000, rate=None), EWR)[0] == "nearby_level"

    def test_high_altitude_is_overflying_even_if_climbing(self):
        assert classify(ac(alt=33000, rate=1500), EWR)[0] == "overflying"

    def test_far_away_is_overflying_even_if_low(self):
        far = ac(lat=41.6, alt=2000, rate=1500)   # ~100 km north
        assert classify(far, EWR)[0] == "overflying"

    def test_positionless_aircraft_is_unknown(self):
        assert classify(ac(lat=None, lon=None), EWR)[0] == "unknown"

    def test_distance_is_reported(self):
        bucket, km = classify(ac(alt=2000, rate=-900), EWR)
        assert km < 1


class TestActivityReport:
    def test_buckets_and_counts(self):
        result = describe_activity([
            ac("a1", alt=1800, rate=2000),
            ac("a2", alt=2500, rate=-1200),
            ac("a3", ground=True, alt=0),
            ac("a4", alt=35000, rate=0),
        ], EWR)
        assert result["counts"]["departing"] == 1
        assert result["counts"]["arriving"] == 1
        assert result["counts"]["on_ground"] == 1
        assert result["counts"]["overflying"] == 1

    def test_sorted_by_distance(self):
        result = describe_activity([
            ac("far", lat=40.85, alt=2000, rate=1000),
            ac("near", lat=40.70, alt=2000, rate=1000),
        ], EWR)
        assert [r["hex"] for r in result["departing"]] == ["near", "far"]

    def test_airport_metadata_included(self):
        result = describe_activity([], EWR)
        assert result["airport"]["iata"] == "EWR"
        assert result["airport"]["name"].startswith("Newark")

    def test_disclaims_being_a_departure_board(self):
        """The single most important field: this is inference, not a schedule."""
        note = describe_activity([], EWR)["how_this_is_derived"]
        assert "NOT a departure board" in note
        assert "schedule" in note
        assert "misattributed" in note

    def test_attribution_present(self):
        assert any("ODbL" in a for a in describe_activity([], EWR)["attribution"])

    def test_limit_is_respected(self):
        many = [ac(f"a{i}", lat=40.70 + i * 0.001, alt=2000, rate=1000)
                for i in range(40)]
        assert len(describe_activity(many, EWR, limit=5)["departing"]) == 5


class TestLookups:
    async def test_airport_lookup_parses_the_real_shape(self):
        payload = {"alt_feet": 18.0, "countryiso2": "US", "iata": "EWR", "icao": "KEWR",
                   "lat": 40.692501, "location": "Newark", "lon": -74.168701,
                   "name": "Newark Liberty International Airport"}
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
        field = await airports.lookup("KEWR", client)
        assert field.iata == "EWR" and field.lat == pytest.approx(40.6925, abs=1e-3)
        await client.aclose()

    async def test_bad_icao_never_reaches_the_network(self):
        def explode(request):
            raise AssertionError("malformed code must not be requested")
        client = httpx.AsyncClient(transport=httpx.MockTransport(explode))
        assert await airports.lookup("../../etc", client) is None
        await client.aclose()

    async def test_airline_lookup_unwraps_the_list(self):
        payload = {"response": [{"name": "JetBlue Airways", "icao": "JBU",
                                 "iata": "B6", "callsign": "JETBLUE"}]}
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
        result = await airports.airline("JBU", client)
        assert result["name"] == "JetBlue Airways"
        assert result["callsign"] == "JETBLUE"
        await client.aclose()

    async def test_missing_airport_returns_none_not_an_exception(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        assert await airports.lookup("KXXX", client) is None
        await client.aclose()


class TestBusiestAirportsAssignment:
    """Regression: counting an aircraft for every airport within range gave San
    Francisco and Oakland identical totals, because they are ~20 km apart and both
    sat inside the same radius. Each aircraft must count once, at its nearest field."""

    async def test_neighbouring_airports_do_not_share_aircraft(self, monkeypatch):
        from skyglance import server
        from skyglance.airport_data import Airport
        from skyglance.feeds import FeedClient
        from skyglance.world import WorldView

        sfo = Airport("KSFO", "SFO", "San Francisco", "US", 37.6188, -122.3750)
        oak = Airport("KOAK", "OAK", "Oakland", "US", 37.7213, -122.2207)
        monkeypatch.setattr(server.airport_data, "MAJOR_AIRPORTS", (sfo, oak))

        # Three aircraft parked at SFO, one at Oakland.
        payload = {"ac": [
            {"hex": "a1", "lat": 37.6190, "lon": -122.3752, "alt_baro": "ground"},
            {"hex": "a2", "lat": 37.6185, "lon": -122.3748, "alt_baro": "ground"},
            {"hex": "a3", "lat": 37.6192, "lon": -122.3755, "alt_baro": "ground"},
            {"hex": "a4", "lat": 37.7215, "lon": -122.2210, "alt_baro": "ground"},
        ]}
        client = FeedClient(httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))))
        monkeypatch.setattr(server, "_world", WorldView(client))

        result = await server.busiest_airports(limit=10)
        totals = {a["icao"]: a["total"] for a in result["airports"]}
        assert totals == {"KSFO": 3, "KOAK": 1}, \
            f"aircraft double-counted across neighbouring airports: {totals}"
        await client.aclose()

    async def test_method_note_discloses_the_coverage_bias(self, monkeypatch):
        from skyglance import server
        from skyglance.feeds import FeedClient
        from skyglance.world import WorldView

        client = FeedClient(httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ac": []}))))
        monkeypatch.setattr(server, "_world", WorldView(client))
        result = await server.busiest_airports()
        assert "counted once" in result["method"]
        assert "receiver coverage" in result["method"]
        await client.aclose()
