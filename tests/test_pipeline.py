"""采集管道集成测试：全量爬取、条件请求、增量补漏、对账零差异。"""
import random

from tests import SimClusterTestCase
from sbom_monitor.collector.crawler import FullCrawler
from sbom_monitor.collector.feed_consumer import FeedConsumer
from sbom_monitor.collector.verifier import Verifier
from sbom_monitor.reconcile.checker import Reconciler


class TestFullCrawlReconcile(SimClusterTestCase):
    port_offset = 100

    def test_full_crawl_zero_diff(self):
        self.crawl_all()
        total_pkgs = sum(self.counts.values())
        self.assertEqual(self.db.count_packages(), total_pkgs)
        self.assertGreater(self.db.count_versions(), total_pkgs)
        self.assertGreater(len(self.db.all_edges()), 0)
        self.assert_reconcile_clean()

    def test_second_crawl_uses_304(self):
        self.crawl_all()
        # 重置游标强制重跑一遍爬取流程；数据未变，应全部命中条件请求 304
        for eco in self.urls:
            self.db.reset_cursor(eco)
        for eco in self.urls:
            c = FullCrawler(self.db, eco, self.urls[eco], page_size=10)
            c.run_once()
            self.assertGreater(c.client.stats["cached_304"], 0, eco)
            self.assertEqual(c.processed, 0, eco)
        self.assert_reconcile_clean()


class TestIncrementalAndVerification(SimClusterTestCase):
    port_offset = 200

    def test_feed_gap_filled_by_verifier(self):
        self.crawl_all()
        # 制造变异，部分事件从 feed 抹掉
        rng = random.Random(11)
        dropped = 0
        for h in self.cluster.handles:
            for _ in range(6):
                evt = h.state.mutate(rng, drop_from_feed=(rng.random() < 0.35))
                if evt and evt["id"] in h.state.feed_skipped:
                    dropped += 1
        self.assertGreater(dropped, 0, "测试前提：至少有一个事件被抹掉")

        # 只消费 feed
        for eco in self.urls:
            r = FeedConsumer(self.db, eco, self.urls[eco]).drain()
            self.assertEqual(r["status"], "caught_up")
        pre = Reconciler(self.db, self.urls).run()
        self.assertGreater(pre["total_diffs"], 0, "feed 丢事件应造成可见差异")

        # 校验通道补漏
        for eco in self.urls:
            r = Verifier(self.db, eco, self.urls[eco]).run()
            self.assertIn(r["status"], ("completed", "completed_with_skips"))
            self.assertEqual(r["skipped"], [])

        self.assert_reconcile_clean()

    def test_alerts_generated_and_dedup(self):
        self.crawl_all()
        rng = random.Random(23)
        for h in self.cluster.handles:
            for _ in range(8):
                h.state.mutate(rng)
        for eco in self.urls:
            FeedConsumer(self.db, eco, self.urls[eco]).drain()
        alerts = self.db.list_alerts(limit=5000)
        types = {a["type"] for a in alerts}
        self.assertIn("new_version", types)
        self.assertTrue({"version_yanked", "license_changed", "new_dependency"} & types)

        # 再消费一轮不应产生重复告警（指纹去重 + 事件去重）
        before = len(self.db.list_alerts(limit=5000))
        for eco in self.urls:
            r = FeedConsumer(self.db, eco, self.urls[eco]).drain()
            self.assertEqual(r["status"], "caught_up")
        self.assertEqual(len(self.db.list_alerts(limit=5000)), before)


class TestWatchlistEscalation(SimClusterTestCase):
    port_offset = 300

    def test_watched_alerts_escalated(self):
        self.crawl_all()
        row = self.db.conn.execute(
            "SELECT ecosystem, name FROM packages ORDER BY name LIMIT 1").fetchone()
        self.db.add_watch(row["ecosystem"], row["name"], "核心组件", "测试")
        rng = random.Random(31)
        # 对该包连续变异直到产生告警
        handle = next(h for h in self.cluster.handles if h.ecosystem == row["ecosystem"])
        from sbom_monitor.common.model import canonical_name
        target = canonical_name(row["ecosystem"], row["name"])
        for _ in range(20):
            handle.state.mutate(rng)
        FeedConsumer(self.db, row["ecosystem"], self.urls[row["ecosystem"]]).drain()
        watched_alerts = [a for a in self.db.list_alerts(limit=5000) if a["watched"]]
        self.assertTrue(watched_alerts, "关注包应产生带 watched 标记的告警")
        for a in watched_alerts:
            self.assertNotEqual(a["severity"], "info", "关注包的 info 告警应被提级")
