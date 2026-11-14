"""测试公共夹具：在临时目录启动三生态仿真集群。"""
import os
import shutil
import sys
import tempfile
import unittest

# -I 模式下 cwd 不在 sys.path，显式把仓库根目录加入
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sbom_monitor.sim.cluster import SimCluster

TEST_PORTS = {"npm": 19101, "pypi": 19102, "maven": 19103}


class SimClusterTestCase(unittest.TestCase):
    """每个测试类获得独立临时目录与端口偏移，避免并行冲突。"""

    counts = {"npm": 8, "pypi": 6, "maven": 5}
    port_offset = 0

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="sbom-test-")
        ports = {eco: p + cls.port_offset for eco, p in TEST_PORTS.items()}
        cls.cluster = SimCluster(
            os.path.join(cls.tmpdir, "sim"), counts=cls.counts, ports=ports,
            mutate_interval=1000.0, rate_per_sec=500.0, burst=1000)
        cls.cluster.start(populate=True)
        cls.cluster.wait_healthy()
        cls.urls = cls.cluster.base_urls()
        cls.db_path = os.path.join(cls.tmpdir, "sbom.db")
        from sbom_monitor.collector.storage import Database
        cls.db = Database(cls.db_path)
        for eco, url in cls.urls.items():
            cls.db.upsert_repo(eco, url, 500.0, 1000)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.cluster.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def crawl_all(self):
        from sbom_monitor.collector.crawler import FullCrawler
        for eco in self.urls:
            res = FullCrawler(self.db, eco, self.urls[eco], page_size=10).run_once()
            # done=本轮跑完；already_done=此前用例已完成全量，均属正常
            self.assertIn(res["status"], ("done", "already_done"))
            if res["status"] == "done":
                self.assertEqual(res["failed"], [])

    def assert_reconcile_clean(self):
        from sbom_monitor.reconcile.checker import Reconciler
        report = Reconciler(self.db, self.urls).run()
        if not report["ok"]:
            from sbom_monitor.reconcile.checker import format_report
            self.fail("对账存在差异:\n" + format_report(report))
        return report
