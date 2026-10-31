"""命令行入口：不依赖 AstrBot，可直接 ``python -m cm_pipeline.cli`` 运行。

子命令：

- ``status``   查看每个会话的游标、状态与待消费条目数
- ``once``     跑一轮扫描（读窗口 → 萃取 → 提交）
- ``backfill`` 从指定游标开始逐窗口回填（默认关闭，需显式触发）
- ``init``     生成一份配置模板
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from .config import PipelineConfig, load_config, save_config
from .extract import ExtractionSpec, WindowExtractor, load_prompt
from .llm import HttpOpenAiCompatClient
from .pipeline import Pipeline
from .sink import SqliteSink
from .source import open_cm_source
from .ticker import AdaptiveTicker, TickerConfig


def build_pipeline(config: PipelineConfig) -> tuple[Pipeline, SqliteSink, object]:
    """按配置装配 ``Pipeline``；返回 (pipeline, sink, source) 供调用方关闭。"""
    problems = config.validate()
    if problems:
        raise SystemExit("配置有问题：\n- " + "\n- ".join(problems))

    source = open_cm_source(
        config.cm_db,
        dialect=config.dialect,
        include_kinds=config.include_kinds,
    )
    sink = SqliteSink(config.own_db)
    client = HttpOpenAiCompatClient(
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
    )
    spec = ExtractionSpec()
    prompt_path = getattr(config, "prompt_path", "")
    if prompt_path:
        spec = ExtractionSpec(system_prompt=load_prompt(prompt_path))
    extractor = WindowExtractor(
        spec=spec,
        client=client,
        attempts=config.llm_attempts,
        timeout_seconds=config.llm_timeout_seconds,
        backoff_seconds=config.llm_backoff_seconds,
    )
    pipeline = Pipeline(source, extractor, sink, config)
    return pipeline, sink, source


def _cmd_status(config: PipelineConfig) -> int:
    source = open_cm_source(config.cm_db, dialect=config.dialect)
    sink = SqliteSink(config.own_db)
    try:
        sessions = source.list_sessions()
        print(f"CM 方言: {getattr(source, 'dialect', config.dialect)}")
        print(f"物理会话: {len(sessions)}")
        rows = {row["session_key"]: row for row in sink.list_cursors()}
        for session in sessions:
            latest = source.latest_cursor(session)
            row = rows.get(session.key, {})
            print(
                f"  {session.key:<60} 最新={latest.token() if latest else '-':<16}"
                f" 游标={row.get('cursor_token', '-') or '-':<16}"
                f" 状态={row.get('last_status', '-') or '-':<16}"
                f" 错误={row.get('last_error', '') or ''}"
            )
        print(f"待消费条目: {sink.count_items(consumed=False)}")
    finally:
        source.close()
        sink.close()
    return 0


def _cmd_once(config: PipelineConfig) -> int:
    pipeline, sink, source = build_pipeline(config)
    try:
        report = pipeline.scan_once()
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    finally:
        source.close()
        sink.close()
    return 0


def _cmd_backfill(config: PipelineConfig, args: argparse.Namespace) -> int:
    pipeline, sink, source = build_pipeline(config)
    try:
        outcomes = pipeline.backfill(
            from_seq=int(args.from_seq),
            max_windows=int(args.max_windows),
            protect=bool(args.protect),
        )
        for outcome in outcomes:
            print(
                f"{outcome.session.key} {outcome.status} units={outcome.unit_count} "
                f"items={outcome.items} cursor={outcome.cursor_token}"
            )
    finally:
        source.close()
        sink.close()
    return 0


def _cmd_daemon(config: PipelineConfig, args: argparse.Namespace) -> int:
    """常驻：默认自适应节拍；``--fixed-tick`` 可退化为固定节拍。"""
    pipeline, sink, source = build_pipeline(config)
    fixed = float(getattr(args, "fixed_tick", 0) or 0)
    ticker_config = (
        TickerConfig.fixed(fixed) if fixed > 0 else config.ticker_config()
    )
    ticker = AdaptiveTicker(pipeline, ticker_config)
    try:
        ticker.run()
    finally:
        source.close()
        sink.close()
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    template = PipelineConfig()
    template.cm_db = args.cm_db or template.cm_db
    template.own_db = args.own_db or template.own_db
    save_config(template, args.output)
    print(f"已写出配置模板: {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cm_pipeline", description="CM 萃取管线")
    parser.add_argument("--config", required=False, help="配置文件（JSON）")
    parser.add_argument("--log-level", default="", help="覆盖日志级别")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="查看游标与状态")
    sub.add_parser("once", help="跑一轮扫描")
    daemon = sub.add_parser("daemon", help="常驻循环（默认自适应节拍）")
    daemon.add_argument(
        "--fixed-tick",
        type=float,
        default=0.0,
        help="固定节拍秒数；0=自适应（积压加速、空闲降速）",
    )
    backfill = sub.add_parser("backfill", help="历史回填（显式触发）")
    backfill.add_argument("--from-seq", type=int, default=0)
    backfill.add_argument("--max-windows", type=int, default=1)
    backfill.add_argument("--protect", action="store_true", help="跳过受保护会话")
    init = sub.add_parser("init", help="生成配置模板")
    init.add_argument("--output", default="cm_pipeline.json")
    init.add_argument("--cm-db", default="")
    init.add_argument("--own-db", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "init":
        return _cmd_init(args)

    if not args.config:
        parser.error("除 init 外都需要 --config")
    config = load_config(args.config)
    level = args.log_level or config.log_level
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="[%(asctime)s] [cm_pipeline] %(levelname)s %(message)s",
    )
    if args.command == "status":
        return _cmd_status(config)
    if args.command == "once":
        return _cmd_once(config)
    if args.command == "daemon":
        return _cmd_daemon(config, args)
    if args.command == "backfill":
        return _cmd_backfill(config, args)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
