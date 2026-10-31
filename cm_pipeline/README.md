# cm_pipeline —— CM 读取 / 萃取 / 结果保存的可搬移参考实现

从 `chat_memory`（CM）的消息表读窗口 → 用 LLM 萃取 → 把结果与游标一起落库。
**不依赖 AstrBot，不依赖任何第三方包**（只用标准库），可以整包复制到任意插件里。

> 出处：`portrait/docs/DESIGN.md` 的 M1～M3 抽成了通用件；
> CM 表结构以 `chat_memory/shared_store.py` 与 `chat_memory/storage.py` 为准。

## 1. 它做什么 / 不做什么

| 能力 | 说明 |
| --- | --- |
| 读 CM 消息表 | 两种方言：影子表（`chat_memory_messages` + `chat_memory_message_bots`）与旧表（`chat_memory_records`）；`dialect="auto"` 自动探测。**只读**（`mode=ro`），撤回/软删默认过滤 |
| 窗口与游标 | 影子表用 `seq`（会话内递增、跨实例一致）；旧表用 `(created_at, id)` 严格复合。窗口不跨会话，`window_id` 稳定可幂等 |
| 渲染 | 按 CM 身份格式渲染成提示词文本（`<cm_time>/<cm_speaker>/<cm_nickname>`，**不含账号 id**）；单位判定跟随 CM `llm_status` 语义（轮/条） |
| 萃取 | 提示词模板 + OpenAI 兼容客户端（标准库）+ 严格 JSON 解析 + **逐条**校验（证据引用必须在窗口正文里找到） |
| 结果保存 | 宿主自己的 SQLite（WAL）：`cursor` / `window` / `item` 三表；**游标与结果同事务**，失败不推进 |
| 驱动节奏 | 自适应 ticker（积压多就快、空闲就慢、预算耗尽就等久），也可退化成固定节拍 |

不做的：不写 CM、不 import AstrBot、不做跨会话聚合、不做画像/记忆等业务语义
（那些属于使用它的插件）。

## 2. 快速开始

```python
from cm_pipeline import (
    ExtractionSpec, HttpOpenAiCompatClient, Pipeline, PipelineConfig,
    SqliteSink, WindowExtractor, open_cm_source,
)

config = PipelineConfig(
    cm_db="/path/to/plugin_data/chat_memory/chat_memory.db",
    own_db="/path/to/plugin_data/<your_plugin>/extract.db",
    extract_threshold=100,          # 每 100 个单位萃取一次
    llm_base_url="https://api.example.com/v1",
    llm_model="your-model",
)
source = open_cm_source(config.cm_db, dialect=config.dialect)
sink = SqliteSink(config.own_db)
extractor = WindowExtractor(
    spec=ExtractionSpec(
        system_prompt="只输出 JSON。",
        user_template="对话：\n{conversation}\n当前日期：{current_date}",
    ),
    client=HttpOpenAiCompatClient(
        base_url=config.llm_base_url,
        api_key="<key>",
        model=config.llm_model,
    ),
)
pipeline = Pipeline(source, extractor, sink, config)

print(pipeline.scan_once().as_dict())   # 一轮：基线 → 阈值 → 萃取 → 提交
source.close(); sink.close()
```

常驻运行时接上自适应 ticker（自己决定快慢，见 §5）：

```python
from cm_pipeline import AdaptiveTicker

ticker = AdaptiveTicker(pipeline, config.ticker_config())
ticker.run(max_ticks=3)          # 常驻用 ticker.run()；固定节拍用 TickerConfig.fixed(300)
```

命令行（无需 AstrBot）：

```bash
python -m cm_pipeline.cli init --output config.json          # 生成配置模板
python -m cm_pipeline.cli --config config.json status        # 看游标/状态/待消费条目
python -m cm_pipeline.cli --config config.json once          # 跑一轮
python -m cm_pipeline.cli --config config.json daemon        # 常驻（自适应节拍）
python -m cm_pipeline.cli --config config.json daemon --fixed-tick 300   # 固定 300s
python -m cm_pipeline.cli --config config.json backfill --from-seq 0 --max-windows 5
```

可直接运行的示例：`python example.py`（内置假 CM 库与假 LLM，不需要网络）。

## 3. 三段接口（只替换你需要的那段）

```text
CmSource ──► WindowExtractor ──► ResultSink
   │               │                  │
   │               │                  └─ load_cursor / commit / record_failure
   │               └─ LlmClient（可换成 AstrBot Provider 的包装）
   └─ list_sessions / latest_cursor / read_window（只读）
```

