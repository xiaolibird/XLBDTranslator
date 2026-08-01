#!/usr/bin/env python3
"""
批量翻译书籍（顺序执行完整 workflow，每本独立日志与断点）

示例:
    # 翻译多本指定的书
    python scripts/translate_books.py a.epub b.pdf --mode 3 --retain-original

    # 翻译整个目录（递归找 .epub/.pdf），用 claude CLI 订阅通路
    python scripts/translate_books.py ~/Downloads/wanna-read --provider claude-agent

    # 只列出将要翻译的清单，不实际执行
    python scripts/translate_books.py ~/Downloads/wanna-read --dry-run

说明:
    - 顺序执行（非并发）：每本书内部自有 async 并发，书间并发只会争抢
      API 限额与内存；claude-agent 的进程信号量本就是全局上限 2。
    - 每本书独立 checkpoint（output/<hash>/），中断后重跑同一命令即从断点续传。
    - 单本失败不中断队列，最后统一汇总。
    - --provider claude-agent 走本机 claude CLI 订阅。注意：Claude 对整段
      版权书籍翻译可能被输出内容过滤器拦截（概率性），拦截即该本失败并
      按回退链切换到下一供应商，脚本不做任何绕过。
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.schema import Settings  # noqa: E402
from src.workflow import TranslationWorkflow  # noqa: E402
from src.workflow.builder import SettingsBuilder, PRESETS  # noqa: E402
from src.utils.logger import setup_logging, logger  # noqa: E402
from src.utils.ui import load_modes_config  # noqa: E402

SUPPORTED_SUFFIXES = {'.epub', '.pdf'}


def collect_books(paths: list[str]) -> list[Path]:
    """展开文件/目录参数为去重后的书籍清单（目录递归、按名排序）"""
    books: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            candidates = sorted(
                f for f in p.rglob('*') if f.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
            candidates = [p]
        else:
            print(f"⚠️  跳过（不存在或非 epub/pdf）: {p}", file=sys.stderr)
            continue
        for f in candidates:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                books.append(r)
    return books


def build_settings(args, book: Path) -> Settings:
    """为单本书构建 Settings：config.env 为基底，CLI 参数覆盖"""
    base = Settings.from_env_file(Path(args.config))
    builder = SettingsBuilder(base)
    if args.preset:
        builder.use_preset(args.preset)
    if args.provider:
        builder.translation_provider(args.provider)
    builder.document_path(book)
    # 每本书独立日志（文件名截断，避免 Anna's Archive 式超长文件名）
    builder.log_file(REPO_ROOT / 'logs' / f"batch_{book.stem[:60]}.log")
    settings = builder.build()

    if args.retain_original:
        settings.processing.retain_original = True

    modes = load_modes_config(settings.files.modes_config_path)
    mode_id = args.mode or settings.processing.translation_mode
    if mode_id not in modes:
        raise ValueError(f"翻译模式 '{mode_id}' 不存在，可用: {', '.join(modes.keys())}")
    settings.processing.translation_mode_entity = modes[mode_id]
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="批量翻译书籍（顺序执行，断点续传，失败不中断队列）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('paths', nargs='+', help='书籍文件或目录（目录递归收集 .epub/.pdf）')
    parser.add_argument('--config', default='config/config.env', help='配置文件路径')
    parser.add_argument('--mode', help='翻译模式 ID（默认用配置文件中的值）')
    parser.add_argument('--provider', help='翻译供应商（deepseek/gemini/claude-agent 等）')
    parser.add_argument('--preset', choices=list(PRESETS.keys()), help='配置预设')
    parser.add_argument('--retain-original', action='store_true', help='双语对照输出')
    parser.add_argument('--dry-run', action='store_true', help='只列出将要翻译的书，不执行')
    args = parser.parse_args()

    books = collect_books(args.paths)
    if not books:
        print("❌ 没有找到任何 .epub/.pdf 文件", file=sys.stderr)
        return 1

    print(f"📚 待翻译: {len(books)} 本")
    for i, b in enumerate(books, 1):
        print(f"  {i:2d}. {b.name}")
    if args.dry_run:
        return 0

    results: list[tuple[Path, str, float, str]] = []  # (book, status, seconds, detail)
    for i, book in enumerate(books, 1):
        print(f"\n{'=' * 70}\n📖 [{i}/{len(books)}] {book.name}\n{'=' * 70}")
        start = time.monotonic()
        try:
            settings = build_settings(args, book)
            setup_logging(settings)
            TranslationWorkflow(settings).execute()
            results.append((book, 'ok', time.monotonic() - start, ''))
        except KeyboardInterrupt:
            results.append((book, 'interrupted', time.monotonic() - start, '用户中断'))
            print("\n⚠️  用户中断，停止队列（已完成部分见汇总；重跑同一命令可断点续传）")
            break
        except Exception as e:
            # 单本失败不中断队列；断点已由 workflow 落盘，重跑可续传
            results.append((book, 'failed', time.monotonic() - start, f"{type(e).__name__}: {e}"))
            logger.opt(exception=True).error("💥 本书翻译失败，继续下一本: {}", str(e))

    print(f"\n{'=' * 70}\n📊 批量翻译汇总\n{'=' * 70}")
    icon = {'ok': '✅', 'failed': '❌', 'interrupted': '⏸️'}
    for book, status, seconds, detail in results:
        line = f"{icon[status]} {book.name}  ({seconds / 60:.1f} 分钟)"
        if detail:
            line += f"  —— {detail[:160]}"
        print(line)
    n_ok = sum(1 for r in results if r[1] == 'ok')
    print(f"\n完成 {n_ok}/{len(books)} 本" + ("" if n_ok == len(books) else "；失败/未跑的书重跑同一命令即可续传"))
    return 0 if n_ok == len(books) else 1


if __name__ == '__main__':
    sys.exit(main())
