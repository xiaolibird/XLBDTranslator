# finalize「僵死」的候选根因：卡的不是向量同步，是它之后的 topics 子进程

日期：2026-09-03（写于 2026-09-04）
状态：**已修复（2026-09-04，见文末「修复」）**。原状态：已被独立观测佐证，仍缺一次抓栈——本条是
[`2026-09-04-finalize-vector-sync-hang.md`](2026-09-04-finalize-vector-sync-hang.md)
（「阻塞点未定位」）的补充线索，不是独立缺陷；
[`2026-09-04-topics-subprocess-orphaned-on-parent-kill.md`](2026-09-04-topics-subprocess-orphaned-on-parent-kill.md)
由另一会话独立记录，实测「父进程被 kill 后 `build_topics.py` 子进程仍在写概念页」——
两条合起来基本坐实了「向量同步之后卡的是 topics 子进程」这一支
严重度：中——若成立，则现行处置（kill）会**静默跳过概念页刷新**，而文档把它描述成「空转」
发现者：xiaolibird / Claude（2026-09-03 景观地基精读入库会话）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python3.12 scripts/read_pdf.py finalize <bundle.json>` |
| 具体位置 | `scripts/read_pdf.py:757-777`（`_rebuild_month` 尾部，锁外两个 best-effort 同步）→ `_sync_topics_best_effort`（:229）→ `src/scholar/topics.trigger_topic_refresh`，**子进程、默认 timeout 2400s** |
| 报错情况 | 无报错、无日志。打印「向量库已同步」后进程 0.0% CPU、`S` 态挂住；本轮两次分别挂 3 分 40 秒与约 1 分后被 kill（退出码 144） |
| 影响 | 中断后**无法判断概念页跑没跑**——子进程不随父进程死（见文末「证实」），照跑但输出随父进程蒸发；按现行文档判为「失败→重跑」还会与孤儿并发写同一批页 |
| 复现测试 | 无。取证步骤见下（`pgrep -P`，一条命令即可判定） |

---

## 现场

```
2026-09-03 11:28:36 | INFO | embed_store:sync_store_best_effort:852 -
  向量库已同步：+565 嵌入 / -5 删除 / 30153 元数据刷新
（此后无任何日志）

$ ps -o pid,etime,%cpu,stat -p 51714
  PID ELAPSED  %CPU STAT
