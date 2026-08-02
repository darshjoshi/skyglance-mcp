"""The single-instance lock, which is what stops several Claude sessions from
multiplying load on volunteer feeds, and the pass-recording loop."""

import json
import os

import httpx
import pytest

from skyglance.feeds import FeedClient
from skyglance.poller import MIN_INTERVAL_S, Poller, SingleInstanceLock
from skyglance.store import Store


@pytest.fixture()
def lockfile(tmp_path):
    return tmp_path / "poller.lock"


class TestSingleInstanceLock:
    def test_first_acquires(self, lockfile):
        assert SingleInstanceLock(lockfile).acquire() is True

    def test_second_is_refused_while_first_holds_it(self, lockfile):
        first = SingleInstanceLock(lockfile)
        assert first.acquire() is True
        assert SingleInstanceLock(lockfile).acquire() is False

    def test_release_lets_the_next_one_in(self, lockfile):
        first = SingleInstanceLock(lockfile)
        first.acquire()
        first.release()
        assert SingleInstanceLock(lockfile).acquire() is True

    def test_stale_lock_from_a_dead_pid_is_reclaimed(self, lockfile):
        """Otherwise one hard kill stops history recording permanently."""
        # PID 999999 is above the default max on macOS and Linux, so it cannot exist.
        lockfile.write_text(json.dumps({"pid": 999999, "started_at": 0}))
        assert SingleInstanceLock(lockfile).acquire() is True
        assert json.loads(lockfile.read_text())["pid"] == os.getpid()

    def test_live_pid_is_not_reclaimed(self, lockfile):
        lockfile.write_text(json.dumps({"pid": os.getpid(), "started_at": 0}))
        assert SingleInstanceLock(lockfile).acquire() is False

    def test_corrupt_lock_is_cleared_not_fatal(self, lockfile):
        """A crash mid-write leaves a truncated file; it must not wedge forever."""
        lockfile.write_text("{not json")
        assert SingleInstanceLock(lockfile).acquire() is True

    def test_holder_reports_who_has_it(self, lockfile):
        SingleInstanceLock(lockfile).acquire()
        assert SingleInstanceLock(lockfile).holder()["pid"] == os.getpid()

    def test_release_is_safe_when_not_held(self, lockfile):
        SingleInstanceLock(lockfile).release()  # must not raise


def _feed_client(payload):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    return FeedClient(httpx.AsyncClient(transport=transport))


AIRCRAFT = {"ac": [{"hex": "abc123", "flight": "TEST1  ", "r": "N1AB", "t": "B738",
                    "lat": 40.70, "lon": -74.02, "alt_baro": 3000, "gs": 250,
                    "track": 90}]}


class TestPolling:
    async def test_poll_records_a_pass(self, tmp_path, lockfile):
        store = Store(tmp_path / "sky.db")
        poller = Poller(_feed_client(AIRCRAFT), store, 40.71, -74.01,
                        lock=SingleInstanceLock(lockfile))
        result = await poller.poll_once()
        assert result["new_passes"] == 1
        assert store.counts()["passes"] == 1
        store.close()

    async def test_stale_snapshot_is_not_folded_into_history(self, tmp_path, lockfile):
        """Re-recording a stale snapshot would inflate counts and hide an outage."""
        state = {"fail": False}

        def handler(request):
            if state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json=AIRCRAFT)

        client = FeedClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        store = Store(tmp_path / "sky.db")
        poller = Poller(client, store, 40.71, -74.01, lock=SingleInstanceLock(lockfile))

        await poller.poll_once()
        state["fail"] = True
        result = await poller.poll_once()

        assert result["skipped"] is True
        assert store.history_for(hex_id="abc123")[0]["observations"] == 1
        store.close()

    async def test_ground_traffic_is_ignored(self, tmp_path, lockfile):
        payload = {"ac": [{"hex": "abc123", "lat": 40.70, "lon": -74.02,
                           "alt_baro": "ground"}]}
        store = Store(tmp_path / "sky.db")
        poller = Poller(_feed_client(payload), store, 40.71, -74.01,
                        lock=SingleInstanceLock(lockfile))
        await poller.poll_once()
        assert store.counts()["passes"] == 0
        store.close()

    async def test_interval_cannot_be_set_below_the_floor(self, tmp_path, lockfile):
        """A tight loop against volunteer feeds is the thing to prevent."""
        store = Store(tmp_path / "sky.db")
        poller = Poller(_feed_client(AIRCRAFT), store, 40.71, -74.01, interval_s=1,
                        lock=SingleInstanceLock(lockfile))
        assert poller.interval_s == MIN_INTERVAL_S
        store.close()

    async def test_status_discloses_the_sampling_caveat(self, tmp_path, lockfile):
        store = Store(tmp_path / "sky.db")
        poller = Poller(_feed_client(AIRCRAFT), store, 40.71, -74.01,
                        lock=SingleInstanceLock(lockfile))
        status = poller.status()
        assert "caveat" in status
        assert status["interval_seconds"] >= MIN_INTERVAL_S
        store.close()

    async def test_second_poller_does_not_start(self, tmp_path, lockfile):
        store = Store(tmp_path / "sky.db")
        first = Poller(_feed_client(AIRCRAFT), store, 40.71, -74.01,
                       lock=SingleInstanceLock(lockfile))
        second = Poller(_feed_client(AIRCRAFT), store, 40.71, -74.01,
                        lock=SingleInstanceLock(lockfile))
        assert first.start() is True
        assert second.start() is False
        await first.stop()
        store.close()
