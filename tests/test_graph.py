"""依赖图与版本范围解析测试。"""
from sbom_monitor.collector.graph import DependencyGraph
from tests import SimClusterTestCase
from sbom_monitor.collector.crawler import FullCrawler


class TestDependencyGraph(SimClusterTestCase):
    port_offset = 400

    def setUp(self):
        self.crawl_all()
        self.graph = DependencyGraph(self.db)

    def test_graph_stats(self):
        stats = self.graph.stats()
        self.assertGreater(stats["edges"], 0)
        self.assertGreater(stats["nodes"], 0)

    def test_upstream_downstream_consistency(self):
        # 每条边的下游遍历与上游遍历应互相吻合
        edges = self.db.all_edges()
        edge = edges[0]
        down = self.graph.downstream(edge["source_key"], depth=2)
        targets = {n["key"] for n in down["nodes"]}
        self.assertIn(edge["target_key"], targets)
        up = self.graph.upstream(edge["target_key"], depth=2)
        sources = {n["key"] for n in up["nodes"]}
        self.assertIn(edge["source_key"], sources)

    def test_satisfying_versions(self):
        edge = self.db.all_edges()[0]
        versions = self.graph.satisfying_versions(edge["target_key"], edge["spec"])
        # 解析出的每个版本都必须真正满足该范围
        from sbom_monitor.versions import resolver
        pkg = self.db.get_package(edge["target_key"])
        for v in versions:
            self.assertTrue(
                resolver.satisfies(pkg["ecosystem"], v, edge["spec"]),
                (pkg["ecosystem"], v, edge["spec"]))
        all_versions = [r["version"] for r in
                        self.db.get_package_versions(edge["target_key"])]
        expected = [v for v in all_versions
                    if resolver.satisfies(pkg["ecosystem"], v, edge["spec"])]
        self.assertEqual(sorted(versions), sorted(expected))

    def test_impact_analysis(self):
        key = self.db.all_edges()[0]["target_key"]
        impact = self.graph.impact_analysis(key)
        self.assertGreaterEqual(impact["impacted_count"], 1)
        self.assertEqual(impact["target"], key)

    def test_cycle_safe(self):
        # 人造一个环，遍历必须终止（visited 去重）
        key = self.db.conn.execute("SELECT key FROM packages LIMIT 1").fetchone()["key"]
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO dependency_edges(source_key, source_version, "
                "target_ecosystem, target_name, target_key, spec, optional) "
                "SELECT key, 'x', ecosystem, name, key, '*', 0 FROM packages WHERE key=?",
                (key,))
        result = self.graph.upstream(key, depth=50)
        self.assertIn(key, {n["key"] for n in result["nodes"]})
