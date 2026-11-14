"""令牌桶限流器：每个仿真仓库独立计数，HTTP 429 + Retry-After。"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = float(rate_per_sec)
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self, cost: float = 1.0) -> tuple[bool, float]:
        """尝试取令牌。返回 (是否放行, 建议等待秒数)。"""
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= cost:
                self.tokens -= cost
                return True, 0.0
            deficit = cost - self.tokens
            return False, deficit / self.rate

    def update_config(self, rate_per_sec: float, burst: int) -> None:
        with self.lock:
            self.rate = float(rate_per_sec)
            self.capacity = float(burst)
            self.tokens = min(self.tokens, self.capacity)
