# 2026-09-03 工程可靠性收敛：CI / 一致性 / 备份 / 拆分（四阶段）

> 五个 commit：4f22a79 + 6105587（阶段1）、8cb00dd（阶段2）、d200d74（阶段3）、981610c（阶段4）。
> 流程：PRD 先经 **5 轮 × 3 个无记忆 subagent** 对抗审核（15 席，累计 7 BLOCKER / 45+ MAJOR
> 全部吸收后签发）；每阶段完成后再经 **3 轮无记忆 subagent 代码审计 + 压力测试**，
> 全部 CONFIRMED 发现修完才 commit。本文是执行留痕与"为什么长这样"的权威出处。

## 阶段 1：CI 上线（4f22a79, 6105587）

- GitHub Actions：apt 装 weasyprint 67 dlopen 五库 + `import weasyprint` 硬探针
  （模块级 skip 会把 apt 装漏吞成静默绿）+ ruff E9,F63,F7,F82 语法门（专兜 scripts/
  下 pytest 不 import 的无人值守入口）+ 全量 pytest。
- 收集数双口径：本机 1884（weasyprint 模块级 skip）、CI 1889；后随阶段 2-4 增至 1944。
- web 遗留测试 17 例：11 例独有覆盖迁移保留（async 402 传导是唯一守卫）、6 例真
  web 依赖删除。**web/ 本体仍在磁盘，只是移出 git 跟踪。**
- CI 首跑红过一次：6 个存量测试依赖 gitignore 掉的本机真实文件（config/scholar.env、
  output/AGENTS.md、写死本地目录名的 cwd 断言）——本机永远绿、干净 checkout 才暴露。
  修法沿用 test_notes_search 的"真实文件缺失即 skip"先例。第二跑全绿。
- 本机 pip 直连 pypi 不通（挂死无 TCP），装依赖走清华镜像；CI 侧无此问题。

## 阶段 2：一致性检查（8cb00dd）

freshness 子项设计的三个来之不易的结论（都被对抗审核证伪过一版）：
1. **三态判定**：落后且源侧刚变（< per-item grace）= ⌛未判定，不是新鲜也不是陈旧。
   单一宽限窗先被证伪为假阳性（月度链路必报）、又被证伪为假阴性（vault job 死半年
   月度 lint 永远赶在源侧刚变的窗口）——grace 按子项分档（vault/xlsx 600s、embed
   1800s）+ 死 job 诊断（embed 用 built_at 心跳直接升格；vault/xlsx 查 launchctl，
   查询失败维持原判）。
2. **报告块位置**：哨兵内、首个 LINT-SECTION 标记之前——split_lint_sections 丢弃
   首标记前文本，保证永不被结转、不进 checks_ran_at。其他任何位置都会被结转机制
   吞成永久化石（三个 reviewer 独立收敛的结论）。
3. **告警面**：rc0 时 summarize_lint_run 原本不读 stdout——报警只写进一份要靠
   "死掉的 vault job"才能送达 Obsidian 的报告，等于没报。现在 `🧭⚠` 前缀行（只有
   陈旧行用它，全新鲜不弹）→ LintOutcome.freshness_alert → backfill 低音量 notify。
- W5 完整修复：vault plist WatchPaths 三条（索引 + topics + **topics/qa**——目录
  watch 不递归，少 qa 就是修一半）。哨兵实验实战验证触发链。
- 改键提醒扩 rekey_map：刷新命令必须用**新键**（函数先重刷索引，旧键已注销，旧键
  命令静默匹配不到）；_lint.md 用词边界匹配（撞键 b/c 后缀让旧键天然是近亲键前缀）。
- 输出注入防线（压测实证的攻击面）：detail 单行化 + HTML 注释定界符/C0 控制符/RTL
  中和——含换行的文件名可注入伪 LINT-SECTION 标记穿透 checks_ran_at、伪 🚨 行污染
  撤稿通知；行内伪 GEN_END 能让下一轮 merge 把生成块尾巴复制进用户区
  （vault.extract_user_zone 的 substring-find 是预存在缺陷，本阶段只堵新增载体）。

