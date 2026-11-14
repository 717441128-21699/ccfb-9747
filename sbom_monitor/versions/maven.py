"""Maven 版本排序与版本范围（Maven 3 / maven-artifact 语义子集）。

版本排序要点（按 Maven 规范）：
- 版本拆成数字段与限定符段交替的 token 序列
- 数字段按数值比较，限定符按字典序；数字段 < 限定符段
- 已知限定符顺序：alpha < beta < milestone < rc < snapshot < 正式(空) < sp
- 连字符/点均可作为分隔符，``a``/``b``/``m`` 分别等同 alpha/beta/milestone
- 缺位补齐：数字缺位补 0（``2.0 == 2.0.0``），限定符缺位补“正式发布”（``1.0 < 1.0-sp1``）

版本范围语法（maven 3.x）：
- ``[1.0]``        精确 1.0
- ``[1.0,2.0)``    1.0 <= v < 2.0
- ``[1.5,)``       v >= 1.5
- ``(,1.0]``       v <= 1.0
- 不带括号的裸版本是“软需求”，本系统按 ``>= 该版本`` 处理（依赖选版场景），
  并提供 :func:`soft_match` 单独表达该语义
"""
from __future__ import annotations

import re
from functools import total_ordering

_QUALIFIER_ORDER = {
    "alpha": 0, "a": 0,
    "beta": 1, "b": 1,
    "milestone": 2, "m": 2,
    "rc": 3, "cr": 3,
    "snapshot": 4,
    "": 5,          # 正式发布
    "ga": 5, "final": 5, "release": 5,
    "sp": 6,
}
_TOKEN_SPLIT_RE = re.compile(r"[._-]")
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
# key 形态：(0, int) 数字段；(1, 顺序, 字符串) 限定符段
_NUMERIC = 0
_QUALIFIER = 1
_RELEASE_KEY = (1, _QUALIFIER_ORDER[""], 0, "")


def _tokenize(version: str) -> list:
    """拆成 token 列表；数字为 int，限定符为小写字符串。

    限定符后面紧跟的数字属于该限定符的后缀（alpha1 -> ("alpha", 1) 合成 "alpha-1"），
    独立数字段只出现在版本号位置。Maven 用这个后缀区分 alpha1/alpha2。
    """
    tokens: list = []
    for raw in _TOKEN_SPLIT_RE.split(version.strip().lower()):
        if raw == "":
            continue
        if raw.isdigit():
            tokens.append(int(raw))
            continue
        # 混合段：前导数字是版本号，随后“字母+数字”按限定符（含后缀）处理
        i = 0
        while i < len(raw) and raw[i].isdigit():
            i += 1
        if i:
            tokens.append(int(raw[:i]))
            raw = raw[i:]
        m = re.match(r"^([a-z]+)([0-9]*)$", raw)
        if m:
            word = _ALIASES.get(m.group(1), m.group(1))
            if m.group(2):
                tokens.append(f"{word}-{int(m.group(2))}")
            else:
                tokens.append(word)
        else:
            tokens.append(_ALIASES.get(raw, raw))
    return tokens


def _to_key_token(t) -> tuple:
    # 统一 4 元组，避免不同长度 tuple 比较时短路：
    # 数字段 (0, 数值, 0, "")；限定符 (1, 限定符序, 后缀数字, 限定符名)
    if isinstance(t, int):
        return (_NUMERIC, t, 0, "")
    if isinstance(t, str) and "-" in t:
        name, _, suffix = t.partition("-")
        order = _QUALIFIER_ORDER.get(name, 99)
        return (_QUALIFIER, order, int(suffix), name)
    order = _QUALIFIER_ORDER.get(t, 99)
    return (_QUALIFIER, order, 0, t)


def _pad_for(kind: int) -> tuple:
    return (0, 0, 0, "") if kind == _NUMERIC else _RELEASE_KEY


