# 人物画像插件：设计草案（v5，历史归档）

> 状态：**已归档，不再维护**；当前设计见 [`../DESIGN.md`](../DESIGN.md)。
> 本文保留早期分层/存储/注入讨论，文中“守护进程”“V1 表”“bot_set”等方案已废弃，
> **不要按本文实现**。
> 日期：2026-09-12（v5）
> 工作名：`portrait`（当时未定稿）

## 0. 一句话概括

**一个常驻进程，读一张物理去重消息表，按会话游标增量萃取，每日把候选合并进中性分层画像，
再为每个 persona 预渲染一份说法，由极薄插件在对话时粘贴。**

---

## 1. 前提假设与数据源

### 1.1 唯一的数据来源假设（本设计的地基）

```text
chat_memory_messages        物理消息，跨 BOT 实例唯一一行
  msg_uid            TEXT PK        -- "p:<platform>:<type>:<session>:<uid>" 或 "l:<fp>:<n>"
  fingerprint        TEXT           -- md5(platform_name|message_type|session_id|sender_id
                                              |platform_timestamp|content)
  seq                INTEGER        -- ★会话内递增序号，跨实例一致（唯一索引含它）
  platform_name      TEXT           -- 平台类型（如 qq）
  message_type       TEXT           -- GroupMessage / FriendMessage
  session_id         TEXT           -- 群号或用户号
  sender_id          TEXT           -- ★入站=用户 id，出站=self_id（bot 账号）
  sender_nickname    TEXT
  platform_timestamp INTEGER        -- 平台时间（跨实例一致）
  content            TEXT           -- 正文（渲染后）
  content_kind       TEXT           -- 内容类型
  relation_data      TEXT
  at_id              TEXT
  recalled_at        DATETIME       -- 撤回是客观事件，按消息维度
  deleted_at         DATETIME       -- 软删按消息维度
  created_at         DATETIME

chat_memory_message_bots    参与行，一行 = 某实例在某会话看到/发出这条消息
  msg_uid / platform_id / self_id / conversation_id / umo / role
  platform_message_id / reply_id / forward_id / turn_id / pair_id
  llm_status / send_status / persona_id / target_id / raw_timestamp
  UNIQUE(msg_uid, platform_id, self_id, conversation_id)
```

**「一张表」带来的一条铁律：分析单元是「物理会话」，不是 UMO。**

| 旧形态（复存表） | 新形态（去重表） |
| --- | --- |
| 分析单元 = UMO（含 `platform_id`），同一群 ×3 个 BOT = 3 份重复工作 | 分析单元 = `(platform_name, message_type, session_id)`，**同一群只有一份** |
| 游标 = `(created_at, record_id)`，`record_id` 是每实例自增 | 游标 = `(session, seq)`，`seq` 跨实例一致且单调 |
| 主体 key = `platform_id:user_id`（一个真人可能分裂成 3 个主体） | 主体 key = **`platform_name:sender_id`**（一个真人一个主体） |
| 重复读取系数 ≈ 2.48 | **1.0** |

### 1.2 关于物理消息表的实测依据（来自 CM 文档与代码）

- 复存是旧表的常态：97.6% 的行落在多实例会话，按「会话+发送者+平台秒+正文 md5」合并后
  339,461 行 → 136,699 条物理消息，平均 **2.48 份/条**。
- 平台 `message_id` 跨实例一致率仅 **0.03%**（115,135 组里 31 组）→ 绝不能做物理主键；
  真正的平台共享 id 是 NapCat 的 `real_seq`（服务器 `msgSeq`），落在 `platform_uid`。
- 仅用「会话+发送者+平台秒」做键，有 **8.54%** 的键含多种正文 → 正文 md5 必须进指纹。
- 同一条消息在不同实例眼里可能是不同分段：「发送侧全文 vs 观察侧分段」9,602 组里
  strip 后一致 0 组（长度比中位 0.56）→ **不要按内容互相覆写**。
- `seq` 是会话内递增序号，唯一索引 `(platform_name, message_type, session_id, seq)`，
  是排序、游标、展示的公共键 —— 正好是我们的 keyset 游标。
- **`msg_uid` 不是自增整数**（是 `p:…`/`l:…` 字符串），不能拿来做游标。
- 物理表刻意不含 `umo`/`platform_id`/`persona_id`/`llm_status`：
  「哪个实例看的」「哪一轮进了 LLM」「当时是什么人设」都是**每实例状态**，在参与行里。
- 影子表自身的 schema 版本在 `chat_memory_shadow_meta`，**版本不等时 `DROP` 重建、不迁移数据**
  （阶段一明确不迁移影子数据）→ 不能把它当唯一数据源做长期回填。

> ⚠️ **落地前提核对**：以上是 CM `shared_store.py` 的**目标**结构（`SHADOW_SCHEMA_VERSION=15`）。
> 截至 2026-09-12，部署端库还是旧形态（`user_version=7`，只有 4 张旧表），
> `shared_store.py` 仍是工作区未提交文件。**本设计从「切读新表」那一刻开始生效**；
> 在那之前需要一份最小兼容（§10.3），不做双实现。

### 1.3 其它数据源

| 需要的东西 | 在哪 | 说明 |
| --- | --- | --- |
| persona 人格文本 | `data/data_v4.db` 的 `personas` 表（`persona_id` + `system_prompt`） | 只读；实测 51 条 |
| LLM Provider（含 key） | `data/cmd_config.json` 的 `provider[]` + `provider_sources[]` | **带 UTF-8 BOM，用 `utf-8-sig` 读** |
| 运行时 | AstrBot 自带 `backend/python/python.exe`（3.12.12） | `sqlite3`/`httpx`/`requests`/`openai 3.6.0` 都在，**`litellm` 不在** → 不 import AstrBot 源码 |
| 输出 | 自己的 `profile.db` | 与 CM 完全分离，只读 CM、不写 CM |

---