| 协议 | 文件 | 说明 |
| --- | --- | --- |
| `CmSource` | `source.py` | 只读 CM；`ShadowCmSource`（影子表）/ `LegacyCmSource`（旧表）/ `open_cm_source`（自动探测） |
| `LlmClient` | `llm.py` | `complete(system, user, timeout) -> LlmReply`；`HttpOpenAiCompatClient`（标准库）/ `FakeLlmClient`（测试） |
| `ResultSink` | `sink.py` | `SqliteSink`：幂等提交、事务性游标、失败记录 |

宿主自定义时只需实现同名方法；`Pipeline` 只依赖这些方法。

## 4. 不可破坏的约定

1. **只读 CM**：`mode=ro`，任何情况下不写上游。
2. **撤回/软删不读**：`recalled_at` / `deleted_at` 非空即过滤。
3. **不按内容覆写、不跨会话拼窗口**：窗口永远是单会话内连续区间。
4. **至少一次**：萃取成功（含空结果与 `status=skip`）才提交游标；失败保持原游标，
   下次重读同一窗口，幂等键防重复落库。
5. **新会话只建基线**：默认跳过历史；回填必须显式触发（`backfill`）。
6. **逐条校验**：单条不合规只丢该条并记入 `dropped`，不整批失败；证据引用找不到即丢弃。
7. **重试不叠加**：`LlmClient` 单次调用，重试由 `WindowExtractor(attempts=...)` 控制
   （避免 Provider 内部 5 次 × 读超时与外层重试相乘）。

## 5. 动态 ticker（按积压调速）

`ticker.py::AdaptiveTicker` 每轮 `scan_once()` 后取一次 `Pipeline.backlog_snapshot()`
（全部是廉价 `COUNT`，不读正文），据此决定下次扫描前等多久：

| 快照 | 模式 | 间隔 |
| --- | --- | --- |
| `budget_remaining <= 0`（当日预算耗尽） | `budget_exhausted` | `tick_budget_exhausted_seconds` |
| 有会话达到可萃取阈值，或本轮用满 `budget_per_tick` | `fast` | `max(下限, 上次 × speedup)` |
| 有积压但都没到阈值 | `normal` | 维持当前间隔（夹在上下限内） |
| 完全没有积压 | `idle` | `min(上限, 上次 × slowdown)` |

- 最后叠加 `±tick_jitter_ratio` 抖动（默认 ±10%），避免多个实例同时打 LLM；
  固定节拍用 `TickerConfig.fixed(300)`（上下限相等、抖动为 0）。
- 「本轮用满 `budget_per_tick`」说明还有活儿没干完，继续加速；只有当日预算耗尽才长睡。
- 可观测：`ticker.state`（`interval` / `mode` / `ticks` / `last_snapshot` / `history`）；
  模式切换记一条 INFO，单轮异常只 WARNING 且不终止循环。
- `run(max_ticks=N)` 便于测试和一次性驱动；`run_forever(tick_seconds=...)` 是固定节拍兼容入口，
  `cli.py daemon --fixed-tick 300` 走的就是它。

## 6. 数据表（宿主自己的库）

```sql
cursor(session_key PK, platform_name, message_type, session_id,
       cursor_token, last_status, last_error, updated_at)
window(window_id PK, session_key, start_token, end_token, unit_count,
       message_count, status, model, prompt_digest, usage_json, dropped_json, created_at)
item(item_id PK, window_id, session_key, item_index, subject_key, category,
     text, evidence_quote, importance, payload_json, consumed_at, created_at)
runtime_stat(k PK, v, updated_at)   -- 每日预算计数等
```

- `window_id = sha256(session_key ⧿ start_token ⧿ end_token)[:32]`
- `item_id = sha256(window_id ⧿ index ⧿ subject_key ⧿ text)[:32]`
- `cursor_token`：影子表 `s:<seq>`；旧表 `t:<created_at>#<id>`

## 7. 配置字段

