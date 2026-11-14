"""按生态分派版本范围匹配与版本排序的统一入口。"""
from __future__ import annotations

from . import semver, pep440, maven as maven_ver

_MODULES = {"npm": semver, "pypi": pep440, "maven": maven_ver}


def satisfies(ecosystem: str, version: str, spec: str) -> bool:
    """具体版本 ``version`` 是否满足该生态的版本范围 ``spec``。"""
    mod = _MODULES[ecosystem]
    if not spec or spec.strip() in ("*", ""):
        return True
    return mod.satisfies(version, spec)


def sort_versions(ecosystem: str, versions: list[str]) -> list[str]:
    return _MODULES[ecosystem].sort_versions(versions)


def latest_version(ecosystem: str, versions: list[str]) -> str:
    if not versions:
        return ""
    return sort_versions(ecosystem, versions)[-1]