## 2. 已确认的决策

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | 存储用 **SQLite 单库**（`profile.db`） | 条目级 CRUD、幂等唯一键、版本历史、部分查询 |
| D2 | **一份中性画像**；主体 = `platform_name:sender_id`，persona 只出现在证据与渲染缓存 | 同一实例内不按人设分裂事实；跨实例也不分裂主体 |
| D3 | **不依赖任何额外插件**（包括 `chat_memory_extractor`） | 开箱即用；它只省重复代码，却让「装一个插件」变成「装两个」 |
| D4 | 几周内的具体事件**不进画像**，交给 CM 上下文接管 + LM 召回 | 画像只管 mid/long 层稳定属性 |
| D5 | 每 N 条消息（默认 100）萃取一次 → 本地暂存；每日固定时间一次 LLM 评估全部暂存结果后落库 | 抽取与合并两段，合并是唯一写画像的入口 |
| D6 | **单实例视角**：其它 BOT 在本实例眼里就是普通用户 | 不需要维护「哪些账号是 Bot」的名单 |
| D7 | **单进程常驻萃取器**：萃取/合并/分层/渲染全在一个独立进程里 | 无论多少 BOT、多少 persona，只跑一份 |
| D8 | 萃取器与注入插件**解耦为两个交付物**，注入插件只做「查表 + 粘贴」 | 画像逻辑迭代不用动注入插件 |
| D9 | **WebUI 配置项即开关**；单例靠守护进程自己的 OS 文件锁 | 配置在插件侧、保证在进程侧 |
| D10 | 分析单元是**物理会话**，游标是 `(session, seq)` | 去重表的直接推论，也是本版与 v4 最大的区别 |
| D11 | 绝不用「内容更全的那份」覆盖观察侧分段 | 同一条消息在不同实例眼里可能不同分段，覆写会造出用户没说过的话 |

---

## 3. 总体架构

```text
                    ┌──────────────── AstrBot 进程（可有多个 BOT）────────────────┐
平台消息 ──┬─→ chat_memory ──→ chat_memory_messages（物理去重）+ message_bots（参与行）
           │                          │
           │                          │ 只读 SQL（mode=ro）
           │                          ▼
           │            ┌──────────────────────────────────────────┐
           └─→ 各 BOT    │  portrait daemon（常驻单进程）      │
               对话 → LLM │   ① 会话扫描 → 窗口读取（seq 游标）        │
                          │   ② 窗口萃取（LLM）→ 候选暂存             │
                          │   ③ 每日合并（LLM）→ 中性分层画像           │
                          │   ④ 按 persona 预渲染                     │
                          └──────────────────────────────────────────┘
                                          │ 写 profile.db（WAL）
                                          ▼
                    ┌──────── 可选：极薄注入插件（每个 BOT 各装一份）────────┐
                    │  on_llm_request → 查 (subject, persona) 渲染 → 粘贴     │
                    └────────────────────────────────────────────────────┘
```

数据流三条边界：

1. **只读 CM**：daemon 只读 `chat_memory_messages` / `chat_memory_message_bots` / `personas`，
   从不写这三个源。
2. **单向产出**：daemon 是 `profile.db` 的**唯一写者**；注入插件只读（或经 HTTP 读）。
3. **不碰对话链路**：萃取与合并都在守护进程里，主对话路径上只有注入插件的一次查表。

---

## 4. 萃取链路

### 4.1 触发与节奏

后台 ticker（默认 300s）轮询，**不挂任何 Hook**：

```text
每 tick：
  1. 列出「有待处理消息」的物理会话（platform_name/message_type/session_id）
     —— 存在 seq > 本会话游标的、未被删除/撤回的消息，按各自最新消息时间排序
  2. 对每个会话：统计可用单位数（unit = 一条 user 消息，或一个完整问答轮，见 4.3）
  3. 可用单位 >= extract_threshold（默认 100）→ 读窗口 → 萃取 → 暂存 → 提交游标
  4. 受 extract_budget_per_tick / daily_extract_budget 限制，超出顺延到下一 tick
```

为什么不用 Hook：画像属旁路分析，不需要消息级实时性；不参与主链路、无 priority 之争、
失败只影响自己、LLM 调用完全可控。代价是最多延迟一个 tick。

为什么按**会话**而不是按 UMO 做单元：新表里同一物理消息只有一行，
一个群就是一个会话 —— 这正是「只跑一份」在数据层的体现。

> 参考实现：`cm_pipeline/ticker.py` 的 `AdaptiveTicker` 把「固定 300s」换成按积压调速
> （有达标会话就加速、空闲就降速、当日预算耗尽就长睡），策略表见 `cm_pipeline/README.md` §5。

### 4.2 窗口读取与游标

```sql
-- 游标：每个物理会话一条
CREATE TABLE cursor (
  session_key  TEXT PRIMARY KEY,   -- "platform_name|message_type|session_id"
  platform_name TEXT NOT NULL,
  message_type  TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  last_seq      INTEGER NOT NULL DEFAULT 0,   -- 已处理的最后 seq
  last_run_at   TEXT,
  last_status   TEXT NOT NULL DEFAULT '',     -- ok | below_threshold | llm_failed | cm_error
  last_error    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL
);
```

读取（严格 keyset，全部可用现有索引）：

```sql
SELECT m.msg_uid, m.seq, m.sender_id, m.sender_nickname, m.content, m.content_kind,
       m.platform_timestamp, m.recalled_at, m.deleted_at,
       b.platform_id, b.self_id, b.conversation_id, b.umo, b.role,
       b.turn_id, b.pair_id, b.persona_id, b.llm_status, b.target_id
FROM chat_memory_messages m
LEFT JOIN chat_memory_message_bots b ON b.msg_uid = m.msg_uid
WHERE m.platform_name = ? AND m.message_type = ? AND m.session_id = ?
  AND m.seq > ?                       -- 游标
  AND m.deleted_at IS NULL
  AND m.recalled_at IS NULL           -- ★撤回过滤：画像不吃撤回内容
ORDER BY m.seq ASC
LIMIT ?;
```

规则：

- **撤回与软删一律不读**（`recalled_at`/`deleted_at` 都过滤）——撤回的内容本就不该进画像。
  已在画像里的事实若其唯一证据后来被撤回，由 §7 的证据核查降权（不追溯删除，除非管理页手动）。
- **只有萃取成功才提交游标**；LLM 失败/超时/DB 异常都不推进，下个 tick 重试同一窗口。
- **幂等键** `window_id = sha256(session_key ⧿ start_seq ⧿ end_seq)[:32]`；
  重复读取同一窗口不会重复暂存。
- **新会话只建基线不重放历史**（`last_seq` = 当前最大 seq）；历史回填走 §10.4 的手动工具。
- **一次窗口不跨会话**：窗口是单会话内的连续 `seq` 区间，避免把不同群的话拼在一起。

### 4.3 「单位」的判定

跟随 CM 的 `ct_llm_status_filter` 语义（配置读取，不依赖 AstrBot 进程）：

