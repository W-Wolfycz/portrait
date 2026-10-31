"""cm_pipeline —— 从 CM 消息表读取窗口、LLM 萃取、结果落库的可搬移参考实现。

设计目标（给要复用的插件）：

1. **零第三方依赖**：只用标准库（``sqlite3`` / ``urllib`` / ``hashlib`` / ``json``），
   不需要 AstrBot，也不 import ``astrbot`` 任何模块。
2. **三种方言可切换**：CM 影子表（``chat_memory_messages`` + ``chat_memory_message_bots``）
   与旧表（``chat_memory_records``）都能读；``dialect="auto"`` 自动探测。
3. **只读上游**：对 CM 库只用 ``mode=ro`` 打开，绝不写入。
4. **至少一次**：萃取成功才提交游标；失败保持原游标，下次重试同一窗口；
   窗口用 ``window_id`` 幂等，重复处理不会重复落库。
5. **可替换的三段**：``CmSource`` / ``Extractor`` / ``ResultSink`` 都是显式协议，
   宿主可以只替换其中一段。

最小用法：

    from cm_pipeline import (
        PipelineConfig, SqliteSink, WindowExtractor, open_cm_source, Pipeline,
        HttpOpenAiCompatClient,
    )

    cfg = PipelineConfig(cm_db=".../chat_memory.db", own_db=".../profile.db")
    source = open_cm_source(cfg.cm_db, dialect=cfg.dialect)
    sink = SqliteSink(cfg.own_db)
    extractor = WindowExtractor(spec=my_spec, client=my_client)
    report = Pipeline(source, extractor, sink, cfg).scan_once()
"""

from .config import PipelineConfig
from .contracts import (
    CmMessage,
    Cursor,
    ExtractionItem,
    ExtractionResult,
    LlmReply,
    Participant,
    Perspective,
    SessionKey,
    Window,
)
from .extract import ExtractionSpec, WindowExtractor, load_prompt
from .llm import FakeLlmClient, HttpOpenAiCompatClient, LlmClient
from .pipeline import Pipeline, SessionOutcome, TickReport
from .render import RenderOptions, count_units, render_window
from .sink import CommitOutcome, SqliteSink
from .source import (
    CmSource,
    LegacyCmSource,
    ShadowCmSource,
    open_cm_source,
)
from .ticker import (
    AdaptiveTicker,
    BacklogSnapshot,
    TickerConfig,
    TickerState,
    run_forever,
)

__all__ = [
    "AdaptiveTicker",
    "BacklogSnapshot",
    "CmMessage",
    "CmSource",
    "CommitOutcome",
    "Cursor",
    "ExtractionItem",
    "ExtractionResult",
    "ExtractionSpec",
    "FakeLlmClient",
    "HttpOpenAiCompatClient",
    "LegacyCmSource",
    "LlmClient",
    "LlmReply",
    "Participant",
    "Perspective",
    "Pipeline",
    "PipelineConfig",
    "RenderOptions",
    "SessionKey",
    "SessionOutcome",
    "ShadowCmSource",
    "SqliteSink",
    "TickReport",
    "TickerConfig",
    "TickerState",
    "Window",
    "WindowExtractor",
    "count_units",
    "load_prompt",
    "open_cm_source",
    "render_window",
    "run_forever",
]
