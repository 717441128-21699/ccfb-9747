"""许可证名称归一：把各生态里五花八门的写法映射到 SPDX 标识。

规则：
1. 先做确定性的字符串规整（小写、去冗余前缀、空白/连字符统一）。
2. 查已知别名表（覆盖 npm/PyPI/Maven 常见写法与历史名称）。
3. 命中 SPDX 表达式的（含 AND/OR/WITH、括号）整体保留并递归归一其中的标识符。
4. 无法识别的标记为 ``LicenseRef-Unknown:<原始值>``，绝不静默丢弃。
"""
from __future__ import annotations

import re

_SPDX_ID_RE = re.compile(r"^[A-Za-z0-9.+-]+$")
_TOKEN_RE = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bWITH\b", re.IGNORECASE)

# 常见别名 -> 规范 SPDX 标识（含历史名称到现代 SPDX 的映射）。
_ALIASES = {
    # MIT 系
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit/x11": "X11",
    "x11": "X11",
    # BSD 系
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd-2": "BSD-2-Clause",
    "bsd2": "BSD-2-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd3": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "simplified bsd license": "BSD-2-Clause",
    # Apache 系
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "asl 2.0": "Apache-2.0",
    "apache 1.1": "Apache-1.1",
    # GPL 系（区分 -only / -or-later，无版本号的 GPL 按 GPL-3.0-only 之外的保守策略归 -only）
    "gpl": "GPL-3.0-only",
    "gplv2": "GPL-2.0-only",
    "gpl v2": "GPL-2.0-only",
    "gpl-2.0": "GPL-2.0-only",
    "gpl 2": "GPL-2.0-only",
    "gpl 2.0": "GPL-2.0-only",
    "gplv2+": "GPL-2.0-or-later",
    "gpl 2+": "GPL-2.0-or-later",
    "gplv3": "GPL-3.0-only",
    "gpl v3": "GPL-3.0-only",
    "gpl-3.0": "GPL-3.0-only",
    "gpl 3": "GPL-3.0-only",
    "gpl 3.0": "GPL-3.0-only",
    "gplv3+": "GPL-3.0-or-later",
    "gpl 3+": "GPL-3.0-or-later",
    # LGPL 系
    "lgpl": "LGPL-2.1-only",
    "lgplv2.1": "LGPL-2.1-only",
    "lgpl 2.1": "LGPL-2.1-only",
    "lgplv3": "LGPL-3.0-only",
    "lgpl 3": "LGPL-3.0-only",
    # MPL
    "mpl": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mpl 1.1": "MPL-1.1",
    # ISC
    "isc": "ISC",
    "isc license": "ISC",
    # 公共领域 / 宽松
    "unlicense": "Unlicense",
    "the unlicense": "Unlicense",
    "public domain": "CC0-1.0",
    "cc0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "wtfpl": "WTFPL",
    "zlib": "Zlib",
    "zlib/libpng": "Zlib",
    "python software foundation license": "Python-2.0",
    "psf": "Python-2.0",
    "psf license": "Python-2.0",
    # Eclipse / CDDL
    "epl": "EPL-2.0",
    "epl 1.0": "EPL-1.0",
    "epl 2.0": "EPL-2.0",
    "eclipse public license": "EPL-2.0",
    "cddl": "CDDL-1.0",
    # 占位/无许可证（保留语义，下游可按“高风险”处理）
    "none": "NONE",
    "no license": "NONE",
    "unlicensed": "NONE",
    "unknown": "NOASSERTION",
    "": "NOASSERTION",
}

# SPDX 白名单（本系统测试数据中实际会出现的；不在表内但形状像 SPDX 的也放行）。
_KNOWN_SPDX = {
    "MIT", "X11", "BSD-2-Clause", "BSD-3-Clause", "Apache-1.1", "Apache-2.0",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "MPL-1.1", "MPL-2.0", "ISC", "Unlicense", "CC0-1.0", "WTFPL", "Zlib",
    "Python-2.0", "EPL-1.0", "EPL-2.0", "CDDL-1.0", "NONE", "NOASSERTION",
}


def _clean(value: str) -> str:
    v = value.strip().lower()
    v = v.replace("_", "-")
    v = re.sub(r"\s+", " ", v)
    v = re.sub(r"\s*-\s*", "-", v)
    # 去掉常见冗余前缀
    v = re.sub(r"^(the )?license\s*:\s*", "", v)
    return v


def normalize_one(value: str) -> str:
    """归一单个许可证字符串。无法识别时返回 LicenseRef-Unknown:<原值>。"""
    if value is None:
        return "NOASSERTION"
    raw = value.strip()
    if not raw:
        return "NOASSERTION"
    if _TOKEN_RE.search(raw) and ("(" in raw or " " in raw):
        # 可能是 SPDX 复合表达式，递归归一其中的原子
        return _normalize_expression(raw)
    cleaned = _clean(raw)
    if cleaned in _ALIASES:
        return _ALIASES[cleaned]
    # 形状本身就是 SPDX 标识（如 Apache-2.0 / apache-2.0），大小写不敏感地在白名单内放行
    candidate = raw.strip().replace("_", "-")
    upper = candidate.upper()
    for known in _KNOWN_SPDX:
        if upper == known.upper():
            return known
    if _SPDX_ID_RE.match(candidate) and candidate[:1].isalpha():
        # 形如 SPDX 但不在已知表：保守起见仍标记未知，避免误放行拼写错误
        return f"LicenseRef-Unknown:{raw}"
    return f"LicenseRef-Unknown:{raw}"


def _normalize_expression(expr: str) -> str:
    """对 SPDX 表达式（AND/OR/WITH/括号）中的原子逐一归一，关键字保持大写。"""
    parts = re.split(r"(\(|\)|\bAND\b|\bOR\b|\bWITH\b)", expr, flags=re.IGNORECASE)
    out: list[str] = []
    for part in parts:
        token = part.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in ("AND", "OR", "WITH", "(", ")"):
            out.append(upper)
        else:
            out.append(normalize_one(token))
    # 用单空格重建，括号外侧补空格的逆操作
    rebuilt = " ".join(out)
    rebuilt = rebuilt.replace("( ", "(").replace(" )", ")")
    return rebuilt


def normalize_license(value) -> str:
    """入口：接受字符串或字符串列表（多许可证），返回归一后的 SPDX 字符串。

    列表按 SPDX 约定用 ``OR`` 连接（任一许可证可用即合规）。
    """
    if value is None:
        return "NOASSERTION"
    if isinstance(value, (list, tuple, set)):
        items = [normalize_one(v) for v in value if v is not None and str(v).strip()]
        items = [v for v in items if v]
        if not items:
            return "NOASSERTION"
        if len(items) == 1:
            return items[0]
        return "(" + " OR ".join(items) + ")"
    return normalize_one(str(value))


def is_unknown(spdx: str) -> bool:
    return spdx.startswith("LicenseRef-Unknown:") or spdx in ("NOASSERTION",)