- 过滤集合仅含 `llm_success` → 单位 = **完整问答轮**（用 `turn_id` 把同一轮的多行归一组）；
- 含其它状态（如 `llm_pending`/空）→ 单位 = **单条消息**。

`turn_id` 在参与行上、是每实例的。同一物理消息被多个实例参与时，取**参与行里第一个非空
且该实例 `role` 匹配**的 `turn_id`；若各实例 `turn_id` 不一致（理论上同一轮同一实例才一致），
以 `(self_id, turn_id)` 分组，不跨实例合并。

### 4.4 窗口渲染成提示词

按 CM 的身份格式渲染（**不含账号 id，只给昵称**，隐私口径与 CM/LM 对齐）：

```text
<cm_time>2026-09-12 21:30</cm_time>
<cm_speaker current="1"/><cm_nickname>小明</cm_nickname> 明天想去打球
<cm_time>2026-09-12 21:31</cm_time>
<cm_speaker bot="1"/><cm_nickname>某bot</cm_nickname> 好啊，几点？
```

角色判定**不是靠昵称猜**，而是靠数据：

| 行 | 判定 | 依据 |
| --- | --- | --- |
| 入站用户消息 | `role=user`，`sender_id` = 用户 | 参与行 `role` |
| 本实例 bot 发言 | `role=assistant` 且 `sender_id == b.self_id` | `sender_id` 就是 `self_id`（出站行） |
| **其它 BOT 的发言** | 对当前实例而言就是**群成员发言**（D6），但可标记 `<cm_bot/>` 供模型参考 | 参与行里对该消息有 `role=assistant` 且 `platform_id != 本实例` 的行 |
| 无参与行的消息 | 该会话内没有任何实例参与（极少见），按 `sender_id` 与昵称渲染，标 `unknown` | `LEFT JOIN` 为 NULL |

> 关于「其它 BOT 的发言」：物理表里 `sender_id` 就是对方的 `self_id`，而它在**当前实例眼里
> 就是一个普通账号**。我们**不维护 Bot 名单**（会过期），只在渲染时如实标出「这条是某个 bot
> 账号发的」，让模型自己判断可信度；代价是可能给某个 Bot 账号建了画像，
> 管理页可按主体标记 `ignored=1`。

**内容处理**：`content` 是渲染后正文（可能含 `<cm_image/>`、`<cm_mention target=…/>` 等占位符），
按 `content_kind` 决定是否入选：默认只取文本类（跟随 CM `include_content_kinds`），
占位符保留但**不作为事实来源**（图片/At 本身不构成属性陈述）。

### 4.5 窗口萃取（LLM 第一段）

- **输入**：渲染后的窗口对话 + 当前日期 + 受控类别表（见 §6.1）。
- **输出**：严格 JSON，候选事实数组。**只暂存，不改画像。**
- 提示词要点：
  1. 只提取**关于某个具体人的稳定属性/偏好/关系/习惯/身份/计划**，不提取事件摘要；
  2. 第三人称中性表述，**禁止任何 bot 口吻、称呼、语气词**，禁止「我/你」；
  3. 不把 bot 自己说的话当作关于用户的事实来源；
  4. 角色扮演、虚构设定、玩笑、假设句一律不提取（进 `reject` 清单）；
  5. 相对时间（今天/下周）转绝对日期，无法确定留空；
  6. 每条候选带 `speaker_nickname`（谁说的）、`subject_nickname`（关于谁）、
     `category`（受控表内）、`text`（≤60 字中性陈述）、`evidence_quote`（≤50 字原话片段）、
     `explicit`（是否本人原话直述）。
- **校验**（代码侧，逐条，不整批失败）：JSON 可解析、`category` 在表内（否则归 `other` 并记 WARNING）、
  `text` 非空且截断、`evidence_quote` 必须能在窗口正文里找到（防幻觉，找不到则丢弃该条）。
- **LLM 适配**：`openai` SDK 直连（provider 从 `cmd_config.json` 读）；
  `max_retries=1`（避免与自己的外层重试叠加）、单次 `timeout` 可配、外层退避重试 ≤2 次、
  记录 token 用量。

### 4.6 每日合并（LLM 第二段，唯一写画像的入口）

```text
触发：ticker 判断「配置时区当前时间 >= merge_time」且「merge_date 未成功执行」

1. 选主体：有未消费 obs_item 的 subject，按 importance 降序、条数降序
2. 分桶：单主体候选 > max_items_per_merge（默认 40）→ 多轮，每轮一个 run 记录
3. 每主体一次 LLM 调用：
     [中性主干现状]   -- 只给 text/category/layer/confidence，不给 persona、不给原文
     [新增候选事实]   -- 带 speaker 与 evidence_quote 片段
     [已有陈述名单]   -- 让模型主动避开重复（借 memory_sync 的思路）
     [受控类别表 + 硬性规则]
4. 输出 ops（§4.7）→ 校验 → 单事务落库 → 写 fact_version
5. 更新 consumed_at、写 merge_run、标记受影响主体的 persona_render 失效
```

失败处理：整主体本轮跳过并写 `merge_run.status=failed`，候选保持 `consumed_at IS NULL`
次日自动重试；连续失败 N 次告警。**绝不静默丢弃候选。**

### 4.7 合并的输出契约（只出操作，不重写）

```json
{
  "ops": [
    {"op":"add","category":"occupation","text":"在做后端开发","confidence":0.7,
     "evidence_indices":[0,3],"reason":"两条独立证据一致"},
    {"op":"confirm","fact_id":"...","evidence_indices":[1]},
    {"op":"update","fact_id":"...","text":"转到前端方向","reason":"最近说法改变"},
    {"op":"supersede","fact_id":"...","by_text":"现在住在杭州","reason":"本人明确说已搬家"},
    {"op":"merge","fact_ids":["...","..."],"text":"合并后的中性陈述"},
    {"op":"retire","fact_id":"...","reason":"长期无支持且与新增证据冲突"},
    {"op":"reject","evidence_indices":[4],"reason":"属于角色扮演内容，非真实属性"}
  ],
  "notes": "可选：给人工看的歧义说明"
}
```

代码强校验：`op` 白名单；`fact_id` 必须存在且属于该主体；`evidence_indices` 必须在本轮范围内
（越界只丢该 op）；`text` 截断 60 字、去 markdown、去自称；`category` 受控。
`add` 之后再跑一次代码侧相似度去重（不依赖模型自觉）。

---

## 5. 存储设计

