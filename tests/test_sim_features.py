"""仿真仓库能力测试：分页、ETag/304、限流、故障注入、变更流。"""
import json
import urllib.request
import urllib.error

from sbom_monitor.collector.httpclient import RepoClient, RepoUnavailable
from sbom_monitor.sim.ratelimit import TokenBucket
from tests import SimClusterTestCase


class TestSimFeatures(SimClusterTestCase):
    port_offset = 500

    def _client(self, eco):
        return RepoClient(self.urls[eco])

    def test_listing_pagination(self):
        c = self._client("npm")
        r = c.get("/-/all?page=1&page_size=5")
        data = r.json()
        self.assertEqual(len(data["items"]), 5)
        self.assertTrue(data["has_more"])
        r2 = c.get("/-/all?page=999&page_size=5")
        self.assertEqual(r2.json()["items"], [])

    def test_etag_304(self):
        c = self._client("pypi")
        listing = c.get("/simple/?page=1&page_size=5").json()
        name = listing["items"][0]["name"]
        r1 = c.get(f"/pypi/{name}/json")
        self.assertEqual(r1.status, 200)
        etag = r1.headers["ETag"]
        r2 = c.get(f"/pypi/{name}/json", etag=etag)
        self.assertEqual(r2.status, 304)
        self.assertEqual(c.stats["cached_304"], 1)
        # 错误 ETag 必须回 200
        r3 = c.get(f"/pypi/{name}/json", etag='"stale"')
        self.assertEqual(r3.status, 200)

    def test_rate_limit_429(self):
        bucket = TokenBucket(rate_per_sec=1, burst=1)
        ok1, _ = bucket.take()
        ok2, wait = bucket.take()
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertGreater(wait, 0)

    def test_feed_pagination_and_skip(self):
        c = self._client("maven")
        handle = next(h for h in self.cluster.handles if h.ecosystem == "maven")
        import random
        rng = random.Random(5)
        for _ in range(3):
            handle.state.mutate(rng)
        feed = c.get("/feed?after_id=0&limit=2").json()
        self.assertLessEqual(len(feed["events"]), 2)
        self.assertTrue(feed["has_more"] or len(feed["events"]) <= 2)

    def test_fault_injection_offline(self):
        eco = "npm"
        c = self._client(eco)
        c.post_json("/__admin__/fault", {"offline": True})
        c.max_retries = 1
        c.backoff_base = 0.02
        with self.assertRaises(RepoUnavailable):
            c.get("/-/all?page=1&page_size=5")
        # 恢复
        c2 = self._client(eco)
        c2.post_json("/__admin__/fault", {"offline": False})
        self.assertEqual(c2.get("/-/all?page=1&page_size=5").status, 200)

    def test_ground_truth_endpoint(self):
        c = self._client("pypi")
        gt = c.get("/__ground_truth").json()
        self.assertEqual(gt["ecosystem"], "pypi")
        self.assertEqual(len(gt["packages"]), self.counts["pypi"])

    def test_external_host_blocked(self):
        c = RepoClient("http://127.0.0.1:1")
        with self.assertRaises(RuntimeError):
            c._validate_url("http://registry.npmjs.org/x")
