"""把仿真真值序列化成各生态“原生风格”的 HTTP 响应。

采集端必须像对接真实仓库一样解析这些响应，不能直接读 SimState：
- npm   : registry.npmjs.org 风格的 JSON packument
- pypi  : pypi.org/pypi JSON API（包级 + 单版本两级）
- maven : maven-metadata.xml + maven 目录树 XML
"""
from __future__ import annotations

import json
from urllib.parse import quote

from ..common.timeutil import http_date, parse_http_date
from datetime import datetime


def npm_packument(pkg: dict) -> dict:
    versions = {}
    times = {"created": pkg["versions"][0]["released_at"]}
    for v in pkg["versions"]:
        versions[v["version"]] = {
            "name": pkg["name"],
            "version": v["version"],
            "license": v["license_raw"],
            "description": pkg["description"],
            "homepage": pkg["project_url"],
            "dependencies": {d["name"]: d["spec"]
                             for d in v["dependencies"] if not d["optional"]},
            "optionalDependencies": {d["name"]: d["spec"]
                                     for d in v["dependencies"] if d["optional"]},
            "dist": {
                "tarball": v["artifacts"][0]["url"],
                "integrity": v["artifacts"][0]["checksum"].replace("sha256:", "sha256-"),
                "shasum": v["artifacts"][0]["checksum"].split(":", 1)[1][:40],
            },
            # 真实 npm 没有 yanked 字段，这里用自定义字段模拟撤回，采集端需识别
            "yanked": v["yanked"],
            "yankedReason": v.get("yanked_reason"),
        }
        times[v["version"]] = v["released_at"]
    latest = pkg["versions"][-1]["version"]
    return {
        "_id": pkg["name"],
        "name": pkg["name"],
        "description": pkg["description"],
        "homepage": pkg["project_url"],
        "dist-tags": {"latest": latest},
        "versions": versions,
        "time": times,
    }


def pypi_package_json(pkg: dict) -> dict:
    releases = {}
    latest_version = pkg["versions"][-1]["version"]
    for v in pkg["versions"]:
        releases[v["version"]] = [{
            "filename": v["artifacts"][0]["name"],
            "url": v["artifacts"][0]["url"],
            "digests": {"sha256": v["artifacts"][0]["checksum"].split(":", 1)[1]},
            "yanked": v["yanked"],
            "yanked_reason": v.get("yanked_reason") or "",
            "upload_time_iso_utc": v["released_at"],
        }]
    latest = pkg["versions"][-1]
    # PyPI 新版 API 把依赖放在每个 release 的 info.requires_dist 里
    info = {
        "name": pkg["name"],
        "version": latest_version,
        "summary": pkg["description"],
        "home_page": pkg["project_url"],
        "license": latest["license_raw"],
        "requires_python": ">=3.9",
        "requires_dist": _pypi_requires_dist(latest),
        "yanked": latest["yanked"],
    }
    return {"info": info, "releases": releases, "urls": releases[latest_version]}


def pypi_version_json(pkg: dict, version: str) -> dict | None:
    v = next((x for x in pkg["versions"] if x["version"] == version), None)
    if v is None:
        return None
    return {
        "info": {
            "name": pkg["name"],
            "version": version,
            "summary": pkg["description"],
            "home_page": pkg["project_url"],
            "license": v["license_raw"],
            "requires_python": ">=3.9",
            "requires_dist": _pypi_requires_dist(v),
            "yanked": v["yanked"],
        },
        "urls": [{
            "filename": v["artifacts"][0]["name"],
            "url": v["artifacts"][0]["url"],
            "digests": {"sha256": v["artifacts"][0]["checksum"].split(":", 1)[1]},
            "yanked": v["yanked"],
            "yanked_reason": v.get("yanked_reason") or "",
            "upload_time_iso_utc": v["released_at"],
        }],
    }


def _pypi_requires_dist(v: dict) -> list[str]:
    out = []
    for d in v["dependencies"]:
        item = f'{d["name"]}{d["spec"]}'
        if d["optional"]:
            item += '; extra == "optional"'
        out.append(item)
    return out


def maven_metadata_xml(pkg: dict) -> str:
    group, artifact = pkg["name"].split(":", 1)
    versions = "\n".join(f"    <version>{v['version']}</version>" for v in pkg["versions"])
    latest = pkg["versions"][-1]["version"]
    last_updated = _maven_timestamp(pkg["versions"][-1]["released_at"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<metadata>\n"
        f"  <groupId>{group}</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        f"  <versioning>\n    <latest>{latest}</latest>\n"
        f"    <release>{latest}</release>\n"
        f"    <versions>\n{versions}\n    </versions>\n"
        f"    <lastUpdated>{last_updated}</lastUpdated>\n"
        f"  </versioning>\n"
        "</metadata>\n"
    )


def maven_pom_xml(pkg: dict, v: dict) -> str:
    group, artifact = pkg["name"].split(":", 1)
    deps = []
    for d in v["dependencies"]:
        g, a = d["name"].split(":", 1)
        scope = "<scope>test</scope>" if d["optional"] else ""
        optional = "<optional>true</optional>" if d["optional"] else ""
        deps.append(
            "    <dependency>\n"
            f"      <groupId>{g}</groupId>\n"
            f"      <artifactId>{a}</artifactId>\n"
            f"      <version>{d['spec']}</version>\n"
            f"      {optional}{scope}\n"
            "    </dependency>\n"
        )
    deps_xml = "  <dependencies>\n" + "".join(deps) + "  </dependencies>\n" if deps else ""
    # 撤回标记藏在自定义 XML 元素里，采集端需解析
    yanked_xml = f"  <yanked>true</yanked>\n  <yankedReason>{v.get('yanked_reason') or ''}</yankedReason>\n" if v["yanked"] else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"  <groupId>{group}</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        f"  <version>{v['version']}</version>\n"
        f"  <name>{artifact}</name>\n"
        f"  <description>{pkg['description']}</description>\n"
        f"  <url>{pkg['project_url']}</url>\n"
        f"  <licenses>\n    <license>\n      <name>{v['license_raw']}</name>\n"
        "    </license>\n  </licenses>\n"
        + deps_xml + yanked_xml +
        "</project>\n"
    )


def maven_directory_xml(pkg: dict) -> str:
    """maven 仓库目录风格的 XML，携带每个版本的制品名与校验和（仿真增强）。"""
    items = []
    for v in pkg["versions"]:
        art = v["artifacts"][0]
        items.append(
            f'  <item><name>{v["version"]}/</name>'
            f"<artifact>{art['name']}</artifact>"
            f"<checksum>{art['checksum']}</checksum>"
            f'<yanked>{"true" if v["yanked"] else "false"}</yanked></item>'
        )
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<directory>\n'
            + "\n".join(items) + "\n</directory>\n")


def _maven_timestamp(iso: str) -> str:
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return parsed.strftime("%Y%m%d%H%M%S")


def last_modified(pkg: dict) -> str:
    # 优先用对象级最后变更时间（覆盖 yank/许可证变更），否则回退到版本发布时间
    ts = pkg.get("last_changed") or max(v["released_at"] for v in pkg["versions"])
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return http_date(dt)


def encode_ecosystem_name(ecosystem: str, name: str) -> str:
    """URL 路径中的包名编码。"""
    if ecosystem == "maven":
        g, a = name.split(":", 1)
        return "/".join([g.replace(".", "/"), a])
    return quote(name, safe="@/")