`plugin_data/portrait/profile.db`，WAL + `busy_timeout=5000`；
daemon 单连接 + `asyncio.Lock` 写入；注入插件只读（WAL 下多进程只读安全）。

### 5.1 主体与别名

```sql
-- 画像主体：跨 BOT 唯一（platform_name + sender_id，不含 platform_id）
CREATE TABLE subject (
  subject_id    TEXT PRIMARY KEY,     -- "platform_name:sender_id"
  platform_name TEXT NOT NULL,
  sender_id     TEXT NOT NULL,
  primary_name  TEXT,                 -- 最近一次见到的昵称
  ignored       INTEGER NOT NULL DEFAULT 0,   -- 管理页标记「不给它建画像」
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 昵称/群名片历史：解决「同一人多名」「同名不同人」
CREATE TABLE alias (
  subject_id   TEXT NOT NULL,
  alias        TEXT NOT NULL,
  session_id   TEXT NOT NULL DEFAULT '',   -- 空 = 未知会话
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  seen_count   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (subject_id, alias, session_id)
);
CREATE INDEX ix_alias_lookup ON alias(alias, session_id);
```

> `subject_id` 不含 `platform_id` 是刻意的：同一个人在同一群被三个 BOT 服务时，
> 旧形态会分裂成三个主体，新形态**天然合并成一个**。

### 5.2 观察层（幂等暂存）

```sql
-- 已萃取的窗口（物理会话内的连续 seq 区间）
CREATE TABLE obs_window (
  window_id     TEXT PRIMARY KEY,      -- sha256(session_key ⧿ start_seq ⧿ end_seq)[:32]
  session_key   TEXT NOT NULL,
  platform_name TEXT NOT NULL,
  message_type  TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  start_seq     INTEGER NOT NULL,
  end_seq       INTEGER NOT NULL,
  unit_count    INTEGER NOT NULL DEFAULT 0,
  participant_bots TEXT NOT NULL DEFAULT '[]',   -- [{"platform_id","self_id","persona_id"}]
  status        TEXT NOT NULL,         -- success | empty | skip
  extracted_at  TEXT NOT NULL
);

-- 候选事实：每日合并的输入
CREATE TABLE obs_item (
  obs_id       TEXT PRIMARY KEY,   -- sha256(window_id ⧿ index ⧿ subject ⧿ text)
  window_id    TEXT NOT NULL,
  subject_id   TEXT,               -- 归因结果；NULL = 未绑定池
  observer_bots TEXT NOT NULL DEFAULT '[]',  -- 该窗口有哪些实例参与（证据属性）
  observer_persona TEXT NOT NULL DEFAULT '', -- 该窗口主要人设（仅证据属性）
  speaker_nickname TEXT NOT NULL DEFAULT '', -- 谁说的
  category     TEXT NOT NULL,
  raw_text     TEXT NOT NULL,      -- 中性第三人称候选陈述
  evidence_quote TEXT NOT NULL DEFAULT '',
  explicit     INTEGER NOT NULL DEFAULT 0,
  event_time   TEXT,
  importance   REAL NOT NULL DEFAULT 0.5,
  attr_confidence REAL NOT NULL DEFAULT 0.5,
  created_at   TEXT NOT NULL,
  consumed_at  TEXT                -- NULL = 待合并
);
CREATE INDEX ix_obs_pending ON obs_item(created_at) WHERE consumed_at IS NULL;
CREATE INDEX ix_obs_subject ON obs_item(subject_id, created_at);
```

**为什么记 `observer_bots` 而不是「一份/实例」**：同一物理消息被 3 个实例看到是
**同一条证据的 3 次独立确认**，而不是 3 条不同证据。合并时按「不同实例独立观察到」
给**证据加权**（例如三个实例都看到 → `evidence_strength=3`），
但**事实只有一条**。这是去重表带来的正确语义，也是旧形态做不到的。

### 5.3 事实层（核心）

```sql
CREATE TABLE fact (
  fact_id      TEXT PRIMARY KEY,
  subject_id   TEXT NOT NULL,
  category     TEXT NOT NULL,
  text         TEXT NOT NULL,      -- ★中性陈述：第三人称、无 bot 口吻
  layer        TEXT NOT NULL,      -- long | mid | short（代码推导，见 §6）
  confidence   REAL NOT NULL DEFAULT 0.5,
  status       TEXT NOT NULL DEFAULT 'active',   -- active|stale|superseded|retired|conflict
  explicit     INTEGER NOT NULL DEFAULT 0,
  conflict     INTEGER NOT NULL DEFAULT 0,
  support      INTEGER NOT NULL DEFAULT 1,       -- 独立证据次数（含跨实例确认）
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  last_confirmed TEXT NOT NULL,
  superseded_by TEXT,
  created_from_run TEXT,
  updated_at   TEXT NOT NULL,
  UNIQUE(subject_id, category, text)
);
CREATE INDEX ix_fact_inject ON fact(subject_id, status, layer, confidence DESC);
CREATE INDEX ix_fact_category ON fact(subject_id, category, status);

CREATE TABLE fact_evidence (
  fact_id     TEXT NOT NULL,
  obs_id      TEXT NOT NULL,
  window_id   TEXT NOT NULL,
  observer_bots TEXT NOT NULL DEFAULT '[]',
  observed_at TEXT NOT NULL,
  PRIMARY KEY (fact_id, obs_id)
);

-- 全量版本历史：审计、回滚、解释
CREATE TABLE fact_version (
  version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  fact_id     TEXT NOT NULL,
  op          TEXT NOT NULL,   -- add|confirm|update|supersede|retire|restore|admin_edit
  before_json TEXT, after_json TEXT,
  run_id      TEXT NOT NULL,
  actor       TEXT NOT NULL,   -- merge | admin | lifecycle | evidence_check
  created_at  TEXT NOT NULL
);
```

### 5.4 渲染层与运行记录

```sql
-- 每 persona 的风味渲染缓存（注入时零延迟）
CREATE TABLE persona_render (
  subject_id  TEXT NOT NULL,
  persona_id  TEXT NOT NULL,
  style_key   TEXT NOT NULL,   -- sha256(persona 文本 + 中性事实指纹)
  base_digest TEXT NOT NULL,
  text        TEXT NOT NULL,
  rendered_at TEXT NOT NULL,
  PRIMARY KEY (subject_id, persona_id)
);

-- 合并运行记录：幂等 + 可观测
CREATE TABLE merge_run (
  run_id     TEXT PRIMARY KEY,
  run_date   TEXT NOT NULL,     -- 配置时区日期
  subject_id TEXT NOT NULL,
  obs_count  INTEGER NOT NULL DEFAULT 0,
  op_count   INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL,     -- success | partial | failed | skipped
  llm_status TEXT NOT NULL DEFAULT '',
  error      TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_date, subject_id)  -- 同一天同主体只合并一次
);

-- 心跳与统计
CREATE TABLE runtime_stat (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL);
```

