# 数据表结构设计

> 版本：v1.1
> 日期：2026-11-03
> 配套文档：[ARCHITECTURE.md](./ARCHITECTURE.md) v2.1
> 状态：设计稿（待落成 `storage/migrations/sqlite/*.sql`）
> ✅ **P1 已决策**：`content_hash` 唯一索引改为普通索引（方案 A，2026-11-03）。**建表前仍需读** [ARCHITECTURE.md 第 13 章 架构自评](./ARCHITECTURE.md#13-架构自评2026-11-03)。

---

## 目录

1. [设计约定](#1-设计约定)
2. [表清单与关系图](#2-表清单与关系图)
3. [表结构详解](#3-表结构详解)
   - [3.1 raw_posts](#31-raw_posts--原始层)
   - [3.2 comments](#32-comments--评论层)
   - [3.3 signal](#33-signal--信号层-核心)
   - [3.4 product_idea](#34-product_idea--产品机会层-核心)
   - [3.5 signal_idea](#35-signal_idea--信号与机会关联表)
   - [3.6 scorer_audit](#36-scorer_audit--规则审计)
   - [3.7 crawl_cursor](#37-crawl_cursor--增量游标)
   - [3.8 run_log](#38-run_log--运行记录)
   - [3.9 keyword_seed](#39-keyword_seed--关键词库)
   - [3.10 llm_usage](#310-llm_usage--LLM 调用记账)
4. [幂等依据汇总](#4-幂等依据汇总)
5. [索引策略](#5-索引策略)
6. [SQLite → PostgreSQL 迁移映射](#6-sqlite--postgresql-迁移映射)
7. [待定项](#7-待定项)

> ✅ **P1 已决策**（方案 A）：`raw_posts.content_hash` 用普通索引，去重在清洗层做。见 3.1 的说明。

---

## 1. 设计约定

### 1.1 通用约定

| 约定 | 说明 |
|---|---|
| **主键** | SQLite 用 `INTEGER PRIMARY KEY AUTOINCREMENT`；PG 用 `BIGSERIAL` |
| **时间戳** | 一律 `TIMESTAMPTZ`（SQLite 存 ISO8601 字符串，PG 用原生类型） |
| **布尔** | SQLite 用 `INTEGER`（0/1）；PG 用 `BOOLEAN` |
| **JSON** | SQLite 用 `TEXT`（存 JSON 字符串）；PG 用 `JSONB` |
| **数组** | SQLite 用 `TEXT`（存 JSON 数组字符串）；PG 用原生数组类型 |
| **命名** | 表名、字段名全小写 snake_case；索引名 `idx_<表>_<用途>`；唯一索引 `uk_<表>_<字段>` |
| **金额** | 一律 `VARCHAR` 存原始文本（"5000-8000/套"），**不用数值类型**——金额单位/币种/区间语义复杂，数值化会丢失信息 |

### 1.2 三条硬约束

**① 幂等靠业务字段上的标记，不靠状态机表**

每个阶段都有一个"处理标记"字段。查询待处理项 = `WHERE 标记 IS NULL`。这使得 `run_log` 损坏不影响系统正确运行。

**② 溯源强制**

`product_idea` 必须通过 `signal_idea` 关联表挂上至少一条 `signal`。生成后校验通过才写入关联。

**③ 原始层不可变**

`raw_posts` / `comments` 的**统一字段（`title` / `content` / `likes` / `comments_count` / `collects`）由 spider 在采集时一次性填入**，不是事后回填的派生列——没有"清洗层改写原始行"这件事发生。平台原始 JSON 解析完即丢弃，**不落库**（理由见 ARCHITECTURE.md §5.2.2.3）。

> **唯一的例外**：`raw_posts.source_platforms` 会被清洗层 UPDATE（合并跨平台重复内容的平台列表）。这不违反不可变语义——它记录的是**观察结果**（这条内容出现在哪些平台），不是原始采集数据的改写。

### 1.3 多对多关联的设计说明

`signal` 与 `product_idea` 采用多对多（中间表 `signal_idea`），理由：

- 一条 signal 可能支撑多个 idea（如"账号系统太难用"同时支撑"SSO 工具"和"权限管理工具"）
- 一个 idea 的证据可能来自多个平台的多条 signal
- LLM 有时会把内容相近但归属不同的 signal 归到同一个 idea

**代价：幂等判定需要额外处理。** 单列 `signal.idea_id` 时判定很简单，多对多时需要额外约定：

> **归属状态由 `signal_idea.is_primary` 决定。** 一条 signal 至多有一个 `is_primary = 1` 的关联行，该行表示"这条 signal 当前归属的 idea"。归并阶段通过 `NOT EXISTS (SELECT 1 FROM signal_idea WHERE signal_id = s.id AND is_primary = 1)` 找出未归并的 signal。

这样既保留了多对多的灵活性，又保持了幂等判定的单一性。`is_primary = 0` 的行表示"该 signal 也作为次要证据出现在这个 idea 里"。

---

## 2. 表清单与关系图

### 2.1 表清单

| 表 | 作用 | 层级 | 写入方 |
|---|---|---|---|
| `raw_posts` | 原始帖子（含平台原始 payload） | 原始层 | crawler |
| `comments` | 评论（Phase 3 启用） | 原始层 | crawler |
| `signal` | 评分后的信号 ★ | 信号层 | engine/scorer |
| `product_idea` | 产品机会 ★ | 机会层 | engine/idea_generator |
| `signal_idea` | signal ↔ idea 多对多关联 | 关联层 | engine/cluster |
| `scorer_audit` | D/E 级留痕（规则调参数据源） | 审计层 | engine/scorer |
| `crawl_cursor` | 增量采集游标 | 采集层 | crawler |
| `run_log` | 阶段运行记录 | 运维层 | workflow |
| `keyword_seed` | 种子关键词 + 反哺关键词 | 配置层 | engine/feedback |
| `llm_usage` | LLM 调用与 token 记账 | 运维层 | engine/llm_client |

### 2.2 关系图

```
                        ┌─────────────┐
                        │  raw_posts  │ ← crawler 写入(原始payload)
                        └──────┬──────┘
                               │ 1:N
                        ┌──────▼──────┐
                        │  comments   │ ← crawler 写入(Phase 3)
                        └─────────────┘

                        ┌─────────────┐
                        │   signal    │ ← engine/scorer 写入(A/B/C)
                        └──────┬──────┘
                               │ N:M
                        ┌──────▼──────┐
                        │ signal_idea │ ★ is_primary 标记归属
                        └──────┬──────┘
                               │ N:1
                        ┌──────▼──────┐
                        │ product_idea│ ← engine/idea_generator
                        └─────────────┘

    独立表：scorer_audit / crawl_cursor / run_log / keyword_seed / llm_usage
```

### 2.3 数据流与表的关系

```
crawler  ──写──> raw_posts(已归一)
                      │
                      │ 打分
                      ▼
              ┌───────────────┬──────────────┐
              │   signal      │ scorer_audit │
              │  (A/B/C)      │   (D/E)      │
              └───────┬───────┴──────────────┘
                      │ 归并
                      ▼
              ┌──────────────┐      ┌──────────────┐
              │ product_idea │─N:M─>│ signal_idea  │
              └──────────────┘      └──────────────┘
```

---

## 3. 表结构详解

### 3.1 `raw_posts` — 原始层

**粒度**：一条平台原始帖子。

**写入方**：crawler（Scrapy spider，**采集时已完成平台内归一**）。**更新方**：仅 workflow 写 `cleaned_at` / `scored_at` 两个幂等标记，业务字段不回填。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | 平台标识，如 `xiaohongshu` / `xianyu` / `github` |
| `channel` | VARCHAR(128) | Y | ★ 平台内频道/分区/话题：reddit 的 subreddit、github 的 repo、小红书的话题 tag、Trustpilot 的公司名。无分区概念的平台留空 |
| `external_id` | VARCHAR(128) | N | 平台内唯一 ID |
| `content_hash` | VARCHAR(64) | N | SHA256，用于跨平台去重与幂等 |
| `search_keyword` | VARCHAR(128) | Y | 命中的检索词 |
| `url` | TEXT | Y | 原文链接 |
| `author` | VARCHAR(128) | Y | 昵称（**采集时即脱敏**） |
| `published_at` | TIMESTAMPTZ | Y | 发布时间（平台原始值，可能缺失） |
| `collected_at` | TIMESTAMPTZ | N | 采集时间 |
| `source_platforms` | TEXT(JSON) | Y | ★ 该内容出现过的平台列表（初始 = `[platform]`）。跨平台搬运时合并，见 ARCHITECTURE.md 自评 P1/P3 |
| `title` | TEXT | Y | ⬇ 以下由 spider 采集时填入（平台差异已在 spider 内抹平） |
| `content` | TEXT | Y | 归一化正文（纯文本，去 HTML/水印） |
| `likes` | INTEGER | Y | 点赞数；平台无此指标 → NULL |
| `comments_count` | INTEGER | Y | 评论数（平台口径，非本表 comments 行数） |
| `collects` | INTEGER | Y | 收藏数；**部分平台无此指标** → NULL |
| `cleaned_at` | TIMESTAMPTZ | Y | **清洗幂等标记** |
| `scored_at` | TIMESTAMPTZ | Y | **打分幂等标记** |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_raw_platform_extid ON raw_posts(platform, external_id);
-- content_hash 故意不唯一：原始层保留全部采集事实，去重在清洗层做（见下方 P1 方案 A）
CREATE INDEX idx_raw_hash ON raw_posts(content_hash);
CREATE INDEX idx_raw_pending_clean ON raw_posts(cleaned_at) WHERE cleaned_at IS NULL;
CREATE INDEX idx_raw_pending_score ON raw_posts(scored_at) WHERE scored_at IS NULL;
CREATE INDEX idx_raw_platform_time ON raw_posts(platform, collected_at DESC);
CREATE INDEX idx_raw_keyword ON raw_posts(search_keyword, platform);
CREATE INDEX idx_raw_channel ON raw_posts(platform, channel);
```

**设计要点**

- **归一化在 spider 内完成**（2026-11-03 修正）。平台差异（字段叫什么、怎么抽、类型怎么转、缺失怎么办）由各平台自己的 spider 代码处理，产出统一字段。**本表没有任何"事后回填的派生列"**——`cleaned_at` / `scored_at` 是流程标记，不参与业务语义
  - 为什么不用 `field_mapping` 配置：DOM 提取（Playwright CSS selector）无法用路径表达式表达；JSON 平台的类型转换与缺失兜底也必须写代码。详见 ARCHITECTURE.md §5.2.2.1
- **平台原始 JSON 不落库**。spider 直接把平台返回的结构解析成统一字段，原始 JSON 解析完即丢弃——完整响应 80%+ 是无用内容（图片元数据、推荐流、埋点数据），且存下来也换不回"当初就没存的字段"。`url` 始终保留，需要时回源重取。详见 ARCHITECTURE.md §5.2.2.3
- **两个重复问题，两个处理位置**：
  - `uk_raw_platform_extid`（唯一）— 同平台同一条被重复采集（爬虫重试、翻页重叠）→ **采集层**拦截
  - `idx_raw_hash`（**不唯一**）— 内容重复（跨平台搬运 + 同平台转发）→ **清洗层**按 hash 分组处理
- **✅ `content_hash` 故意不做唯一索引**（ARCHITECTURE.md 自评 P1，2026-11-03 采纳方案 A）。同一内容在**同一平台**出现多次是常态——小红书转发、闲鱼重复上架、GitHub duplicate issue、微博刷屏。若加唯一索引，第二条 insert 直接失败，"留给清洗层去重"这条路是**断的**（数据根本没进库，清洗层看不到它）。

  **落地规则**：

  | 场景 | 处理 |
  |---|---|
  | 同平台同 hash 多条 | **全部留在 `raw_posts`**（各是独立帖子，互动数据不同）。清洗层按 hash 分组，组内按互动量取**代表**进 `signal`，其余记 `scorer_audit`（`noise_reason='duplicate'`）并标记 `cleaned_at` |
  | 跨平台同 hash | 合并进代表的 `source_platforms`（如 `["xiaohongshu","weibo"]`）。signal 层据此给强度加成 |
  | 同平台同 `external_id` | `uk_raw_platform_extid` 在采集层拦截 |

  > `source_platforms` 的合并是 **UPDATE**，但**不违反"原始层不可变"**——它记录的是观察结果（这条内容出现在哪），不是原始 payload 的改写。
- **⚠️ `signal.content_hash` 仍是唯一索引**（`uk_signal_hash`）——这与 raw_posts 语义不同：signal 层已按 hash 分组去重过，每条 signal 代表一个唯一内容。不是设计不一致，是"原始层保留全部 / 信号层去重"的分层意图。
- **`content_hash` 计算后不可变**。重算会让同 hash 的行无法与已存行区分，必须在 spider 产出时算一次定死
- **`content_hash` 的归一化规则必须放在 `core/` 下**（如 `core/normalize.py`），spider 和 cleaner 共用同一份。否则 spider 依赖 engine，违反单向依赖硬约束——见 ARCHITECTURE.md 自评 P2
- `comments_count` 字段名带 `_count` 后缀，避免与 `comments` 表混淆
- `author` 在**采集时脱敏**（哈希 + 截断），不在清洗层做——避免明文昵称落库

---

### 3.2 `comments` — 评论层

**粒度**：一条平台原始评论。

**写入方**：crawler（Phase 3 启用）。**当前**：表已建好，Phase 1-2 不写入。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | |
| `channel` | VARCHAR(128) | Y | ★ 所属帖子的频道（同 raw_posts.channel） |
| `external_id` | VARCHAR(128) | N | 评论自身在平台内的 ID |
| `post_id` | INTEGER FK → raw_posts.id | N | 所属帖子 |
| `content_hash` | VARCHAR(64) | N | 评论内容去重 |
| `content` | TEXT | Y | 评论正文（**spider 采集时即为纯文本**） |
| `author` | VARCHAR(128) | Y | 脱敏昵称 |
| `likes` | INTEGER | Y | 评论点赞数；平台无此指标 → NULL |
| `published_at` | TIMESTAMPTZ | Y | |
| `collected_at` | TIMESTAMPTZ | N | |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_comment_platform_extid ON comments(platform, external_id);
CREATE INDEX idx_comment_post ON comments(post_id);
CREATE INDEX idx_comment_hash ON comments(content_hash);
```

**设计要点**

- 评论**独立成表**而非塞进 `raw_posts`：生命周期、去重策略、评分逻辑都不同；一条帖子可能 0 条也可能 500 条评论
- 评论是**平台原创内容**（不像帖子那样会被搬运），所以不需要跨平台 hash 去重，`content_hash` 只建普通索引不建唯一
- 评论的"痛点信号"由 engine 抽取时写入 `signal`（`signal.raw_post_id` 指向所属帖子），**评论本身不直接进 signal 表**——避免 signal 表混装两种粒度

---

### 3.3 `signal` — 信号层 ★

**粒度**：一条被判为 A/B/C 的痛点信号。

**写入方**：engine/scorer。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `raw_post_id` | INTEGER FK → raw_posts.id | N | 来源帖子 |
| `source` | VARCHAR(32) | N | 来源平台 |
| `source_channel` | VARCHAR(64) | Y | 平台内频道/分区/话题（如 github 的 repo、xhs 的 tag） |
| `title` | TEXT | Y | 标题 / 首句 |
| `body` | TEXT | N | 正文 |
| `num_comments` | INTEGER | Y | 评论数（热度指标，来自 raw_posts） |
| `score` | INTEGER | Y | ★ 平台热度字段（点赞等）——入库门槛 + 排序 |
| `url` | TEXT | Y | 原文链接 |
| `triage_grade` | CHAR(1) | N | A / B / C（D/E 不入此表，见 `scorer_audit`） |
| `pain_type` | VARCHAR(32) | Y | 功能缺失/体验差/效率低/成本高/集成难/可靠性/其他 |
| `dollar_amounts` | VARCHAR(128) | Y | ★ 提取到的金额信号，如 `"5000-8000/套"` |
| `content_hash` | VARCHAR(64) | N | 跨平台去重 |
| `pain_score` | SMALLINT | Y | 0-100 综合痛点分 |
| `target_user` | TEXT | Y | 目标人群（LLM 提取） |
| `willingness_to_pay` | VARCHAR(16) | Y | 高/中/低/无 |
| `pain_type` | VARCHAR(32) | Y | （同上） |
| `rule_score` | SMALLINT | Y | 规则分（可解释性） |
| `rule_hits` | TEXT(JSON) | Y | ★ 命中的规则列表，如 `["痛点词:崩溃","疑问句式"]` |
| `llm_score` | SMALLINT | Y | LLM 分级得分 |
| `scored_at` | TIMESTAMPTZ | N | 打分时间 |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_signal_hash ON signal(content_hash);   -- ★ 唯一：signal 层已按 hash 去重（与 raw_posts 的不唯一语义不同，见 P1）
CREATE INDEX idx_signal_pending_merge ON signal(triage_grade, id)
    WHERE triage_grade IN ('A','B');
CREATE INDEX idx_signal_score ON signal(score DESC);
CREATE INDEX idx_signal_channel ON signal(source, source_channel);
CREATE INDEX idx_signal_grade ON signal(triage_grade);
CREATE INDEX idx_signal_dollar ON signal(dollar_amounts) WHERE dollar_amounts IS NOT NULL;
```

**设计要点**

- **`score` 是平台热度字段**（点赞数等），不是综合评分——综合评分是 `pain_score`。命名刻意区分，避免混淆
- **`rule_hits` 存 JSON**：记录命中了哪些规则，是**规则调参的核心数据**。某条信号被误杀时，能反查是哪条规则导致的。它同时存在 `signal` 和 `scorer_audit` 两张表——signal 表只存 A/B/C（需要审计其规则依据），scorer_audit 存 D/E
- **`dollar_amounts` 是付费意愿的最强信号**。归并阶段注入 prompt 时单独拼接，显著影响 LLM 对"会不会有人付钱"的判断
- **⚠️ 幂等查询的索引设计**：`idx_signal_pending_merge` 的条件是 `triage_grade IN ('A','B')`，但真正的待归并判定还需要 `NOT EXISTS (signal_idea.is_primary)`——这在 SQL 里是半连接，索引只能帮到前半段。实际数据量下（signal 万级）全表扫描可接受，不额外优化

**🔧 修正说明**：本表**不设 `idea_id` 列**。ARCHITECTURE.md v2.0 §6.2 已同步修正为多对多方案——归属状态由 `signal_idea.is_primary` 承载，避免两处状态不一致（单列说归了、关联表说没归）。

---

### 3.4 `product_idea` — 产品机会层 ★

**粒度**：一个产品机会假设。

**写入方**：engine/idea_generator。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `name` | VARCHAR(256) | N | 机会名称，如 `PriceCheck AI` |
| `title` | VARCHAR(256) | Y | 展示标题 |
| `product_idea` | TEXT | N | 产品设想（具体做什么、技术与 UX） |
| `buyer` | TEXT | Y | 买家画像与处境 |
| `why_pay` | TEXT | Y | 付费理由 / ROI |
| `problem_statement` | TEXT | Y | 问题陈述 |
| `target_user_profile` | TEXT | Y | 用户画像 |
| `current_solutions_and_gaps` | TEXT(JSON) | Y | 现有方案与差距 |
| `proposed_solution` | TEXT(JSON) | Y | 建议方案（core/mvp/differentiator） |
| `market_evidence` | TEXT(JSON) | Y | 市场证据（含 `sample_quotes`） |
| `pain_type` | VARCHAR(32) | Y | |
| `monetization_hypothesis` | TEXT | Y | 变现假设 |
| `confidence` | VARCHAR(16) | Y | high / medium / low |
| `next_validation_step` | TEXT | Y | 下一步验证动作 |
| `source_signal_ids` | TEXT(JSON) | Y | ★ LLM 返回的原始 signal id 列表（生成时的快照） |
| `signal_count` | INTEGER | Y | 证据信号数 |
| `source_platforms` | TEXT(JSON) | Y | 涉及平台列表 |
| `opportunity_type` | VARCHAR(32) | Y | micro-saas / autopilot-service / revenue-agent / marketplace / app / browser-extension |
| `copilot_or_autopilot` | VARCHAR(16) | Y | copilot / autopilot |
| `buyer_income` | SMALLINT | Y | ★ 六维评分 1-5 |
| `usage_frequency` | SMALLINT | Y | |
| `alternative_gap` | SMALLINT | Y | |
| `buyer_unsophistication` | SMALLINT | Y | |
| `buildability` | SMALLINT | Y | |
| `integration_depth` | SMALLINT | Y | |
| `is_boring_industry` | INTEGER(BOOL) | Y | ★ 框架标记 |
| `is_abandoned_work` | INTEGER(BOOL) | Y | |
| `is_already_outsourced` | INTEGER(BOOL) | Y | |
| `has_dual_signal` | INTEGER(BOOL) | Y | |
| `is_intelligence_gap` | INTEGER(BOOL) | Y | |
| `total_score` | REAL | Y | ★ 六维乘积 × 框架倍数连乘。排序直接用它，不在应用层重算 |
| `reasoning` | TEXT | Y | 推理说明 |
| `model` | VARCHAR(64) | Y | ★ 生成模型（可复现） |
| `prompt_version` | VARCHAR(32) | Y | ★ prompt 版本（可复现 + A/B 对比） |
| `scoring_version` | VARCHAR(32) | Y | ★ 评分配置版本（`config/scoring.yaml` 的版本号）。公式调整后据此区分批次 |
| `status` | VARCHAR(16) | N | ★ 人工决策状态：new / tracking / active / rejected / archived。默认 `new` |
| `created_at` | TIMESTAMPTZ | N | |

**约束与索引**

```sql
CREATE INDEX idx_idea_created ON product_idea(created_at DESC);
CREATE INDEX idx_idea_type ON product_idea(opportunity_type);
CREATE INDEX idx_idea_copilot ON product_idea(copilot_or_autopilot);
CREATE INDEX idx_idea_confidence ON product_idea(confidence);
CREATE INDEX idx_idea_scores ON product_idea(buyer_income, usage_frequency, alternative_gap);
CREATE INDEX idx_idea_total ON product_idea(total_score DESC);
CREATE INDEX idx_idea_status ON product_idea(status);
```

**设计要点**

- **六维评分拆成独立列**（而非塞 JSON）：评分是**排序依据**，要能直接 `ORDER BY`。拆列后排序/筛选是纯 SQL，不用应用层解析 JSON
- **`total_score` 是唯一的排序键**：公式为 `base = 六维乘积`、`mult = 框架标记倍数连乘`、`total = base × mult`（倍数见 `config/scoring.yaml`）。**落库时算好，不在查询时重算**——公式可能随配置调整，历史数据的 total 必须冻结，否则排序结果会随配置漂移。需要重算时用 `scoring_version` 区分批次
- **框架标记同样拆列**：`is_boring_industry` 等 5 个标记是差异化筛选维度，PM 会按"找枯燥行业的机会"这种条件查。拆列后可建组合索引
- **布尔值用 `INTEGER` 0/1 而非 `NULL` 语义**：LLM 可能漏输出某个标记，此时存 `NULL` 而非 0（"不知道" ≠ "否"）。筛选"枯燥行业机会"时用 `is_boring_industry = 1`，NULL 不会被误命中
- **`source_signal_ids` 与 `signal_idea` 并存**：前者是 LLM 生成时的**快照**（不可变，用于回溯"当时模型引用了哪些 id"），后者是**实时关联**（用于查"这条 signal 现在支撑哪些 idea"）。两者可能不一致——LLM 返回的 id 校验失败会被丢弃，但保留快照有助于排查
- **`status` 承载人工决策流**：新生成的机会默认 `new`，PM 通过 CLI 标记 `tracking`（在跟）/ `active`（要做）/ `rejected`（否掉）/ `archived`（归档）。**不做看板，但保留决策的落点**——这些标记是后续调评分权重的反馈数据来源。变更写入 `run_log` 审计
- **`model` + `prompt_version` 必须写入**：prompt 是核心资产，没有版本号就无法对比迭代效果

**🔧 修正说明**：本表**无 `cluster_id` 字段**——系统不引入聚类/簇概念，机会由 LLM 直接从信号池归纳产生。

---

### 3.5 `signal_idea` — 信号与机会关联表

**粒度**：一条 signal 与一个 idea 的关联。

**写入方**：engine/cluster（归并阶段）。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `signal_id` | INTEGER FK → signal.id | N | 信号 |
| `idea_id` | INTEGER FK → product_idea.id | N | 机会 |
| `is_primary` | INTEGER(BOOL) | N | ★ 1 = 该 signal 归属于此 idea；0 = 次要证据 |
| `relation` | VARCHAR(16) | Y | `primary` / `supporting`（次要证据的原因，如"同类佐证"） |
| `weight` | REAL | Y | 证据权重 0-1（互动量高/痛点强的信号权重更高） |
| `created_at` | TIMESTAMPTZ | N | |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_signal_idea ON signal_idea(signal_id, idea_id);
-- ★ 一条 signal 至多一个 primary 归属（部分唯一索引）
CREATE UNIQUE INDEX uk_signal_primary ON signal_idea(signal_id) WHERE is_primary = 1;
CREATE INDEX idx_signal_idea_idea ON signal_idea(idea_id);
```

**设计要点**

- **`uk_signal_primary` 是本设计的关键约束**：部分唯一索引保证"一条 signal 至多归属一个 idea"。这让幂等判定成为一条简单查询：

```sql
-- 找出未归并的 A/B 信号
SELECT s.* FROM signal s
WHERE s.triage_grade IN ('A','B')
  AND NOT EXISTS (
      SELECT 1 FROM signal_idea si
      WHERE si.signal_id = s.id AND si.is_primary = 1
  )
ORDER BY s.score DESC;
```

- **`relation` 字段**：`primary` = 该 signal 是这个 idea 的核心证据；`supporting` = 同类佐证。LLM 一次返回多个 signal 时，需要区分"直接支撑"和"顺带印证"——这对 PM 判断证据强度有意义
- **`weight`**：给证据排序用。互动量高、明确表达付费意愿的信号，其证据权重应更高。导出 Markdown 时可按此排序展示

---

### 3.6 `scorer_audit` — 规则审计

**粒度**：一条被判为 D 或 E 的内容（留痕但不进 signal）。

**写入方**：engine/scorer。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `raw_post_id` | INTEGER FK → raw_posts.id | N | |
| `grade` | CHAR(1) | N | D / E |
| `rule_score` | SMALLINT | N | 规则分 |
| `rule_hits` | TEXT(JSON) | Y | ★ 命中的规则 + 未命中的关键规则（用于反查误杀） |
| `noise_reason` | VARCHAR(64) | Y | E 级原因：广告 / 招聘 / 教程 / 带货 / 闲聊 |
| `created_at` | TIMESTAMPTZ | N | |

**约束与索引**

```sql
CREATE INDEX idx_audit_grade ON scorer_audit(grade);
CREATE INDEX idx_audit_reason ON scorer_audit(noise_reason);
```

**设计要点**

- **这张表是规则调参的唯一数据源**。当发现某个好信号被误杀成 E 时，能反查命中了哪条规则。没有它，规则阈值只能靠猜
- **`rule_hits` 建议同时记录"命中"和"未命中"**：只记命中的话，无法分析"为什么这条没命中痛点词"。建议格式：`{"hit": [...], "miss": [...]}`
- **只存 D/E，不存 A/B/C**：A/B/C 在 `signal` 表已有一份（`rule_hits` 字段），不重复存
- **⚠️ 这张表会快速膨胀**：D/E 占比约 80%，采 10 万条帖子就有 8 万行审计记录。Phase 1-2 不用管，Phase 3 加保留策略（如只保留最近 90 天，或只保留与 A/B/C 相似度高的误杀样本）

---

### 3.7 `crawl_cursor` — 增量游标

**粒度**：一个平台 + 一个关键词的采集位点。

**写入方**：crawler（**仅在 raw_posts 写入成功后**）。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | |
| `channel` | VARCHAR(128) | N | ★ 平台内频道（无分区概念的平台用空串 `''`，不用 NULL——NULL 在唯一索引里不参与去重） |
| `keyword` | VARCHAR(128) | N | |
| `payload` | TEXT(JSON) | N | ★ 平台特有的游标内容（**不透明**，各平台自定义结构） |
| `last_external_id` | VARCHAR(128) | Y | 冗余：便于通用排查（不作为判定依据） |
| `last_published_at` | TIMESTAMPTZ | Y | 冗余：时间水位线 |
| `updated_at` | TIMESTAMPTZ | N | |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_cursor ON crawl_cursor(platform, channel, keyword);
```

**设计要点**

- **唯一索引是三列 `(platform, channel, keyword)`**。同一平台同一关键词、不同频道（reddit 的不同 subreddit、github 的不同 repo）必须各存一个游标——否则 subreddit A 的游标会污染 B。无分区概念的平台用空串 `''` 而非 `NULL`（`NULL` 在 SQLite 唯一索引里不参与去重，会导致重复行）
- **`payload` 是核心，`last_external_id` / `last_published_at` 是冗余**。各平台游标语义差异太大（GitHub 是单调 issue number，小红书是时间水位线 + id，抖音是 max_cursor + 雪花 id），统一成固定列表达不了，所以主存 JSON
  - **`payload` 的结构由各平台 spider 自行定义**，不在配置层声明类型（曾有的 `cursor.kind` 字段已移除，理由见 ARCHITECTURE.md §5.2.6）。**可以是 JSON 对象，也可以是 JSON 标量**（如 GitHub 若只想存一个数字，`json.dumps("5234")` → `"\"5234\""`）——storage 层反序列化后原样返回，**不做任何结构假设、不含任何平台分支**
- **冗余两列的用途**：通用运维查询（如"上周各平台的采集水位线到哪了"）不需要解析 JSON。**它们不是判定依据**，各平台的判定逻辑用自己的 `payload`
- **⚠️ 推进顺序是硬约束**：

```
✅ 写 raw_posts → 提交事务 → 推进游标
❌ 推进游标 → 写 raw_posts
```

反过来一旦写库失败，游标已跳过这批数据，**永久丢失**。反过来最坏是重复采集，靠 `uk_raw_platform_extid` 去重

---

### 3.8 `run_log` — 运行记录

**粒度**：一次 workflow 运行中的一个阶段。

**写入方**：workflow/runner。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `run_id` | VARCHAR(64) | N | 一次 workflow 运行的标识 |
| `stage` | VARCHAR(32) | N | collect / clean / score / signal / cluster / generate |
| `status` | VARCHAR(16) | N | pending / running / success / failed / skipped |
| `started_at` | TIMESTAMPTZ | N | |
| `finished_at` | TIMESTAMPTZ | Y | |
| `duration_ms` | INTEGER | Y | |
| `input_count` | INTEGER | Y | |
| `output_count` | INTEGER | Y | |
| `skipped_count` | INTEGER | Y | |
| `error_message` | TEXT | Y | |
| `error_traceback` | TEXT | Y | |
| `stats` | TEXT(JSON) | Y | ★ 该阶段细分统计：各等级分布、LLM 调用数、token 数、去重丢弃数 |
| `params` | TEXT(JSON) | Y | 本次运行的参数（platform / keyword / stages） |

**约束与索引**

```sql
CREATE INDEX idx_run_runid ON run_log(run_id, stage);
CREATE INDEX idx_run_status ON run_log(status, started_at DESC);
CREATE INDEX idx_run_stage_time ON run_log(stage, started_at DESC);
```

**设计要点**

- **`stats` 是预算熔断的数据源**，必须结构化入库，不能只进日志文件（日志会轮转、格式不统一、无法聚合查询）
- **`status = 'skipped'` 是正常状态**：信号不足门槛时 cluster 阶段会 skip，这不是错误
- **`params` 记录本次运行的参数**，便于复现"当时跑的是什么"
- **⚠️ 这是纯审计表**。系统正确性不依赖它——阶段幂等靠 `raw_posts.cleaned_at` / `signal_idea.is_primary` 等业务字段。即使这张表损坏，系统仍能正确运行

---

### 3.9 `keyword_seed` — 关键词库

**粒度**：一个检索关键词。

**写入方**：config 加载器（种子词）/ engine/feedback（反哺词）。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `keyword` | VARCHAR(128) | N | 关键词文本 |
| `platform` | VARCHAR(32) | N | 适用平台 |
| `origin` | VARCHAR(16) | N | ★ `seed`（人工配置）/ `feedback`（从 signal/idea 反哺） |
| `status` | VARCHAR(16) | N | `active` / `paused` / `retired` |
| `weight` | REAL | Y | 优先级权重（调度时排序） |
| `source_signal_ids` | TEXT(JSON) | Y | 反哺来源：提取自哪些 signal |
| `hit_count` | INTEGER | Y | 累计命中条数（衡量关键词有效性） |
| `yield_rate` | REAL | Y | 命中率：命中条数 / 采集条数（衡量信号产出效率） |
| `created_at` | TIMESTAMPTZ | N | |
| `updated_at` | TIMESTAMPTZ | Y | |

**约束与索引**

```sql
CREATE UNIQUE INDEX uk_keyword ON keyword_seed(platform, keyword);
CREATE INDEX idx_keyword_status ON keyword_seed(status, weight DESC);
```

**设计要点**

- **`origin` 区分种子词与反哺词**：反哺词是 Phase 4 飞轮的产物，需要能单独统计效果（"反哺词贡献了 X% 的 idea"）
- **`hit_count` / `yield_rate` 是关键词效果的度量**：一个命中 1000 条但产出 0 个 A 级信号的关键词应该降权或下线。这是关键词库自我优化的基础
- **`source_signal_ids` 让反哺可追溯**：能回答"这个关键词是从哪条痛点里提取的"

---

### 3.10 `llm_usage` — LLM 调用记账

**粒度**：一次 LLM 调用。

**写入方**：engine/llm_client。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `called_at` | TIMESTAMPTZ | N | |
| `provider` | VARCHAR(32) | N | ollama / openai / claude / ... |
| `model` | VARCHAR(64) | N | |
| `purpose` | VARCHAR(32) | N | ★ `score` / `cluster` / `generate` |
| `prompt_version` | VARCHAR(32) | Y | |
| `input_tokens` | INTEGER | Y | |
| `output_tokens` | INTEGER | Y | |
| `cost` | REAL | Y | 估算成本（按当时单价算） |
| `duration_ms` | INTEGER | Y | |
| `cache_hit` | INTEGER(BOOL) | Y | 是否命中缓存 |
| `success` | INTEGER(BOOL) | Y | |
| `error_message` | TEXT | Y | |

**约束与索引**

```sql
CREATE INDEX idx_llm_time ON llm_usage(called_at DESC);
CREATE INDEX idx_llm_purpose ON llm_usage(purpose, called_at DESC);
```

**设计要点**

- **预算熔断的数据源**。`config/llm.yaml` 里的 `daily_token_limit` 和 `monthly_cost_limit` 靠这张表计算
- **`purpose` 区分用途**，因为分级模型策略需要知道：打分用量大（走本地小模型），生成用量小（走云端大模型）。成本归因到用途，才能判断分级策略是否生效
- **`cache_hit` 追踪缓存效果**：`content_hash` 命中跳过是重要降本手段，需要能验证它真的生效了
- **写入频率高**：每次 LLM 调用插一行，量级与 LLM 调用数相同（远小于帖子数），可接受

---

## 4. 幂等依据汇总

这是本设计最核心的部分——**每个阶段的幂等由哪个字段保证**。

| 阶段 | 待处理项查询条件 | 完成标记 | 写入方 |
|---|---|---|---|
| **collect** | `crawl_cursor` 水位线 + `uk_raw_platform_extid` | `crawl_cursor.payload` 推进 | crawler |
| **clean** | `WHERE cleaned_at IS NULL` | `raw_posts.cleaned_at` | engine/cleaner |
| **score** | `WHERE scored_at IS NULL` | `raw_posts.scored_at` + `signal` 插入 | engine/scorer |
| **signal** | （与 score 同批完成） | `signal` 插入 / `scorer_audit` 插入 | engine/scorer |
| **cluster** | A/B 信号 且 无 primary 归属 | `signal_idea.is_primary = 1` 行插入 | engine/cluster |
| **generate** | （与 cluster 同批完成） | `product_idea` 插入 + `signal_idea` 插入 | engine/idea_generator |

**clean 阶段的去重分组查询**（P1 方案 A 的落地）：

```sql
-- ① 找出待清洗行按 hash 分组后的分组
SELECT content_hash, COUNT(*) AS n
FROM raw_posts
WHERE cleaned_at IS NULL
GROUP BY content_hash
HAVING COUNT(*) > 1;          -- 只有重复组需要特殊处理

-- ② 组内取代表（互动量公式见 ARCHITECTURE.md 5.3.2，权重按平台配置）
SELECT * FROM raw_posts
WHERE content_hash = :hash
ORDER BY engagement_score DESC
LIMIT 1;
```

- 代表进 `signal`；组内其余行记 `scorer_audit`（`noise_reason='duplicate'`）
- 跨平台组：所有涉及平台合并进代表的 `source_platforms`
- 单条组（`n = 1`）走常规路径，不做额外查询

**核心查询（cluster 阶段）**

```sql
SELECT s.id, s.source, s.source_channel, s.title, s.body,
       s.score, s.num_comments, s.dollar_amounts, s.url
FROM signal s
WHERE s.triage_grade IN ('A','B')
  AND NOT EXISTS (
      SELECT 1 FROM signal_idea si
      WHERE si.signal_id = s.id AND si.is_primary = 1
  )
ORDER BY s.score DESC
LIMIT :batch_size;
```

> **⚠️ 这条查询是 workflow 层"不循环"的根据**：捞一次 → 处理 → 结束。下一轮跑时，因为 `signal_idea.is_primary` 仍在，天然只捞到新信号。**绝不写成 `while True` 循环**——等待交给 scheduler 的频率。

**幂等设计的额外收益**：`run_log` 从"控制流程"降级为"纯审计"。即使 `run_log` 表损坏，系统仍能正确运行。

---

## 5. 索引策略

### 5.1 按查询场景归类

| 场景 | 查询 | 依赖索引 |
|---|---|---|
| 采集幂等 | `WHERE platform=? AND external_id=?` | `uk_raw_platform_extid` |
| 增量采集 | `WHERE platform=? AND collected_at > ?` | `idx_raw_platform_time` |
| 按频道分析 | `WHERE platform=? AND channel=?` | `idx_raw_channel` |
| 游标定位 | `WHERE platform=? AND channel=? AND keyword=?` | `uk_cursor`（三列唯一） |
| **清洗去重分组** | `GROUP BY content_hash`（仅待清洗行） | `idx_raw_hash` + `idx_raw_pending_clean` |
| 清洗待处理 | `WHERE cleaned_at IS NULL` | `idx_raw_pending_clean`（部分索引） |
| 打分待处理 | `WHERE scored_at IS NULL` | `idx_raw_pending_score`（部分索引） |
| 归并待处理 | 见 4. 核心查询 | `idx_signal_pending_merge` + `uk_signal_primary` |
| idea 排序 | `ORDER BY created_at DESC` | `idx_idea_created` |
| idea 按分排序 | `ORDER BY total_score DESC` | `idx_idea_total` |
| idea 筛选 | `WHERE opportunity_type=? AND is_boring_industry=1` | `idx_idea_type`（可扩展组合索引） |
| idea 决策流 | `WHERE status='tracking'` | `idx_idea_status` |
| 溯源 | `WHERE si.idea_id=?` | `idx_signal_idea_idea` |
| 反查 signal | `WHERE si.signal_id=?` | `uk_signal_idea` |
| 成本统计 | `WHERE called_at BETWEEN ? AND ?` | `idx_llm_time` |
| 关键词调度 | `WHERE status='active' ORDER BY weight DESC` | `idx_keyword_status` |

### 5.2 部分索引（Partial Index）的价值

SQLite 和 PG 都支持部分索引，是本设计的性能利器：

```sql
-- 只索引"待处理"的行
CREATE INDEX idx_raw_pending_clean ON raw_posts(cleaned_at) WHERE cleaned_at IS NULL;
```

**为什么有效**：待处理行数远小于总行数。比如 10 万条帖子里只有 2000 条未清洗，索引只覆盖这 2000 条 → 查询代价与总表规模无关。

**这是"标记字段驱动幂等"能同时保持高性能的关键。** 如果用状态机表记录阶段状态，那张表会有 10 万行（每个帖子一行状态），且没有部分索引可用。

### 5.3 索引数量控制

当前共 24 个索引。SQLite 写放大随索引数增长，建议：

- Phase 1-2 保持现状
- 观察实际查询计划后，用 `ANALYZE` 更新统计信息
- 若写入变慢，优先删 `idx_raw_keyword`（可通过 `search_keyword` 的其他查询替代）

---

## 6. SQLite → PostgreSQL 迁移映射

| SQLite | PostgreSQL | 迁移动作 |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` | 类型替换 |
| `TIMESTAMPTZ`（TEXT 存 ISO8601） | `TIMESTAMPTZ`（原生） | 类型替换 + 格式转换 |
| `INTEGER`（布尔 0/1） | `BOOLEAN` | 类型替换 |
| `TEXT`（JSON 字符串） | `JSONB` | 类型替换 + 加 GIN 索引 |
| `TEXT`（JSON 数组字符串） | `JSONB` / `TEXT[]` | 类型替换 |
| `VARCHAR(64)` | `VARCHAR(64)` | 不变 |
| `REAL` | `DOUBLE PRECISION` | 类型替换 |
| 部分索引 `WHERE ...` | 部分索引 `WHERE ...` | **语法一致，直接迁移** |
| `TEXT` 全文检索 | `to_tsvector` + GIN | 需重写（中文配 `zhparser`） |

**迁移注意事项**

1. **DDL 用迁移脚本管理**：`storage/migrations/sqlite/` 和 `storage/migrations/postgres/` 各一套，应用代码禁止 `CREATE TABLE`
2. **迁移脚本需要 dialect 分支**：SQLite 与 PG 的类型名、部分索引语法、JSON 函数都不同。同一份逻辑的建表语句需要分别写
3. **无向量类型映射**：schema 里没有 embedding 列，迁移时也不用管向量扩展
4. **SQLite 单写者 → PG 并发**：这是迁移最大的实际收益。Scrapy 进程与 workflow 进程在 SQLite 下不能同时写，PG 下可以
5. **迁移触发条件**（明确写出来，避免拍脑袋）：写入出现明显锁等待 / 数据量超千万 / 需要多机部署

**迁移的真正成本不在 schema**——上面这些映射都是机械替换。成本在**索引策略**（SQLite 的部分索引很实用，但 PG 在大数据量下可能需要不同的索引组合）和**并发写入改造**（SQLite 的重试逻辑在 PG 下可以去掉）。

---

## 7. 待定项

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | `source_signal_ids` 是否冗余 | 与 `signal_idea` 有重复 | 保留——它是生成时快照，用于排查 LLM 幻觉 |
| 2 | `scorer_audit` 的保留策略 | 表会快速膨胀 | Phase 3 加保留策略（近 90 天 / 或只保留疑似误杀） |
| 3 | `keyword_seed.yield_rate` 的计算 | 需要定期重算 | Phase 4 再实现 |
| 4 | `product_idea` 是否需要版本化 | 同一 idea 重新生成时如何处理 | 建议：LLM 生成时用 `content_hash` 去重，相同输入不重复生成；人工修订另开 `idea_revision` 表（Phase 3） |
| 5 | `signal.rule_hits` 是否需要单独成表 | 便于聚合分析"哪条规则最常命中" | 当前 JSON 够用；若要做规则效果统计，可用 SQLite 的 `json_each` 展开查询 |

---

*文档结束。下一步：把本文档落成 `storage/migrations/sqlite/001_init.sql`。*

*2026-11-03 修订：*
- *同步「归一化在 spider 层完成」的方案变更（§1.2 约束③、§2.3 数据流、§3.1 raw_posts）。已与 ARCHITECTURE.md v2.1 §3.2/§3.3/§4.1/§5.1.1/§5.2.2/§5.3.1/§6.2/§10.2 一致。*
- *补 `raw_posts.source_platforms` 字段（自评 P3）。*
- *✅ P1 已决策：采纳方案 A——`uk_raw_hash` 改为普通索引 `idx_raw_hash`，去重在清洗层按 hash 分组做；`uk_signal_hash` 保持唯一（语义不同，见 §3.1 说明）。*
- *标注 `content_hash` 归一化规则需抽到 `core/normalize.py`（自评 P2，已修）。*
