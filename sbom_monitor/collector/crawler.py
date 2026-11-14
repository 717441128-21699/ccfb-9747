"""全量爬取通道：分页遍历包清单，逐包条件请求入库，进度落库可断点续跑。

幂等保证：
- 清单页码存在 ``crawl_cursors``，进程重启从下一页继续
- 包写入是 UPSERT，重复抓取不产生重复数据
- 全部页跑完后置 done=1；重新触发时显式 reset 游标
"""
from __future__ import annotations

import logging

from ..common.model import make_key
from .alerts import diff_package, record_alerts
from .fetcher import make_fetcher
from .httpclient import RepoClient, RepoUnavailable

log = logging.getLogger("sbom.crawl")


class FullCrawler:
    def __init__(self, db, ecosystem: str, base_url: str, page_size: int = 20,
                 max_packages: int | None = None):
        self.db = db
        self.ecosystem = ecosystem
        self.page_size = page_size
        self.max_packages = max_packages
        self.client = RepoClient(base_url)
        self.fetcher = make_fetcher(ecosystem, self.client)
        self.processed = 0
        self.cached = 0
        self.failed: list[str] = []

    def run_once(self) -> dict:
        """跑一轮全量爬取；中途失败抛错时游标已保存，下次可续。"""
        cursor = self.db.get_cursor(self.ecosystem)
        if cursor is None:
            self.db.save_cursor(self.ecosystem, 1, False, self.page_size)
            page = 1
        else:
            page = cursor["next_page"]
            if cursor["done"]:
                return {"status": "already_done", "page": page}

        total_seen = 0
        while True:
            listing = self.fetcher.list_package_names(page, self.page_size)
            for name in listing["items"]:
                self._process_one(name)
                total_seen += 1
                if self.max_packages and total_seen >= self.max_packages:
                    self.db.save_cursor(self.ecosystem, page, False, self.page_size)
                    return self._summary(page, done=False)
            self.db.save_cursor(self.ecosystem, page + 1,
                                not listing["has_more"], self.page_size)
            if not listing["has_more"]:
                return self._summary(page, done=True)
            page += 1

    def _process_one(self, name: str) -> None:
        from ..common.model import canonical_name
        cname = canonical_name(self.ecosystem, name)
        key = make_key(self.ecosystem, cname)
        existing = self.db.get_package(key)
        etag = existing["etag"] if existing else None
        last_modified = existing["last_modified"] if existing else None
        try:
            pkg, headers = self.fetcher.fetch_package(cname, etag, last_modified)
        except RepoUnavailable:
            self.failed.append(name)
            log.warning("仓库不可用，跳过 %s（等待校验通道补）", name)
            return
        if pkg is None:
            self.cached += 1
            return  # 304，无变化
        if pkg is False:
            log.info("包已在上游消失：%s", name)
            return
        resp_etag = headers.get("ETag")
        resp_lm = headers.get("Last-Modified")
        watched = self.db.is_watched(key)
        alerts = diff_package(self.db, pkg, watched)
        is_new = self.db.upsert_package(pkg.to_dict(), resp_etag, resp_lm,
                                        content_rev=0)
        record_alerts(self.db, alerts)
        self.processed += 1
        log.info("爬取 %s %s（新版本数=%d，告警=%d，新包=%s）",
                 self.ecosystem, cname, len(pkg.versions), len(alerts), is_new)

    def _summary(self, page: int, done: bool) -> dict:
        return {
            "status": "done" if done else "paused_at_limit",
            "ecosystem": self.ecosystem, "last_page": page,
            "processed": self.processed, "cached_304": self.cached,
            "failed": self.failed,
            "http": self.client.stats,
        }
