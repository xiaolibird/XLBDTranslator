# duplicate 条目的行内 citekey 只告警不修：每轮重建都复读同一批，改一条要手工动三处派生物

日期：2026-09-04
状态：**已修复（2026-09-04）**——善后工具 + 告警升级，见文末。原状态：未修（本次手工修了 3 条，机制照旧）
严重度：低——不丢数据，但**从札记抄引用会引错论文**，且告警会在每轮 finalize 里复读
发现者：xiaolibird / Claude（2026-09-03 付费墙三篇补入会话）

---

## 现象

每次 `finalize` / `notes_index` 收尾都打同一批告警（`src/scholar/notes_index.py:835`）：

```
⚠️ 3 条 duplicate 条目的行内 citekey 与其 keeper 不一致（从该月札记抄引用会引错论文，
   须人工把 md/sidecar 改成 keeper 键）：
     2026-08 的 ammon2026Comparison（应为 keeper 的 patel2026Comparison，见 科研札记_2026-08_全文精读.md:91）
     2026-08 的 kang2026Fracture（应为 keeper 的 cowan2026Fracture，见 …:308）
     2026-08 的 anon2026Translating（应为 keeper 的 jiang2026Translating，见 …:587）
```

危害是实的：`all_references.json` 只收 keeper，所以从该月 md 里复制
`[@ammon2026Comparison]` 写进稿子，pandoc 会报 undefined citation 或静默丢引用。

## 现状是有意为之，但缺一个安全出口

`notes_index.py:830` 的注释写明了理由：

```python
# 只告警不自动改写：md 回写有 RENAME_PARTIAL 半改风险，留给人工。
```

顾虑成立（半改比不改更糟）。问题在于**「留给人工」没有配套工具**：人得自己找出
要改哪几个文件。这次我手工改的是**三处派生物**，缺一处就会半改：

| 文件 | 该月 md 里的位置 |
|---|---|
| `科研札记_<月>_全文精读.md` | 小节标题里的 `[@key]` |
| `科研札记_<月>_全文精读.index.json` | sidecar 的 `citekey` 字段 |
| `科研札记_<月>_全文精读.references.json` | CSL 条目的 `id` |

（本次核对过：向量库 `embeddings.sqlite3` 的 chunks 与 ScholarVault 里**没有**这三个
旧键——duplicate 条目本来就不进向量库，所以派生物只有上面三处。这个结论值得写进实现，
免得下次又把范围想大。改完重跑 `notes_index.py` 即可，2491 篇 / 撞键 0 组。）

## 建议修法

给 `notes_index.py` 加一个 `--fix-inline-citekeys`（对齐已有的 `--fix-collisions`）：

- 只改上面三个文件，**三处要么全改要么全不改**（先写临时文件再原子替换，规避
  RENAME_PARTIAL 顾虑）；
- 只处理「duplicate 且 keeper 有 DOI 而自己没有」这类**判据明确**的情形——本次三对
  都是标题完全相同、duplicate 侧无 DOI（title 兜底键、作者名还是 Scholar 邮件解析的
  截断产物如 `Miller…`/`Jeyakumar…`）、keeper 侧有 DOI；
- 默认 `--dry-run` 打印将改的行号，确认后再落盘。

若不想加自动修，退一步也行：**把告警文案里的「须人工把 md/sidecar 改成 keeper 键」
补成可复制的三行命令**（含 references.json，这一处最容易漏）。

## 与 `manual-upgrade-citekey-suffix` 的分工（同一行告警，两种成因）

`notes_index.py:835` 这一条告警会被**两种不同的缺陷**触发，别混为一谈：

| | 本条 | [`2026-09-04-manual-upgrade-citekey-suffix.md`](2026-09-04-manual-upgrade-citekey-suffix.md) |
|---|---|---|
| duplicate 侧的键 | **错的作者兜底键**（无 DOI、标题兜底、作者名是 Scholar 邮件解析的截断产物） | **干净的基键**（正常的 auto 浅读条目） |
| keeper 侧的键 | 正确的键（有 DOI） | **带 `b` 后缀的键**（因为 citekey 在去重之前分配） |
| 危害 | 从该月 md 抄引用会引到一个**全局书目里不存在**的键 | 干净基键从全局书目里**消失**，此前所有 `[@基键]` 引用悬空（已波及主稿 v10） |
| 本轮实例 | `ammon→patel` / `kang→cowan` / `anon→jiang`（已手工改） | `shi2025Federated→b`（已处置）、`lin2025Addressing→b`（未处置） |

即：那条要修的是**分配顺序**（citekey 该在去重之后定），本条要修的是**善后工具**
（无论成因，行内键与 keeper 不一致时，人得有个安全的三处齐改的通道）。两条都修完，
这行告警才会真正清零；只修一条的话另一种成因仍会复读。

本轮我遇到的 `lin2025Addressing` 那例属于**那一条**（keeper 拿了 `lin2025Addressingb`
后缀键），已由那份文档记录与追踪，此处不重复。

---

## 修复（2026-09-04 台账批）

- **善后工具**：`scripts/notes_index.py --fix-inline-citekeys [--apply]`（核心 `notes_index.fix_inline_citekeys`）。默认 dry-run 打印计划；
  两种形态分别处置（判据 `find_stale_inline_citekeys` / `_is_suffix_key`）：
  - `stale-dup`（本条的三对：错的兜底键）：改 **duplicate 所在月** md / `.references.json` / `.index.json` 凡存在者，`[@dup 键]` → keeper 键；
  - `suffix-keeper`（`manual-upgrade-citekey-suffix` 那条）：改 **keeper 所在月** `<基键>b` → `<基键>`（与 write_notes 新的继承规范态一致），
    改前检查基键没被另一篇 live 条目占用，改后 `announce_rekey_side_effects` 告知派生物并 best-effort 同步向量库。
  三处齐改走既有 `_rename_citekey_in_note`（先全量预检、再全量写盘、失败逆序回滚；RENAME_PARTIAL 单列），43 个 auto 月没有 sidecar 时只改存在的两处。
  apply 后自动重建索引并报告「重建后仍不一致 N 条」。
- **告警升级**：`_global_pass` 那条从 `warning` 升 `error`（同 `_reuse_citekeys` 先例：warning 在自动权限
  的 agent 会话里看不见），文案带形态标注、补上 `.references.json`，清单同时挂到索引的
  `stale_inline_citekeys` 供回执读。**刻意不 notify**：这条描述的是「库里存在什么」这种**持久状态**，
  不是「本次发生了什么」——每周每月弹同一条会把告警面训练成噪音（`notes_index.py:852` 有同款注释，
  `test_bugs_batch_2026_09.py:338` 钉死「不在这里 notify」）
  与「43 个 auto 月没有 sidecar」，并给出上面的命令。
- 本条关于「派生物只有三处」的结论写进了 `fix_inline_citekeys` docstring。
- 测试：`test/test_bugs_batch_2026_09.py` W8-B 节（两形态 dry-run/apply 端到端、基键被占拒绝、无事可做、告警文案与 notify）。
