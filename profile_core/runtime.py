"""扫描器运行时：单实例后台 task、按 session 扫描、每日合并。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .contracts import RawMessage, SessionKey, sha256_hex
from .extract import SegmentExtractor
from .llm import AstrBotLlmClient, LlmClient, LlmError
from .merge import MergeError, ProfileMerger
from .policy import is_policy_refusal
from .refcodec import RefCatalog, encode_profile, render_profile
from .render import RenderOptions, render_segment
from .source import V2Source
from .store import ProfileStore, detect_reset, utc_now_iso
from .segment import (
    SegmentPolicy,
    plan_segment,
    prepend_turn_context,
    reply_turn_ids,
    with_reply_context,
)

_EMPTY_SUMMARY = {"persons": [], "group": {}, "mentioned": []}


def load_timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


class Runtime:
    """一个进程只有一个实例；插件禁用/重载时由 terminate 取消。"""

    def __init__(
        self,
        context: Any,
        settings: Settings,
        *,
        logger: Any,
        data_dir: str,
        llm: LlmClient | None = None,
        cm_db_path: str = "",
        profile_db_path: str = "",
    ) -> None:
        self.context = context
        self.settings = settings
        self.logger = logger
        self.data_dir = data_dir
        self._injected_llm = llm
        self._cm_db_path = (cm_db_path or "").strip()
        self._profile_db_path = (profile_db_path or "").strip()
        self.tz = timezone.utc
        self.source: V2Source | None = None
        self.store: ProfileStore | None = None
        self.llm: LlmClient | None = None
        self.extractor: SegmentExtractor | None = None
        self.merger: ProfileMerger | None = None
        self.error = ""

        self._lock = asyncio.Lock()
        self._merge_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._last_flush_date: date | None = None
        self._last_merge_date: date | None = None
        self._merge_retry_at = 0.0
        self._merge_attempt_date: date | None = None
        self._merge_attempts = 0
        self._extract_failures: dict[str, int] = {}
        self._merge_failures: dict[str, int] = {}
        self._max_interval = float(max(10, settings.tick_seconds))
        self._min_interval = 10.0
        self._interval = self._max_interval

    # -- 生命周期 -------------------------------------------------------

    async def start(self, auto_loop: bool = True) -> None:
        try:
            self.tz = self._resolve_timezone()
            cm_path = self._cm_db_path
            if not cm_path:
                from pathlib import Path

                from astrbot.api.star import StarTools

                cm_path = str(
                    Path(StarTools.get_data_dir("chat_memory")) / "chat_memory.db"
                )
            profile_path = self._profile_db_path
            if not profile_path:
                from pathlib import Path

                profile_path = str(Path(self.data_dir) / "profile.db")
            from pathlib import Path

            Path(profile_path).parent.mkdir(parents=True, exist_ok=True)
            self.source = V2Source(cm_path)
            self.store = ProfileStore(profile_path)
            llm: LlmClient = self._injected_llm or AstrBotLlmClient(
                self.context,
                provider_id=self.settings.llm_provider,
                timeout_seconds=self.settings.llm_timeout_seconds,
                max_concurrency=self.settings.llm_max_concurrency,
                logger=self.logger,
            )
            self.llm = llm
            self.extractor = SegmentExtractor(llm)
            self.merger = ProfileMerger(llm)
            if auto_loop:
                self._task = asyncio.create_task(
                    self._loop(), name="portrait_scanner"
                )
                self._log(
                    "INFO",
                    "扫描器启动：sessions=%d pending_segments=%d interval=%.0fs"
                    % (
                        len(self.source.list_sessions()),
                        self.store.profile_counts()["pending_segments"],
                        self._interval,
                    ),
                )
            else:
                self._log("INFO", "画像插件已加载，扫描器未启用")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"扫描器启动失败：{self.error}")
            await self._close()

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._log("WARNING", f"扫描器退出异常：{type(exc).__name__}")
            self._task = None
        await self._close()

    async def _close(self) -> None:
        if self.source is not None:
            self.source.close()
            self.source = None
        if self.store is not None:
            self.store.close()
            self.store = None

    @property
    def ready(self) -> bool:
        return (
            self.source is not None
            and self.store is not None
            and self.extractor is not None
        )

    # -- 主循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopped:
            processed = 0
            try:
                async with self._lock:
                    processed = await self._scan_once()
                await self._maybe_merge()
                if self.store is not None:
                    self.store.purge_expired()
                self._adapt_interval(processed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("ERROR", f"扫描轮次失败：{type(exc).__name__}: {exc}")
            await asyncio.sleep(self._interval)

    def _adapt_interval(self, processed: int) -> None:
        """智能扫描：积压（>1 片段）缩短 5%，恰好 1 个不变，空闲放大 5% 回上限。"""
        if not self.settings.smart_scan:
            self._interval = self._max_interval
            return
        before = self._interval
        if processed > 1:
            self._interval = max(self._min_interval, self._interval * 0.95)
        elif processed == 0:
            self._interval = min(self._max_interval, self._interval * 1.05)
        if abs(self._interval - before) > 0.05:
            remaining_sessions, remaining_messages, pending = self._backlog()
            self._log(
                "DEBUG",
                f"扫描间隔 {before:.1f}s -> {self._interval:.1f}s "
                f"(processed={processed}, remaining_sessions={remaining_sessions}, "
                f"remaining_msgs={remaining_messages}, pending={pending})",
            )

    def _backlog(self) -> tuple[int, int, int]:
        """返回还有新消息的 session 数、待推进消息数、待合并片段数。"""
        if self.source is None or self.store is None:
            return 0, 0, 0
        sessions = 0
        messages = 0
        for session in self.source.list_sessions():
            if self.settings.is_blacklisted(session):
                continue
            gap = self.unextracted_messages(session)
            if gap > 0:
                sessions += 1
                messages += gap
        pending = self.store.profile_counts().get("pending_segments", 0)
        return sessions, messages, pending

    def unextracted_messages(self, session: SessionKey) -> int:
        """当前 session 游标之后、尚未萃取的消息数（CM seq 口径）。"""
        if self.source is None or self.store is None:
            return 0
        max_seq = self.source.max_seq(session)
        cursor = self.store.get_cursor(session)
        return max(0, max_seq - (cursor.seq if cursor else 0))

    def current_sender_name(
        self, session: SessionKey, sender_id: str
    ) -> str:
        """只查当前会话 CM 历史昵称，不做跨会话填充。"""
        if self.source is None:
            return ""
        for name in self.source.sender_names(session, sender_id):
            if name and not name.isdigit():
                return name
        return ""

    def _resolve_timezone(self):
        """跟随 AstrBot 主配置；缺失/非法静默回退 UTC。"""
        name = ""
        try:
            config = self.context.get_config() if self.context else None
            if config is not None and hasattr(config, "get"):
                name = str(config.get("timezone") or "")
        except Exception:
            name = ""
        return load_timezone(name)

    # -- 扫描 -----------------------------------------------------------

    async def _scan_once(self) -> int:
        if self.source is None or self.store is None or self.extractor is None:
            return 0
        if not (
            self.settings.enable_group_profile
            or self.settings.enable_person_profile
        ):
            return 0

        meta = self.source.shadow_meta()
        bumped = self.store.apply_schema_guard(meta)
        if bumped:
            self._log(
                "INFO",
                f"CM schema 标记变化，重绑 {bumped} 个会话游标，保留 seq",
            )
        schema_ver = int(meta.get("schema_version") or 0)
        dual = str(meta.get("dual_write_started_at") or "")

        now = datetime.now(self.tz)
        daily_flush = self._take_daily_flush(now)
        processed = 0
        hit_budget = False
        active = [
            session
            for session in self.source.list_sessions()
            if not self.settings.is_blacklisted(session)
        ]
        if self.settings.failure_advance_after > 0:
            active.sort(
                key=lambda item: 0
                if self._extract_failures.get(item.key)
                else 1
            )
        while active and processed < self.settings.max_segments_per_scan:
            budget = self.settings.max_segments_per_scan - processed
            batch = active[:budget]
            more = len(active) > len(batch)

            async def scan_one(session: SessionKey) -> bool:
                try:
                    return await self._scan_session(
                        session, meta, schema_ver, dual, daily_flush
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log(
                        "WARNING",
                        f"会话扫描失败 session={session.key}："
                        f"{type(exc).__name__}",
                    )
                    return False

            results = await asyncio.gather(*(scan_one(item) for item in batch))
            ok_sessions = [
                session
                for session, ok in zip(batch, results)
                if ok
            ]
            processed += len(ok_sessions)
            if more or processed >= self.settings.max_segments_per_scan:
                hit_budget = True
            active = ok_sessions
        if daily_flush and not hit_budget:
            self._last_flush_date = now.date()
        return processed

    def _fetch_rows(
        self,
        session: SessionKey,
        cursor_seq: int,
    ) -> list[RawMessage]:
        """按批读取，直到够一个片段的 text 阈值或没有更多消息。"""
        assert self.source is not None
        threshold = max(1, self.settings.extract_threshold)
        batch_size = threshold
        rows: list[RawMessage] = []
        last_seq = int(cursor_seq)
        text_count = 0
        while text_count < threshold:
            batch = self.source.fetch_after(
                session, last_seq, limit=batch_size
            )
            if not batch:
                break
            rows.extend(batch)
            last_seq = int(batch[-1].seq)
            text_count += sum(
                1 for message in batch if message.has_text and message.identity
            )
            if len(batch) < batch_size:
                break
        return rows

    def _advance_extract_cursor(
        self,
        session: SessionKey,
        plan: Any,
        reason: str,
    ) -> int:
        """政策拒绝/连续失败时跳过 N 条 text，返回实际推进条数。"""
        advance = self.settings.extract_refusal_advance_messages
        if advance <= 0 or not plan.messages:
            return 0
        count = min(advance, len(plan.messages))
        target = plan.messages[count - 1]
        assert self.store is not None
        self.store.advance_cursor(
            session,
            seq=target.seq,
            msg_uid=target.msg_uid,
            reason=reason,
        )
        return count

    async def _scan_session(
        self,
        session: SessionKey,
        meta: dict[str, str],
        schema_ver: int,
        dual: str,
        daily_flush: bool,
    ) -> bool:
        assert self.source is not None
        assert self.store is not None
        assert self.extractor is not None
        cursor = self.store.get_cursor(session)
        max_seq = self.source.max_seq(session)
        initial_seq = 0
        initial_uid = ""
        if (
            cursor is None
            and self.settings.initial_cursor == "latest"
            and max_seq > 0
        ):
            initial_seq = max_seq
            initial_uid = self.source.msg_uid_at(session, max_seq) or ""
        if cursor is None:
            cursor = self.store.ensure_cursor(
                session,
                schema_ver=schema_ver,
                dual_started=dual,
                initial_seq=initial_seq,
                initial_uid=initial_uid,
            )
        elif (
            self.settings.initial_cursor == "latest"
            and cursor.seq == 0
            and cursor.generation == 0
            and max_seq > 0
            and not self.store.pending_segments(session, limit=1)
        ):
            cursor = self.store.advance_cursor(
                session,
                seq=max_seq,
                msg_uid=self.source.msg_uid_at(session, max_seq) or "",
                reason="initial_latest",
            )
        uid_at = (
            self.source.msg_uid_at(session, cursor.seq) if cursor.seq else None
        )
        decision = detect_reset(cursor, max_seq, uid_at, meta)
        if decision.reset:
            cursor = self.store.reset_cursor(
                session,
                reason=decision.reason,
                schema_ver=schema_ver,
                dual_started=dual,
            )
            self._log(
                "INFO",
                f"游标重置 session={session.key} reason={decision.reason} "
                f"generation={cursor.generation}",
            )

        rows = self._fetch_rows(session, cursor.seq)
        plan = plan_segment(
            session,
            rows,
            cursor.seq,
            policy=SegmentPolicy(
                threshold=self.settings.extract_threshold,
                flush_min=self.settings.flush_min_units,
            ),
            daily_flush=daily_flush,
        )
        if not plan.should_run:
            return False

        original_uids = {message.msg_uid for message in plan.messages}
        segment_messages = list(plan.messages)
        if segment_messages:
            first_seq = int(segment_messages[0].seq)
            if first_seq > 1:
                before = self.source.fetch_by_seq_range(
                    session, max(1, first_seq - 5), first_seq - 1
                )
                segment_messages = prepend_turn_context(segment_messages, before)
            targets = []
            for turn_id in reply_turn_ids(segment_messages)[:5]:
                target = self.source.find_by_turn(session, turn_id)
                if target is not None:
                    targets.append(target)
            segment_messages = with_reply_context(segment_messages, targets)
        context_uids = {
            message.msg_uid
            for message in segment_messages
            if message.msg_uid not in original_uids
        }

        rendered = render_segment(
            segment_messages,
            RenderOptions(
                timezone=self.tz,
                context_uids=tuple(context_uids),
            ),
        )
        cursor_uid = self.source.msg_uid_at(
            session, plan.last_scanned_seq
        ) or plan.messages[-1].msg_uid

        if rendered.message_count == 0:
            self.store.commit_segment(
                session,
                cursor,
                segment_messages,
                _EMPTY_SUMMARY,
                status="empty",
                cursor_seq=plan.last_scanned_seq,
                cursor_msg_uid=cursor_uid,
                ttl_days=self.settings.segment_ttl_days,
            )
            self._extract_failures.pop(session.key, None)
            self._log(
                "INFO",
                f"空片段 session={session.key} seq={plan.last_scanned_seq}",
            )
            return True

        started_at = self._format_ts(segment_messages[0].timestamp)
        ended_at = self._format_ts(segment_messages[-1].timestamp)
        result = await self.extractor.extract(
            session, rendered, started_at=started_at, ended_at=ended_at
        )
        retry_attempts = max(0, self.settings.extract_retry_attempts)
        retry = 0
        while result.status == "failed" and retry < retry_attempts:
            retry += 1
            self._log(
                "WARNING",
                f"萃取失败 session={session.key} reason={result.error}，"
                f"立即重试 {retry}/{retry_attempts}",
            )
            result = await self.extractor.extract(
                session, rendered, started_at=started_at, ended_at=ended_at
            )
        if result.status == "refused":
            advanced = self._advance_extract_cursor(
                session, plan, "extract_refused"
            )
            self._extract_failures.pop(session.key, None)
            if advanced:
                self._log(
                    "WARNING",
                    f"萃取被政策拒绝 session={session.key}，"
                    f"推进 {advanced} 条消息",
                )
                return True
            return False
        if result.status == "failed":
            failures = self._extract_failures.get(session.key, 0) + 1
            self._extract_failures[session.key] = failures
            threshold = self.settings.failure_advance_after
            if threshold > 0 and failures >= threshold:
                advanced = self._advance_extract_cursor(
                    session, plan, "extract_failed_advance"
                )
                if advanced:
                    self._extract_failures.pop(session.key, None)
                    self._log(
                        "WARNING",
                        f"萃取连续失败 {failures} 次 session={session.key}，"
                        f"推进 {advanced} 条消息",
                    )
                    return True
            self._log(
                "WARNING",
                f"萃取失败 session={session.key} reason={result.error}，"
                "连续失败计数保留，下一次扫描优先重试",
            )
            return False

        self._extract_failures.pop(session.key, None)

        segment_id = self.store.commit_segment(
            session,
            cursor,
            segment_messages,
            result.summary,
            status=result.status,
            model="",
            prompt_digest="",
            usage=result.usage,
            cursor_seq=plan.last_scanned_seq,
            cursor_msg_uid=cursor_uid,
            ttl_days=self.settings.segment_ttl_days,
        )
        self._log(
            "INFO",
            f"片段落库 session={session.key} seq={plan.last_scanned_seq} "
            f"messages={len(segment_messages)} status={result.status} "
            f"persons={len(result.summary.get('persons', []))} segment={segment_id}",
        )
        return True

    def _take_daily_flush(self, now: datetime) -> bool:
        if now.time() < self.settings.flush_clock:
            return False
        if self._last_flush_date == now.date():
            return False
        return True

    # -- 合并 -----------------------------------------------------------

    async def _maybe_merge(self) -> None:
        if self.store is None or self.merger is None:
            return
        if not (
            self.settings.enable_group_profile
            or self.settings.enable_person_profile
        ):
            return
        now = datetime.now(self.tz)
        if now.time() < self.settings.merge_clock:
            return
        if self._last_merge_date == now.date():
            return
        if self._merge_attempt_date != now.date():
            self._merge_attempt_date = now.date()
            self._merge_attempts = 0
        if self._merge_attempts >= 3:
            return
        if now.timestamp() < self._merge_retry_at:
            return
        ok = await self.merge_now(run_date=now.date().isoformat())
        if ok:
            self._last_merge_date = now.date()
            self._merge_attempts = 0
            self._merge_retry_at = 0.0
        else:
            self._merge_attempts += 1
            delay = self._merge_retry_delay(self._merge_attempts)
            self._merge_retry_at = now.timestamp() + delay if delay else 0.0
            self._log(
                "DEBUG",
                f"合并失败（今天第 {self._merge_attempts}/3 次）"
                + (f"，{delay / 60:.0f} 分钟后重试" if delay else "，今天不再自动重试"),
            )

    @staticmethod
    def _merge_retry_delay(attempt: int) -> float:
        """第 1/2 次失败分别 5/15 分钟后重试；第 3 次后等次日。"""
        return 300.0 if attempt == 1 else 900.0 if attempt == 2 else 0.0

    async def merge_now(self, run_date: str | None = None) -> bool:
        if self.store is None or self.merger is None:
            return False
        async with self._merge_lock:
            date_text = run_date or datetime.now(self.tz).date().isoformat()
            sessions = [
                session
                for session in self.store.sessions_with_pending()
                if not self.settings.is_blacklisted(session)
            ]
            if not sessions:
                return True
            results = await asyncio.gather(
                *(self._merge_session(session, date_text) for session in sessions)
            )
            return all(results)

    async def _merge_session(self, session: SessionKey, date_text: str) -> bool:
        assert self.store is not None
        assert self.merger is not None
        while True:
            pending = self.store.pending_segments(session, limit=None)
            if not pending:
                return True
            segment_ids = [item.segment_id for item in pending]
            summaries = [_summary(item.summary_json) for item in pending]
            started_at = utc_now_iso()
            try:
                group_task = asyncio.create_task(
                    self._merge_group(session, summaries)
                )
                persons_task = asyncio.create_task(
                    self._merge_persons(session, summaries)
                )
                group_payload, persons = await asyncio.gather(
                    group_task, persons_task
                )
            except (LlmError, MergeError) as exc:
                advance = self.settings.merge_refusal_advance_segments
                if is_policy_refusal(str(exc)):
                    if advance > 0:
                        skipped = pending[:advance]
                        self.store.skip_segments(
                            [item.segment_id for item in skipped],
                            reason="policy_skipped",
                        )
                        self._merge_failures.pop(session.key, None)
                        self._log(
                            "WARNING",
                            f"合并被政策拒绝 session={session.key}，"
                            f"跳过 {len(skipped)} 个片段",
                        )
                        continue
                    self._log(
                        "WARNING",
                        f"合并被政策拒绝 session={session.key}，"
                        "但跳过数量为 0",
                    )
                    return False

                failures = self._merge_failures.get(session.key, 0) + 1
                self._merge_failures[session.key] = failures
                threshold = self.settings.failure_advance_after
                if threshold > 0 and failures >= threshold and advance > 0:
                    skipped = pending[:advance]
                    self.store.skip_segments(
                        [item.segment_id for item in skipped],
                        reason="merge_failed_advance",
                    )
                    self._merge_failures.pop(session.key, None)
                    self._log(
                        "WARNING",
                        f"合并连续失败 {failures} 次 session={session.key}，"
                        f"跳过 {len(skipped)} 个片段",
                    )
                    continue
                self._log(
                    "WARNING",
                    f"合并失败 session={session.key}：{type(exc).__name__}",
                )
                return False
            self.store.commit_merge(
                session,
                group_payload=group_payload,
                persons=persons,
                segment_ids=segment_ids,
                run_info={
                    "run_id": sha256_hex(
                        session.key, date_text, *segment_ids
                    ),
                    "run_date": date_text,
                    "status": "success",
                    "segment_count": len(segment_ids),
                    "person_count": len(persons),
                    "started_at": started_at,
                },
            )
            self._merge_failures.pop(session.key, None)
            self._log(
                "INFO",
                f"合并成功 session={session.key} segments={len(segment_ids)} "
                f"persons={len(persons)}",
            )
            return True

    def _ref_catalog(
        self,
        session: SessionKey,
        summaries: list[dict[str, Any]],
    ) -> RefCatalog:
        assert self.store is not None
        entries: list[tuple[str, str]] = []
        for sender_id, name in self.store.list_profile_persons(session):
            entries.append((sender_id, name))
        for summary in summaries:
            persons = summary.get("persons")
            if not isinstance(persons, list):
                continue
            for person in persons:
                if not isinstance(person, dict):
                    continue
                sender_id = str(person.get("sender_id") or "").strip()
                name = str(person.get("name") or "").strip()
                if sender_id:
                    entries.append((sender_id, name))
        return RefCatalog(entries)

    async def _merge_group(
        self,
        session: SessionKey,
        summaries: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        assert self.merger is not None
        if not self.settings.enable_group_profile:
            return None
        if session.message_type != "GroupMessage":
            return None
        previous = self.store.get_profile_session(session)
        catalog = self._ref_catalog(session, summaries)
        batch_size = self.settings.max_segments_per_merge
        if batch_size <= 0 or len(summaries) <= batch_size:
            fragments = [
                render_profile(item["group"], catalog, for_llm=True)
                for item in summaries
                if isinstance(item.get("group"), dict) and item["group"]
            ]
            if not fragments:
                return None
            previous_llm = (
                render_profile(previous, catalog, for_llm=True)
                if previous
                else None
            )
            merged = await self.merger.merge_group(previous_llm, fragments)
            return encode_profile(merged, catalog)

        chunks = [
            summaries[index : index + batch_size]
            for index in range(0, len(summaries), batch_size)
        ]

        def fragments_of(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                render_profile(item["group"], catalog, for_llm=True)
                for item in chunk
                if isinstance(item.get("group"), dict) and item["group"]
            ]

        async def merge_chunk(
            chunk: list[dict[str, Any]],
        ) -> dict[str, Any] | None:
            fragments = fragments_of(chunk)
            if not fragments:
                return None
            merged = await self.merger.merge_group(None, fragments)
            return encode_profile(merged, catalog)

        batch_profiles = [
            item
            for item in await asyncio.gather(*(merge_chunk(chunk) for chunk in chunks))
            if item
        ]
        if not batch_profiles:
            return None
        previous_llm = (
            render_profile(previous, catalog, for_llm=True)
            if previous
            else None
        )
        final_fragments = [
            render_profile(item, catalog, for_llm=True)
            for item in batch_profiles
        ]
        merged = await self.merger.merge_group(previous_llm, final_fragments)
        return encode_profile(merged, catalog)

    async def _merge_persons(
        self,
        session: SessionKey,
        summaries: list[dict[str, Any]],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        assert self.merger is not None
        if not self.settings.enable_person_profile:
            return []
        catalog = self._ref_catalog(session, summaries)
        grouped: dict[str, dict[str, Any]] = {}
        for summary in summaries:
            persons = summary.get("persons")
            if not isinstance(persons, list):
                continue
            for person in persons:
                sender_id = str(person.get("sender_id") or "").strip()
                sections = person.get("sections")
                if not sender_id or not isinstance(sections, dict) or not sections:
                    continue
                entry = grouped.setdefault(
                    sender_id, {"name": "", "fragments": []}
                )
                name = str(person.get("name") or "").strip()
                if name:
                    entry["name"] = name
                entry["fragments"].append(sections)

        async def merge_one(
            sender_id: str, entry: dict[str, Any]
        ) -> tuple[str, str, dict[str, Any]]:
            previous = self.store.get_profile_person(session, sender_id)
            previous_llm = (
                render_profile(previous, catalog, for_llm=True)
                if previous
                else None
            )
            name = catalog.name(sender_id) or entry["name"]
            fragments = [
                render_profile(item, catalog, for_llm=True)
                for item in entry["fragments"]
            ]
            payload = await self.merger.merge_person(
                previous_llm, fragments, name
            )
            payload = encode_profile(payload, catalog)
            return sender_id, name, payload

        return list(
            await asyncio.gather(
                *(merge_one(sender_id, entry) for sender_id, entry in grouped.items())
            )
        )

    # -- 状态 -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        counts = (
            self.store.profile_counts()
            if self.store is not None
            else {"sessions": 0, "persons": 0, "pending_segments": 0}
        )
        return {
            "ready": self.ready,
            "error": self.error,
            "interval": round(self._interval, 1),
            "last_flush": str(self._last_flush_date or ""),
            "last_merge": str(self._last_merge_date or ""),
            **counts,
        }

    async def scan_now(self) -> int:
        async with self._lock:
            return await self._scan_once()

    # -- 内部 -----------------------------------------------------------

    def _format_ts(self, timestamp: int) -> str:
        try:
            return datetime.fromtimestamp(int(timestamp), tz=self.tz).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError, OSError):
            return "未知时间"

    def _log(self, level: str, message: str) -> None:
        text = f"[Portrait] {message}"
        if level == "ERROR":
            self.logger.error(text)
        elif level == "WARNING":
            self.logger.warning(text)
        elif level == "DEBUG":
            self.logger.debug(text)
        else:
            self.logger.info(text)


def _summary(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text or "")
    except (TypeError, ValueError):
        return dict(_EMPTY_SUMMARY)
    return parsed if isinstance(parsed, dict) else dict(_EMPTY_SUMMARY)
