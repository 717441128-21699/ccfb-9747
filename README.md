# 软件供应链合规监测系统（本地仿真版）

监测公司使用的开源包：哪个包换了许可证、哪个版本被撤回、依赖里混进了什么新东西。
全部组件在本机运行，**不访问任何真实包仓库**——npm / PyPI / Maven 中央仓库由三个
高保真仿真服务扮演，采集端像对接真实仓库一样走 HTTP 解析它们。

- 纯 Python 标准库实现，**零第三方依赖**（Python 3.10+，开发环境为 3.14）
- 存储：SQLite（WAL 模式）
- 控制台：标准库 `http.server` 服务端渲染，五个页面
- 验收：内置与仿真真值的对账器，端到端差异为 0

---

## 1. 快速开始

```powershell
# 一条命令拉起：三个仿真仓库 + 采集引擎 + Web 控制台
py -I run.py demo
```

启动后访问 **http://127.0.0.1:8080** 。仿真仓库每隔几秒制造一次变更（发新版本、
改许可证、撤回版本），其中约 15% 的事件会故意从变更流里“抹掉”，用来考验定期校验补漏。

分步骤运行（便于观察各通道）：

```powershell
py -I run.py reset       # 清空 data/，重新开始
py -I run.py sim         # 终端 A：只启动三个仿真仓库 + 变异器
py -I run.py init-db     # 终端 B：建库、写仓库配置与关注名单
py -I run.py crawl       # 全量爬取（可重复，断点续跑）
py -I run.py feed        # 消费一轮变更订阅流
py -I run.py verify      # 跑一轮定期校验补漏
py -I run.py engine      # 常驻引擎：全量 → 增量 → 周期校验
py -I run.py web         # 终端 C：Web 控制台（默认 8080）
py -I run.py reconcile   # 与仿真真值对账，打印零差异报告
```

运行全部自动化测试（36 个用例，含强杀恢复）：

```powershell
py -I run_tests.py
```

---

## 2. 系统架构

