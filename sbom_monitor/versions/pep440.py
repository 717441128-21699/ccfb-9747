"""PEP 440 版本解析与版本说明符（Requirement specifier）匹配。

覆盖 PyPI 实际使用的主要语法：
- 发布版本：``1`` ``1.2`` ``1.2.3`` ``1!2.0``（epoch）
- 预发/后发/开发版：``1.0a1`` ``1.0b2`` ``1.0rc1`` ``1.0.post1`` ``1.0.dev3``
- 兼容版本：``~= X.Y``
- 说明符：``==`` ``!=`` ``>=`` ``<=`` ``>`` ``<``，含通配 ``==1.2.*``
- 环境标记（``; python_version >= ...``）在依赖入库前已剥离，这里不处理

参考：https://peps.python.org/pep-0440/
"""
from __future__ import annotations

import re
from functools import total_ordering

_PREFIX_RE = re.compile(
    r"""^\s*
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|beta|preview|a|b|rc|c)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>[0-9]+)?))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE_ORDER = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "preview": 2}


@total_ordering
class PEP440Version:
    __slots__ = ("epoch", "release", "pre", "post", "dev", "local", "raw")

    def __init__(self, epoch, release, pre, post, dev, local, raw):
        self.epoch = epoch
        self.release = tuple(release)
        self.pre = pre            # (order, n) 或 None（正式）
        self.post = post          # n 或 None
        self.dev = dev            # n 或 None
        self.local = local
        self.raw = raw

    @staticmethod
    def parse(text: str) -> "PEP440Version":
        m = _PREFIX_RE.match(text.strip())
        if not m:
            raise ValueError(f"非法 PEP 440 版本: {text!r}")
        epoch = int(m.group("epoch") or 0)
        release = [int(x) for x in m.group("release").split(".")]
        pre = None
        if m.group("pre"):
            label = m.group("pre_l").lower()
            pre = (_PRE_ORDER[label], int(m.group("pre_n") or 0))
        post = None
        if m.group("post"):
            post = int(m.group("post_n") or 0)
        dev = int(m.group("dev_n") or 0) if m.group("dev") else None
        return PEP440Version(epoch, release, pre, post, dev,
                             m.group("local"), text.strip())

    @staticmethod
    def is_valid(text: str) -> bool:
        try:
            PEP440Version.parse(text)
            return True
        except ValueError:
            return False

    @property
    def is_prerelease(self) -> bool:
        return self.dev is not None or self.pre is not None

    def _key(self):
        # 规则见 PEP 440 "Version ordering"
        release = self.release + (0,) * max(0, 3 - len(self.release))
        if self.dev is not None and self.pre is None and self.post is None:
            # 纯 dev 版（如 1.0.dev1）排在同名正式版之前
            pre_key = (0, 0, 0)
        elif self.pre is not None:
            pre_key = (self.pre[0], self.pre[1], 0)
        else:
            pre_key = (99, 99, 99)  # 正式版靠后
        # dev：有 dev 段时排在同阶段非 dev 之前；-inf 用 -1
        dev_key = self.dev if self.dev is not None else 999_999
        # post：无 post 为 -inf
        post_key = self.post if self.post is not None else -1
        return (self.epoch, release, pre_key, post_key, dev_key)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PEP440Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: "PEP440Version") -> bool:
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return f"PEP440Version({self.raw!r})"


# ---------- 说明符 ----------

_SPEC_RE = re.compile(r"^\s*(~=|==|!=|>=|<=|>|<)\s*([A-Za-z0-9.*+!_-]+)\s*$")


class Specifier:
    def __init__(self, op: str, version_text: str):
        self.op = op
        self.wildcard = version_text.endswith(".*")
        base = version_text[:-2] if self.wildcard else version_text
        self.base = PEP440Version.parse(base)
        self.raw = version_text

    def satisfies(self, v: PEP440Version, prereleases_allowed: bool = False) -> bool:
        # 默认排除预发版（pip 的默认行为）；显式允许，或说明符本身带预发/开发段时放行
        implicit_allow = self.base.pre is not None or self.base.dev is not None
        if v.is_prerelease and not prereleases_allowed and not implicit_allow:
            return False
        op = self.op
        if op == "==":
            if self.wildcard:
                return self._prefix_match(v)
            return v == self.base
        if op == "!=":
            if self.wildcard:
                return not self._prefix_match(v)
            return v != self.base
        if op == ">=":
            return v >= self.base
        if op == "<=":
            return v <= self.base
        if op == ">":
            return v > self.base
        if op == "<":
            return v < self.base
        if op == "~=":
            # ~= X.Y 等价于 >= X.Y, == X.*；~= X.Y.Z 等价于 >= X.Y.Z, == X.Y.*
            if v < self.base:
                return False
            return self._compatible_prefix(v)
        raise ValueError(f"未知说明符: {op}")

    def _release_prefix(self) -> tuple[int, ...]:
        rel = self.base.release
        if len(rel) < 2:
            raise ValueError("~= 要求至少两级版本号")
        return rel[:-1]

    def _compatible_prefix(self, v: PEP440Version) -> bool:
        prefix = self._release_prefix()
        return v.epoch == self.base.epoch and v.release[:len(prefix)] == prefix

    def _prefix_match(self, v: PEP440Version) -> bool:
        if v.epoch != self.base.epoch:
            return False
        rel = self.base.release
        return v.release[:len(rel)] == rel


def parse_specifiers(spec: str) -> list[Specifier]:
    """解析逗号分隔的说明符集合（AND）。"""
    out: list[Specifier] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _SPEC_RE.match(part)
        if not m:
            raise ValueError(f"无法解析的版本说明符: {part!r}")
        out.append(Specifier(m.group(1), m.group(2)))
    return out


def satisfies(version: str, spec: str, prereleases: bool = False) -> bool:
    try:
        v = PEP440Version.parse(version)
        specs = parse_specifiers(spec)
    except ValueError:
        return False
    return all(s.satisfies(v, prereleases) for s in specs)


def sort_versions(versions: list[str]) -> list[str]:
    annotated = []
    for i, s in enumerate(versions):
        try:
            annotated.append((0, PEP440Version.parse(s)._key(), s))
        except ValueError:
            annotated.append((1, i, s))
    annotated.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in annotated]
