"""仿真仓库 HTTP 服务（stdlib ``http.server``，线程池处理请求）。

每个生态起一个独立端口，互不影响。统一能力：
- 分页的包清单（npm/PyPI 风格 JSON、Maven 风格 XML 目录）
- 包详情/版本详情的条件请求：If-None-Match(ETag) / If-Modified-Since
- 变更订阅流：GET /feed?after_id=&limit=，分页 + 断点
- 限流：令牌桶，超额返回 429 + Retry-After
- 故障注入：管理端点切换 offline(503)/error_rate(随机5xx)
- 真值端点：GET /__ground_truth（仅对账器使用，采集端禁止访问）
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from ..common.timeutil import parse_http_date
from . import serializers
from .ratelimit import TokenBucket
from .state import SimState

DEFAULT_PAGE_SIZE = 20


class SimHTTPHandler(BaseHTTPRequestHandler):
    # 由 server 实例注入：server.state / server.bucket / server.rng
    server: "SimServer"

    def log_message(self, fmt, *args):  # 安静点
        pass

    # ---------- 公共处理骨架 ----------

    def _check_available(self) -> bool:
        state = self.server.state
        if state.offline:
            self._send_json(503, {"error": "repository offline (fault injection)"},
                            extra={"Retry-After": "5"})
            return False
        if state.error_rate > 0 and self.server.rng.random() < state.error_rate:
            self._send_json(500, {"error": "simulated internal error"})
            return False
        ok, wait = self.server.bucket.take()
        if not ok:
            self._send_json(429, {"error": "rate limited"},
                            extra={"Retry-After": f"{max(1, int(wait + 0.999))}"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._check_available():
            return
        parsed = urlparse(self.path)
        try:
            self.route(parsed.path, parse_qs(parsed.query))
        except Exception as exc:  # 仿真服务自身异常不应让进程崩溃
            self._send_json(500, {"error": f"sim bug: {exc!r}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/__admin__/fault":
            self._send_json(404, {"error": "unknown endpoint"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rdr_read(length) or "{}")
        state = self.server.state
        with state.lock:
            if "offline" in body:
                state.offline = bool(body["offline"])
            if "error_rate" in body:
                state.error_rate = float(body["error_rate"])
        self._send_json(200, {"offline": state.offline, "error_rate": state.error_rate})

    def rdr_read(self, length: int) -> str:
        return self.rfile.read(length).decode("utf-8")

    # ---------- 路由 ----------

    def route(self, path: str, query: dict) -> None:
        state = self.server.state
        eco = state.ecosystem
        if path == "/health":
            return self._send_json(200, {"status": "ok", "ecosystem": eco,
                                         "offline": state.offline})
        if path == "/__ground_truth":
            return self._send_json(200, state.ground_truth())
        if path == "/feed":
            return self._feed(query)
        if eco == "npm":
            return self._npm_routes(path, query)
        if eco == "pypi":
            return self._pypi_routes(path, query)
        return self._maven_routes(path, query)

    # ---------- 变更流 ----------

    def _feed(self, query: dict) -> None:
        after_id = int(query.get("after_id", ["0"])[0])
        limit = min(int(query.get("limit", [DEFAULT_PAGE_SIZE])[0]), 100)
        data = self.server.state.feed(after_id, limit)
        self._send_json(200, data)

    # ---------- npm ----------

    def _npm_routes(self, path: str, query: dict) -> None:
        if path in ("/-/all", "/packages"):
            return self._listing_json(query)
        name = unquote(path.lstrip("/"))
        pkg = self.server.state.packages.get(name)
        if pkg is None:
            return self._send_json(404, {"error": "package not found"})
        if not self._not_modified(pkg):
            body = serializers.npm_packument(pkg)
            self._send_json(200, body, etag=self.server.state.package_etag(name),
                            last_modified=serializers.last_modified(pkg))

    # ---------- pypi ----------

    def _pypi_routes(self, path: str, query: dict) -> None:
        if path in ("/simple/", "/simple", "/packages"):
            return self._listing_json(query)
        # /pypi/<name>/json 与 /pypi/<name>/<version>/json
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "pypi":
            name = unquote(parts[1])
            pkg = self.server.state.packages.get(name)
            if pkg is None:
                return self._send_json(404, {"error": "package not found"})
            if len(parts) >= 3 and parts[2] != "json":
                version = parts[2]
                body = serializers.pypi_version_json(pkg, version)
                if body is None:
                    return self._send_json(404, {"error": "version not found"})
                return self._send_json(200, body,
                                       etag=self.server.state.package_etag(name),
                                       last_modified=serializers.last_modified(pkg))
            if not self._not_modified(pkg):
                body = serializers.pypi_package_json(pkg)
                self._send_json(200, body,
                                etag=self.server.state.package_etag(name),
                                last_modified=serializers.last_modified(pkg))
            return
        # /simple/<name>/ 也给一个简单索引页（PEP 503 风格）
        if parts and parts[0] == "simple" and len(parts) >= 2:
            name = unquote(parts[1])
            pkg = self.server.state.packages.get(name)
            if pkg is None:
                return self._send_json(404, {"error": "package not found"})
            files = [{"name": v["artifacts"][0]["name"],
                      "url": v["artifacts"][0]["url"],
                      "yanked": v["yanked"]} for v in pkg["versions"]]
            return self._send_json(200, {"name": name, "files": files})
        self._send_json(404, {"error": "unknown pypi path"})

    # ---------- maven ----------

    def _maven_routes(self, path: str, query: dict) -> None:
        if path in ("/packages", "/"):
            return self._listing_json(query) if path == "/packages" else \
                self._send_json(200, {"ecosystem": "maven",
                                      "paths": self._maven_listing_paths()})
        # /<group 路径>/<artifact>/maven-metadata.xml
        if path.endswith("/maven-metadata.xml"):
            coord = self._path_to_coord(path[: -len("/maven-metadata.xml")])
            pkg = self.server.state.packages.get(coord)
            if pkg is None:
                return self._send_xml(404, "<error>not found</error>")
            if not self._not_modified(pkg):
                self._send_xml(200, serializers.maven_metadata_xml(pkg),
                               etag=self.server.state.package_etag(coord),
                               last_modified=serializers.last_modified(pkg))
            return
        # /<group>/<artifact>/<version>/<artifact>-<version>.pom
        if path.endswith(".pom"):
            coord, version = self._pom_path(path)
            pkg = self.server.state.packages.get(coord)
            v = next((x for x in pkg["versions"] if x["version"] == version), None) \
                if pkg else None
            if v is None:
                return self._send_xml(404, "<error>not found</error>")
            if not self._not_modified(pkg):
                self._send_xml(200, serializers.maven_pom_xml(pkg, v),
                               etag=self.server.state.package_etag(coord),
                               last_modified=serializers.last_modified(pkg))
            return
        # 目录 XML
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            coord = ":".join([".".join(parts[:-1]), parts[-1]])
            pkg = self.server.state.packages.get(coord)
            if pkg is not None:
                return self._send_xml(200, serializers.maven_directory_xml(pkg),
                                      etag=self.server.state.package_etag(coord))
        self._send_xml(404, "<error>unknown maven path</error>")

    def _maven_listing_paths(self) -> list[str]:
        out = []
        for name in sorted(self.server.state.packages):
            g, a = name.split(":", 1)
            out.append("/".join([g.replace(".", "/"), a]))
        return out

    @staticmethod
    def _path_to_coord(prefix: str) -> str:
        parts = [p for p in prefix.split("/") if p]
        artifact = parts[-1]
        group = ".".join(parts[:-1])
        return f"{group}:{artifact}"

    def _pom_path(self, path: str) -> tuple[str, str]:
        # .../group/artifact/version/artifact-version.pom
        parts = [p for p in path.split("/") if p]
        version = parts[-2]
        artifact = parts[-3]
        group = ".".join(parts[:-3])
        return f"{group}:{artifact}", version

    # ---------- 分页清单 ----------

    def _listing_json(self, query: dict) -> None:
        state = self.server.state
        page = int(query.get("page", ["1"])[0])
        page_size = min(int(query.get("page_size", [DEFAULT_PAGE_SIZE])[0]), 100)
        names = sorted(state.packages.keys())
        start = (page - 1) * page_size
        slice_ = names[start:start + page_size]
        items = [{"name": n,
                  "latest": state.packages[n]["versions"][-1]["version"]} for n in slice_]
        self._send_json(200, {
            "ecosystem": state.ecosystem,
            "page": page, "page_size": page_size,
            "total": len(names),
            "has_more": start + page_size < len(names),
            "items": items,
        }, etag=state.listing_etag())

    # ---------- 条件请求 ----------

    def _not_modified(self, pkg: dict) -> bool:
        """命中条件请求返回 304 并返回 True；否则 False。

        规则（RFC 7232 求值顺序）：
        - 客户端发了 If-None-Match 时，只认 ETag 精确匹配，绝不回退到日期判断
          （yank/许可证变更不改变发布时间，仅日期会漏判）
        - 没发 ETag 时才用 If-Modified-Since，且 Last-Modified 反映对象最后变更
        """
        etag = self.server.state.package_etag(pkg["name"])
        inm = self.headers.get("If-None-Match")
        if inm:
            if etag and inm == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", serializers.last_modified(pkg))
                self.end_headers()
                return True
            return False
        ims = self.headers.get("If-Modified-Since")
        if ims:
            since = parse_http_date(ims)
            latest = parse_http_date(serializers.last_modified(pkg))
            if since is not None and latest is not None and latest <= since:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return True
        return False

    # ---------- 底层发送 ----------

    def _send_json(self, status: int, payload: dict, extra: dict | None = None,
                   etag: str | None = None, last_modified: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_xml(self, status: int, xml: str, etag: str | None = None,
                  last_modified: str | None = None) -> None:
        body = xml.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        self.end_headers()
        self.wfile.write(body)


class SimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], state: SimState,
                 rate_per_sec: float, burst: int, seed: int):
        super().__init__(addr, SimHTTPHandler)
        self.state = state
        self.bucket = TokenBucket(rate_per_sec, burst)
        self.rng = random.Random(seed)

    def set_rate_limit(self, rate_per_sec: float, burst: int) -> None:
        self.bucket.update_config(rate_per_sec, burst)


def start_server(state: SimState, host: str, port: int,
                 rate_per_sec: float = 50.0, burst: int = 100,
                 seed: int = 0) -> SimServer:
    server = SimServer((host, port), state, rate_per_sec, burst, seed)
    thread = threading.Thread(target=server.serve_forever, name=f"sim-{state.ecosystem}",
                              daemon=True)
    thread.start()
    return server
