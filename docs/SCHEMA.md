# 数据表结构设计

> 版本：v2.1｜日期：2026-11-06｜配套：[ARCHITECTURE.md](./ARCHITECTURE.md) v3.1
>
> 本文是字段级定义。DDL 只写在 `storage/migrations/`，应用代码不写 `CREATE TABLE`。

## 目录

1. [设计约定](#1-设计约定)
2. [表清单与关系](#2-表清单与关系)
3. [表结构](#3-表结构)
4. [幂等与关键查询](#4-幂等与关键查询)
5. [索引策略](#5-索引策略)
6. [SQLite → PostgreSQL](#6-sqlite--postgresql)
7. [待观察项](#7-待观察项)

---

## 1. 设计约定

### 1.1 类型映射

| 逻辑类型 | SQLite | PostgreSQL |
|---|---|---|
| 主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` |
| 时间 | `TEXT`（ISO8601） | `TIMESTAMPTZ` |
| 布尔 | `INTEGER`（0/1） | `BOOLEAN` |
| JSON | `TEXT` | `JSONB` |
| 金额 | `VARCHAR(128)`（保留原始写法，如 `"5000-8000/套"`） | `VARCHAR(128)` |

命名：表 / 字段全小写 snake_case；索引 `idx_<表>_<用途>`；唯一索引 `uk_<表>_<字段>`。

### 1.2 三条硬约束

**① 原始层只写不改**

`raw_posts` / `comments` 的业务字段由 spider 一次填入，清洗层不回填、不改写。

例外只有一个：`raw_posts.source_platforms` 在跨平台去重时被 UPDATE——它记录"这条内容出现在哪些平台"，是观察结果，不是内容改写。

**② 幂等靠标记字段**

每个执行阶段都有完成标记，待处理 = 标记为空。`run_log` 只是审计，损坏不影响正确性。

**③ 溯源强制**

`product_idea` 必须通过 `signal_idea` 关联至少一条 `signal`；`source_signal_ids` 另存生成时快照，用于排查模型幻觉。

### 1.3 多对多与归属

一条 signal 可以支撑多个 idea，一条 idea 也有多条证据。关系存 `signal_idea`：

- `is_primary = 1`：该 signal 的主证据归属，**每条 signal 至多一行**（部分唯一索引保证）。
- `is_primary = 0`：辅助证据。
- 归并待处理 = A/B 且 `clustered_at IS NULL`；`is_primary` 只表达归属，不承担幂等。

---

## 2. 表清单与关系

| 表 | 作用 | 写入方 |
|---|---|---|
| `raw_posts` | 原始帖子（统一字段，无平台 payload） | crawler |
| `comments` | 评论（后续阶段） | crawler |
| `signal` | 分级后的信号（A/B/C）★ | engine/scorer |
| `product_idea` | 产品机会（含六维评分）★ | engine/cluster |
| `signal_idea` | signal ↔ idea 关联 | engine/cluster |
| `scorer_audit` | D/E 分级留痕 | engine/scorer |
| `crawl_cursor` | 增量游标 | crawler |
| `run_log` | 阶段运行记录 | workflow |
| `keyword_seed` | 种子词 + 反哺词 | config / engine/feedback |
| `llm_usage` | LLM 调用记账 | engine/llm_client |

```
raw_posts 1──N comments
raw_posts 1──N signal
signal    N──M product_idea   (signal_idea)

scorer_audit / crawl_cursor / run_log / keyword_seed / llm_usage 为独立表
```

---

## 3. 表结构

### 3.1 `raw_posts` — 原始层

粒度：一条平台原始帖子。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | `github` / `xiaohongshu` / `xianyu` … |
| `channel` | VARCHAR(128) | Y | 平台内频道/分区/话题；无此概念留空串 |
| `external_id` | VARCHAR(128) | N | 平台内唯一 ID |
| `content_hash` | VARCHAR(64) | N | 归一化 SHA256，**采集时算定，不可变** |
| `search_keyword` | VARCHAR(128) | Y | 命中的检索词 |
| `url` | TEXT | Y | 原文链接（回源用） |
| `author` | VARCHAR(128) | Y | 昵称，**采集时脱敏** |
| `published_at` | TIMESTAMPTZ | Y | 发布时间，可能缺失 |
| `collected_at` | TIMESTAMPTZ | N | 采集时间 |
| `source_platforms` | TEXT(JSON) | Y | 初始 `[platform]`；跨平台去重时合并 |
| `title` | TEXT | Y | 标题，可能为空 |
| `content` | TEXT | Y | 正文（纯文本） |
| `likes` | INTEGER | Y | 无此指标 → NULL |
| `comments_count` | INTEGER | Y | 帖子评论数（不是 comments 表行数） |
| `collects` | INTEGER | Y | 无此指标 → NULL |
| `cleaned_at` | TIMESTAMPTZ | Y | 清洗完成标记 |
| `scored_at` | TIMESTAMPTZ | Y | **已消费**：已分级，或去重时被吸收 |

```sql
CREATE UNIQUE INDEX uk_raw_platform_extid ON raw_posts(platform, external_id);
CREATE INDEX idx_raw_hash ON raw_posts(content_hash);            -- 故意不唯一
CREATE INDEX idx_raw_pending_clean ON raw_posts(cleaned_at) WHERE cleaned_at IS NULL;
CREATE INDEX idx_raw_pending_score ON raw_posts(scored_at) WHERE scored_at IS NULL;
CREATE INDEX idx_raw_platform_time ON raw_posts(platform, collected_at DESC);
CREATE INDEX idx_raw_keyword ON raw_posts(search_keyword, platform);
CREATE INDEX idx_raw_channel ON raw_posts(platform, channel);
```

要点：

- **无平台 payload**：完整响应 80%+ 是无用字段，且补不回"当初没存"的新字段；`url` 保留，需要时回源。
- **content_hash 不唯一**：同内容同平台多条（转发、重复上架、duplicate issue）是常态，原始层保留全部事实。
- **scored_at 是"已消费"**：代表行表示已分级；被去重吸收的重复行也置此标记，避免再次送 LLM。
- 同平台同 `external_id` 重复由 `uk_raw_platform_extid` 拦截。

### 3.2 `comments` — 评论层（后续阶段）

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | |
| `channel` | VARCHAR(128) | Y | 所属频道 |
| `external_id` | VARCHAR(128) | N | 评论自身 ID |
| `post_id` | INTEGER FK → raw_posts.id | N | 所属帖子 |
| `content_hash` | VARCHAR(64) | N | 评论内容去重 |
| `content` | TEXT | Y | 评论正文 |
| `author` | VARCHAR(128) | Y | 脱敏昵称 |
| `likes` | INTEGER | Y | |
| `published_at` | TIMESTAMPTZ | Y | |
| `collected_at` | TIMESTAMPTZ | N | |

```sql
CREATE UNIQUE INDEX uk_comment_platform_extid ON comments(platform, external_id);
CREATE INDEX idx_comment_post ON comments(post_id);
CREATE INDEX idx_comment_hash ON comments(content_hash);
```

评论体量是帖子的 50–100 倍，只采高互动帖子的评论；评论不进入当前分级 / 归并链路，后续再设计证据抽取。具体策略见 ARCHITECTURE.md 的「设计取舍」。

### 3.3 `signal` — 信号层 ★

粒度：一条被判为 A/B/C 的内容。D/E 不进入本表。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `raw_post_id` | INTEGER FK → raw_posts.id | N | |
| `source` | VARCHAR(32) | N | 平台 |
| `source_channel` | VARCHAR(64) | Y | 频道/分区/话题 |
| `title` | TEXT | Y | 标题 / 首句 |
| `body` | TEXT | N | 正文（展示清洗后） |
| `num_comments` | INTEGER | Y | 评论数 |
| `score` | INTEGER | Y | **平台热度**（点赞等），只用于排序 |
| `url` | TEXT | Y | 原文链接 |
| `triage_grade` | CHAR(1) | N | A / B / C |
| `pain_type` | VARCHAR(32) | Y | 功能缺失/体验差/效率低/成本高/集成难/可靠性/其他 |
| `pain_score` | SMALLINT | Y | 0–100 综合痛点分 |
| `target_user` | TEXT | Y | LLM 提取的目标人群 |
| `willingness_to_pay` | VARCHAR(16) | Y | 高 / 中 / 低 / 不明 |
| `dollar_amounts` | VARCHAR(128) | Y | 金额信号，如 `"5000-8000/套"` |
| `content_hash` | VARCHAR(64) | N | 去重 |
| `rule_hits` | TEXT(JSON) | Y | 命中的短语（score 阶段复算），用于解释准入 |
| `model` | VARCHAR(64) | Y | 分级模型 |
| `prompt_version` | VARCHAR(32) | Y | triage prompt 版本 |
| `reason` | TEXT | Y | 分级理由（LLM 返回） |
| `scored_at` | TIMESTAMPTZ | N | 分级时间 |
| `clustered_at` | TIMESTAMPTZ | Y | 已参与归并（匹配或判定无匹配） |

```sql
CREATE UNIQUE INDEX uk_signal_hash ON signal(content_hash);
CREATE INDEX idx_signal_pending ON signal(triage_grade, id)
    WHERE triage_grade IN ('A','B') AND clustered_at IS NULL;
CREATE INDEX idx_signal_score ON signal(score DESC);
CREATE INDEX idx_signal_channel ON signal(source, source_channel);
CREATE INDEX idx_signal_grade ON signal(triage_grade);
CREATE INDEX idx_signal_dollar ON signal(dollar_amounts) WHERE dollar_amounts IS NOT NULL;
```

要点：

- `score` 是平台热度，**不是入库门槛**；准入由 LLM 分级决定，跨平台热度不可比。
- `content_hash` 唯一：进入 signal 前已按 hash 去重，每条 signal 代表一个内容。
- `rule_hits` 由 score 阶段用 `core/phrases.py` 对标题 + 正文复算，记录"因为命中哪些短语被准入"，不参与评分。
- `model` / `prompt_version` / `reason` 用于复现"这条信号当时由哪个模型、哪版 prompt、为什么定这个级"。
- `clustered_at` 是归并消费标记：进入过归并批次（无论是否匹配到 idea）即置值；C 级不参与归并，保持 NULL。

### 3.4 `product_idea` — 机会层 ★

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `name` | VARCHAR(256) | N | 产品名，如 `PriceCheck AI` |
| `product_idea` | TEXT | N | 具体做什么（技术 + UX） |
| `buyer` | TEXT | Y | 买家与处境 |
| `why_pay` | TEXT | Y | 付费理由 / ROI |
| `current_solutions_and_gaps` | TEXT(JSON) | Y | `{existing, gaps}` |
| `sample_quotes` | TEXT(JSON) | Y | 原文引文数组（校验子串） |
| `confidence` | VARCHAR(16) | Y | high / medium / low |
| `opportunity_type` | VARCHAR(32) | Y | micro-saas / autopilot-service / revenue-agent / marketplace / app / browser-extension |
| `copilot_or_autopilot` | VARCHAR(16) | Y | copilot / autopilot |
| `buyer_income` | SMALLINT | Y | 六维 1–5 |
| `usage_frequency` | SMALLINT | Y | |
| `alternative_gap` | SMALLINT | Y | |
| `buyer_unsophistication` | SMALLINT | Y | |
| `buildability` | SMALLINT | Y | |
| `integration_depth` | SMALLINT | Y | |
| `is_boring_industry` | INTEGER(BOOL) | Y | 框架标记 |
| `is_abandoned_work` | INTEGER(BOOL) | Y | |
| `is_already_outsourced` | INTEGER(BOOL) | Y | |
| `has_dual_signal` | INTEGER(BOOL) | Y | |
| `is_intelligence_gap` | INTEGER(BOOL) | Y | |
| `total_score` | REAL | Y | 六维乘积 × 框架倍数，落库冻结 |
| `reasoning` | TEXT | Y | 推理说明 |
| `source_signal_ids` | TEXT(JSON) | Y | 生成时的 signal 快照 |
| `signal_count` | INTEGER | Y | 证据数 |
| `source_platforms` | TEXT(JSON) | Y | 涉及平台 |
| `model` | VARCHAR(64) | Y | 生成模型 |
| `prompt_version` | VARCHAR(32) | Y | idea prompt 版本 |
| `scoring_version` | VARCHAR(32) | Y | 评分配置版本 |
| `status` | VARCHAR(16) | N | new / tracking / active / rejected / archived；默认 new |
| `created_at` | TIMESTAMPTZ | N | |

```sql
CREATE INDEX idx_idea_total ON product_idea(total_score DESC);
CREATE INDEX idx_idea_created ON product_idea(created_at DESC);
CREATE INDEX idx_idea_status ON product_idea(status);
CREATE INDEX idx_idea_type ON product_idea(opportunity_type);
CREATE INDEX idx_idea_copilot ON product_idea(copilot_or_autopilot);
```

要点：

- 六维拆列：排序是纯 SQL，不解析 JSON。
- `total_score` 落库时算好；公式调整以 `scoring_version` 区分批次，不回溯重算历史。
- 布尔标记用 NULL 表示"模型未给出"，与 false 区分。
- `source_signal_ids`（快照）与 `signal_idea`（实时关联）并存：前者排查幻觉，后者查当前证据。
- `status` 承载人工决策（CLI），是评分权重与反哺的反馈源。

### 3.5 `signal_idea` — 关联表

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `signal_id` | INTEGER FK → signal.id | N | |
| `idea_id` | INTEGER FK → product_idea.id | N | |
| `is_primary` | INTEGER(BOOL) | N | 1 = 主证据（该 signal 归属此 idea）；0 = 辅助证据 |
| `created_at` | TIMESTAMPTZ | N | |

```sql
CREATE UNIQUE INDEX uk_signal_idea ON signal_idea(signal_id, idea_id);
CREATE UNIQUE INDEX uk_signal_primary ON signal_idea(signal_id) WHERE is_primary = 1;
CREATE INDEX idx_signal_idea_idea ON signal_idea(idea_id);
```

- `uk_signal_primary` 保证每条 signal 至多一个主归属。
- 同一 signal 的其余关联用 `is_primary = 0`（辅助证据）。
- 不强制每条 idea 都有 primary：没有可用主证据时，idea 仍可由多条辅助证据支撑。
- 主证据分配规则见 ARCHITECTURE.md §4.4。
- "判过但没匹配"的 signal 由 `clustered_at` 消费，不会再被捞出。

### 3.6 `scorer_audit` — 分级审计

粒度：一条被判为 D/E 的内容。只留痕，不进 signal。

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `raw_post_id` | INTEGER FK → raw_posts.id | N | |
| `grade` | CHAR(1) | N | D / E |
| `pain_type` | VARCHAR(32) | Y | |
| `pain_score` | SMALLINT | Y | 0–100 |
| `reason` | TEXT | Y | 模型给出的短文说明 |
| `model` | VARCHAR(64) | Y | |
| `prompt_version` | VARCHAR(32) | Y | |
| `created_at` | TIMESTAMPTZ | N | |

```sql
CREATE INDEX idx_audit_grade ON scorer_audit(grade);
CREATE INDEX idx_audit_time ON scorer_audit(created_at DESC);
```

- 用途：反查误杀、校准分级阈值、抽检 prompt。
- D/E 约占 80%，会快速膨胀。后续阶段起保留最近 90 天，或只留疑似误杀样本。

### 3.7 `crawl_cursor` — 增量游标

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `platform` | VARCHAR(32) | N | |
| `channel` | VARCHAR(128) | N | 无频道概念用空串（NULL 不参与唯一索引） |
| `keyword` | VARCHAR(128) | N | |
| `payload` | TEXT(JSON) | N | 平台自定义，**不透明** |
| `last_external_id` | VARCHAR(128) | Y | 冗余，便于排查；不作判定依据 |
| `last_published_at` | TIMESTAMPTZ | Y | 冗余时间水位线 |
| `updated_at` | TIMESTAMPTZ | N | |

```sql
CREATE UNIQUE INDEX uk_cursor ON crawl_cursor(platform, channel, keyword);
```

`payload` 结构由各平台 spider 定义：GitHub 用 `{"last_created_at": "...", "last_issue_id": ...}`（按 `created` 升序搜索，时间水位线 + id 去重）；小红书用时间水位线 + note id；抖音用 max_cursor + 雪花 id。**先写 `raw_posts` 提交事务，再推进游标。**

### 3.8 `run_log` — 运行记录

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `run_id` | VARCHAR(64) | N | 一次运行标识 |
| `stage` | VARCHAR(32) | N | collect / clean / score / cluster |
| `status` | VARCHAR(16) | N | pending / running / success / failed / skipped |
| `started_at` | TIMESTAMPTZ | N | |
| `finished_at` | TIMESTAMPTZ | Y | |
| `duration_ms` | INTEGER | Y | |
| `input_count` | INTEGER | Y | |
| `output_count` | INTEGER | Y | |
| `skipped_count` | INTEGER | Y | |
| `error_message` | TEXT | Y | |
| `error_traceback` | TEXT | Y | |
| `stats` | TEXT(JSON) | Y | 等级分布、LLM 调用数、token 数、丢弃原因 |
| `params` | TEXT(JSON) | Y | platform / keyword / stages |

```sql
CREATE INDEX idx_run_runid ON run_log(run_id, stage);
CREATE INDEX idx_run_stage_time ON run_log(stage, started_at DESC);
CREATE INDEX idx_run_status ON run_log(status, started_at DESC);
```

纯审计表：阶段正确性只看业务标记。`stats` 与 `llm_usage` 都可用于统计，但**预算熔断以 `llm_usage` 为准**。

### 3.9 `keyword_seed` — 关键词库

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `keyword` | VARCHAR(128) | N | |
| `platform` | VARCHAR(32) | N | |
| `origin` | VARCHAR(16) | N | seed / feedback |
| `status` | VARCHAR(16) | N | active / paused / retired |
| `weight` | REAL | Y | 调度优先级 |
| `source_signal_ids` | TEXT(JSON) | Y | 反哺来源 |
| `hit_count` | INTEGER | Y | 累计命中 |
| `yield_rate` | REAL | Y | 命中 / 采集 |
| `created_at` | TIMESTAMPTZ | N | |
| `updated_at` | TIMESTAMPTZ | Y | |

```sql
CREATE UNIQUE INDEX uk_keyword ON keyword_seed(platform, keyword);
CREATE INDEX idx_keyword_status ON keyword_seed(status, weight DESC);
```

关键词的**唯一权威源**是本表；platform yaml 里的 `keywords` 只在首次启动时导入 seed。

### 3.10 `llm_usage` — LLM 记账

| 字段 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | INTEGER PK | N | |
| `called_at` | TIMESTAMPTZ | N | |
| `provider` | VARCHAR(32) | N | ollama / openai / claude / deepseek … |
| `model` | VARCHAR(64) | N | |
| `purpose` | VARCHAR(32) | N | score / cluster / generate |
| `prompt_version` | VARCHAR(32) | Y | |
| `input_tokens` | INTEGER | Y | |
| `output_tokens` | INTEGER | Y | |
| `cost` | REAL | Y | 按当时单价估算 |
| `duration_ms` | INTEGER | Y | |
| `cache_hit` | INTEGER(BOOL) | Y | |
| `success` | INTEGER(BOOL) | Y | |
| `error_message` | TEXT | Y | |

```sql
CREATE INDEX idx_llm_time ON llm_usage(called_at DESC);
CREATE INDEX idx_llm_purpose ON llm_usage(purpose, called_at DESC);
```

预算熔断读这张表；`purpose` 用于区分分级与生成的成本。

---

## 4. 幂等与关键查询

### 4.1 执行阶段与标记

| 阶段 | 待处理条件 | 完成标记 |
|---|---|---|
| collect | 游标 + 关键词 | `crawl_cursor` 推进 |
| clean | `cleaned_at IS NULL` | `cleaned_at`；被吸收的重复行同时置 `scored_at` |
| score | `cleaned_at IS NOT NULL AND scored_at IS NULL` 且 hash 不在 signal | `scored_at` + signal/audit 行（同一事务） |
| cluster | A/B 信号且 `clustered_at IS NULL` | 批量 `clustered_at`；有匹配时另写 `product_idea` + `signal_idea` |

> `signal` / `generate` 是 score / cluster 内部的逻辑子步骤，不是独立执行单元。

### 4.2 清洗去重

```sql
-- 1) 取待清洗的 hash
SELECT DISTINCT content_hash FROM raw_posts WHERE cleaned_at IS NULL;

-- 2) 取该 hash 下所有未消费的行（含已清洗未分级）
SELECT * FROM raw_posts WHERE content_hash = :hash AND scored_at IS NULL;
```

代表行在 Python 中按平台加权互动分选出（权重来自 `config/platforms/*.yaml`）：

- 代表行：置 `cleaned_at`，保持 `scored_at = NULL`，进入 score。
- 非代表行：置 `cleaned_at` + `scored_at`（已消费，不送分级）。
- hash 已存在于 `signal`：不新建 signal；把组内平台并入既有 signal 代表的 `source_platforms`，整组置 `cleaned_at` + `scored_at`。
- hash 已存在于 `scorer_audit`（被判过 D/E）：整组消费，不重复调用 LLM。

> 查 `scorer_audit` 需要 join `raw_posts` 取 hash；用一次索引查询换一次 LLM 调用，划算。

### 4.3 归并查询

```sql
SELECT s.id, s.source, s.source_channel, s.title, s.body,
       s.score, s.num_comments, s.dollar_amounts, s.url
FROM signal s
WHERE s.triage_grade IN ('A','B')
  AND s.clustered_at IS NULL
ORDER BY s.source, s.score DESC;

-- signal 万级以内可全取；量大时按 source 分批查询。
-- 不要按 score 全局 LIMIT：score 是平台热度，跨平台不可比。
```

---

## 5. 索引策略

| 场景 | 依赖索引 |
|---|---|
| 采集幂等 | `uk_raw_platform_extid` |
| 清洗待处理 | `idx_raw_pending_clean`（部分索引） |
| 打分待处理 | `idx_raw_pending_score`（部分索引） |
| 去重分组 | `idx_raw_hash` + 待处理部分索引 |
| 按频道分析 | `idx_raw_channel` |
| 归并待处理 | `idx_signal_pending` + `uk_signal_primary` |
| idea 排序 | `idx_idea_total` |
| idea 决策流 | `idx_idea_status` |
| 溯源 | `idx_signal_idea_idea` |
| 成本统计 | `idx_llm_time` |
| 关键词调度 | `idx_keyword_status` |

部分索引是本设计的关键性能手段：待处理行远小于总行数，索引只覆盖待处理部分，查询代价与总表规模无关。

当前共 34 个索引；SQLite 写放大随索引数增长，起步阶段保持现状，观察 `EXPLAIN QUERY PLAN` 后再增删。

---

## 6. SQLite → PostgreSQL

| SQLite | PostgreSQL | 动作 |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL` | 类型替换 |
| `TEXT`（ISO8601） | `TIMESTAMPTZ` | 类型替换 + 格式转换 |
| `INTEGER`（布尔） | `BOOLEAN` | 类型替换 |
| `TEXT`（JSON） | `JSONB` | 类型替换 + 按需 GIN |
| `REAL` | `DOUBLE PRECISION` | 类型替换 |
| 部分索引 | 部分索引 | 语法基本一致 |

迁移触发条件：写入出现明显锁等待 / 数据量超千万 / 需要多机部署。

迁移成本不在 schema，而在**索引策略**与**并发写入**（SQLite 单写者 → PG 并发）。

---

## 7. 待观察项

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | C 级信号长期保留在 `signal` 还是迁 `scorer_audit` | 只占存储，不参与归并 | 先保留；4 周内无人工使用则迁移 |
| 2 | comments 采集阈值 | 体量是帖子的 50–100 倍 | 只采高互动帖；阈值在实现时定 |
| 3 | idea 重复的召回 | 同一机会可能在多批生成 | 当前接受；重复率 >10% 再引入相似度召回 |
| 4 | `scorer_audit` 保留策略 | D/E 占 80%，增长快 | 保留 90 天，或只留疑似误杀 |