见 `config.py::PipelineConfig`；常用项：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `cm_db` / `own_db` | 空 | CM 库（只读）与自己的结果库 |
| `dialect` | `auto` | `auto` / `shadow` / `legacy` |
| `perspective_platform_id` / `perspective_self_id` | 空 | 观察视角；留空取参与行首条。`self_id` 用于区分「我方 Bot 发言」 |
| `include_kinds` | `("text",)` | 内容类型白名单（与 CM `include_content_kinds` 对齐） |
| `assume_kinds_when_missing` | `("text",)` | 老库缺 `content_kind` 列/值为空时的兜底类型（避免白名单把全部消息静默丢掉） |
| `llm_status_filter` | `("llm_success",)` | 决定单位是「完整轮」还是「消息条数」 |
| `unit` | `follow_cm` | `follow_cm` / `message` / `round` |
| `extract_threshold` | `100` | 每多少个单位萃取一次 |
| `window_limit` | `500` | 单窗口最多读多少条消息 |
| `budget_per_tick` / `daily_budget` | `2` / `200` | 每轮/每日 LLM 调用上限 |
| `tick_seconds` / `tick_min_seconds` / `tick_max_seconds` | `300` / `30` / `300` | ticker 初值与上下限 |
| `tick_speedup_factor` / `tick_slowdown_factor` | `0.5` / `1.6` | 忙时乘 0.5、闲时乘 1.6 |
| `tick_jitter_ratio` / `tick_budget_exhausted_seconds` | `0.1` / `900` | 抖动比例；当日预算耗尽后的间隔 |
| `llm_attempts` / `llm_timeout_seconds` / `llm_backoff_seconds` | `3` / `120` / `3` | 外层重试策略 |
| `protected_user_ids` | 空 | 回填时跳过这些会话（隐私黑名单） |

## 8. 怎么搬走

```bash
cp -r portrait/cm_pipeline <你的插件>/cm_pipeline
```

- 核心文件：`contracts.py` / `source.py` / `render.py` / `extract.py` / `sink.py` /
  `pipeline.py` / `ticker.py` / `config.py` / `cli.py` / `llm.py`。
- `optional/` 只是「定位 AstrBot 数据目录 + 解析 `cmd_config.json`」的适配层，
  不需要就删掉；核心不会 import 它。
- 不做 AstrBot 插件包装：宿主自己决定用 Hook、定时任务还是独立进程驱动
  （`Pipeline.scan_once()` 是同步的，异步宿主可用 `asyncio.to_thread` 包裹）。

## 9. 测试

```bash
python tests/run_tests.py     # 55 passed，纯标准库 + pytest，不导入 AstrBot
```

覆盖：方言探测、keyset 边界（同秒 + id）、撤回/软删过滤、内容类型过滤、参与行与视角、
身份渲染与单位计数、JSON 契约与证据引用核对、重试与失败语义、幂等提交与事务游标、
预算、配置转换、可选适配层、自适应 ticker（增速/减速/封顶/预算耗尽/抖动/循环次数）。

## 10. 兼容性策略（重要）

CM 的库形态会演进，本包对「表/列缺失」采取**能力探测 + 降级**而不是硬失败：

| 情况 | 行为 |
| --- | --- |
| 影子表存在 | 走 `shadow` 方言（`seq` 游标、参与行 join） |
| 只有旧表 | 走 `legacy` 方言（`(created_at, id)` 游标） |
| 影子表缺可选列（如 `target_id`） | 缺列读作空值，不抛 `IndexError` |
| 旧表缺 `recalled_at` / `deleted_at` | 跳过对应过滤（并在语义上说明：该库无法表达撤回） |
| 旧表缺 `platform_name` / `message_type` / `session_id` | 从 `umo` 解析会话 |
| `content_kind` 缺失或为空 | 按 `assume_kinds_when_missing`（默认 `text`）处理 |

> 实测：`chat_memory/shared_store.py` 的 `SHADOW_SCHEMA_VERSION` 当前为 **9**，
> 而 `portrait/docs/CM_SHARED_STORE.md` 写的是 15 —— 以代码为准。

## 11. 已知边界

- 影子表 schema 版本会变（当前 `SHADOW_SCHEMA_VERSION=9`，见 `shared_store.py`）；
  版本不等意味着表刚被 `DROP` 重建，数据可能为空——`ShadowCmSource.schema_version` 可用于自检。
- 旧表同群多实例会看到重复消息，本包不做内容去重（那是 CM 影子表的职责）；
  跨实体合并应基于 `platform_name + sender_id`，不要用 `platform_id`。
- `Pipeline` 是单线程编排；多实例并发请各自持有 `SqliteSink` 实例（WAL 只读安全）。
- 不做 token 预算与上下文长度保护：超长窗口请调小 `window_limit`。
- ticker 的「是否达标」按消息条数估算（轮模式按 2 条/轮），只用于调速，
  与 `process_session()` 里按 `unit_count` 的精确判定可能有一条消息的偏差——不影响正确性。
- 积压快照按会话逐个 `COUNT`；会话数极多（数万）时每轮成本不可忽略，可加大 `tick_min_seconds`。
