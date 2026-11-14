"""采集端 HTTP 客户端：条件请求 + 限流退避 + 故障隔离。

- 429 读 ``Retry-After``，其余 5xx 指数退避 + 抖动，重试到上限
- 单仓库持续失败只抛 :class:`RepoUnavailable`，由引擎隔离，不影响其他仓库
- 支持 ETag / If-Modified-Since 条件请求，304 不下载正文
- 只访问本机仿真地址，真实域名一律拒绝（双保险）
"""
from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ALLOWED_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")


class RepoUnavailable(Exception):
    """仓库在重试预算内持续不可用。"""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict:
        import json
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


class RepoClient:
    def __init__(self, base_url: str, max_retries: int = 5,
                 timeout: float = 10.0, backoff_base: float = 0.4,
                 rng: random.Random | None = None):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.rng = rng or random.Random()
        self.user_agent = "sbom-monitor/1.0 (local-simulation)"
        # 简单统计，供控制台展示
        self.stats = {"requests": 0, "cached_304": 0, "retries": 0,
                      "rate_limited": 0, "errors": 0}

    def _validate_url(self, url: str) -> None:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"安全拦截：只允许访问本机仿真仓库，收到主机 {host!r}（真实仓库一律不碰）")

    def get(self, path: str, etag: str | None = None,
            if_modified_since: str | None = None) -> Response:
        url = self.base_url + path
        self._validate_url(url)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml"}
        if etag:
            headers["If-None-Match"] = etag
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            req = urllib.request.Request(url, headers=headers)
            self.stats["requests"] += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return Response(resp.status, {k: v for k, v in resp.headers.items()},
                                    resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    self.stats["cached_304"] += 1
                    return Response(304, {k: v for k, v in e.headers.items()}, b"")
                if e.code == 429:
                    self.stats["rate_limited"] += 1
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self._backoff(attempt)
                    self.stats["retries"] += 1
                elif 500 <= e.code < 600:
                    self.stats["errors"] += 1
                    delay = self._backoff(attempt)
                    self.stats["retries"] += 1
                else:
                    # 404 等 4xx 不是可重试错误，原样返回交给业务处理
                    return Response(e.code, {k: v for k, v in e.headers.items()},
                                    e.read() or b"")
                last_exc = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.stats["errors"] += 1
                delay = self._backoff(attempt)
                self.stats["retries"] += 1
                last_exc = e
            attempt += 1
            if attempt > self.max_retries:
                break
            time.sleep(delay)
        raise RepoUnavailable(f"{url} 重试 {self.max_retries} 次仍失败: {last_exc!r}")

    def post_json(self, path: str, payload: dict) -> Response:
        url = self.base_url + path
        self._validate_url(url)
        import json
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return Response(resp.status, {k: v for k, v in resp.headers.items()},
                            resp.read())

    def _backoff(self, attempt: int) -> float:
        # 指数退避 + 全抖动：base * 2^attempt * (0.5~1.5)
        return self.backoff_base * (2 ** attempt) * (0.5 + self.rng.random())
