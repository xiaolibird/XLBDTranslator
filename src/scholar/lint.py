# -*- coding: utf-8 -*-
"""知识层 lint 门面（阶段 4b 拆分后的稳定 import 路径）。

实现已拆到四个平级模块（按原文件自带的分节横幅切分）：
  lint_checks   四项检查 + 数据类（叶模块，头部常量也在这）
  lint_ack      ack 解析/折叠/按 section 结转（纯字符串处理）
  lint_render   整份报告渲染（唯一汇聚四项产出的层）
  lint_io       frontmatter/落盘/退出码/summarize（backfill 只依赖这里）
依赖图（无环）：checks ← ack ← io；checks/ack/io ← render。
lint_freshness 刻意**不在**此门面内、也不被 lint* 任何模块 import（防环硬约束，
经参数把渲染块传进 write_lint_report）。

纪律（PRD 审定）：
- 经门面调用的符号不得改为实现模块内直调——lint_notes.py 经 `L.` 属性晚绑定
  调用，test_lint 对门面的 monkeypatch（如 L.check_retractions）靠它才生效；
- 对全文跑 ids_in_text 是禁区（freshness 块在首标记前，故意不进 ack 全集）；
- 新增对外符号先加实现模块，再显式加进这里。
"""
from .lint_checks import (                                        # noqa: F401
    CandidatePair,
    CoverageReport,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_PAIRS,
    DEFAULT_ORPHAN_LIMIT,
    DEFAULT_PAIR_MIN_SIM,
    DEFAULT_PER_HIGHLIGHT_CAP,
    DEFAULT_PROMPT,
    DEFAULT_RECENT_MONTHS,
    DEFAULT_STALE_ANCHOR_LIMIT,
    DEFAULT_STALE_YEARS,
    LINT_GENERATOR,
    LINT_REPORT_NAME,
    PageClaim,
    PairSide,
    RELATION_EMOJI,
    RELATION_LABEL,
    REPORTABLE_RELATIONS,
    RetractionHit,
    RetractionScan,
    StaleClaim,
    Verdict,
    VerdictReport,
    _RETRACTED_TITLE_RE,
    _month_ord,
    adjudicate_contradictions,
    attach_pair_titles,
    build_contradiction_prompt,
    check_retractions,
    cited_by_page,
    cited_citekeys,
    coverage_report,
    find_contradiction_candidates,
    find_stale_claims,
    load_prompt_template,
    normalize_doi,
    parse_page_claims,
    validate_verdicts,
    year_is_implausible,
)
from .lint_ack import (                                        # noqa: F401
    ACK_ID_MARK,
    DEFAULT_CONTRADICTION_REMINDER_DAYS,
    LINT_SECTIONS,
    LINT_SECTION_MARK,
    SECTION_HEADING,
    _LINT_SECTION_RE,
    _days_since,
    contradiction_reminder,
    fold_acked_blocks,
    ids_in_text,
    parse_acks,
    read_lint_acks,
    read_previous_lint,
    read_previous_lint_counts,
    split_lint_sections,
)
from .lint_render import (                                        # noqa: F401
    _pair_lines,
    render_lint_report,
)
from .lint_io import (                                        # noqa: F401
    LINT_EXIT_CODES,
    LintCounts,
    LintOutcome,
    _insert_freshness_block,
    build_lint_frontmatter,
    carry_forward_counts,
    summarize_lint_run,
    write_lint_report,
)

# docstring 兼容：lint_notes.py 的模块说明提到 `lint.is_topic_page_file`
from .topics import is_topic_page_file                                    # noqa: F401
