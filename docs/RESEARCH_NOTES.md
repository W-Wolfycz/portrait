# 参考插件调研笔记

> 日期：2026-09-12
> 目的：为群像（`portrait`）设计做同类实现调研。
> 源码位置：`../reference_profiles/<repo>/`（codeload HTTPS tarball 拉取，**勿删**）
> 注意：本机 `git clone` 走不通（git 协议超时），拉取一律用
> `curl -sSL https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/main`。

## 0. 一句话结论

**5 个参考插件都没有实现我们要做的东西**：没有「事件订阅 + 每日 LLM 合并 + 长/中/短期分层
+ 中性存储 + 注入期按人格上色」。它们全是扁平字段或自由文本 Markdown，
LLM 输出多为自由文本或弱校验 JSON，且**没有一个感知 `persona_id`/`self_id`**——
多 Bot 单实例下会直接串数据。可借的是工程手法，不是架构。

## 1. 横向对照

| 插件 | 触发 | 存储 | 分层 | LLM 结构化输出 | persona/bot 隔离 | 注入 | 许可证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `Bluezeamer/memory` | 无自动（主 Agent 调 16 个工具） | Markdown 槽位文件 + `history/*.md` | ✗ | ✗（参数由 docstring 生成） | ✗ | system_prompt 全量拼接 | **无（保留所有权利）** |
| `Luna-channel/soulmap` | 主 LLM 输出内联标签 | 单 JSON `user_profiles.json` | ✗（16 个平铺字段） | 正则标签 `[Profile: k=v]` | ✗（仅默认 user_id 键） | system_prompt 全量拼接 | **无** |
| `Trantormann/memory_sync` | 6h 轮询核心库 `data_v4.db` | 2 个 Markdown + 游标文本 | ✗（profile/task 二分） | JSON 数组（仅 isinstance 校验） | ✗（SQL 连 platform_id 都没选） | **无注入** | **无** |
| `TaoXiaoBai/user_profile` | 查询时惰性触发 | AstrBot KV（3 个键） | ✗（扁平标签 + 风险分） | JSON + 白名单/夹紧校验 | ✗ | 无自动注入，只有 llm_tool | **MIT** |
| `Zhalslar/portrayal` | 聊天命令 | `portrayal.json` | ✗ | 自由文本 | ✗（`persona_id=f"{nick}_{uid}"` 会撞） | 注入的其实是基础资料，画像本体没注入 | **MIT** |

许可证要点：**只有 `user_profile` 与 `portrayal` 是 MIT（可借代码）；
另外三个无 LICENSE 文件、GitHub API `license=null`，默认保留所有权利，只能借思路。**

## 2. 值得搬到本项目的工程手法

| 手法 | 出处 | 为什么值得 |
| --- | --- | --- |
| 纯逻辑抽到不 import astrbot 的独立模块（`profile_core.py`） | user_profile | 可直接单测，符合本项目「单测不导入真实 astrbot.core」 |
| **材料指纹当缓存键**（prompt 任一输入变化即失效） | user_profile | 比纯 TTL 准，用于 `persona_render` 失效判断 |
| `singleflight + generation + asyncio.shield` 防在途写回覆盖新值 | user_profile | 合并/渲染并发时的正确性保障 |
| `<untrusted_evidence>` 包裹证据 + 白名单 + 长度夹紧 | user_profile | 防提示词注入，直接用于候选事实的渲染 |
| 失败态显式暴露（`llm_status`/`partial_errors`） | user_profile | 对应我们的 `merge_run.status`，不静默丢 |
| 类型注解驱动的 `ConfigNode` 配置树 | portrayal | 避免 `dict.get("a", {}).get("b")` 散落 |
| `protected_user_ids` 隐私黑名单 | portrayal | 直接抄进配置设计 |
| prompt 与代码分离（内置 yaml 可覆盖） | portrayal | 提示词迭代不需要改代码 |
| 游标 + 严格比较的极简增量；LLM 失败**不推进游标** | memory_sync | 天然 at-least-once，对应我们的游标提交规则 |
| 「已有名单塞进 prompt 让 LLM 自去重」 | memory_sync | 用在每日合并的提示词里 |
| 用户 ID 白名单正则清洗再落盘 | memory（无许可证，仅思路） | 防路径穿越/IDOR |
| 注释锚点做块级 CRUD | memory（仅思路） | 若将来要导出 Markdown 视图可借 |

## 3. 必须避开的共性坑（5 个全中或多数中）

1. **非原子整文件覆写、无锁**：`soulmap` 的 `_load_data` 遇 `JSONDecodeError` 直接返回 `{}`
   → 下次保存**整库清零**；`memory_sync` 的 `_trim_memory` 因 `sec_end` 陈旧导致
   裁剪时静默丢弃该节之后的全部内容。
2. **LLM 输出无 schema 校验，解析失败静默丢数据**：`memory_sync` 的
   `_parse_entries` 坏 JSON 返回 `[]` 被当成功、游标照推 → 这批消息永久跳过。
