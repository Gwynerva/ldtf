"""One network budget for all syncs, fair turns, per-job stop, and parallel jobs (offline)."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.http import AdaptiveLimiter, FatalNetworkError  # noqa: E402
from dtf_backup.netpool import NetPool  # noqa: E402
from dtf_backup.web.jobs import JobManager, job_percent  # noqa: E402


def hammer(lease, counter, key, until, hold=0.0):
    while time.monotonic() < until:
        try:
            lease.acquire()
        except FatalNetworkError:
            return
        if time.monotonic() >= until:   # a permit granted after the window does not count (precise timers on Linux)
            lease.release(True)
            return
        counter[key] = counter.get(key, 0) + 1
        time.sleep(hold)
        lease.release(True)


class OfflineTest(unittest.TestCase):
    def test_no_network_fails_fast(self) -> None:
        from dtf_backup.http import HttpClient
        lim = AdaptiveLimiter("api", 2, rate=10)
        client = HttpClient({"*": lim}, timeout=5)
        client.offline_wait = 0.05
        t0 = time.monotonic()
        with self.assertRaises(FatalNetworkError) as cm:
            client.fetch("https://ldtf-test-host.invalid/")   # .invalid never resolves
        self.assertIn("нет подключения", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual((lim.fail_streak, lim.backoff_until), (0, 0.0))   # the shared budget is not punished


class NetPoolTest(unittest.TestCase):
    def test_shared_rate_and_fair_share(self) -> None:
        pool = NetPool(api_rate=40, api_conn=4, media_conn=2)
        (a, _), (b, _) = pool.lease(), pool.lease()
        counts: dict = {}
        until = time.monotonic() + 1.0
        threads = [threading.Thread(target=hammer, args=(lease, counts, name, until))
                   for lease, name in ((a, "a"), (b, "b")) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = counts["a"] + counts["b"]
        self.assertLessEqual(total, 40 * 1.0 + 2, counts)     # two jobs together stay within one budget
        self.assertGreater(total, 25, counts)
        self.assertLess(abs(counts["a"] - counts["b"]), total * 0.3, counts)   # first come, first served

    def test_stopping_one_job_keeps_the_other(self) -> None:
        pool = NetPool(api_rate=100, api_conn=2)
        (a, _), (b, _) = pool.lease(), pool.lease()
        a.stopped = True
        with self.assertRaises(FatalNetworkError):
            a.acquire()
        b.acquire()          # still served
        b.release(True)

    def test_429_slows_everyone_and_fuse_cools_down(self) -> None:
        lim = AdaptiveLimiter("api", 2, base_backoff=0.01, fuse=2, rate=10, max_rate=10, fuse_cooldown=0.4)
        lim.acquire()
        lim.release(False, reason="HTTP 429")
        self.assertLess(lim.rate, 10)                    # the shared pace dropped for all users
        time.sleep(0.05)
        lim.acquire()
        lim.release(False, reason="HTTP 429")            # second failure window: fuse trips
        with self.assertRaises(FatalNetworkError):
            lim.acquire()
        time.sleep(0.45)
        lim.acquire()                                    # reopened after the cooldown
        lim.release(True)

    def test_configure_applies_live(self) -> None:
        pool = NetPool(api_rate=10, api_conn=4, media_conn=8)
        pool.configure({"api_rate": 5, "api_conn": 2, "media_conn": 3})
        st = pool.state()
        self.assertEqual((st["api"]["max"], st["api"]["rate"], st["media"]["max"]), (2, 5.0, 3))


class SlowJobs(JobManager):
    def __init__(self, *a, **kw):
        self.live = 0
        self.peak = 0
        self.order = []
        self.gate = threading.Lock()
        super().__init__(*a, **kw)

    def _run(self, job) -> None:
        job.state = "running"
        with self.gate:
            self.order.append(job.nick)
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if job.params.get("boom"):
                raise SystemExit("профиль недоступен")
            time.sleep(0.3)
            job.state = "done"
        finally:
            with self.gate:
                self.live -= 1


class JobsTest(unittest.TestCase):
    def wait_idle(self, jm, timeout=5.0):
        end = time.monotonic() + timeout
        while jm.active() and time.monotonic() < end:
            time.sleep(0.05)

    def test_parallel_archives_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            jm = SlowJobs(Path(d), max_parallel=2)
            j1 = jm.submit("a", "sync", reason="schedule")
            self.assertIs(jm.submit("a", "sync", reason="schedule"), j1)     # one job per archive
            jm.submit("b", "sync", reason="schedule")
            jm.submit("c", "sync", reason="schedule")
            self.wait_idle(jm)
            self.assertEqual(jm.peak, 2)
            self.assertTrue(all(j.state == "done" for j in jm.jobs.values()))

    def test_manual_goes_first_and_system_exit_does_not_kill_the_queue(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            jm = SlowJobs(Path(d), max_parallel=1)
            first = jm.submit("x", "sync", reason="schedule", boom=True)
            jm.submit("y", "sync", reason="schedule")
            manual = jm.submit("z", "sync", reason="manual")
            self.wait_idle(jm)
            self.assertLess(jm.order.index("z"), jm.order.index("y"))   # asked by the user: before scheduled ones
            self.assertEqual(first.state, "error")
            self.assertIn("профиль", first.error)
            self.assertEqual(manual.state, "done")
            self.assertEqual(jm.for_nick("y").state, "done")

    def test_stop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            jm = SlowJobs(Path(d), max_parallel=1)
            jm.submit("a", "sync")
            later = jm.submit("b", "sync")
            time.sleep(0.05)
            jm.stop(timeout=3)
            self.assertEqual(later.state, "cancelled")
            self.assertFalse(jm.running)

    def test_job_percent(self) -> None:
        snap = {"stages": [{"status": "done"}, {"status": "running", "pct": 50}, {"status": "pending"},
                           {"status": "skipped"}]}
        self.assertEqual(job_percent(snap), 62)


if __name__ == "__main__":
    unittest.main()
