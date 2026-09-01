# translation-server 停机整周的补救与告警（2026-09-01）

## 起因

用户问「Awounvo 那篇入库了吗」→ 顺带发现 Zotero translation-server（docker `zotero-translation-server`，:1969）
自 08-25 起不在线：`docker inspect` FinishedAt=08-24T22:09Z（本地 08-25 06:09）、StartedAt=09-01T00:41Z。
期间周一 09:30 weekly-ingest 入库的论文全部静默跳过权威元数据解析，无任何告警。

## 根因（对抗审核 B 查出，改动没碰）

`last reboot`：08-25 06:12 关机 / 06:15 重启。docker 运行时是 **OrbStack**，不在登录项、不随登录启动；
容器 `restart=unless-stopped` 对「守护进程没起」无效。**下次任何一次重启会原样复现。**
治本是把 OrbStack 加进登录项（或在两个 plist 前加 `orb start && docker start …` wrapper）——留给用户决定。

另外两个此前不知道的事实：
- manual 手动精读（read_pdf/pdf_ingest）**从未**接 translation-server，只有 auto ingest 与 digest --zotero 走它；
- 日志里**本来就有** WARNING（08-31 有 12 条 `Errno 47`）和汇总行 `增强 CR22/AX1/TS0`——缺的不是日志，
  是没人看 `cron_ingest.err.log`、没有系统通知。

## 做了什么

### 1. 存量重对齐：`scripts/realign_metadata_ts.py`

对 8 月 295 条（auto+manual，book 不含）用 TS 重对齐 authors/journal/volume/issue/pages/issued，
三处同步（md 元信息行 / sidecar / references.json）+ manual bundle 回写；不碰 title/DOI/arxiv_id/citekey。
两轮 --apply：33 + 14 条变更，备份 `output/scholar_notes/_archive/realign_ts_20260901-{0852,0958}/`。
审核 C 用备份逐字段比对：只动目标字段、33 条三处一致；两处存疑值（xu2026Large 末位作者、
helms2026 页码 `JMJ26-0050-R`→`360-367`）与 Crossref 核对都是 TS 对、原值错。

按审核修掉的脚本问题：
- TS 对 arXiv 返回**被请求版本**的日期（v4 → 2026-08），第一轮把 anonTabimpute（2510.02625）首发写成 2026-08
  → 改为按号段 YYMM 取首发月、年份不符时强制改；第二轮已纠回 2025-10。
- Zenodo DOI 回 `itemType=computerProgram`、creators 是代码仓署名 → itemType 守卫整条跳过。
- 作者守卫加「新旧姓氏集合无交集拒绝」；arXiv 号解析到正刊（回非 10.48550 DOI）时不写刊名卷期页与 issued，
  记 `doi_candidate`（hu2024Personalizedb→IJCAI、fan2026Interdisciplinary→FnT SP）留人判。
- issued 变更同步 sidecar.year（索引/向量库/vault 的 year 都读它）；只改卷期页时不重写 sidecar；保持 JSON 尾换行。
- 「重跑 dry-run 0 变更」不是完成判据：TS 命中集合随时间漂移（并发 501 = 上游限流，串行重试全回；
  arXiv 新号 export API 滞后几天）。报告加 `unresolved[]`，完成 = 变更 0 且 unresolved 空。
- --apply 收尾自己 `update_index(since=until=前缀)`：增量模式只认 md mtime，只改 sidecar/CSL 的文件不会被重扫。

终态：变更 0 / 命中 213/215 / 未解析 2（拼接坏 DOI `10.1177/2515…10.1177/2515…`、arXiv:2608.16273）。

### 2. 停机告警：`translation_server.resolve_batch`

ingest.enrich_segments 与 zotero_sync 共用一个入口：
- 探活 `is_available` 改**空 body**（0.04s 零出网，server 路由层回 400；此前假 DOI 探活 0.63s 真出网）；
  **只有 ConnectError/ConnectTimeout 算离线**，ReadTimeout 等一律按在线放行让逐篇 30s 超时兜底
  （审核 A：否则容器忙时整批被判离线还误报）。
- 离线 → warning + 系统通知；在线但「有标识符 0 命中」→ warning + 通知（审核 B：出网断/限流/翻译器坏探活看不出）。
- 通知进程内按 (url, 事由) 只弹一次（审核 A：多月 backfill 默认 41 个月会弹 41 条）。
- config `PROCESSING__ZOTERO_TRANSLATION_SERVER_URL` 改 `127.0.0.1`（localhost 先试 ::1，离线时浮出的是
  误导性的 Errno 47 而非 refused）。

## 三轮压测

