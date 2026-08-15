#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 PMLR slug 当权威源，校验本库 PMLR 论文的 citekey 首作者姓与年份。

为什么 slug 能当权威：`v297/torres-fuertes26a` 由出版方分配，`torres-fuertes` 是
官方作者列表首位的姓（复姓带连字符原样保留）、`26` 是卷年份。而 citekey 是**派生量**
（_fallback_citekey：首作者姓 + 年 + 标题首个≥4字母实词），三项都可能错：
  - 复姓被 split()[-1] 截断（Torres Fuertes → fuertes、Leon Tramontini → tramontini）；
  - 元数据解析把非首作者当成首作者（molaei24a 的 citekey 却是 thakur…）；
  - publication_date 缺失/错误导致年份差一年（26a 卷却写 2025）。
citekey 错不影响身份（身份用 dedup_key），但会让出稿时的引用显示挂到错的作者名下。

只报告不改写：改 citekey 要同步 md + references.json + sidecar 三处，
交给 notes_index 的 _rename_citekey_in_note（--apply 时调用）。

用法：
    PYTHONPATH=. python3 scripts/audit_citekeys_vs_pmlr.py                    # 只审计
    PYTHONPATH=. python3 scripts/audit_citekeys_vs_pmlr.py --apply --dry-run  # 看新键，不写盘
    PYTHONPATH=. python3 scripts/audit_citekeys_vs_pmlr.py --apply            # 真正改键

改键的三道闸（--apply 会真改磁盘，任何一道不过就跳过并点名，绝不拼垃圾键）：
  1. 原 citekey 必须能拆出「姓 + 4 位年」，否则拿不到标题实词尾巴 → 跳过（citekey_tail）；
  2. 拟生成的新键必须过 pandoc 合法字符校验 → 否则跳过（VALID_CITEKEY_RE）；
  3. _rename_citekey_in_note 三处预检全过才写盘，返回三态：
     "refused"（磁盘零改动）与 "partial"（写盘失败且回滚失败 = 磁盘半改）分列两张清单，
     任一非空都非零退出；"partial" 优先展示——那一批必须人工核对三处文件。
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                  # noqa: E402
from src.scholar.notes_index import (                                      # noqa: E402
    update_index, _rename_citekey_in_note, RENAME_OK, RENAME_PARTIAL,
    announce_rekey_side_effects)

# 姓氏字符类与消歧后缀都必须与 _citekey_utils.venue_native_id 抽出的 slug 口径一致：
#   - 后缀允许多字母（PMLR 真实存在 chen22aa / zhang22ab，已联网核实 HTTP 200）；
#   - 姓氏允许数字（如 3dgs24a）。
# 口径窄了不会报错，只会让这些条目 slug_parts 返回 None，既不进 rows 也不计入
# 「PMLR 条目 N 篇」——审计覆盖率被高报、漏审无声。故 main 里另行统计认不出的条数。
SLUG_RE = re.compile(r"^pmlr:v(\d+)/([a-z0-9\-]+?)(\d{2})([a-z]*)$")

# citekey 的「姓 + 4 位年」前缀。citekey_parts 与 citekey_tail 必须用**同一个**正则拆，
# 两处各写各的会让「判为不一致」与「据此拼新键」错位（见下方 citekey_tail 注释）。
CITEKEY_HEAD_RE = re.compile(r"^(.*?)(\d{4})")

# pandoc 允许的 citekey 字符集（首字符须为字母/数字/下划线）。生成的新键必须过这一关，
# 否则出稿时引用会在非法字符处被截断或直接解析不到条目——静默失效。
VALID_CITEKEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.#$%&+?<>~/-]*$")


def slug_parts(dedup_key):
    """pmlr:v297/torres-fuertes26a → ('torres-fuertes', 2026)；不匹配返回 None。"""
    m = SLUG_RE.match(dedup_key or "")
    if not m:
        return None
    surname, yy = m.group(2), int(m.group(3))
    # PMLR 用两位年；本库跨 2000 前后无风险，统一补 20xx。
    return surname, 2000 + yy


