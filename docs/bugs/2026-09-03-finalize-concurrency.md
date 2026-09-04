# finalize 整月重建的并发缺口：盘上有 bundle 的论文会被静默抹出札记

日期：2026-09-03
状态：**已修复**（2026-09-04，见文末「修复」一节）。诊断部分原样保留——它论证了几条
「看起来对、实测不成立」的修法，是这次改动为什么长这样的权威出处
严重度：中——**数据不丢**（bundle 是真值源，始终在盘上），丢的是派生视图，重跑 `finalize` 即可复原
发现者：xiaolibird / Claude（2026-09-03 精读入库会话）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python3.12 scripts/read_pdf.py finalize <bundle.json>`（两个会话同时跑） |
| 具体位置 | `scripts/read_pdf.py:408` —— `if (broken or skipped) and not allow_removals:` |
| 报错情况 | **无任何报错**。`refused=None`，退出码 0，回执是绿的；论文被静默抹出 md / references / sidecar / 全局索引 |
| 数据是否丢失 | **否**。bundle 仍在 `output/scholar_notes/manual/<月>/`，重跑 `finalize` 即复原 |
| 复现测试 | `test/test_read_pdf_cli.py::test_rebuild_month_must_not_drop_bundle_still_on_disk`（`xfail(strict=True)`） |
| 修法要点 | flock 与「路径集 + mtime 签名重查」**两者都要**，防的不是同一件事（见文末） |

---

## 起因

2026-09-03 一次手动精读归档结束后复查，发现当月手动精读篇数从我这一轮写完的 **123 篇变成 126 篇**，
而 md 的 mtime（09:27）与向量库（11:48）都在我的会话之外被更新过，本机无我发起的进程残留。
即：**同一时段有另一个会话也在对同月跑 finalize**。本轮三篇未受损，但暴露出一个问题——
`_rebuild_month` 是**整月整篇重写**，两个会话同时跑会怎样？

---

## 结论（先说结果）

| 问题 | 实测结论 |
|---|---|
| 并发会不会丢精读数据？ | **不会**。bundle（`manual/<月>/<paper_id>.paper.json`）是真值源，各写各的文件，无竞争 |
| 并发会不会丢派生视图？ | **会**。md / references / sidecar / 全局索引里那篇会被静默删掉 |
| 净删除守卫拦得住吗？ | **拦不住**，`refused=None`，完全不触发 |
| 能自愈吗？ | 能。bundle 还在盘上，重跑一次 `finalize` 就复原 |

---

## 根因

`scripts/read_pdf.py:_rebuild_month` 的净删除止损闸只在**确实有 bundle 被拒收**时才检查：

```python
# scripts/read_pdf.py:408
if (broken or skipped) and not allow_removals:
```

并发场景下，B 会话新写的 bundle 在 A 会话眼里**既不是 `broken` 也不是 `skipped`**——
它只是没被 A 那次 `mdir.glob()` 列进来。于是守卫整个分支被跳过，A 用陈旧列表把
md 重写成不含那篇的版本。

### 与「人主动删 bundle」为何无法靠现有判据分辨

已有测试 `test_rebuild_month_does_not_refuse_on_clean_removal`（test/test_read_pdf_cli.py:707）
钉死了一条相反的语义：**没有拒收时条目变少，是人主动删了 bundle，不该拦**。

而这两种场景在 `segments` 列表上**完全同形**（都少一篇）：

| 场景 | segments | bundle 是否在盘上 | 期望 |
|---|---|---|---|
| 人主动删 bundle | 少一篇 | ❌ 不在 | 放行 |
| 并发陈旧列表 | 少一篇 | ✅ **在** | 应拦截 |

所以**「把守卫放宽成无条件比对」是错的**——本次排查中先提出过这个方案，随即被实验证伪：
它会连带打破上面那条既有测试的语义。**文件是否仍在盘上**才是唯一能分辨两者的判据。

---

## 复现证据

复现脚本全程零接触真库（临时目录），实测输出：

```
[基线]        papers=2 | md 含 A=True  B=True
[A 陈旧重建]  refused=None papers=1 | md 含 A=True  B=False
             b 的 bundle 仍在盘上=True
             全局索引条目: ['Paper A Archived']