### 5.5 为什么 `persona_id` 不落在 `fact` 上

若把 `persona_id` 放进事实主键，同一件事会按人设存多份，还会出现
「人设 A 认为他在北京、人设 B 认为他在上海」这种无法裁决的分裂。因此：

- `fact`（中性、本库内唯一）＝ 这个人是什么样；
- `fact_evidence.observer_bots` / `observer_persona` ＝ 哪些实例、在什么人设下看到的；
- `persona_render` ＝ 每个 persona 该怎么说。

**人格隔离**与**跨实例隔离**是两件事：前者靠这张表结构解决，后者靠
`subject_id` 不含 `platform_id` 解决（同一人合一份）。

---

## 6. 分层模型（长/中/短期）

### 6.1 分层由谓词先验决定，不由 LLM 猜

`category` 是**受控表**（代码常量 + 配置可覆盖映射）：

| 层 | 默认置信度 | 类别 | 语义 |
| --- | --- | --- | --- |
| `long` 长期 | 0.75 | `identity_name` `birth_year` `age_band` `gender` `native_place` `residence` `education` `school` `major` `family_role` `native_language` `long_term_health` | 极长时间内不变 |
| `mid` 中期 | 0.6 | `occupation` `employer` `work_schedule` `commute` `relationship_status` `close_relations` `city_moved` `device` `chronic_hobby` `diet_habit` `pet` | 可预见数月内不变 |
| `short` 短期 | 0.45 | `current_project` `recent_event` `recent_trip` `mood_baseline` `recent_interest` `upcoming_plan` `current_game` `study_exam` | 可预见数周内不变 |

规则：

1. LLM **不输出 layer**，只输出 `category` + 陈述 + `explicit`；层由映射得出。
   改映射只需重算，不需要重新问 LLM。
2. **本人显式声明 > LLM 推断**：`explicit=1` 的事实置信度上限 0.95，且不参与降级。
3. **升层靠存活**（§6.2），不靠再次断言；跨实例确认计入 `support`。
4. `short` 过期不清空，转 `stale`（降权但仍可注入，排在最后）。

### 6.2 证据时效性：升层 / 降权 / 退役（纯代码，可单测）

```text
# 升层
layer==short 且 (now-first_seen) >= 30d  且 support>=2 且 近 30d 无矛盾 → mid
layer==mid   且 (now-first_seen) >= 180d 且 support>=3 且 近 90d 无矛盾 → long

# 降权
now - last_seen > layer_ttl(layer)  →  confidence *= decay_factor，status=stale
layer_ttl: short=21d, mid=180d, long=1080d

# 退役
stale 且 confidence < 0.25 且 now-last_seen > retire_days(layer) 且 explicit==0
    → status=retired（保留历史，不注入）
retire_days: short=60d, mid=365d, long=1095d

# 矛盾
新证据与旧 fact 互斥 → 新 add + 旧 supersede；
若旧 fact 由 explicit 支持 → 不覆盖，两条都置 conflict=1，同时注入并在管理页高亮，交人工裁决

# 证据失效（★去重表特有能力）
合并时若某 fact 的全部证据窗口的底层消息都已 recalled/deleted
    → status=stale（不直接删），并在管理页标注「证据已撤回」
```

全部阈值是配置项；每次状态迁移写 `fact_version`（`actor=lifecycle`）。
**这一层完全不需要 LLM**，所以确定、可回归测试。

### 6.3 与 LM 的边界

| 内容 | 归属 |
| --- | --- |
| 最近 N 轮原始对话 | CM 上下文接管 |
| 具体事件、对话片段、长尾可检索内容 | LM（向量召回） |
| 「这周在赶什么项目」这类数周尺度的结论 | 本插件 `short` 层 |
| 数月/长期稳定属性 | 本插件 `mid`/`long` 层 |

画像**不主动查 LM**，也**不存**具体事件原文。

---

## 7. 归因设计（纯代码）

归因是这类系统最容易出错的地方（参考插件 `memory_sync` 就是因为字符串型 content
整块返回，把同群所有人塌成一节）。新表让这件事简单了一档：

```text
1. 归因的依据是 role + sender_id，不是昵称：
   - 候选的 subject_nickname 在「本窗口的 subject→nickname 映射」里唯一命中 → 直接归因
   - 命中多个（同群重名）→ 用 evidence_quote 在该昵称的发言里做子串匹配消歧；
     仍歧义 → subject_id=NULL（未绑定池）
   - 未命中但 alias 表全局唯一命中（同会话优先）→ 归因，attr_confidence=0.6
   - 全局也无 → NULL（未绑定池，管理页可人工指派）
2. subject 映射的构建：窗口内所有消息的 (sender_id, sender_nickname)，
   剔除出站行（sender_id == 任一参与行的 self_id）；昵称冲突时保留最近的
3. 别名 upsert：alias(subject_id, nickname, session_id)
4. attr_confidence 影响合并权重（<0.5 的候选在提示词里标注「归因存疑」）
```

未绑定池不是垃圾桶：管理页可指派、可批量丢弃，且**不进画像、不影响注入**。

**证据强度**（去重表新增的维度）：同一候选若在窗口里被多个实例确认
（`obs_window.participant_bots` 里同一 `msg_uid` 有多个参与行），
`support` 按「独立实例数」累加，但**事实仍只有一条**。

---

## 8. 注入设计

### 8.1 选区与组装

```text
1. 注入插件的输入：当前 UMO → (platform_id, platform_name, message_type, session_id)
   + 当前对话对象 sender_id + 当前 persona_id
2. 主体 = platform_name:sender_id（与 daemon 一致，不含 platform_id）
3. 取 persona_render[subject, persona]；没有则取中性事实兜底
4. 选区：long ≤ X / mid ≤ Y / short ≤ Z 条，按 confidence、last_seen 排序，
   总字符 ≤ 预算（默认约 400 字）；conflict 条目永远带上
5. 组装：
   - 声明「以下是背景认知，可能过时或不完整」
   - 声明「其中第三人称陈述不得转述为『你/您』说过」
     （复用 chat_memory docs/MEMORY_ATTRIBUTION.md 的措辞约定）
   - 每条只给类别 + 中性陈述；不给证据原文、不给对话片段
6. 挂载点：extra_user_content_parts（本轮动态附加），不复用 contexts 整体替换（会与 CM 打架）
7. 无画像 → 完全不注入（不产生「暂无记录」噪音）
```