51714   03:40   0.0  Ss
```

第二次完全同形：11:35:13 打印同步完成 → 静默 → kill，退出码 144。

## 为什么怀疑是 topics 而不是向量同步

1. **代码路径**：`_rebuild_month` 尾部在向量同步之后**还有一步**：

   ```python
   _sync_embedding_best_effort(notes_dir, idx, settings)
   if out.get("md"):
       _sync_topics_best_effort(notes_dir, out["md"])   # W6：接入 P2
   ```

   同一处的注释自己写明：「topics 合成是子进程、默认 timeout 2400s（40 分钟）」。
   父进程 0% CPU + `S` 态正是**等子进程**的形态；子进程输出不进父 logger，故日志静默。

2. **旁证（关键）**：`output/scholar_notes/topics/adversarial-evidence.md` 于 **11:45**
   被写入，且含 `zhao2024Eprnet` 2 处引用——即概念页合成这一步确实会跑、且会把本轮新论文写进去。

   ⚠️ 更正：本条最初把这次写入解释成「另一会话 11:39 的 finalize 跑了约 6 分钟」。
   按 `2026-09-04-topics-subprocess-orphaned-on-parent-kill.md` 的实测时间线（09:35 起概念页
   被逐页重写、最后一页与 INDEX.md 落在 **11:45**），11:45 这次写入更可能来自 **09:27 那轮
   finalize 被 kill 后遗留的孤儿子进程**，前后跑了约 2 小时 18 分。所以**不能**据此推断
   topics 合成只要 6 分钟——它可以跑很久，这反过来让「父进程静默等待」的时长完全合理。

3. **与台账 #2 的时间线相容**：该调用自 `7d2d45a`（2026-08-17，概念页层）就存在，
   早于 #2 的观测日 2026-08-31。#2 排除的四个嫌疑（嵌入 HTTP 超时、osascript、
   向量库 flock、SQLite 写锁）**全都在向量同步内部**，没有一个覆盖「已经离开向量同步、
   进入 topics」这一支；而 #2 现场「kill 后补跑 notes_embed 报『新嵌 0』」恰恰说明
   同步早已完成——与本条一致。

4. **另一会话的独立实测**（`2026-09-04-topics-subprocess-orphaned-on-parent-kill.md`）：
   父进程 SIGTERM 后 `scripts/build_topics.py` 子进程被 init 收养、继续写
   `topics/*.md` 近两小时。这既证明该子进程确实存在于此位置，也解释了为什么父进程
   在日志静默的同时 0% CPU——`subprocess.run(capture_output=True)` 等的就是它。

**仍缺的那一步**：本轮同样是先 kill 后排查，没抓栈，也没跑 `pgrep -P` 看子进程。
下次复现请先执行：

```bash
PID=$(pgrep -f "read_pdf.py finalize"); pgrep -lP $PID   # 有 build_topics.py 子进程 → 本条坐实
py-spy dump --pid $PID                                    # 顺手把栈抓了，同时结掉「阻塞点未定位」那条
```

## 文档缺陷（无论根因是否成立，这条都要改）

`docs/skills/read-paper/SKILL.md:163-166`：

> ⚠️ **finalize 可能在向量库同步那步僵死不退出**（本机 Ollama 没起来时）……
> 就说明归档已完成，剩下的等待是空转，杀掉即可（退出码 144 = SIGTERM，不是失败）。

三处与实现不符：

1. **归因**：说卡在向量库同步、因 Ollama 没起来。实测向量同步**已成功返回**（日志有
   `+565 嵌入`），卡住的位置在它之后。（#2 也已独立指出「Ollama 正常在跑」，
   该归因至少不完整。）
2. **判据**：文档给的自愈判据是「md 的 mtime 已更新 + 索引能解析」——这两条在 topics
   开始跑之前就满足，所以它**永远**判为「可以杀」。
3. **后果**：只说「剩下的等待是空转」，未提概念页会被跳过；随后建议补跑的也只有
   `notes_embed.py`，没有 topics。

## 影响（本轮实测）

> ⚠️ 本节的原始推论「kill = 概念页不刷新」**已被文末「证实」一节推翻**——子进程活了下来。
> 保留此节是因为它记录了「事后无法判断」这个真正的危害，那部分仍然成立。

本轮四篇里只有 `zhao2024Eprnet` 在 `topics/` 下有命中，
`xu2012Potential` / `lynn2021Broken` / `lavenant2024Mathematical` 零命中。
关键在于：**我当时无法区分**「没被路由中」与「被我杀掉了」——概念页刷新有没有跑过、
跑成什么样，事后没有任何痕迹可查（子进程的 stdout 随父进程一起蒸发）。

事后补跑路由 dry-run，结论是**覆盖缺口，不是被 kill 掉的**：

```
$ build_topics.py --dry-run --affected-by xu2012Potential --affected-by zhao2024Eprnet \
                  --affected-by lynn2021Broken --affected-by lavenant2024Mathematical
🔎 路由：4 篇新论文，检查哪些概念页受影响……
   adversarial-evidence         ← zhao2024Eprnet
0 页成功 · 1 页跳过 · 0 页失败或冲突
```

四篇里只有 `zhao2024Eprnet` 路由到 `adversarial-evidence`（且已在页内），其余三篇
一页都不进——与文末「顺带的观察」同因：现有 8 页全在 EHR 缺失/插补/MNAR/ICU 一圈，
景观-流形-缺失这条线没有对应页。

但要点不变：**这个结论是事后另跑一条命令才拿到的，正常回执里没有**。中断当时我无法区分
「没被路由中」与「被我杀掉了」，只能靠这次补查才排除。
（顺带：`--affected-by` 是单值重复标志，写成 `--affected-by a b c` 会报
`unrecognized arguments` —— 每个 citekey 都得带一次标志。）

## 备选修法

1. **文档先改**（最低成本）：按上面三条改写 SKILL.md:163-166，明确「打印向量同步完成后
   仍会跑 topics 子进程，最长 40 分钟；中止的代价是本轮概念页不刷新」。
2. **进度可见**：`_sync_topics_best_effort` 调用前后各打一行 INFO（开始 / 耗时 / 结果）。
   这条是根治误判的关键——现在「还在跑」与「真卡死」在日志上完全同形。
3. **超时收紧 + 可关**：2400s 对交互式会话过长；加 `--no-topics` 开关，或把默认降到分钟级、
   超时打 WARNING 而非静默。

---

## 证实（2026-09-03 付费墙三篇补入会话，2026-09-04 补记）

本条的候选根因**成立**，另一场会话拿到了现场。同时它推翻了上面「影响」一节的一个推论：
**kill 掉父进程并不会让概念页不刷新——子进程活下来了，继续写了近两小时。**

### 现场时间线

```
09:27:53  finalize 开始重建 2026-08 桶（126 篇）
09:28:01  向量库已同步：+168 嵌入 / -0 删除 / 29602 元数据刷新   ← 日志到此为止
09:35 起  topics/*.md 被逐页重写（mtime 可见）
09:42     父进程被 SIGTERM（会话后台任务 10 分钟超时），退出码 144，回执一字未打印
09:50–11:45  概念页**仍在**被重写；8 页全部重建完成，INDEX.md 落在 11:45
```

父进程死后 `ps` 仍能看到子进程（这正是上面要求的 `pgrep -P` 那条判据，只是从另一侧取到的）：

```
2942   0.0% .../python3.12 .../scripts/build_topics.py --affecte…
72806  0.0% .../python3.12 .../scripts/build_topics.py --affect…
```

两个同时在跑：`72806` 是本会话 finalize 留下的孤儿，`2942` 是另一会话的。到 14:25 复查时
两个都已自然退出，`build_topics` / `read_pdf` / `notes_embed` / `notes_index` 全部清空。

### 三条结论（按对修法的影响排序）

1. **父进程极可能就是在等 topics 子进程**——0% CPU + `S` 态 + 父死后概念页继续被写，三者互证。
   但证据边界要说清：父子关系是**从启动时刻与命令行推断**的，我**没有**跑
   `pgrep -P <父pid>`，事后父进程已消失、无法补证。所以表头「已被独立观测佐证、仍缺一次抓栈」
   是准确措辞——下次复现仍值得按现场那两条命令抓一次栈，才能同时结掉台账
   `finalize-vector-sync-hang` 里「阻塞点未定位」那条。
2. **子进程不随父进程终止**：`subprocess.run`（`src/scholar/topics.py:1455`）没有建进程组，
   父进程也没装信号转发，父被杀后子进程被 init 收养继续跑。所以「kill 的代价是概念页不刷新」
   这个推论**不成立**——真实代价是**概念页照跑，但没人知道它跑没跑、跑成什么样**。
3. **输出连同父进程一起蒸发**：`capture_output=True` 让子进程 stdout 积在管道里，
   等 `subprocess.run` 返回后才逐行进 logger（`topics.py:1467`）。父进程先死 →
   「哪几页重建了、哪几页失败」全丢。本轮 8 页实际全部重建成功，但这件事在日志里零痕迹。

### 对备选修法的补充

上面列的三条（改文档 / 进度可见 / 超时可关）都仍然成立，另外补两条针对孤儿的：

4. **发子进程前先打一行 pid**：`logger.info("已发起概念页子进程 pid=%s")`。一行代码，
   中断后就能 `kill` 或 `wait`，也能事后对上账。
5. **让子进程随父进程死**（或至少可选）：`Popen` + 进程组 + 父进程 SIGTERM handler
   `os.killpg`。注意别破坏 best-effort 语义（概念页失败不影响 finalize 退出码）。

**为什么 4/5 不只是洁癖**：孤儿存活时，人按现行文档的判断（「回执没出来 = 失败」）会重跑
finalize，于是新的 build_topics 与孤儿**并发写同一批 topics 页**。本轮实测确实同时存在
两个 build_topics 进程，双份烧订阅额度；而 topics 页不受
[`2026-09-03-finalize-concurrency.md`](2026-09-03-finalize-concurrency.md) 那把 flock 保护
（那把锁刻意只管重建、把 topics 放在锁外）。

### 一个顺带的观察（不是缺陷）

本轮补入的三篇（`lin2004Analysis` / `pullenayegum2016Longitudinal` / `ladobaleato2025Testing`）
**一页都没进**。`build_topics.py --dry-run --affected-by <三个键>` 回答得很干净：

```
🔎 路由：3 篇新论文，检查哪些概念页受影响……
✅ 新论文没有进入任何概念页的证据集，也没有页面超期未更新，无需重新合成
```

概念页当轮被重建，是因为路由吃的是整份 `科研札记_2026-08_手动精读.md`（126 篇）里**其他**
论文命中的。现有 8 页全在 EHR 缺失/插补/MNAR/ICU 基准一圈，每页证据位 60–70 条且被更贴题的
材料占满，而「访视强度加权」「双变量参考域」是隔壁课题——**这是覆盖缺口，不是路由缺陷**。
要收这条线得新开一页（如 `observation-process`：就诊/开单过程与信息性观测，
现成材料 `lin2004Analysis` + `pullenayegum2016Longitudinal` + `agniel2018Biases`）。

---

## 修复（2026-09-04 台账批）

与 `2026-09-04-topics-subprocess-orphaned-on-parent-kill.md` / `2026-09-04-finalize-vector-sync-hang.md` 合并处理，改动全在
`src/scholar/topics.py`（`trigger_topic_refresh` + 新增 `_run_refresh_child`），三处调用方（read_pdf / ingest_notes / backfill_notes）零改动：

- **进度可见**（备选修法 2/4）：向量同步之后先打 `🧵 开始概念页刷新（…）：子进程最长 2400s（约 40 分钟）…stdout 实时落 logs/topics_refresh/…`，
  发起后打 `🧵 已发起概念页子进程 pid=N（进程组 N…）`，结束打 `pid=N 结束：退出码 X，耗时 Ys`。「还在跑」与「真卡死」在日志上不再同形。
- **随父进程死**（备选修法 5）：子进程 `start_new_session=True` 自成进程组；父进程等待期间装 SIGTERM/SIGINT/SIGHUP 转发（先 `killpg` 子进程组，
  再按原语义处理该信号——SIG_DFL 对自己重发，SIGINT 照常抛 KeyboardInterrupt），超时同样 `killpg`（此前 `run()` 只 kill 直接子进程，
  build_topics 往下派生的 LLM CLI 会漏）。只有主线程能装 handler，非主线程降级为不转发 + debug 日志。**`kill -9` 拦不住**，文档写明。
- **输出不随父进程蒸发**：子进程 stdout 直接落 `logs/topics_refresh/build_topics_<时间戳>_<父pid>.log`（14 天自动清理），正常结束后整份回灌 logger（W3 契约）。
  放 logs/ 而不放 notes_dir/topics/ 是因为后者被 vault WatchPaths 盯着。
- **超时收紧 / 可关**（备选修法 3）：未做——2400s 保留，改为让人看得见它在跑；`--no-topics` 不加。
- 文档：`docs/skills/read-paper/SKILL.md` finalize 节按本条三点改写（归因、判据、后果），补「中断后怎么补概念页」。
- 测试：`test/test_topics.py` 末节（真子进程：stdout 落文件回灌 / 超时 killpg 连孙进程 / SIGINT 转发后子进程死 + handler 复原 /
  非主线程降级 / pid+耗时两行 / 日志清理）。既有 5+3 个 patch `subprocess.run` 的测试改到新接缝 `_run_refresh_child`。

**仍未做**：现场抓栈。两次现场都已过去、无法补证；但现在只要日志里出现「开始概念页刷新」那一行，就已能判定停在 topics 而非向量同步，抓栈的诊断价值大部分被这行替代。
