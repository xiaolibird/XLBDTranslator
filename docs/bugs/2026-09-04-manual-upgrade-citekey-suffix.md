# 手动精读升级已入库论文时，keeper 拿到后缀键、干净基键被 duplicate 占住并从全局书目里消失

日期：2026-09-04（观察发生在 2026-09-03 的精读入库会话）
状态：**A+B 均已修（2026-09-04）；存量 `lin2025Addressing` 待跑工具处置**，见文末。原状态：未修
严重度：**中高**——不丢数据，但**已发出的 `[@基键]` 引用在全局书目里悬空**，且已实际波及 npj 主稿 v10
发现者：xiaolibird / Claude（2026-09-03 `read-paper` 精读 `shi2025Federatedb` 会话）

---

## 一句话

一篇论文先被月度流水线浅读入库（auto，0 条取证句），后来被 `read-paper` 手动全文精读（manual，几十条取证句）。
去重层正确地把手动深读判为 keeper、把 auto 条目判为 duplicate——**但 citekey 是在去重之前分配的**，
于是 keeper 只能拿到 `<基键>b`，而**干净的基键留在了那条 0 条取证句的 duplicate 上**。
`all_references.json` 只收 keeper，所以**基键从全局书目里消失**，此前所有 `[@基键]` 引用悬空。

---

## 实测证据

### 本轮触发的那一例（已人工处置）

```
$ PYTHONPATH=. python3 scripts/read_pdf.py finalize output/scholar_notes/manual/2026-09/88995ff1eb6c425b.paper.json
...
⚠️ 2 条 duplicate 条目的行内 citekey 与其 keeper 不一致（从该月札记抄引用会引错论文，
   须人工把 md/sidecar 改成 keeper 键）：
     2025-09 的 lin2025Addressing（应为 keeper 的 lin2025Addressingb，见 科研札记_2025-09_全文精读.md:807）
     2025-10 的 shi2025Federated（应为 keeper 的 shi2025Federatedb，见 科研札记_2025-10_全文精读.md:655）
```

索引里的两条（同一 `dedup_key = doi:10.1016/j.knosys.2025.114601`）：

| citekey | series | month | reading_depth | highlights | duplicate_of |
|---|---|---|---|---|---|
| `shi2025Federated` | auto | 2025-10 | None | **0** | `doi:10.1016/j.knosys.2025.114601@2026-09` |
| `shi2025Federatedb` | manual | 2026-09 | chunked | **69** | None（keeper） |

而 `all_references.json` 里只有 `shi2025Federatedb`——**`shi2025Federated` 不在全局书目中**。

### 存量那一例（未处置，且波及面更大）

```
基键 lin2025Addressing → keeper lin2025Addressingb
  keeper = manual / 2026-09-02-插补扭曲（32 条取证句）
  dup    = auto   / 2025-09（0 条取证句），科研札记_2025-09_全文精读.md:807
全局书目：lin2025Addressing ❌ 不在 ⇒ 下游 [@lin2025Addressing] 悬空
```

**它已经进了主稿**（`~/Desktop/Lab/P4/manuscript/`）：

| 悬空基键 | md 引用行数 | 涉及稿件 |
|---|---|---|
| `@lin2025Addressing` | **6 行 / 5 个文件** | `npj/manuscript_npj_v{3,4,5,9,10}.md`——**含最新的 v10**，且是「current guidance」这一 load-bearing 位置 |
| `@shi2025Federated` | 2 行 / 2 个文件 | `causal/manuscript_causal_v1.md:614`、`npj/manuscript_npj_v2.md:104` |

各稿有自包含的本地 bibliography（各 2 个 json 里定义了该 id），所以 **pandoc 目前照样出稿**——
这正是它一直没被发现的原因。**一旦改用 `scholar-write` 走全局书目取证，这 8 处引用会全部解析失败。**

---

## 根因（三段，位置具体）

### R1 —— 键分配发生在去重之前，且 `used` 是无 dedup_key 意识的扁平字符串集合