### 8.2 按 persona 预渲染（daemon 侧完成）

因为 daemon 读得到 `personas` 表，它在写画像时就为每个 persona 预渲染好一份：

- 输入 = 中性事实集 + 该 persona 的 `system_prompt`；输出 = 该人格口吻的背景认知段落；
- 约束：只改表述/顺序/称呼/强调点，**不得新增、删除或改变事实**，不得输出解说性前缀；
- 缓存：`style_key = sha256(persona 文本 + base_digest)`，任一变则重渲染；
- 失败 → 注入中性文本兜底，**绝不阻塞对话**；
- 触发时机：合并后（受影响主体）+ 配置变更后（`rebuild-render`）。

**渲染是惰性的**：不预先给「所有主体 × 所有 persona（实测 51 个）」渲染。
只在某主体出现在某个 `persona_render` 需求时（合并后或注入缺缓存时）才渲染，
否则 51 × 主体数 会把 LLM 调用放大一到两个数量级。

### 8.3 极薄注入插件

```python
# 伪代码，约 150 行
@filter.on_llm_request()
async def inject(self, event, req):
    render = await self._fetch(subject_id(event), persona_id(event))  # HTTP 或只读 profile.db
    if render:
        req.extra_user_content_parts.append(TextPart(render.text))
```

它**不解析 persona、不调 LLM、不做分层筛选、不感知别的 BOT**。好处：

- 代码量与改动频率最低（画像逻辑迭代时注入插件不用动）；
- 某个 BOT 没装/装旧了，不影响 daemon 继续积累；
- 新增一个 BOT 时，daemon 侧只是在参与行里多一个 `platform_id`，天然被覆盖。

访问方式二选一：**① HTTP（推荐）**——插件无状态、SQLite 只有 daemon 一个写者；
**② 只读 `profile.db`**——省掉 `--serve`，WAL 下多进程只读安全，但要自己处理锁等待与
schema 版本兼容。

---

## 9. Daemon：单进程与生命周期

### 9.1 为什么天然只跑一份

因为分析单元是**物理会话**，与「有多少个 BOT」无关：

| 旧形态（插件挂 Hook，按 UMO） | 新形态（daemon 读物理会话） |
| --- | --- |
| 3 个 BOT → 3 个 UMO → 同一群读 3 遍、萃取 3 次 | 1 个会话 → **读 1 遍、萃取 1 次** |
| LLM 调用 ∝ BOT 数 | **LLM 调用与 BOT 数无关** |
| 多份观测需要归并、去重、投票 | 同一条消息的多个实例参与行 = 同一条证据的多次确认（`support`） |

再加一层进程级保证（§9.3 的 OS 文件锁），**结构 + 锁两道**。

### 9.2 运行形态

```text
python daemon/run.py --daemon        常驻（前台 Ctrl+C 停；生产走计划任务）
python daemon/run.py --once          单轮跑完退出（调试、手动补）
python daemon/run.py --status        心跳：上次 tick/萃取/合并、今日调用与 token
python daemon/run.py --merge-now     手动触发一次合并
python daemon/run.py --render-now    重建某个主体的渲染缓存
python daemon/run.py --backfill ...  历史回填（§10.4）
python daemon/run.py --serve         额外监听 127.0.0.1:<port>（GET /status、GET /render、
                                     POST /merge-now），供注入插件与管理页使用
```

- **单实例保证**：启动时对 `profile_dir/daemon.lock` 取排他锁
  （Windows `msvcrt.locking` / POSIX `fcntl.flock`）；拿不到就记一行日志并退出 0。
  锁随进程死亡自动释放，崩溃后不会留死锁。
- **可观测**：心跳写 `runtime_stat`；日志走自己的文件。
- **幂等与断点续跑**：`cursor` + `window_id` + `merge_run` 唯一键 → 随时被杀、
  重启不重复萃取、不重复合并同一天。
- **读取安全**：CM `mode=ro` 只读（不写 CM）；`data_v4.db` 只读且只查 `personas`；
  不需要 AstrBot 在运行，也不需要在 AstrBot 里装任何东西。
- **路径发现**（顺序固定）：命令行参数 → `ASTRBOT_ROOT` 环境变量 → 与 `profile_dir` 同级推断
  → 报错退出；启动时校验 `chat_memory.db` / `data_v4.db` / `cmd_config.json` 都能读。
  **不把 `astrbot_root` 做成 WebUI 配置项**（机器级路径，多实例各填一遍必然填错）。

### 9.3 WebUI 配置项即开关（D9）

```text
daemon.enabled (bool, 默认 false)
  false → 插件什么都不做
  true  → 插件确保「本机有且只有一个萃取器在跑」：读锁/心跳 → 已在跑就直接返回 →
          没跑就 spawn_detached（脱离父进程，插件 reload / AstrBot 重启都不影响它）
```

逐场景确认「多 BOT 最终只有一个守护进程」：

| 场景 | 为什么只有一个 | 强度 |
| --- | --- | --- |
| 一个 AstrBot 进程、多个 BOT（**你的部署**） | 插件是全局单实例、`_conf_schema.json` 全局一份 → 开关唯一 | 结构上唯一 |
| 插件被 reload | spawn 是 detach 的；新的这次命中「已在运行」 | 幂等 |
| 同机多个 AstrBot 实例 | 都想 spawn，但**守护进程自己抢锁** | OS 锁兜底 |
| daemon 崩溃 | 锁释放，下次 tick / 计划任务重新拉起 | 自愈 |

两个边界必须说清：

1. **锁文件与 `profile.db` 必须同目录**（`plugin_data/portrait/`）。
   若多个 AstrBot 实例的 data 目录**不是同一个**，就会各跑一个、各写各库 ——
   这等于两份独立画像，不是同一个。要合并必须让两边指向同一个机器级 `profile_dir`，
   那就要面对多进程写同一个库，不建议。
2. **「永远只有一个」仅限单机**。跨机需要租约/中心协调，超出本期范围。

