"""Web 控制台 HTTP 服务（stdlib，服务端渲染 + 少量 JSON 接口）。

页面：
  /            总览（统计卡片 + 引擎心跳 + 最近告警）
  /repos       仓库配置（查看/限流参数/故障注入入口）
  /tasks       采集任务（全量游标、feed 位点、校验历史、HTTP 统计）
  /packages    包库（筛选/分页/详情含版本与依赖）
  /graph       依赖图（上下游 BFS、版本范围解析、影响面）
  /alerts      变更告警（类型/严重度筛选、确认、关注名单管理）
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from ..collector.graph import DependencyGraph
from .views import esc, page_shell


class WebHandler(BaseHTTPRequestHandler):
    server: "WebServer"

    def log_message(self, fmt, *args):
        pass

    @property
    def db(self):
        return self.server.db

    # ---------- 入口 ----------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            self._route_get(parsed.path, qs)
        except Exception as exc:
            self._html(500, page_shell("错误", f"<pre>{esc(repr(exc))}</pre>"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(body).items()}
        try:
            self._route_post(parsed.path, params)
        except Exception as exc:
            self._html(500, page_shell("错误", f"<pre>{esc(repr(exc))}</pre>"))

    # ---------- GET 路由 ----------

    def _route_get(self, path: str, qs: dict) -> None:
        if path == "/":
            return self._page_dashboard()
        if path == "/repos":
            return self._page_repos()
        if path == "/tasks":
            return self._page_tasks()
        if path == "/packages":
            return self._page_packages(qs)
        if path.startswith("/packages/"):
            return self._page_package_detail(path[len("/packages/"):])
        if path == "/graph":
            return self._page_graph(qs)
        if path == "/alerts":
            return self._page_alerts(qs)
        if path == "/api/stats":
            return self._json(200, self._stats())
        self._html(404, page_shell("404", "<p>页面不存在</p>"))

    # ---------- POST 路由 ----------

    def _route_post(self, path: str, p: dict) -> None:
        if path == "/repos/fault":
            self._inject_fault(p)
        elif path == "/watch/add":
            self.db.add_watch(p["ecosystem"], p["name"], p.get("component", ""),
                              p.get("note", ""))
            self._redirect("/alerts")
        elif path == "/alerts/ack":
            self._ack_alert(p)
        else:
            self._html(404, page_shell("404", "<p>操作不存在</p>"))

    # ---------- 总览 ----------

    def _page_dashboard(self) -> None:
        s = self._stats()
        cards = f"""
        <div class="cards">
          {self._card('监测包总数', s['packages'])}
          {self._card('版本总数', s['versions'])}
          {self._card('依赖边数', s['edges'])}
          {self._card('未处理告警', s['open_alerts'], bad=s['open_alerts'] > 0)}
          {self._card('关注组件', s['watched'])}
          {self._card('校验补采次数', s['verified_fixed'])}
        </div>"""
        heart = self._heartbeat_panel()
        recent = self.db.list_alerts(limit=10)
        rows = "".join(
            f"<tr><td><span class='badge {esc(a['severity'])}'>{esc(a['severity'])}</span></td>"
            f"<td><span class='badge eco'>{esc(a['ecosystem'])}</span></td>"
            f"<td class='mono'>{esc(a['package'])}{('@'+esc(a['version'])) if a['version'] else ''}</td>"
            f"<td>{esc(a['title'])}</td><td class='muted'>{esc(a['created_at'])}</td></tr>"
            for a in recent) or "<tr><td colspan=5 class=muted>暂无告警</td></tr>"
        body = f"""
        <h1>监测总览</h1>{cards}
        <h2>引擎心跳</h2><div class="panel">{heart}</div>
        <h2>最近告警</h2>
        <div class="panel"><table><thead><tr><th>级别</th><th>生态</th><th>对象</th><th>内容</th><th>时间</th></tr></thead>
        <tbody>{rows}</tbody></table></div>"""
        self._html(200, page_shell("总览", body, "/"))

    def _heartbeat_panel(self) -> str:
        rows = []
        for r in self.db.list_repos():
            raw = self.db.get_meta(f"heartbeat:{r['ecosystem']}")
            hb = json.loads(raw) if raw else {"status": "未启动", "at": "-"}
            dot = "green" if hb.get("status") in ("feed", "verifying", "full_crawl") else "amber"
            rows.append(
                f"<tr><td><span class='badge eco'>{esc(r['ecosystem'])}</span></td>"
                f"<td><span class='dot {dot}'></span>{esc(hb.get('status','-'))}</td>"
                f"<td class='mono'>{esc(r['base_url'])}</td>"
                f"<td class='muted'>{esc(hb.get('at','-'))}</td>"
                f"<td class='mono'>{esc(json.dumps(hb.get('summary',{}), ensure_ascii=False)[:120])}</td></tr>")
        return ("<table><thead><tr><th>生态</th><th>状态</th><th>地址</th><th>心跳时间</th>"
                f"<th>最近一轮</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")

    # ---------- 仓库配置 ----------

    def _page_repos(self) -> None:
        rows = []
        for r in self.db.list_repos():
            rows.append(f"""
            <tr><td><span class='badge eco'>{esc(r['ecosystem'])}</span></td>
            <td class='mono'>{esc(r['base_url'])}</td>
            <td>{'启用' if r['enabled'] else '停用'}</td>
            <td class='mono'>{esc(r['rate_per_sec'])}/s · burst {esc(r['burst'])}</td>
            <td>
              <form method=post action="/repos/fault" class="formrow" style="margin:0">
                <input type=hidden name=ecosystem value="{esc(r['ecosystem'])}">
                <input type=hidden name=base_url value="{esc(r['base_url'])}">
                <button class="btn secondary" name="offline" value="true">模拟下线</button>
                <button class="btn secondary" name="offline" value="false">恢复</button>
                <button class="btn secondary" name="error_rate" value="0.5">50%报错</button>
                <button class="btn secondary" name="error_rate" value="0">清除报错</button>
              </form>
            </td></tr>""")
        body = f"""
        <h1>仓库配置</h1>
        <p class="muted">三个本地仿真仓库，风格分别模拟 npm / PyPI / Maven Central。
        可随时注入故障验证限流退避与故障隔离。</p>
        <div class="panel"><table><thead><tr><th>生态</th><th>地址</th><th>状态</th>
        <th>限流配置</th><th>故障注入</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""
        self._html(200, page_shell("仓库配置", body, "/repos"))

    def _inject_fault(self, p: dict) -> None:
        from ..collector.httpclient import RepoClient
        payload = {}
        if "offline" in p:
            payload["offline"] = p["offline"] == "true"
        if "error_rate" in p:
            payload["error_rate"] = float(p["error_rate"])
        RepoClient(p["base_url"]).post_json("/__admin__/fault", payload)
        self._redirect("/repos")

    # ---------- 采集任务 ----------

    def _page_tasks(self) -> None:
        blocks = []
        for r in self.db.list_repos():
            eco = r["ecosystem"]
            cur = self.db.get_cursor(eco)
            cp = self.db.get_feed_checkpoint(eco)
            with self.db._lock:
                verifs = [dict(x) for x in self.db.conn.execute(
                    "SELECT * FROM verification_runs WHERE ecosystem=? "
                    "ORDER BY id DESC LIMIT 5", (eco,)).fetchall()]
            cur_txt = ("未开始" if cur is None else
                       f"第 {cur['next_page']} 页 · {'已完成' if cur['done'] else '进行中'}")
            vrows = "".join(
                f"<tr><td>#{v['id']}</td><td>{esc(v['status'])}</td>"
                f"<td>{esc(v['started_at'])}</td><td>{esc(v.get('finished_at') or '-')}</td>"
                f"<td>{v['packages_checked']}</td><td>{v['packages_fixed']}</td>"
                f"<td>{v['missing_found']}</td></tr>" for v in verifs) or \
                "<tr><td colspan=7 class=muted>暂无校验记录</td></tr>"
            blocks.append(f"""
            <div class="panel">
              <h2 style="margin-top:0"><span class='badge eco'>{esc(eco)}</span> 采集进度</h2>
              <p>全量爬取：<b>{esc(cur_txt)}</b>　|　变更流位点：<b class=mono>{cp}</b></p>
              <table><thead><tr><th>运行</th><th>状态</th><th>开始</th><th>结束</th>
              <th>检查包</th><th>补采</th><th>发现缺失</th></tr></thead>
              <tbody>{vrows}</tbody></table>
            </div>""")
        self._html(200, page_shell("采集任务",
                    f"<h1>采集任务</h1>{''.join(blocks)}", "/tasks"))

    # ---------- 包库 ----------

    def _page_packages(self, qs: dict) -> None:
        page = int(qs.get("page", ["1"])[0])
        page_size = 20
        eco_filter = qs.get("ecosystem", [""])[0]
        search = qs.get("q", [""])[0]
        where, args = [], []
        if eco_filter:
            where.append("ecosystem=?"); args.append(eco_filter)
        if search:
            where.append("(name LIKE ? OR display_name LIKE ?)")
            args += [f"%{search}%", f"%{search}%"]
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        with self.db._lock:
            total = self.db.conn.execute(
                f"SELECT COUNT(*) n FROM packages {where_sql}", args).fetchone()["n"]
            rows = self.db.conn.execute(
                f"SELECT * FROM packages {where_sql} ORDER BY ecosystem, name LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
        trs = []
        for r in rows:
            watched = self.db.is_watched(r["key"])
            nver = len(self.db.get_package_versions(r["key"]))
            trs.append(
                f"<tr><td><span class='badge eco'>{esc(r['ecosystem'])}</span></td>"
                f"<td><a class=mono href='/packages/{esc(r['key'])}'>{esc(r['name'])}</a>"
                f"{' ⭐' if watched else ''}</td>"
                f"<td class=mono>{esc(r['licenses'])}</td>"
                f"<td>{nver}</td><td class=mono>{esc(r['latest_version'])}</td>"
                f"<td class='muted'>{esc(r['updated_at'])}</td></tr>")
        pages = max(1, (total + page_size - 1) // page_size)
        pager = self._pager("/packages", page, pages,
                            {"ecosystem": eco_filter, "q": search})
        body = f"""
        <h1>包库</h1>
        <form class="formrow" method=get action="/packages">
          <select name=ecosystem>
            <option value="">全部生态</option>
            {''.join(f'<option {("selected" if e==eco_filter else "")}>{e}</option>'
                     for e in ('npm','pypi','maven'))}
          </select>
          <input name=q placeholder="搜索包名" value="{esc(search)}">
          <button class=btn>筛选</button>
        </form>
        <div class="panel"><table><thead><tr><th>生态</th><th>包名</th><th>许可证(SPDX)</th>
        <th>版本数</th><th>最新</th><th>更新时间</th></tr></thead>
        <tbody>{''.join(trs) or '<tr><td colspan=6 class=muted>无数据</td></tr>'}</tbody></table>
        {pager}<p class=muted>共 {total} 个包</p></div>"""
        self._html(200, page_shell("包库", body, "/packages"))

    def _page_package_detail(self, key: str) -> None:
        pkg = self.db.get_package(key)
        if not pkg:
            return self._html(404, page_shell("未找到", f"<p>包 {esc(key)} 不存在</p>"))
        versions = self.db.get_package_versions(key)
        vrows = []
        for v in versions:
            deps = json.loads(v["dependencies_json"] or "[]")
            yank = "<span class='badge high'>已撤回</span>" if v["yanked"] else ""
            vrows.append(
                f"<tr><td class=mono>{esc(v['version'])}</td>"
                f"<td class=mono>{esc(v['licenses'])}</td><td>{yank}</td>"
                f"<td>{esc(v.get('yanked_reason') or '')}</td>"
                f"<td>{len(deps)}</td><td class='muted'>{esc(v['released_at'])}</td></tr>")
        graph = DependencyGraph(self.db)
        up = graph.upstream(key, depth=3)
        down = graph.downstream(key, depth=3)
        up_list = "".join(
            f"<li class=mono>{esc(n['key'])}（{n['level']} 跳）</li>"
            for n in up["nodes"] if n["key"] != key) or "<li class=muted>无</li>"
        down_list = "".join(
            f"<li class=mono>{esc(n['key'])}（{n['level']} 跳）</li>"
            for n in down["nodes"] if n["key"] != key) or "<li class=muted>无</li>"
        body = f"""
        <h1 class=mono>{esc(pkg['name'])} <span class="badge eco">{esc(pkg['ecosystem'])}</span></h1>
        <p class=muted>{esc(pkg['description'])}</p>
        <div class="cards">
          {self._card('版本数', len(versions))}
          {self._card('最新版本', pkg['latest_version'])}
          {self._card('上游依赖数', len(down['nodes'])-1)}
          {self._card('下游被依赖数', len(up['nodes'])-1)}
        </div>
        <h2>版本清单</h2>
        <div class="panel"><table><thead><tr><th>版本</th><th>许可证 SPDX</th><th>撤回</th>
        <th>撤回原因</th><th>依赖数</th><th>发布时间</th></tr></thead><tbody>{''.join(vrows)}</tbody></table></div>
        <h2>依赖关系</h2>
        <div class="panel" style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
          <div><b>谁依赖我（上游/入边）</b><ul>{up_list}</ul></div>
          <div><b>我依赖谁（下游/出边）</b><ul>{down_list}</ul></div>
        </div>
        <p><a class="btn secondary" href="/graph?key={esc(key)}">在依赖图中查看 →</a></p>"""
        self._html(200, page_shell(pkg["name"], body, "/packages"))

    # ---------- 依赖图 ----------

    def _page_graph(self, qs: dict) -> None:
        key = qs.get("key", [""])[0]
        direction = qs.get("dir", ["down"])[0]
        spec = qs.get("spec", [""])[0]
        result_html = ""
        if key:
            pkg = self.db.get_package(key)
            if not pkg:
                result_html = "<p class='muted'>包不存在</p>"
            else:
                graph = DependencyGraph(self.db)
                walk = graph.upstream(key, 8) if direction == "up" else graph.downstream(key, 8)
                impact = graph.impact_analysis(key)
                lines = self._render_ascii_tree(walk, direction)
                satisfy_html = ""
                if spec:
                    vers = graph.satisfying_versions(key, spec)
                    satisfy_html = (f"<h2>版本范围解析 <code>{esc(spec)}</code></h2>"
                                    f"<div class=panel><p>满足的具体版本：</p><p>"
                                    + " ".join(f"<span class=tag>{esc(v)}</span>" for v in vers)
                                    + "</p></div>")
                watched_impact = "".join(
                    f"<li class=mono>{esc(x['key'])}（{x['level']} 跳）{'⭐' if x['watched'] else ''}</li>"
                    for x in impact["watched_impacted"]) or "<li class=muted>无关注组件受影响</li>"
                result_html = f"""
                <h2>{'上游（谁依赖我）' if direction == 'up' else '下游（我依赖谁）'} 遍历</h2>
                <div class="panel graph"><pre>{esc(lines)}</pre></div>
                {satisfy_html}
                <h2>影响面分析</h2>
                <div class="panel"><p>受影响组件总数：<b>{impact['impacted_count']}</b>，
                其中公司关注：<b>{impact['watched_count']}</b></p>
                <ul>{watched_impact}</ul></div>"""
        body = f"""
        <h1>依赖图</h1>
        <form class="formrow" method=get action="/graph">
          <input name=key placeholder="包 key，如 npm:chalk-like" value="{esc(key)}" style="min-width:320px">
          <select name=dir>
            <option value=down {'selected' if direction=='down' else ''}>下游（我依赖谁）</option>
            <option value=up {'selected' if direction=='up' else ''}>上游（谁依赖我）</option>
          </select>
          <input name=spec placeholder="可选：版本范围，如 ^1.0.0 / [1.0,2.0)" value="{esc(spec)}">
          <button class=btn>查询</button>
        </form>
        <p class=muted>版本范围按各生态语义计算：npm semver、PyPI PEP 440、Maven range。</p>
        {result_html}"""
        self._html(200, page_shell("依赖图", body, "/graph"))

    def _render_ascii_tree(self, walk: dict, direction: str) -> str:
        by_level: dict[int, list[str]] = {}
        for n in walk["nodes"]:
            by_level.setdefault(n["level"], []).append(n["key"])
        lines = []
        for level in sorted(by_level):
            indent = "  " * level
            for k in by_level[level]:
                mark = "●" if level == 0 else ("→" if direction == "down" else "←")
                star = " ⭐" if self.db.is_watched(k) else ""
                lines.append(f"{indent}{mark} {k}{star}")
        return "\n".join(lines)

    # ---------- 告警 ----------

    def _page_alerts(self, qs: dict) -> None:
        status_filter = qs.get("status", ["open"])[0]
        type_filter = qs.get("type", [""])[0]
        alerts = self.db.list_alerts(
            status=None if status_filter == "all" else status_filter, limit=500)
        if type_filter:
            alerts = [a for a in alerts if a["type"] == type_filter]
        rows = []
        for a in alerts:
            star = "⭐" if a["watched"] else ""
            ack = ("" if a["status"] == "ack" else
                   f"<form method=post action='/alerts/ack' style='display:inline'>"
                   f"<input type=hidden name=id value='{a['id']}'>"
                   f"<button class='btn secondary'>确认</button></form>")
            rows.append(
                f"<tr><td><span class='badge {esc(a['severity'])}'>{esc(a['severity'])}</span>{star}</td>"
                f"<td>{esc(self._alert_type_label(a['type']))}</td>"
                f"<td><span class='badge eco'>{esc(a['ecosystem'])}</span></td>"
                f"<td class=mono>{esc(a['package'])}{('@'+esc(a['version'])) if a['version'] else ''}</td>"
                f"<td>{esc(a['title'])}</td><td class=muted>{esc(a['created_at'])}</td>"
                f"<td>{esc(a['status'])}</td><td>{ack}</td></tr>")
        watches = "".join(
            f"<tr><td class=mono>{esc(w['key'])}</td><td>{esc(w['component'])}</td>"
            f"<td class=muted>{esc(w['note'])}</td><td class=muted>{esc(w['added_at'])}</td></tr>"
            for w in self.db.list_watchlist()) or "<tr><td colspan=4 class=muted>暂无关注</td></tr>"
        body = f"""
        <h1>变更告警</h1>
        <form class="formrow" method=get action="/alerts">
          <select name=status>
            {''.join(f"<option value={v} {'selected' if v==status_filter else ''}>{l}</option>"
                     for v, l in (('open','未处理'),('ack','已确认'),('all','全部')))}
          </select>
          <select name=type>
            <option value="">全部类型</option>
            {''.join(f"<option value={t} {'selected' if t==type_filter else ''}>{self._alert_type_label(t)}</option>"
                     for t in ('license_change','version_yanked','new_dependency','new_version'))}
          </select>
          <button class=btn>筛选</button>
        </form>
        <div class="panel"><table><thead><tr><th>级别</th><th>类型</th><th>生态</th><th>对象</th>
        <th>内容</th><th>时间</th><th>状态</th><th></th></tr></thead>
        <tbody>{''.join(rows) or '<tr><td colspan=8 class=muted>无告警</td></tr>'}</tbody></table></div>
        <h2>公司关注名单（告警优先级自动提升）</h2>
        <div class="panel">
          <form method=post action="/watch/add" class="formrow">
            <select name=ecosystem>
              <option>npm</option><option>pypi</option><option>maven</option>
            </select>
            <input name=name placeholder="包名（规范名）" required>
            <input name=component placeholder="所属公司组件">
            <input name=note placeholder="备注">
            <button class=btn>加入关注</button>
          </form>
          <table><thead><tr><th>包 key</th><th>组件</th><th>备注</th><th>加入时间</th></tr></thead>
          <tbody>{watches}</tbody></table>
        </div>"""
        self._html(200, page_shell("变更告警", body, "/alerts"))

    def _ack_alert(self, p: dict) -> None:
        from ..common.timeutil import now_iso
        with self.db.tx() as c:
            c.execute("UPDATE alerts SET status='ack', updated_at=? WHERE id=?",
                      (now_iso(), int(p["id"])))
        self._redirect("/alerts")

    # ---------- 工具 ----------

    def _stats(self) -> dict:
        with self.db._lock:
            open_alerts = self.db.conn.execute(
                "SELECT COUNT(*) n FROM alerts WHERE status='open'").fetchone()["n"]
            watched = self.db.conn.execute(
                "SELECT COUNT(*) n FROM watchlist").fetchone()["n"]
            fixed = self.db.conn.execute(
                "SELECT COALESCE(SUM(packages_fixed),0) n FROM verification_runs").fetchone()["n"]
        return {
            "packages": self.db.count_packages(),
            "versions": self.db.count_versions(),
            "edges": len(self.db.all_edges()),
            "open_alerts": open_alerts,
            "watched": watched,
            "verified_fixed": fixed,
        }

    @staticmethod
    def _card(label: str, value, bad: bool = False) -> str:
        style = "color:var(--bad)" if bad else ""
        return (f"<div class='card'><div class='lbl'>{esc(label)}</div>"
                f"<div class='num' style='{style}'>{esc(value)}</div></div>")

    @staticmethod
    def _pager(base: str, page: int, pages: int, params: dict) -> str:
        def link(p: int, label: str) -> str:
            q = dict(params); q["page"] = p
            qs = "&".join(f"{k}={quote(str(v))}" for k, v in q.items() if v)
            return f"<a class='btn secondary' href='{base}?{qs}'>{label}</a>"
        parts = []
        if page > 1:
            parts.append(link(page - 1, "← 上一页"))
        parts.append(f"<span class='muted'>{page} / {pages}</span>")
        if page < pages:
            parts.append(link(page + 1, "下一页 →"))
        return f"<div class='pager'>{''.join(parts)}</div>"

    @staticmethod
    def _alert_type_label(t: str) -> str:
        return {"license_change": "许可证变化", "version_yanked": "版本撤回",
                "new_dependency": "新增依赖", "new_version": "新版本发布"}.get(t, t)

    # ---------- 底层 ----------

    def _html(self, status: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], db):
        super().__init__(addr, WebHandler)
        self.db = db


def start_web(db, host: str = "127.0.0.1", port: int = 8080) -> WebServer:
    server = WebServer((host, port), db)
    import threading
    threading.Thread(target=server.serve_forever, name="web-console",
                     daemon=True).start()
    return server
