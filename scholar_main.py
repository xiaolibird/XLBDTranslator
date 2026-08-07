#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scholar Digest 主入口（薄壳）
实现已下沉到 src/scholar/cli.py；此处仅转发 + re-export 供旧测试 import。
"""
from src.scholar.cli import (  # noqa: F401  （re-export：test_backfill.py 等直接 import 这些符号）
    main,
    run_digest,
    run_deep_research,
    run_zotero_sync,
    add_digest_arguments,
    _parse_month_range,
)

if __name__ == "__main__":
    main()
