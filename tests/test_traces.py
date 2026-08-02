"""Trace reduction. The danger here is size: a raw trace is ~2,600 points and 450 KB,
which would swamp a context window, so segmentation and downsampling are the product."""

import pytest

from skyglance import traces
from skyglance.traces import summarise, trace_url


def _trace(points, *, base=1_700_000_000.0, **meta):
    return {"icao": "a689b8", "r": "N520JB", "t": "A320",
            "timestamp": base, "trace": points, **meta}


def _leg(start_offset, count, *, alt=30000, step=60, lat0=40.0, lon0=-74.0):
    """A run of airborne points drifting north."""
    return [[start_offset + i * step, lat0 + i * 0.05, lon0, alt, 450.0, 0.0, 0, 0]
            for i in range(count)]


GROUND = lambda t, lat=40.0: [t, lat, -74.0, "ground", 0.0, 0.0, 0, None]  # noqa: E731


class TestUrl:
    def test_shards_by_last_two_hex_characters(self):
        assert trace_url("a689b8") == \
            "https://globe.adsb.lol/data/traces/b8/trace_full_a689b8.json"

    def test_recent_variant(self):
        assert "trace_recent_" in trace_url("a689b8", recent=True)

    def test_tisb_tilde_prefix_stripped(self):
        """adsb.lol prefixes TIS-B targets with ~, which is not part of the path."""
        assert trace_url("~abc123") == \
            "https://globe.adsb.lol/data/traces/23/trace_full_abc123.json"


class TestSegmentation:
    def test_single_leg(self):
        result = summarise(_trace(_leg(0, 30)))
        assert result["found"] is True
        assert result["legs_found"] == 1

    def test_ground_stop_splits_legs(self):
        points = _leg(0, 30) + [GROUND(2000), GROUND(2100)] + _leg(3000, 30)
        assert summarise(_trace(points))["legs_found"] == 2

    def test_coverage_gap_splits_legs(self):
        """A long silence is lost reception, and must not be drawn as a straight leg."""
        points = _leg(0, 30) + _leg(0 + 30 * 60 + traces.COVERAGE_GAP_S + 60, 30)
        assert summarise(_trace(points))["legs_found"] == 2

    def test_brief_gap_does_not_split(self):
        points = _leg(0, 30) + _leg(30 * 60 + 120, 30)
        assert summarise(_trace(points))["legs_found"] == 1

    def test_taxiing_does_not_end_a_leg_but_parking_does(self):
        taxiing = [[2000, 40.0, -74.0, "ground", 15.0, 0.0, 0, None]]
        parked = [GROUND(2000), GROUND(2100)]
        assert summarise(_trace(_leg(0, 30) + taxiing + _leg(3000, 30)))["legs_found"] == 1
        assert summarise(_trace(_leg(0, 30) + parked + _leg(3000, 30)))["legs_found"] == 2

    def test_stray_points_are_not_a_leg(self):
        """Two blips of reception noise is not a flight."""
        assert summarise(_trace(_leg(0, 2)))["legs_found"] == 0


class TestReduction:
    def test_path_is_downsampled_to_a_bounded_size(self):
        result = summarise(_trace(_leg(0, 2000)))
        assert len(result["legs"][0]["path"]) == traces.PATH_SAMPLES

    def test_short_leg_is_not_padded(self):
        result = summarise(_trace(_leg(0, 8)))
        assert len(result["legs"][0]["path"]) == 8

    def test_only_the_most_recent_legs_are_returned(self):
        points = []
        for i in range(10):
            points += _leg(i * 100000, 20) + [GROUND(i * 100000 + 50000)]
        result = summarise(_trace(points), max_segments=3)
        assert result["legs_found"] == 10
        assert result["legs_returned"] == 3

    def test_output_stays_small_for_a_realistic_trace(self):
        """The whole point: a full day must not blow up the context window."""
        import json
        points = []
        for i in range(6):
            points += _leg(i * 20000, 400) + [GROUND(i * 20000 + 19000),
                                              GROUND(i * 20000 + 19500)]
        payload = json.dumps(summarise(_trace(points)))
        assert len(payload) < 20000, f"reduced trace is {len(payload)} bytes"


class TestLegFacts:
    def test_max_altitude_and_distance_reported(self):
        points = _leg(0, 10, alt=12000) + _leg(700, 10, alt=35000)
        leg = summarise(_trace(points))["legs"][0]
        assert leg["max_altitude_ft"] == 35000
        assert leg["ground_track_km"] > 0

    def test_ground_only_trace_yields_no_legs(self):
        result = summarise(_trace([GROUND(i * 100) for i in range(50)]))
        assert result["legs_found"] == 0

    def test_limits_are_always_disclosed(self):
        result = summarise(_trace(_leg(0, 30)))
        assert "24 hours" in result["limits"]
        assert "coverage" in result["limits"]


class TestMalformedInput:
    def test_missing_timestamp(self):
        assert summarise({"trace": [[0, 1, 2, 3000, 400, 0, 0, 0]]})["found"] is False

    def test_empty_trace(self):
        assert summarise(_trace([]))["found"] is False

    def test_short_and_nonnumeric_rows_are_skipped(self):
        points = [[0, 40.0], "garbage", [None, 1, 2, 3, 4], *_leg(100, 10)]
        assert summarise(_trace(points))["found"] is True

    def test_string_altitude_other_than_ground_becomes_none(self):
        points = [[i * 60, 40.0 + i * 0.01, -74.0, "weird", 400.0, 0.0, 0, 0]
                  for i in range(10)]
        result = summarise(_trace(points))
        assert result["legs"][0]["max_altitude_ft"] is None
