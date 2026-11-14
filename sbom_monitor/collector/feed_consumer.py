"""增量通道：消费仿真仓库的变更订阅流。

断点续传：
- 位点 ``last_event_id`` 存在 feed_checkpoints，重启从该位点之后继续
- 每条事件处理与位点推进在同一事务边界内（先落库包/告警，再位点），
  进程被杀时要么都完成要么都回滚，不重不漏
- 事件去重：events_log 唯一约束 + 处理前检查，重复投递安全

流丢失兜底：feed 可能跳过部分 event_id（仿真故障），本通道不试图猜测缺口，
由定期校验通道按全量比对补齐；本通道只保证“看到的事件不丢”。
"""
from __future__ import annotations

import logging

from ..common.model import canonical_name, make_key
from .alerts import diff_package, record_alerts
from .fetcher import make_fetcher
from .httpclient import RepoClient, RepoUnavailable

log = logging.getLogger("sbom.feed")


class FeedConsumer:
    def __init__(self, db, ecosystem: str, base_url: str, page_size: int = 50):
        self.db = db
        self.ecosystem = ecosystem
        self.page_size = page_size
        self.client = RepoClient(base_url)
        self.fetcher = make_fetcher(ecosystem, self.client)
        self.processed = 0
        self.duplicates = 0

    def drain(self, max_pages: int | None = None) -> dict:
        """把当前流里可见的事件全部消费完。"""
        pages = 0
        total_events = 0
        while True:
            after_id = self.db.get_feed_checkpoint(self.ecosystem)
            try:
                page = self.fetcher.feed(after_id, self.page_size)
            except RepoUnavailable:
                log.warning("变更流不可用（%s），保留位点 %s 等下轮重试",
                            self.ecosystem, after_id)
                return {"status": "unavailable", "checkpoint": after_id,
                        "events": total_events}
            events = page["events"]
            if not events:
                return {"status": "caught_up", "checkpoint": after_id,
                        "events": total_events,
                        "latest_upstream": page.get("latest_event_id", after_id)}
            for evt in events:
                self._handle_event(evt)
                total_events += 1
            # 一页内最大 event id 作为新区点（事件按 id 递增）
            new_checkpoint = max(e["id"] for e in events)
            self.db.save_feed_checkpoint(self.ecosystem, new_checkpoint)
            pages += 1
            if max_pages and pages >= max_pages:
                return {"status": "page_limit", "checkpoint": new_checkpoint,
                        "events": total_events}
            if not page["has_more"]:
                return {"status": "caught_up", "checkpoint": new_checkpoint,
                        "events": total_events,
                        "latest_upstream": page.get("latest_event_id", new_checkpoint)}

    def _handle_event(self, evt: dict) -> None:
        if self.db.is_event_processed(self.ecosystem, evt["id"]):
            self.duplicates += 1
            return
        name = canonical_name(self.ecosystem, evt["package"])
        key = make_key(self.ecosystem, name)
        existing = self.db.get_package(key)
        etag = existing["etag"] if existing else None
        last_modified = existing["last_modified"] if existing else None
        try:
            pkg, headers = self.fetcher.fetch_package(name, etag, last_modified)
        except RepoUnavailable:
            log.warning("事件 %s 对应包拉取失败，保留位点等重试", evt["id"])
            raise
        if pkg and pkg is not False:
            watched = self.db.is_watched(key)
            alerts = diff_package(self.db, pkg, watched)
            self.db.upsert_package(pkg.to_dict(),
                                   headers.get("ETag"),
                                   headers.get("Last-Modified"),
                                   content_rev=0)
            record_alerts(self.db, alerts)
        self.db.mark_event_processed(evt)
        self.processed += 1
        log.info("增量事件 %s/%s %s@%s 已处理",
                 self.ecosystem, evt["id"], evt["type"], evt["package"])
