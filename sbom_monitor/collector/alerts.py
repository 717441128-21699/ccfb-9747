"""变更检测：把新采集的包与库内旧状态对比，产出合规告警。

告警类型：
- license_change     某版本许可证 SPDX 发生变化
- version_yanked     版本被撤回
- new_dependency     新版本相对上一版本新增了依赖（含关注包优先级提升）
- new_version        新版本发布（信息级，关注包升为 high）

严重度：公司关注名单（watchlist）中的包整体提升一级。
"""
from __future__ import annotations

from ..common.model import Package
from ..versions import resolver

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _escalate(severity: str, watched: bool) -> str:
    if not watched:
        return severity
    idx = min(SEVERITY_ORDER[severity] + 1, 4)
    return [k for k, v in SEVERITY_ORDER.items() if v == idx][0]


def diff_package(db, new_pkg: Package, watched: bool) -> list[dict]:
    """对比库内旧版本，返回告警 dict 列表（尚未落库）。"""
    alerts: list[dict] = []
    old_rows = {r["version"]: r for r in db.get_package_versions(new_pkg.key)}
    new_versions = {v.version: v for v in new_pkg.versions}

    for ver, v in new_versions.items():
        old = old_rows.get(ver)
        if old is None:
            # 新版本发布
            alerts.append(_alert("new_version", _escalate("info", watched),
                                 new_pkg, ver, f"新版本发布 {ver}",
                                 {"licenses": v.licenses, "yanked": v.yanked}, watched))
            if v.dependencies:
                alerts.append(_alert(
                    "new_dependency", _escalate("medium", watched), new_pkg, ver,
                    f"新版本 {ver} 携带 {len(v.dependencies)} 个依赖",
                    {"dependencies": [d.to_dict() for d in v.dependencies]}, watched))
            continue
        # 已有版本：许可证变化
        if old["licenses"] != v.licenses:
            alerts.append(_alert(
                "license_change", _escalate("high", watched), new_pkg, ver,
                f"许可证变更：{old['licenses']} -> {v.licenses}",
                {"old": old["licenses"], "new": v.licenses}, watched))
        # 撤回
        if not old["yanked"] and v.yanked:
            alerts.append(_alert(
                "version_yanked", _escalate("high", watched), new_pkg, ver,
                f"版本被撤回：{ver}" + (f"（{v.yanked_reason}）" if v.yanked_reason else ""),
                {"reason": v.yanked_reason}, watched))

    # 新增依赖检测：同一包内，新版本相对“上一版本”多出的依赖边
    sorted_vers = resolver.sort_versions(
        new_pkg.ecosystem, [v.version for v in new_pkg.versions])
    for i in range(1, len(sorted_vers)):
        prev_v, cur_v = sorted_vers[i - 1], sorted_vers[i]
        if prev_v not in old_rows:
            continue  # 上一版本也是这次才入库，差异已在 new_version 里体现
        prev_deps = {d.name for d in new_versions[prev_v].dependencies}
        cur_deps = {d.name for d in new_versions[cur_v].dependencies}
        added = cur_deps - prev_deps
        if added:
            added_details = [d.to_dict() for d in new_versions[cur_v].dependencies
                             if d.name in added]
            alerts.append(_alert(
                "new_dependency", _escalate("medium", watched), new_pkg, cur_v,
                f"版本 {cur_v} 相对 {prev_v} 新增依赖：{', '.join(sorted(added))}",
                {"added": added_details, "from_version": prev_v}, watched))
    return alerts


def _alert(alert_type: str, severity: str, pkg: Package, version: str | None,
           title: str, detail: dict, watched: bool) -> dict:
    return {
        "type": alert_type, "severity": severity,
        "ecosystem": pkg.ecosystem, "package": pkg.name,
        "version": version, "title": title, "detail": detail, "watched": watched,
    }


def record_alerts(db, alerts: list[dict]) -> int:
    written = 0
    for a in alerts:
        if db.add_alert(a["type"], a["severity"], a["ecosystem"], a["package"],
                        a["version"], a["title"], a["detail"], a["watched"]):
            written += 1
    return written