## 阶段 3：iCloud 周快照（d200d74）

- launchd 周日 20:00 + RunAtLoad（关机跨周日的日历事件 launchd **不补**，登录补跑
  是必需）+ 6 天守卫（"上次成功"=目录文件名时间戳最大值，backup_naming 单一出处；
  未来戳/冲突副本判异类不计入——计入会让守卫永远秒退、备份静默停摆）。
- 一致性双读是主防线：launchd 唤醒会把错过的日历事件**坍缩补跑**，与 digest 同刻
  开火是常态，错峰保证不了。
- 首份真实快照由 launchd RunAtLoad 语境自己产出（740 文件/26.6M + 19M vault
  bundle）——这是对"手动能跑、定时永远挂"模式（PATH/no_proxy 两案）的最强验证。
- restore 侧修过的坑：未来戳遮蔽、坏 tar 崩+留半截、目标为文件/悬空链接崩、
  同 staging 双实例碰撞败者裸崩（mkdtemp 私有化）、库外符号链接潜伏弹（打包绿
  自检绿、恢复日整份拒收——打包侧排除）、manifest 被驱逐时静默跳过 sha 核对、
  用户自建目录被失败清理连根删、vault bundle 恢复 **clone 先行**（裸机
  `git bundle verify` 需在仓库内，verify-first 的 && 链第一步就断）。
- **docs/backup_restore.md 曾被 .gitignore 的 `docs/*` 规则吞掉**——恢复手册进不了
  git，白名单补了一行。检查新增 docs 文件是否被追踪应成为习惯。
- 灾难日演练全流程通过：restore → sha256 全量吻合 → bundle clone → 重建索引
  "内容未变跳过写盘"（快照与索引逐字节自洽的最强证明）。

## 阶段 4：大文件拆分（981610c）

- engine.py 2329 → 门面 49 + common/gemini/gemini_async/openai；lint.py 2710 →
  门面 93 + checks/ack/render/io。全部 <1200 行。
- 门面符号清单 AST+grep 自动归属（手写清单被审计抓过漏 GLOSSARY_WINDOW_SIZE）。
- 零行为变化的证明：双树四轮真实 CLI 序列 16 份产物归一化逐字节相同；153 函数 +
  51 常量与 HEAD 逐字节一致（唯一差异=声明过的破环延迟 import）；收集数 1944 节点
  ID 逐条相同；机器符号核对脚本 0 BLOCKER。
- 拆分后的纪律（写在两个门面 docstring 里）：经门面调用的符号不得改为实现模块内
  直调（monkeypatch 晚绑定靠它）；对全文跑 ids_in_text 是禁区（freshness 块故意
  不进 ack 全集）；新增对外符号先加实现模块再显式进门面。
- 已知无消费方的可观察变化：logger 名、__module__、pickle 路径（lint→lint_io）。
  reload 实现模块后须补 reload 门面才对齐（标准语义）。

## 运维备忘

- launchd job 现共 **6 个**（+com.xlbd.scholar-backup）；备份验证真产出要用
  `XLBD_BACKUP_FORCE=1`（守卫会让紧跟手动测试的 kickstart 秒退成空验）。
- 备份死亡最坏检出延迟 ≈6 周（14 天阈值 + 月度 lint 节奏）；上线第一周人工核对
  一次 XLBDBackups/ 与 cron_backup.log。
- 用户可见变化：lint 报告顶部 🧭 freshness 块（n_freshness_stale 随 skip 轮忽隐
  忽现是设计）；月度可能多一类低音量"派生物陈旧"通知；ask_notes 归档提示语更新
  （qa 同步已自动化）；vault git 提交变频繁；iCloud 明细将出现每周 ~50M 的
  XLBDBackups；CI 红了会有 GitHub 邮件。
