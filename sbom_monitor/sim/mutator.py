"""后台变异器：按固定节拍对各仿真仓库制造变更事件。

- 每个仓库独立线程、独立随机源，单个仓库异常不影响其他
- 周期性把状态落盘，仿真仓库进程被杀后真值可恢复
- 支持把一部分事件从 feed 中“抹掉”，用于考验采集端定期校验补漏
"""
from __future__ import annotations

import random
import threading

from .state import SimState


class MutatorThread(threading.Thread):
    def __init__(self, state: SimState, interval_sec: float, state_path: str,
                 drop_ratio: float = 0.15, seed: int = 0, persist_every: int = 5):
        super().__init__(name=f"mutator-{state.ecosystem}", daemon=True)
        self.state = state
        self.interval = interval_sec
        self.state_path = state_path
        self.drop_ratio = drop_ratio
        self.rng = random.Random(seed)
        self.persist_every = persist_every
        self._stop = threading.Event()
        self.ticks = 0
        self.produced_events: list[dict] = []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                drop = self.rng.random() < self.drop_ratio
                evt = self.state.mutate(self.rng, drop_from_feed=drop)
                if evt is not None:
                    self.produced_events.append(evt)
                self.ticks += 1
                if self.ticks % self.persist_every == 0:
                    self.state.save(self.state_path)
            except Exception:
                # 变异失败不允许杀死线程
                continue
        self.state.save(self.state_path)
