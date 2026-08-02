"""The 11 known-answer cases from reference/overhead.mjs, ported verbatim.

These are the contract for the JS -> Python port. If any of them fails, the port is
wrong, and everything built on top of it is wrong too.
"""

import math

import pytest

from skyglance.geometry import (
    bearing_deg,
    closest_approach,
    compass,
    describe,
    haversine_km,
)

# One degree of latitude is ~111.19 km on a sphere of R=6371.
KM_PER_DEG_LAT = 2 * math.pi * 6371 / 360


def _north_of(lat: float, km: float) -> float:
    return lat + km / KM_PER_DEG_LAT


def _east_of(lat: float, lon: float, km: float) -> float:
    return lon + km / (KM_PER_DEG_LAT * math.cos(math.radians(lat)))


class TestElevation:
    def test_45_degrees_1km_out_1km_up(self):
        """1 km ground distance, 1 km altitude -> 45 degrees."""
        s = describe(0.0, 0.0, _north_of(0.0, 1.0), 0.0, altitude_ft=1.0 / 0.0003048)
        assert s.elevation_deg == pytest.approx(45, abs=0.01)

    def test_90_degrees_directly_overhead(self):
        s = describe(51.47, -0.45, 51.47, -0.45, altitude_ft=30000)
        assert s.elevation_deg == pytest.approx(90, abs=0.01)

    def test_5_71_degrees_10km_out_1km_up(self):
        s = describe(0.0, 0.0, _north_of(0.0, 10.0), 0.0, altitude_ft=1.0 / 0.0003048)
        assert s.elevation_deg == pytest.approx(5.71, abs=0.01)
        # Exact parity with reference/overhead.mjs on the same inputs.
        assert s.elevation_deg == pytest.approx(5.710593137499644, abs=1e-9)


class TestBearing:
    def test_due_north(self):
        assert bearing_deg(0.0, 0.0, 1.0, 0.0) == pytest.approx(0, abs=0.01)

    def test_east_along_a_parallel(self):
        """Bearing to a point at equal latitude is not 90 on a sphere.

        The expected value is what reference/overhead.mjs returns for these exact
        inputs, captured by running it: 89.64797024553724. (OVERHEAD-DETECTION.md
        quotes 89.944 for a different pair of coordinates.)
        """
        assert bearing_deg(51.47, -0.45, 51.47, 0.45) == pytest.approx(
            89.64797024553724, abs=1e-9)


class TestClosestApproach:
    def test_cpa_seconds_10km_south_at_360kt(self):
        """Aircraft 10 km south tracking due north reaches us in 54 s at 360 kt."""
        obs_lat, obs_lon = 51.47, -0.45
        cpa = closest_approach(
            obs_lat, obs_lon,
            lat=_north_of(obs_lat, -10.0), lon=obs_lon,
            ground_speed_kt=360, track_deg=0, altitude_ft=10000, vertical_rate_fpm=0,
        )
        assert cpa is not None
        assert cpa.seconds_away == pytest.approx(54, abs=0.5)
        assert cpa.approaching is True

    def test_cpa_ground_miss_distance_is_zero_when_heading_straight_at_us(self):
        obs_lat, obs_lon = 51.47, -0.45
        cpa = closest_approach(
            obs_lat, obs_lon,
            lat=_north_of(obs_lat, -10.0), lon=obs_lon,
            ground_speed_kt=360, track_deg=0, altitude_ft=10000,
        )
        assert cpa is not None
        assert cpa.ground_km == pytest.approx(0, abs=0.01)

    def test_cpa_t_is_zero_when_flying_perpendicular(self):
        """Abeam and tracking across: we are already at the closest point."""
        obs_lat, obs_lon = 51.47, -0.45
        cpa = closest_approach(
            obs_lat, obs_lon,
            lat=obs_lat, lon=_east_of(obs_lat, obs_lon, 10.0),
            ground_speed_kt=360, track_deg=0, altitude_ft=10000,
        )
        assert cpa is not None
        assert cpa.seconds_away == pytest.approx(0, abs=0.01)

    def test_altitude_at_cpa_after_descent(self):
        """10,000 ft descending 2,000 ft/min, 54 s out -> 8,200 ft at CPA."""
        obs_lat, obs_lon = 51.47, -0.45
        cpa = closest_approach(
            obs_lat, obs_lon,
            lat=_north_of(obs_lat, -10.0), lon=obs_lon,
            ground_speed_kt=360, track_deg=0,
            altitude_ft=10000, vertical_rate_fpm=-2000,
        )
        assert cpa is not None
        assert cpa.altitude_ft == pytest.approx(8200, abs=25)

    def test_returns_none_without_speed_or_track(self):
        assert closest_approach(0, 0, 1, 1, None, 90) is None
        assert closest_approach(0, 0, 1, 1, 400, None) is None


class TestCompass:
    def test_labels(self):
        assert compass(0) == "N"
        assert compass(90) == "E"
        assert compass(180) == "S"
        assert compass(270) == "W"
        assert compass(135) == "SE"
        assert compass(359) == "N"


class TestGuards:
    def test_negative_altitude_does_not_produce_negative_elevation(self):
        """alt_baro reads slightly negative near sea level in low pressure."""
        s = describe(51.47, -0.45, _north_of(51.47, 5.0), -0.45, altitude_ft=-75)
        assert s.elevation_deg == 0.0

    def test_naked_eye_limit(self):
        near = describe(51.47, -0.45, _north_of(51.47, 10.0), -0.45, altitude_ft=5000)
        far = describe(51.47, -0.45, _north_of(51.47, 200.0), -0.45, altitude_ft=35000)
        assert near.naked_eye_plausible is True
        assert far.naked_eye_plausible is False

    def test_haversine_zero_distance(self):
        assert haversine_km(51.47, -0.45, 51.47, -0.45) == pytest.approx(0, abs=1e-9)
