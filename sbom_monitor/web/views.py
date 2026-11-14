"""控制台共用：页面骨架、导航、简单转义。"""
from __future__ import annotations

import html

NAV_ITEMS = [
    ("/", "总览"),
    ("/repos", "仓库配置"),
    ("/tasks", "采集任务"),
    ("/packages", "包库"),
    ("/graph", "依赖图"),
    ("/alerts", "变更告警"),
]


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page_shell(title: str, body: str, active: str = "") -> str:
    nav = []
    for href, label in NAV_ITEMS:
        cls = ' class="active"' if href == active else ""
        nav.append(f'<a href="{href}"{cls}>{label}</a>')
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 供应链合规监测</title>
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">🛡️ 供应链合规监测 <span class="sub">本地仿真 · 零外部访问</span></div>
  <nav>{''.join(nav)}</nav>
</header>
<main class="container">
{body}
</main>
<footer class="foot">数据仅来自本机仿真仓库（npm/PyPI/Maven 风格），不访问任何真实仓库</footer>
</body>
</html>"""


CSS = """
:root{
  --bg:#0f1420; --panel:#171e2e; --panel2:#1e2740; --line:#2a3550;
  --text:#e6ebf5; --muted:#8b97ad; --accent:#4f8cff; --accent2:#7c5cff;
  --ok:#2fbf71; --warn:#f5a623; --bad:#ff5d5d; --info:#56b6ff;
}
@media (prefers-color-scheme: light){
  :root{ --bg:#f4f6fb; --panel:#ffffff; --panel2:#f0f3fa; --line:#dce2ee;
    --text:#1c2434; --muted:#64708a; --accent:#2563eb; --accent2:#6d45e8;
    --ok:#128a52; --warn:#c47a08; --bad:#d23b3b; --info:#0b6bcb; }
}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  background:var(--bg);color:var(--text);font-size:14px}
.topbar{display:flex;align-items:center;gap:28px;padding:0 24px;height:60px;
  background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;white-space:nowrap}
.brand .sub{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:500}
nav a:hover{background:var(--panel2);color:var(--text)}
nav a.active{background:var(--accent);color:#fff}
.container{max-width:1280px;margin:0 auto;padding:24px}
.foot{text-align:center;color:var(--muted);padding:24px;font-size:12px}
h1{font-size:20px;margin:0 0 18px} h2{font-size:16px;margin:24px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
.card .num{font-size:28px;font-weight:700;margin-top:6px}
.card .lbl{color:var(--muted);font-size:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr:hover td{background:var(--panel2)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.badge.high,.badge.critical{background:rgba(255,93,93,.16);color:var(--bad)}
.badge.medium{background:rgba(245,166,35,.16);color:var(--warn)}
.badge.low,.badge.info{background:rgba(86,182,255,.16);color:var(--info)}
.badge.ok{background:rgba(47,191,113,.16);color:var(--ok)}
.badge.eco{background:var(--panel2);color:var(--muted);border:1px solid var(--line)}
.muted{color:var(--muted)}
code,.mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:12px}
.btn{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:8px;
  padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none}
.btn.secondary{background:var(--panel2);color:var(--text);border:1px solid var(--line)}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  border-radius:8px;padding:8px 10px;font-size:13px}
.formrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.pager{display:flex;gap:8px;align-items:center;margin-top:14px}
.tag{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.green{background:var(--ok)} .dot.red{background:var(--bad)} .dot.amber{background:var(--warn)}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12px}
.graph{font-family:Consolas,monospace;font-size:12px;line-height:1.7}
"""
