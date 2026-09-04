# finalize 收尾在向量库同步处僵死不退出（无报错、0% CPU）

日期：2026-09-04（观测于 2026-08-31 21:53–22:07 的一次手动精读归档）
状态：**症状已确证，阻塞点未定位**——四条最可能的嫌疑已逐一排除，需下次复现时抓栈。
⚠️ 2026-09-04 补：[`2026-09-03-finalize-topics-mistaken-for-hang.md`](2026-09-03-finalize-topics-mistaken-for-hang.md)
提出了一条**候选根因**——卡的不是向量同步本身，而是它之后的 `topics` 子进程
（`_sync_topics_best_effort`，默认 timeout 2400s）。该线索与本条的全部观测自洽
（「新嵌 0」正说明同步已完成、进程停在其**之后**），两条应合并处理，勿当独立缺陷
严重度：中低——**归档已完成**，只是进程不退；但无人值守链路（launchd / agent 会话）会一直干等

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python3.12 scripts/read_pdf.py finalize <bundle.json>` |
| 具体位置 | finalize 收尾的向量库同步：`src/scholar/embed_store.py:818 sync_store_best_effort` → `sync_store` |
| 报错情况 | **无任何报错、无日志输出**。进程 0.0% CPU 挂住不退，实测 13 分钟；skill 文档另记过一次 28 分钟 |
| 判定已完成的依据 | 四件套与索引 mtime 已更新；kill 后单独跑 `notes_embed.py` 报「期望 29364 条 \| **新嵌 0**」 |
| 处置 | `pkill -f "read_pdf.py finalize"`（退出码 144 = SIGTERM，非失败），再补跑 `scripts/notes_embed.py` |

---

## 现场记录（2026-08-31）

```
产物写盘         21:53   科研札记_2026-08_手动精读.{md,docx,index.json} / literature_index.json
进程状态         22:07   PID 11866  %CPU 0.0  ELAPSED 13:00+  STAT S    ← 睡眠态，不是忙等
embeddings.sqlite3      mtime 停在 21:53，13 分钟内无写入
Ollama                  llama-server 在跑，%CPU 0.3；curl /api/tags 通
kill 后补跑 notes_embed ✅ 同步完成：期望 29364 条 | 新嵌 0（paper 0 + abstract 0 + highlight 0）
```

「新嵌 0」说明**同步的写入其实早已完成**——进程是在同步完成之后、返回之前的某处挂住。

---

## 已排除的嫌疑（逐个查过源码）

| 嫌疑 | 位置 | 排除理由 |
|---|---|---|
| 嵌入 HTTP 调用无超时 | `src/scholar/embeddings.py:84` | `httpx.Client(timeout=120.0)`，有超时 |
| 系统通知 osascript 卡住 | `src/utils/notify.py:44` | `subprocess.run(..., timeout=10)`，有超时 |
| 抢向量库 flock 阻塞 | `src/scholar/embed_store.py:456` | `LOCK_EX \| LOCK_NB` 非阻塞，且**仅 `--full` 模式**才抢；本次是增量 |
| SQLite 写锁等待 | `src/scholar/embed_store.py:670` | `sqlite3.connect(...)` 用默认 `timeout=5.0`，`BEGIN IMMEDIATE` 争不到锁会抛 `database is locked` 而非挂起 |

---

## 与 skill 文档记载不符

`docs/skills/read-paper/SKILL.md` 记载该现象的成因是「本机 Ollama 没起来时」。
本次观测**Ollama 正常在跑**（`llama-server` 进程存活、`/api/tags` 可达），
故该解释至少不完整，skill 里的归因需要修正。

---

## 下次复现时的取证步骤（关键）

本次因先 kill 后排查，栈信息永久丢失。下次遇到**先抓栈再 kill**：

```bash
PID=$(pgrep -f "read_pdf.py finalize")
py-spy dump --pid $PID              # 首选，直接给 Python 栈
# 或
lldb -p $PID -o "bt all" -o "detach" -o "quit"
sample $PID 5 -f /tmp/finalize_hang.txt   # macOS 自带，无需装包
```

抓到栈之后本条目即可从「未定位」转为可修。

---

## 临时规避

- finalize 跑完后若迟迟不退：先确认 `科研札记_<月>_手动精读.md` 的 mtime 已更新、
  `literature_index.json` 能正常解析且含新篇 → **归档部分**已完成。
- ⚠️ **但 kill 不是无代价的**（此前本条写的「kill 即可」不准确，据上述候选根因更正）：
  若真正卡住的是 topics 子进程，kill 掉等于**本轮概念页不刷新**，而回执全绿、
  退出码 144 又被文档解释成「不是失败」——这件事在输出上是哑的。kill 前先查一眼有没有子进程：
  `pgrep -P $(pgrep -f "read_pdf.py finalize")`；有的话按 topics 那条处理，别当空转。
- kill 后补跑增量同步：`PYTHONPATH=. python3.12 scripts/notes_embed.py`
- 用 dense 模式验证新篇可检索（hybrid 会给假阴性）：
  `PYTHONPATH=. python3.12 scripts/notes_search.py "<核心论断>" --mode dense --min-score 0.62 --limit 5`