`src/scholar/notes.py:286-300`：

```python
# used 初始化为"本批已用键 + 索引已有 citekey 全集"
used = {v for v in citekeys.values() if v} | (existing_citekeys or set())
...
base = _fallback_citekey(seg.metadata)
if key in used:
    for suf in _suffix_seq():          # b…z, bb…zz（src/scholar/_citekey_utils.py）
        if cand not in used: ...
```

`used` 只是一个字符串集合，**不知道占着 `shi2025Federated` 的那条与本篇是同一个 `dedup_key`**。
而 keeper/duplicate 的判定发生在更晚的 `src/scholar/notes_index.py::_global_pass`（约 :790–:840）。
⇒ 时序上，键分配拿不到「这个冲突键属于同一篇、且它马上就要被降为 duplicate」这个信息，只能盲目加后缀。

### R2 —— 陈旧键守卫只告警，且修复指引对无 sidecar 的月份只能执行一半

`src/scholar/notes_index.py:831-840` 的注释写明了不自动改写的理由（md 回写有 RENAME_PARTIAL 半改风险），
这个取舍本身合理。但提示语是「**须人工把 md/sidecar 改成 keeper 键**」，而：

- **76 个 auto 月里有 43 个没有 sidecar**（`ls *全文精读.index.json` 得 33，`ls *全文精读.md` 得 76）；
  本例的 `科研札记_2025-10_全文精读.index.json` **就不存在**，只能改 md + `.references.json` 两处。
- 该指引也没说要改 `.references.json`。本轮实测：只改 md 不改 references，
  该月 references 里仍留着旧 id，下一次索引重建照样把它读回来。

### R3 —— 提醒只走 `logger.warning`，而同仓已论证这类消息在自动权限 agent 会话里看不见

`scripts/read_pdf.py::_reuse_citekeys` 的注释里已经把这个论证写死过一次：

> warning 在自动权限模式的 agent 会话里看不见（同 `_sync_embedding_best_effort` 的论证），故走 notify。

那里因此用了 `logger.error` + `notify(...)`。但 `notes_index.py:836` 的陈旧键守卫**只有 `logger.warning`**。
本例能被发现纯属偶然（我恰好在读 finalize 的完整输出）。`lin2025Addressing` 那条从 2026-09-02 起就在报，一直没人处置——这就是证据。

---

## 复现

```bash
# 1. 任取一篇已被月度流水线浅读入库的论文（series=auto、highlights=0）
# 2. 对同一 PDF 跑手动精读
PYTHONPATH=. python3 scripts/read_pdf.py ingest <该篇.pdf>
#    → ingest 末尾会提示「索引里已有同文 …… 继续 finalize 则手动深读成为 keeper」
# 3. 亲读核验、写回 close_reading_final、status=final、finalize
# 4. 观察：
python3 -c "
import json;ps=json.load(open('output/scholar_notes/literature_index.json'))['papers']
g=[p for p in ps if p.get('dedup_key')=='<该篇 dedup_key>']
print([(p['citekey'],p.get('series'),len(p.get('highlights') or []),p.get('duplicate_of')) for p in g])"
#    → keeper 带 b 后缀，duplicate 占着基键
python3 -c "
import json;ar=json.load(open('output/scholar_notes/all_references.json'))
print('<基键>' in {i['id'] for i in (ar if isinstance(ar,list) else ar['references'])})"
#    → False（基键从全局书目消失）
```

---

## 本轮已做的人工处置（供参考，不是修复）

对 `shi2025Federated` 采取了守卫提示的方向——**把 duplicate 的行内键指向 keeper**：

```
output/scholar_notes/科研札记_2025-10_全文精读.md:655        [@shi2025Federated] → [@shi2025Federatedb]
output/scholar_notes/科研札记_2025-10_全文精读.references.json  id 同上替换
```
随后 `scripts/notes_index.py` 重建：**撞键 0 组不变**，该条提醒消失，只剩 `lin2025Addressing` 一条。
备份在 `/tmp/bk_md_2025-10.md`、`/tmp/bk_ref_2025-10.json`。

