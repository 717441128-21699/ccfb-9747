"""统一元数据模型：三个生态采集后都归一成这套结构。

核心结构（纯 dict，可直接 JSON 序列化入库）：

Package
  key            全局唯一键 ``<ecosystem>:<name>``
  ecosystem      npm | pypi | maven
  name           包名（生态内规范名：npm 小写、pypi PEP503、maven group:artifact）
  display_name   展示名（保留大小写）
  description    描述
  project_url    项目主页
  repo_url       代码仓库
  licenses       包级归一 SPDX（取最新非撤回版本，仅作展示；合规判定逐版本）
  latest_version 最新版本号（按生态版本序）
  first_seen     首次采集时间
  last_seen      最近采集时间
  updated_at     上游变更时间（取版本最大发布时间）
  raw            原始元数据（调试/取证用）

PackageVersion
  key            同包 key
  version        版本号（原始字符串）
  released_at    发布时间 ISO
  licenses       该版本归一后的 SPDX
  yanked         是否撤回
  yanked_reason  撤回原因
  artifacts      制品列表 [{name, url, checksum, checksum_algo}]
  dependencies   [{name, spec, ecosystem, optional}]
  raw            原始版本元数据
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .licenses import normalize_license

ECOSYSTEMS = ("npm", "pypi", "maven")


def make_key(ecosystem: str, name: str) -> str:
    return f"{ecosystem}:{name}"


def canonical_name(ecosystem: str, name: str) -> str:
    """生态内规范名，作为同一包的归并依据。"""
    if ecosystem == "npm":
        # npm 作用域保留，整体小写
        return name.strip().lower()
    if ecosystem == "pypi":
        # PEP 503：小写、连字符/下划线/点归一为连字符
        import re
        return re.sub(r"[-_.]+", "-", name.strip().lower())
    if ecosystem == "maven":
        parts = name.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"maven 坐标必须是 group:artifact，收到: {name!r}")
        g, a = parts
        return f"{g.strip().lower()}:{a.strip().lower()}"
    raise ValueError(f"未知生态: {ecosystem}")


@dataclass
class Dependency:
    name: str
    spec: str
    ecosystem: str
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Dependency":
        return Dependency(
            name=d["name"], spec=d.get("spec", ""),
            ecosystem=d.get("ecosystem", ""), optional=bool(d.get("optional", False)),
        )


@dataclass
class PackageVersion:
    version: str
    released_at: str
    licenses: str
    yanked: bool = False
    yanked_reason: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dependencies"] = [x.to_dict() if isinstance(x, Dependency) else x
                             for x in self.dependencies]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PackageVersion":
        return PackageVersion(
            version=d["version"],
            released_at=d.get("released_at", ""),
            licenses=d.get("licenses", "NOASSERTION"),
            yanked=bool(d.get("yanked", False)),
            yanked_reason=d.get("yanked_reason"),
            artifacts=list(d.get("artifacts", [])),
            dependencies=[Dependency.from_dict(x) for x in d.get("dependencies", [])],
            raw=d.get("raw", {}),
        )


@dataclass
class Package:
    ecosystem: str
    name: str
    display_name: str = ""
    description: str = ""
    project_url: str = ""
    repo_url: str = ""
    licenses: str = "NOASSERTION"
    latest_version: str = ""
    first_seen: str = ""
    last_seen: str = ""
    updated_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    versions: list[PackageVersion] = field(default_factory=list)

    @property
    def key(self) -> str:
        return make_key(self.ecosystem, self.name)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        d["versions"] = [v.to_dict() for v in self.versions]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Package":
        p = Package(
            ecosystem=d["ecosystem"],
            name=d["name"],
            display_name=d.get("display_name", ""),
            description=d.get("description", ""),
            project_url=d.get("project_url", ""),
            repo_url=d.get("repo_url", ""),
            licenses=d.get("licenses", "NOASSERTION"),
            latest_version=d.get("latest_version", ""),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
            updated_at=d.get("updated_at", ""),
            raw=d.get("raw", {}),
            versions=[PackageVersion.from_dict(v) for v in d.get("versions", [])],
        )
        return p


def normalize_versions_licenses(versions: list[PackageVersion]) -> None:
    """就地把每个版本的许可证原始值归一为 SPDX（raw 保留原值）。"""
    for v in versions:
        raw_lic = v.raw.get("_license_raw", v.licenses) if v.raw else v.licenses
        v.licenses = normalize_license(raw_lic)