状态必须回显（否则「打开开关却不知道有没有生效」）：插件页面/命令显示 `enabled` +
`运行状态`（`disabled`/`already_running`/`spawned`/`lock_held_by_other`/`failed: 原因`）+
心跳（上次 tick/萃取/合并、今日调用与 token）。拿不到锁时明确写「已由其它实例运行」。

---

## 10. 交付物与实施路线

### 10.1 两个交付物

```text
portrait/
├── daemon/          ① 萃取器（常驻单进程，核心，必需）
│   ├── run.py           CLI 入口与 ticker
│   ├── config.py        配置加载与路径发现（含 BOM 处理）
│   ├── cm_reader.py     只读 CM：会话枚举、窗口读取（seq keyset）、撤回过滤
│   ├── astrbot_reader.py 只读 data_v4.db personas + cmd_config.json provider
│   ├── render.py        窗口渲染成身份标签格式（§4.4）
│   ├── extract.py       萃取提示词 + JSON 校验 + 证据引用核对
│   ├── attribute.py     归因 + 别名表（§7）
│   ├── merge.py         每日合并 ops 契约 + 事务落库
│   ├── lifecycle.py     分层状态机（纯函数）
│   ├── flavor.py        按 persona 预渲染
│   ├── store.py         SQLite 仓储（§5 全部表）
│   └── serve.py         可选本地 HTTP
└── plugin/          ② 注入插件（可选，标准 AstrBot 插件）
    ├── main.py          约 150 行：查表/HTTP + 粘贴 + 状态回显
    └── _conf_schema.json
```

**① 先做，②后做**：先只跑 daemon（画像照样积累、落盘可检阅），
确认质量与成本后再装 ② 让它「说出来」。

> **已落地**：`cm_pipeline/` 是 M1～M3（读窗口 / 渲染 / 萃取 / 落库）加自适应 ticker 的
> 可搬移参考实现（纯标准库、不 import AstrBot、`tests/run_tests.py` 55 项通过、
> `example.py` 可直接跑）；上表的 `cm_reader.py` / `render.py` / `extract.py` / `store.py`
> 与 `run.py` 的 ticker 直接用它的同名模块，只在其上补归因/合并/分层（M4～M7）。

### 10.2 配置项（daemon 侧）

```text
daemon.enabled               false      总开关（WebUI）
daemon.profile_dir           ""         留空 = plugin_data/portrait
daemon.tick_seconds          300
daemon.extract_threshold     100        每多少单位萃取一次
daemon.extract_unit          follow_cm  follow_cm | message | round
daemon.extract_budget_per_tick  2       每 tick 最多几次萃取
daemon.daily_extract_budget  200        每日萃取调用上限（超出顺延）
daemon.merge_time            "04:00"    每日合并时刻（配置时区）
daemon.max_items_per_merge   40
daemon.daily_merge_budget    100
daemon.llm_provider_id       ""         留空 = cmd_config.json 默认
daemon.llm_timeout_seconds   120
daemon.inject_budget_chars   400
daemon.include_content_kinds ["text"]
daemon.protected_user_ids    []
daemon.sensitive_categories  false      是否采集敏感类别（默认关）
daemon.log_with_bot_id       true       顶层日志开关
```

### 10.3 落地前提：切读新表之前

本设计从「CM 的物理去重表可用」那一刻生效。当前部署端还是旧形态
（`user_version=7`、只有 4 张旧表、`shared_store.py` 未提交）。因此实施顺序：

1. **等 CM 交付**：`shared_store.py` 提交 → 部署端出现 `chat_memory_messages` /
   `chat_memory_message_bots` 且开始影子写入；
2. **一致性核对**（一次性脚本）：拿旧表按 `fingerprint` 规则重算，与影子表对账
   （行数、每会话条数、`seq` 单调性、昵称/正文抽样），**通过才允许切读**；
3. **切读**：CM 把读取路径切到新表（这是 CM 侧的工作，不是我们的）；
4. **daemon 上线**：此时 daemon 直接读新表，无兼容负担。

在 1–3 完成前，daemon 可以做的最小验证是：**只读旧表跑通「会话枚举 + 窗口渲染」**
（不写库、不调 LLM），用来提前验证渲染与归因逻辑，不追求产出画像。

### 10.4 历史回填

新表上线前已存在的历史消息，只有旧表有。回填方式：

- 一次性脚本把旧表按 `fingerprint` 规则**去重后**写入我们自己的 `obs_window`/`obs_item`
  （不写 CM），再走正常合并流程；
- 单群量级大（旧库 339,461 行 → 约 136,699 条物理消息），必须**分桶**（按会话 × 时间段）、
  可中断续跑、带每日预算；
- **默认不回填**；回填与实时萃取共用同一个 `daily_extract_budget`，
  避免把线上配额吃光。

### 10.5 实施阶段

| 阶段 | 交付物 | 内容 | 可验证物 |
| --- | --- | --- | --- |
| M0 | daemon | 配置加载、路径发现与校验、锁文件、SQLite 仓储与迁移、日志 | 仓储单测（幂等、版本、schema 迁移）+ 锁文件单测 |
| M1 | daemon | CM 只读：会话枚举、`seq` keyset 窗口、撤回/软删过滤、参与行 join | 窗口边界单测（seq 连续性、跨会话不越界、撤回过滤） |
| M2 | daemon | 窗口渲染（身份标签、bot/用户判定）+ 单位判定（follow_cm） | 渲染快照测试 + 单位计数单测 |
| M3 | daemon | 萃取：提示词、JSON 校验、证据引用核对、LLM 适配 | 校验单测（含幻觉引用应被丢弃）+ 假 LLM 端到端 |
| M4 | daemon | 归因 + 别名表 + 未绑定池 | 归因单测（重名、未绑定、出站行、跨实例同名） |
| M5 | daemon | 每日合并：ops 契约、事务落库、`merge_run`、预算 | ops 校验单测、合并幂等、失败重试单测 |
| M6 | daemon | 分层生命周期：升/降/退役/冲突/证据撤回降权 | 状态机单测（重点，覆盖所有迁移与边界） |
| M7 | daemon | 按 persona 预渲染（惰性）+ 指纹失效 | 指纹失效单测 + 惰性触发单测 |
| M8 | daemon | 控制面：CLI → `--serve` → 心跳与状态回显 | 部署端手工验收 |
| M9 | plugin | 极薄注入插件：查表/HTTP + 粘贴 + 状态页 | 选区/预算单测、注入格式快照 |
| M10 | daemon | 历史回填工具（手动触发） | 分桶、中断续跑、预算单测 |

