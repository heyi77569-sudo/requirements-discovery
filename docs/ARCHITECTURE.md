# 需求痛点发现系统 — 架构设计

> 版本：v3.1｜日期：2026-11-06
>
> 配套文档：[SCHEMA.md](./SCHEMA.md)（字段级定义）｜Prompt：`config/prompts/`｜规则数据：`config/phrases/`

从多个内容平台的真实用户发言中提取**痛点信号**，再用 LLM 转成**可执行的产品机会假设**。系统的价值锚点是 idea 的质量与可追溯性，不是采集量或主题划分精度。

## 目录

1. [目标与范围](#1-目标与范围)
2. [系统总览](#2-系统总览)
3. [设计取舍](#3-设计取舍)
4. [处理管线](#4-处理管线)
5. [数据模型](#5-数据模型)
6. [运行与成本](#6-运行与成本)
7. [风险](#7-风险)
8. [代码组织](#8-代码组织)

---

## 1. 目标与范围

### 1.1 目标

- 从 GitHub、小红书、闲鱼等平台采集真实发言：抱怨、求助、询价、吐槽。
- 用「采集层规则过滤 + LLM 分级」把噪音压到可处理规模。
- 由 LLM 直接归纳产品机会，输出带原文证据的假设与可解释排序。
- 每条 idea 可回溯到 signal 与原始帖子。

**成功标准**：产品经理愿意基于产出的 idea 做下一步验证，而不是只看不动作。

### 1.2 非目标

- ❌ 通用舆情 / 情感分析
- ❌ 全网采集平台（只做少数高价值平台）
- ❌ 自动决策（输出是**假设**，不是结论）
- ❌ 实时流分析（按批处理周期运行）
- ❌ 可视化产品看板（人工干预走 CLI，交付 Markdown / CSV）

### 1.3 用户与交付

面向产品经理 / 创业者（找真实需求）、产品运营（验证需求）、竞品分析（找空白）。交付两种文件：

- **Markdown / CSV 榜单**：按评分排序，含原文引文与链接。
- **CLI**：signal 提级、idea 状态变更（new / tracking / active / rejected / archived）、手动重跑、重新归并。

### 1.4 数据源优先级

GitHub（无账号封禁风险、英文规则）→ 小红书（中文主力）→ 闲鱼（需求表达最直白）→ 抖音 / 视频号（成本最高，最后做）；微博 / 知乎作补充。

---

## 2. 系统总览

### 2.1 核心链路

```
种子关键词（platform yaml / keyword_seed）
   │
   ▼
① collect（Scrapy 独立进程）
   │  采集层过滤：不通过不落库
   ▼
raw_posts
   │
   ▼
② clean（按 content_hash 去重取代表）
   │
   ▼
③ score（LLM 批量定级）
   ├── A/B/C → signal
   └── D/E   → scorer_audit
   │
   ▼
④ cluster + generate（LLM 归并 + idea + 六维评分）
   │
   ▼
product_idea → ⑤ export（Markdown / CSV）
```

### 2.2 模块职责

| 模块 | 职责 | 不负责 |
|---|---|---|
| `scheduler/` | 触发时机、错峰、全局配额、告警 | 阶段内部逻辑 |
| `workflow/` | 阶段编排、幂等、重跑、导出 | 业务判断 |
| `crawler/` | 平台采集、反爬、平台内归一、写 `raw_posts` | 跨平台去重、噪音判断、分级 |
| `engine/` | 跨平台清洗、去重、LLM 分级、归并、生成 | 网络细节、平台字段映射 |
| `storage/` | CRUD、schema、索引、迁移 | 业务规则 |
| `config/` | 配置加载与校验、Prompt、词表 | 业务逻辑 |
| `core/` | 纯函数：类型、归一化、短语匹配 | 任何 I/O、配置读取 |

### 2.3 依赖规则

```
scheduler → workflow → { crawler, engine } → storage → config
                              │        │
                              └── core ┘
```

- 严格单向向下，不允许反向依赖。
- `crawler` 与 `engine` 并行，通过 `raw_posts` 解耦，没有直接调用。
- `crawler` 不知道 `engine` 存在；`engine` 不处理平台字段差异，但可用 `source` 做分组统计。
- `storage` 只做 CRUD；`config` 被所有层依赖，自身不依赖任何层。
- `core` 不读配置文件、不做 I/O；短语与归一化规则由 `config/loader.py` 加载后传入。

### 2.4 执行阶段

| 阶段 | 输入 | 输出 | 完成标记 |
|---|---|---|---|
| **collect** | 关键词 + 游标 | `raw_posts` | `crawl_cursor` 推进（写库之后） |
| **clean** | `cleaned_at IS NULL` | 代表行进入待分级；重复行消费 | `cleaned_at`（被吸收行同时 `scored_at`） |
| **score** | 代表行 | `signal` / `scorer_audit` | `scored_at` + 产物行（同一事务） |
| **cluster** | A/B 且 `clustered_at IS NULL` | 有匹配时写 `product_idea` + `signal_idea` | 批量 `clustered_at`（空结果也消费） |

`signal` 与 `generate` 是 score / cluster 内部的逻辑步骤，不是独立执行单元；`export` 是 CLI 动作，不是阶段。每个阶段都是**捞一次 → 处理完 → 返回**，不做循环等待。

触发方式：起步用 CLI 手动串行执行；后续接 APScheduler，采集与处理不并发写。

---

## 3. 设计取舍

每条写明：选了什么、放弃了什么、什么条件下重估。重估条件触发时再改设计，这里不留模糊的 TBD。

| # | 议题 | 选择 | 放弃的替代 | 重估条件 |
|---|---|---|---|---|
| 1 | 归并方式 | LLM 直接归纳，validator + 溯源兜底 | 向量聚类 / 主题模型 | 需要主题级统计 / 可复现聚类，或 signal 超过 10 万 |
| 2 | 采集接法 | Scrapy 独立进程，Pipeline 直写 `raw_posts` | `CrawlerProcess` 嵌入；httpx 双实现 | 需要统一 async，或平台只剩纯 API |
| 3 | 归一化 | 平台字段映射在 spider，通用规则在 `core` | 配置化 `field_mapping` | 平台 ≥5 且响应结构高度同质 |
| 4 | 平台原始响应 | 不存完整 JSON / HTML，只留统一字段 + `url` | 存原始响应 | 平台改版且回源不可行 |
| 5 | 过滤位置 | 采集层前置，两段式 | 规则召回 + 灰区层 | LLM 成本超预算，且采集层已收紧仍不够 |
| 6 | 幂等与执行 | 业务标记字段 + 捞一次返回 | 状态机表 / workflow 常驻循环 | 需要跨阶段回滚，或需要准实时 |
| 7 | 存储 | SQLite + WAL，保留 PG 迁移路径 | PostgreSQL | 锁等待明显 / 超千万 / 多机部署 |
| 8 | signal 消费 | `clustered_at` 批次消费 | `NOT EXISTS is_primary` | 需要按批次重放或回滚 |
| 9 | 关联模型 | `signal_idea` + `is_primary` 布尔 + 部分唯一；不强制每条 idea 有 primary | `relation` 枚举 + `weight`；每条 idea 必须 primary | 需要证据权重排序，或必须固定主证据 |
| 10 | C 级 | 暂存 `signal`，不参与归并 | 迁 `scorer_audit` / 直接丢弃 | 4 周内无人工使用 |
| 11 | 去重范围 | `signal` + `scorer_audit`（D/E） | 只查 `signal` | 模型升级后需要重判历史内容 |
| 12 | GitHub 游标 | `created` 时间水位线 + id；1000 条内按时间窗切分 | issue number 单调递增 | Search API 语义变化 |
| 13 | 排序公式 | 六维乘积 × 框架倍数 | 加权和 | 系数标定后发现乘积过于极端 |
| 14 | 分级语义 | 自定义 A~E | 参考实现的 C/D 语义 | 与外部标注集合并 |
| 15 | 重复 idea | 暂不做相似度召回 | 向量召回 | 重复率 >10% 或人工反馈强烈 |
| 16 | 交互形态 | CLI + Markdown / CSV | Web 看板 | 多人协作 / 需要可视化 |
| 17 | comments | 只采高互动帖的评论（后续阶段，不进入当前信号链路） | 全量评论 | 需要评论做趋势或证据 |
| 18 | 审计保留 | D/E 保留 90 天，或只留疑似误杀 | 全量长期保留 | 需要历史训练 / 合规审计 |
| 19 | 消费标记 | `scored_at` 复用为"已消费" | 独立 `deduped_at` 列 | 语义混淆导致查询 / 审计错误 |
| 20 | 归并批次 | 每组 ≤20、总批 ≤60，超出分批 | 单批不限 | token 成本或召回率变化 |
| 21 | 规则命中 | score 阶段复算 `rule_hits` | crawl 时落 `filter_hits` 列 | 复算成本高，或需要原始命中 |

---

## 4. 处理管线

### 4.1 collect

**接法**：Scrapy 作为独立进程运行（`scrapy crawl <platform>`），Item Pipeline 直接写 `raw_posts`。嵌入 `CrawlerProcess` 会与 asyncio 冲突，且 spider 崩溃会带崩 workflow。

**Spider 契约**：每个平台产出同一组字段，平台差异在 spider 内抹平。

| 字段 | 说明 |
|---|---|
| `platform` | 平台标识 |
| `channel` | 平台内频道 / 分区 / 话题（GitHub repo、XHS tag） |
| `external_id` | 平台内唯一 ID |
| `content_hash` | 归一化 SHA256，**采集时算定，不可变** |
| `search_keyword` | 命中的检索词 |
| `url` | 原文链接（回源） |
| `author` | 昵称，采集时脱敏 |
| `published_at` | 发布时间，可空 |
| `collected_at` | 采集时间 |
| `source_platforms` | 初始 `[platform]`，跨平台去重时合并 |
| `title` / `content` | 标题 / 正文（纯文本） |
| `likes` / `comments_count` / `collects` | 互动指标，缺失为 `NULL` |

字段映射、类型转换（`"1.2万"` → `12000`）、缺失兜底、时间解析全部写在 spider；`content_hash` 调 `core.normalize.content_hash()`，不自己实现。

**采集层过滤**（不通过不落库）：

```
通过 = 长度合规 AND 未命中噪音词 AND (互动量达标 OR 短语命中)

长度合规   = 标题在区间内（有标题时） AND (正文为空 OR len(正文) >= min_body_length)
互动量达标 = likes >= min_likes OR comments_count >= min_comments
短语命中   = Tier1 命中 OR (Tier2 命中 AND 有佐证)
佐证       = 价格信号 ∪ 工具信号 ∪ 金额
```

| 平台 | 互动量 | 标题 | 正文 |
|---|---|---|---|
| 小红书 | likes ≥ 5 或 comments ≥ 3 | 3–200 | ≥ 15 |
| GitHub | likes ≥ 5 或 comments ≥ 3 | 10–200 | ≥ 30 |

互动量阈值按平台配置，且是 OR——痛点帖典型特征是评论多、点赞少。

短语匹配使用**标题 + 正文**；正文为空的条目走短语 / 互动量判定，不会被长度门槛直接丢掉。

**短语规则**（`config/phrases/{en,zh}/`，匹配在 `core/phrases.py`）：

| 词表 | 作用 | en | zh |
|---|---|---|---|
| `tier1` | 高置信痛点，命中即保留 | 264 | 252 |
| `tier2` | 上下文依赖，需佐证 | 62 | 113 |
| `price_signals` | 价格 / 商业信号 | 57 | 85 |
| `tool_signals` | 工具 / 载体信号 | 47 | 54 |

相对参考实现 [`albertorsesc/pain-discovery`](https://github.com/albertorsesc/pain-discovery) 做了三点：词边界、词形后缀、补齐工具信号。已知局限：后缀变体 `s` 会让 `am i beings` 仍命中 `am i being`，词边界不是安全边界；词表是枚举，覆盖不了全部同义表达。

**噪音词**：命中即丢弃，优先级高于所有"通过"条件。按平台配置——"求购"在闲鱼是需求信号，在小红书是带货信号。

**误杀控制**：被丢弃内容不进 `raw_posts`，调词表是永久性的。因此起步阶段用宽松阈值，丢弃原因记 `run_log`，并按天抽样"未命中但互动量高"的内容到 JSONL（保留 7–14 天），用于评估召回率。

**执行顺序**：查库去重（`platform + external_id`）→ 判定 → 落库。

**增量游标**：游标状态（`cursor_state`）由平台 spider 定义。GitHub Search API 跨 repo 返回结果，issue number **不是全局单调**，因此用 `created` 时间水位线 + issue id：`{"last_created_at": "...", "last_issue_id": ...}`，按 `created` 升序搜索并加 `created:>水位线`；单查询最多 1000 条，超出时按时间窗二分，`lookback_window: 300` 秒重采边界，`external_id` 去重。小红书用时间水位线 + note id；抖音用 max_cursor + 雪花 id。

硬约束：**写库提交事务后再推进游标**；反过来会永久丢数据。

**关键词**：唯一权威源是 `keyword_seed`；platform yaml 的 `keywords` 只做首次导入，反哺词以 `origin=feedback` 写回，后续阶段实现。

### 4.2 clean

**归一化**（`core/normalize.py`，spider 与 engine 共用）：

```python
def normalize_for_hash(text: str) -> str:
    # 1) NFKC（全角→半角）  2) 小写
    # 3) 去掉全部空白（空格、\t、\r、\n、\u3000）
    # 4) 去 emoji、话题标签、@提及、水印文案

def content_hash(content: str, platform: str, external_id: str) -> str:
    # 正文非空 → sha256(normalize_for_hash(content))
    # 正文为空 → sha256(platform + external_id)（唯一但不参与跨平台去重）
```

- hash 只算一次，永不回溯重算；规则变更只影响新数据。
- 去标签 / 水印必须发生在 hash 之前，否则搬运帖加个 `#tag` 就跨平台去不了重。
- 展示清洗（保留可读空格、折叠空行）在写 `signal.body` 时执行，不回写 `raw_posts`。

**去重流程**：

1. 取 `cleaned_at IS NULL` 的行，得到待处理 hash 集合。
2. 对每个 hash，取所有 `scored_at IS NULL` 的同 hash 行（含此前已清洗未分级的行）一起比较。
3. 按平台加权互动分选代表：`base + likes×w + comments_count×w + collects×w`，权重来自平台配置，Python 计算，不落列。
4. 非代表行：置 `cleaned_at` + `scored_at`，表示已消费、不送分级。
5. 若 `signal` 已存在该 hash：不新建 signal，把组内平台并入既有 signal 代表的 `source_platforms`，整组置 `cleaned_at` + `scored_at`。
6. 若该 hash 已在 `scorer_audit`（D/E）中判过：整组置 `cleaned_at` + `scored_at`，不重复调用 LLM。

> 第 6 步需要 join `raw_posts` 取 hash；用一次索引查询换一次 LLM 调用，划算。

语义边界：

- `raw_posts.content_hash` **不唯一**，原始层保留全部采集事实（转发、重复上架、duplicate issue）。
- `signal.content_hash` **唯一**，signal 已去重。
- `source_platforms` 是唯一允许 UPDATE 的业务字段，记录观察结果，不是内容改写。
- hash 归一化是**激进**的：去掉全部空白、标签与水印，接受少量误合并，换取搬运帖的高召回。

### 4.3 score

未被去重吸收的行全部送 LLM，一次定级。没有规则召回 / 灰区层。

**输出契约**（批量）：

```json
[
  {
    "id": 123,
    "grade": "A",
    "pain_score": 0,
    "pain_type": "功能缺失",
    "target_user": "小微餐饮店主",
    "willingness_to_pay": "高",
    "dollar_amounts": "5000-8000/套",
    "reason": "现有工具无法对账，正在找替代方案"
  }
]
```

- `pain_type`：功能缺失 / 体验差 / 效率低 / 成本高 / 集成难 / 可靠性 / 其他
- `willingness_to_pay`：高 / 中 / 低 / 不明
- `is_pain` 可由 grade 推导，不单独输出。
- `dollar_amounts` 优先由 triage 提取；规则正则作为兜底，金额模式待补进 `config/scoring.yaml`。

| 级别 | 判定 | 处理 | 预期占比 |
|---|---|---|---|
| A | 明确痛点 + 人群 + 付费意愿 / 找替代 | 入 signal，优先生成 | ~2% |
| B | 明确痛点，人群或付费意愿模糊 | 入 signal，标注待验证 | ~6% |
| C | 隐含需求 / 含蓄抱怨 | 入 signal，不触发归并 | ~12% |
| D | 弱信号，只有情绪 | 只进 `scorer_audit` | ~20% |
| E | 噪音 / 广告 / 无关 | 只进 `scorer_audit` | ~60% |

- 写 `signal` 时同事务写 `scored_at`；D/E 写 `scorer_audit`。
- 写 `signal` 时用 `core/phrases.py` 对原文复算命中短语，写 `signal.rule_hits`，用于解释准入。
- 写 `signal.body` 时做展示清洗；同时记录 `model`、`prompt_version`、`reason`。
- C 级不进入归并，`clustered_at` 保持 `NULL`。
- 解析失败重试 1 次；仍失败不伪造等级——不写 `scored_at`，记 `run_log`，下轮重试；同批其余行正常提交。

### 4.4 cluster + generate

**触发条件**（先判全池，再分组）：

1. 取 `triage_grade IN ('A','B')` 且 `clustered_at IS NULL` 的 signal（按 source 分批取，不按跨平台 `score` 全局排序）。
2. **A/B 未归并全池** < 3 → 返回 `skipped`，等下一轮。
3. 按 `source` 分组（组内按 `score` 排序），每组最多 20 条，**总批最多 60 条**，超出分批；不足 3 条的组与零散信号合并成跨平台小批次。
4. LLM 输出机会列表 + `signal_ids`。
5. 校验 `signal_ids` 必须来自本批输入，否则丢弃该机会。
6. 同一事务：有机会则写 `product_idea` + `signal_idea`；无论是否匹配，把**本批所有** A/B signal 的 `clustered_at` 置为当前时间。

归属与消费：

- 每条 signal 至多一个 `is_primary = 1` 的关联（部分唯一索引）；其余关联 `is_primary = 0` 表示辅助证据。
- 主证据分配：按 idea 的 `total_score` 降序处理（同分按输出顺序）；每个 idea 内按 `triage_grade`（A>B>C）、`pain_score`、`score` 依次降序，把第一个尚未被占用的 signal 置 primary。允许 idea 没有 primary（证据全为辅助）。
- 未被任何 idea 引用的 signal 也会被 `clustered_at` 消费，不会每轮重新捞出。
- PM 否决 idea：置 `status = rejected`，删除其 `signal_idea` 关联；只对因此失去全部关联的 signal 清空 `clustered_at`，其余保持消费。
- PM 提级 signal：把 `triage_grade` 提升为 A（或 B），清空 `clustered_at` 与关联，强制进入下一批（可绕过全池门槛）。
- LLM 失败、输出不可解析，或全部机会都因校验失败被丢弃：不写 `clustered_at`，整批下轮重试；LLM 明确返回空数组（确实没有机会）则正常消费。
- 部分机会校验失败：其余机会照常写入，本批 signal 仍全部消费；失败机会对应的 signal 视为"未匹配"，不单独重试。

**输出契约**：

```json
[
  {
    "name": "PriceCheck AI",
    "product_idea": "具体做什么，技术与 UX",
    "buyer": "谁买，处境与紧迫性",
    "why_pay": "ROI / 痛点缓解",
    "current_solutions_and_gaps": {"existing": "现有凑合方案", "gaps": "为什么不行"},
    "sample_quotes": ["原文子串"],
    "confidence": "high",
    "opportunity_type": "autopilot-service",
    "copilot_or_autopilot": "autopilot",
    "signal_ids": [123, 456],
    "buyer_income": 5, "usage_frequency": 5, "alternative_gap": 4,
    "buyer_unsophistication": 5, "buildability": 3, "integration_depth": 5,
    "is_boring_industry": true, "is_abandoned_work": false,
    "is_already_outsourced": true, "has_dual_signal": true,
    "is_intelligence_gap": true,
    "reasoning": "一段推理"
  }
]
```

`current_solutions_and_gaps`、`sample_quotes`、`confidence` 必填；引文必须是源 signal 文本的子串。写入时 `signal_ids` 映射为 `source_signal_ids` 快照，并插入 `signal_idea`。

**六维评分**（1–5，与 idea 同一次调用）：

| 维度 | 5 分 | 1 分 |
|---|---|---|
| `buyer_income` | 年预算 $500K+ 的企业 | 破产消费者 |
| `usage_frequency` | 每天 | 一次性 |
| `alternative_gap` | 没有任何方案 | 已完全解决 |
| `buyer_unsophistication` | 不懂技术的蓝领 | 开发者 |
| `buildability` | 2 个月 MVP | 需要团队 |
| `integration_depth` | 替代日常流程 | 可有可无 |

框架标记：`is_boring_industry`、`is_abandoned_work`、`is_already_outsourced`、`has_dual_signal`、`is_intelligence_gap`。

**排序公式**（`config/scoring.yaml`）：

```
base  = 六维乘积
mult  = 命中标记的倍数连乘（1.5 / 1.4 / 1.3 / 1.5 / 1.2）
total = base × mult
```

落库 `total_score` + `scoring_version`，排序直接 `ORDER BY total_score DESC`。`config/scoring.yaml` 支持 `method: product`（默认）与 `weighted_sum`，切换不改代码。系数是先验值，用 PM 反馈标定。

**校验**：

| 校验 | 规则 | 失败 |
|---|---|---|
| JSON 结构 | Schema | 重试 1 次，仍失败丢弃 |
| 溯源 | `signal_ids` 非空、存在、来自本批 | 丢弃 idea |
| 引文 | `sample_quotes` 是源文本子串 | 删引文；样本不足降 confidence |
| 枚举 | pain_type / confidence / opportunity_type | 修正或丢弃 |
| 分数 | 六维 1–5 | 修正或丢弃 |
| 幻觉 | 数字 / 竞品名可溯源 | 标记待人工复核 |

### 4.5 export

`workflow/export.py` 读 `product_idea`，按 `total_score DESC` 输出 Markdown / CSV：名称、一句话、买家、付费理由、六维分、框架标记、引文、原文链接、关联 signal 数。默认只导出 `status != 'rejected'` 的机会。

---

## 5. 数据模型

### 5.1 表

| 表 | 作用 | 关键约束 |
|---|---|---|
| `raw_posts` | 原始采集（统一字段，无原始响应） | `(platform, external_id)` 唯一；`content_hash` 普通索引 |
| `comments` | 评论（后续阶段） | 独立粒度；不进入当前信号链路 |
| `signal` | 分级后的信号（A/B/C） | `content_hash` 唯一；`clustered_at` 归并消费 |
| `product_idea` | 产品机会 + 六维评分 | `total_score` 有索引 |
| `signal_idea` | signal ↔ idea | `is_primary` 部分唯一 |
| `scorer_audit` | D/E 留痕 | 只存 D/E |
| `crawl_cursor` | 增量游标 | `(platform, channel, keyword)` 唯一 |
| `run_log` | 阶段运行记录 | 纯审计 |
| `keyword_seed` | 种子词 + 反哺词 | 关键词唯一权威源 |
| `llm_usage` | LLM 记账 | 预算熔断数据源 |

字段、索引、迁移映射见 [SCHEMA.md](./SCHEMA.md)。

### 5.2 不变量

- **原始层只写不改**：业务字段由 spider 一次填入；例外只有 `source_platforms` 合并。
- **`scored_at` = 已消费**：代表行是"已分级"，被去重吸收的行是"无需分级"。
- **`clustered_at` = 已归并消费**：匹配到 idea 的信号与"判过但没匹配"的信号都会置值；C 级不参与归并，保持 `NULL`。
- **`is_primary` 只表达归属**：幂等由 `clustered_at` 承担，不要求每条 idea 都有 primary。
- **溯源强制**：idea 必须关联至少一条 signal；`source_signal_ids` 保留生成时快照。
- **关键词以 `keyword_seed` 为准**：platform yaml 的 `keywords` 只做首次导入。

### 5.3 存储与索引

- SQLite + WAL + `busy_timeout=5s`；单写者，采集与处理不同时写。
- 部分索引覆盖"待处理"：`cleaned_at IS NULL` / `scored_at IS NULL`，查询代价与总表规模无关。
- 排序、状态、溯源各有索引；当前 34 个（见 [SCHEMA.md](./SCHEMA.md) 第 5 章）。
- DDL 只在 `storage/migrations/`；应用代码禁止 `CREATE TABLE`。

迁移 PostgreSQL 的触发条件：写入明显锁等待 / 数据量超千万 / 需要多机部署。迁移成本在索引策略与并发写入，不在 schema 类型。

---

## 6. 运行与成本

### 6.1 配置与 Prompt

```
config/
├── settings.py / loader.py        # 全局配置 + 校验
├── platforms/*.yaml               # 平台参数、阈值、噪音词、关键词
├── phrases/{en,zh}/               # 四张词表
├── prompts/{triage,idea_generation}.md
├── scoring.yaml                   # 排序公式 + 倍数 + 版本
├── llm.yaml                       # provider、模型、预算
└── workflow.yaml                  # 批大小、阶段、调度
```

- Prompt 独立文件、可版本化、可 A/B；`prompt_version` 写入 signal / product_idea。
- `config/loader.py` 用 Pydantic 校验，错误在启动时暴露。
- 代码不硬编码平台细节与阈值。
- 现有 platform yaml 还缺 `filter.noise_words`，落地时补；词表按平台分开。

### 6.2 LLM 与成本

- `LLMProvider` 抽象：Ollama / OpenAI / Claude / 通义 / DeepSeek，`llm.yaml` 切换。
- 默认分级用本地小模型（量大、成本敏感），生成用云端大模型（量小、质量敏感）。
- 结构化输出用 JSON Schema；不支持时退化为 prompt 约束 + 解析重试，再由 validator 兜底。

| 降本手段 | 说明 |
|---|---|
| 采集层过滤 | 主杠杆：噪音不落库、不进 LLM |
| 批量调用 | 一批 raw_posts / signal → 一次调用 |
| 信号门槛 | 全池 ≥ 3 才生成，避免碎片化调用 |
| 分层模型 | 分级小模型、生成大模型 |
| 预算熔断 | 日 token / 月成本上限；超限暂停或跳过生成，不做"降级到规则" |

### 6.3 采集约束与合规

- UA / 代理轮换，令牌桶限速 + 随机抖动（小红书 12 req/min，GitHub Search API 30 req/min）。
- 指数退避重试；登录态 Cookie 独立文件，不入库、不入 git。
- 小红书需 Playwright + 签名；GitHub 用官方 Search API（受独立配额约束，单查询最多 1000 条结果）。
- 合规：优先官方 API；昵称哈希；只做引用式摘要；商用前确认 ToS 与版权。

### 6.4 Prompt 与契约落地

- triage prompt 改为批量：`[{id,title,body}]` → `[{id,grade,pain_score,...}]`，删 `is_pain`，补 `dollar_amounts`。
- idea prompt 补 `confidence`；其余字段与 §4.4 契约一致。
- 两个 prompt 的版本号分别写入 `signal.prompt_version` / `product_idea.prompt_version`。
- 现有 `core/phrases.py` 直读 `config/phrases/`，需改为由 `config/loader` 注入规则，否则 `core` 反向依赖 config 目录。

### 6.5 可观测性

- `run_log`：每阶段状态、耗时、输入/输出数、丢弃原因、LLM token 统计。
- `llm_usage`：每次调用一行，预算熔断与成本归因以它为准。
- 失败信号：连续失败、被封、空产出，均由 scheduler / CLI 报出。

---

## 7. 风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 反爬 / 封号 | 数据源断供 | 先 GitHub；登录态隔离；代理与随机节奏；第三方数据平台 fallback |
| LLM 幻觉 | idea 不可信 | 强制 signal_ids + 引文子串校验 + 枚举 / 范围校验；输出定位为假设 |
| 分级质量 | 后续全错 | 起步宽松 + 人工抽检；`signal` / `scorer_audit` 反查；标注足够后训练轻量分类器 |
| 成本失控 | 账单超预算 | 采集层过滤 + 批量 + 分层模型 + 熔断；超预算先收紧采集阈值 |
| SQLite 单写者 | 锁等待 / 写入失败 | WAL + busy_timeout + 错峰 + 重试；达到阈值迁 PG |
| 合规 | 法律风险 | 官方 API 优先；昵称哈希；引用式摘要；商用前法务确认 |

---

## 8. 代码组织

```
core/        types / normalize / phrases（纯函数）
crawler/     spiders / pipelines / middlewares / runner
engine/      cleaner / scorer / cluster / idea_generator / validator / llm_client
storage/     db / migrations / *_repo
workflow/    stages / runner / cli / export
config/      loader / platforms / phrases / prompts / *.yaml
scheduler/   后续阶段
tests/       单测 + fixtures
docs/        ARCHITECTURE.md / SCHEMA.md
```

- Python 3.11+；类型注解必填；Pydantic v2 守边界；结构化日志。
- 采集走 `subprocess`（同步），LLM 可 async；workflow 不做全 async。
- 新增平台 = spider + registry + platform yaml，不改 engine。
- `workflow/cli.py` 提供 `run` / `export` / signal 提级 / idea 状态变更。
- `core` / `engine` 关键模块必须有单测；`tests/fixtures` 存真实样本。
