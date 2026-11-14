"""确定性测试数据生成器：为三个生态生成风格各异但结构真实的包数据。

所有随机数来自带种子的 ``random.Random``，同种子必然产出同一份数据，
便于端到端对账。生成的是“仿真仓库内部真值对象”，序列化由 sim.server 负责。

真值对象结构（与采集端统一模型对应，但保留各生态的原始写法）：

{
  "ecosystem": "npm",
  "name": "@scope/pkg",          # 生态内原始名
  "display_name": "...",
  "description": "...",
  "project_url": "...",
  "versions": [
    {
      "version": "1.2.3",
      "released_at": "2024-01-01T00:00:00.000Z",
      "license_raw": "MIT",       # 故意使用生态里杂乱的许可证写法
      "yanked": false,
      "yanked_reason": null,
      "dependencies": [{"name": "...", "spec": "^1.0.0", "optional": false}],
      "artifacts": [{"name": "...", "url": "...", "checksum": "sha256:...", "checksum_algo": "sha256"}],
      "extra": {...}              # 生态特有字段
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

from ..common.timeutil import to_iso

# 各生态“杂乱”的许可证写法池，归一压力测试用
LICENSE_POOL = [
    "MIT", "mit", "MIT License", "Apache 2.0", "Apache-2.0", "Apache License 2.0",
    "BSD 3-Clause", "BSD-2-Clause", "ISC", "ISC License", "GPLv3", "GPL-3.0",
    "LGPL 2.1", "MPL 2.0", "The Unlicense", "BSD 2-Clause", "ASL 2.0",
]

# 版本演进时可能切换到的许可证（制造 license_change 事件）
LICENSE_CHANGE_POOL = ["Apache 2.0", "MIT", "BSD 3-Clause", "ISC", "MPL 2.0"]

_BASE_TIME = datetime(2023, 1, 1, tzinfo=timezone.utc)


def _checksum(ecosystem: str, name: str, version: str, idx: int) -> str:
    digest = hashlib.sha256(f"{ecosystem}:{name}:{version}:{idx}".encode()).hexdigest()
    return f"sha256:{digest}"


def _release_time(seed_index: int, step: int) -> str:
    return to_iso(_BASE_TIME + timedelta(days=seed_index * 7 + step * 3, hours=step))


# ---------- 版本号生成 ----------

def _semver_versions(rng: random.Random, count: int) -> list[str]:
    major = rng.randint(0, 2)
    minor = rng.randint(0, 4)
    out: list[str] = []
    for i in range(count):
        kind = rng.random()
        if kind < 0.12 and i > 0:  # 预发版
            out.append(f"{major}.{minor}.{i}-rc.{rng.randint(1, 3)}")
        elif kind < 0.2 and i > 0:
            out.append(f"{major}.{minor}.{i}-beta.{rng.randint(1, 2)}")
        else:
            out.append(f"{major}.{minor}.{i}")
        if rng.random() < 0.25:
            minor += 1
    return out


def _pep440_versions(rng: random.Random, count: int) -> list[str]:
    major = rng.randint(0, 2)
    minor = rng.randint(0, 5)
    out: list[str] = []
    for i in range(count):
        kind = rng.random()
        if kind < 0.1 and i > 0:
            out.append(f"{major}.{minor}.{i}a{rng.randint(1, 3)}")
        elif kind < 0.18 and i > 0:
            out.append(f"{major}.{minor}.{i}rc{rng.randint(1, 2)}")
        elif kind < 0.25 and i > 0:
            out.append(f"{major}.{minor}.{i}.post{rng.randint(1, 2)}")
        else:
            out.append(f"{major}.{minor}.{i}")
        if rng.random() < 0.2:
            minor += 1
    return out


def _maven_versions(rng: random.Random, count: int) -> list[str]:
    major = rng.randint(1, 3)
    minor = rng.randint(0, 4)
    out: list[str] = []
    for i in range(count):
        kind = rng.random()
        if kind < 0.1 and i > 0:
            out.append(f"{major}.{minor}.{i}-alpha-{rng.randint(1, 2)}")
        elif kind < 0.2 and i > 0:
            out.append(f"{major}.{minor}.{i}-beta-{rng.randint(1, 3)}")
        elif kind < 0.28 and i > 0:
            out.append(f"{major}.{minor}.{i}-RC{rng.randint(1, 2)}")
        else:
            out.append(f"{major}.{minor}.{i}")
        if rng.random() < 0.2:
            minor += 1
    return out


# ---------- 依赖规格的生态写法 ----------

def _npm_spec(rng: random.Random) -> str:
    styles = ["^{maj}.0.0", "~{maj}.{min}.0", ">={maj}.0.0 <{maj2}.0.0", "{maj}.x", "*"]
    maj = rng.randint(0, 2)
    return rng.choice(styles).format(maj=maj, maj2=maj + 1, min=rng.randint(0, 4))


def _pypi_spec(rng: random.Random) -> str:
    maj = rng.randint(0, 2)
    styles = [f">={maj}.0", f"~={maj}.{rng.randint(0, 3)}",
              f">={maj}.0,<{maj + 1}", f"=={maj}.*", ""]
    return rng.choice(styles)


def _maven_spec(rng: random.Random) -> str:
    maj = rng.randint(1, 3)
    styles = [f"[{maj}.0,{maj + 1}.0)", f"[{maj}.0,)", f"[{maj}.{rng.randint(0, 2)}.0]",
              f"{maj}.0"]  # 最后一个是裸版本软需求
    return rng.choice(styles)


# ---------- 包名 ----------

def _npm_names(rng: random.Random, n: int) -> list[str]:
    words = ["lodash-ish", "request-ng", "chalk-like", "express-kit", "react-hooks",
             "vue-utils", "webpack-plugin", "babel-preset", "eslint-config", "jest-helper",
             "core-lib", "cli-tool", "state-store", "form-engine", "router-lite",
             "cache-manager", "logger-pro", "config-loader", "task-queue", "event-bus"]
    names = []
    for i in range(n):
        base = words[i % len(words)]
        if i >= len(words):
            base = f"{base}-{i // len(words)}"
        if rng.random() < 0.3:
            names.append(f"@acme/{base}")
        else:
            names.append(base)
    return names


def _pypi_names(rng: random.Random, n: int) -> list[str]:
    words = ["requests-like", "numpy-tools", "flask-kit", "django-helper", "pytest-plugin",
             "celery-beat", "sqlalchemy-mix", "pydantic-addon", "fastapi-guard",
             "uvicorn-worker", "click-ext", "rich-output", "httpx-mock", "pillow-filter",
             "pandas-utils", "scipy-sparse", "matplotlib-theme", "boto3-local",
             "cryptography-extra", "pyyaml-loader"]
    names = []
    for i in range(n):
        base = words[i % len(words)]
        if i >= len(words):
            # 连字符 + 序号，保证 PEP 503 规范名唯一（下划线写法的归一由单测覆盖）
            base = f"{base}-{i // len(words)}"
        names.append(base)
    return names


def _maven_names(rng: random.Random, n: int) -> list[str]:
    groups = ["com.acme.platform", "org.acme.common", "io.acme.cloud", "net.acme.data"]
    arts = ["core", "web", "persistence", "security", "messaging", "scheduler",
            "reporting", "audit", "gateway", "registry", "sdk", "client",
            "annotations", "utils", "config", "cache", "metrics", "tracing",
            "notification", "workflow"]
    names = []
    for i in range(n):
        g = groups[i % len(groups)]
        a = arts[i % len(arts)]
        if i >= len(arts):
            a = f"{a}-{i // len(arts)}"
        names.append(f"{g}:{a}")
    return names


_VERSION_GEN = {"npm": _semver_versions, "pypi": _pep440_versions, "maven": _maven_versions}
_NAME_GEN = {"npm": _npm_names, "pypi": _pypi_names, "maven": _maven_names}
_SPEC_GEN = {"npm": _npm_spec, "pypi": _pypi_spec, "maven": _maven_spec}


def generate_ecosystem(ecosystem: str, count: int, seed: int) -> list[dict]:
    """生成一个生态的 ``count`` 个包（含版本与依赖），纯函数、可复现。"""
    rng = random.Random(seed)
    names = _NAME_GEN[ecosystem](rng, count)
    packages: list[dict] = []
    for idx, name in enumerate(names):
        ver_count = rng.randint(2, 7)
        versions_raw = _VERSION_GEN[ecosystem](rng, ver_count)
        # 同一包内版本去重保序
        seen: set[str] = set()
        versions: list[str] = []
        for v in versions_raw:
            if v not in seen:
                seen.add(v)
                versions.append(v)
        initial_license = rng.choice(LICENSE_POOL)
        # 依赖只指向“编号更小”的包，保证无环；约 60% 的包有依赖
        possible_deps = names[:idx]
        dep_count = min(len(possible_deps), rng.randint(0, 3)) if rng.random() < 0.6 else 0
        dep_targets = rng.sample(possible_deps, dep_count) if dep_count else []
        version_objs = []
        for step, ver in enumerate(versions):
            deps = []
            for dep_name in dep_targets:
                deps.append({
                    "name": dep_name,
                    "spec": _SPEC_GEN[ecosystem](rng),
                    "optional": rng.random() < 0.15,
                })
            license_raw = initial_license
            yanked = rng.random() < 0.08
            version_objs.append({
                "version": ver,
                "released_at": _release_time(idx, step),
                "license_raw": license_raw,
                "yanked": yanked,
                "yanked_reason": "security advisory" if yanked else None,
                "dependencies": deps,
                "artifacts": [{
                    "name": _artifact_name(ecosystem, name, ver),
                    "url": f"https://artifact.local/{ecosystem}/{name}/{ver}/package",
                    "checksum": _checksum(ecosystem, name, ver, step),
                    "checksum_algo": "sha256",
                }],
                "extra": {},
            })
        packages.append({
            "ecosystem": ecosystem,
            "name": name,
            "display_name": name,
            "description": f"Simulated {ecosystem} package {name}",
            "project_url": f"https://example.invalid/{ecosystem}/{name}",
            "versions": version_objs,
        })
    return packages


def _artifact_name(ecosystem: str, name: str, version: str) -> str:
    if ecosystem == "npm":
        short = name.split("/")[-1]
        return f"{short}-{version}.tgz"
    if ecosystem == "pypi":
        short = name.split("/")[-1]
        return f"{short.replace('-', '_')}-{version}-py3-none-any.whl"
    g, a = name.split(":", 1)
    gpath = g.replace(".", "/")
    return f"{a}-{version}.jar"
