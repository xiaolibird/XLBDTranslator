# `replace_closeread` 吞掉精读节之后到下一篇之间的全部内容（篇末 `---` 与人手写批注）

日期：2026-09-04
状态：**已修复（2026-09-04 台账批）**；**存量 270 篇的篇末 `---` 已经丢了**（2026-09-05 复核订正，原记 169 偏低 101 篇），处置见文末
严重度：中——不丢论文、不丢精读句，丢的是篇末分隔线与写在那里的人工批注；解析不受影响（`_SECTION_RE` 认的是 `## ` 标题）
发现者：2026-09-04 台账批第 3 轮对抗审计（压测镜片报出，验伪镜片用**生产数据**证实）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python scripts/backfill_deepread.py run --apply`（任何会重写精读节的批） |
| 具体位置 | `scripts/backfill_deepread.py` 的 `replace_closeread()`：精读节终点 `cr_end` 初始化为 `end`（下一篇 `## ` 标题的行号） |
| 报错情况 | **无**。写盘成功、回执正常、退出码 0 |
| 影响 | 精读节与下一篇标题之间的一切被整段替换掉：`build_digest_note` 每篇后追加的 `---`、以及人写在篇末的批注 |
| 复现测试 | `test/test_backfill_deepread.py::test_replace_closeread_keeps_the_paper_separator_and_handwritten_notes` |

## 根因

```python
cr_end = end                      # end = 下一篇 "## " 标题的行号
for k in range(cr_start + 1, end):
    if lines[k].startswith("### ") or lines[k].startswith("## ") or lines[k].startswith("# "):
        cr_end = k
        break
merged = lines[:cr_start] + new_lines + lines[cr_end:]
```

只有遇到**标题**才提前收尾。而月度 digest 的每篇之后只有空行和一条 `---`（`notes.build_digest_note`
每篇后追加），两者都不是标题——于是 `cr_end` 一路走到 `end`，那条 `---` 连同任何写在那里的
批注一起落进被替换的区间。与本函数 docstring 写的「只动这一节：篇内的标题/裁决/摘要、以及
**其余所有论文**的字节一律不碰」相悖。

## 生产实证

第 3 轮验伪镜片对全库做只读交叉（backfill 账本 × 全部 `科研札记_*.md`）：

| 口径 | 篇数 | 丢了篇末 `---` |
|---|---|---|
| 修复前（< `021b33e`）被 `backfill_deepread` 改写过的 | 279 | **270** |
| 修复后被改写过的（45 篇 09-04 23:xx + 3 篇 09-05） | 48 | **0** |
| 从未被改写过的 | 2103 | **0** |

> 2026-09-05 复核订正：原表记「272 篇 / 丢 169」。正确的归因是把「下一篇标题前最后一个非空行」
> 算到**上一篇**头上（那才是 `replace_closeread` 实际吞掉的东西）；按本篇归因只得 80，是错的。
> 因果结论不变（对照组恰好 0），只是数字偏低了 101 篇。审计之后再无「修复前」的重写发生，
> 这 270 篇是个不再增长的闭集。

对照组是干净的 0，因果链没有别的解释。

## 修复

精读节的终点改成两步：先取**硬边界**（下一个 `#` 系标题，或篇末 `---`），再在硬边界内退到
**最后一行机器生成的精读内容**为止。精读内容只有两种形态——`**【小节名】**` 与 `- ` 开头的
句级标记行（`notes._paper_section` / `_render_closeread` 的唯一输出形态），其余一律视为外来
内容、原样留在盘上。

这样：`---` 保住；写在精读节之后的批注保住；而精读节本身仍被完整替换（不会残留旧句子）。

## 存量处置（未做，需要人决定）

那 270 篇的 `---` 无法从任何备份**精确**还原（`.backfill_deepread_backup` 只保留每次 run 的
原件，且早期的 run 已被上一条缺陷
`2026-09-04-quota-failure-looks-like-no-fulltext.md` 之外的另一条 BLOCKER 覆盖过——见本批对
`backup_files` 的修复）。但这条分隔线是**纯粹的版面元素**、位置完全确定（每篇 `## ` 标题之前），
可以机械补回：

⚠️ **下面这个脚本判据不对，别照着落盘**（2026-09-05 审计实测）：它原样跑会报
**94 个文件、364 处**，其中 **94 处是每份 md 的 `## 优先级速览` 小节标题**——那不是论文边界，
本来就不该有 `---`。照它落盘会往 94 个文件的概览节前插入错误分隔线。
`l.startswith('## ')` 数不准（把非论文小节也算进去），「往前看 3 行」也筛不净。

**正确的判据是拿索引的 `note_line` 定位论文边界**（下面第二段），它给出 270 处，与实证表一致。

```bash
# ❌ 原始脚本，仅作记录，报 364 处（含 94 处误报）
python3 - <<'PY'
import glob, re
for f in sorted(glob.glob('output/scholar_notes/科研札记_*.md')):
    lines = open(f, encoding='utf-8').read().splitlines()
    need = [i for i, l in enumerate(lines)
            if l.startswith('## ') and i > 0
            and '---' not in [x.strip() for x in lines[max(0, i-3):i]]]
    if need:
        print(f, '缺分隔线', len(need), '处')
PY
```

```bash
# ✅ 按索引 note_line 定论文边界，只数真正的篇末（给出 270 处）
PYTHONPATH=. python3 - <<'PY'
import json
from pathlib import Path
idx = json.load(open('output/scholar_notes/literature_index.json'))
byfile = {}
for e in idx['papers']:          # 顶层键是 papers，不是 entries
    nf, nl = e.get('note_file'), e.get('note_line')
    if nf and nl:
        byfile.setdefault(nf, []).append((nl, e.get('citekey')))
lost = []
for nf, lst in sorted(byfile.items()):
    p = Path('output/scholar_notes') / nf
    if not p.exists():
        continue
    lines = p.read_text(encoding='utf-8').split('\n')
    lst.sort()
    for i, (nl, ck) in enumerate(lst[:-1]):          # 末篇没有「下一篇」，不参与
        seg = [l for l in lines[nl:lst[i + 1][0] - 1] if l.strip()]
        if seg and seg[-1].strip() != '---':
            lost.append((nf, ck))
print('缺篇末分隔线', len(lost), '处，涉及', len({f for f, _ in lost}), '份 md')
PY
```

**是否值得补由用户决定**：它不影响解析、检索、书目与向量库，只影响人读 md 的分节观感。