⚠️ **这个方向让丑键成为规范键**，与「已发出的引用用的是基键」相冲突——所以它是止血，不是修法。
**P4 主稿里那 8 处 `[@基键]` 我没有动**（属投稿稿件正文，需人工确认）。

---

## 候选修法（按推荐度，每条附风险）

### A（推荐）——`used` 排除同 dedup_key 的条目，让 keeper 继承基键

在 `notes.py:288` 构造 `used` 时，把「与本 segment 同 `dedup_key` 的既有条目所占的键」从 `used` 里剔除，
使手动深读直接拿到干净基键；随后 `_global_pass` 把 auto 条目判为 duplicate 时，两者 citekey 天然一致，陈旧键守卫不再触发。

- **前提**：`notes.py` 那一层要能拿到 `existing` 条目的 `dedup_key`，而不只是 citekey 集合——
  现在传进来的 `existing_citekeys` 是 `set[str]`，需要改成 `dict[citekey → dedup_key]` 或并传一份映射。
- **风险 1**：会造成**同月 md 内两条同 citekey**（keeper 与 duplicate 都叫基键）。需确认索引的撞键检测把 duplicate 排除在外，
  否则「撞键 0 组」会变成 1 组。本轮实测：把 duplicate 的行内键改成与 keeper **完全相同**时撞键仍为 0，所以这条大概率安全，但**必须在真数据上验证一遍**。
- **风险 2**：全库有大量以 b/c/d 结尾的 live citekey，但**其中绝大多数是正常键而非消歧后缀**
  （我用 `endswith` 粗数得 403，这个数不可引用——它把 `…Federated`/`…Missingb` 这类都算进去了）。
  改动前**必须先跑下方普查脚本**，它只认「keeper 键 = duplicate 键 + 纯小写字母后缀」这一严格模式；
  当前该模式只命中 1 组。不要按 403 这个量级去评估改动面。

### B —— 保持现状，但把守卫升级为 notify + 自动改 references

改 md 有 RENAME_PARTIAL 风险，这个理由成立；但 **`.references.json` 是结构化 JSON、按 id 精确替换无歧义**，可以自动改。
同时把 `notes_index.py:836` 那条 `logger.warning` 按 `_reuse_citekeys` 的先例改成 `logger.error` + `notify`。
- 成本最低，但**不解决「丑键成为规范键」**，只是让人更早知道。

### C —— 给基键在 `all_references.json` 里留一条 alias
让 `[@基键]` 仍能解析到 keeper 的书目项。
- **风险**：CSL-JSON 没有 alias 语义，等于往全局书目里塞重复条目，`scholar-write` 侧要能识别，改动面反而大。**不推荐。**

---

## 普查脚本（跑一次就能知道存量有多少）

```python
import json
from collections import defaultdict
ps = json.load(open('output/scholar_notes/literature_index.json'))['papers']
by = defaultdict(list)
for p in ps:
    if p.get('dedup_key'):
        by[p['dedup_key']].append(p)

def is_suffix_of(base, key):            # 后缀恒为纯小写字母 b..z，最长 3（_suffix_seq）
    return (key.startswith(base) and 0 < len(key) - len(base) <= 3
            and all('b' <= c <= 'z' for c in key[len(base):]))

ar = json.load(open('output/scholar_notes/all_references.json'))
ids = {i['id'] for i in (ar if isinstance(ar, list) else ar['references'])}
for k, g in by.items():
    keeper = [x for x in g if not x.get('duplicate_of')]
    dups   = [x for x in g if x.get('duplicate_of')]
    if not keeper or not dups:
        continue
    kk = keeper[0].get('citekey') or ''
    for d in dups:
        dk = d.get('citekey') or ''
        if dk and is_suffix_of(dk, kk):
            print(dk, '→', kk, '| keeper', keeper[0].get('series'), keeper[0].get('month'),
                  len(keeper[0].get('highlights') or []), '条 | 基键在书目:', dk in ids)
```

