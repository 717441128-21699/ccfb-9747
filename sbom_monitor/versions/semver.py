"""npm 语义化版本（semver）解析与范围匹配。

实现 npm semver 2.x 的常用子集：
- 版本解析：major.minor.patch[-prerelease][+build]
- 比较器：``=`` ``>`` ``>=`` ``<`` ``<=``
- 范围：空格（AND）、``||``（OR）、连字符范围、``*``/``x`` 部分版本
- 标签：``^``、``~``、``>`` 等
- 预发版规则：带预发标签的版本，仅当比较器显式提到同一 major.minor.patch
  的预发版时才能满足（与 npm 行为一致）

参考：https://semver.org/ 与 https://github.com/npm/node-semver
"""
from __future__ import annotations

import re
from functools import total_ordering

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)
_PARTIAL_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?$"
)


@total_ordering
class SemVer:
    __slots__ = ("major", "minor", "patch", "prerelease", "build", "raw")

    def __init__(self, major: int, minor: int, patch: int,
                 prerelease: tuple | None = None, build: str | None = None,
                 raw: str = ""):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease  # None 表示正式版；元组表示预发
        self.build = build
        self.raw = raw

    @staticmethod
    def parse(text: str) -> "SemVer":
        m = _VERSION_RE.match(text.strip())
        if not m:
            raise ValueError(f"非法 semver: {text!r}")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        pre = None
        if m.group(4):
            pre = tuple(_ident(x) for x in m.group(4).split("."))
        return SemVer(major, minor, patch, pre, m.group(5), text.strip())

    @staticmethod
    def is_valid(text: str) -> bool:
        try:
            SemVer.parse(text)
            return True
        except ValueError:
            return False

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def _cmp_key(self):
        # 正式版 > 任何同号预发版（预发元组存在时排在前面）
        return (self.major, self.minor, self.patch,
                (1, ()) if self.prerelease is None else (0, self.prerelease))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() == other._cmp_key()

    def __lt__(self, other: "SemVer") -> bool:
        a, b = self._cmp_key(), other._cmp_key()
        if a[:3] != b[:3]:
            return a[:3] < b[:3]
        # 第 4 位：(0, 预发元组) 在前，(1, ()) 正式版在后
        if a[3][0] != b[3][0]:
            return a[3][0] < b[3][0]
        if a[3][0] == 0:
            return _pre_tuple_lt(a[3][1], b[3][1])
        return False

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease is None))

    def __repr__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease is not None:
            s += "-" + ".".join(str(x) for x in self.prerelease)
        return s


def _ident(token: str):
    return int(token) if token.isdigit() else token


def _pre_tuple_lt(a: tuple, b: tuple) -> bool:
    for x, y in zip(a, b):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return x < y
        if isinstance(x, int):  # 数字 < 字符串
            return True
        if isinstance(y, int):
            return False
        return x < y
    return len(a) < len(b)


# ---------- 比较器 ----------

class Comparator:
    """单个比较条件，如 >=1.2.3、<2.0.0-beta。"""

    def __init__(self, op: str, version: SemVer | None, preref: tuple | None = None):
        self.op = op
        self.version = version      # None 表示 *（任意版本）
        self.preref = preref        # 比较器中显式出现的预发标识

    def satisfies(self, v: SemVer) -> bool:
        if self.version is None:
            return True
        # 预发限制：被测版本是预发版时，比较器必须显式提到同一三元组的预发
        if v.is_prerelease:
            if self.preref is None or (v.major, v.minor, v.patch) != (
                    self.version.major, self.version.minor, self.version.patch):
                return False
        if self.op == "=":
            return v == self.version
        if self.op == ">":
            return v > self.version
        if self.op == ">=":
            return v >= self.version
        if self.op == "<":
            return v < self.version
        if self.op == "<=":
            return v <= self.version
        raise ValueError(f"未知操作符: {self.op}")

    def __repr__(self) -> str:
        return f"<Comparator {self.op}{self.version}>"


def _expand_partial(text: str) -> tuple[SemVer | None, str, tuple | None]:
    """把 1 / 1.2 / 1.x / * 这类部分版本展开成比较器用的边界版本。

    返回 (边界版本或 None, 隐含操作符, 显式预发标识)。
    """
    t = text.strip()
    if t in ("", "*", "x", "X", "latest"):
        return None, "*", None
    m = _PARTIAL_RE.match(t)
    if not m:
        raise ValueError(f"无法解析的版本片段: {text!r}")
    major = int(m.group(1))
    minor_raw, patch_raw = m.group(2), m.group(3)
    pre = tuple(_ident(x) for x in m.group(4).split(".")) if m.group(4) else None
    if minor_raw in (None, "x", "X", "*"):
        return SemVer(major, 0, 0), ">=", pre
    minor = int(minor_raw)
    if patch_raw in (None, "x", "X", "*"):
        return SemVer(major, minor, 0), ">=", pre
    return SemVer(major, minor, int(patch_raw), pre), "=", pre