```

已固化为测试：`test_rebuild_month_must_not_drop_bundle_still_on_disk`
（当时是 `xfail(strict=True)`；2026-09-04 修复后它真通过了，strict 转 xpass 报错，
按预设摘掉标记——那个"提醒回来删标记"的机制如期生效）。

---

## 爆炸半径：不止于当月

`_rebuild_month` 收尾会调 `update_index(notes_dir)` + `write_outputs()`，
而 `update_index` 扫的是**全部月份**，重建 `literature_index.json` / `INDEX.md` /
`AGENTS.md` / `all_references.json`。

因此**两个会话即使精读的是不同月份，全局索引仍会对撞**——按月加锁不够，得锁到索引层。

已固化为测试：`test_rebuild_month_rewrites_global_index_across_all_months`（通过）。

---

## 为什么不能改成「按论文 id append / 单篇 upsert」

排查中提过这个方向，实测不成立。月度 md 的小节标题形如：

```
## 🔴 高 3. When Attention Fails: … [@yadav2024When]
```

标题里嵌了**全月排名序号**与**优先级档位**，而档位由 `_priority_tier(rank, total)` 计算
（`src/scholar/_citekey_utils.py`），**依赖全月总数 `total`**。新增一篇会改变 `total`
与其后所有篇的序号，故单篇 upsert 无法就地完成——除非先把序号与档位从标题里摘掉。

另有两条结构性约束：

1. **幂等性会被破坏**。现在重跑 finalize 是幂等的（`write_outputs` 内容未变就不落盘），
   naive append 之后重跑会重复追加。
2. **全局索引 append 不了**。`literature_index.json` 是全库聚合——并查集成簇、
   IDF 加权标题相似度合并，天然是全量计算；且其数据源是**按月**的 sidecar
   （`科研札记_YYYY-MM_手动精读.index.json`），月级粒度烙在架构里。

已固化为测试：`test_adding_a_paper_renumbers_existing_headings`（通过）。

---

## 候选修法（**两条都已实施**，下面是当初的实测依据）

两者防的**不是同一件事**，单独任何一个都有缺口：

### A. `fcntl.flock` 排他锁

套在 finalize / regen / `update_index` 外层，让两个重建进程串行。
仓库内已有现成模式可抄：`src/scholar/embed_store.py:456`（向量库同步用的
`LOCK_EX | LOCK_NB`，注释已说明「flock 随进程退出自动释放，不存在需要手动清理的残留」）。

**防**：两个 finalize 进程对撞（含跨月的全局索引对撞）。
**不防**：写 bundle 的一方根本不持锁——本工作流里 bundle 是 **agent 用 Write 工具直接写的**。

### B. 提交前重查（乐观并发控制）

开工时记下 bundle 列表，落盘前重新 glob 比对，有变化就中止。实测判据有效性：

```
场景1 并发陈旧（文件在盘上、没进列表） → 中止（判为并发）      ✅
场景2 人工删除（文件已从盘上移走）     → 放行（判为人工删除）  ✅
```

**但只比路径集不够**——若对方不是新增文件，而是把一份 draft 翻成 final
（正是本工作流的常态：ingest 先落 draft，agent 后写 final），路径集合根本没变：

```
只比路径集      → 差集=[]                    → ❌ 漏掉
路径集 + mtime  → 变化=['pb1.paper.json']    → ✅ 中止
```

故判据须是**路径集 + mtime 签名**。

### 不推荐：把精读产物 SQLite 化

存储层**已经是**一篇一文件的真值源，数据本来就没丢。付出重写索引构建
与全部下游消费方（notes_search / scholar-write / vault / topics）的代价，
换的只是「少一次重跑 finalize」，不划算。

---

## 相关测试

全部在 `test/test_read_pdf_cli.py` 尾部「R4」小节：

| 测试 | 状态 | 钉住的事实 |
|---|---|---|
| `test_rebuild_month_must_not_drop_bundle_still_on_disk` | pass（原 xfail(strict)，2026-09-04 修复后摘标记） | 缺陷本身 |
| `test_rebuild_month_rewrites_global_index_across_all_months` | pass | 爆炸半径跨月 → 按月加锁不够 |
| `test_adding_a_paper_renumbers_existing_headings` | pass | 单篇 append 不成立的结构原因 |

配套既有测试（勿一并改动，它们钉的是相反方向的语义）：

- `test_rebuild_month_refuses_net_removal_when_a_bundle_is_rejected`（守卫该拦时要拦）
- `test_rebuild_month_does_not_refuse_on_clean_removal`（人主动删该放行）

---

## 运维提示

- `read_pdf.py` 的 finalize/regen 现在**可以**并发跑：会自动串行 + 自愈重来。
  但若回执打出 ⛔/⚠️（refused/concurrent），照它说的等对方结束后重跑一次 regen。
- **另外六个入口仍不持这把锁**（ingest_notes / backfill_notes / notes_index /
  promote_identity_doi / realign_metadata_ts / book_notes）：它们也重写同一份全局索引。
  后果有限（派生物 + 原子写，最坏"陈旧但完整"、下一轮自愈），但与它们并行跑
  finalize 时全局索引仍可能对撞。根治要把锁上提到 `notes_index.write_outputs` 一侧。
- 若发现某月篇数莫名变少：**别慌，数据没丢**。确认 bundle 仍在
  `output/scholar_notes/manual/<月>/` 下，重跑一次 `finalize` 即复原。
- 复原后记得补跑向量库同步：
  `PYTHONPATH=. python3.12 scripts/notes_embed.py`

---

## 修复（2026-09-04）

诊断里的 A（锁）+ B（提交前重查）两条**都实施了**，因为它们防的不是同一件事。
实现在 `scripts/read_pdf.py`，测试在 `test/test_read_pdf_cli.py` 的 R5/R6/R7 三节。

### 机制

| 部件 | 作用 |
|---|---|
| `_RebuildLock` | flock 排他锁，**跨月共用一把**（收尾的 update_index 扫全部月份）。阻塞等待上限 180s，超时 → `refused/reason=locked`。打不开锁文件/非普通文件 → fail-open 且回执打 `unlocked` |
| `_bundle_inventory` | `{文件名:(mtime_ns,size)}` 指纹，**用 os.scandir 独立重读**，不复用采集那次 glob——重查的意义就是重新问一次磁盘 |
| `_collect_month_segments` | 采集，额外返回 `consumed`＝本轮**实际处理过**的文件指纹（stat 取在 load **之前**） |
| 重试提交循环 | 最多 3 轮「采集 → 重查(一) → 净删除闸 → 写盘 → 重查(二)」，轮间小退避 |

判据是「**我处理过的清单** vs **提交前独立重读的磁盘现状**」，不是"两次目录列表比对"——
后者对"列表本身永久陈旧"完全隐形。

### 关键取舍（每一条都有测试钉住，别顺手改回去）

1. **重查排在净删除闸之前**：反过来的话，另一会话原地改写 bundle 时本轮读到半截 JSON
   → 记成 broken → 闸开火直接 return，一轮都不重试，回执还教人「去修一份根本没坏的 JSON」。
2. **已写过盘之后闸再触发，不 return refused**：那一轮写的是完整数据，回滚反而丢东西；
   更不能谎称"整月一字未动"（refused 分支不刷 write_outputs，会让月度 md 与全局索引持久不一致）。
3. **非普通文件（同名目录/悬空软链）必须记进 broken**：只 `continue` 的话它会从
   consumed、disk、broken 三处同时消失 → 闸失明 → 已归档论文在绿回执 + exit 0 下被抹掉。
   但**不进 consumed**，否则与重查侧的 scandir+is_file 口径不齐 → 指纹恒不等 → 该月永久卡死。
4. **只有通过了重查(一)的那一轮，其 broken/skipped 才进回执**；未证实的单独走
   `unverified_broken` 并明说"先别改"。
5. **两个 best-effort 同步（向量库、topics）在锁外做**：topics 是 timeout 2400s 的子进程，
   等锁上限只有 180s，放锁内一次慢合成能让全库 finalize 撞 refused/locked 半小时。
6. **锁外同步前重比全局索引指纹**：锁一放开对方就可能已经刷新了索引，拿陈旧 idx 去
   sync_store 会把对方刚嵌入的 chunk 当"多出来的"删掉（向量库的 0.5 骤缩闸只少一篇时拦不住）。
7. `_RebuildLock` 的 timeout **在调用时读模块常量**，不写成默认参数（默认参数在函数定义时
   求值，之后 monkeypatch 无效——测试会真等 180 秒）。

### 残余窗口（诚实声明）

- 乐观并发控制：`write_notes` 之后仍有极短窗口，其间到达的 bundle 要等下一轮 regen 收敛
  （已有 `concurrent` 标记 + 非 0 退出码提醒重跑）。
- 写 bundle 的 agent 用 Write 工具直写，**不持锁**——锁只让两个重建进程串行。
- 读者（notes_search / scholar-write / vault / 备份）都不持锁，仍可能采到
  「月度 sidecar 已更新、全局索引还没」的瞬时中间态（压测采样约 3%，自愈）。
- 人主动删 bundle 后重建成功，回执是绿的、退出码 0——"一篇论文离开了札记"这件事
  在回执上是哑的（bundle 是真值源，不丢数据）。
- 锁外同步的指纹守卫只管**同步开始前那一刻**。同步跑着时对方刷新索引这后半段，靠的是
  `sync_store` 自己的 `BEGIN IMMEDIATE` 快照复核（压测实测：撞上时当场放弃，`deleted` 恒 0，
  库仍完整）。**两道叠起来这条路才是封死的**，别以为只有指纹守卫。
- 净删除闸开火时整月是**冻结**的：那个月的新论文也进不了库，直到坏 bundle 被修好。
- 等锁的实际最短等待是 0.5s（`LOCK_NB` 失败后固定 sleep 再复检 deadline），把
  `_REBUILD_LOCK_TIMEOUT` 调到亚秒级不是可信旋钮。

### 验证

- 3 轮无记忆 subagent 对抗审计 + 压力测试（每轮各一名，全新上下文）；两轮各抓到一个
  **由上一轮修复引入的回归**（闸挪进重试循环否定已写的盘；`is_file()` 过滤让闸失明），
  均已修并补测试。
- 变异测试：修复涉及的判据逐一削弱。**三轮里每一轮都有存活项**（= 改对了但没人守），
  存活的都当场补了测试再复验：R1 存活 5（M1/M4b/M6/M7/M12）、R2 存活 4（锁作用域、
  written_papers、unstable 复位、未证实 broken）、R3 存活 6（早退位置、papers+md 保留、
  FIFO 守卫、unlocked 标记、锁 timeout 默认参数、退避）。补测后各自复验全杀，唯二
  **有意不覆盖**的是「锁 timeout 不写默认参数」（削弱它测试仍绿、只是套件从 3s 变 182s，
  靠墙钟发现）与「重试退避」（纯时序启发式，削弱后更快且无行为差）。
- 真并发压测：多进程同月/跨月、写入风暴、kill -9、200 轮长跑、锁文件异常、
  文件系统边界（mtime 分辨率实测 APFS 28µs、NFC/NFD、emoji 文件名）、
  120 份 bundle 性能回归（+0.84%）。
- 生产库只读演练：14 个月桶 311 份 bundle 全部 parse 成功，采集口径与重查口径一致。

### 同形缺陷（**未修，另记**）

`src/scholar/book_notes.py::rebuild_book` 与 `_rebuild_month` 同形：读一份章 bundle 列表 →
整本重写 → 刷同一份全局索引，**既无锁也无重查**。触发概率低（书籍链路手动、单会话、
一次一本），但缺陷类别完全相同。要么复用这把锁，要么在函数头写明不做并发防护的理由。
