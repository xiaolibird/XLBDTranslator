# 父进程被杀后 build_topics 子进程变孤儿：概念页在无人知情的情况下继续重写

日期：2026-09-04
状态：**已修复（2026-09-04）**——与 `2026-09-03-finalize-topics-mistaken-for-hang.md` 同一处改动，细节见那份文末「修复」节。原状态：未修
严重度：中——不丢数据，但**中断后无法判断概念页是否已更新**，且会诱发「以为失败→重跑」的并发写
发现者：xiaolibird / Claude（2026-09-03 付费墙三篇补入会话）
关联：与 [`2026-09-03-finalize-topics-mistaken-for-hang.md`](2026-09-03-finalize-topics-mistaken-for-hang.md) 是**同一段代码的两个侧面**——那条问「父进程为什么静默挂住、人为什么会误杀」，本条问「杀完之后子进程怎么了」。现场证据已互相合入，修法建议也在那边合并列出，**一起处理**。

---

## 现象（实测时间线）

```
09:27:53  finalize 开始重建 2026-08 桶（126 篇）
09:28:01  向量库同步完成：+168 嵌入        ← 日志到此为止
09:35 ~   概念页被逐页重写（mtime 可见）
09:42     父进程 read_pdf.py finalize 被 SIGTERM，退出码 144，回执一个字都没打印
09:50 ~ 11:45   概念页仍在被重写，最后一页与 INDEX.md 落在 11:45
```

父进程死后**近两小时**，`scripts/build_topics.py` 子进程还活着并继续写
`output/scholar_notes/topics/*.md`；`ps` 里能看到它，但终端、日志、回执里都没有任何痕迹。

（我这边的 SIGTERM 来自会话后台任务的 10 分钟超时。但**任何**中断——Ctrl+C、
launchd 超时、终端关闭——都会走到同一状态。）

## 根因

`src/scholar/topics.py:1455`（`trigger_topic_refresh` 内）：

```python
proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                      cwd=str(repo_path(".")))
```

两个性质叠加：

1. **子进程不随父进程终止**。`subprocess.run` 不建进程组、父进程也没装信号转发，
   父被 SIGTERM 后子进程被 init 收养继续跑（默认 `timeout=2400`，即最长 40 分钟；
   实测这次跑了约 2 小时 18 分——超时计时在父进程里，父死了就没人计时了）。
2. **输出全部积在管道里**。`capture_output=True` 决定了子进程的 stdout 要等
   `subprocess.run` 返回后才被逐行写进日志（`topics.py:1467`）。父进程先死 →
   缓冲区连同「哪几页重建了、哪几页失败」一起蒸发。

调用点：`scripts/read_pdf.py:229 _sync_topics_best_effort`（finalize 收尾，锁外，
见 `read_pdf.py:759` 的注释——放锁外是**对的**，别改回锁内）。

## 影响

- **可观测性**：退出码 144 + 日志断在向量库那一行，看起来像「finalize 挂了」。
  实际归档早在 09:28 完成、概念页也在慢慢长——但这两件事在输出上都不可见。
  （`docs/skills/read-paper` 里已教人「看 md 的 mtime 判断是否归档完成」，
  但没覆盖概念页这条尾巴。）
- **诱发并发**：按上面的观感，人的自然反应是重跑 finalize。这会让新的
  build_topics 与仍在跑的孤儿子进程**并发写同一批 topics 页**。本次实测确实
  同时存在两个 `build_topics.py` 进程（我的孤儿 + 另一会话的），双份烧订阅额度。
  概念页没有 `2026-09-03-finalize-concurrency.md` 那把锁的保护。

## 建议修法（按性价比排序）

1. **发子进程前打一行回执**：`logger.info("已发起概念页子进程 pid=%s，日志见 …")`。
   一行代码就让中断后可追踪、可 kill。
2. **让子进程随父进程死**：`subprocess.Popen(..., start_new_session=False)` 配合
   父进程的 SIGTERM handler 转发信号；或用 `preexec_fn`/进程组 + `os.killpg`。
   注意别破坏「best-effort、失败不影响 finalize 退出码」的现有语义。
3. **流式落日志**：把 `capture_output=True` 换成边读边写（子进程输出直接
   `logger.info`），中断也保留已完成部分。代价是 `summarize_build_topics_run`
   需要改成吃增量文本。

## 与已修的并发缺口的关系

`docs/bugs/2026-09-03-finalize-concurrency.md` 给 finalize/regen 上了 flock，
并明确把 topics 同步放在锁外（`read_pdf.py:759`：topics 子进程 timeout 2400s，
等锁上限只有 180s，放锁内会让全库 finalize 撞 refused/locked）。
本条不是要推翻那个取舍，而是补上它留下的口子：**锁外的长尾进程需要自己的可观测性**。

---

## 修复（2026-09-04 台账批）

三条建议修法的落地情况：① 发子进程前打 pid 行——已做；② 子进程随父进程死——已做（新会话进程组 + 信号转发 + 超时 killpg，`kill -9` 除外）；
③ 流式落日志——以「stdout 直接落文件 + 结束后回灌」替代（父死也留痕，`summarize_build_topics_run` 不必改成吃增量）。
实现与测试见 `src/scholar/topics.py::_run_refresh_child` 与 `test/test_topics.py` 末节。