def norm(s):
    """去重音、只留字母，用于比较姓氏（Beñat→benat、Müller→muller）。"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if c.isalpha() and not unicodedata.combining(c)).lower()


def citekey_parts(citekey):
    """thakur2024Federated → ('thakur', 2024)；没有 4 位年时返回 (norm(整串), None)。"""
    m = CITEKEY_HEAD_RE.match(citekey or "")
    if not m:
        return norm(citekey), None
    return norm(m.group(1)), int(m.group(2))


def citekey_tail(citekey):
    """取「姓 + 4 位年」之后的标题实词部分；**拆不出年就返回 None**（不可改）。

    早先用 `re.sub(r'^[a-zA-ZÀ-ɏ]*?\\d{4}', '', ck)`：字符类只认 ASCII + 拉丁扩展字母，
    且强制年紧跟姓之后。正则不命中时 re.sub **原样返回整串**，于是 tail = 整个旧 citekey，
    新键 = 姓 + 年 + 旧键（`torresfuertes2026куксенко2024Аналіз` / `…2026anonProceedings`）。
    而这类键在 citekey_parts 里恰好返回 (norm(整串), None) → bad_name 恒为 True → 它们
    **必然**被列进 rows，--apply 一定会去改。库里真实存在 23 个这样的 citekey
    （无年键 `molaeiFederated`、连字符键 `guillen-ramirez2025Prediction`、西里尔键…），
    而无年 citekey 正是 PMLR 论文的常态来源（publication_date 缺失时 _fallback_citekey
    的 year=''）。故此处宁可返回 None 让调用方跳过并点名，绝不拼垃圾键。
    """
    m = CITEKEY_HEAD_RE.match(citekey or "")
    return citekey[m.end():] if m else None


def main():
    ap = argparse.ArgumentParser(description="用 PMLR slug 校验 citekey 的作者姓与年份")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--apply", action="store_true", help="按 slug 改写不一致的 citekey")
    ap.add_argument("--dry-run", action="store_true",
                    help="与 --apply 连用：只打印将要生成的新键与跳过项，不碰任何文件")
    args = ap.parse_args()
    if args.dry_run and not args.apply:
        ap.error("--dry-run 需与 --apply 连用（不加 --apply 本来就只审计不改写）")

    notes_dir = repo_path(args.notes_dir)
    index = update_index(notes_dir)
    all_keys = {e.get("citekey") for e in index["papers"]}

    rows = []
    audited = 0
    unparsed_slugs = []
    for e in index["papers"]:
        dk = e.get("dedup_key") or ""
        parts = slug_parts(dk)
        if not parts:
            if dk.startswith("pmlr:"):
                unparsed_slugs.append(dk)      # 显式计数，不静默归零
            continue
        audited += 1
        slug_surname, slug_year = parts
        ck_surname, ck_year = citekey_parts(e.get("citekey"))
        bad_name = norm(slug_surname) != ck_surname
        bad_year = ck_year is not None and ck_year != slug_year
        if bad_name or bad_year:
            rows.append((e, slug_surname, slug_year, ck_surname, ck_year, bad_name, bad_year))

    print("PMLR 条目 {} 篇；citekey 与官方 slug 不一致 {} 篇\n".format(audited, len(rows)))
    if unparsed_slugs:
        print("⚠️ 另有 {} 条 pmlr: 键的 slug 形态未识别、未参与审计：{}\n".format(
            len(unparsed_slugs), "、".join(sorted(unparsed_slugs))))
    if not rows:
        return 0
    print("{:<30} {:<26} {:<22} {}".format("slug（权威）", "现 citekey", "应为", "错在"))
    for e, sn, sy, cn, cy, bad_name, bad_year in rows:
        want = "{}{}{}".format(sn.replace("-", ""), sy, "")
        why = "、".join(([("姓 {} ≠ {}".format(cn, norm(sn)))] if bad_name else [])
                        + ([("年 {} ≠ {}".format(cy, sy))] if bad_year else []))
        print("{:<30} {:<26} {:<22} {}".format(
            (e.get("dedup_key") or "").replace("pmlr:", ""), e.get("citekey"), want + "…", why))

    if not args.apply:
        print("\n（只审计。加 --apply 按 slug 改写 citekey，会同步 md + references.json + sidecar；"
              "先加 --apply --dry-run 看一遍将要生成的新键）")
        return 0

    renamed = 0
    plan, skipped, failed, partial = [], [], [], []
    for e, sn, sy, _cn, _cy, _bn, _by in rows:
        ck = e.get("citekey") or ""
        # 标题实词沿用原 citekey 的尾部（那部分本来就是对的），只换姓与年。
        # 拆不出「姓+4 位年」的键一律跳过——见 citekey_tail 文档。
        tail = citekey_tail(ck)
        if tail is None:
            skipped.append((ck, "citekey 无 4 位年、无法解析出标题实词，需人工处理"))
            continue
        base = "{}{}{}".format(norm(sn.replace("-", "")), sy, tail)
        new = base
        if new in all_keys and new != ck:
            from src.scholar._citekey_utils import _suffix_seq
            for suf in _suffix_seq():
                if base + suf not in all_keys:
                    new = base + suf
                    break
        if new == ck:
            continue
        if not VALID_CITEKEY_RE.match(new):
            skipped.append((ck, "拟生成的新键 {} 含 pandoc 非法字符，拒绝改写".format(new)))
            continue
        plan.append((e, new))
        all_keys.add(new)          # 占位，避免同批内互撞

    print()
    if args.dry_run:
        for e, new in plan:
            print("  （dry-run）{} → {}".format(e["citekey"], new))
        print("\n将改键 {} 篇；跳过 {} 篇。确认无误后去掉 --dry-run 重跑。".format(
            len(plan), len(skipped)))
    else:
        touched = []          # 新键真落到磁盘上的条目（OK + PARTIAL），供收尾告知派生物
        for e, new in plan:
            # 三态：refused = 三处一处都没改（预检不过，或写盘失败已回滚）；
            # partial = 写盘失败且回滚失败，磁盘半改（「md 改了 refs 没改」会让 pandoc
            # 静默解析不到条目）。两者都不算成功，但汇报口径必须分开。
            res = _rename_citekey_in_note(notes_dir, e, e["citekey"], new)
            if res == RENAME_OK:
                renamed += 1
                touched.append(e)
                print("  🔧 {} → {}".format(e["citekey"], new))
            elif res == RENAME_PARTIAL:
                partial.append((e["citekey"], new, e.get("note_file")))
                touched.append(e)     # 新键已在磁盘上 → 派生物同样失效
            else:
                failed.append((e["citekey"], new, e.get("note_file")))
        print("\n改键 {} 篇；请重跑 scripts/notes_index.py --full 重建索引".format(renamed))
        # 改键只动 md/references/sidecar 三处；向量库与已渲染 docx 是不会自己跟上的派生物。
        # 不在这里告知，运维就只能靠"某天发现 pandoc 报 citation not found"才知道库旧了。
        announce_rekey_side_effects(notes_dir, touched)

    if skipped:
        print("\n⚠️ 跳过 {} 篇（未改动任何文件）：".format(len(skipped)))
        for ck, why in skipped:
            print("    {:<34} {}".format(ck, why))
    if partial:
        # 半改优先展示：这些札记的 md / references.json / sidecar 可能已经不一致，
        # 重跑修不好（md 里已无旧键），只能人工核对。
        print("\n⛔ {} 篇改键**已半改且回滚失败**（md/references/sidecar 可能不一致），"
              "务必优先人工核对三处：".format(len(partial)))
        for old, new, nf in partial:
            print("    {:<30} → {:<30} {}".format(old, new, nf))
    if failed:
        print("\n❌ {} 篇改键失败（md/references/sidecar 预检不过，磁盘未改动），需人工修：".format(
            len(failed)))
        for old, new, nf in failed:
            print("    {:<30} → {:<30} {}".format(old, new, nf))
    if partial or failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
