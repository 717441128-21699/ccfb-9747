"""采集引擎：为每个仓库独立调度 全量 / 增量 / 校验 三个通道。

调度模型（每个仓库一个工作线程）：
1. 全量爬取未完成时优先跑全量（断点续跑）
2. 全量完成后循环：消费变更流 -> 休眠 -> 周期性触发校验
3. 任一通道抛出 RepoUnavailable 只隔离该仓库，退避后重试，其他仓库不受影响

进程韧性：
- 所有进度在 SQLite，线程只做无状态调度，kill -9 后重启自动续
- 每轮结果写 engine_runs 日志（通过 kv_meta 记录心跳），控制台可见存活状态
"""
from __future__ import annotations

import logging
import threading
import time

from ..common.timeutil import now_iso
from .crawler import FullCrawler
from .feed_consumer import FeedConsumer
from .httpclient import RepoUnavailable
from .verifier import Verifier

log = logging.getLogger("sbom.engine")


class RepoWorker(threading.Thread):
    def __init__(self, db, repo_row: dict, feed_interval: float,
                 verify_interval: float, stop_event: threading.Event,
                 page_size: int = 20, max_packages: int | None = None):
        super().__init__(name=f"worker-{repo_row['ecosystem']}", daemon=True)
        self.db = db
        self.repo = repo_row
        self.feed_interval = feed_interval
        self.verify_interval = verify_interval
        self.stop_event = stop_event
        self.page_size = page_size
        self.max_packages = max_packages
        self.last_verify = 0.0
        self.status = "starting"
        self.last_summary: dict = {}
        self.consecutive_failures = 0

    def run(self) -> None:
        eco = self.repo["ecosystem"]
        base_url = self.repo["base_url"]
        log.info("工作线程启动：%s @ %s", eco, base_url)
        while not self.stop_event.is_set():
            try:
                self._cycle(eco, base_url)
                self.consecutive_failures = 0
            except RepoUnavailable as exc:
                self.consecutive_failures += 1
                self.status = f"backoff(failures={self.consecutive_failures})"
                self.last_summary = {"error": str(exc), "at": now_iso()}
                self._heartbeat(eco)
                # 退避等待但可被停止信号唤醒
                if self.stop_event.wait(min(30.0, 2 ** self.consecutive_failures)):
                    break
                continue
            except Exception as exc:  # 防御：任何意外异常都不能杀死工作线程
                log.exception("工作线程 %s 未预期异常", eco)
                self.status = "error"
                self.last_summary = {"error": repr(exc), "at": now_iso()}
                if self.stop_event.wait(5.0):
                    break
        self.status = "stopped"
        log.info("工作线程退出：%s", eco)

    def _cycle(self, eco: str, base_url: str) -> None:
        # 1. 全量优先
        cursor = self.db.get_cursor(eco)
        if cursor is None or not cursor["done"]:
            self.status = "full_crawl"
            crawler = FullCrawler(self.db, eco, base_url, self.page_size,
                                  self.max_packages)
            self.last_summary = crawler.run_once()
            self._heartbeat(eco)
            if self.stop_event.wait(self.feed_interval):
                return
            return

        # 2. 增量
        self.status = "feed"
        consumer = FeedConsumer(self.db, eco, base_url)
        self.last_summary = consumer.drain()
        self._heartbeat(eco)

        # 3. 周期校验
        now = time.monotonic()
        if now - self.last_verify >= self.verify_interval:
            self.status = "verifying"
            verifier = Verifier(self.db, eco, base_url, self.page_size)
            self.last_summary = verifier.run()
            self.last_verify = now
            self._heartbeat(eco)

        if self.stop_event.wait(self.feed_interval):
            return

    def _heartbeat(self, eco: str) -> None:
        import json
        self.db.set_meta(f"heartbeat:{eco}", json.dumps(
            {"status": self.status, "at": now_iso(),
             "summary": self.last_summary,
             "failures": self.consecutive_failures}, ensure_ascii=False))


class Engine:
    def __init__(self, db, feed_interval: float = 2.0,
                 verify_interval: float = 30.0, page_size: int = 20,
                 max_packages: int | None = None, repos: list[dict] | None = None):
        self.db = db
        self.feed_interval = feed_interval
        self.verify_interval = verify_interval
        self.page_size = page_size
        self.max_packages = max_packages
        self._stop = threading.Event()
        self.workers: list[RepoWorker] = []
        self._repos_override = repos

    def start(self) -> None:
        repos = self._repos_override or [dict(r) for r in self.db.list_repos()
                                         if r["enabled"]]
        for row in repos:
            w = RepoWorker(self.db, row, self.feed_interval, self.verify_interval,
                           self._stop, self.page_size, self.max_packages)
            self.workers.append(w)
            w.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for w in self.workers:
            w.join(timeout=timeout)

    def wait(self) -> None:
        """阻塞直到所有工作线程结束（一般配合外部信号）。"""
        try:
            while any(w.is_alive() for w in self.workers):
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    def worker_status(self) -> list[dict]:
        out = []
        for w in self.workers:
            out.append({"ecosystem": w.repo["ecosystem"], "alive": w.is_alive(),
                        "status": w.status, "failures": w.consecutive_failures,
                        "summary": w.last_summary})
        return out
