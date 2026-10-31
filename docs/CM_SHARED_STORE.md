# CM 物理去重消息表：契约与核对要点

> 状态：数据源契约背景（2026-09-12）。当前实现与取舍以 [`DESIGN.md`](DESIGN.md) 为准；
> 文中“daemon / 旧表尚未生效”等描述属于历史核对，不代表当前运行状态。
> 用途：群像（`portrait`）插件的**数据源契约摘要**。本文件是摘要，
> 权威定义在 `../../chat_memory/shared_store.py` 与 `../../chat_memory/docs/multi_bot_shared_store.md`；
> 两者冲突时以代码为准，并回头修订本文件。
> 日期：2026-09-12

## 1. 目标结构（CM `shadow_schema_statements()`）

`chat_memory_messages` —— 物理消息，跨 BOT 实例唯一一行：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `msg_uid` | TEXT PK | `p:<platform_name>:<message_type>:<session_id>:<platform_uid>`（有平台共享 id 时）或 `l:<fingerprint>:<n>`（派生序号） |
| `fingerprint` | TEXT NOT NULL | `md5(platform_name\|message_type\|session_id\|sender_id\|platform_timestamp\|content)`，跨实例独立算出同一个值 |
| `seq` | INTEGER NOT NULL | **会话内递增序号**，唯一索引 `(platform_name,message_type,session_id,seq)` |
| `platform_name` / `message_type` / `session_id` | TEXT | 物理会话身份（**不含 platform_id**） |
| `sender_id` | TEXT | 入站 = `user_id`；出站 = `self_id` |
| `sender_nickname` | TEXT | 出站行的昵称靠「同平台会话+同发送者」的既有记录补齐 |
| `platform_timestamp` | INTEGER | **平台**时间（OneBot 事件 `time`），跨实例一致；取不到才回落本地接收时刻 |
| `content` / `content_kind` / `relation_data` | TEXT | 只留一份；**只补空、不覆写** |
| `at_id` | TEXT | 跨实例一致（实测 115,170 组里仅 1 组不一致） |
| `recalled_at` / `deleted_at` | DATETIME | 撤回是客观事件；软删按消息维度 |
| `created_at` | DATETIME | 首次入库时间 |

`chat_memory_message_bots` —— 参与行，一行 = 某实例在某会话看到/发出这条消息：

| 列 | 说明 |
| --- | --- |
| `msg_uid` | 指向物理消息 |
| `platform_id` / `self_id` | 实例 + 账号（实测 3 个 platform_id ↔ 4 个 self_id，必须一起入键） |
| `conversation_id` / `umo` | 该实例视角的会话 |
| `role` | `assistant` / `user`；读取时以 `sender_id == self_id` 复核 |
| `platform_message_id` | **该实例自己的**平台消息 id（撤回/回复定位） |
| `reply_id` / `forward_id` | 属该实例 id 空间 |
| `turn_id` / `pair_id` | 该实例轮次与配对 |
| `llm_status` / `send_status` / `persona_id` / `target_id` / `raw_timestamp` | 每实例状态；`target_id` 是出站行的「被回复/目标用户」 |
| UNIQUE | `(msg_uid, platform_id, self_id, conversation_id)` |

另有 `chat_memory_tool_calls`（工具调用，锚参与行）与 `chat_memory_shadow_meta`
（**影子表自身的 schema 版本，当前 15**）。

**物理表刻意不含**：`umo` / `platform_id` / `group_id` / `role` / `llm_status` /
`send_status` / `turn_id` / `pair_id` / `persona_id` / `message_id` / `reply_id` / `forward_id`。
理由：`umo`/`group_id` 可派生（实测零不一致），其余是每实例状态或属该实例的 id 空间。

## 2. 画像器要遵守的六条

1. **游标用 `seq`，不用 `msg_uid`**。`msg_uid` 是字符串代理键（`p:` / `l:` 前缀），
   不是自增整数，不能做 keyset 游标，也不保证与时间同序。
2. **撤回与软删必须过滤**：`recalled_at IS NULL AND deleted_at IS NULL`。
   撤回的内容不该进画像（旧表的 `query_history` **不**过滤，这是新表的额外好处）。
3. **persona 与 llm_status 从参与行 join 回来**，物理表没有；
   同一条消息可能有多行参与行（每个看到它的实例一行）。
4. **不要按内容覆写**：同一条消息在不同实例眼里可能是不同分段
   （CM 实测 9,602 组，strip 后一致 0 组，长度比中位 0.56）。
   物理行给什么就用什么，宁可少一条候选，也不要拼出用户没说过的话。
5. **多行参与行 = 同一条证据的多次独立确认**，不是多条证据。
   合并时计入 `support`，但事实只有一条。
6. **影子表会被 `DROP` 重建**（schema 版本不等时，阶段一明确不迁移影子数据）。
   所以它只能当运行期数据源，不能当唯一备份；不要依赖它做长期历史回填。

## 3. 落地前提核对清单（切读前逐项确认）

- [ ] `shared_store.py` 已提交，部署端插件版本含该文件；
- [ ] 库里出现 `chat_memory_messages` / `chat_memory_message_bots` / `chat_memory_shadow_meta`；
- [ ] `chat_memory_shadow_meta` 的版本与代码 `SHADOW_SCHEMA_VERSION` 一致（否则刚被重建，数据为空）；
- [ ] `chat_memory_messages` 行数 ≈ 旧表按 `fingerprint` 去重后的条数（量级核对）；
- [ ] 每个会话的 `seq` 从 1 起、连续、无重复（`MIN`/`MAX`/`COUNT(DISTINCT seq)` 三者自洽）；
- [ ] 抽样比对：同一条消息在旧表多行、在新表一行，且 `sender_id`/`content` 取自其中一行（不做合并覆写）；
- [ ] `recalled_at`/`deleted_at` 有值，且撤回消息确实不再出现在新表可读集合里；
- [ ] CM 侧**读取路径已切到新表**（这是 CM 的工作；我们只消费结果）。

> 截至 2026-09-12 的实际状态：库里 **没有**这三张表（`user_version=7`，仅 4 张旧表），
> `shared_store.py` 仍是工作区未提交文件，影子双写尚未在部署端生效。
> 因此当时只能做「旧表只读验证」，不能产出画像（见设计文档 §10.3）。
> 当前插件按 `DESIGN.md` 只读 V2 的 `chat_memory_messages`。
