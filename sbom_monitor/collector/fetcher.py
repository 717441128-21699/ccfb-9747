"""各生态抓取器：清单分页 + 包详情条件请求，输出统一模型。

每个 fetcher 持有一个 :class:`RepoClient`，对上层暴露同样的三个方法：
- ``list_package_names(page, page_size)``  分页清单
- ``fetch_package(name, etag, last_modified)``  条件请求详情，304 返回 None
- ``feed(after_id, limit)``  变更流（各生态 URL 一致，放基类）
"""
from __future__ import annotations

from urllib.parse import quote

from . import parsers
from .httpclient import RepoClient


class BaseFetcher:
    ecosystem = ""

    def __init__(self, client: RepoClient):
        self.client = client

    def feed(self, after_id: int, limit: int) -> dict:
        resp = self.client.get(f"/feed?after_id={after_id}&limit={limit}")
        if resp.status != 200:
            raise RuntimeError(f"feed 请求失败: HTTP {resp.status}")
        return resp.json()

    def list_package_names(self, page: int, page_size: int) -> dict:
        raise NotImplementedError

    def fetch_package(self, name: str, etag: str | None,
                      last_modified: str | None):
        raise NotImplementedError


class NpmFetcher(BaseFetcher):
    ecosystem = "npm"
    LIST_PATH = "/-/all"

    def list_package_names(self, page: int, page_size: int) -> dict:
        resp = self.client.get(f"{self.LIST_PATH}?page={page}&page_size={page_size}")
        resp.raise_for_status = None
        if resp.status != 200:
            raise RuntimeError(f"npm 清单失败: {resp.status}")
        data = resp.json()
        return {"items": [i["name"] for i in data["items"]],
                "has_more": data["has_more"], "total": data["total"],
                "etag": resp.headers.get("ETag")}

    def fetch_package(self, name: str, etag: str | None, last_modified: str | None):
        resp = self.client.get("/" + quote(name, safe="@/"), etag, last_modified)
        if resp.status == 304:
            return None, resp.headers
        if resp.status == 404:
            return False, resp.headers
        if resp.status != 200:
            raise RuntimeError(f"npm 包详情失败 {name}: {resp.status}")
        return parsers.parse_npm(resp.json()), resp.headers


class PypiFetcher(BaseFetcher):
    ecosystem = "pypi"
    LIST_PATH = "/simple/"

    def list_package_names(self, page: int, page_size: int) -> dict:
        resp = self.client.get(f"{self.LIST_PATH}?page={page}&page_size={page_size}")
        if resp.status != 200:
            raise RuntimeError(f"pypi 清单失败: {resp.status}")
        data = resp.json()
        return {"items": [i["name"] for i in data["items"]],
                "has_more": data["has_more"], "total": data["total"],
                "etag": resp.headers.get("ETag")}

    def fetch_package(self, name: str, etag: str | None, last_modified: str | None):
        resp = self.client.get(f"/pypi/{quote(name)}/json", etag, last_modified)
        if resp.status == 304:
            return None, resp.headers
        if resp.status == 404:
            return False, resp.headers
        if resp.status != 200:
            raise RuntimeError(f"pypi 包详情失败 {name}: {resp.status}")
        pkg = parsers.parse_pypi(resp.json())
        # 逐版本补全依赖与许可证（包级 JSON 只有最新版的 requires_dist）
        self._enrich_versions(pkg)
        return pkg, resp.headers

    def _enrich_versions(self, pkg) -> None:
        from ..common.model import PackageVersion
        for v in pkg.versions:
            r = self.client.get(
                f"/pypi/{quote(pkg.display_name)}/{quote(v.version)}/json")
            if r.status != 200:
                continue  # 单版本失败不阻塞整包，校验通道会兜底
            full = parsers.parse_pypi_version(pkg.display_name, r.json())
            v.licenses = full.licenses
            v.dependencies = full.dependencies
            v.yanked = full.yanked
            v.yanked_reason = full.yanked_reason
            if full.artifacts and full.artifacts[0].get("url"):
                v.artifacts = full.artifacts
            v.raw["license_raw"] = full.raw.get("license_raw")


class MavenFetcher(BaseFetcher):
    ecosystem = "maven"

    def list_package_names(self, page: int, page_size: int) -> dict:
        resp = self.client.get(f"/packages?page={page}&page_size={page_size}")
        if resp.status != 200:
            raise RuntimeError(f"maven 清单失败: {resp.status}")
        data = resp.json()
        return {"items": [i["name"] for i in data["items"]],
                "has_more": data["has_more"], "total": data["total"],
                "etag": resp.headers.get("ETag")}

    def _coord_path(self, name: str) -> str:
        group, artifact = name.split(":", 1)
        return "/".join([group.replace(".", "/"), artifact])

    def fetch_package(self, name: str, etag: str | None, last_modified: str | None):
        base = "/" + self._coord_path(name)
        meta_resp = self.client.get(base + "/maven-metadata.xml", etag, last_modified)
        if meta_resp.status == 304:
            return None, meta_resp.headers
        if meta_resp.status == 404:
            return False, meta_resp.headers
        if meta_resp.status != 200:
            raise RuntimeError(f"maven metadata 失败 {name}: {meta_resp.status}")
        metadata = parsers.parse_maven_metadata(meta_resp.text())
        # 目录 XML 提供制品名/校验和（POM 不含这些信息）
        dir_resp = self.client.get(base + "/")
        directory = parsers.parse_maven_directory(dir_resp.text()) \
            if dir_resp.status == 200 else {}
        poms = []
        for ver in metadata["versions"]:
            pom_path = f"{base}/{quote(ver)}/{name.split(':', 1)[1]}-{quote(ver)}.pom"
            pom_resp = self.client.get(pom_path)
            if pom_resp.status != 200:
                continue
            poms.append(parsers.parse_maven_pom(pom_resp.text()))
        pkg = parsers.build_maven_package(name, metadata, poms, directory)
        return pkg, meta_resp.headers


_FETCHERS = {"npm": NpmFetcher, "pypi": PypiFetcher, "maven": MavenFetcher}


def make_fetcher(ecosystem: str, client: RepoClient) -> BaseFetcher:
    return _FETCHERS[ecosystem](client)
