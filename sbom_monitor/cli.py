"""命令行入口（``py run.py <command>``）。

子命令：
  init-db       建库并写入三个仿真仓库配置/关注名单
  sim           启动三个仿真仓库 + 变异器（前台运行）
  crawl         跑一轮全量爬取后退出
  feed          消费一轮变更流后退出
  verify        跑一轮定期校验后退出
  engine        常驻采集引擎（全量→增量→周期校验）
  web           启动 Web 控制台（常驻）
  reconcile     与仿真真值对账并打印报告
  demo          一条命令：仿真库+引擎+控制台 全部拉起
  reset         清空采集库与仿真状态（重新演示用）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from .collector.engine import Engine
from .collector.storage import Database
from .common.timeutil import now_iso
from .reconcile.checker import Reconciler, format_report
from .sim.cluster import DEFAULT_PORTS, SimCluster

DEFAULT_WORKDIR = os.path.join(os.getcwd(), "data")
DB_PATH = os.path.join(DEFAULT_WORKDIR, "sbom.db")
SIM_DIR = os.path.join(DEFAULT_WORKDIR, "sim")
REPO_CFG = os.path.join(DEFAULT_WORKDIR, "repos.json")


def base_urls() -> dict[str, str]:
    if os.path.exists(REPO_CFG):
        with open(REPO_CFG, "r", encoding="utf-8") as f:
            return json.load(f)["base_urls"]
    return {eco: f"http://127.0.0.1:{port}" for eco, port in DEFAULT_PORTS.items()}


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S")


def init_db(db: Database, watch_seed: bool = True) -> None:
    urls = base_urls()
    for eco, url in urls.items():
        db.upsert_repo(eco, url, rate_per_sec=20.0, burst=50, enabled=True)
    if watch_seed:
        # 预置几条关注名单，验证告警优先级提升
        for eco, name in (("npm", "@acme/core-lib"), ("pypi", "requests-like"),
                          ("maven", "com.acme.platform:core")):
            try:
                db.add_watch(eco, name, "支付网关", "核心业务依赖")
            except Exception:
                pass


# ---------- 子命令 ----------

def cmd_init_db(args) -> int:
    os.makedirs(DEFAULT_WORKDIR, exist_ok=True)
    db = Database(DB_PATH)
    init_db(db)
    print(f"数据库已初始化：{DB_PATH}")
    for r in db.list_repos():
        print(f"  仓库 {r['ecosystem']:6s} -> {r['base_url']}")
    db.close()
    return 0


def cmd_sim(args) -> int:
    cluster = SimCluster(SIM_DIR, mutate_interval=args.interval,
                         drop_ratio=args.drop_ratio)
    cluster.start(populate=True)
    cluster.wait_healthy()
    cluster.write_repo_config(REPO_CFG)
    urls = cluster.base_urls()
    print("仿真仓库已启动（Ctrl+C 退出）：")
    for eco, url in urls.items():
        print(f"  {eco:6s} {url}  (变更流 {url}/feed, 真值 {url}/__ground_truth)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止仿真仓库...")
        cluster.stop()
    return 0


def _run_single_channel(args, channel: str) -> int:
    db = Database(DB_PATH)
    urls = base_urls()
    from .collector.crawler import FullCrawler
    from .collector.feed_consumer import FeedConsumer
    from .collector.verifier import Verifier
    for eco in (args.ecosystem or ("npm", "pypi", "maven")):
        if channel == "crawl":
            res = FullCrawler(db, eco, urls[eco], page_size=args.page_size,
                              max_packages=args.max_packages).run_once()
        elif channel == "feed":
            res = FeedConsumer(db, eco, urls[eco]).drain()
        else:
            res = Verifier(db, eco, urls[eco]).run()
        print(f"[{eco}] {json.dumps(res, ensure_ascii=False)}")
    db.close()
    return 0


def cmd_crawl(args) -> int:
    return _run_single_channel(args, "crawl")


def cmd_feed(args) -> int:
    return _run_single_channel(args, "feed")


def cmd_verify(args) -> int:
    return _run_single_channel(args, "verify")


def cmd_engine(args) -> int:
    db = Database(DB_PATH)
    init_db(db)
    engine = Engine(db, feed_interval=args.feed_interval,
                    verify_interval=args.verify_interval,
                    page_size=args.page_size, max_packages=args.max_packages)
    engine.start()
    print(f"采集引擎已启动，{len(engine.workers)} 个仓库工作线程运行中（Ctrl+C 退出）")
    try:
        engine.wait()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        db.close()
    return 0


def cmd_web(args) -> int:
    from .web.server import start_web
    db = Database(DB_PATH)
    init_db(db)
    server = start_web(db, port=args.port)
    print(f"Web 控制台：http://127.0.0.1:{args.port}（Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        db.close()
    return 0


def cmd_reconcile(args) -> int:
    db = Database(DB_PATH)
    urls = base_urls()
    ecos = (args.ecosystem or tuple(urls))
    report = Reconciler(db, {eco: urls[eco] for eco in ecos}).run()
    print(format_report(report))
    if args.json:
        out = os.path.join(DEFAULT_WORKDIR, "reconcile-report.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"JSON 报告已写入：{out}")
    db.close()
    return 0 if report["ok"] else 2


def cmd_demo(args) -> int:
    os.makedirs(DEFAULT_WORKDIR, exist_ok=True)
    cluster = SimCluster(SIM_DIR, mutate_interval=args.mutate_interval,
                         drop_ratio=args.drop_ratio)
    cluster.start(populate=True)
    cluster.wait_healthy()
    cluster.write_repo_config(REPO_CFG)
    db = Database(DB_PATH)
    init_db(db)
    engine = Engine(db, feed_interval=args.feed_interval,
                    verify_interval=args.verify_interval,
                    page_size=args.page_size, max_packages=args.max_packages)
    engine.start()
    from .web.server import start_web
    web = start_web(db, port=args.port)
    print("=" * 64)
    print("演示环境已就绪：")
    for eco, url in cluster.base_urls().items():
        print(f"  仿真仓库 {eco:6s} {url}")
    print(f"  Web 控制台        http://127.0.0.1:{args.port}")
    print("=" * 64)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭演示环境...")
        engine.stop()
        web.shutdown()
        cluster.stop()
        db.close()
    return 0


def cmd_reset(args) -> int:
    import shutil
    if os.path.exists(DEFAULT_WORKDIR):
        shutil.rmtree(DEFAULT_WORKDIR)
    print(f"已清空工作目录：{DEFAULT_WORKDIR}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sbom-monitor",
                                description="软件供应链合规监测（本地仿真）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    s = sub.add_parser("sim", help="启动仿真仓库集群")
    s.add_argument("--interval", type=float, default=3.0, help="变异间隔秒")
    s.add_argument("--drop-ratio", type=float, default=0.15, help="feed 丢事件比例")
    s.set_defaults(func=cmd_sim)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ecosystem", nargs="*", choices=("npm", "pypi", "maven"))
    common.add_argument("--page-size", type=int, default=20)
    common.add_argument("--max-packages", type=int, default=None)

    sub.add_parser("crawl", parents=[common]).set_defaults(func=cmd_crawl)
    sub.add_parser("feed", parents=[common]).set_defaults(func=cmd_feed)
    sub.add_parser("verify", parents=[common]).set_defaults(func=cmd_verify)

    e = sub.add_parser("engine", parents=[common])
    e.add_argument("--feed-interval", type=float, default=2.0)
    e.add_argument("--verify-interval", type=float, default=30.0)
    e.set_defaults(func=cmd_engine)

    w = sub.add_parser("web")
    w.add_argument("--port", type=int, default=8080)
    w.set_defaults(func=cmd_web)

    r = sub.add_parser("reconcile")
    r.add_argument("--ecosystem", nargs="*", choices=("npm", "pypi", "maven"))
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_reconcile)

    d = sub.add_parser("demo")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--feed-interval", type=float, default=2.0)
    d.add_argument("--verify-interval", type=float, default=30.0)
    d.add_argument("--mutate-interval", type=float, default=3.0)
    d.add_argument("--drop-ratio", type=float, default=0.15)
    d.add_argument("--page-size", type=int, default=20)
    d.add_argument("--max-packages", type=int, default=None)
    d.set_defaults(func=cmd_demo)

    sub.add_parser("reset").set_defaults(func=cmd_reset)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    return args.func(args)
