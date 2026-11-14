"""依赖图查询与版本范围解析。

- 上下游：基于 dependency_edges 做有向图遍历（BFS，带环保护）
- 版本范围：给定依赖关系，列出目标包中满足该生态语义的具体版本
- 可达性：关注包受影响面分析（某包出问题时，沿入边找到所有受影响的公司组件）
"""
from __future__ import annotations

from ..common.model import make_key
from ..versions import resolver


class DependencyGraph:
    def __init__(self, db):
        self.db = db

    def downstream(self, key: str, depth: int = 5) -> dict:
        """我（传递）依赖谁。返回分层节点与边。"""
        return self._walk(key, direction="down", depth=depth)

    def upstream(self, key: str, depth: int = 5) -> dict:
        """谁（传递）依赖我。"""
        return self._walk(key, direction="up", depth=depth)

    def _walk(self, start: str, direction: str, depth: int) -> dict:
        visited = {start: 0}
        edges: list[dict] = []
        frontier = [start]
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                if direction == "down":
                    rels = self.db.downstream(node)
                else:
                    rels = self.db.upstream(node)
                for rel in rels:
                    neighbor = rel["target_key"] if direction == "down" else rel["source_key"]
                    edges.append({"from": rel["source_key"], "to": rel["target_key"],
                                  "spec": rel["spec"], "optional": bool(rel["optional"])})
                    if neighbor not in visited:
                        visited[neighbor] = level
                        next_frontier.append(neighbor)
            if not next_frontier:
                break
            frontier = next_frontier
        nodes = [{"key": k, "level": lv} for k, lv in sorted(visited.items(),
                                                              key=lambda x: (x[1], x[0]))]
        # 边去重
        uniq = {(e["from"], e["to"], e["spec"]): e for e in edges}
        return {"start": start, "direction": direction, "nodes": nodes,
                "edges": list(uniq.values())}

    def satisfying_versions(self, target_key: str, spec: str) -> list[str]:
        """目标包中满足版本范围 spec 的所有具体版本（按生态语义）。"""
        pkg = self.db.get_package(target_key)
        if not pkg:
            return []
        eco = pkg["ecosystem"]
        versions = [r["version"] for r in self.db.get_package_versions(target_key)]
        return [v for v in versions if resolver.satisfies(eco, v, spec)]

    def impact_analysis(self, key: str) -> dict:
        """受影响面：沿入边 BFS 找所有（传递）依赖该包的组件，并标注关注名单。"""
        up = self.upstream(key, depth=10)
        impacted = []
        for node in up["nodes"]:
            if node["key"] == key:
                continue
            pkg = self.db.get_package(node["key"])
            impacted.append({
                "key": node["key"],
                "level": node["level"],
                "ecosystem": pkg["ecosystem"] if pkg else "",
                "name": pkg["name"] if pkg else node["key"],
                "watched": self.db.is_watched(node["key"]),
            })
        watched_impact = [x for x in impacted if x["watched"]]
        return {"target": key, "impacted": impacted,
                "impacted_count": len(impacted),
                "watched_impacted": watched_impact,
                "watched_count": len(watched_impact)}

    def stats(self) -> dict:
        edges = self.db.all_edges()
        nodes = set()
        for e in edges:
            nodes.add(e["source_key"])
            nodes.add(e["target_key"])
        return {"nodes": len(nodes), "edges": len(edges)}
