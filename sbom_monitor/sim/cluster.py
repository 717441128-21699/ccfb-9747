"""仿真仓库编排：一次性启动 npm/PyPI/Maven 三个独立服务与后台变异器。

所有状态与真值落盘到工作目录，仿真进程重启后可恢复；
三个服务分别监听不同端口，单个服务故障不影响其余两个。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .mutator import MutatorThread
from .server import SimServer, start_server
from .state import SimState

DEFAULT_PORTS = {"npm": 8101, "pypi": 8102, "maven": 8103}
DEFAULT_SEEDS = {"npm": 1001, "pypi": 2002, "maven": 3003}


@dataclass
class SimHandle:
    ecosystem: str
    server: SimServer
    state: SimState
    mutator: MutatorThread
    state_path: str


class SimCluster:
    def __init__(self, workdir: str, counts: dict[str, int] | None = None,
                 ports: dict[str, int] | None = None,
                 mutate_interval: float = 3.0, drop_ratio: float = 0.15,
                 rate_per_sec: float = 20.0, burst: int = 50):
        self.workdir = workdir
        self.counts = counts or {"npm": 30, "pypi": 25, "maven": 20}
        self.ports = ports or DEFAULT_PORTS
        self.mutate_interval = mutate_interval
        self.drop_ratio = drop_ratio
        self.rate_per_sec = rate_per_sec
        self.burst = burst
        os.makedirs(workdir, exist_ok=True)
        self.handles: list[SimHandle] = []

    def start(self, populate: bool = True) -> list[SimHandle]:
        for eco in ("npm", "pypi", "maven"):
            state_path = os.path.join(self.workdir, f"sim-{eco}.json")
            if os.path.exists(state_path):
                state = SimState.load(state_path)
            else:
                state = SimState(eco)
                if populate:
                    state.populate(self.counts[eco], DEFAULT_SEEDS[eco])
                    state.save(state_path)
            server = start_server(state, "127.0.0.1", self.ports[eco],
                                  self.rate_per_sec, self.burst,
                                  seed=DEFAULT_SEEDS[eco] + 7)
            mutator = MutatorThread(
                state, self.mutate_interval, state_path,
                drop_ratio=self.drop_ratio, seed=DEFAULT_SEEDS[eco] + 99)
            mutator.start()
            self.handles.append(SimHandle(eco, server, state, mutator, state_path))
        return self.handles

    def base_urls(self) -> dict[str, str]:
        return {eco: f"http://127.0.0.1:{self.ports[eco]}" for eco in self.ports}

    def stop(self) -> None:
        for h in self.handles:
            h.mutator.stop()
        for h in self.handles:
            h.mutator.join(timeout=5)
            h.state.save(h.state_path)
            h.server.shutdown()

    def wait_healthy(self, timeout: float = 10.0) -> None:
        import time
        import urllib.request
        deadline = time.time() + timeout
        for url in self.base_urls().values():
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(url + "/health", timeout=2) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError(f"仿真仓库未就绪: {url}")

    def write_repo_config(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ports": self.ports, "base_urls": self.base_urls(),
                       "counts": self.counts,
                       "rate_per_sec": self.rate_per_sec, "burst": self.burst},
                      f, ensure_ascii=False, indent=2)
