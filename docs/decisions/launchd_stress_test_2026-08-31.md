# launchd 定时链路压力测试（2026-08-31）

## 起因

周一 09:00 digest 首跑（F7 修复后首次实跑）暴露 launchd 洁净环境 PATH 无 `/opt/homebrew/bin`，
claude-agent 静默跳过、gemini 兜底（详见 commit 268a1e2）。修完后不该等下周一才知道对不对——
「一周一班车」的功能需要**造一班测试车**。

## 方法：一次性 launchd job + 回填窗口

```
Label            com.xlbd.scholar-digest-stresstest（临时，跑完 bootout）
ProgramArguments 同 digest job 的 python + scholar_main.py digest --since 2026-08-24 --until 2026-08-30
EnvironmentVariables  逐字复制 digest job（no_proxy/NO_PROXY/PATH），不继承 shell env
RunAtLoad        true；无 StartCalendarInterval；日志指到会话 scratchpad
```

要点：
- **必须走 launchd**，不能在 shell 里手跑——要验的正是 launchd 的洁净环境（PATH/USER 等）。
- `--since/--until` 触发回填模式：`cli.py` 自动 `auto_mark_read=False`，Gmail 零写入；
  digest 本身不写札记/索引；Zotero 本机关闭。唯一副作用是多一份带独立 run_id 的 digest 产物。
- 真实负载：本次窗口 29 封邮件 → 8 个 LLM 裁决批次 → 32 篇入选 → 摘要/翻译 7 批。
- 监控 grep 别用裸数字：`402` 会匹配到时间戳毫秒 `.402`（本次真踩，虚报 6 条回退事件）。

## 结果

| 验证点 | 结果 |
|---|---|
| 「claude CLI 未安装/不在 PATH」 | 0 次（当日 09:00 真跑为 2 次） |
| 回退链推进 / 402 | 0 次 |
| claude-agent 实际服役 | `claude CLI ok` 15 次，裁决模型 sonnet / filter-v3 |
| 回填模式关标已读 | 日志明示，Gmail 零写入 |
| 退出码 | 0 |
| 处理 | 32 入选 / 31 成功 / 1 FAILED（翻译 JSON 畸形，抢救层按设计：salvage_batches=1、dropped 0、缺裁决篇标 FAILED 不静默） |

对抗审计另实测：claude CLI 登录态硬依赖 `USER` 变量（按 account 读 login Keychain），
launchd gui 域自动提供、system domain 不提供——已写进三个模板注释。

## 同日三轮功能压测（commit 前门禁）

- **R1 launchd 层**：embed kickstart exit 0（早间的 exit 2 是设计内并发避让，终态 dry-run 待嵌/待删 0）、
  vault kickstart exit 0、weekly-ingest 环境用一次性探针跑 `ingest_notes.py --list` exit 0（识别本周窗口、
  跨库去重正常）。
- **R2 CLI 主功能 19/19**：notes_index 增量（撞键 0/守卫静默）、notes_embed 终态、notes_search 六种
  形态（hybrid 默认重排 6s / dense 不重排且分数单调 / sparse / --cite / --role / 判重契约 dense+0.62
  自命中 score_from=paper）、三种故障路径（离题探针 0.95→exit1、reranker 缓存缺失→降级 exit0、
  Ollama 不可达→exit3 且 sparse 照常）、rag_bench 两臂 summary 与基线全等且 gold rank 零变化
  （auto 75/79/0.8642、off 66/76/0.8162；top-10 尾部因当日新入库 46 篇有正常换人）、notes_query、
  lint_notes、gen_bench、locate_pdf 找回挪动后的 PDF。
  ⚠️ 成本备忘：`lint_notes --offline` 只跳撤稿**不跳对撞**，R2 这一项实际送了 60 对句对给 sonnet
  裁决（11 分钟）并重写 topics/_lint.md；要零成本须加 `--skip-contradictions`。gen_bench 的
  `main()` 恒 return 0，判通过应解析 JSON 看 production_mismatch<0 页数=0（本次 0，数字接地 99.5%）。
- **R3**：pytest 1867 passed / 2 skipped / 0 failed；独立对抗复核：R2 的 9 项弱断言加强后无一被击破，
  终裁可提交。覆盖真空白（未压测、仅 -h/--list/--dry-run 探针通过）：monthly 全跑、build_topics、
  ask_notes/qa、book_digest、read_pdf、entail_audit、translator 主链路、zotero 子命令。
  卫生瑕疵待办：check_models.py / verify_deepread_batch.py 无 argparse（-h 直接跑）；后者基线快照
  已因 citekey regen 过期需刷新。

## 当日定时任务实况

digest 09:00 exit 0（36/36）；weekly-ingest 10:18 exit 0（8 个概念页刷新全 ✅）；vault exit 0；
embed 10:00 exit 2 = 并发避让（压测收尾触发索引变动与 watcher 叠跑，「库没损坏、数据没丢」），
kickstart 复跑 exit 0。

## 复用

下次改动任何 launchd job 的 env/程序/参数，照本文件造一班测试车跑回填窗口，不要等自然触发。
monthly-backfill 与 digest 共用同一代码路径（`--month` 也是回填模式），本压测等价覆盖 LLM 链路；
它的 PATH 修复首个实证是 2026-09-01 21:30 的自然首跑（`科研札记_2026-08_全文精读.md` 尚不存在，
会真跑整月），跑完核对 cron_monthly.err.log 无「未安装」。压测产物已挪至
`output/scholar_digest/_archive/stresstest/`，避免本周手跑 `ingest --auto` 把压测独有入选混入。
