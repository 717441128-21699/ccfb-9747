"""SQLite 存储层。

设计要点：
- WAL 模式 + 业务级事务，进程被杀时已提交事务不丢、未提交事务回滚
- 所有“进度”（全量游标、feed 位点、校验水位）都在库里，重启自动续跑
- 包/版本写入走 UPSERT，天然幂等，at-least-once 采集不会产生重复行
- 依赖边单独建表，重写前先删旧边，保证与上游真值一致
- 告警用 (类型, key, 版本, 指纹) 唯一约束去重
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    rate_per_sec REAL NOT NULL DEFAULT 20,
    burst INTEGER NOT NULL DEFAULT 50,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    key TEXT PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    project_url TEXT DEFAULT '',
    repo_url TEXT DEFAULT '',
    licenses TEXT DEFAULT 'NOASSERTION',
    latest_version TEXT DEFAULT '',
    first_seen TEXT,
    last_seen TEXT,
    updated_at TEXT,
    etag TEXT,
    last_modified TEXT,
    content_rev INTEGER DEFAULT 0,
    raw_json TEXT,
    UNIQUE(ecosystem, name)
);

CREATE TABLE IF NOT EXISTS package_versions (
    key TEXT NOT NULL,
    version TEXT NOT NULL,
    released_at TEXT,
    licenses TEXT DEFAULT 'NOASSERTION',
    yanked INTEGER NOT NULL DEFAULT 0,
    yanked_reason TEXT,
    artifacts_json TEXT,
    dependencies_json TEXT,
    raw_json TEXT,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (key, version),
    FOREIGN KEY (key) REFERENCES packages(key)
);

CREATE TABLE IF NOT EXISTS dependency_edges (
    source_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    target_ecosystem TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_key TEXT NOT NULL,
    spec TEXT NOT NULL,
    optional INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_key, source_version, target_key, spec),
    FOREIGN KEY (source_key) REFERENCES packages(key)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON dependency_edges(target_key);
CREATE INDEX IF NOT EXISTS idx_edges_source ON dependency_edges(source_key);

CREATE TABLE IF NOT EXISTS feed_checkpoints (
    ecosystem TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crawl_cursors (
    ecosystem TEXT PRIMARY KEY,
    next_page INTEGER NOT NULL DEFAULT 1,
    page_size INTEGER NOT NULL DEFAULT 20,
    done INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS verification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ecosystem TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    packages_checked INTEGER DEFAULT 0,
    packages_fixed INTEGER DEFAULT 0,
    missing_found INTEGER DEFAULT 0,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS events_log (
    event_id INTEGER NOT NULL,
    ecosystem TEXT NOT NULL,
    type TEXT NOT NULL,
    package TEXT NOT NULL,
    payload_json TEXT,
    processed_at TEXT,
    PRIMARY KEY (ecosystem, event_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    package TEXT NOT NULL,
    version TEXT,
    title TEXT NOT NULL,
    detail_json TEXT,
    watched INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, severity);

CREATE TABLE IF NOT EXISTS watchlist (
    key TEXT PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    name TEXT NOT NULL,
    component TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = __import__("threading").RLock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化的事务上下文；异常回滚，正常提交。"""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ---------- 仓库配置 ----------

    def upsert_repo(self, ecosystem: str, base_url: str, rate_per_sec: float,
                    burst: int, enabled: bool = True) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO repositories(ecosystem, base_url, enabled, rate_per_sec, burst, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(ecosystem) DO UPDATE SET "
                "base_url=excluded.base_url, enabled=excluded.enabled, "
                "rate_per_sec=excluded.rate_per_sec, burst=excluded.burst",
                (ecosystem, base_url, int(enabled), rate_per_sec, burst,
                 _now()),
            )

    def list_repos(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM repositories ORDER BY ecosystem"))

    # ---------- 包/版本 UPSERT ----------

    def upsert_package(self, pkg: dict, etag: str | None, last_modified: str | None,
                       content_rev: int) -> bool:
        """写包及其版本、依赖边。返回是否为首次见到的新包。"""
        from ..common.model import canonical_name
        from ..common.timeutil import now_iso
        key = pkg["key"]
        now = now_iso()
        with self.tx() as c:
            old = c.execute("SELECT key, first_seen FROM packages WHERE key=?",
                            (key,)).fetchone()
            is_new = old is None
            c.execute(
                "INSERT INTO packages(key, ecosystem, name, display_name, description, "
                "project_url, repo_url, licenses, latest_version, first_seen, last_seen, "
                "updated_at, etag, last_modified, content_rev, raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET display_name=excluded.display_name, "
                "description=excluded.description, project_url=excluded.project_url, "
                "repo_url=excluded.repo_url, licenses=excluded.licenses, "
                "latest_version=excluded.latest_version, last_seen=excluded.last_seen, "
                "updated_at=excluded.updated_at, etag=excluded.etag, "
                "last_modified=excluded.last_modified, content_rev=excluded.content_rev, "
                "raw_json=excluded.raw_json",
                (key, pkg["ecosystem"], pkg["name"], pkg.get("display_name", ""),
                 pkg.get("description", ""), pkg.get("project_url", ""),
                 pkg.get("repo_url", ""), pkg.get("licenses", "NOASSERTION"),
                 pkg.get("latest_version", ""),
                 now if is_new else (old["first_seen"] if old else now),
                 now, pkg.get("updated_at", ""), etag, last_modified, content_rev,
                 json.dumps(pkg.get("raw", {}), ensure_ascii=False)),
            )
            for v in pkg["versions"]:
                self._upsert_version(c, key, pkg["ecosystem"], v, now, is_new)
            # 重写依赖边：先删该包全部边，再按当前版本写入
            c.execute("DELETE FROM dependency_edges WHERE source_key=?", (key,))
            for v in pkg["versions"]:
                self._replace_edges(c, key, v, pkg["ecosystem"])
            return is_new

    def _upsert_version(self, c: sqlite3.Connection, key: str, ecosystem: str,
                        v: dict, now: str, pkg_is_new: bool) -> None:
        existing = c.execute(
            "SELECT 1 FROM package_versions WHERE key=? AND version=?",
            (key, v["version"])).fetchone()
        first_seen = now if existing is None or pkg_is_new else now
        c.execute(
            "INSERT INTO package_versions(key, version, released_at, licenses, yanked, "
            "yanked_reason, artifacts_json, dependencies_json, raw_json, first_seen, last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key, version) DO UPDATE SET "
            "released_at=excluded.released_at, licenses=excluded.licenses, "
            "yanked=excluded.yanked, yanked_reason=excluded.yanked_reason, "
            "artifacts_json=excluded.artifacts_json, dependencies_json=excluded.dependencies_json, "
            "raw_json=excluded.raw_json, last_seen=excluded.last_seen",
            (key, v["version"], v.get("released_at", ""), v.get("licenses", "NOASSERTION"),
             int(bool(v.get("yanked", False))), v.get("yanked_reason"),
             json.dumps(v.get("artifacts", []), ensure_ascii=False),
             json.dumps([d if isinstance(d, dict) else d.to_dict()
                         for d in v.get("dependencies", [])], ensure_ascii=False),
             json.dumps(v.get("raw", {}), ensure_ascii=False),
             first_seen, now),
        )

    def _replace_edges(self, c: sqlite3.Connection, source_key: str,
                       v: dict, ecosystem: str) -> None:
        from ..common.model import canonical_name, make_key
        for d in v.get("dependencies", []):
            d = d if isinstance(d, dict) else d.to_dict()
            target_eco = d.get("ecosystem") or ecosystem
            target_name = canonical_name(target_eco, d["name"])
            target_key = make_key(target_eco, target_name)
            c.execute(
                "INSERT OR IGNORE INTO dependency_edges(source_key, source_version, "
                "target_ecosystem, target_name, target_key, spec, optional) "
                "VALUES(?,?,?,?,?,?,?)",
                (source_key, v["version"], target_eco, target_name, target_key,
                 d.get("spec", ""), int(bool(d.get("optional", False)))),
            )

    def get_package(self, key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM packages WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def get_package_versions(self, key: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM package_versions WHERE key=? ORDER BY released_at",
                (key,)).fetchall()
            return [dict(r) for r in rows]

    def count_packages(self, ecosystem: str | None = None) -> int:
        with self._lock:
            if ecosystem:
                return self.conn.execute(
                    "SELECT COUNT(*) n FROM packages WHERE ecosystem=?", (ecosystem,)).fetchone()["n"]
            return self.conn.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]

    def count_versions(self, ecosystem: str | None = None) -> int:
        with self._lock:
            if ecosystem:
                return self.conn.execute(
                    "SELECT COUNT(*) n FROM package_versions pv JOIN packages p ON p.key=pv.key "
                    "WHERE p.ecosystem=?", (ecosystem,)).fetchone()["n"]
            return self.conn.execute("SELECT COUNT(*) n FROM package_versions").fetchone()["n"]

    # ---------- 进度位点 ----------

    def get_cursor(self, ecosystem: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM crawl_cursors WHERE ecosystem=?", (ecosystem,)).fetchone()
            return dict(row) if row else None

    def save_cursor(self, ecosystem: str, next_page: int, done: bool,
                    page_size: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO crawl_cursors(ecosystem, next_page, page_size, done, started_at, updated_at) "
                "VALUES(?,?,?,?,(SELECT started_at FROM crawl_cursors WHERE ecosystem=?),?) "
                "ON CONFLICT(ecosystem) DO UPDATE SET next_page=excluded.next_page, "
                "page_size=excluded.page_size, done=excluded.done, updated_at=excluded.updated_at",
                (ecosystem, next_page, page_size, int(done), ecosystem, _now()),
            )
            if done:
                c.execute(
                    "UPDATE crawl_cursors SET started_at=COALESCE(started_at, ?) WHERE ecosystem=?",
                    (_now(), ecosystem))

    def reset_cursor(self, ecosystem: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM crawl_cursors WHERE ecosystem=?", (ecosystem,))

    def get_feed_checkpoint(self, ecosystem: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_event_id FROM feed_checkpoints WHERE ecosystem=?",
                (ecosystem,)).fetchone()
            return row["last_event_id"] if row else 0

    def save_feed_checkpoint(self, ecosystem: str, last_event_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO feed_checkpoints(ecosystem, last_event_id, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(ecosystem) DO UPDATE SET "
                "last_event_id=excluded.last_event_id, updated_at=excluded.updated_at",
                (ecosystem, last_event_id, _now()),
            )

    def mark_event_processed(self, event: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO events_log(event_id, ecosystem, type, package, "
                "payload_json, processed_at) VALUES(?,?,?,?,?,?)",
                (event["id"], event["ecosystem"], event["type"], event["package"],
                 json.dumps(event.get("payload", {}), ensure_ascii=False), _now()),
            )

    def is_event_processed(self, ecosystem: str, event_id: int) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM events_log WHERE ecosystem=? AND event_id=?",
                (ecosystem, event_id)).fetchone() is not None

    # ---------- 告警 ----------

    def add_alert(self, alert_type: str, severity: str, ecosystem: str,
                  package: str, version: str | None, title: str,
                  detail: dict, watched: bool) -> bool:
        import hashlib
        fp_src = f"{alert_type}|{ecosystem}|{package}|{version or ''}|" \
                 f"{json.dumps(detail, sort_keys=True, ensure_ascii=False)}"
        fingerprint = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO alerts(fingerprint, type, severity, ecosystem, package, "
                "version, title, detail_json, watched, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)",
                (fingerprint, alert_type, severity, ecosystem, package, version, title,
                 json.dumps(detail, ensure_ascii=False), int(watched), now, now),
            )
            return cur.rowcount > 0

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM alerts WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- 关注名单 ----------

    def add_watch(self, ecosystem: str, name: str, component: str, note: str) -> None:
        from ..common.model import canonical_name, make_key
        cname = canonical_name(ecosystem, name)
        key = make_key(ecosystem, cname)
        with self.tx() as c:
            c.execute(
                "INSERT INTO watchlist(key, ecosystem, name, component, added_at, note) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET component=excluded.component, "
                "note=excluded.note",
                (key, ecosystem, cname, component, _now(), note),
            )

    def is_watched(self, key: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM watchlist WHERE key=?", (key,)).fetchone() is not None

    def list_watchlist(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM watchlist ORDER BY added_at DESC")]

    # ---------- 依赖图查询 ----------

    def upstream(self, key: str) -> list[dict]:
        """谁依赖我（入边）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT source_key, target_key, spec, optional "
                "FROM dependency_edges WHERE target_key=?", (key,)).fetchall()
            return [dict(r) for r in rows]

    def downstream(self, key: str) -> list[dict]:
        """我依赖谁（出边）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT source_key, target_key, spec, optional "
                "FROM dependency_edges WHERE source_key=?", (key,)).fetchall()
            return [dict(r) for r in rows]

    def all_edges(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT source_key, source_version, target_ecosystem, target_name, "
                "target_key, spec, optional FROM dependency_edges")]

    # ---------- kv ----------

    def set_meta(self, k: str, v: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv_meta(k,v) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def get_meta(self, k: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT v FROM kv_meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else None


def _now() -> str:
    from ..common.timeutil import now_iso
    return now_iso()