依赖：M0→M1→M2→M3→M4→M5 是主链；M6 可与 M4/M5 并行；M7 依赖 M5；
M8 随 M0 起就有最小版；M9 最后（可选）；M10 视是否需要历史。

---

## 11. 风险与对策

| # | 风险 | 对策 |
| --- | --- | --- |
| 1 | 依赖的物理去重表尚未在部署端存在 | §10.3 的落地前提与三段顺序；在此之前只做只读验证，不产出画像 |
| 2 | 影子表会被 CM 按 schema 版本 `DROP` 重建、不迁移数据 | 只把它当**运行期数据源**，不做唯一备份；`profile.db` 里的证据链自带 `window_id`/`obs_id`，CM 重建不影响已合并的事实 |
| 3 | 自己写游标出错 → 重复萃取或漏萃取 | `seq` keyset + `window_id` 幂等 + 边界单测；只有成功才提交游标 |
| 4 | 首次回填成本高（13 万条物理消息） | 默认不回填；M10 分桶 + 可中断 + 与实时共用预算 |
| 5 | LLM 归因错误（A 说的记到 B 头上） | 归因走 `role` + `sender_id`，不靠昵称字符串；歧义进未绑定池而非硬写；`evidence_quote` 必须能对上原文 |
| 6 | 角色扮演/虚构被当真实属性 | 提示词显式排除 + 合并 `reject` + 管理页可删 |
| 7 | 提示词注入污染画像 | 证据包裹、受控 category、text 硬截断、op 白名单 |
| 8 | 敏感属性（健康/情感/财务）伦理与泄露 | 默认不采集敏感类别；`protected_user_ids` 黑名单；删除入口；INFO 不落正文 |
| 9 | 注入内容被模型当成「用户说过」 | 复用 `MEMORY_ATTRIBUTION.md` 的归因声明，块内标注第三方陈述边界 |
| 10 | 合并失败导致候选堆积 | 候选不消费、次日重试；连续失败告警；`merge_run` 可观测 |
| 11 | 与 LM 抢 LLM 配额 | 合并时刻错开（可配置）；provider 可分开指定；`daily_*_budget` 限流 |
| 12 | **萃取器是新的单点** | 计划任务失败重启 + 心跳表 + 启动锁防重复起 |
| 13 | 自己打 LLM API，绕过 AstrBot 的重试/回退/多配置路由 | 显式超时 + 单次重试 + 外层退避 + token 统计；多配置路由需自行从 `preferences` 表复刻（否则只用默认配置），日志记录实际使用的 provider |
| 14 | `plugin_data/` 路径写错导致读不到数据 | 固定发现顺序 + 启动时三文件校验，缺一个就报错退出 |
| 15 | 多实例各跑一个 daemon（若 data 目录不同） | 锁文件与 `profile.db` 同目录；同目录天然单例；不同目录视为两份独立画像（§9.3） |
| 16 | 混淆「内容更全的那份」与观察侧分段 | 明确禁止按内容覆写（D11）；渲染只用物理行给的 `content` |

---

## 12. 管理面

- **WebUI 插件页面**（注入插件附带，或 `--serve` 的本地页面）：
  主体列表与搜索、画像详情（分层活跃事实）、**证据链**（这条事实来自哪些窗口、
  哪些实例参与、原话片段、是否已被撤回）、冲突裁决、手工编辑（写 `admin_edit` 版本）、
  删除/遗忘（级联删 obs/evidence/render，保留审计计数）、未绑定池指派、
  运行状态（游标水位、最近 `merge_run`、失败原因、今日 token）。
- **指令**：`/profile`（本人查看自己被记了什么，可要求删除）、`/profile admin <user>`、
  `/profile merge now`、`/profile extract now`、`/profile rebuild-render`。
- **隐私**：`protected_user_ids` 黑名单、敏感类别默认关、删除入口、
  INFO 日志不承载画像正文（画像陈述属正文类，只进 DEBUG）。

---

## 13. 与 v4（旧表形态）的差异对照

| 维度 | v4（按 `chat_memory_records`） | v5（按 `chat_memory_messages`） |
| --- | --- | --- |
| 分析单元 | UMO（含 `platform_id`） | **物理会话** `(platform_name, message_type, session_id)` |
| 主体 key | `platform_id:user_id` | **`platform_name:sender_id`** |
| 游标 | `(created_at, record_id)` | **`(session, seq)`** |
| 重复读取 | ×2.48（物理复存，无法去重） | **1.0** |
| LLM 调用量 | ∝ BOT 实例数 | **与 BOT 数无关** |
| 多实例观测 | 三份独立萃取结果，需要在主体层归并 | 同一条消息的**多次独立确认**，直接计入 `support` |
| 读取层 | 双实现适配器（Legacy/Shared）+ 切读开关 | **单一实现**；旧表只用于一次性历史回填 |
| persona 归属 | 窗口属性（行上有 `persona_id`） | 从参与行 join（物理表刻意不含） |
| 撤回处理 | `query_history` 不过滤，需自行判断 | 物理行上有 `recalled_at`，**直接过滤** |
| 段落不一致 | 未知风险 | 已知（9,602 组）→ 明确禁止按内容覆写（D11） |

---

## 14. 与参考插件的差异

| 维度 | 5 个参考插件 | 本设计 |
| --- | --- | --- |
| 触发 | 命令 / 主 LLM 顺带 / 查询时 | ticker 周期 + 每日定时合并 |
| 数据源 | 自有 history / 核心库 / 主 LLM 输出 | CM 物理去重表 + 自持 `seq` 游标 |
| LLM 输出 | 自由文本或弱校验 JSON | 两段契约：候选 JSON + ops JSON，逐条校验 |
| 分层 | 无 | 谓词先验 + 证据时效性，纯代码状态机 |
| 主体键 | `user_id` 单键 | `platform_name:sender_id` + 别名历史 |
| persona | 无感知 | 证据带 observer、渲染带 persona，主干中性 |
| 历史/回滚 | 覆盖式，无版本 | `fact_version` 全量审计 + 冲突裁决 |
| 并发 | 整文件覆写 | SQLite 事务 + 幂等唯一键 + 单进程 |
| 注入 | 全量拼 system_prompt | 预算化选区 + 归因声明 + 渲染缓存 |
| 多 BOT | 无感知（必然串数据或重复工作） | 物理会话为单元 → **天然只跑一份** |