```
┌──────────────────────────── 本机（127.0.0.1）────────────────────────────┐
│                                                                          │
│  仿真层 sim/                    采集层 collector/          展示层 web/     │
│  ┌──────────────┐  HTTP         ┌──────────────────┐      ┌───────────┐  │
│  │ npm 风格     │◄──────────────│ FullCrawler 全量  │      │ 总览      │  │
│  │  :8101       │  ETag/304     │ FeedConsumer 增量 │──►SQLite◄── 仓库配置│  │
│  ├──────────────┤  429/退避     │ Verifier 校验     │      │ 采集任务  │  │
│  │ PyPI 风格    │◄──────────────┤ RepoClient        │      │ 包库      │  │
│  │  :8102       │  /feed 变更流 │ （令牌桶感知/重试）│      │ 依赖图    │  │
│  ├──────────────┤  /__ground_   └──────────────────┘      │ 变更告警  │  │
│  │ Maven 风格   │   truth 真值◄──── reconcile/ 对账器（仅验收用）         │  │
│  │  :8103       │                                                         │  │
│  └──────────────┘  mutator 变异器：新版本/许可证变更/撤回/元数据编辑       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 模块清单

| 路径 | 职责 |
| --- | --- |
| `sim/datagen.py` | 带种子的确定性测试数据生成（三生态各自的命名/版本/依赖写法） |
| `sim/state.py` | 仿真真值、只增事件流、ETag 版本号、变异器、落盘恢复 |
| `sim/server.py` | 仿真 HTTP 服务：分页、条件请求、限流、故障注入、变更流 |
| `sim/serializers.py` | npm packument / PyPI JSON / Maven maven-metadata+POM 原生格式 |
| `sim/mutator.py` | 后台变异线程，周期性制造四类变更并持久化真值 |
| `sim/cluster.py` | 三仓库编排，独立端口/线程，单仓库故障不波及其他 |
| `collector/httpclient.py` | 条件请求、429/5xx 指数退避重试、**外网主机硬拦截** |
| `collector/fetcher.py` | 三生态抓取器（清单分页 + 详情条件请求） |
| `collector/parsers.py` | 原生响应 → 统一模型，许可证在此归一 SPDX |
| `collector/crawler.py` | 全量通道：游标分页、断点续跑、UPSERT 幂等 |
| `collector/feed_consumer.py` | 增量通道：变更流消费、位点事务、事件去重 |
| `collector/verifier.py` | 校验通道：全量比对补漏，feed 丢事件的最终防线 |
| `collector/alerts.py` | 许可证/撤回/新依赖/新版本告警，关注包自动提级 |
| `collector/engine.py` | 每仓库一个工作线程，三通道调度 + 故障隔离退避 |
| `collector/storage.py` | SQLite schema、UPSERT、位点、告警指纹去重 |
| `collector/graph.py` | 上下游 BFS、版本范围解析、影响面分析 |
| `versions/` | semver / PEP 440 / Maven 三套独立版本语义实现 |
| `common/licenses.py` | 杂乱许可证写法 → SPDX 标识（含复合表达式） |
| `reconcile/checker.py` | 拉真值端点逐字段对账 |
| `web/server.py` | 五页面控制台 + 故障注入/关注名单/告警确认操作 |

---

## 3. 七项需求的实现方式

### 3.1 全量爬完、增量订阅、断点续传、定期校验补漏

三条独立通道，进度全部存 SQLite：

- **全量** `crawl_cursors` 记录下一页页码；进程被杀重启后从该页继续。
  包写入是 `INSERT ... ON CONFLICT DO UPDATE`，重复抓取不产生重复行。
- **增量** 消费 `/feed?after_id=` 分页流，位点 `feed_checkpoints.last_event_id`
  与业务数据在同一事务里推进；`events_log` 唯一约束保证重复投递幂等。
  订阅中断（仓库下线）时保留位点，退避后下轮自动续上。
- **校验** 不依赖事件流，重新分页拉全量清单 + 逐包条件请求，专门兜底
  “流里漏了的”事件。仿真器会主动把一部分事件标记为 feed 不可见，
  测试 `test_feed_gap_filled_by_verifier` 验证了：只跑增量会有差异，
  校验跑完后对账归零。

### 3.2 ETag / Last-Modified 省流量

- 每个包对象维护单调 `rev` + 内容哈希，形成强校验 ETag；
  响应同时带 `Last-Modified`（取对象级最后变更时间，yank/改许可证也会推进）。
- 采集端存每个包上次的 ETag，请求带 `If-None-Match`，未变化返回 **304 不下载正文**。
- 条件请求严格按 RFC 7232 求值：客户端发了 `If-None-Match` 就只认 ETag 精确匹配，
  绝不回退到日期判断（否则“不产生新版本的 yank 事件”会被日期短路漏掉——这是开发中
  实测发现并修复的坑，见 `tests/test_sim_features.py`）。
- `test_second_crawl_uses_304` 验证第二轮全量爬取 304 命中且处理数为 0。

### 3.3 统一元数据模型 + SPDX 归一

- 三生态解析后统一为 `common/model.py` 的 `Package / PackageVersion / Dependency`，
  全局键 `ecosystem:规范名`（npm 小写、PyPI PEP 503 连字符归一、Maven `group:artifact` 小写）。
- 许可证经 `common/licenses.py` 归一：内置 50+ 别名（`GPLv3`→`GPL-3.0-only`、
  `ASL 2.0`→`Apache-2.0` …），支持 `(MIT OR Apache-2.0) AND ...` 复合表达式递归归一，
  无法识别的保留为 `LicenseRef-Unknown:<原值>`，绝不静默吞掉。
- 原始响应完整存 `raw_json` 便于取证。

### 3.4 依赖图与各生态版本范围语义

`versions/` 是三套**独立实现**（不是简化的通用比较器）：

- **npm semver**：`^`/`~`、连字符范围、x 通配、`||`、预发版门禁规则
- **PEP 440**：epoch、`a/b/rc/post/dev` 排序、`~=` 兼容版本、`==1.2.*` 通配、
  预发版默认排除（说明符自带预发时隐式放行，同 pip）
- **Maven**：`[1.0,2.0)` 开闭区间、`[1.0]` 精确、裸版本软需求、
  alpha/beta/milestone/rc/snapshot/空/sp 限定符排序、数字段缺位补 0

控制台“依赖图”页可对任意包做上下游 BFS 遍历、按生态语义解析某条依赖范围
**具体命中哪些版本**、以及某包出问题时的受影响面（沿入边找到所有组件并标注关注名单）。

### 3.5 告警与优先级

四类告警（`collector/alerts.py`）：

| 类型 | 触发条件 | 默认级别 |
| --- | --- | --- |
| `license_change` | 同版本 SPDX 发生变化 | high |
| `version_yanked` | 版本 yanked 由 false 变 true | high |
| `new_dependency` | 新版本相对上一版本新增依赖 | medium |
| `new_version` | 发现新版本 | info |

关注名单（watchlist）中的包，告警严重度**自动提升一级**（info→low … high→critical），
告警带 ⭐ 标识。告警按内容指纹去重，同一变更不会重复告警；控制台可单条确认（ack）。

### 3.6 限流退避与故障隔离

- 仿真仓库内置令牌桶，超额返回 `429 + Retry-After`。
- 客户端：429 按 `Retry-After` 等待，5xx/连接错误走**指数退避 + 全抖动**，
  重试预算耗尽抛 `RepoUnavailable`；4xx（如 404）不重试。
- 引擎为每个仓库开独立工作线程，一个仓库持续失败只让该线程进入退避，
  其他仓库的爬取/消费/校验不受影响（`tests/test_sim_features.py` 与
  `tests/test_pipeline.py` 覆盖下线、随机 5xx、恢复后追平）。
- 控制台“仓库配置”页可一键注入故障（下线 / 50% 报错 / 恢复）。

### 3.7 进程被杀后重启不重不漏

- SQLite WAL + 全部写操作走事务：已提交不丢、未提交回滚。
- 所有进度（全量页码、feed 位点、校验记录）都在库里，工作线程本身无状态。
- 包/版本 UPSERT 幂等 + 事件唯一约束 + 告警指纹去重，重放不会产生重复数据。
- `tests/test_crash_recovery.py` 用真实子进程验证：引擎爬到一半
  `taskkill /F /T` 强杀（等价 kill -9），重启后自动续跑，最终
  包数/版本数与真值完全一致、零重复行、对账零差异。

---

## 4. 真值对账（验收标准）

仿真仓库暴露 `GET /__ground_truth`（采集业务流程禁止调用，仅对账器使用），
返回完整真值。`reconcile/checker.py` 逐字段比对：

1. 包集合（缺失包 / 多余包）
2. 每包版本集合与撤回状态、撤回原因
3. 许可证：上游原始值经 SPDX 归一后与本地一致
4. 逐版本依赖（名字、版本范围、optional 标记）
5. 制品校验和（sha256）

```powershell
py -I run.py reconcile --json
# 退出码 0 = 零差异；非 0 = 存在差异，报告列出每一处不同
```

确定性保证：测试数据由带固定种子的 `random.Random` 生成，同种子产出完全相同的数据。

---

## 5. 安全边界

- `RepoClient._validate_url` 硬编码只允许 `127.0.0.1 / localhost / 0.0.0.0`，
  任何外部主机请求直接抛异常——真实仓库一律不碰（有单元测试守护）。
- Web 控制台默认只绑定 `127.0.0.1`。
- 所有 HTML 输出经 `html.escape` 转义。

---

## 6. 数据目录

运行后生成在 `./data/`：

```
data/
├── repos.json              # 仿真仓库地址与限流配置
├── sbom.db                 # SQLite 主库（+ .db-wal / .db-shm）
└── sim/
    ├── sim-npm.json        # 各仿真仓库的持久化真值与事件流
    ├── sim-pypi.json
    └── sim-maven.json
```

`py run.py reset` 可一键清空重来。
