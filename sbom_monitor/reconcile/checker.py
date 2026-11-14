"""对账器：本地采集库 vs 仿真仓库真值，目标差异为 0。

真值只来自仿真服务的 ``/__ground_truth`` 端点（采集端业务流程不允许调用它，
对账器是独立的验收工具）。比对维度：
1. 包集合：上游有的包本地必须全有，数量一致
2. 版本集合：每个包的版本号、撤回状态、撤回原因一致
3. 许可证：上游原始许可证经 SPDX 归一后，与本地存储一致
4. 依赖边：逐版本的依赖（名字/范围/optional）一致
5. 制品校验和一致

输出结构化差异列表 + 汇总统计；exit code 非 0 表示存在差异。
"""
from __future__ import annotations

import json
import logging

from ..common.licenses import normalize_license
from ..common.model import Dependency, canonical_name, make_key
from ..collector.httpclient import RepoClient

log = logging.getLogger("sbom.reconcile")


class Reconciler:
    def __init__(self, db, base_urls: dict[str, str]):
        self.db = db
        self.base_urls = base_urls
        self.diffs: list[dict] = []

    def run(self) -> dict:
        report = {"ecosystems": {}, "total_diffs": 0, "ok": True}
        for eco, url in self.base_urls.items():
            result = self._reconcile_one(eco, url)
            report["ecosystems"][eco] = result
            report["total_diffs"] += result["diff_count"]
        report["ok"] = report["total_diffs"] == 0
        return report

    def _reconcile_one(self, ecosystem: str, base_url: str) -> dict:
        client = RepoClient(base_url)
        resp = client.get("/__ground_truth")
        if resp.status != 200:
            return {"diff_count": 1, "error": f"真值端点 HTTP {resp.status}",
                    "diffs": [{"kind": "ground_truth_unavailable"}]}
        truth = resp.json()
        truth_pkgs = truth["packages"]
        diffs: list[dict] = []

        with self.db._lock:
            local_rows = self.db.conn.execute(
                "SELECT key, name FROM packages WHERE ecosystem=?",
                (ecosystem,)).fetchall()
        local_by_name = {r["name"]: r["key"] for r in local_rows}
        local_key_set = set(local_by_name.values())

        # 1. 包集合
        truth_names = {canonical_name(ecosystem, n) for n in truth_pkgs}
        missing = truth_names - set(local_by_name)
        extra = set(local_by_name) - truth_names
        for n in sorted(missing):
            diffs.append({"kind": "missing_package", "package": n})
        for n in sorted(extra):
            diffs.append({"kind": "extra_package", "package": n})

        # 2. 逐包逐版本比对
        for raw_name, truth_pkg in sorted(truth_pkgs.items()):
            cname = canonical_name(ecosystem, raw_name)
            key = make_key(ecosystem, cname)
            if key not in local_key_set:
                continue  # 已记 missing_package
            diffs.extend(self._compare_package(ecosystem, key, cname, truth_pkg))

        return {"diff_count": len(diffs), "upstream_packages": len(truth_pkgs),
                "local_packages": len(local_rows), "diffs": diffs}

    def _compare_package(self, ecosystem: str, key: str, cname: str,
                         truth_pkg: dict) -> list[dict]:
        diffs: list[dict] = []
        local_versions = {r["version"]: r for r in self.db.get_package_versions(key)}
        truth_versions = {v["version"]: v for v in truth_pkg["versions"]}

        for ver, tv in truth_versions.items():
            lv = local_versions.get(ver)
            if lv is None:
                diffs.append({"kind": "missing_version", "package": cname, "version": ver})
                continue
            # 撤回状态
            if bool(lv["yanked"]) != bool(tv["yanked"]):
                diffs.append({"kind": "yanked_mismatch", "package": cname,
                              "version": ver, "truth": tv["yanked"],
                              "local": bool(lv["yanked"])})
            if (tv.get("yanked_reason") or None) != (lv["yanked_reason"] or None):
                diffs.append({"kind": "yanked_reason_mismatch", "package": cname,
                              "version": ver, "truth": tv.get("yanked_reason"),
                              "local": lv["yanked_reason"]})
            # 许可证 SPDX
            expected_spdx = normalize_license(tv["license_raw"])
            if lv["licenses"] != expected_spdx:
                diffs.append({"kind": "license_mismatch", "package": cname,
                              "version": ver, "truth_raw": tv["license_raw"],
                              "truth_spdx": expected_spdx,
                              "local": lv["licenses"]})
            # 制品 checksum
            local_artifacts = json.loads(lv["artifacts_json"] or "[]")
            truth_checksums = {a["checksum"] for a in tv["artifacts"]}
            local_checksums = {a.get("checksum") for a in local_artifacts}
            if truth_checksums != local_checksums:
                diffs.append({"kind": "artifact_mismatch", "package": cname,
                              "version": ver, "truth": sorted(truth_checksums),
                              "local": sorted(local_checksums)})
            # 依赖
            diffs.extend(self._compare_deps(ecosystem, cname, ver, tv, lv))

        for ver in local_versions:
            if ver not in truth_versions:
                diffs.append({"kind": "extra_version", "package": cname, "version": ver})
        return diffs

    def _compare_deps(self, ecosystem: str, cname: str, ver: str,
                      tv: dict, lv) -> list[dict]:
        diffs: list[dict] = []
        truth_deps = {}
        for d in tv["dependencies"]:
            dn = canonical_name(ecosystem, d["name"])
            truth_deps[(dn, d["spec"])] = bool(d.get("optional", False))
        local_deps_list = json.loads(lv["dependencies_json"] or "[]")
        local_deps = {}
        for d in local_deps_list:
            local_deps[(d["name"], d["spec"])] = bool(d.get("optional", False))
        if set(truth_deps) != set(local_deps):
            only_truth = sorted(set(truth_deps) - set(local_deps))
            only_local = sorted(set(local_deps) - set(truth_deps))
            if only_truth or only_local:
                diffs.append({"kind": "dependency_set_mismatch", "package": cname,
                              "version": ver, "missing_local": only_truth,
                              "extra_local": only_local})
        for k in set(truth_deps) & set(local_deps):
            if truth_deps[k] != local_deps[k]:
                diffs.append({"kind": "dependency_optional_mismatch",
                              "package": cname, "version": ver, "dep": k[0],
                              "truth": truth_deps[k], "local": local_deps[k]})
        return diffs


def format_report(report: dict) -> str:
    lines = ["=" * 68, "对账报告（本地库 vs 仿真仓库真值）", "=" * 68]
    for eco, r in report["ecosystems"].items():
        lines.append(f"\n[{eco}] 上游包={r.get('upstream_packages')} "
                     f"本地包={r.get('local_packages')} 差异={r['diff_count']}")
        if r.get("error"):
            lines.append(f"  ERROR: {r['error']}")
        for d in r["diffs"][:50]:
            lines.append("  - " + json.dumps(d, ensure_ascii=False))
        if len(r["diffs"]) > 50:
            lines.append(f"  ... 其余 {len(r['diffs']) - 50} 条省略")
    lines.append("\n" + "-" * 68)
    total = report["total_diffs"]
    lines.append(f"总差异数：{total}    结论：{'PASS（零差异）' if report['ok'] else 'FAIL'}")
    return "\n".join(lines)