def _parse_comparator(token: str) -> Comparator:
    token = token.strip()
    if token in ("", "*", "x", "X", "latest"):
        return Comparator("*", None)
    m = re.match(r"^(>=|<=|>|<|=|~|\^)?(.*)$", token)
    op, rest = m.group(1) or "=", m.group(2).strip()
    if op == "~":
        return _tilde(rest)
    if op == "^":
        return _caret(rest)
    # 连字符范围在外层处理；这里只处理单比较器
    boundary, implicit_op, preref = _expand_partial(rest)
    if boundary is None:
        return Comparator("*", None)
    if implicit_op == ">=" and op == "=":
        # 部分版本 1.2 表示 >=1.2.0 <1.3.0，由调用方处理成对；这里先给下界
        return Comparator(">=", boundary, preref)
    return Comparator(op, boundary, preref)


def _tilde(rest: str) -> Comparator:
    """~1.2.3 := >=1.2.3 <1.3.0；~1.2 := >=1.2.0 <1.3.0；~1 := >=1.0.0 <2.0.0。

    为简化实现返回下界比较器，上界在 _comparator_set 里补。
    """
    boundary, _, preref = _expand_partial(rest)
    if boundary is None:
        return Comparator("*", None)
    comp = Comparator(">=", boundary, preref)
    comp._tilde_upper = (  # type: ignore[attr-defined]
        SemVer(boundary.major, boundary.minor + 1, 0)
        if _PARTIAL_RE.match(rest).group(2) not in (None, "x", "X", "*")
        else SemVer(boundary.major + 1, 0, 0)
    )
    return comp


def _caret(rest: str) -> Comparator:
    """^1.2.3 := >=1.2.3 <2.0.0；^0.2.3 := >=0.2.3 <0.3.0；^0.0.3 := >=0.0.3 <0.0.4。"""
    boundary, _, preref = _expand_partial(rest)
    if boundary is None:
        return Comparator("*", None)
    comp = Comparator(">=", boundary, preref)
    M, m, p = boundary.major, boundary.minor, boundary.patch
    if M > 0:
        upper = SemVer(M + 1, 0, 0)
    elif m > 0:
        upper = SemVer(0, m + 1, 0)
    else:
        upper = SemVer(0, 0, p + 1)
    comp._caret_upper = upper  # type: ignore[attr-defined]
    return comp


def _comparator_set(text: str) -> list[Comparator]:
    """解析一个 AND 比较器集合（空格分隔的若干条件）。"""
    tokens = text.split()
    comps: list[Comparator] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # 连字符范围：1.2.3 - 2.0.0
        if tok == "-" and i >= 1 and i + 1 < len(tokens):
            left = comps.pop()
            right_text = tokens[i + 1]
            r_boundary, r_op, r_pre = _expand_partial(right_text)
            # 左侧若是部分版本，已被展开为 >=，保持；右侧部分版本要转成 < 上界
            if r_op == ">=":  # 部分版本
                mm = _PARTIAL_RE.match(right_text)
                if mm.group(2) in (None, "x", "X", "*"):
                    upper = SemVer(r_boundary.major + 1, 0, 0)
                elif mm.group(3) in (None, "x", "X", "*"):
                    upper = SemVer(r_boundary.major, r_boundary.minor + 1, 0)
                else:
                    upper = SemVer(r_boundary.major, r_boundary.minor,
                                   r_boundary.patch + 1)
                comps.append(left)
                comps.append(Comparator("<", upper, r_pre))
            else:
                comps.append(left)
                comps.append(Comparator("<=", r_boundary, r_pre))
            i += 2
            continue
        comp = _parse_comparator(tok)
        upper = getattr(comp, "_tilde_upper", None) or getattr(comp, "_caret_upper", None)
        comps.append(comp)
        if upper is not None:
            comps.append(Comparator("<", upper))
        i += 1
    return comps


def parse_range(spec: str) -> list[list[Comparator]]:
    """把 npm 版本范围解析成比较器集合的列表（OR 关系）。"""
    spec = (spec or "").strip()
    if not spec or spec in ("*", "x", "X", "latest"):
        return [[Comparator("*", None)]]
    sets: list[list[Comparator]] = []
    for group in spec.split("||"):
        group = group.strip()
        if not group:
            continue
        sets.append(_comparator_set(group))
    return sets or [[Comparator("*", None)]]


def satisfies(version: str, spec: str) -> bool:
    """判断具体版本是否满足 npm 版本范围。"""
    try:
        v = SemVer.parse(version)
    except ValueError:
        return False
    try:
        groups = parse_range(spec)
    except ValueError:
        return False
    for comps in groups:
        if all(c.satisfies(v) for c in comps):
            return True
    return False


def sort_versions(versions: list[str]) -> list[str]:
    """按 semver 升序排序版本字符串，非法值排最后并保持原相对顺序。"""
    annotated = []
    for i, s in enumerate(versions):
        try:
            annotated.append((0, SemVer.parse(s)._cmp_key(), s))
        except ValueError:
            annotated.append((1, i, s))
    annotated.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in annotated]
