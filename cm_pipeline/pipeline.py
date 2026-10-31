"""编排：一个 tick 内扫描会话、判定阈值、读窗口、萃取、提交游标。

核心循环（与 portrait 设计一致）：

.. code-block:: text

    每 tick：
      1. 列出所有物理会话，读取各自游标与最新位置
      2. 新会话 → 只建基线（跳过历史），本轮不萃取
      3. 读窗口 → 计单位 → 不足阈值 → 记 below_threshold 后跳过
      4. LLM 萃取 → 成功/空/跳过 → 与游标同事务提交
                   → 失败 → 记失败状态，游标不动（下次重试同一窗口）
      5. 受每 tick / 每日预算限制，超出顺延

本模块不做任何 AstrBot 相关的事情，也没有 Hook；宿主可以在线程、定时任务或
自己的 asyncio 循环里通过 ``asyncio.to_thread`` 调用它。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .contracts import (
    LLM_SUCCESS,
    STATUS_FAILED,
    STATUS_SUCCESS,
    CmSource,
    Cursor,
    Perspective,
    ResultSink,
    SessionKey,
    Window,
)
from .config import PipelineConfig
from .extract import WindowExtractor
from .render import RenderOptions, render_window

LOGGER_NAME = "cm_pipeline"

STATUS_NO_DATA = "no_data"
STATUS_BASELINE = "baseline"
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_BUDGET_EXHAUSTED = "budget_exhausted"
STATUS_LLM_FAILED = "llm_failed"


@dataclass(frozen=True)
class SessionOutcome:
    """单会话在本轮的处理结果。"""

    session: SessionKey
    status: str
    unit_count: int = 0
    items: int = 0
    cursor_token: str = ""
    error: str = ""


@dataclass
class TickReport:
    """一轮扫描的汇总。"""

    started_at: str
    finished_at: str = ""
    outcomes: list[SessionOutcome] = field(default_factory=list)
    extract_calls: int = 0

    @property
    def committed(self) -> list[SessionOutcome]:
        return [item for item in self.outcomes if item.status == STATUS_SUCCESS]

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "extract_calls": self.extract_calls,
            "outcomes": [
                {
                    "session": item.session.key,
                    "status": item.status,
                    "unit_count": item.unit_count,
                    "items": item.items,
                    "cursor_token": item.cursor_token,
                    "error": item.error,
                }
                for item in self.outcomes
            ],
        }


class Pipeline:
    """把「源 → 萃取器 → 结果库」串起来的编排器。"""

    def __init__(
        self,
        source: CmSource,
        extractor: WindowExtractor,
        sink: ResultSink,
        config: PipelineConfig | None = None,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
        today_key: Callable[[], str] | None = None,
    ) -> None:
        self.source = source
        self.extractor = extractor
        self.sink = sink
        self.config = config or PipelineConfig()
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._today_key = today_key or self._default_today_key

    # -- 主循环 ---------------------------------------------------------

    def scan_once(self) -> TickReport:
        """扫描一轮；单会话失败不影响其它会话。"""
        report = TickReport(started_at=self._now_iso())
        remaining = self._remaining_daily_budget()
        tick_budget = max(0, int(self.config.budget_per_tick))
        for session in self._ordered_sessions():
            if tick_budget <= 0 or remaining <= 0:
                report.outcomes.append(
                    SessionOutcome(session=session, status=STATUS_BUDGET_EXHAUSTED)
                )
                continue
            try:
                outcome = self.process_session(session)
            except Exception as error:  # 防御：任何未预期异常都不该中断整轮
                message = f"{type(error).__name__}: {error}"
                self.log.warning("[pipeline] 会话处理异常 session=%s err=%s", session.key, message)
                self.sink.record_failure(session, STATUS_FAILED, message)
                outcome = SessionOutcome(session=session, status=STATUS_FAILED, error=message)
            report.outcomes.append(outcome)
            # 成功与失败都真的调用过 LLM，都要计入预算（失败重试也算成本）。
            if outcome.status in (STATUS_SUCCESS, STATUS_LLM_FAILED):
                report.extract_calls += 1
                remaining -= 1
                tick_budget -= 1
                self._bump_daily_usage(1)
        report.finished_at = self._now_iso()
        return report

    def process_session(self, session: SessionKey) -> SessionOutcome:
        """处理单个会话：基线 → 阈值 → 萃取 → 提交。"""
        cursor = self.sink.load_cursor(session)
        if cursor is None:
            baseline = self.source.latest_cursor(session)
            if baseline is None:
                return SessionOutcome(session=session, status=STATUS_NO_DATA)
            self.sink.ensure_baseline(session, baseline)
            return SessionOutcome(
                session=session,
                status=STATUS_BASELINE,
                cursor_token=baseline.token(),
            )

        window = self.source.read_window(
            session, cursor, max(1, int(self.config.window_limit))
        )
        if window is None or window.is_empty:
            self.sink.record_status(session, STATUS_NO_DATA)
            return SessionOutcome(
                session=session, status=STATUS_NO_DATA, cursor_token=cursor.token()
            )

        rendered = render_window(window, self._render_options())
        if rendered.unit_count < max(1, int(self.config.extract_threshold)):
            self.sink.record_status(session, STATUS_BELOW_THRESHOLD)
            return SessionOutcome(
                session=session,
                status=STATUS_BELOW_THRESHOLD,
                unit_count=rendered.unit_count,
                cursor_token=cursor.token(),
            )

        result = self.extractor.extract(window, rendered.text)
        if not result.is_committable:
            self.sink.record_failure(session, STATUS_LLM_FAILED, result.error)
            return SessionOutcome(
                session=session,
                status=STATUS_LLM_FAILED,
                unit_count=rendered.unit_count,
                error=result.error,
                cursor_token=cursor.token(),
            )
        outcome = self.sink.commit(
            result, window.end, unit_count=rendered.unit_count
        )
        self.log.info(
            "[pipeline] 提交会话=%s 窗口=%s 单位=%d 条目=%d 新增=%d 状态=%s",
            session.key,
            window.window_id[:10],
            rendered.unit_count,
            outcome.items_total,
            outcome.items_inserted,
            result.status,
        )
        return SessionOutcome(
            session=session,
            status=STATUS_SUCCESS,
            unit_count=rendered.unit_count,
            items=outcome.items_inserted,
            cursor_token=outcome.cursor_token,
        )

    # -- 积压快照（供自适应 ticker） -------------------------------------

    def backlog_snapshot(self):
        """统计待处理会话与消息量；每次扫描后调用一次，全部是廉价 COUNT。"""
        from .ticker import BacklogSnapshot

        ready_threshold = self._ready_message_threshold()
        sessions_total = 0
        with_pending = 0
        ready = 0
        pending_messages = 0
        for session in self.source.list_sessions():
            sessions_total += 1
            cursor = self.sink.load_cursor(session)
            count = self.source.count_messages_after(session, cursor)
            if count <= 0:
                continue
            with_pending += 1
            pending_messages += count
            if count >= ready_threshold:
                ready += 1
        return BacklogSnapshot(
            sessions_total=sessions_total,
            sessions_with_pending=with_pending,
            sessions_ready=ready,
            pending_messages=pending_messages,
            budget_remaining=self._remaining_daily_budget(),
        )

    def _ready_message_threshold(self) -> int:
        """达到「可萃取」大约需要多少条消息（轮模式按 2 条/轮估算）。"""
        base = max(1, int(self.config.extract_threshold))
        mode = str(self.config.unit or "follow_cm")
        if mode == "follow_cm":
            statuses = {str(item) for item in self.config.llm_status_filter}
            mode = "round" if statuses == {LLM_SUCCESS} else "message"
        return base * 2 if mode == "round" else base

    # -- 历史回填（手动） -----------------------------------------------

    def backfill(
        self, *, from_seq: int = 0, max_windows: int = 1, protect: bool = False
    ) -> list[SessionOutcome]:
        """从指定位置开始逐窗口回填（默认不启用；调用方须显式触发）。

        ``protect=True`` 时跳过 ``protected_user_ids`` 命中的会话。
        """
        outcomes: list[SessionOutcome] = []
        for session in self.source.list_sessions():
            if protect and self._is_protected(session):
                continue
            cursor: Cursor | None = Cursor(seq=int(from_seq))
            for _ in range(max(1, int(max_windows))):
                window = self.source.read_window(
                    session, cursor, max(1, int(self.config.window_limit))
                )
                if window is None or window.is_empty:
                    break
                rendered = render_window(window, self._render_options())
                result = self.extractor.extract(window, rendered.text)
                if not result.is_committable:
                    outcomes.append(
                        SessionOutcome(
                            session=session,
                            status=STATUS_LLM_FAILED,
                            unit_count=rendered.unit_count,
                            error=result.error,
                        )
                    )
                    break
                self.sink.commit(result, window.end, unit_count=rendered.unit_count)
                outcomes.append(
                    SessionOutcome(
                        session=session,
                        status=STATUS_SUCCESS,
                        unit_count=rendered.unit_count,
                        items=len(result.items),
                        cursor_token=window.end.token(),
                    )
                )
                cursor = window.end
        return outcomes

    # -- 内部 -----------------------------------------------------------

    def _render_options(self) -> RenderOptions:
        return RenderOptions(
            perspective=self.config.perspective(),
            include_kinds=self.config.include_kinds,
            llm_status_filter=self.config.llm_status_filter,
            unit=self.config.unit,
            current_user_id=self.config.current_user_id,
        )

    def _ordered_sessions(self) -> list[SessionKey]:
        sessions = self.source.list_sessions()
        with_pending: list[tuple[int, SessionKey]] = []
        for session in sessions:
            latest = self.source.latest_cursor(session)
            cursor = self.sink.load_cursor(session)
            if latest is None:
                continue
            pending = latest.is_after(cursor) if cursor else True
            with_pending.append((1 if pending else 0, session))
        with_pending.sort(key=lambda item: (-item[0], item[1].key))
        return [session for _, session in with_pending]

    def _is_protected(self, session: SessionKey) -> bool:
        protected = {str(item) for item in self.config.protected_user_ids if str(item)}
        return bool(protected) and session.session_id in protected

    def _remaining_daily_budget(self) -> int:
        used = int(self.sink.get_stat(self._usage_key(), "0") or 0)
        return max(0, int(self.config.daily_budget) - used)

    def _bump_daily_usage(self, delta: int) -> None:
        used = int(self.sink.get_stat(self._usage_key(), "0") or 0)
        self.sink.set_stat(self._usage_key(), str(used + int(delta)))

    def _usage_key(self) -> str:
        return f"extract_calls:{self._today_key()}"

    def _default_today_key(self) -> str:
        try:
            from zoneinfo import ZoneInfo

            moment = self._clock().astimezone(ZoneInfo(self.config.timezone_name))
        except Exception:  # 时区不可用时不阻塞主流程
            moment = self._clock()
        return moment.strftime("%Y-%m-%d")

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "LOGGER_NAME",
    "STATUS_BASELINE",
    "STATUS_BELOW_THRESHOLD",
    "STATUS_BUDGET_EXHAUSTED",
    "STATUS_LLM_FAILED",
    "STATUS_NO_DATA",
    "Pipeline",
    "SessionOutcome",
    "TickReport",
]
