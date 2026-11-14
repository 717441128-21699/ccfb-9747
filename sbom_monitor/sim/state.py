"""仿真仓库的内存真值状态 + 变更事件日志 + 变异器（mutator）。

每个仿真仓库持有一个 :class:`SimState`：
- ``packages``：当前完整真值（包 -> 版本列表），变更就地修改
- ``events``：只增不改的变更事件流，采集端通过 HTTP 分页拉取
- 每个对象维护单调版本号 ``rev``，用于生成强校验 ETag

变异器模拟真实仓库的四类变化：
1. ``version_published``  发新版本（可能带新依赖）
2. ``license_changed``    某版本许可证被修改
3. ``version_yanked``     撤回某版本
4. ``metadata_edited``    描述等元数据编辑

状态全部可落盘（JSON），仿真仓库重启后真值与事件流不丢。
"""
from __future__ import annotations

import copy
import json
import os
import random
import threading
from dataclasses import dataclass, field

from ..common.timeutil import now_iso
from . import datagen

EVENT_TYPES = ("version_published", "license_changed", "version_yanked", "metadata_edited")


@dataclass
class SimState:
    ecosystem: str
    packages: dict[str, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    next_event_id: int = 1
    next_rev: int = 1
    lock: threading.RLock = field(default_factory=threading.RLock)
    # 故障注入：offline 时所有请求返回 503；error_rate 控制随机 5xx 比例
    offline: bool = False
    error_rate: float = 0.0
    # 变更流“丢事件”模式：被标记跳过的 event_id 不出现在 feed 里（考验定期校验补漏）
    feed_skipped: set[int] = field(default_factory=set)

    # ---------- 持久化 ----------

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "ecosystem": self.ecosystem,
                "packages": self.packages,
                "events": self.events,
                "next_event_id": self.next_event_id,
                "next_rev": self.next_rev,
                "offline": self.offline,
                "error_rate": self.error_rate,
                "feed_skipped": sorted(self.feed_skipped),
            }

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @staticmethod
    def load(path: str) -> "SimState":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        st = SimState(ecosystem=data["ecosystem"])
        st.packages = data["packages"]
        st.events = data["events"]
        st.next_event_id = data["next_event_id"]
        st.next_rev = data["next_rev"]
        st.offline = data.get("offline", False)
        st.error_rate = data.get("error_rate", 0.0)
        st.feed_skipped = set(data.get("feed_skipped", []))
        return st

    # ---------- 构建 ----------

    def populate(self, count: int, seed: int) -> None:
        with self.lock:
            for pkg in datagen.generate_ecosystem(self.ecosystem, count, seed):
                # 记录对象级最后变更时间（yank/改许可证不产生新版本，靠它驱动 Last-Modified）
                pkg["last_changed"] = max(v["released_at"] for v in pkg["versions"])
                self.packages[pkg["name"]] = pkg
            self.next_rev += 1

    # ---------- ETag ----------

    def package_etag(self, name: str) -> str | None:
        """包对象的强 ETag：内容哈希 + 当前 rev。"""
        import hashlib
        with self.lock:
            pkg = self.packages.get(name)
            if pkg is None:
                return None
            h = hashlib.sha256(
                json.dumps(pkg, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16]
            return f'"{h}-{self.next_rev}"'

    def listing_etag(self) -> str:
        import hashlib
        with self.lock:
            names = sorted(self.packages.keys())
            h = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
            return f'"listing-{h}-{self.next_rev}"'

    # ---------- 事件 ----------

    def _append_event(self, etype: str, package: str, payload: dict) -> dict:
        ts = now_iso()
        evt = {
            "id": self.next_event_id,
            "type": etype,
            "ecosystem": self.ecosystem,
            "package": package,
            "timestamp": ts,
            "payload": payload,
        }
        self.next_event_id += 1
        self.events.append(evt)
        # 对象级最后变更时间：即使没有新版本（yank/改许可证）也会推进
        if package in self.packages:
            self.packages[package]["last_changed"] = ts
        return evt

    def feed(self, after_id: int, limit: int) -> dict:
        """变更订阅流分页。被 feed_skipped 标记的事件不出现（模拟流丢失）。"""
        with self.lock:
            visible = [e for e in self.events
                       if e["id"] > after_id and e["id"] not in self.feed_skipped]
            page = visible[:limit]
            return {
                "ecosystem": self.ecosystem,
                "after_id": after_id,
                "limit": limit,
                "events": page,
                "has_more": len(visible) > len(page),
                "latest_event_id": (self.events[-1]["id"] if self.events else 0),
            }

    # ---------- 变异 ----------

    def mutate(self, rng: random.Random, drop_from_feed: bool = False) -> dict | None:
        """执行一次随机变异，返回产生的事件。无可变异对象时返回 None。"""
        with self.lock:
            if not self.packages:
                return None
            name = rng.choice(sorted(self.packages.keys()))
            pkg = self.packages[name]
            etype = rng.choice(
                ["version_published", "version_published", "license_changed",
                 "version_yanked", "metadata_edited"]
            )
            evt: dict
            if etype == "version_published":
                evt = self._mutate_new_version(pkg, rng)
            elif etype == "license_changed":
                candidates = [v for v in pkg["versions"] if not v["yanked"]]
                if not candidates:
                    return None
                ver = rng.choice(candidates)
                old = ver["license_raw"]
                new = rng.choice([x for x in datagen.LICENSE_CHANGE_POOL if x != old])
                ver["license_raw"] = new
                evt = self._append_event("license_changed", name, {
                    "version": ver["version"], "old_license": old, "new_license": new,
                })
            elif etype == "version_yanked":
                candidates = [v for v in pkg["versions"] if not v["yanked"]]
                if not candidates:
                    return None
                ver = rng.choice(candidates)
                ver["yanked"] = True
                ver["yanked_reason"] = rng.choice(
                    ["security advisory", "deprecated", "release mistake"])
                evt = self._append_event("version_yanked", name, {
                    "version": ver["version"], "reason": ver["yanked_reason"],
                })
            else:
                old = pkg.get("description", "")
                pkg["description"] = old + " [edited]"
                evt = self._append_event("metadata_edited", name, {
                    "field": "description",
                })
            self.next_rev += 1
            if drop_from_feed:
                self.feed_skipped.add(evt["id"])
            return evt

    def _mutate_new_version(self, pkg: dict, rng: random.Random) -> dict:
        existing = {v["version"] for v in pkg["versions"]}
        new_ver = self._bump_version(pkg, rng, existing)
        # 新版本继承依赖，并有概率增加/替换一个依赖（制造“新增依赖”）
        deps = copy.deepcopy(pkg["versions"][-1]["dependencies"]) if pkg["versions"] else []
        added_dep = None
        if rng.random() < 0.5:
            added_dep = self._pick_new_dep(pkg, rng)
            if added_dep and not any(d["name"] == added_dep["name"] for d in deps):
                deps.append(added_dep)
        ver_obj = {
            "version": new_ver,
            "released_at": now_iso(),
            "license_raw": rng.choice(datagen.LICENSE_CHANGE_POOL),
            "yanked": False,
            "yanked_reason": None,
            "dependencies": deps,
            "artifacts": [{
                "name": datagen._artifact_name(self.ecosystem, pkg["name"], new_ver),
                "url": f"https://artifact.local/{self.ecosystem}/{pkg['name']}/{new_ver}/package",
                "checksum": datagen._checksum(self.ecosystem, pkg["name"], new_ver,
                                              len(pkg["versions"])),
                "checksum_algo": "sha256",
            }],
            "extra": {},
        }
        pkg["versions"].append(ver_obj)
        evt = self._append_event("version_published", pkg["name"], {
            "version": new_ver,
            "added_dependency": added_dep,
        })
        return evt

    def _bump_version(self, pkg: dict, rng: random.Random, existing: set[str]) -> str:
        last = pkg["versions"][-1]["version"] if pkg["versions"] else "1.0.0"
        # 抽取主版本数字，失败就退回随机版本
        digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in last).split()
        nums = [int(x) for x in digits if x.isdigit()]
        base = (nums + [0, 0, 0])[:3]
        for _ in range(20):
            which = rng.choice([0, 1, 2])
            cand = list(base)
            cand[which] += 1
            for j in range(which + 1, 3):
                cand[j] = 0
            if self.ecosystem == "npm":
                v = f"{cand[0]}.{cand[1]}.{cand[2]}"
            elif self.ecosystem == "pypi":
                v = f"{cand[0]}.{cand[1]}.{cand[2]}"
            else:
                v = f"{cand[0]}.{cand[1]}.{cand[2]}"
            if v not in existing:
                return v
        # 兜底：时间戳风格
        v = f"{base[0]}.{base[1]}.{base[2]}.{len(existing) + 1}"
        return v

    def _pick_new_dep(self, pkg: dict, rng: random.Random) -> dict | None:
        current = {d["name"] for d in (pkg["versions"][-1]["dependencies"] if pkg["versions"] else [])}
        candidates = [n for n in self.packages if n != pkg["name"] and n not in current]
        if not candidates:
            return None
        dep_name = rng.choice(sorted(candidates))
        spec_gen = datagen._SPEC_GEN[self.ecosystem]
        return {"name": dep_name, "spec": spec_gen(rng), "optional": rng.random() < 0.15}

    # ---------- 对账用真值快照 ----------

    def ground_truth(self) -> dict:
        """供对账器拉取的完整真值（独立路径，采集端不允许使用）。"""
        with self.lock:
            return {
                "ecosystem": self.ecosystem,
                "packages": copy.deepcopy(self.packages),
                "event_count": len(self.events),
                "latest_event_id": (self.events[-1]["id"] if self.events else 0),
            }