3. **全量注入 system_prompt、无 token 预算**：`soulmap` 每轮无条件注入全量画像；
   `memory` 的 `history_index.md` 无界追加，多 bot 下成倍放大。
4. **无 persona/bot 维度**：全部按 `user_id` 单键，多 Bot 单实例必然串数据。
5. **画像正文进日志**：`soulmap` 用 `logger.info` 直接打 `字段=值`（违反本项目
   「INFO 不承载正文」约定）。
6. **时区**：`soulmap` 用 `datetime.now()` 朴素本地时间（违反本项目时区约定）。

## 4. 与各自上游实现对齐的细节（排障时可能用到）

- `portrayal`：数据源是 OneBot11 `get_group_msg_history`（命令触发，每轮固定 200 条、
  `max_query_rounds=200`），画像 `portrait` 是自由文本；`core/profile_service.py` 是死代码
  （用 `gender=` 而模型字段是 `sex`）；`requirements.txt` 的 `pillowmd` 是 v1.1.5 移除 t2i 后的残留。
  部署端已安装（`data/plugins/astrbot_plugin_portrayal`，v1.1.7，与 upstream main 一致），
  但 `plugin_data/` 下无数据 → **从未运行过**。
- `user_profile`：`llm_generate` 未传 umo → 多配置路由下可能取到非会话命中的 provider；
  `/画像删除` 用 generation++ 让在途写回失效（不阻塞等待），这个思路可借。
- `memory_sync`：`master_id` 是死配置（只在 L59 赋值，全文件无引用）；
  `_resolve_provider_id` 读 `context.provider_manager.inst_map` 私有属性，兜底可能拿到非 chat provider。
  实测其 `_parse_user_content` 在 content 为字符串时直接整块返回：本机库 413/413 条字符串型
  user 消息中 412 条内嵌 `<system_reminder>`，会把群历史上下文整块喂给 LLM 并把同群所有人塌成一节。
- 同名不同项目提醒：`Kess66666/astrbot-plugin-memory-sync` 是做 A2A 跨实例同步的另一个仓，
  与本项目的 `reference_profiles/astrbot_plugin_memory_sync` 无关；若将来真要做跨实例，应另拉该仓。

## 5. `chat_memory_extractor`（萃取中心）评估

**结论：评估过，最终决定不依赖它**（用户决策 D3：画像插件自己读 CM、自持游标）。

它的能力（已按代码核对）：严格 `(created_at, record_id)` keyset 游标、
`partition = sha256(conversation_id ␟ persona_id ␟ scope ␟ mode_signature)[:24]`、
`batch_id` 幂等、消费者注册协议（`register_consumer(name, handler, required_ack)`）、
`ExtractionEvent` 携带 `records[]`（原始 CM 记录）+ `conversation_text`（中性身份渲染）
+ `participant_identities[]`（`platform:sender_id`/`display_name`/`aliases[]`/`is_bot`）+ `memories[]`。

不用它的原因：

1. 部署端**根本没装**（`data/plugins/` 无 `chat_memory_extractor`，`plugin_data/cm_extract_archive` 为空）。
2. README 首行标注「调研产物，不再维护」，`docs/RESEARCH.md` 的结论是
   「进度改由消费者自持、中心退化为无状态服务」——采纳它等于压一条冻结的依赖。
3. 它的提示词面向**通用事实记忆**（事件/主题颗粒），画像需要的「关于人的稳定属性」
   必须在插件侧另写一套提示词，复用它反而多一层不匹配。
4. 但它仍是**优秀参考实现**：窗口读取、身份渲染、JSON 校验、游标算法（≈700 行 + 88 单测）
   都在 `chat_memory_extractor/core/`，本插件的 M1/M2 可对照它的做法，但**不复制其代码**
   （它是 AGPL-3.0 派生，直接抄会把许可证带进来）。

## 6. 实测到的环境事实（设计依据）

以下结论来自对部署端 CM 库的**只读**抽样（`mode=ro&immutable=1`），仅用于确认架构前提：

- `chat_memory_records` 33 列，`self_id` / `persona_id` / `platform_id` / `sender_nickname` /
  `user_id` / `turn_id` / `created_at_utc` 都在；339461 行、21 个 UMO、4 个 self_id、3 个平台实例。
- **同群三个 Bot 的 `message_id` 交集为 0**（BOT1∩BOT2∩BOT3 = 0，union 318257）：
  每个 Bot 只看得见自己的那份消息流 → 重复萃取不可去重，三份是互补观测。
- 同一群里同一 Bot 有多个 `conversation_id`（会话切换）与多个 `persona_id`（换人格）
  → 代码不能假设「一 UMO 一会话一人格」。
- 单群记录量 10 万+ / Bot → 首次回填成本真实存在，必须分桶限速（默认不回填）。
