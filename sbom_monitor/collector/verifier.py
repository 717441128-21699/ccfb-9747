"""定期校验通道：不依赖变更流，直接对照上游清单做全量比对补漏。

职责：
1. 重新分页拉取上游完整包清单，发现库里缺失的包（含 feed 丢事件导致的漏网）
2. 对每个已入库包做条件请求，ETag 变化就重新拉详情并比对
3. 检测上游已删除的包（本仿真暂不删除，仅记录）
4. 每次运行写 verification_runs，控制台可看校验历史与修复数

这是“不重不漏”承诺的最后防线：即使变更流完全中断/丢事件，
校验通道也能把本地数据追平到上游真值。
"""
from __future__ import annotations

import logging

from ..common.model import canonical_name, make_key
from ..common.timeutil import now_iso
from .alerts import diff_package, record_alerts
from .fetcher import make_fetcher
from .httpclient import RepoClient, RepoUnavailable

log = logging.getLogger("sbom.verify")


class Verifier:
    def __init__(self, db, ecosystem: str, base_url: str, page_size: int = 50):
        self.db = db
        self.ecosystem = ecosystem
        self.page_size = page_size
        self.client = RepoClient(base_url)
        self.fetcher = make_fetcher(ecosystem, self.client)

    def run(self) -> dict:
        run_id = self._start_run()
        checked = 0
        fixed = 0
        missing = 0
        skipped: list[str] = []
        upstream_names: list[str] = []
        try:
            upstream_names = self._collect_listing()
            local_keys = {r["key"] for r in self._local_packages()}
            for name in upstream_names:
                cname = canonical_name(self.ecosystem, name)
                key = make_key(self.ecosystem, cname)
                if key not in local_keys:
                    missing += 1
                    log.info("校验发现缺失包 %s，立即补采", key)
                try:
                    changed = self._verify_one(cname, key)
                except RepoUnavailable as exc:
                    # 单包失败不拖垮整轮，但必须显式记录，不能假装校验成功
                    skipped.append({"package": cname, "error": str(exc)})
                    log.warning("校验跳过 %s：%s", cname, exc)
                    continue
                if changed:
                    fixed += 1
                checked += 1
            detail = {"upstream_packages": len(upstream_names),
                      "local_packages": len(local_keys),
                      "missing_at_start": missing, "skipped": skipped}
            status = "completed" if not skipped else "completed_with_skips"
            self._finish_run(run_id, status, checked, fixed, missing, detail)
            return {"status": status, "run_id": run_id, "checked": checked,
                    "fixed": fixed, "missing_found": missing,
                    "skipped": skipped, **detail}
        except RepoUnavailable as exc:
            self._finish_run(run_id, "failed", checked, fixed, missing,
                             {"error": str(exc)})
            return {"status": "unavailable", "run_id": run_id, "error": str(exc)}

    def _collect_listing(self) -> list[str]:
        names: list[str] = []
        page = 1
        while True:
            listing = self.fetcher.list_package_names(page, self.page_size)
            names.extend(listing["items"])
            if not listing["has_more"]:
                return names
            page += 1

    def _local_packages(self) -> list[dict]:
        import sqlite3
        with self.db._lock:
            rows = self.db.conn.execute(
                "SELECT key FROM packages WHERE ecosystem=?", (self.ecosystem,)).fetchall()
            return [dict(r) for r in rows]

    def _verify_one(self, cname: str, key: str) -> bool:
        existing = self.db.get_package(key)
        etag = existing["etag"] if existing else None
        last_modified = existing["last_modified"] if existing else None
        # RepoUnavailable 向上抛给 run()，由其计入 skipped，避免静默漏校
        pkg, headers = self.fetcher.fetch_package(cname, etag, last_modified)
        if pkg is None:
            return False  # 304，与上游一致
        if pkg is False:
            log.info("上游已移除包 %s（仿真数据不会出现，仅记录）", cname)
            return False
        watched = self.db.is_watched(key)
        alerts = diff_package(self.db, pkg, watched)
        self.db.upsert_package(pkg.to_dict(), headers.get("ETag"),
                               headers.get("Last-Modified"), content_rev=0)
        record_alerts(self.db, alerts)
        log.info("校验补采 %s（告警 %d）", key, len(alerts))
        return True

    def _start_run(self) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO verification_runs(ecosystem, started_at, status) "
                "VALUES(?,?,'running')", (self.ecosystem, now_iso()))
            return cur.lastrowid

    def _finish_run(self, run_id: int, status: str, checked: int, fixed: int,
                    missing: int, detail: dict) -> None:
        import json
        with self.db.tx() as c:
            c.execute(
                "UPDATE verification_runs SET finished_at=?, status=?, packages_checked=?, "
                "packages_fixed=?, missing_found=?, detail_json=? WHERE id=?",
                (now_iso(), status, checked, fixed, missing,
                 json.dumps(detail, ensure_ascii=False), run_id))