- **R1 launchd 层**（照 launchd_stress_test_2026-08-31.md 造一次性测试车，EnvironmentVariables 逐字复制
  weekly-ingest 的；harness 零写盘、静音 Crossref/arXiv/PubMed、把 osascript 返回码写进日志）：
  | 车次 | 结果 |
  |---|---|
  | A 容器在线 | exit 0；命中 1/2；无告警；osascript 0 次；env http_proxy=None/USER 有值 |
  | B `docker stop` | exit 0；1 条「不在线」warning；**osascript rc=0**；0.11s 早退；无逐篇解析；`docker start` 后探活 400 恢复 |
  | C 在线但标识符不存在 | exit 0；1 条「在线但 0 命中」warning；osascript rc=0 |
  判定 grep 别用「权威解析」——离线 warning 文案里也含「跳过权威解析」，会虚报一条。
- **R2 CLI 层**：realign dry-run 幂等（0 变更）；`notes_index.py --since 2026-08 --until 2026-08` 重解析 14；
  增量 `notes_index.py` 重解析 0；`sync_vault.py` 655 篇无变化无提交；`ingest_notes.py --list` exit 0。
- **R3**：pytest 1883 passed / 2 skipped / 0 failed（08-31 基线 1867 + 本次新增 16）。
  独立对抗复核终裁：见文末。

## doi_candidate 的处置（用户裁定「升级」）

- `hu2024Personalizedb` → `10.24963/ijcai.2024/649` 是 **arXiv 翻译器误配**：TS 按该 DOI 回的作者是
  Lorello/Lippi/Melacci，另一篇论文。realign 的 doi_candidate 现在附「作者对得上 / 疑似误配」标记，
  误配时连作者也不写。**教训：arXiv 号解析到的正刊 DOI 不能不核对作者就用。**
- `fan2026Interdisciplinary` → `10.1108/FTSIG-11-2025-0139` 核对无误，用新脚本 `scripts/promote_identity_doi.py`
  升级：sidecar / CSL（type article-journal、issued 2026-05-06）/ md DOI 行 / bundle / **abstracts.json 改键**
  （摘要缓存按 dedup_key 存，不改键向量库同步会把该篇 ab: 级向量当孤儿删掉）五层 + 索引强扫；
  dedup_key `arxiv:2511.01196v3` → `doi:10.1108/ftsig-11-2025-0139`，citekey 不动。脚本自带作者闸
  （TS 首作者姓必须在现有作者表里），hu2024 实测被拒。备份 `_archive/promote_doi_20260901-1014/`。

## 留给用户

- OrbStack 登录项（治本）：`orb config show` 里 `app.start_at_login: false`。容器 `restart=unless-stopped`，
  只要 docker 引擎起来就会自动拉起它（09-01 08:41 打开 OrbStack 时容器随即自启就是证据），所以开
  `orb config set app.start_at_login true` 一项即可；别用 macOS 登录项加 OrbStack.app（那是 GUI，引擎由它带起，
  等价但绕）。注意 `unless-stopped` 记「手动停过」：压测 B 车 `docker stop` 后已 `docker start` 归位。
- `anonJolt`/`anonTabimpute` 有作者了但 citekey 仍是 anon*。
- `rendered/` 下 docx 不跟着更新，下次 finalize 才重出。
- 通知是横幅约 5s 消失；Focus 若打开 09:00–17:00 定时会静默——第二告警面（周札记头部写一行）未做。

## R3 独立对抗复核终裁

**可提交。** 复核员用影子树（`git show HEAD:` 覆盖四个文件）验证新测试在旧代码下 12 failed / 168 passed
（三条否定式用例对 ingest 改动无分辨力，作回归守卫保留）；逐条核实 A/B/C 三轮发现全部「已解决」。
不阻塞但顺手收掉的：zotero_sync 也改按位置作键（同批重复 paper_id 会折叠一篇）+ 补两条 digest --zotero 接线用例
（在线用权威 item / 离线告警一次且不挡写库）；0 命中告警文案改「探活通过但」（URL 拼错时非连接层异常按在线放行，
这是它唯一的告警出口，不能把锅甩给上游）；手工补 anonJolt year 时写坏的 sidecar 缩进（1→2 空格）恢复。
已知可接受的权衡：zero-hit 去重键在 41 月长进程里会掩盖后发的真断网（warning 每批照记）；arXiv 条目
「同年异月」不纠（存量扫过，仅 doi_candidate 与 unresolved 两条不符，均已记）；作者守卫对「等长全换但一个常见姓相同」
不拦（只在 DOI 本身错配时才有意义）。终态 pytest **1885 passed / 2 skipped / 0 failed**。