2026-09-04 实测输出（`shi2025Federated` 已人工处置后）：

```
lin2025Addressing → lin2025Addressingb | keeper manual 2026-09-02-插补扭曲 32 条 | 基键在书目: False
```

---

## 待办清单

- [ ] 选定修法（A / B），先跑普查脚本确认存量规模
- [ ] 处置存量 `lin2025Addressing`（改 `科研札记_2025-09_全文精读.md:807` + 同月 `.references.json`）
- [ ] ~~改 P4 主稿的 8 处引用~~ —— **2026-09-04 用户决定：暂不动正文。**
      各稿本地 bibliography 自包含，pandoc 出稿不受影响，故可以推迟。
      **但它是这个 bug 的最终暴露面**：一旦改用 `scholar-write` 走全局书目取证，
      `npj/manuscript_npj_v{3,4,5,9,10}.md` 的 `@lin2025Addressing`（6 行）与
      `causal/manuscript_causal_v1.md:614`、`npj/manuscript_npj_v2.md:104` 的 `@shi2025Federated`（2 行）会全部解析失败。
      若修法选了 A（keeper 继承干净基键），这 8 处**无需改动即自动恢复**——这是 A 优于 B 的实际理由。
- [ ] 把 `notes_index.py:836` 的陈旧键守卫从 `logger.warning` 改为 `logger.error` + `notify`（与 `_reuse_citekeys` 一致）
- [ ] 修「须人工把 md/sidecar 改成 keeper 键」这句提示：补上 `.references.json`，并说明 43 个 auto 月没有 sidecar

---

## 修复（2026-09-04 台账批）

用户决定 A+B 都做：

- **A（根治分配顺序）**：`notes.write_notes(..., existing_key_owners=…)` 新增可选参数（`notes_index.existing_citekey_owners(index_path, exclude_note_files)`
  提供 citekey → dedup_key 映射；撞键态映射为空串不许继承）。兜底键分配时：基键被占、但占有者与本篇 **dedup_key 相同**、且本批尚未有人用它 → 直接继承基键，
  keeper 与 duplicate 同键，`_global_pass` 陈旧键守卫不再触发。本批两篇同 dedup_key 仍各得不同键（与 `_reuse_citekeys` 口径一致）；不同论文同基键照旧加后缀。
  三处调用方已接：`read_pdf._rebuild_month`、`backfill_notes.run_month`（新可选参数 `existing_owners`）、`ingest.run_ingest`。`book_notes` 未接（书籍链路无此场景）。
  风险 1（同 citekey 两条）已由测试验证：撞键检测只看 live 条目，keeper/duplicate 同键撞键为 0，全局书目含基键。
- **B**：守卫升 `error` + `notify`、文案补 `.references.json` 与 43 个无 sidecar 月——见 `2026-09-04-inline-citekey-mismatch-warn-only.md` 文末。
- **存量处置工具**：`scripts/notes_index.py --fix-inline-citekeys --apply` 的 `suffix-keeper` 形态正是把 keeper 的 `lin2025Addressingb` 改回 `lin2025Addressing`
  （md + references + sidecar 三处，随后同步向量库），改完 P4 主稿那 8 处 `[@基键]` 无需改动即自动恢复。**本批未在生产库上执行**（先 dry-run 看计划）。
- 测试：`test/test_bugs_batch_2026_09.py` W8-A 节（继承 / 不同论文加后缀 / 不传 owners 保旧行为 / 本批两篇不共键 / owners 读取与撞键）+ W8-B 端到端。
- 待办清单更新：选定修法 ✅ A+B；处置存量 → 跑上面的工具；守卫升级 ✅；提示文案 ✅；P4 主稿 8 处按用户决定不动（A 落地后无需改）。
