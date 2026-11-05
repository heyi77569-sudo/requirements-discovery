# 需求痛点发现系统 — 整体架构设计

> 版本：v2.1
> 日期：2026-11-05
> 状态：设计定稿 + 自评待审（尚未进入编码）
> 取代：v1.0（Scrapy 接入方式、层命名、存储选型、归一化位置均有变更，差异见第 12 章）

---

## 目录

1. [项目定位](#1-项目定位)
2. [系统本质与价值锚点](#2-系统本质与价值锚点)
3. [整体架构](#3-整体架构)
4. [核心数据流](#4-核心数据流)
5. [分层详细设计](#5-分层详细设计)
6. [数据模型设计](#6-数据模型设计) — 字段级定义见 [SCHEMA.md](./SCHEMA.md)
7. [LLM 集成设计](#7-llm-集成设计)
8. [实施路线](#8-实施路线)
9. [关键风险与难点](#9-关键风险与难点)
10. [目录结构与代码契约](#10-目录结构与代码契约)
11. [待确认事项](#11-待确认事项)
12. [相对 v1.0 的变更](#12-相对-v10-的变更)
13. [架构自评](#13-架构自评2026-11-03) — ★ **16 项待修问题，先看这个**
14. [配套文档](#14-配套文档)

---

## 1. 项目定位

### 1.1 一句话描述

从多个内容平台的真实用户发言中，**发现高价值的用户痛点信号**，并通过 LLM 转化为**可执行的产品机会假设（product idea）**。

### 1.2 目标用户

| 角色 | 诉求 | 使用方式 |
|---|---|---|
| 产品经理 / 创业者 | 找真实需求，而不是拍脑袋 | 浏览按痛点归并的 idea 榜单（Markdown/CSV） |
| 产品运营 | 验证需求真实性 | 追溯每个 idea 的原始帖子与热度 |
| 竞品分析 | 找市场空白 | 按痛点类型/人群维度筛选 |

### 1.3 非目标

- ❌ 不做通用的舆情情感分析系统
- ❌ 不做全网内容采集/大数据平台
- ❌ 不做替代产品经理的自动决策系统（输出是**假设**，不是结论）
- ❌ 不做实时流式分析（按批处理周期运行）
- ❌ 不做前端看板（产出以 Markdown/CSV 交付）

---

## 2. 系统本质与价值锚点

### 2.1 三个关键动作

```
采集 → 筛选 → 转化
  ↓       ↓       ↓
原料    降噪     决策依据
```

1. **采集（Collect）** — 从各平台抓取用户的抱怨、求助、吐槽、询价等原始内容。
2. **筛选（Filter）** — 用规则 + 模型打分，把 90% 的噪音过滤掉。
3. **转化（Convert）** — 把高分痛点归并后喂给 LLM，生成结构化的 `product_idea`。

### 2.2 价值锚点

> **本系统不是"数据采集器"，而是"痛点 → 可执行产品需求"的转化器。**

爬虫只是原料入口，**评分体系**和**LLM 转化质量**才是真正的壁垒。

### 2.3 主路线：LLM 直接产出机会

信号不经聚类，直接送 LLM 归纳出产品机会：

```
A/B 级信号池 → LLM 归并 + 生成 → product_idea → 回挂信号
```

**为什么这样设计**

1. 系统的价值锚点是"idea 质量"，不是"主题划分质量"。LLM 直接看信号集，能归纳出"这三个不同行业的抱怨其实是同一个预算审批流程问题"——这正是本系统想要的产出。
2. 起步阶段信号量小（Phase 1 只有 GitHub + 小红书两个源），任何硬性的簇大小阈值都会直接卡死。LLM 归并没有最小簇约束。
3. 依赖少、成本优：只需 LLM，无额外向量服务；且只在信号量达到阈值时才调一次 LLM。

**已知代价**

- 主题边界依赖 LLM 判断，非完全可复现 → 用 `validator.py` 做输出校验 + `source_signal_ids` 溯源
- 需要二次校验防止"同质伪需求" → 校验规则见 5.3.5

> **实现约束**：系统不引入任何向量/聚类基础设施。`signal` 表无 `embedding` 列，`storage` 只用标准 SQLite，无扩展依赖。
>
> 若将来真出现"idea 重复"的问题，届时作为**独立的召回层**引入相似度召回——那是新需求下的新决策，不在当前架构预留位置。

---

## 3. 整体架构

### 3.1 分层图

```
┌──────────────────────────────────────────────────────┐
│  scheduler/    定时调度 · 错峰 · 全局限流 · 失败告警     │
├──────────────────────────────────────────────────────┤
│  workflow/     编排层：阶段编排 · 幂等控制 · 断点续跑     │
│                collect → clean → score → signal →      │
│                cluster → generate                      │
├──────────────────────────────────────────────────────┤
│  crawler/      采集层：Scrapy 独立进程 · 平台原始结构     │
│                xiaohongshu / xianyu / github / ...     │
├──────────────────────────────────────────────────────┤
│  engine/       核心发现层（★核心价值）                 │
│                cleaner → scorer(A~E) → signal →       │
│                cluster → idea_generator                │
├──────────────────────────────────────────────────────┤
│  storage/      存储层：raw_posts / signal /            │
│                product_idea / signal_idea / cursor /    │
│                run_log / llm_usage / keyword_seed       │
├──────────────────────────────────────────────────────┤
│  config/       配置层：全局 · 平台 · 评分 · Prompt · DB │
└──────────────────────────────────────────────────────┘
```

### 3.2 依赖规则（硬约束）

```
scheduler  →  workflow  →  crawler
                            ↓
                          engine
                            ↓
                          storage
                            ↓
                          config

       core（纯函数，无依赖，被 crawler / engine 共用）
```

- **严格单向向下**，不允许反向依赖
- `config` 被所有层依赖，本身不依赖任何层
- `crawler` 负责"把数据拿进来并转成统一字段"，**不做任何业务判断**（不判断这是不是痛点）
- `engine` 不关心数据来自哪个平台（平台差异在 crawler 层已被抹平）
- `storage` 只做 CRUD，不含业务规则
- **`core` 是例外**：它不依赖任何层，只被依赖。内容归一化规则（`content_hash` 的算法）必须放在这里——spider 算 hash、cleaner 做清洗，两边共用一份，否则 `crawler` 会反向依赖 `engine`。见自评 P2

> **注意 `crawler` 与 `engine` 的关系是并行的，不是串行的。**
> `crawler` 写入 `raw_posts`（统一字段 + 原始 payload），`engine` 从 `raw_posts` 读出做**跨平台通用清洗**。两者通过 `storage` 解耦，没有直接调用关系。

### 3.3 各层职责一览

| 层 | 职责 | 不负责 |
|---|---|---|
| `config` | 配置加载、校验、Prompt 模板管理 | 业务逻辑 |
| `crawler` | 平台数据采集、反爬对抗、**平台内归一化**、原始 payload 落库 | 跨平台去重、噪音判断、打分 |
| `engine` | **跨平台通用清洗**、去重、噪音过滤、打分、主题归并、idea 生成 | 网络请求细节、平台字段映射 |
| `storage` | 数据读写、schema、索引 | 业务规则 |
| `workflow` | 阶段编排、幂等控制、错误恢复 | 具体业务逻辑 |
| `scheduler` | 触发时机、错峰、全局配额 | 业务逻辑 |

> **平台差异 vs 通用规则的分界**
>
> | 归属 | 内容 |
> |---|---|
> | **crawler（平台差异）** | 这个平台的数据怎么拿到（Playwright / JSON API / 签名）· 平台字段叫什么、怎么抽、类型怎么转、缺失怎么办 |
> | **engine（通用规则）** | 这条内容是不是垃圾（广告/招聘/带货）· 是否与其他平台重复（`content_hash`）· 长度/表情/水印清洗 |
>
> **为什么归一化在 crawler 层**：平台采集方式差异极大——JSON API 平台靠字段路径，Playwright 平台靠 DOM 选择器。**两者的提取逻辑根本不同**，无法统一到配置或清洗层。因此"从平台原始形态提取统一字段"必须由各平台自己的 spider 完成。
>
> 详见 5.2.2。

---

## 4. 核心数据流

### 4.1 完整链路

以关键词 "A" 为例的一次完整运行：

```
       ┌──────────────┐
       │  关键词 "A"   │  ← config/platforms/*.yaml 种子词
       │  + 平台       │    或 signal/idea 反哺的关键词
       └──────┬───────┘
              │  ① 采集（Scrapy 独立进程）+ 平台内归一
              │     每个平台的 spider 把原始形态转成统一字段
              │     原始 JSON 解析完即丢弃，不落库（见 5.2.2.3）
              ▼
       ┌──────────────┐
       │  ★ 采集层过滤 │  写库前执行，不通过不落库
       │              │  · 互动量门槛（点赞够 **或** 评论够）
       │              │  · 长度阈值（标题 / 正文）
       │              │  · 短语过滤（Tier1 命中 / Tier2 命中 + 佐证）
       │              │  · 噪音词过滤（广告/招聘/教程/带货）
       │              │  ★ 存储量的主要闸门在这里
       └──────┬───────┘
              ▼
       ┌──────────────┐
       │  raw_posts   │  原始层（统一字段，无原始 payload）
       │              │  ※ 落库时已通过采集层全部过滤
       └──────┬───────┘
              │  ② 清洗（engine/cleaner.py）—— 只做归一与去重
              │     · 文本规范化（core/normalize.py，与 spider 共用同一份规则）
              │     · 跨平台去重（按 content_hash 分组取代表）
              │     · 表情/水印/全角半角清洗
              │     ※ 不做噪音过滤——已在采集层拦过
              ▼
       ┌──────────────┐
       │  LLM 分级     │  A / B / C / D / E
       │              │  落库数据全部送 LLM，一次定级
       │              │  ※ 无中间规则召回层
              └──────┬───────┘
              ▼
       ┌──────────────┐
       │   signal     │  ★ 信号层（只入 A/B/C；D/E 仅审计留痕）
              │         │  ★ 字段：source / source_channel / title /
              │         │    num_comments / body / score / url /
              │         │    triage_grade
              ▼
       ┌──────────────┐
       │  主题归并     │  LLM 归并
              │         │  门槛：≥N 条未归并信号
              ▼
       ┌──────────────┐
       │ product_idea │  ★ 产品机会层（带原文溯源）
              │         │  + 六维评分 + 框架标记 + 分类
              ▼
       ┌──────────────┐
       │  导出         │  Markdown / CSV
              └──────────────┘
```

> **采集层过滤是存储量的主要闸门。** 不通过采集层硬门槛、短语过滤与噪音词过滤的内容**连 `raw_posts` 都进不去**——采集量会随关键词反哺增长，而存储只增不减。详见 5.2.5。
>
> **过滤全部前置到采集层**（2026-11-05 决策）：噪音词过滤原本在清洗层，现已前移。落库的每一行都已经是"值得送 LLM 分级"的候选，因此**打分阶段直接调 LLM 定级**，不再有中间的规则召回/灰区层。代价是 LLM 调用量上升（全部落库数据都要过 LLM），换来的是链路更短、规则只有一处、阈值更好调。详见 5.3.2。

### 4.2 `signal` 表是中间枢纽

- **可追溯**：每个 idea 都能回溯到原始帖子（`source_signal_ids`）
- **可干预**：PM 手动把某条 signal 提升为 A 级，立即触发 idea 生成
- **可度量**：统计"本月高信号量""某痛点趋势上升"，为打分权重提供反馈数据

### 4.3 数据流设计原则

| 原则 | 说明 |
|---|---|
| **原始层不可变** | `raw_posts` 只写不改，保证可重放、可回溯 |
| **处理幂等** | 同一条 raw 数据重复处理结果一致（依赖 `content_hash` 去重） |
| **分层过滤** | 每层只做一件事，过滤成本从低到高排列（规则 → 模型 → LLM） |
| **溯源强制** | 每个 idea 必须携带 `source_signal_ids`，否则视为无效输出丢弃 |
| **先写数据后推进游标** | 游标推进必须在 `raw_posts` 写入成功之后，否则写库失败会永久丢数据 |

---

## 5. 分层详细设计

### 5.1 config/ — 配置层

```
config/
├── settings.py            # 全局：DB、LLM provider、并发数、限速、超时
├── loader.py              # 统一加载 + Pydantic 校验 + 热重载
├── platforms/             # 每个平台一个 yaml
│   ├── xiaohongshu.yaml
│   ├── xianyu.yaml
│   ├── github.yaml
│   ├── douyin.yaml
│   └── channels.yaml
├── phrases/               # ★ 采集层短语规则集（按语言分目录）
│   ├── en/{tier1,tier2,price_signals,tool_signals}.yaml
│   └── zh/{tier1,tier2,price_signals,tool_signals}.yaml
├── keywords.yaml          # ★ 种子关键词库 + 反哺关键词（Phase 4）
├── scoring.yaml           # ★ 六维评分排序公式 + 框架标记倍数 + 入库门槛
├── llm.yaml               # provider 配置、模型、temperature、预算上限
├── workflow.yaml          # 阶段顺序、批大小、重试策略、调度 cron
└── prompts/               # ★ LLM Prompt 模板（独立文件，可版本化）
    ├── triage.md              # A~E 分级（我们自己改写的定义）
    ├── triage.reference.md    # 参考实现原文（留档，不使用）
    └── idea_generation.md     # LENS 1-5 归并 + 生成
```

#### 5.1.1 平台配置项示例

```yaml
# config/platforms/xiaohongshu.yaml
name: xiaohongshu
enabled: true
priority: 3

# ★ 语言 —— 决定加载哪套短语规则集 config/phrases/{lang}/
lang: zh                 # zh | en

collect:
  # Scrapy 独立进程运行，由 workflow 的 collect 阶段触发
  spider: xiaohongshu
  mode: search              # search | list | detail
  base_url: "https://www.xiaohongshu.com"
  page_size: 20
  max_pages: 10
  max_results_per_run: 500
  keyword_source: config    # config | feedback（signal/idea 反哺）
  # 搜索方式 —— 见 5.1.3
  keywords: [记账, 对账, 报销, 开票]
  fetch:
    method: search_api      # search_api | search_html | feed | detail
    keyword_strategy: list  # list | feedback（反哺关键词）

anti_bot:
  user_agents: ${UA_POOL}
  proxy_pool: ${PROXY_POOL}
  rate_limit:
    requests_per_minute: 12
    delay_between_requests: [2000, 5000]   # ms 随机区间
  login_state_file: "./state/xhs_cookies.json"

# ★ 采集层硬门槛 —— 写库前执行，不满足直接丢弃（见 5.3.2 ①）
#   三条规则全部是 OR 关系，满足任一条即通过
filter:
  min_likes: 5            # 互动量门槛：likes>=5 或 comments>=3
  min_comments: 3
  title_length: [3, 200]  # 标题太短无信息，太长是长文
  min_body_length: 15     # 正文太短多半是标题党
  # 短语过滤见 5.2.5，规则集由 lang 决定，此处只开关
  phrase_filter: true

# ★ 互动量权重 —— 互动量阈值按平台配置，因各平台指标不齐
#   （抖音/闲鱼/GitHub 无收藏数，微博是 attitudes 而非 likes）
engagement:
  base: 30            # 该平台"有热度"的基础分
  likes: 1            # 点赞权重
  comments: 3         # 评论权重（求助/吐槽特征更强）
  collects: 2         # 收藏权重（部分平台无此指标）

# ★ 游标 —— 语义由该平台的 spider 解释（payload 存什么、怎么比，全在代码里）
#   这里只配"防漏采"的通用冗余量，与游标结构无关
cursor:
  lookback_window: 300   # 水位线往前回退多少（秒）。0 = 不回退
```

**GitHub 的对照配置**（同结构，差异只在 `lang` 与阈值）：

```yaml
# config/platforms/github.yaml
name: github
enabled: true
priority: 3
lang: en                 # ← 加载 config/phrases/en/

collect:
  spider: github
  mode: search
  base_url: "https://api.github.com"
  page_size: 50
  max_pages: 5
  max_results_per_run: 500
  keyword_source: config
  keywords: [feature request, "is there a tool for", alternatives, annoying]
  fetch:
    method: search_api
    keyword_strategy: list

filter:
  min_likes: 5
  min_comments: 3
  title_length: [10, 200]  # issue 标题短，没那么多可选
  min_body_length: 30     # issue 正文通常更长
  phrase_filter: true

engagement:
  base: 20
  likes: 1
  comments: 3
  collects: 0             # GitHub 无收藏

cursor:
  lookback_window: 0     # GitHub issue number 单调递增，不会漏采，无需回退
```

#### 5.1.2 关键设计点

- **配置即代码**：所有可变项外置到 yaml/环境变量，代码不硬编码任何平台细节
- **Prompt 独立文件**：prompt 是核心资产，必须能版本化、A/B 对比、独立迭代
- **校验前置**：`loader.py` 用 Pydantic 校验配置，错误在启动时暴露而非运行时崩
- **平台差异不进配置**：字段映射/提取逻辑写在各平台 spider 里（见 5.2.2 的理由）；配置只放**跨平台通用的参数**（限速、互动量权重、游标类型、过滤阈值）

---

### 5.2 crawler/ — 采集层

> **核心决策：Scrapy 作为独立进程运行，结果直接写入 `raw_posts` 表。**

#### 5.2.1 为什么是独立进程（接法 A）

Scrapy **不是**一个可以被 `await` 的爬虫函数——它本身是一套完整的爬虫应用，自带调度器、下载器、Spider、Item Pipeline。三种接法的权衡：

| 接法 | 描述 | 结论 |
|---|---|---|
| **A. 独立进程** | `scrapy crawl xiaohongshu` 独立跑，Item Pipeline 直接写 `raw_posts` | ✅ **采用** |
| B. 库嵌入 | `CrawlerProcess` 在 workflow 进程内跑 | ❌ Twisted reactor 与 asyncio 冲突；Scrapy 崩溃带崩 workflow |
| C. 混合 | 部分平台用 httpx | ❌ 现阶段平台少，收益不足以抵掉双实现成本 |

接法 A 的收益：

1. **故障隔离** — Scrapy 被封号 / 崩溃，workflow 只记一条 failed，可重跑，互不影响
2. **配置解耦** — Scrapy 的 cookie、代理、限速配置在 Scrapy 侧；workflow 只关心"跑没跑成"
3. **无 async 冲突** — workflow 用 `subprocess` 同步调用，无需处理 Twisted/asyncio 桥接

#### 5.2.2 Spider 契约（★归一化在这里完成）

每个平台的 spider **必须产出统一字段**，无论底层是 JSON API 还是 Playwright DOM：

```python
# crawler/base.py —— 所有 spider 产出同一组字段

@dataclass
class RawPost:
    """统一字段。平台差异已在 spider 内部抹平。"""
    platform: str
    external_id: str          # 平台内唯一 ID
    content: str              # 正文（已做 trim / 全角半角 / 空白折叠）
    title: str | None
    author: str | None        # 已脱敏
    published_at: datetime | None
    url: str | None
    likes: int | None
    comments_count: int | None
    collects: int | None      # 部分平台无此指标 → None
```

**统一字段契约**：engine 层只认这 10 个字段，不认任何平台特有字段。

#### 5.2.2.1 为什么归一化必须在 spider 层

平台的采集方式差异极大，**提取逻辑根本不同**：

| 采集方式 | 提取手段 | 例子 |
|---|---|---|
| JSON API（GitHub、抖音接口） | 字段路径 | `issue["user"]["login"]`、`body["note_card"]["desc"]` |
| Playwright DOM（小红书、闲鱼） | CSS 选择器 | `div.note-item h1::text`、`span.like-count::text` |
| RSS / 第三方平台 | 结构各异的 XML/JSON | —— |

**配置化（`field_mapping`）在这里不成立**，原因：

1. DOM 提取无法用"路径"表达——选择器、等待、滚动、懒加载都是逻辑，必须写代码
2. 即使是 JSON 平台，配套的**类型转换**（`"1.2万"` → `12000`）、**缺失兜底**、**时间戳解析**也得写代码
3. 平台字段"改名"通常伴随"结构变更"，配置救不了

因此：**"从平台原始形态提取统一字段"由各平台 spider 独立完成**。配置里只放跨平台通用的参数（限速、互动量权重、游标类型）。

#### 5.2.2.2 Spider 实现形态

```python
# crawler/spiders/xiaohongshu.py —— Playwright DOM 型
from core.normalize import content_hash

class XiaohongshuSpider(BaseSpider):
    name = "xiaohongshu"
    mode = "search"

    def parse_search(self, response):
        for node in response.css("section.note-item"):
            content = node.css("span.desc::text").get("") or ""
            yield RawPost(
                platform=self.name,
                external_id=node.attrib.get("data-note-id"),
                title=node.css("a.title::text").get(),
                content=content,
                content_hash=content_hash(content),   # ★ 采集时定死，之后不可变
                author=node.css("a.author::text").get(),
                published_at=parse_xhs_time(node.css("span.time::text").get()),
                url=node.css("a.title::attr(href)").get(),
                likes=parse_count(node.css("span.like-count::text").get()),
                comments_count=parse_count(node.css("span.comment-count::text").get()),
                collects=parse_count(node.css("span.collect-count::text").get()),
            )
```

```python
# crawler/spiders/github.py —— JSON API 型
from core.normalize import content_hash

class GithubSpider(BaseSpider):
    name = "github"
    mode = "search"

    def parse_search(self, response):
        for issue in response.json()["items"]:
            body = issue.get("body") or ""
            yield RawPost(
                platform=self.name,
                external_id=str(issue["number"]),
                title=issue["title"],
                content=body,
                content_hash=content_hash(body),
                author=issue["user"]["login"],
                published_at=parse_iso(issue["created_at"]),
                url=issue["html_url"],
                likes=issue.get("reactions", {}).get("total_count"),
                comments_count=issue.get("comments"),
                collects=None,                    # GitHub 无收藏
            )
```

**两种形态的 spider 产出同一个 `RawPost`**——这是 crawler 层对 engine 层的唯一契约。

> `content_hash` 由 spider 在采集时算好并写入（归一化规则来自 `core/normalize.py`）。**必须采集时定死**——唯一索引要求行入库时 hash 就存在，且重算规则变更会造成索引冲突。见自评 P2。

#### 5.2.2.3 不存平台原始 payload

spider 直接把平台返回的结构**解析成统一字段**，原始 JSON 解析完即丢弃，**不落库**。

**为什么不存**：

平台一条内容的完整 JSON 里，80%+ 是永远用不上的东西——图片元数据、推荐流、用户全集、埋点数据。GitHub 的 issue 原始 JSON 约 2.1 KB，其中 85% 是不需要的内容；小红书笔记原始 JSON 约 1.5 KB，约 74% 是不需要的内容。存下来是每条帖子 300~400 字节的纯冗余，10 万条就是 30~40 MB。

更根本的问题是**它换不回东西**：

| 场景 | 存了 payload 够不够 |
|---|---|
| 统一字段算错了，想用原始数据重算 | ⚠️ 勉强够——但平台没改版的话，`url` 回源重爬单条等价 |
| 平台把 `liked_count` 改名为 `like_count` | ✅ 够 |
| 想加一个**当初就没存**的字段（如 `labels[].name`） | ❌ **不够**——当初就没留，存了也没用 |

而"想加个新字段"才是改版时更常见的需求。**统一字段本身已经是"重新解析后的结果"**，且是跨平台可比的——平台改的只是平台字段名，我们这层已经统一命名了。

**代价**：平台改版后，历史数据里的新字段拿不回来，只能重爬。对 GitHub 这类内容长期存在的平台无所谓（URL 一访问就能拿到 JSON）；对小红书这类删帖/限流频繁的平台，重爬可能拿不到——这是接受这个取舍的原因。

**需要时怎么办**：`url` 字段始终保留，任何一条数据都能回源重取。这是"数据没了就再取一次"，而不是"提前存一份可能用不上的副本"。

#### 5.2.3 调用契约

```python
# crawler/runner.py

@dataclass
class CollectResult:
    spider: str
    returncode: int
    items_scraped: int
    duration_ms: int
    error: str | None
    log_path: str | None


async def collect(platform: str, keywords: list[str]) -> CollectResult:
    """触发 Scrapy 独立进程，等待其写入 raw_posts，返回执行摘要。

    约定：
    - spider 产出统一字段的 RawPost，Item Pipeline 负责写库
    - 执行摘要通过 Scrapy 的 stats 文件读回
    - 失败不抛异常，返回 returncode != 0 的结果，由 workflow 决定重试
    """
```

**关键约定**：Scrapy 的 Item Pipeline（Scrapy 内部概念）与本系统的 `workflow` 层是两回事：

- Scrapy Item Pipeline：单次爬取内部的数据加工链（提取字段 → 写库），生命周期只有一次 crawl
- `workflow`：跨运行的阶段编排（collect → clean → score → ...），生命周期是天/小时级

#### 5.2.3 各平台技术路线

| 平台 | 难度 | 数据价值 | 建议路线 | 备注 |
|---|---|---|---|---|
| **GitHub** | ⭐ 低 | 中 | **官方 API** | 首选，issue 里痛点表达极清晰 |
| **闲鱼** | ⭐⭐ 中 | **高** | Scrapy + 登录态 | 金矿——"求购""有没有""代找"全是明文需求 |
| **小红书** | ⭐⭐⭐⭐ 高 | **高** | Scrapy + 登录态 + 签名算法 | 主力数据源，需投入 |
| **抖音** | ⭐⭐⭐⭐⭐ 很高 | 高 | 第三方数据平台 或 Scrapy 逆向 | 签名/滑块风控，难度最高 |
| **视频号** | ⭐⭐⭐⭐⭐ 很高 | 中高 | 第三方平台 | 无公开 API，反爬严格 |
| **微博/知乎** | ⭐⭐ 低 | 中 | 公开接口 / RSS | 可作补充 |

#### 5.2.4 实施优先级

> **不要一上来全做。**

```
Phase 1:  小红书 + GitHub（用户 2026-11-04 定：中文平台 + 英文平台各一个，
         顺便把中英两套过滤规则都跑通）
Phase 2:  闲鱼（价值最高，技术难度适中）
Phase 3:  抖音 / 视频号（成本最高，最后考虑）
```

**为什么 Phase 1 是这两个**

| | 小红书 | GitHub |
|---|---|---|
| 痛点表达强度 | **高**——"求推荐""有没有""太贵了"全是明文需求 | 中——issue 里痛点清晰但偏技术 |
| 数据价值 | **高**——消费级产品机会的主要来源 | 中——偏开发者工具 |
| 技术难度 | 高（登录态 + 签名算法） | **低**（官方 API） |
| 过滤规则 | `config/phrases/zh/` | `config/phrases/en/` |
| 检验什么 | 中文规则的召回率 | 英文规则的召回率 |

**两个一起做不是为了凑数**——它们检验的是同一套代码路径下的两套规则集。只有同时做，才能在 Phase 2 接闲鱼之前确认「中文规则准不准」这个问题。

#### 5.2.5 采集层过滤规则 ★

> **采集层是第一道关卡。** 不通过的内容**不落库**——这是控制存储量的主要手段。
> 采集量是分钟级会变大的（关键词反哺后搜索面变宽），而存储是小时级只增不减的。

**过滤链**（实现在 `core/phrases.py`）：

```
文本 → Tier1 命中？ ────────────────────→ 保留
     → Tier2 命中？ → 有佐证吗？ ──是──→ 保留
                                └──否──→ 丢弃
     → 都没命中 ──────────────────────→ 丢弃
```

**四张词表**，按语言分目录：

| 词表 | 作用 | 英文条数 | 中文条数 |
|---|---|---|---|
| `tier1.yaml` | 高置信痛点短语，命中即保留，无需佐证 | 264 | 252 |
| `tier2.yaml` | 上下文依赖短语，需佐证才成立 | 62 | 113 |
| `price_signals.yaml` | 价格/商业信号（Tier2 佐证之一） | 57 | 85 |
| `tool_signals.yaml` | 工具/载体信号（Tier2 佐证之一） | 47 | 54 |

```
config/phrases/
├── en/{tier1,tier2,price_signals,tool_signals}.yaml
└── zh/{tier1,tier2,price_signals,tool_signals}.yaml
```

**规则集移植自 `albertorsesc/pain-discovery`**，做了三处修正：

| 修正 | 参考实现 | 我们的做法 | 为什么 |
|---|---|---|---|
| **词边界** | 朴素子串匹配 | `\b` 锚定 + 空白归一 | 参考实现会把 `"am i being"` 误配到 `"am i beings"` |
| **词形变体** | 精确匹配 | 允许 `s/es/ed/ing/er` 等后缀 | 参考实现漏掉 `"crashing"`（词表里是 `"crashes constantly"`） |
| **工具信号** | 注释写 "need dollar/tool signal"，只实现了 dollar | 补齐 `tool_signals.yaml` | 否则 `"有没有人推荐个记账软件"` 这类高频中文求助句式全部漏杀 |

**佐证判定**：

```python
evidence = price_signals ∪ tool_signals ∪ {金额}
# 金额：英文 $500，中文 3000块 / ¥3000 / 三千
```

**平台语言绑定**：平台 yaml 声明 `lang: en | zh`，spider 按此加载对应规则集。

**已知限制**：

- **子串匹配会漏掉同义表达**。`"太贵"` 覆盖了 `"价格太贵"`，但覆盖不了 `"这个价格劝退"`——虽然后者在词表里。词表是**枚举**不是**语义模型**，长尾表达需要靠 Phase 2 的反哺机制补
- **规则命中与否不可解释到"为什么"**。命中的具体短语会记录到 `run_log`（见 5.2.7），但"漏了什么"无法从规则本身看出——需要靠引擎层的 `scorer_audit` 采样反查

> **⚠️ 误杀不可挽回。** 被丢弃的内容连 `raw_posts` 都进不去，所以**规则变更不会追溯影响历史数据**——这是好事(避免去重错乱)，但也意味着**每次调词表都是永久性的**。因此：
> - Phase 1 先用**宽松阈值**(宁可多留)
> - 被丢弃条数按原因分类记入 `run_log`，调参有数据依据
> - 抽样保留「未命中但互动量高」的内容，作为词库召回率的对照样本（**存放方式见自评 P17**——不进 `raw_posts`，走独立 JSONL 滚动文件）

#### 5.2.6 增量采集与游标

**游标语义由各平台自行定义**（不同平台的"最后一条"含义不同），但**游标的存取统一在 `storage` 层**：

```python
# crawler/base.py —— 游标是不透明对象，平台自己定义其含义
@dataclass
class Cursor:
    """平台自定义的增量游标。不透明，由各平台自行解释与推进。"""
    platform: str
    keyword: str
    payload: Any            # 平台特有的游标内容：JSON 对象或标量均可，storage 层不假设结构
    updated_at: datetime


class BaseCrawler(ABC):
    name: str
    priority: int

    @abstractmethod
    async def get_cursor(self, keyword: str) -> Cursor | None:
        """读取该平台该关键词的增量游标。无游标返回 None。

        实现方需自行应用 lookback_window 防漏采冗余。
        """
        ...

    @abstractmethod
    async def save_cursor(self, keyword: str, cursor: Cursor) -> None:
        """持久化新游标。仅在数据写入成功后调用。"""
        ...
```

各平台游标语义差异示例：

| 平台 | 游标内容（`payload` 结构） | 说明 |
|---|---|---|
| GitHub | `{"last_issue_number": 5234}` | issue number 单调递增，可直接 `> cursor` 过滤 |
| 小红书 | `{"last_published_at": "...", "last_note_id": "..."}` | note_id 无序，需时间水位线 + id 去重双保险 |
| 抖音 | `{"max_cursor": "...", "last_aweme_id": "..."}` | 雪花 ID 勉强有序，需配合接口 max_cursor |

> **⚠️ 游标结构不写在配置里。** 各平台的 `payload` 结构由**spider 代码自己定义**——配置层不声明游标类型（曾有的 `cursor.kind` 字段已移除）。理由：
> - **类型声明会过期**：spider 换实现后，配置里的 `kind` 不会跟着变，变成过期的谎言；而 `payload` 永远是真的
> - **枚举限制平台**：抖音的 `max_cursor + 雪花 id` 不在常见枚举里，加枚举就得加分支；`payload` 是 JSON，天然支持任意结构
> - **无人消费**：没有任何代码读 `kind`——游标本就该是"不透明对象，由平台自行解释"
>
> 配置层只保留**与游标结构无关的通用参数**：`cursor.lookback_window`（水位线往前回退多少秒，防边界漏采）。GitHub 用 0（单调递增不会漏），小红书用 300。

**⚠️ 推进顺序是硬约束**：

```
✅ 正确：写 raw_posts → 提交事务 → 推进游标
❌ 错误：推进游标 → 写 raw_posts
```

反过来的话，写库失败时游标已跳过这批数据，**永久丢失**。反过来最坏情况是重复采集，靠 `uk_raw_platform_extid`（平台 + external_id）去重——损失可接受，丢失不可接受。

#### 5.2.7 通用工程能力

- **UA 池 + 代理池**：轮换出口 IP
- **限速器**：令牌桶，按平台独立配额（Scrapy `DOWNLOAD_DELAY` + 随机抖动）
- **重试与退避**：指数退避 + 抖动
- **反爬对抗**：登录态持久化（Cookie 单独存文件，**不入库、不入 git**）
- **可观测性**：每次采集记录成功/失败数、耗时、被封信号

---

### 5.3 engine/ — 核心发现引擎 ★

> 这是整个系统最有价值的部分，也是最难的部分。

```
engine/
├── cleaner.py           # 跨平台通用清洗：内容哈希归一 + 去重 + 文本规范化
├── keyword_bank.py      # 痛点信号词库 / 噪音词库（加载自 config，供采集层使用）
├── scorer.py            # LLM 分级 → A/B/C/D/E
├── signal_store.py      # signal 表读写
├── cluster.py           # LLM 主题归并（route B）
├── idea_generator.py    # LLM 生成 product_idea + 六维评分
├── llm_client.py        # LLM provider 抽象 + 重试 + 缓存 + 预算熔断
├── validator.py         # 输出校验（JSON schema + 溯源完整性）
└── feedback.py          # 反哺：idea/signal → 新关键词
```

#### 5.3.1 清洗（跨平台通用规则）

**全部在 `cleaner.py`。全是跨平台通用规则，不含任何平台分支代码**（平台差异已在 crawler 层抹平）。本层只做**归一与去重**——噪音过滤已前置到采集层：

**① 内容哈希归一化 + 跨平台去重**

归一化规则放在 **`core/normalize.py`**（纯函数，不依赖任何层），spider 算 hash、cleaner 做清洗共用同一份：

```python
# core/normalize.py
def normalize(content: str) -> str:
    """内容归一化。content_hash 与文本清洗共用此规则。"""
    norm = (
        content
        .strip()
        .lower()
        .replace(" ", "")            # 折叠空白
        .replace("\n", "")
    )
    norm = re.sub(r"[\U0001F600-\U0001F9FF]", "", norm)   # 去 emoji
    norm = unicodedata.normalize("NFKC", norm)            # 全角→半角
    return norm


def content_hash(content: str) -> str:
    """归一化后取 SHA256。用于跨平台去重。"""
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()
```

同一条抱怨被跨平台搬运（很常见）→ 哈希相同 → 只留一条。

> ⚠️ **为什么必须放在 `core/`**：spider 需要算 `content_hash`（否则 hash 要等清洗层才算，但唯一索引要求采集时就有），cleaner 需要同一套规则做文本清洗。若规则留在 `engine/cleaner.py`，`crawler` 就会反向依赖 `engine`，违反 3.2 的单向依赖硬约束。见自评 P2。

**② 噪音过滤** — **不在本层**。噪音词过滤（广告/招聘/教程/带货）已前置到采集层，见 5.3.2 ①。

**③ 文本规范化** — trim、折叠连续空白、统一换行、去水印文案（"点击链接..."）、去话题标签（`#xxx`）、去 @提及。

> **⚠️ `content_hash` 计算后不可变。** `normalize()` 规则一旦改变，只能影响新数据，**不得回溯重算历史数据的 hash**——否则同 hash 的行会无法与已存行区分，去重结果错乱。

**④ 去重时的来源保留**：跨平台重复的内容需要记录它出现在哪些平台 → `raw_posts.source_platforms`（JSON 数组）。

> **注意**：平台内同一 `external_id` 的重复（爬虫重试、翻页重叠）由 `uk_raw_platform_extid` 在**采集层**拦截；内容重复（跨平台搬运 + 同平台转发）由 `content_hash` 在**清洗层**处理。两者是不同的问题，处理位置也不同。
>
> ✅ **P1 已决策：采纳方案 A**（2026-11-03）。`raw_posts.content_hash` **不唯一**（普通索引）——同一内容在同一平台出现多次是常态（转发、重复上架、duplicate issue、刷屏），原始层保留全部采集事实。清洗层按 hash **分组取代表**：组内按互动量取一条进 `signal`，其余记 `scorer_audit`；跨平台重复合并进 `source_platforms`。详见 13.1 P1。

**分组取代表的具体做法**：

```sql
-- ① 找出同 hash 的多条记录
SELECT content_hash, COUNT(*) AS n, SUM(platforms_n) ...
FROM raw_posts
WHERE cleaned_at IS NULL
GROUP BY content_hash;

-- ② 组内按互动量取代表（互动量公式见 5.3.2，权重按平台配置）
SELECT * FROM raw_posts
WHERE content_hash = ?
ORDER BY engagement_score DESC
LIMIT 1;
```

- 代表进 `signal`，**组内其余行记 `scorer_audit`**（`noise_reason = 'duplicate'`），并标记 `cleaned_at`
- 跨平台组：所有涉及平台合并进代表的 `source_platforms`（`["xiaohongshu", "weibo"]`），signal 层据此给强度加成
- ⚠️ `source_platforms` 的合并是**写操作**（UPDATE），与"原始层不可变"不冲突——它记录的是**观察结果**（这条内容出现在哪），不是原始 payload 的改写

#### 5.3.2 打分策略：两段式（采集层过滤 → LLM 分级）

过滤分两段，职责不同，**不要混在一起**：

| 段 | 位置 | 作用 | 失败后果 |
|---|---|---|---|
| **① 采集层过滤** | `crawler/` | 挡掉绝大多数数据，不落库 | 控制存储量 |
| **② LLM 分级** | `engine/scorer.py` | 对落库数据定级 | 分级 A~E |

**为什么不设中间规则召回层？**

规则召回（粗筛打分 + 灰区 band）原本用于"规则明确判定的短路、只跑存疑样本"，省 LLM 成本。但它让**同一套规则散在两处**——采集层一份、清洗层一份，调阈值时要同时看两个地方，且两处规则语义不一致时很难排查（"这条为什么被丢了"）。

现在规则**全部集中在采集层**，落库时已经过完所有规则过滤，**落库的每一行都是值得送 LLM 的候选**。因此直接调 LLM 定级：

- **链路更短**：raw_posts → LLM → signal，中间没有多余环节
- **规则只有一处**：调阈值只看采集层配置，不用跨层对照
- **分级质量更高**：LLM 看完整内容，而不是只看规则分猜出来的灰区样本

**代价**：LLM 调用量上升（全部落库数据都要过 LLM，而不再只是灰区样本）。落库量因采集层过滤变严而下降，两者部分抵消。**实际调用量需在 Phase 1 用真实数据测**，若超预算，优先收紧采集层阈值，而不是把规则召回层加回来。

---

**① 采集层过滤**（`config/platforms/*.yaml` 的 `filter` 段）

在 spider 里、**写库之前**执行。

**硬门槛**——三条规则，**全部是 OR 关系**——满足任一条即通过：

| 规则 | 默认阈值 | 说明 |
|---|---|---|
| **互动量达标** | `likes >= 5` **或** `comments >= 3` | 门槛按平台配置 |
| **标题长度** | `3 <= len(title) <= 200` | 过短无信息，过长是长文 |
| **正文长度** | `len(body) >= 15`（若有正文） | 太短多半是标题党/广告 |

> **⚠️ 为什么是 OR 而不是 AND。** 痛点帖的典型特征是**评论多、点赞少**——用户不一定点赞一个吐槽，但一定会在下面回"我也是"。如果要求 `likes>=5 AND comments>=3`，这类最典型的求助帖会被系统性漏掉。
> 参考实现 `pain-discovery` 用的也是 OR（`MIN_SIGNAL_SCORE=5` / `MIN_SIGNAL_COMMENTS=3`）。
>
> **⚠️ 互动量门槛必须按平台配置**，不能全局一个 N。因为各平台指标不齐——抖音/闲鱼/GitHub 无收藏数，微博用 `attitudes` 而非 `likes`。

**短语过滤**（Tier1/Tier2 + 佐证），见 **5.2.5 采集层过滤规则**。它与互动量门槛是**并集关系**：命中短语规则 OR 互动量达标 → 通过。

**噪音词过滤**——命中即丢弃：

| 类别 | 词示例 |
|---|---|
| 招聘 | 招聘 / 内推 / 求职 / 薪资 / 岗位 |
| 教程 | 教程 / 教你 / 如何做 / 步骤 / 保姆级 |
| 带货 | 优惠 / 带货 / 包邮 / 优惠码 / 链接 / 下单 / 券 |
| 求购链接 | 求购 / 收 / 出 / 蹲 |

> **⚠️ 噪音词过滤有误杀风险。** "求购"在闲鱼是需求信号，但在小红书是"求购链接"（带货）。噪音词表按**平台**配置（`config/platforms/*.yaml` 的 `filter.noise_words`），闲鱼与小红书不能用同一份。
>
> 与短语过滤的**关系**：两者是**并集**——命中任一即通过硬门槛+短语过滤，但**命中噪音词则一律丢弃**（噪音优先于命中）。

> 两条规则的顺序：**先查库去重，再判门槛**。否则同一批数据反复请求、反复丢弃，白白消耗配额。

---

**② LLM 分级**

落库的 `raw_posts` 全部送 LLM，一次定级。

**LLM 输出契约：**

```json
{
  "pain_score": 0,
  "is_pain": true,
  "pain_type": "功能缺失",
  "target_user": "小微餐饮店主, 每天需要核对多平台账单",
  "willingness_to_pay": "高",
  "grade": "A",
  "reason": "用户明确表达现有工具无法完成对账, 且在寻找替代方案"
}
```

`pain_type` 建议限定枚举值：`功能缺失 | 体验差 | 效率低 | 成本高 | 集成难 | 可靠性 | 其他`

**批处理与降级**：LLM 调用按批提交（batch size 在 `config/workflow.yaml`）。批量中某条解析失败或超时，**降级为 B 级入 `signal`** 并记 `scorer_audit`（`noise_reason='llm_error'`），不整批回滚。

#### 5.3.3 A/B/C/D/E 分级标准

> 分级由 **② LLM 分级** 产出。采集层只负责"要不要留"，LLM 才看"这值不值得做成产品"。

| 级别 | 判定条件 | 处理动作 | 占比预期 |
|---|---|---|---|
| **A** | 明确痛点 + 明确人群 + 有付费意愿或正在找替代品 | 入 signal，最高优先级生成 idea | ~2% |
| **B** | 明确痛点，但人群或付费意愿模糊 | 入 signal，生成 idea 但标注"待验证" | ~6% |
| **C** | 隐含需求/含蓄抱怨，需推理才能识别 | 入 signal，低优先级 | ~12% |
| **D** | 弱信号，仅有情绪表达无具体诉求 | 不入 signal（可留审计） | ~20% |
| **E** | 噪音、广告、无关内容 | 直接丢弃 | ~60% |

> 比例是经验预估，实施时需用真实数据校准。

**入库门槛**：`grade IN ('A','B','C')` 才进 `signal` 表。D/E 仅在 `scorer_audit` 表留一行（用于校准规则阈值），不进主表。

> **⚠️ 分级定义与采集层短语过滤是两套东西，别混。**
> 参考实现 `pain-discovery` 的 `triage_signal` 定义是 **C=纯吐槽、D=专业讨论**，与我们这里的 **C=隐含需求、D=弱信号** 不同。两套分级语义不能交叉使用——采集层只看"要不要留"，引擎层才看"这值不值得做成产品"。

**分级 Prompt**（放在 `config/prompts/triage.md`）：

```
你是产品需求分析师。给下面这条内容打一个级别。只回复一个字母，不要任何其他内容。

标题: {title}
正文: {body_snippet}

A = 可落地的痛点: 用户有具体问题且愿意付费解决 —— 消费决策、流程卡点、
    对工具不满、手工流程需要自动化、规模化的瓶颈、软件不好用、需求未被满足
B = 单纯打听: 想了解或比较，但没有明显挫败感或急迫性
C = 隐含需求/含蓄抱怨: 有抱怨但没直说，需要推理才能识别出痛点
D = 弱信号: 只有情绪表达，没有具体诉求
E = 噪音: 闲聊、段子、梗图、行业闲聊、无商业机会信号

只回复一个字母 (A/B/C/D/E):
```

> **Prompt 里的分级定义必须与上表严格一致。** 参考实现的原文是英文（`A = ACTIONABLE PAIN ... C = VENTING ONLY: Frustrated about the past, not seeking solutions`），我们把 C/D 的语义改成了「隐含需求/弱信号」——所以 prompt **不能直接抄英文版**，要按我们的定义重写。参考实现的英文原文留档见 `config/prompts/triage.reference.md`。
>
> **Prompt 输出契约注意**：参考实现只让 LLM 回一个字母，我们扩成了 JSON（多了 `pain_score` / `is_pain` / `pain_type` / `target_user` / `willingness_to_pay` / `reason`）。两种形式二选一，别混用——如果 prompt 让 LLM 只回字母，就拿不到结构化字段。**用 JSON 版**（见 5.3.2 ③ 的契约），校验靠 `validator.py`。

#### 5.3.4 signal 表字段契约

`signal` 表是引擎的核心产物，字段需覆盖"信号识别"和"热度排序"两类用途：

| 字段 | 类型 | 用途 |
|---|---|---|
| `id` | BIGINT PK | |
| `source` | VARCHAR(32) | 来源平台（xiaohongshu / xianyu / github ...） |
| `source_channel` | VARCHAR(64) | 平台内频道/分区/话题（如 github 的 repo、xhs 的 tag） |
| `title` | TEXT | 标题/首句 |
| `body` | TEXT | 正文 |
| `num_comments` | INTEGER | 评论数（互动强度的关键指标） |
| `score` | INTEGER | 平台热度字段（点赞等），入库门槛 + 排序 |
| `url` | TEXT | 原文链接 |
| `triage_grade` | CHAR(1) | A/B/C/D/E 分级 |
| `pain_type` | VARCHAR(32) | 痛点类型 |
| `dollar_amounts` | VARCHAR(128) | 提取到的金额信号（如 "5000-8000/套"） |
| `content_hash` | VARCHAR(64) UNIQUE | 去重 |
| `raw_post_id` | BIGINT FK | 溯源到原始记录 |
| `rule_hits` | TEXT(JSON) | 命中的规则列表（规则调参依据） |
| `scored_at` | TIMESTAMPTZ | |

> **归属状态不在本表**，而在 `signal_idea.is_primary`（多对多关联表）——见 6.3。`dollar_amounts` 是付费意愿的强信号，在 `cluster` 阶段作为 prompt 上下文单独注入（详见 5.3.5）。

#### 5.3.5 主题归并 + Idea 生成

**流程**：

1. 取 `triage_grade IN ('A','B')` 且**无 primary 归属**的信号（判定见 6.3 的 SQL）
2. **全池数量** < 门槛（默认 3）→ 跳过本轮，返回 `skipped`，等信号积累
3. 按 `source` 分组，每组最多取 N 条（默认 20），**不足 3 条的组与不足 3 条的零散信号合并成一个跨平台小批次**
4. LLM 输出机会列表 + 对应的 `signal_ids`
5. 校验 `signal_ids` 有效性（必须来自本批输入），无效则丢弃该机会
6. 写入 `product_idea`，并插入 `signal_idea` 关联行（`is_primary = 1` 标记归属）

> ⚠️ **第 2 步的门槛必须在分组之前判定**。若在分组后判定"每组 ≥3"，某平台上只有 2 条信号时永远凑不够 → 永远 `skipped` → 那条信号**永久卡住**，系统静默不产出。见自评 P6。
>
> 同理，**不允许把一条信号排除在所有批次之外**——LLM 判定某条 signal 与任何机会都不匹配时，这条 signal 需要被"消耗掉"（记入 `scorer_audit` 或一个 `signal_idea` 的 `relation='rejected'` 行），否则它会一直被下一轮重新捞出，拖住整个批次的处理。

> **⚠️ 第 2 步是"跳过"不是"等待"**：信号不足时阶段立即返回 `skipped`，**绝不写成 `while True` 循环**。等待由 scheduler 的调度频率承担——见 5.5.3。

**LLM Prompt 的 Lens 结构**（这是 idea 质量的核心资产，放在 `config/prompts/idea_generation.md`）：

```
你是 2026 年的产品战略分析师。用下面这些经过验证的视角分析用户抱怨：

LENS 1 - BORING INDUSTRIES PAY 10X: 会计、牙科、暖通空调、法律、保险、建筑、
        水管、屋顶维修 —— 不价格敏感、几乎没有 AI 竞争、用户转介绍极猛。优先这类。

LENS 2 - THE ABANDONED WORK CATEGORY: 因为对人类成本太高而从来没人做过的工作。
        零竞争。例：没人审计每一张收据，没人审每一条合同条款。

LENS 3 - COPILOT vs AUTOPILOT: Copilot = 卖工具（人仍然干活）；
        Autopilot = 卖成品（AI 干活）。优先 autopilot —— 市场大 6 倍。

LENS 4 - THE OUTSOURCING WEDGE: 如果这项工作现在已经被外包（记账、VA、
        代理机构），销售更容易 —— 是换供应商，不是重组流程。

LENS 5 - REVENUE AGENT: 能否做成按计划运行、有可量化财务 KPI 的 AI agent？

以现代 2026 年视角思考：AI 驱动的工具、SaaS、浏览器扩展、AI agent、
自动化工作流、市场平台，或细分领域的 micro-SaaS。不要 PDF、清单、模板。

来自 {source}/{source_channel} 的抱怨:
{signal_texts}

每个机会返回:
- name: 具体的产品名（如 "PriceCheck AI"，不要 "General Tool"）
- product_idea: 到底要做什么？讲清楚技术和 UX
- buyer: 谁买？（他具体的处境和急迫性）
- why_pay: 为什么付钱？ROI 或痛点缓解是什么
- current_solutions_and_gaps:
    existing: 用户现在用什么凑合
    gaps: 为什么现有方案不行
- sample_quotes: 用户原话片段（必须是原文子串）
- opportunity_type: "micro-saas" | "autopilot-service" | "revenue-agent" | "marketplace" | "app" | "browser-extension"
- copilot_or_autopilot: "copilot" | "autopilot"
- signal_ids: 映射到这条机会的抱怨 ID

重要：只保留真实有钱流的场景。跳过纯吐槽和随口一问。

只返回一个 JSON 数组:
[{"name":"...","product_idea":"...","buyer":"...","why_pay":"...",
  "current_solutions_and_gaps":{"existing":"...","gaps":"..."},
  "sample_quotes":["..."],"opportunity_type":"...",
  "copilot_or_autopilot":"...","signal_ids":[IDs]}]
```

**Prompt 输入上下文**（每条信号，格式来自参考实现）：

```
[{signal_id}] ({source}/{source_channel}, {score}pts) {title} [{dollar_amounts}]
```

`dollar_amounts` 有值时追加——具体金额是付费意愿的最强信号，显著影响 LLM 对"会不会有人付钱"的判断。

> **两条必须保留的英文原句**（来自参考实现，作用明显）：
> - `"Only include opportunities where real money flows. Skip venting and casual questions."` —— 挡掉"随口一问"型信号，这正是我们 A/B 分级要拦掉的东西
> - `"Think modern 2026: ... NOT PDFs, checklists, or templates."` —— 挡掉 LLM 生成"整理清单"这类伪产品
>
> LENS 本身可以中文化（我们已译），但这两句的**约束力**来自具体措辞，翻译后仍保留同样语义，存疑时以英文原文为准。

**输出契约：**

```json
{
  "name": "PriceCheck AI",
  "product_idea": "具体做什么，技术与 UX 细节",
  "buyer": "谁买？他的具体处境和紧迫性",
  "why_pay": "为什么付钱？ROI 或痛点缓解",
  "current_solutions_and_gaps": {
    "existing": "用户现在用什么凑合",
    "gaps": "为什么现有方案不行"
  },
  "sample_quotes": ["用户原话片段 1", "用户原话片段 2"],
  "opportunity_type": "micro-saas | autopilot-service | revenue-agent | marketplace | app | browser-extension",
  "copilot_or_autopilot": "copilot | autopilot",
  "signal_ids": [123, 456]
}
```

> ⚠️ `current_solutions_and_gaps` 与 `sample_quotes` 是**必填**——5.3.7 的校验规则依赖它们（引文必须是原文子串；答不出现有方案差距说明这需求已被满足）。少了这两个字段，校验形同虚设。见自评 P11。

#### 5.3.6 六维评分与框架标记

每个机会在生成时一并产出评分（与 idea 同一次 LLM 调用，不额外调）：

**评分维度（1-5）**

| 维度 | 5 分 | 1 分 |
|---|---|---|
| `buyer_income` 买家预算 | 年预算 $500K+ 的企业（牙科/法律/暖通） | 破产的消费者 |
| `usage_frequency` 使用频率 | 每天 | 一次性 |
| `alternative_gap` 替代方案差距 | 没有任何方案 | 已完全解决 |
| `buyer_unsophistication` 买家技术盲区 | 不懂技术的蓝领技工 | 开发者 |
| `buildability` 可开发性 | 周末项目（2 个月内 MVP） | 需要团队 |
| `integration_depth` 整合深度 | 替代日常流程 | 可有可无的加分项 |

**框架标记（true/false）**

| 标记 | 含义 |
|---|---|
| `is_boring_industry` | 是否属于枯燥行业（会计/牙科/暖通/法律/水管/屋顶） |
| `is_abandoned_work` | 是否属于因人类成本太高而从未被做过的工作 |
| `is_already_outsourced` | 是否已外包给代理机构/自由职业者 |
| `has_dual_signal` | 该细分领域是否同时存在付费应用和内容创作者 |
| `is_intelligence_gap` | 目标客户是否还没在这项工作上采用 AI |

**分类**

- `opportunity_type`：`micro-saas` | `autopilot-service` | `revenue-agent` | `marketplace` | `app` | `browser-extension`
- `copilot_or_autopilot`：`copilot` | `autopilot`

**LLM 输出契约：**

```json
{
  "buyer_income": 5, "usage_frequency": 5, "alternative_gap": 4,
  "buyer_unsophistication": 5, "buildability": 3, "integration_depth": 5,
  "is_boring_industry": true, "is_abandoned_work": false,
  "is_already_outsourced": true, "has_dual_signal": true,
  "is_intelligence_gap": true,
  "opportunity_type": "autopilot-service",
  "copilot_or_autopilot": "autopilot",
  "reasoning": "一段话的推理说明"
}
```

**评分用途**：六维分不只作展示，更是**排序依据**。

**排序公式**（定死，写进 `config/scoring.yaml`）：

```
base  = buyer_income × usage_frequency × alternative_gap
      × buyer_unsophistication × buildability × integration_depth

mult  = (is_boring_industry    ? 1.5 : 1.0)
      × (is_abandoned_work      ? 1.4 : 1.0)
      × (is_already_outsourced  ? 1.3 : 1.0)
      × (has_dual_signal        ? 1.5 : 1.0)
      × (is_intelligence_gap    ? 1.2 : 1.0)

total = base × mult
```

| 维度/标记 | 系数 | 理由 |
|---|---|---|
| 六维 base | 各维 1~5 直接相乘 | 六维**同时**高才有价值。乘法让任一维接近 1 分就整体塌掉，符合"缺一不可"的直觉 |
| `is_boring_industry` | 1.5 | 不价格敏感、无 AI 竞争、极强转介绍 |
| `is_abandoned_work` | 1.4 | 零竞争，空白市场 |
| `is_already_outsourced` | 1.3 | 已有付费验证，只是换供应商 |
| `has_dual_signal` | 1.5 | 该细分已有付费应用 + 内容创作者，需求被反复验证 |
| `is_intelligence_gap` | 1.2 | 目标客户还没开始用 AI，有时间窗 |

> **乘法倍数而非加法加成。** 加法会让"框架全中但六维都是 2 分"的 idea 排在"六维都是 5 分但无框架"的 idea 前面——前者实际不可做。乘法保证 base 是主导项，框架只做微调。
>
> **配置化**：`config/scoring.yaml` 同时支持 `method: product`（默认，乘积）和 `method: weighted_sum`（加权求和），公式切换不改代码：

```yaml
# config/scoring.yaml
method: product          # product | weighted_sum

weights:                # weighted_sum 模式下生效
  buyer_income: 2.0
  usage_frequency: 1.5
  alternative_gap: 2.0
  buyer_unsophistication: 1.0
  buildability: 1.0
  integration_depth: 1.5

multipliers:            # 框架标记倍数（两种模式都生效）
  is_boring_industry: 1.5
  is_abandoned_work: 1.4
  is_already_outsourced: 1.3
  has_dual_signal: 1.5
  is_intelligence_gap: 1.2
```

**计算时机**：与 idea 同一次 LLM 调用产出六维分和框架标记 → `validator.py` 校验枚举与范围（1-5）→ 算 `total` → 落 `product_idea.total_score`。排序查询直接 `ORDER BY total_score DESC`，不需要运行时再算。

> 参考实现 `pain-discovery` 的 `scorer.py` 用的就是这个结构（六维乘积 × 框架倍数连乘），我们把它配置化以便调参。

#### 5.3.7 输出校验（validator.py）

LLM 输出不可全信，必须做程序化校验：

| 校验项 | 规则 | 失败处理 |
|---|---|---|
| JSON 结构 | Schema 校验 | 重试 1 次，仍失败则丢弃 |
| 溯源完整性 | `source_signal_ids` 非空且均存在 | 丢弃该 idea |
| 引文真实性 | `sample_quotes` 必须是源 signal 内容的子串 | 清除该引文，样本不足则降级 confidence |
| 幻觉检测 | 数字、竞品名等事实性内容需可溯源 | 标记为待人工复核 |
| 枚举合法性 | `pain_type` / `confidence` / `opportunity_type` 必须在枚举内 | 修正或丢弃 |
| 分数范围 | 六维分必须在 1-5 | 修正或丢弃 |
| 签名完整性 | `signal_ids` 必须全部来自本批输入 | 丢弃该机会 |

> 最后一条是 LLM 归纳路线特有的防护：LLM 可能引用不存在的 signal id（幻觉）或引用其他批次的 id。必须做双向校验——既检查"id 存在"，也检查"id 在本批输入集合内"。

---

### 5.4 storage/ — 存储层

#### 5.4.1 选型：SQLite 起步，可迁移 PostgreSQL

**当前选择**：SQLite（标准库自带，无需任何扩展）。

| 理由 | 说明 |
|---|---|
| 零依赖 | 单文件数据库，`git clone` 即可跑，不需要起服务 |
| 快速验证 | Phase 1-2 的数据量（十万级 raw_posts）SQLite 完全够用 |

**迁移到 PostgreSQL 的路径**：本设计的 schema 刻意规避 PG 专属特性，迁移主要是语法替换：

| SQLite（当前） | PostgreSQL（目标） | 迁移动作 |
|---|---|---|
| `TEXT` (存 JSON 字符串) | `JSONB` | 类型替换 + 加 GIN 索引 |
| `INTEGER` (bool) | `BOOLEAN` | 类型替换 |
| `TEXT` (时间戳，ISO8601) | `TIMESTAMPTZ` | 类型替换 |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL` | 类型替换 |
| `TEXT` (逗号分隔的 signal_ids) | `INT[]` | 类型替换 |

> **无向量类型映射**——schema 里没有 embedding 列，迁移时也不用管向量扩展。

**迁移的真正成本不在 schema，在索引策略**（见 5.4.3）和**并发写入**（SQLite 单写者，PG 支持并发）。迁移触发条件：写入出现明显锁等待，或数据量超过千万级，或需要多机部署。

**约束**：所有建表语句用**迁移脚本**管理（`storage/migrations/`），SQLite 和 PG 各一套，不在应用代码里写 `CREATE TABLE`。

#### 5.4.2 表设计概要

```
raw_posts        原始采集数据（不可变，含平台原始 payload）
scorer_audit     D/E 级留痕（规则校准用）
signal           评分后的信号（核心表）★
product_idea     LLM 生成的产品机会（含六维评分）★
crawl_cursor     各平台增量游标
run_log          运行记录（各阶段状态与统计）
```

#### 5.4.3 索引策略

| 表 | 索引 | 用途 |
|---|---|---|
| `raw_posts` | `(platform, external_id)` UNIQUE | 采集幂等（同平台同一条不重复采） |
| `raw_posts` | `content_hash` 普通索引 | 跨平台内容去重（**不唯一**，见自评 P1 方案 A：原始层保留全部采集事实） |
| `raw_posts` | `cleaned_at` WHERE IS NULL | **部分索引**：清洗待处理 |
| `raw_posts` | `scored_at` WHERE IS NULL | **部分索引**：打分待处理 |
| `raw_posts` | `(platform, collected_at DESC)` | 增量采集 |
| `raw_posts` | `(search_keyword, platform)` | 按关键词重采 |
| `signal` | `content_hash` UNIQUE | 跨平台去重 |
| `signal` | `(triage_grade, id)` WHERE grade IN ('A','B') | **部分索引**：归并待处理 |
| `signal` | `(source, source_channel)` | 按频道分析 |
| `signal` | `score DESC` | 热度排序 |
| `signal_idea` | `(signal_id, idea_id)` UNIQUE | 关联去重 |
| `signal_idea` | `signal_id` WHERE is_primary=1 | **部分唯一索引**：归属唯一性约束 |
| `signal_idea` | `(idea_id)` | 反查 idea 的证据 |
| `product_idea` | `(created_at DESC)` | 榜单排序 |
| `product_idea` | `opportunity_type` | 分类筛选 |
| `crawl_cursor` | `(platform, keyword)` UNIQUE | 游标定位 |

> **部分索引（Partial Index）是本设计的关键性能手段**：待处理行数远小于总行数，索引只覆盖"待处理"部分，查询代价与总表规模无关。完整索引清单见 [SCHEMA.md 第 5 章](./SCHEMA.md#5-索引策略)。

#### 5.4.4 SQLite 特有注意事项

- **WAL 模式**：必须开启（`PRAGMA journal_mode=WAL`），否则 Scrapy 写入与 workflow 读取会互相阻塞
- **`busy_timeout`**：设为 5s，避免瞬时锁冲突直接抛 `database is locked`
- **单写者约束**：Scrapy 进程和 workflow 进程不能同时写。用 WAL + 短事务，必要时在 workflow 侧加重试

---

### 5.5 workflow/ — 编排层

> **命名说明**：本层原名 `pipline/`，v2.0 改名为 `workflow/` 以避免与 Scrapy 内部的 Item Pipeline 概念混淆。
>
> - **Scrapy Item Pipeline**：单次 crawl 内部的数据加工链，生命周期 = 一次爬取
> - **workflow 层**：跨运行的阶段编排，生命周期 = 天/小时级

#### 5.5.1 设计原则

**阶段间通过数据库状态解耦**，而非长事务串联。任一阶段失败可从该阶段续跑，不必重头再来。

**关键设计：阶段幂等性由"处理标记字段"实现，而非由状态机表驱动。**

v1.0 的阶段状态机（INIT → COLLECT → CLEAN → ... → DONE）在实际实现中价值有限：阶段数量少且固定，依赖关系线性，用函数调用顺序表达最直接。真正有价值的是**幂等**——避免重跑时重复消耗 LLM 预算。

幂等由业务字段上的标记实现，查询本身就是幂等条件：

| 阶段 | 幂等依据 | 查询条件 |
|---|---|---|
| CLEAN | `raw_posts.cleaned_at IS NULL` | 捞未清洗的 |
| SCORE | `raw_posts.scored_at IS NULL` | 捞未打分的 |
| SIGNAL | `scorer_audit` 表 | A/B/C 已入 signal |
| CLUSTER+GENERATE | `signal_idea.is_primary` | 捞无 primary 归属的 A/B 信号 |
| COLLECT | `crawl_cursor` | 平台游标水位线 |

> **这样设计的额外收益**：`run_log` 从"控制流程"降级为"纯审计记录"，它记录什么已不再影响系统行为。即使 `run_log` 表损坏，系统仍能正确运行——这正是 v1.0 状态机做不到的。

#### 5.5.1.1 阶段执行契约（重要）

**每个阶段都是"捞一次 → 处理完 → 返回"，绝不是循环。**

```python
async def cluster_stage() -> StageResult:
    signals = fetch_pending_signals()          # 一次查询
    if len(signals) < MIN_SIGNALS:
        return StageResult(status="skipped", reason="信号不足")
    ideas = await generate_ideas(signals)       # 处理完
    return StageResult(status="success", output_count=len(ideas))
```

**⚠️ 三条禁止**：

| 禁止 | 原因 |
|---|---|
| 阶段内 `while True` + `sleep` 等信号 | 阻塞整个 workflow（后续阶段被卡住）；与 scheduler 打架；进程常驻无法优雅退出 |
| 阶段内 `await` 等待外部数据就绪 | 同上——等待应交给调度频率 |
| workflow 常驻循环 | 触发权属于 scheduler，workflow 是被调用方 |

**"持续处理"效果由 scheduler 的重复调用产生，不是由循环产生。**

```yaml
# config/workflow.yaml
schedule:
  - name: collect_github
    cron: "0 3 * * *"          # 每日 03:00
    stages: [collect]
  - name: process_batch
    cron: "*/30 * * * *"       # 每 30 分钟
    stages: [clean, score, signal, cluster, generate]
```

两个 job 频率不同：**采集慢（受反爬约束），处理快（纯计算）**。信号不足时 cluster 阶段返回 `skipped`，30 分钟后再试——跑一遍"什么都没干"只要几毫秒，成本为零。

**手动触发同样可用且可重复执行**（幂等保证）：

```bash
python -m workflow.cli run --stages clean,score,signal,cluster,generate
```

#### 5.5.2 阶段编排

```python
# workflow/runner.py

STAGES = [collect, clean, score, signal, cluster, generate]

async def run_workflow(
    stages: list[str] | None = None,
    platform: str | None = None,
    keyword: str | None = None,
    resume: bool = True,
) -> WorkflowResult:
    """执行工作流。

    - stages: 只跑指定阶段，默认全跑
    - resume: True 时跳过"已完成"的阶段（依据幂等依据判断）
    - 单阶段失败即中断，返回失败结果（不自动重试，重试策略在 workflow.yaml 配置）
    """
    for stage in (stages or STAGES):
        if resume and await stage.is_done():
            await record_skip(stage)
            continue
        await record_start(stage)
        try:
            result = await stage.run(platform=platform, keyword=keyword)
            await record_success(stage, result)
        except Exception as e:
            await record_failure(stage, e)
            raise
```

#### 5.5.3 每阶段记录内容

`run_log` 表按 `(run_id, stage)` 记录：

- `status`：pending / running / success / failed / skipped
- `started_at` / `finished_at` / `duration_ms`
- `input_count` / `output_count` / `skipped_count`
- `error_message` / `error_traceback`
- `stats`（JSON）：该阶段的细分统计——各等级分布、LLM 调用数与 token 数、去重丢弃数

**`stats` 里的 LLM token 统计是预算熔断的数据源**，必须结构化入库，不能只进日志文件（日志会轮转、格式不统一、无法聚合查询）。

#### 5.5.4 调度方案选型

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| **APScheduler + 显式阶段函数** | 简单、单机够用、逻辑透明 | 无分布式 | **Phase 1-2 推荐** |
| **Celery + Redis/RabbitMQ** | 分布式、重试完善、生态成熟 | 部署复杂度上升 | Phase 3 需要多机时 |
| **Airflow / Prefect** | DAG 可视化、重跑方便 | 重量级 | 数据规模上来后再考虑 |

> **注意**：workflow 阶段函数本身不依赖调度器。调度器只是"什么时候调 `run_workflow`"，因此可以先做 CLI 手动触发，稳定后再套 APScheduler——零改造成本。

---

### 5.6 scheduler/ — 调度层

#### 5.6.1 职责

- **定时触发**：每日/每数小时触发采集
- **错峰**：各平台错开启动时间，避免同时打同一站点/撞上游限速
- **优先级**：采集 > 打分 > idea 生成（idea 生成可延后到夜间低谷，成本与时效都可优化）
- **全局限流**：跨平台的请求配额，防止触发风控导致全量 IP 被封
- **失败告警**：连续失败或被封信号触发通知

#### 5.6.2 调度配置示例

```yaml
# config/workflow.yaml
schedule:
  - name: collect_github
    cron: "0 3 * * *"          # 每日 03:00
    stage: collect
    platform: github
  - name: collect_xianyu
    cron: "30 4 * * *"
    stage: collect
    platform: xianyu
  - name: score_batch
    cron: "0 */2 * * *"        # 每 2 小时
    stages: [clean, score, signal]
  - name: generate_ideas
    cron: "0 2 * * *"           # 每日 02:00（夜间低谷）
    stages: [cluster, generate]
```

#### 5.6.3 落地顺序建议

**Phase 1-2 不做调度层。** 理由：

1. 采集有强反爬约束，一天跑一次和跑十次产出增量差不多，但风险天差地别——调度层的"错峰"是为"高频多平台"准备的，而 Phase 1-2 只有 1-3 个平台
2. 调度层解决"何时跑"，但真实痛点是"跑得对"。手工跑一次能看到全流程输出，出问题立刻发现；自动跑起来还得先做监控才知道它挂了

Phase 1-2 用 CLI 手动触发，Phase 3 再套 APScheduler——后者只是把 `run_workflow` 换成 cron 调用，延迟做零损失。

---

## 6. 数据模型设计

> **完整表结构见 [SCHEMA.md](./SCHEMA.md)。** 本章只保留关键设计决策，字段级定义以 SCHEMA.md 为准。

### 6.1 表清单

| 表 | 作用 | 层级 |
|---|---|---|
| `raw_posts` | 原始帖子（含平台原始 payload） | 原始层 |
| `comments` | 评论（Phase 3 启用） | 原始层 |
| `signal` | 评分后的信号 ★ | 信号层 |
| `product_idea` | 产品机会（含六维评分）★ | 机会层 |
| `signal_idea` | signal ↔ idea 多对多关联 | 关联层 |
| `scorer_audit` | D/E 级留痕（规则调参数据源） | 审计层 |
| `crawl_cursor` | 增量游标 | 采集层 |
| `run_log` | 阶段运行记录 | 运维层 |
| `keyword_seed` | 种子词 + 反哺关键词 | 配置层 |
| `llm_usage` | LLM 调用与 token 记账 | 运维层 |

### 6.2 三条硬约束

**① 幂等靠业务字段上的标记，不靠状态机表**

每个阶段都有一个"处理标记"字段，查询待处理项 = `WHERE 标记 IS NULL`。这使得 `run_log` 损坏不影响系统正确运行——这是 v2.0 相对 v1.0 的核心改动之一。

| 阶段 | 待处理查询 | 完成标记 |
|---|---|---|
| collect | 游标水位线 + `uk_raw_platform_extid` | `crawl_cursor.payload` 推进 |
| clean | `raw_posts.cleaned_at IS NULL` | `raw_posts.cleaned_at` |
| score | `raw_posts.scored_at IS NULL` | `raw_posts.scored_at` + `signal` 插入 |
| cluster | A/B 信号 且 无 primary 归属 | `signal_idea.is_primary = 1` |
| generate | （与 cluster 同批） | `product_idea` + `signal_idea` 插入 |

**② 溯源强制**

`product_idea` 必须通过 `signal_idea` 关联表挂上至少一条 `signal`。生成后校验通过才写入关联。

**③ 原始层不可变**

`raw_posts` / `comments` 的统一字段由 spider 在采集时一次性填入，之后只写不改；清洗层只做**通用清洗**（不改字段结构），因此"原始不可变"语义完全成立——**没有任何字段是后续回填的派生列**。

### 6.3 `signal` ↔ `product_idea` 采用多对多

`signal` 与 `product_idea` 之间用中间表 `signal_idea` 关联，理由：一条 signal 可能支撑多个 idea（如"账号系统太难用"同时支撑"SSO 工具"和"权限管理工具"）。

**代价与解法**：多对多会让幂等判定变复杂。约定由 `signal_idea.is_primary` 承载归属状态——一条 signal 至多有一个 `is_primary = 1` 的关联行。这样既保留多对多灵活性，又让幂等判定保持为一条简单查询：

```sql
SELECT s.* FROM signal s
WHERE s.triage_grade IN ('A','B')
  AND NOT EXISTS (
      SELECT 1 FROM signal_idea si
      WHERE si.signal_id = s.id AND si.is_primary = 1
  )
ORDER BY s.score DESC;
```

> 因此 **`signal` 表不设 `idea_id` 列**——避免"单列说归了、关联表说没归"的两处状态不一致。

### 6.4 部分索引是幂等设计能保持高性能的关键

```sql
CREATE INDEX idx_raw_pending_clean ON raw_posts(cleaned_at) WHERE cleaned_at IS NULL;
```

待处理行数远小于总行数，索引只覆盖待处理部分 → 查询代价与总表规模无关。若改用状态机表记录阶段状态，那张表会有与帖子数同量级的行，且没有部分索引可用。

### 6.5 存储选型的迁移影响

当前 SQLite（零扩展依赖），schema 刻意规避 PG 专属特性。逐项映射见 [SCHEMA.md 第 6 章](./SCHEMA.md#6-sqlite--postgresql-迁移映射)。

**迁移的真正成本不在 schema**（都是机械替换），而在**索引策略**和**并发写入改造**（SQLite 单写者 → PG 并发）。

---

## 7. LLM 集成设计

### 7.1 Provider 抽象

```python
class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, schema: type | None = None,
                       temperature: float = 0.3) -> dict: ...
```

支持可替换：OpenAI / Claude / 通义 / DeepSeek / 本地 Ollama。通过 `config/llm.yaml` 切换，不改代码。

```yaml
# config/llm.yaml
provider: ollama          # ollama | openai | claude | dashscope | deepseek
model: qwen2.5:14b
fallback_provider: openai # 主 provider 失败时的兜底
budget:
  daily_token_limit: 2000000
  monthly_cost_limit: 50
  on_exceed: degrade      # degrade（降级到规则模式）| pause（暂停到明日）
```

### 7.2 成本控制策略（关键）

LLM 调用量 ≈ 落库数据量（分级）+ 信号池大小 × 归并批次数（生成）。不做控制会失控。控制手段：

| 手段 | 说明 | 效果 |
|---|---|---|
| **采集层过滤前置** | 噪音在采集层就被丢弃，根本不落库、更不进 LLM | 降本 ~90% |
| **信号门槛** | 达到 N 条未归并信号才调 LLM 生成 | 避免碎片化调用 |
| **批量生成** | 一批信号 → 1 次调用，而非 1:1 | 降本 ~70% |
| **批量分级** | 一批 raw_posts → 1 次调用，而非 1:1 | 降本 ~70% |
| **内容缓存** | `content_hash` 命中则跳过 | 增量场景大幅省 |
| **分级模型** | 打分用小模型，idea 生成用大模型 | 分层降本 |
| **预算熔断** | 单日 token 上限，超限降级（如：分级降级为关键词规则） | 防失控 |

> **⚠️ 成本控制的主要杠杆是采集层过滤。** 过滤前置到采集层后，LLM 调用量与落库量成正比——收紧采集层阈值是**唯一同时降低存储与 LLM 成本**的手段。若 Phase 1 实测调用量超预算，**优先收紧采集层阈值**，而不是把已删除的规则召回灰区层加回来（那会让规则重新散在两处，见 5.3.2）。

### 7.3 Prompt 资产化管理

Prompt 是本系统的核心资产，必须：

- **独立文件管理**（`config/prompts/*.md`），代码中不硬编码
- **版本号**：`prompt_version` 写入 `product_idea` 表，支持回溯与对比
- **可 A/B**：同一批 signal 用不同 prompt 版本生成，对比质量
- **含 few-shot 示例**：从历史高质量 idea 中挑真实样例注入

### 7.4 结构化输出

统一使用 JSON Schema 约束：

```python
response_format = {
    "type": "json_schema",
    "json_schema": {"name": "pain_classify", "schema": PAIN_CLASSIFY_SCHEMA}
}
```

配合 `validator.py` 做二次校验。

### 7.5 本地模型可行性

Ollama 路线（`qwen2.5:14b`）在本项目上是可行的，原因：

- 打分任务（A~E 分级）输出格式固定、判据明确，本地模型足够
- idea 生成需要较强的商业推理能力，14B 模型可用但质量有上限
- **建议分级**：打分用本地小模型（量最大、成本敏感），idea 生成用云端大模型（量小、质量敏感）

---

## 8. 实施路线

### Phase 1 — 打通最小闭环（2~3 周）

**目标：验证 idea 生成质量。这是整个项目的成败点。**

- 接入 **GitHub**（英文）+ **小红书**（中文）两个数据源——顺便把中英两套过滤规则都跑通
- 规则打分为主，暂不接 LLM 精排
- 存储用 SQLite（零扩展依赖）
- 手工触发跑通：`采集 → 清洗 → 打分 → signal → 归并 → LLM → idea`
- 产出 Markdown 报告
- **产出**：一批真实 idea，人工评估质量

> **关键决策：不要在 Phase 1 就做调度、前端看板。这些都不验证核心价值。**
>
> Phase 1 接两个平台不是为了多——**一个中文一个英文，检验的是同一套代码路径下的两套规则集**（`config/phrases/zh/` 与 `config/phrases/en/`）。只有同时跑，才能在 Phase 2 接闲鱼之前确认「中文规则准不准」。

### Phase 2 — 扩大数据源 + 评分精排（2 周）

- 接入 **闲鱼**（价值最高）
- 引入 LLM 精排，完成 A/B/C/D/E 分级
- 六维评分上线，idea 按评分排序
- 信号标注集积累（为后续训练分类器做准备）

### Phase 3 — 调度 + 稳定性（2 周）

- APScheduler 调度
- 代理池、限速、失败重试、告警
- 监控看板（采集成功率、LLM 成本、信号产出量）
- 成本熔断

### Phase 4 — 反哺闭环（持续）

```
采集反馈（用户点赞/关注/采纳/否决的 idea）
        ↓
   强化打分权重 + 优化 prompt
        ↓
   定向关键词（从高分 signal/idea 提取）
        ↓
        ↑ 更好的 idea
     采集（定向采集高分主题）
```

这是飞轮，也是长期壁垒。

### 8.1 里程碑依赖关系

```
Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 4
   │           │
   │           └── 六维评分 + idea 排序
   │
   ├── storage schema（所有阶段的基础，最先落地）
   ├── crawler/base.py + github.py + xiaohongshu.py
   ├── core/phrases.py + core/normalize.py
   ├── engine/cleaner.py + scorer.py + signal_store.py
   ├── workflow/runner.py + stages/
   └── engine/cluster.py + idea_generator.py + validator.py
```

---

## 9. 关键风险与难点

### 9.1 反爬与账号风控 ★最大不确定性

**问题**：小红书 / 抖音 / 视频号都有严格的反爬（签名算法、频控、行为检测），账号一旦被封，前期投入清零。

**对策**：

- Phase 1 **必须同时上小红书**——绕不过去，早晚要做，早踩坑比晚踩坑好。但**先做 GitHub**：它零风险，可以先把整条链路跑通，再攻小红书
- 登录态 Cookie 独立文件管理，**不入库、不入 git**
- 代理池 + 行为节奏模拟（不只是限速，还要有随机间隔）
- 采集频率严格保守，宁可慢不可封
- 预留第三方数据平台方案作为 fallback

**Scrapy 独立进程带来的额外好处**：单个 spider 崩溃不影响其他平台，也不影响 workflow——可以只重跑失败的那个 spider。

### 9.2 LLM 幻觉

**问题**：LLM 容易生成听起来合理但无证据支撑的"伪需求"。

**对策**：

- 每个 idea 强制携带 `source_signal_ids`，缺失即丢弃
- 生成时把原始帖子内容作为上下文输入
- `sample_quotes` 必须是原文子串，程序化校验
- `signal_ids` 必须全部来自本批输入（LLM 归纳路线特有防护）
- 强制模型输出 `current_solutions_and_gaps`（若答不出，说明现有方案已够用 → 低置信度）
- 输出 `confidence` 而非绝对结论，定位为"假设"而非"结论"

### 9.3 打分质量 ★核心难点

**问题**：分级准确率直接决定 idea 质量，分错了后面全错。

**对策**：

- 起步用规则 + LLM 混合，规则保证下限，LLM 保证上限
- `scorer_audit` 表留存 D/E 级的规则命中明细，支持反查误杀
- **持续积累人工标注数据**（把 signal 拖到看板上人工标 A/B/C/D/E）
- 标注量到数千条后，训练轻量分类器（如 fine-tuned BERT / 逻辑回归）**替代 LLM 精排**，成本可降 90%+
- 保留 LLM 精排用于低置信度样本

### 9.4 成本失控

**对策**：见 7.2 节的多级成本控制手段 + 预算熔断。

### 9.5 SQLite 的扩展性边界

**问题**：SQLite 单写者、并发能力弱。

**对策**：

- 开启 WAL 模式 + `busy_timeout`
- Scrapy 写库与 workflow 写库错峰（这是接法 A 的天然优势——采集和清洗本来就不需要同时跑）
- 明确迁移触发条件（见 5.4.1）
- schema 用迁移脚本管理，保证迁移路径可执行

### 9.6 数据合规

| 风险点 | 应对 |
|---|---|
| 平台 ToS / robots.txt | 采集前确认各平台条款；优先用官方 API |
| 个人信息 | 用户昵称/头像脱敏或哈希；仅内部使用不外发 |
| 内容版权 | idea 中只做"引用式"摘要，不大段复制原文 |
| 对外商用 | 产品化前需法务确认 |

### 9.7 价值验证风险

**问题**：技术上跑通了，但产出的 idea 业务上没人用。

**对策**：

- Phase 1 就引入真实 PM 参与评估
- 评估标准：PM 是否愿意基于该 idea 做下一步验证动作
- 保留人工干预通道（PM 可手动提级 signal 触发 idea 生成）

---

## 10. 目录结构与代码契约

### 10.1 完整目录树

```
requirements-discovery/
├── config/
│   ├── settings.py
│   ├── loader.py
│   ├── llm.yaml
│   ├── scoring.yaml
│   ├── workflow.yaml
│   ├── keywords.yaml
│   ├── platforms/
│   │   ├── github.yaml
│   │   ├── xianyu.yaml
│   │   ├── xiaohongshu.yaml
│   │   ├── douyin.yaml
│   │   └── channels.yaml
│   └── prompts/
│       ├── pain_classify.md
│       ├── idea_generation.md
│       └── cluster_summarize.md
│
├── crawler/                     # Scrapy 实现（独立进程）
│   ├── base.py                  # BaseCrawler / Cursor 契约
│   ├── runner.py                # collect() 触发器（subprocess）
│   ├── registry.py              # 平台注册表
│   ├── spiders/                 # ★ Scrapy spider 目录
│   │   ├── github.py
│   │   ├── xianyu.py
│   │   ├── xiaohongshu.py
│   │   └── douyin.py
│   ├── pipelines.py             # Scrapy Item Pipeline → 写 raw_posts
│   ├── middlewares.py           # 限速 / 代理 / 重试 / UA 轮换
│   ├── settings.py              # Scrapy settings
│   └── common/
│       ├── ratelimit.py
│       ├── retry.py
│       ├── proxy.py
│       └── session.py
│
├── engine/
│   ├── cleaner.py               # 跨平台通用清洗：内容哈希归一 + 去重 + 文本规范化
│   ├── keyword_bank.py          # 痛点信号词库 / 噪音词库（供采集层使用）
│   ├── scorer.py                # LLM 分级 → A/B/C/D/E
│   ├── signal_store.py
│   ├── cluster.py               # LLM 主题归并（route B）
│   ├── idea_generator.py        # LLM 生成 + 六维评分
│   ├── llm_client.py
│   ├── validator.py
│   └── feedback.py              # 反哺关键词
│
├── storage/
│   ├── db.py                    # 连接 / WAL / busy_timeout
│   ├── migrations/              # ★ 迁移脚本（sqlite/ + pg/）
│   │   ├── sqlite/
│   │   └── postgres/
│   ├── raw_repo.py              # raw_posts / comments
│   ├── signal_repo.py           # signal / scorer_audit
│   ├── idea_repo.py             # product_idea / signal_idea
│   ├── cursor_repo.py           # crawl_cursor
│   ├── keyword_repo.py          # keyword_seed
│   ├── usage_repo.py            # llm_usage
│   └── run_repo.py              # run_log
│
├── workflow/
│   ├── runner.py                # 阶段编排
│   ├── stages/
│   │   ├── collect.py
│   │   ├── clean.py
│   │   ├── score.py
│   │   ├── signal.py
│   │   ├── cluster.py
│   │   └── generate.py
│   ├── cli.py                   # ★ 手动触发入口
│   └── export.py                # Markdown / CSV 导出
│
├── scheduler/
│   ├── jobs.py
│   └── runner.py                # APScheduler（Phase 3）
│
├── core/
│   ├── types.py                 # RawPost / Signal / ProductIdea
│   ├── normalize.py             # ★ 内容归一化规则（content_hash 计算）——crawler 与 engine 共用
│   ├── logging.py               # structlog
│   └── utils.py
│
├── tests/
│   ├── test_cleaner.py
│   ├── test_scorer.py
│   ├── test_validator.py
│   └── fixtures/
│
├── docs/
│   └── ARCHITECTURE.md
│
├── requirements.txt
├── pyproject.toml
└── README.md
```

### 10.2 跨层数据契约

各层之间传递的数据对象定义在 `core/types.py`：

```python
@dataclass
class RawPost:
    """统一字段。平台差异已在 spider 内部抹平。"""
    platform: str
    external_id: str
    content: str
    content_hash: str              # ★ 采集时算好即定死（core.normalize.content_hash）
    title: str | None
    author: str | None
    published_at: datetime | None
    collected_at: datetime
    url: str | None
    likes: int | None
    comments_count: int | None
    collects: int | None           # 部分平台无此指标
    search_keyword: str | None
    source_platforms: list[str]    # 初始 [platform]，跨平台去重时合并


@dataclass
class Signal:
    id: int
    raw_post_id: int
    source: str
    source_channel: str
    title: str
    body: str
    num_comments: int
    score: int
    url: str
    triage_grade: str              # A/B/C/D/E
    pain_type: str
    dollar_amounts: str | None
    content_hash: str
    # 注：归属状态在 signal_idea.is_primary，不在本表（见 6.3）


@dataclass
class ProductIdea:
    name: str
    product_idea: str
    buyer: str
    why_pay: str
    opportunity_type: str
    copilot_or_autopilot: str
    source_signal_ids: list[int]
    # 六维评分
    buyer_income: int
    usage_frequency: int
    alternative_gap: int
    buyer_unsophistication: int
    buildability: int
    integration_depth: int
    # 框架标记
    is_boring_industry: bool
    is_abandoned_work: bool
    is_already_outsourced: bool
    has_dual_signal: bool
    is_intelligence_gap: bool
    reasoning: str
    model: str
    prompt_version: str
```

### 10.3 编码约定

- Python 3.11+
- **workflow 层不做全 async**：采集走 `subprocess`（同步），LLM 调用可用 async。避免与 Scrapy 的 Twisted 模型冲突
- 类型注解必填，Pydantic v2 校验边界数据
- 日志用 `structlog`（结构化，便于检索分析）
- 配置不硬编码，一律走 `config/loader.py`
- 每个 engine 模块必须有对应单测（`tests/`）
- 爬虫实现 `BaseCrawler`，注册到 `crawler/registry.py` 即可自动接入流水线
- **DDL 只写在 `storage/migrations/`，应用代码中禁止 `CREATE TABLE`**

---

## 11. 待确认事项

> ⚠️ 本表是**需要人来拍板的开放问题**。设计层面的缺口与逻辑漏洞已在 [第 13 章 架构自评](#13-架构自评2026-11-03) 中列出（P1~P16），那部分不需要外部信息就能修。

| # | 问题 | 影响 | 建议默认 |
|---|---|---|---|
| 1 | 使用场景：个人用 / 团队用 / 产品化对外？ | 决定 UI、权限、多租户投入 | 内部团队自用 |
| 2 | LLM provider 默认用哪个？ | 影响成本与延迟 | 打分用本地 Ollama，idea 生成用云端 |
| 3 | 部署环境：本地 / 内网服务器 / 云 | 影响依赖与代理配置 | 本地起步 |
| 4 | 是否有已爬取的数据可直接用？ | 可能大幅缩短 Phase 1 | 待确认 |
| 5 | 六维评分的**系数**如何标定？ | 影响 idea 排序合理性 | 公式结构已定（见 5.3.6 / `config/scoring.yaml`），**系数本身**是先验值——Phase 2 有真实 PM 反馈后标定 |
| 6 | `is_boring_industry` 等框架标记是否单独加权？ | 影响排序 | **已定**：倍数连乘（1.5/1.4/1.3/1.5/1.2），见 `config/scoring.yaml` |
| 7 | 闲鱼/小红书的合规边界确认了吗？ | 决定这两个源能不能做 | 采集前逐个确认 ToS |
| 8 | 小红书登录态用哪个账号？ | 决定被封风险 | 待确认——账号一旦被封前期投入清零（见 9.1） |
| 9 | GitHub 用 PAT 还是匿名请求？ | 影响速率限制 | PAT（5000 次/小时 vs 匿名 60 次/小时） |

---

## 12. 相对 v1.0 的变更

| # | 项 | v1.0 | v2.0 | 原因 |
|---|---|---|---|---|
| 1 | Scrapy 接入 | 未提及 Scrapy，全篇 async 设计 | **Scrapy 独立进程**（接法 A），crawler/ 改名为 spider 目录 | Scrapy 自带调度器/下载器/Item Pipeline，无法被当函数 await；独立进程实现故障隔离 |
| 2 | 层命名 | `pipline/` | **`workflow/`** | 与 Scrapy 内部 pipeline 概念撞名 |
| 3 | 归一化位置 | crawler 层做（5.2 职责表） | **crawler 层做（spider 内部）**，清洗层只做跨平台通用规则 | 平台采集方式差异极大（JSON 路径 vs DOM 选择器），提取逻辑无法统一到配置或清洗层。详见 5.2.2.1 |
| 4 | 存储 | PostgreSQL + pgvector | **SQLite（零扩展依赖）**，可迁移 PG | 零依赖快速验证；schema 规避 PG 专属特性，保留迁移路径 |
| 5 | 主题划分 | 向量硬聚类 | **LLM 直接归纳**（无向量/聚类基础设施） | 价值锚点是 idea 质量不是主题划分质量；起步信号量小聚类易失效；相似召回留待真正需要时再引入 |
| 6 | 阶段状态机 | `pipeline_run` 表驱动状态机 | **处理标记字段驱动幂等**，`run_log` 降级为纯审计 | 阶段少且线性，状态机价值有限；标记字段让系统不依赖状态表即可正确运行 |
| 7 | 增量游标 | `crawler/common/` 横切能力，`crawl_cursor` 表 | **游标语义由各平台定义**（`BaseCrawler.getCursor/save_cursor`），存放在 storage | 平台"最后一条"语义差异大（单调 id / 时间水位线 / 雪花 id），统一字段表达不了 |
| 8 | 阶段顺序 | 含独立 SIGNAL 阶段 | collect → clean → score → signal → cluster → generate | 保持不变，但 SIGNAL 阶段的语义明确为"入库"（A/B/C 落 signal 表，D/E 落 scorer_audit） |
| 9 | 全 async 约束 | 10.3 明确"全程 async" | **取消全 async 约束**，采集走 subprocess 同步 | 与 Scrapy 的 Twisted 模型冲突 |
| 10 | 产出形态 | 未定义 | **CLI + Markdown/CSV 导出**，不做前端看板 | 用户明确选择 |
| 11 | 关键词 | 未定义来源 | **种子词 + signal/idea 反哺** | 闭合 Phase 4 飞轮 |
| 12 | LLM provider | 可插拔 | 可插拔（不变），补充分级模型策略与预算熔断配置 | 打分量大成本敏感，idea 生成量小质量敏感 |
| 13 | `signal` 字段 | 通用字段 + pain_score 等 | 补充 `source` / `source_channel` / `title` / `num_comments` / `score` / `url` / `triage_grade` / `dollar_amounts` | 满足信号识别与热度排序两类用途 |
| 14 | `product_idea` 字段 | 通用 idea 结构 | 补充 `opportunity_type` / `copilot_or_autopilot` / 六维评分 / 五个框架标记 / `reasoning` | 六维评分是排序依据，框架标记是差异化筛选维度 |
| 15 | signal ↔ idea 关联 | `signal.idea_id` 单列 + `signal_cluster` 表 | **多对多中间表 `signal_idea`**，用 `is_primary` 标记归属；移除 `signal_cluster` | 一条 signal 可能支撑多个 idea；`is_primary` 保持幂等判定单一 |
| 16 | 表清单 | 6 张 | **10 张**：新增 `comments` / `signal_idea` / `keyword_seed` / `llm_usage` | 评论表提前建（Phase 3 启用）；关键词库与 LLM 记账是反哺飞轮和预算熔断的基础 |
| 17 | 阶段执行契约 | 未定义 | **明确"捞一次→处理完→返回"，禁止 `while` 循环** | 等待交给 scheduler 频率；避免阻塞整个 workflow |
| 18 | `product_idea.cluster_id` | 有 | **移除** | 无簇概念 |
| 19 | `field_mapping` 配置 | v2.0 初稿有（平台 yaml 里的字段路径映射） | **移除** | DOM 提取无法用路径表达；JSON 平台的类型转换/缺失兜底也得写代码。配置化只在"逻辑一致只是名字不同"时有收益，而这里不成立 |
| 20 | 互动量阈值 | 全局单一 N（`likes+comments+collects > N`） | **按平台配置权重**（`config/platforms/*.yaml` 的 `engagement` 段） | 各平台指标不齐：抖音/闲鱼/GitHub 无收藏数，微博用 `attitudes` |
| 21 | `raw_payload` | 平台完整响应 | **不存储**（spider 解析成统一字段后原始 JSON 即丢弃） | 完整响应 80%+ 是无用内容；更关键的是它换不回"当初就没存的字段"——而统一字段本身已是重新解析后的结果。`url` 始终保留，需要时回源重取 |
| 22 | `content_hash` | 未明确 | **明确不可变**（normalize 规则变更不得回溯重算） | 重算会导致唯一索引冲突 |
| 23 | `raw_posts` 派生列 | 归一化字段由清洗层回填 | **无派生列**，全部由 spider 采集时填入 | "原始不可变"语义完全成立 |
| 24 | 归一化规则位置 | 隐含在 `engine/cleaner.py` | **抽到 `core/normalize.py`**，crawler 与 engine 共用 | spider 要算 hash、cleaner 要用同一套规则；留在 engine 会让 crawler 反向依赖 engine，违反 3.2 硬约束（自评 P2） |
| 25 | `raw_posts.source_platforms` | 未定义 | **新增字段**（JSON 数组），初始 `[platform]` | 跨平台搬运时保留来源强度信号；也是 P1 方案 A 的载体（自评 P3） |
| 26 | 归并门槛判定时机 | 未明确 | **全池判定**，分组后不足 3 条的合并成跨平台小批次 | 分组后判定会让只有 2 条信号的平台永远 `skipped`，信号永久卡死（自评 P6） |
| 27 | idea 输出契约 | 缺 `sample_quotes` / `current_solutions_and_gaps` | **补为必填** | 5.3.7 的校验规则依赖这两个字段，缺了校验形同虚设（自评 P11） |
| 28 | `candidates` | 架构图中的独立节点 | **移除**（过滤前置后无独立含义，见第 39 条） | 落库即候选，中间态不再需要 |
| 29 | `raw_posts.content_hash` 索引 | UNIQUE | **普通索引**（去重在清洗层按 hash 分组做） | 同内容同平台多条是常态（转发/重复上架/duplicate issue/刷屏），唯一索引会让第二条 insert 直接失败，"留给清洗层去重"这条路是断的（自评 P1，采纳方案 A） |
| 30 | `signal.content_hash` 索引 | UNIQUE | UNIQUE（**保持**） | signal 层已去重过，与 raw_posts 的不唯一语义不同——分层意图，不是设计不一致 |
| 31 | 采集层过滤 | 未定义（全部落库后清洗） | **新增采集层硬门槛 + 三层短语过滤**，不通过的内容不落库 | 存储量是小时级只增不减的，而采集量是分钟级会变大的（关键词反哺后搜索面变宽）。见 5.2.5 / 5.3.2 |
| 32 | 短语规则集 | 未定义 | **移植 `albertorsesc/pain-discovery` 的三层过滤**，分 `config/phrases/{en,zh}/`，做三处修正 | 参考实现只做了美元信号，漏掉"tool signal"，中文高频求助句式会全部漏杀 |
| 33 | 平台语言绑定 | 未定义 | 平台 yaml 加 `lang: en\|zh`，spider 按此加载规则集 | 中英文表达结构差异极大，一套规则覆盖不了 |
| 34 | 互动量门槛逻辑 | 权重按平台配（但阈值语义未明） | **阈值按平台配置且为 OR 关系**（`likes>=5` 或 `comments>=3`） | 痛点帖典型特征是评论多、点赞少；用 AND 会系统性漏掉最典型的求助帖 |
| 35 | 六维评分排序公式 | 文字描述（"加权和 + 框架加成"） | **定死**：`base = 六维乘积`、`mult = 框架倍数连乘`、`total = base × mult`，落到 `config/scoring.yaml` | 乘法保证 base 是主导项；加法会让"框架全中但六维都是 2 分"的 idea 排在"六维 5 分但无框架"前面 |
| 36 | `product_idea` 评分列 | 只有六维 + 五个标记 | **新增 `total_score`（REAL）+ `scoring_version`** | 排序直接 `ORDER BY total_score`，不在应用层重算；版本号防止公式调整后历史数据语义漂移 |
| 37 | Phase 1 平台 | GitHub 单平台 | **GitHub + 小红书**（中文英文各一个） | 一个平台检验不了一整套：两个平台走同一套代码路径下的两套规则集，才能确认「中文规则准不准」 |
| 38 | LLM Prompt | 文字描述 | **落到 `config/prompts/`**：`triage.md` / `triage.reference.md` / `idea_generation.md` | prompt 是核心资产，必须能版本化、A/B 对比。分级 prompt 按我们的 A~E 定义重写（参考实现的 C/D 语义不同） |
| 39 | 打分分层 | 三段式（采集层硬门槛 → 规则召回 → LLM 精排） | **两段式（采集层过滤 → LLM 分级）** | 噪音过滤全部前置到采集层，规则召回灰区层删除，落库数据直接送 LLM 定级。规则只有一处、链路更短；代价是 LLM 调用量上升，换来的是可解释性与可调性 |

### 12.1 v1.0 中被保留的部分

- 三层过滤思想（规则 → 模型 → LLM）与成本控制策略（7.2）
- A/B/C/D/E 分级标准与占比预估（5.3.3）
- `source_signal_ids` 强制溯源 + `sample_quotes` 原文子串校验
- Prompt 资产管理（独立文件 + 版本号 + A/B + few-shot）
- 实施路线的 Phase 划分与"Phase 1 不做多平台/调度/UI"的判断
- 反爬、幻觉、成本、合规四类风险的对策
- 原始层不可变、处理幂等、分层过滤、溯源强制四条数据流原则

---

## 13. 架构自评（2026-11-03）

> 这是在 v2.0 设计完成后做的一轮对抗性自检。列出的是**设计本身的缺口**，不是实现细节。按"会不会让项目跑不起来 / 跑起来但拿不到价值"排序。
>
> 标注：🔴 阻塞级（Phase 1 就会踩）· 🟡 重要级（Phase 2 前必须定）· 🟢 优化级（可延后）
>
> **状态**：P1~P6 / P10 / P11 / P12 已定或已修（文档已改）。**P1 用户已拍板：采纳方案 A**（2026-11-03）。**P4 / P5 已于 2026-11-04 定稿**（门槛拆采集层/引擎层两层；排序公式写进 `config/scoring.yaml`）。P7~P9 / P13~P17 待处理。

### 13.1 🔴 阻塞级问题

#### P1. `content_hash` 的唯一索引与"同平台同内容多条帖子"直接冲突 ✅ 已决策

**问题**：`uk_raw_hash ON raw_posts(content_hash)` 是全局唯一索引。但真实场景里**同一个内容在同一平台出现多次**是常态：

- 小红书"转发"（同一条笔记被搬运，标题正文一致，`external_id` 不同）
- 闲鱼同一卖家重复发布同一件商品（改个标题重新上架）
- GitHub 同一 issue 被开成多个（duplicate issue 正文几乎一致）
- 微博刷屏（同一段话被不同账号发）

**当前设计的处理**：SCHEMA.md 3.1 写了"靠 `uk_raw_platform_extid` 判断是否已采过，`uk_raw_hash` 的冲突留给清洗层处理"——但这是**错的**。清洗层拿到的那条数据**根本没进库**（insert 就失败了），它不知道这个 hash 已存在，无法"跳过并记录到 `scorer_audit`"。这条路径实际是断的。

**✅ 决策：采纳方案 A（2026-11-03 用户确认）**

**三个可选方案**：

| 方案 | 做法 | 结论 |
|---|---|---|
| **A. hash 不唯一，加 `source_platforms`** | `uk_raw_hash` 改为普通索引；跨平台重复靠清洗层查已存在 hash 后合并 `source_platforms` | ✅ **采纳（2026-11-03）** |
| **B. hash 加平台前缀** | `hash = sha256(platform + normalized_content)` | ❌ 跨平台去重能力直接消失，违背设计初衷 |
| **C. 部分唯一索引** | `CREATE UNIQUE INDEX ... ON raw_posts(content_hash) WHERE is_primary_dedup = 1` | ❌ 多一个状态列，多一层状态 |

**采纳 A 的理由**：

- 跨平台搬运检测是真实价值——一条抱怨在 xhs 和微博都出现 = 信号强度更高，不能丢
- "同平台同内容多条"应该**全部保留**（它们是不同帖子的不同互动数据，转发量本身是热度信号），只是不重复进 signal

**方案 A 的落地规则**：

| 场景 | 处理 |
|---|---|
| 同平台同 hash 多条（如转发） | **全部留在 `raw_posts`**（各是独立帖子，互动数据不同）。清洗层按 hash 分组，组内按互动量取**代表**进 `signal`，其余记 `scorer_audit` |
| 跨平台同 hash | 合并进 `raw_posts.source_platforms`（如 `["xiaohongshu", "weibo"]`）。signal 取代表时能看到"出现在 2 个平台" = 强度加成 |
| 同平台同 `external_id` | 由 `uk_raw_platform_extid` 在**采集层**拦截（爬虫重试、翻页重叠） |

**连带改动**：

1. `uk_raw_hash` → 普通索引 `idx_raw_hash`
2. `signal` 表的 `uk_signal_hash` **保留唯一**——signal 层已按 hash 去重过，进 signal 的每个 hash 天然唯一。这是去重**之后**的结果，与 raw_posts 语义不同
3. 清洗层需要"按 hash 分组取代表"的查询（`GROUP BY content_hash`）
4. `source_platforms` 落地为 `raw_posts` 字段（已加，见 P3）

> **两个 hash 唯一性的语义区分**（不是设计不一致，是分层意图）：
> - `raw_posts.content_hash` —— **允许重复**（原始层保留全部采集事实）
> - `signal.content_hash` —— **唯一**（信号层已去重，每条 signal 代表一个唯一内容）

#### P2. 归一化移到 spider 后，"清洗"阶段的职责边界出现了空洞

**问题**：原来清洗层负责"归一 + 去重 + 噪音过滤"三件事。现在归一挪到 spider，清洗层只剩"去重 + 噪音过滤"。但 5.3.1 的数据流图里，**清洗层仍然画着"内容哈希归一化 → content_hash"**——而 `content_hash` 现在必须在 spider 产出时就算出来（SCHEMA.md 3.1 已注明"计算后不可变"）。

**具体矛盾**：
- `content_hash` 由 spider 算 → spider 就必须知道归一化规则（去空格、去表情、NFKC、lower）
- 归一化规则写在 `engine/cleaner.py` → **engine 反向依赖 crawler**，违反 3.2 的单向依赖硬约束

**建议**：把归一化规则抽成独立模块 `core/normalize.py`（或 `core/` 下的纯函数），**crawler 和 engine 都依赖 core**，core 不依赖任何层。这样：
- spider 调 `core.normalize.content_hash(text)` 算 hash
- 清洗层调同一函数做内容清洗
- 规则只有一份，两边不会漂移

**这是本次架构调整引入的新问题，必须解决**——否则 3.2 的依赖硬约束在代码层面就破了。

> ✅ **已修**（2026-11-03）：归一化规则已抽到 `core/normalize.py`，`core` 成为"不依赖任何层、只被依赖"的例外层（见 3.2 依赖图）。spider 与 cleaner 共用同一份 `normalize()` / `content_hash()`。

#### P3. `raw_posts.source_platforms` 在 SCHEMA.md 里不存在

**问题**：ARCHITECTURE.md 5.3.1 明确要求"跨平台重复的内容只保留一条 `raw_posts`，但需要记录它出现在哪些平台 → `raw_posts.source_platforms`"。但 SCHEMA.md 3.1 的字段表里没有这一列。

**连带影响**：如果采纳 P1 的方案 A，`source_platforms` 就成了跨平台去重的**载体**——不落这个字段，去重信息就丢了，signal 层拿不到"这条抱怨出现在 3 个平台"这个强度信号。

**建议**：在 SCHEMA.md 3.1 补上 `source_platforms TEXT(JSON)` 字段（JSON 数组，存平台名列表），初始值 = `[platform]`。

> ✅ **已修**（2026-11-03）：SCHEMA.md 3.1 已加该字段，`core/types.py` 的 `RawPost` 也加了 `source_platforms: list[str]`。**但仍取决于 P1 选哪个方案**——若最终选 B（hash 加平台前缀），跨平台去重能力消失，此字段的合并逻辑就不再需要。

### 13.2 🟡 重要级问题

#### P4. 平台配置里"互动量权重"和"入库门槛"是两回事，但只有前者

**问题**：5.1.1 的平台 yaml 里有 `engagement: {base, likes, comments, collects}`，5.3.2 用它算热度分。但**没有配置"多少分才进 signal 表"**——各平台的量级差异极大：

| 平台 | 典型点赞量级 | 典型评论量级 |
|---|---|---|
| GitHub issue | 0~50 | 0~20 |
| 小红书笔记 | 10~2000 | 0~300 |
| 抖音视频 | 1万~100万 | 100~5万 |

同一套权重公式在不同平台上算出来的分差几个数量级。`base: 30` 在 GitHub 上是"高热"，在小红书上什么都不算。

**建议**：平台 yaml 加 `signal_threshold`（进 signal 的热度分门槛），与 `engagement` 权重配套。评分体系用**平台内相对分位**（如"该平台该关键词下热度前 10%"）比绝对值更稳健。

> **✅ 已决策并修（2026-11-04）**：门槛分成两层，各自落位——
>
> | 层 | 位置 | 配置项 | 作用 |
> |---|---|---|---|
> | **采集层硬门槛** | crawler，写库前 | `filter: {min_likes, min_comments, title_length, min_body_length}` | 控制存储量。见 5.3.2 ① |
> | **引擎层分级** | engine，LLM 精排 | `grade IN ('A','B','C')` | 决定是否进 signal 表 |
>
> `engagement` 权重仍留在 `engagement` 段，用于算热度分做**排序**（同平台内比），不做**准入**（跨平台量级不可比）。见 5.3.2。
>
> 平台内相对分位（热度前 10%）作为 Phase 2 的优化项保留，不阻塞 Phase 1。

#### P5. 六维评分的排序权重从未定义，但 5.3.6 直接用了

**问题**：5.3.6 说"默认排序 = `buyer_income × usage_frequency × alternative_gap` 加权和，再叠加框架标记加成"。但：
- 三个维度的**权重**没写（是等权？1:2:3？）
- 另外三个维度（`buyer_unsophistication` / `buildability` / `integration_depth`）**完全没进排序公式**——它们只是展示？
- "框架标记加成"具体加多少分没写

**建议**：Phase 2 前把排序公式写死进 `config/scoring.yaml`，否则同一个 idea 列表每次排序结果可能不同（如果靠 LLM 排），或者所有人各排各的。

> **✅ 已决策并修（2026-11-04）**：公式定死并落到 `config/scoring.yaml`——
>
> ```
> base  = 六维乘积（全部六维进公式，不再有"只展示"的维度）
> mult  = 框架标记倍数连乘（1.5 / 1.4 / 1.3 / 1.5 / 1.2）
> total = base × mult
> ```
>
> 落库为 `product_idea.total_score`，配 `scoring_version`。`method` 可切 `weighted_sum`（权重见 yaml）。见 5.3.6。
>
> **⚠️ 系数本身仍是先验值**，需 Phase 2 有真实 PM 反馈后标定——但公式结构已定，不会再出现"每次排序结果不同"。

#### P6. 归并阶段按 `source` 分组，但"同平台不足 3 条"时永远凑不够

**问题**：5.3.5 第 3 步"按 `source` 分组，每组最多取 N 条送入 LLM"，第 2 步"数量 < 门槛（默认 3）→ 跳过本轮"。

如果门槛判定是在**分组之后**做的，那么某平台上只有 2 条未归并信号时，**永远无法触发归并**——它一直凑不够 3 条，会一直 `skipped`，永远卡在那。而"门槛 < 3"这个条件如果是在**分组之前**（全池 ≥3），那一个平台上 2 条 + 另一个平台 1 条会被强行归并（质量差，且跨平台的相似度低）。

**这是一个真的会卡死系统的逻辑漏洞。**

**建议**：明确门槛语义。建议改成——**全池 ≥3 条即触发**（不分组），LLM 自己判断哪些能归到一起、哪些不能（它有 `signal_ids` 校验机制兜底）。或者：分组后单组不足 3 条的，把所有不足 3 条的组合并成一个"跨平台小批次"送 LLM。

#### P7. `signal_idea.is_primary` 的"一条 signal 至多一个 primary"约束与"重新归并"冲突

**问题**：`uk_signal_primary ON signal_idea(signal_id) WHERE is_primary = 1` 保证一条 signal 只有一个 primary 归属。但现实中：
- PM 发现某个 idea 质量差，想推翻重做 → 需要把该 idea 下的 signal 释放出来重新归并
- 释放 = 删 `signal_idea` 行，或把 `is_primary` 置 0

如果置 0，这条 signal 就变成"未归并"（因为幂等查询找的是 `NOT EXISTS(... is_primary = 1)`），会**被再次归并到别处**——但它同时还挂在原 idea 上作为 supporting。这可能是想要的（一条 signal 支撑多个 idea），也可能不是。

**建议**：明确"重新归并"是显式操作（人工触发的一次性任务），而不是幂等查询的副产品。在 workflow 里加一个 `--recluster` 参数，走专门的路径：先清理关联，再归并。文档里写清楚。

#### P8. `collect` 阶段的幂等依据是游标，但游标语义是"平台自定义"的 —— 缺一个统一的契约

**问题**：`crawl_cursor.payload` 是 JSON，"不透明，各平台自定义"。但 workflow 层要判断"collect 阶段是否已完成"（5.5.2 的 `stage.is_done()`），这需要知道各平台的"完成"是什么意思。5.5.1 的表里 collect 一行写的是"游标水位线"——但对 GitHub（单调 issue number）和小红书（时间水位线），"水位线之上就是采完了"这个判断逻辑是平台特有的。

**建议**：定义 `BaseCrawler` 的一个统一方法即可，例如 `is_caught_up(cursor_payload) -> bool`，或更简单地——**collect 阶段永远视为"已完成"（skipped），由 scheduler 的 cron 决定何时真跑**。因为 collect 有副作用，不能像其他阶段那样用"捞一次"的方式幂等。

#### P9. `run_log` 的 `stage` 字段与实际阶段语义对不齐

**问题**：`run_log.stage` 的取值注释写的是 `collect / clean / score / signal / cluster / generate`（6 个）。但 5.5.1 的幂等表把 SIGNAL 阶段写成"scorer_audit 表"（即 signal 阶段与 score 阶段同批完成），把 CLUSTER+GENERATE 写成同一依据。

**建议**：要么把 6 个阶段改成 4 个真实的执行单元（collect / clean / score / cluster+generate），要么在文档里说明 signal 和 generate 是"逻辑阶段"而非"独立执行单元"，`run_log` 按执行单元记。**现在的写法会让人以为要跑 6 个阶段。**

#### P10. `candidates` 概念只存在于架构图里，没有对应的表或状态 ✅ 已随链路简化移除

**原问题**：4.1 的数据流图画了 `candidates`（候选池，清洗后评分前）。但 SCHEMA.md 的 10 张表里**没有 candidates 表**，也没有对应的状态标记。实际上"清洗后、评分前"就是 `raw_posts` 里 `cleaned_at IS NOT NULL AND scored_at IS NULL` 的行，所以 `candidates` 是一个**逻辑视图**，不是实体。

> ✅ **已修**（2026-11-03）：4.1 数据流图已标注为逻辑视图，并给出对应的 WHERE 条件。
>
> ✅ **已彻底移除**（2026-11-05）：过滤全部前置到采集层后，"清洗后、评分前"这个中间态不再有独立含义——落库即候选。4.1 图中的 `candidates` 节点与规则召回层一并删除，链路简化为 `raw_posts → LLM 分级 → signal`。

#### P11. `sample_quotes` / `current_solutions_and_gaps` 在 idea 的输出契约里没有，但校验规则里有

**问题**：5.3.7 的校验表要求"`sample_quotes` 必须是源 signal 内容的子串"、"强制模型输出 `current_solutions_and_gaps`"。但 5.3.5 的**输出契约 JSON 里没有这两个字段**（只有 name / product_idea / buyer / why_pay / opportunity_type / copilot_or_autopilot / signal_ids）。而 SCHEMA.md 的 `product_idea` 表**有** `current_solutions_and_gaps` 和 `market_evidence`（内含 sample_quotes）。

**建议**：输出契约 JSON 补齐这两个字段（`sample_quotes: [...]`、`current_solutions_and_gaps: {existing, gaps}`），否则校验规则永远校验不到东西。

> ✅ **已修**（2026-11-03）：5.3.5 的输出契约 JSON 已补这两个字段并标为必填。

### 13.3 🟢 优化级问题

#### P12. `embedding` 字段现在就建，但 Phase 3 才用

**✅ 已修（2026-11-04）**：`signal.embedding` 列与相关向量扩展依赖全部移除，`storage` 只用标准 SQLite，Phase 1 无需处理任何扩展加载问题。

**建议**（保留备查）：若将来真出现"idea 重复"的问题，届时作为**独立的召回层**引入相似度召回——那是新需求下的新决策，不在当前架构预留位置。

<!-- P12 已闭合。若将来重新引入相似度召回，另起新问题条目，不要在此复活原设计。 -->

#### P13. `comments` 表已建但 Phase 3 才用，且体量是 `raw_posts` 的 50~100 倍

一条帖子 50~500 条评论。爬 10 万条帖子 = 500 万~5000 万条评论。这个体量在 SQLite 上会成为迁移到 PG 的**首要原因**，比 `raw_posts` 的 1000 万行更早触发。

**建议**：comments 单独考虑存储策略（是否只存高分帖子的评论），并在 Phase 3 之前明确"评论只对高互动帖子采集"这个策略。

#### P14. `dollar_amounts` 的提取时机没定

`signal.dollar_amounts` 是"付费意愿的最强信号"，5.3.5 说它要单独注入 prompt。但**谁来提取**没说：
- 规则提取？需要一套金额正则（¥5000、5k-8w、"一百块"、"包月 30"）
- LLM 提取？

**建议**：规则提取 + LLM 提取并存，规则作为兜底。定义在 `scoring.yaml` 的金额模式里。

> **简化（2026-11-05）**：删除规则召回层后，**每条 signal 都必然经过 LLM 分级**，所以"LLM 提取可能跑不到"这个顾虑消失了。规则提取仍值得保留——它零成本、可解释，且在 LLM 超时降级时兜底。

#### P15. `engine/feedback.py` 在目录树里，但 Phase 4 才有

不急，但注意别在 Phase 1 写个空壳。

#### P16. `config/keywords.yaml` 和 `keyword_seed` 表职责重叠

**问题**：10.1 目录树里有 `config/keywords.yaml`，SCHEMA.md 又有 `keyword_seed` 表（种子词 + 反哺词）。两处都存关键词，**谁是权威源？**

**建议**：`keyword_seed` 表是权威源（它能记录 `hit_count` / `yield_rate` 这些运行时统计），`keywords.yaml` 只作为**初始导入源**（首次启动时 seed 进表）。要么就砍掉 yaml，要么明确这个导入关系。

#### P17. 「未命中短语但互动量高」的抽样样本存哪儿没定

**问题**：5.2.5 要求「抽样保留『未命中但互动量高』的内容，作为词库召回率的对照样本」。但**存哪儿没定**：

- 存进 `raw_posts`？那 `raw_posts` 就不再纯粹是"通过了采集层门槛的数据"，采集层过滤的"不通过不落库"语义被破坏
- 单独一张表？多一张表的运维成本，且抽样样本本来是**临时对照**不是长期资产

**核心矛盾**：这条需求本身跟"控制存储量"是**反向**的——它主动保留本该被丢弃的数据。如果放进 `raw_posts`，就等于在存储层开了个"什么数据都存"的口子。

**建议**：抽样样本**不进 `raw_posts`**，走独立的采样落盘（按天写 JSONL，滚动保留 7~14 天），不进 SQLite。理由：
- 它的用途是**调词表时的短期对照**，调完即弃，不该付长期存储成本
- 不污染 `raw_posts` 的语义，"不通过不落库"这条硬约束保持成立
- JSONL 天然可追加、可按天滚动删除，不需要表和索引

**待定**：抽样比例（按未命中条数百分比还是固定条数）、保留天数，写在 `config/platforms/*.yaml` 的 `filter` 段。

### 13.4 架构上做得对的地方（不用改）

| 设计 | 为什么是对的 |
|---|---|
| 幂等靠标记字段而非状态机表 | 系统正确性不依赖 `run_log`，表坏了也能跑 |
| 阶段"捞一次→处理完→返回" | 避免 workflow 被卡死，调度权清晰归 scheduler |
| Scrapy 独立进程 | 故障隔离 + 绕开 Twisted/asyncio 冲突 + 绕开 SQLite 单写者 |
| 归一化在 spider 内 | DOM 提取和 JSON 提取本质上没法统一，配置化不成立 |
| 六维评分拆独立列 | 排序/筛选是纯 SQL，不用解析 JSON |
| `source_signal_ids` 快照 + `signal_idea` 实时关联并存 | 能区分"模型当时引用了什么"和"现在关联着什么" |
| 部分唯一索引 `uk_signal_primary` | 一条 SQL 表达归属唯一性，幂等判定保持单一 |
| Phase 1 先做 GitHub 再攻小红书 | GitHub 零反爬风险，可先把整条链路跑通；小红书必须做，早踩坑比晚踩坑好 |
| 不做前端看板，CLI + Markdown 导出 | 把精力留给 idea 质量这个真正的成败点 |

### 13.5 建议的处理顺序

| 优先级 | 项 | 为什么这个顺序 |
|---|---|---|
| 1 | **P2**（归一化规则抽 `core/normalize.py`） | 依赖硬约束破了，必须在写第一行 spider 前解决 ✅ **已修** |
| 2 | **P1 + P3**（hash 唯一索引冲突 + `source_platforms`） | 表结构一落地就改不动，越晚改代价越大 ✅ **已决策并修**（方案 A） |
| 3 | **P6**（归并门槛卡死） | 逻辑漏洞，会让系统静默不产出 ✅ **已修** |
| 4 | **P11**（输出契约缺字段） | 改 prompt 比改表便宜，但漏了校验形同虚设 ✅ **已修** |
| 5 | **P4**（平台阈值） | 门槛拆采集层/引擎层两层，配置落位 ✅ **已修**（2026-11-04） |
| 6 | **P5**（排序权重） | 公式写死进 `config/scoring.yaml`，落 `total_score` 列 ✅ **已修**（2026-11-04） |
| 7 | P7 / P8 / P9 / P10 | 实现到对应阶段时自然澄清（P10 已标注） |
| 8 | P13~P17 | 优化级，Phase 3 前后处理 |

> **阻塞级（🔴）与重要级（🟡 P4/P5）已全部清空。** 现在可以进入建表与 Phase 1 骨架搭建。剩下的是 🟡 重要级（P7~P9）与 🟢 优化级。

---

## 14. 配套文档

| 文档 | 内容 |
|---|---|
| [SCHEMA.md](./SCHEMA.md) | 全部 10 张表的字段级定义、索引、幂等依据、SQLite→PG 迁移映射 |
| [ARCHITECTURE.v1.0.md](./ARCHITECTURE.v1.0.md) | v1.0 归档版，供追溯设计演进 |

---

*文档结束（2026-11-05 更新）。设计侧的自评问题 P1~P6 / P10~P12 已全部修完，架构定型。*

*下一步：*
1. *把 SCHEMA.md 落成 `storage/migrations/sqlite/001_init.sql`（DDL 只写在这里，应用代码禁止 `CREATE TABLE`）*
2. *补 `core/normalize.py`（归一化规则，spider 与 cleaner 共用）*
3. *Phase 1 骨架：`core/phrases.py`（✅ 已有）→ storage schema → `crawler/base.py` + `github.py` + `xiaohongshu.py` → `engine/cleaner.py` + `scorer.py` → `workflow/runner.py`*

*配置与 Prompt 已就位：`config/phrases/{en,zh}/`、`config/platforms/{github,xiaohongshu}.yaml`、`config/scoring.yaml`、`config/prompts/{triage,triage.reference,idea_generation}.md`*