@total_ordering
class MavenVersion:
    __slots__ = ("tokens", "raw")

    def __init__(self, text: str):
        self.raw = text
        self.tokens = _tokenize(text)

    @staticmethod
    def parse(text: str) -> "MavenVersion":
        if not text or not text.strip():
            raise ValueError("空 Maven 版本")
        return MavenVersion(text)

    @staticmethod
    def is_valid(text: str) -> bool:
        try:
            MavenVersion.parse(text)
            return True
        except ValueError:
            return False

    def _cmp_key(self):
        return [_to_key_token(t) for t in self.tokens]

    def _padded_keys(self, length: int) -> list:
        """把 key 补齐到指定长度，缺位按对方 token 类型填默认值。"""
        keys = self._cmp_key()
        while len(keys) < length:
            keys.append(_RELEASE_KEY)
        return keys

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MavenVersion):
            return NotImplemented
        return self._compare(other) == 0

    def __lt__(self, other: "MavenVersion") -> bool:
        return self._compare(other) < 0

    def __hash__(self) -> int:
        return hash(tuple(str(t) for t in self.tokens))

    def _compare(self, other: "MavenVersion") -> int:
        a, b = self._cmp_key(), other._cmp_key()
        n = max(len(a), len(b))
        for i in range(n):
            if i < len(a) and i < len(b):
                ta, tb = a[i], b[i]
            elif i < len(a):
                # 自己有 token、对方缺位：按自己 token 的类型补默认
                ta, tb = a[i], _pad_for(a[i][0])
            else:
                ta, tb = _pad_for(b[i][0]), b[i]
            if ta == tb:
                continue
            return -1 if ta < tb else 1
        return 0
    def __repr__(self) -> str:
        return f"MavenVersion({self.raw!r})"


# ---------- 版本范围 ----------

_RANGE_RE = re.compile(r"^\s*([\[\(])\s*([^,\[\]\)]*)\s*(?:,\s*([^,\[\]\)]*)\s*)?([\]\)])\s*$")


class Range:
    def __init__(self, lower: MavenVersion | None, lower_inclusive: bool,
                 upper: MavenVersion | None, upper_inclusive: bool):
        self.lower = lower
        self.lower_inclusive = lower_inclusive
        self.upper = upper
        self.upper_inclusive = upper_inclusive

    def contains(self, v: MavenVersion) -> bool:
        if self.lower is not None:
            if self.lower_inclusive:
                if v < self.lower:
                    return False
            elif v <= self.lower:
                return False
        if self.upper is not None:
            if self.upper_inclusive:
                if v > self.upper:
                    return False
            elif v >= self.upper:
                return False
        return True


def parse_range(spec: str) -> Range:
    m = _RANGE_RE.match(spec or "")
    if not m:
        raise ValueError(f"非法 Maven 版本范围: {spec!r}")
    open_l, lo, hi, open_r = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
    if hi is None:
        # [1.0] 精确版本
        if open_l != "[" or open_r != "]" or not lo:
            raise ValueError(f"非法 Maven 版本范围: {spec!r}")
        v = MavenVersion.parse(lo)
        return Range(v, True, v, True)
    lower = MavenVersion.parse(lo) if lo else None
    upper = MavenVersion.parse(hi.strip()) if hi and hi.strip() else None
    return Range(lower, open_l == "[", upper, open_r == "]")


def satisfies(version: str, spec: str) -> bool:
    """判断版本是否满足给定 Maven 范围；裸版本走软需求语义（>=）。"""
    try:
        v = MavenVersion.parse(version)
    except ValueError:
        return False
    spec = (spec or "").strip()
    if not spec:
        return True
    if spec[0] not in "[(":
        return soft_match(version, spec)
    try:
        return parse_range(spec).contains(v)
    except ValueError:
        return False


def soft_match(version: str, spec: str) -> bool:
    """Maven 软需求：裸版本 X 表示接受 X 及以上。"""
    try:
        v = MavenVersion.parse(version)
        base = MavenVersion.parse(spec.strip())
    except ValueError:
        return False
    return v >= base


def sort_versions(versions: list[str]) -> list[str]:
    annotated = []
    for i, s in enumerate(versions):
        try:
            annotated.append((0, MavenVersion.parse(s), s))
        except ValueError:
            annotated.append((1, i, s))
    # 合法版本用 MavenVersion 的完整比较规则（含缺位补齐），不能直接比 token list
    valid = sorted((x for x in annotated if x[0] == 0), key=lambda x: x[1])
    invalid = [x for x in annotated if x[0] == 1]
    return [x[2] for x in valid] + [x[2] for x in invalid]
