"""Pass detection is inference, not observation — so it gets tested hard."""

import pytest

from skyglance.store import MISSED_POLLS_TO_CLOSE, Observation, Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def obs(hex_id="abc123", *, ground_km=5.0, alt=3000.0, elev=30.0,
        bearing=90.0, callsign="TEST1", reg="N123AB", type_code="B738", mil=False):
    return Observation(hex=hex_id, callsign=callsign, registration=reg,
                       type_code=type_code, ground_km=ground_km, altitude_ft=alt,
                       elevation_deg=elev, bearing_deg=bearing, is_military=mil)


class TestPassLifecycle:
    def test_first_snapshot_opens_a_pass(self, store):
        result = store.record_snapshot([obs()], now=1000.0)
        assert result["new_passes"] == 1
        assert store.counts()["open_passes"] == 1

    def test_same_aircraft_next_poll_extends_not_duplicates(self, store):
        store.record_snapshot([obs()], now=1000.0)
        store.record_snapshot([obs()], now=1060.0)
        assert store.counts()["passes"] == 1
        assert store.history_for(hex_id="abc123")[0]["observations"] == 2

    def test_pass_closes_after_missed_polls(self, store):
        store.record_snapshot([obs()], now=1000.0)
        for i in range(MISSED_POLLS_TO_CLOSE):
            store.record_snapshot([], now=1060.0 + i * 60)
        assert store.counts()["open_passes"] == 0
        assert store.history_for(hex_id="abc123")[0]["ended_at"] is not None

    def test_one_missed_poll_does_not_close(self, store):
        """A single dropped frame is normal; closing on it would fragment every pass."""
        assert MISSED_POLLS_TO_CLOSE > 1
        store.record_snapshot([obs()], now=1000.0)
        store.record_snapshot([], now=1060.0)
        assert store.counts()["open_passes"] == 1

    def test_reappearing_after_close_starts_a_new_pass(self, store):
        store.record_snapshot([obs()], now=1000.0)
        for i in range(MISSED_POLLS_TO_CLOSE):
            store.record_snapshot([], now=1060.0 + i * 60)
        store.record_snapshot([obs()], now=5000.0)
        assert store.counts()["passes"] == 2


class TestPassAggregates:
    def test_keeps_the_closest_and_lowest_of_the_pass(self, store):
        store.record_snapshot([obs(ground_km=20.0, alt=9000.0, elev=10.0)], now=1000.0)
        store.record_snapshot([obs(ground_km=2.0, alt=1200.0, elev=70.0)], now=1060.0)
        store.record_snapshot([obs(ground_km=15.0, alt=4000.0, elev=20.0)], now=1120.0)
        row = store.history_for(hex_id="abc123")[0]
        assert row["closest_km"] == pytest.approx(2.0)
        assert row["min_altitude_ft"] == pytest.approx(1200.0)
        assert row["max_elevation_deg"] == pytest.approx(70.0)

    def test_bearing_recorded_at_the_closest_point(self, store):
        store.record_snapshot([obs(ground_km=20.0, bearing=10.0)], now=1000.0)
        store.record_snapshot([obs(ground_km=2.0, bearing=200.0)], now=1060.0)
        store.record_snapshot([obs(ground_km=9.0, bearing=350.0)], now=1120.0)
        assert store.history_for(hex_id="abc123")[0]["bearing_at_closest"] == pytest.approx(200.0)

    def test_null_altitude_does_not_erase_a_known_minimum(self, store):
        store.record_snapshot([obs(alt=2000.0)], now=1000.0)
        store.record_snapshot([obs(alt=None)], now=1060.0)
        assert store.history_for(hex_id="abc123")[0]["min_altitude_ft"] == pytest.approx(2000.0)

    def test_late_arriving_identity_backfills(self, store):
        store.record_snapshot([obs(callsign=None, reg=None, type_code=None)], now=1000.0)
        store.record_snapshot([obs(callsign="UAL1", reg="N77CD", type_code="B772")],
                              now=1060.0)
        row = store.history_for(hex_id="abc123")[0]
        assert (row["callsign"], row["registration"], row["type_code"]) == \
               ("UAL1", "N77CD", "B772")


class TestFirsts:
    def test_first_sighting_then_not(self, store):
        assert store.is_first_sighting("abc123") is True
        store.record_snapshot([obs()], now=1000.0)
        assert store.is_first_sighting("abc123") is False

    def test_type_counting(self, store):
        assert store.type_seen_before("B738") == 0
        store.record_snapshot([obs(type_code="B738")], now=1000.0)
        assert store.type_seen_before("B738") == 1

    def test_records_pick_the_extremes(self, store):
        store.record_snapshot([obs("aaa111", ground_km=30.0, alt=35000.0, elev=5.0)],
                              now=1000.0)
        store.record_snapshot([obs("bbb222", ground_km=0.5, alt=800.0, elev=85.0)],
                              now=1000.0)
        r = store.records()
        assert r["closest_pass"]["hex"] == "bbb222"
        assert r["lowest_pass"]["hex"] == "bbb222"
        assert r["highest_elevation"]["hex"] == "bbb222"
        assert r["distinct_aircraft"] == 2


class TestEnrichmentCache:
    def test_three_way_miss_semantics(self, store):
        """None = never looked. {} = looked and found nothing. dict = found."""
        assert store.get_enrichment("aircraft", "abc") is None
        store.put_enrichment("aircraft", "abc", {})
        assert store.get_enrichment("aircraft", "abc") == {}
        store.put_enrichment("aircraft", "def", {"type_code": "A320"})
        assert store.get_enrichment("aircraft", "def")["type_code"] == "A320"

    def test_negative_result_is_persisted(self, store):
        """Without this, unknown aircraft are re-looked-up on every single frame."""
        store.put_enrichment("route", "XXX1", {})
        assert store.get_enrichment("route", "XXX1") is not None
        assert store.counts()["enrichment_cached"] == 1


class TestStats:
    def test_counts_and_top_types(self, store):
        store.record_snapshot([obs("a1", type_code="B738"),
                               obs("a2", type_code="B738"),
                               obs("a3", type_code="A320", mil=True)], now=1000.0)
        s = store.stats()
        assert s["passes"] == 3
        assert s["military"] == 1
        assert s["top_types"][0] == {"type_code": "B738", "c": 2}

    def test_stats_respect_since(self, store):
        store.record_snapshot([obs("a1")], now=1000.0)
        store.record_snapshot([obs("a2")], now=9000.0)
        assert store.stats(since_epoch=5000.0)["passes"] == 1
