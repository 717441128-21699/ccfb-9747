"""把各生态的原生响应解析成统一 :class:`~sbom_monitor.common.model.Package`。

解析器只负责“翻译”，不做存储决策。许可证在翻译阶段归一为 SPDX。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..common.licenses import normalize_license
from ..common.model import (
    Dependency, Package, PackageVersion, canonical_name, make_key,
)
from ..versions import resolver


def parse_npm(packument: dict) -> Package:
    name = canonical_name("npm", packument["name"])
    versions: list[PackageVersion] = []
    for ver_str, data in packument.get("versions", {}).items():
        deps = []
        for dep_name, spec in (data.get("dependencies") or {}).items():
            deps.append(Dependency(name=canonical_name("npm", dep_name),
                                   spec=spec, ecosystem="npm", optional=False))
        for dep_name, spec in (data.get("optionalDependencies") or {}).items():
            deps.append(Dependency(name=canonical_name("npm", dep_name),
                                   spec=spec, ecosystem="npm", optional=True))
        dist = data.get("dist") or {}
        checksum = dist.get("integrity", "").replace("sha256-", "sha256:")
        versions.append(PackageVersion(
            version=ver_str,
            released_at=(packument.get("time") or {}).get(ver_str, ""),
            licenses=normalize_license(data.get("license")),
            yanked=bool(data.get("yanked", False)),
            yanked_reason=data.get("yankedReason"),
            artifacts=[{"name": f"{name}-{ver_str}.tgz",
                        "url": dist.get("tarball", ""),
                        "checksum": checksum, "checksum_algo": "sha256"}],
            dependencies=deps,
            raw={"license_raw": data.get("license"), "native": data},
        ))
    latest = (packument.get("dist-tags") or {}).get("latest", "")
    pkg = Package(
        ecosystem="npm", name=name,
        display_name=packument.get("name", name),
        description=packument.get("description", ""),
        project_url=packument.get("homepage", ""),
        licenses=_latest_license(versions, latest),
        latest_version=latest,
        updated_at=max((v.released_at for v in versions), default=""),
        raw={"native_name": packument.get("name")},
        versions=versions,
    )
    return pkg


def parse_pypi(payload: dict) -> Package:
    info = payload["info"]
    name = canonical_name("pypi", info["name"])
    releases = payload.get("releases", {})
    # 依赖以单版本 JSON 为准；包级 JSON 的 info.requires_dist 只代表最新版
    latest_deps = _parse_pypi_requires_dist(info.get("requires_dist") or [])
    versions: list[PackageVersion] = []
    for ver_str, files in releases.items():
        files = files or []
        first = files[0] if files else {}
        # 包级 JSON 不携带每个版本的 requires_dist，最新版用 info，其余版本留空
        # （引擎会对每个版本再请求 /pypi/<name>/<version>/json 补全）
        deps = latest_deps if ver_str == info.get("version") else []
        versions.append(PackageVersion(
            version=ver_str,
            released_at=first.get("upload_time_iso_utc", ""),
            licenses=normalize_license(info.get("license")) if ver_str == info.get("version")
                     else "NOASSERTION",
            yanked=bool(first.get("yanked", False)),
            yanked_reason=first.get("yanked_reason") or None,
            artifacts=[{"name": first.get("filename", ""),
                        "url": first.get("url", ""),
                        "checksum": "sha256:" + (first.get("digests") or {}).get("sha256", ""),
                        "checksum_algo": "sha256"}],
            dependencies=deps,
            raw={"license_raw": info.get("license") if ver_str == info.get("version") else None},
        ))
    latest = info.get("version", "")
    pkg = Package(
        ecosystem="pypi", name=name,
        display_name=info.get("name", name),
        description=info.get("summary", ""),
        project_url=info.get("home_page", ""),
        licenses=normalize_license(info.get("license")),
        latest_version=latest,
        updated_at=max((v.released_at for v in versions), default=""),
        raw={"native_name": info.get("name")},
        versions=versions,
    )
    return pkg


def parse_pypi_version(name: str, payload: dict) -> PackageVersion:
    """解析 /pypi/<name>/<version>/json 的单版本响应（补全依赖与许可证）。"""
    info = payload["info"]
    urls = payload.get("urls") or []
    first = urls[0] if urls else {}
    deps = _parse_pypi_requires_dist(info.get("requires_dist") or [])
    return PackageVersion(
        version=info["version"],
        released_at=first.get("upload_time_iso_utc", ""),
        licenses=normalize_license(info.get("license")),
        yanked=bool(info.get("yanked", False) or first.get("yanked", False)),
        yanked_reason=first.get("yanked_reason") or None,
        artifacts=[{"name": first.get("filename", ""),
                    "url": first.get("url", ""),
                    "checksum": "sha256:" + (first.get("digests") or {}).get("sha256", ""),
                    "checksum_algo": "sha256"}],
        dependencies=deps,
        raw={"license_raw": info.get("license"), "native": info},
    )


def _parse_pypi_requires_dist(entries: list[str]) -> list[Dependency]:
    deps: list[Dependency] = []
    for entry in entries:
        optional = 'extra ==' in entry
        main = entry.split(";")[0].strip()
        if not main:
            continue
        # 拆名字与说明符：名字是第一段字母数字-_.,
        i = 0
        while i < len(main) and (main[i].isalnum() or main[i] in "-_.,"):
            i += 1
        dep_name = canonical_name("pypi", main[:i].strip())
        spec = main[i:].strip().strip("()")
        deps.append(Dependency(name=dep_name, spec=spec,
                               ecosystem="pypi", optional=optional))
    return deps


def parse_maven_metadata(xml_text: str) -> dict:
    """解析 maven-metadata.xml，返回坐标与版本清单。"""
    root = ET.fromstring(xml_text)
    group = root.findtext("groupId", "")
    artifact = root.findtext("artifactId", "")
    versions = [v.text or "" for v in root.findall("./versioning/versions/version")]
    latest = root.findtext("./versioning/latest", versions[-1] if versions else "")
    return {"name": canonical_name("maven", f"{group}:{artifact}"),
            "versions": versions, "latest": latest}


def parse_maven_pom(xml_text: str) -> dict:
    """解析 POM，返回版本级元数据（许可证/依赖/撤回标记）。"""
    root = ET.fromstring(xml_text)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0].strip("{")
    def q(tag: str) -> str:
        return f"{{{ns}}}{tag}" if ns else tag
    group = root.findtext(q("groupId"), "")
    artifact = root.findtext(q("artifactId"), "")
    version = root.findtext(q("version"), "")
    lic = root.findtext(f"./{q('licenses')}/{q('license')}/{q('name')}")
    deps: list[Dependency] = []
    for dep in root.findall(f"./{q('dependencies')}/{q('dependency')}"):
        dg = dep.findtext(q("groupId"), "")
        da = dep.findtext(q("artifactId"), "")
        dv = dep.findtext(q("version"), "")
        optional = (dep.findtext(q("optional")) or "").strip() == "true"
        deps.append(Dependency(
            name=canonical_name("maven", f"{dg}:{da}"),
            spec=dv, ecosystem="maven", optional=optional,
        ))
    yanked = (root.findtext(q("yanked")) or "").strip() == "true"
    yanked_reason = root.findtext(q("yankedReason")) or None
    description = root.findtext(q("description"), "") or ""
    project_url = root.findtext(q("url"), "") or ""
    return {
        "name": canonical_name("maven", f"{group}:{artifact}"),
        "version": version,
        "license_raw": lic,
        "licenses": normalize_license(lic),
        "dependencies": deps,
        "yanked": yanked,
        "yanked_reason": yanked_reason,
        "description": description,
        "project_url": project_url,
    }


def parse_maven_directory(xml_text: str) -> dict:
    """解析目录 XML，返回 version -> {artifact, checksum, yanked}。"""
    root = ET.fromstring(xml_text)
    out: dict[str, dict] = {}
    for item in root.findall("./item"):
        name = (item.findtext("name") or "").rstrip("/")
        if not name:
            continue
        out[name] = {
            "artifact": item.findtext("artifact", "") or "",
            "checksum": item.findtext("checksum", "") or "",
            "yanked": (item.findtext("yanked") or "").strip() == "true",
        }
    return out


def build_maven_package(coord: str, metadata: dict, poms: list[dict],
                        directory: dict | None = None) -> Package:
    name = canonical_name("maven", coord)
    versions: list[PackageVersion] = []
    directory = directory or {}
    for pom in poms:
        dir_info = directory.get(pom["version"], {})
        versions.append(PackageVersion(
            version=pom["version"],
            released_at="",  # maven-metadata 无逐版本时间，引擎用 lastUpdated 兜底
            licenses=pom["licenses"],
            yanked=pom["yanked"] or dir_info.get("yanked", False),
            yanked_reason=pom.get("yanked_reason"),
            artifacts=[{"name": dir_info.get("artifact")
                        or f"{name.split(':')[1]}-{pom['version']}.jar",
                        "url": "",
                        "checksum": dir_info.get("checksum", ""),
                        "checksum_algo": "sha256"}],
            dependencies=pom["dependencies"],
            raw={"license_raw": pom.get("license_raw"), "native": {"pom": True}},
        ))
    latest = metadata.get("latest", "")
    first_pom = poms[0] if poms else {}
    pkg = Package(
        ecosystem="maven", name=name, display_name=coord,
        description=first_pom.get("description", ""),
        project_url=first_pom.get("project_url", ""),
        licenses=_latest_license(versions, latest),
        latest_version=latest,
        updated_at="",
        raw={"native_name": coord},
        versions=versions,
    )
    return pkg


def _latest_license(versions: list[PackageVersion], latest: str) -> str:
    if not versions:
        return "NOASSERTION"
    if latest:
        for v in versions:
            if v.version == latest and not v.yanked:
                return v.licenses
    non_yanked = [v for v in versions if not v.yanked]
    return (non_yanked[-1] if non_yanked else versions[-1]).licenses
