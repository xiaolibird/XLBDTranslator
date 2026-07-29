#!/usr/bin/env python3
"""把 agent 裁决出的终稿注入 bundle，不改动 bundle 的其余字段。

    PYTHONPATH=. python scripts/merge_final.py <bundle.json> <final.json>

final.json 的结构：
  {
    "close_reading_final": {...},      # 严格 CloseReading
    "cross_check_report": {...},
    "one_line": "...",
    "abstract_en": "...",              # 可选，补 segment.original_abstract
    "abstract_zh": "...",              # 可选，补 segment.translated_abstract
    "metadata": {...}                  # 可选，覆盖 segment.metadata 的若干字段
  }

存在的理由：bundle 含 close_reading_script 等大字段，agent 直接 Read+Write 整个
bundle 会把几万 token 的脚本草稿吃进上下文。这里只做定点字段替换 + 原子落盘。
"""
import json
import sys
from pathlib import Path

from src.scholar.schema import CloseReading


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    bundle_file, final_file = Path(sys.argv[1]), Path(sys.argv[2])
    b = json.loads(bundle_file.read_text("utf-8"))
    f = json.loads(final_file.read_text("utf-8"))

    cr = f["close_reading_final"]
    CloseReading.model_validate(cr)          # 校验失败就别落盘

    b["close_reading_final"] = cr
    b["cross_check_report"] = f["cross_check_report"]
    b["one_line"] = f["one_line"]
    b["status"] = "final"

    seg = b.setdefault("segment", {})
    if f.get("abstract_en"):
        seg["original_abstract"] = f["abstract_en"]
    if f.get("abstract_zh"):
        seg["translated_abstract"] = f["abstract_zh"]
    for k, v in (f.get("metadata") or {}).items():
        seg.setdefault("metadata", {})[k] = v

    tmp = bundle_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(bundle_file)

    n_sec = len(cr.get("sections", []))
    n_sent = sum(len(s.get("sentences", [])) for s in cr.get("sections", []))
    n_tag = sum(1 for s in cr.get("sections", []) for x in s.get("sentences", []) if x.get("tag"))
    print("✅ {} | status=final | {} 节 {} 句（{} 句带 tag）| one_line={}".format(
        bundle_file.name, n_sec, n_sent, n_tag, b["one_line"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
