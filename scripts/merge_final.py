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
import os
import sys
from pathlib import Path

from src.scholar.schema import CloseReading


def _load_json(path: Path, what: str):
    """读 JSON，失败给「哪个文件、什么问题」的可操作报错（返回 None 表示失败）。
    本脚本的典型调用者是写 JSON 的 agent——漏字段/半截 JSON 正是它最常犯的错，
    裸 KeyError/JSONDecodeError 栈看不出病灶在哪个文件哪个字段。"""
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        print("❌ {} 不存在: {}".format(what, path))
    except json.JSONDecodeError as e:
        print("❌ {} 不是合法 JSON（{}）: {}".format(what, path, e))
    return None


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    bundle_file, final_file = Path(sys.argv[1]), Path(sys.argv[2])
    b = _load_json(bundle_file, "bundle")
    f = _load_json(final_file, "final.json")
    if b is None or f is None:
        return 2

    missing = [k for k in ("close_reading_final", "cross_check_report", "one_line")
               if not f.get(k)]
    if missing:
        print("❌ final.json 缺必填字段：{}（结构见本脚本 docstring）".format("、".join(missing)))
        return 2
    cr = f["close_reading_final"]
    try:
        CloseReading.model_validate(cr)      # 校验失败就别落盘
    except Exception as e:
        print("❌ close_reading_final 不符合 CloseReading schema：{}".format(str(e)[:500]))
        return 2
    # 归档硬前提与 read_pdf.py cmd_finalize 同一道门：verified_count ≥ 1。此前这里
    # 原样透传，一份没有 verified_count 的 report 若之后走「同月其他 bundle 触发的
    # 整月重建」路径（legacy 兼容对缺失字段放行），会绕过 cmd_finalize 的强校验入库。
    vc = f["cross_check_report"].get("verified_count") if isinstance(f["cross_check_report"], dict) else None
    if not isinstance(vc, int) or isinstance(vc, bool) or vc < 1:
        # isinstance(vc, bool)：bool 是 int 子类，`true` 会以 True>=1 冒充合法计数（压测抓到）
        print("❌ cross_check_report.verified_count 须为 ≥1 的整数（现为 {!r}）——"
              "亲读核验是归档硬前提，缺失或为 0 都拒绝".format(vc))
        return 2

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

    # tmp 带 pid：固定名时两个进程（如 agent 超时重试）共用同一路径互相截断；
    # `.tmp-<pid>` 也不匹配任何 `*.paper.json` glob，不会被误当 bundle 读入。
    tmp = bundle_file.with_name(bundle_file.name + ".tmp-{}".format(os.getpid()))
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
