# 2026-09-04 缺陷台账清理批：25 条 → 20 条已修 / 1 条已实现但默认关 / 4 条推后

> 本文是执行留痕与「为什么长这样」的权威出处。台账逐条的诊断与修法细节在 `docs/bugs/*.md`
> 各自文末的「修复（2026-09-04 台账批）」节，本文只记**跨条目的决策、协议与教训**。
>
> 口径：开工时 `docs/bugs/` 有 **21 条**存量，五轮审计过程中又新写了 **4 条**
> （replace_closeread 吃尾 / backup 时间戳覆盖 / find_local_pdf 单向匹配 / 摘要降级收货），
> 合计 **25 条**，与 `docs/bugs/README.md` 三张表（20 / 1 / 4）逐条对得上。
> 下表「这批修了什么」按**主题**归并，故行数少于 20。
>
> 流程：用户要求「spawn 3 agents 进行对抗审查和修复、压力测试，至少 5 轮」。
> 每轮 = 3 个**无记忆** subagent 并行审计（镜片各轮轮换）→ 每条发现由 3 名独立怀疑者
> 对抗证伪（多数证伪即淘汰）→ 编排者修 CONFIRMED 的 → 下一轮。
> 另由编排者自跑**变异测试**验证「测试有没有牙」（最终 71 个变异 / 70 杀 / 1 等价）。

## 一、这批修了什么（按台账条目）

| 条目 | 修法要点 |
|---|---|
| finalize「僵死」三条（topics 子进程误诊 / 孤儿 / 向量同步挂起） | `topics._run_refresh_child` 取代裸 `subprocess.run`：发起前后各打一行（pid / 上限 / 日志路径 / 耗时），子进程自成进程组并随父进程 SIGTERM/SIGINT/SIGHUP 一起终止，stdout 落 `logs/topics_refresh/`（父死也留痕）。三条合并处理 |
| ingest 的 `degraded` 状态混判 | 五态拆分：`ok` / `degraded`（部分块失败，**有**草稿）/ `synth_failed`（块有成功、汇总失败）/ `api_error` / `empty`；汇总失败原因落 `draft_note` |
| 元数据退化不报 | `resolve_metadata(diag=…)` 记权威链每一级为什么没走通；`_is_thin_metadata` 判据从「抽取通道」改成**书目可用性** |
| 分块草稿四类系统性错误 | ③ 代码/数据可得性改**确定性抽取**；④ `ingest --context` 按批注入阅读目的；② chunk prompt 要求参数标注所属算例；① 分数抽成整数**未修**（启发式风险高） |
| `digest --month` 静默覆盖历史札记 | 两道护栏：开跑前预检（省一整轮 LLM）+ 写盘前守卫；`--overwrite-notes` 逃生门，覆盖前备份四件套 |
| Crossref 丢 article-number | `page` 空时取 `article-number`；存量 36 条回填**待跑** |
| 索引没有 keeper 视图 | `notes_index.iter_keepers` / `keepers_by_citekey`，两处手写过滤改为委托 |
| sidecar 写失败被静默吞掉 | error 级 + 返回 `sidecar_ok` 两态；四条消费链路（read_pdf 回执与退出码、backfill 逐月 notify、ingest 回执）都接上 |
| 摘要降级通路失效 | `abstract_from_note_md` 从 md 的 `### 摘要` 节回读；中文译文进 `translated_abstract` 而非 `original_abstract` |
| 行内 citekey 只告警不修 | `--fix-inline-citekeys`（默认 dry-run，三处原子齐改，两种形态一轮收敛，计划之间互查防造出 live 撞键）+ 告警升 **error 级**、清单挂进索引；**刻意不 notify**（持久状态反复弹会训练成噪音）|
| 手动精读升级拿后缀键 | A+B 都做：`write_notes(existing_key_owners=…)` 让同一篇论文的 keeper **继承基键**；善后工具处置存量 |
| 取全文只走两条路 | 四通道下沉进 `fulltext.resolve_oa_pdf(extra_routes=…)`，**默认关**；PDF 三闸校验 |
| 额度耗尽被记成 `no_output` | 精读链路加失败原因出参；账本分 `llm_unavailable` / `error:` / `no_output`，前两者计入熔断，重跑不再跳过 |
| Zotero saveItems 吞 target | 复审并纳入另一会话的修复；补 updateSession 的断言；删掉无调用方的 `resolve_target` |
| 精读节标题机读锚点 | 给另两对「渲染 ↔ 解析」契约补往返测试 |

**推后 4 条**（理由写在各自文档）：长论文截尾（需「正文/附录两轮」设计）、`fulltext_truncated`
混用两种成因（要新增索引字段五处联动）、存量缺 DOI 回补（第一步是联网只读盘点）、
P4 的门/证据缺口（**不属本仓库**）。

### 第 5 轮终审补的四条（都是本批**自己引入**的）

| 症状 | 修法 |
|---|---|
| `start_new_session=True` 让概念页子进程逃出 **launchd 的进程组清扫网**：job 被 `-9` 时新产生孤儿，而旧代码由 launchd 顺手收尸（用真 launchd job 实测：同组子进程被收尸、新会话子进程存活且 bootout 后仍在） | 保留自成会话（整组终止需要它），另加一条**不依赖信号**的父死联动：父进程经 `TOPICS_PARENT_PID_ENV` 把 pid 传给子进程，`build_topics.py` 起守护线程轮询，父一没就退。信号转发覆盖 TERM/INT/HUP，这条补 KILL |
| `--expand` 把 `replace_closeread` 的**插入分支**从罕见兜底变成主路径，新精读节落在本篇 `---` **之后**（与规范排布相反，原样带进 vault 单篇页） | 插入点回退时把单独一行的 `---` 也跳过 |
| Zotero 半成功（写进去了但没归类）被报成 `saved=False`，收尾汇总出现「写库 0 篇 / citekey 1 篇」——citekey 解析得出来恰恰证明条目在库里，而看见「0 篇」的人会重跑并写出重复条目 | `save_items` 改三态 `ok`/`unfiled`/`failed`；`SyncResult` 拆成 `saved`（在不在库里）与 `filed`（归没归对类）；汇总行改「写库 N 篇（其中 M 篇未归类）」 |
| `test_snapshot_restore_roundtrip_sha256` 的 `git bundle verify` 没传 cwd，断言成败取决于**跑测试时人在哪**（真仓库里通过，代码快照/tarball 解包里报「need a repository」） | 按恢复手册三步来：先 `git clone` 出仓库，再在它里面 verify |

## 二、跨条目的决策

1. **凡两处同义的判据，只留一份。** `scripts/fetch_missing_pdfs.py` 曾自持一份 arXiv 标题检索、
   PDF 三闸、换宿主——主链路修了「单向词面重合会把长综述当成本篇拉回来」，副本没跟改。
   现在四条通道、三闸、换宿主全部**委托** `src/scholar/fulltext`。这条教训比那个 bug 本身重要。
2. **新能力一律默认关。** `fulltext_extra_routes` 默认 False：真网络下的限流表现没验证过，
   而月度链路一开就是每篇多打 2~3 个 API。生产配置实测 `zotero_enabled=False` /
   `closeread_enabled=False` / `fulltext_extra_routes=False`，故覆盖护栏与四通道对 6 个
   launchd 无人值守任务**完全惰性**。
3. **摘要级产出永不覆盖全文精读。** 此前这道闸只在 `--expand` 生效，而补深度批的目标集
   本就限定 `has_full_text_reading=True`——全文这次抓不到、退化成摘要级时只要句子数不减
   就照常写盘，把索引的该字段从真翻成假、回执还打 ✅。
4. **「模型侧挂了」必须与「没东西可读」分开记账。** 两者此前都记 `no_output`，于是
   35 篇全文已到手的论文混进待下载清单等一个根本不需要的 PDF；且额度耗尽走 catch 分支、
   `err` 恒为 None，恰好被 expand 批的熔断豁免吃掉，一路跑到底烧了 65 篇。
5. **身份键不动，只放宽「继承比较」的等价类——而且等价类要与索引真正的合并规则对齐。**
   曾按 dedup_key 家族（auto 剥 vN / manual 保留 vN / DataCite DOI）比较，但那是**猜**索引会不会合并：
   第 4 轮审计指出全库有 **277 条**走 `title:` 兜底键（auto 侧 245 / manual 侧 32），
   这类条目的家族比较恒不等、keeper 照旧拿后缀键。
   现在直接用索引自己的第二把钥匙——**规范标题**（`_entry_keys` 无条件把 norm_title 也登记为身份键）：
   标题相等 ⟺ 索引一定合并，判据与后果严格等价，不多不少。`_dedup_key_family` 已删。
   `dedup_key_fields` 的身份语义自始至终一字未动。

## 三、协议与教训（下次照做）

- **变异测试是这轮性价比最高的一件事。** 第 1 轮三名审计员报了 25 项、全修完之后，
  46 个变异里**仍有 17 个存活**——`sidecar_ok` 的四条消费链路、cli 预检的接线、补抓脚本的
  keeper 视图，全都是「改对了但没人守」。补齐守卫后重跑 45 杀 1 等价；第 2 轮的修复又补
  8 个变异。第 5 轮又给「最后一公里」的 6 处修复补了变异，当场打出 3 个存活
  （`--accept-abstract` 的「别白读」半边、可得性打分的强信号、后缀提示白名单——全是
  「改对了但没人守」的老毛病），补齐守卫、并给第 5 轮自己的 4 条修复各配一个变异后重跑，
  最终 **71 个变异 / 70 杀 / 1 等价**。
  唯一存活的 R13 判为**等价变异**：`fix_inline_citekeys` 里 `new_key == dup["citekey"] → continue`
  不可达——若 duplicate 的键正是 keeper 后缀键的基键，`_stale_inline_shape` 必然把它判成
  suffix-keeper 走另一分支，进不了那个 else。这个互斥性由
  `test_stale_dup_shape_can_never_want_the_dup_key_it_already_has` 钉住，将来判据一改它会先红。
- **helper 有测试 ≠ 接线有测试。** 变异 R28b（`run_digest` 不再调预检）、R38/R41
  （`cmd_run` 把表达式抄回旧的）都是 helper 单测全绿而 call site 被换掉没人喊。
  对这类接线，`inspect.getsource` 断言是最便宜的诚实守卫——但它**只证明那行文本在函数体里**，
  不证明它会被执行（把语句塞进 `if False:` 照样绿）。能抽成纯函数就抽（如
  `abstract_source_dir`），真单测它，getsource 只用来钉住「call site 走的是这个函数」。
- **变异运行器自己也会说谎。** 第 5 轮全量重跑时 R32 报存活，查下去是运行器给它配错了
  测试集（`T_FM` 里根本没有 `test_ingest.py`），而守卫一直在。另有 6 条变异因后续重构
  锚点失效报 NOT_FOUND——`NOT_FOUND` 与 `SURVIVED` 都不能当成「测试没牙」直接采信，
  必须逐条看靶子还在不在。
- **无记忆 agent 会读到过时快照。** 编排者边审边修时，给 agent 的代码快照会落后于仓库；
  第 2 轮就有一条发现（「`_killpg_then_kill` 的 proc 从未被使用」）是快照过时导致的假阳性。
  对策：把「本轮已修」实时写进 agent 共享文档，让证伪阶段的怀疑者能据此淘汰陈旧发现。
- **变异测试跑在真仓库上，agent 必须用快照。** 否则 agent 的 pytest 会读到变异中的文件。
  且变异运行器被中途杀掉会**留下未还原的变异**——第 2 轮就发生过一次（agent 撞会话限额），
  靠 `git diff` 与备份逐字节比对才发现。凡会改仓库的批量作业，编排者自己跑，不交给 agent。
- **对抗证伪省下的返工。** 每条发现由 3 名独立怀疑者（复现 / 声明 / 影响三个镜片）投票，
  多数证伪即淘汰——挡掉了「已声明取舍」「只在打桩造出的不可能输入下成立」「生产路径不可达」三类。

## 四、用户可见的变化

- `read_pdf.py ingest` 新增 `--context "本批阅读目的"`；回执的「⚠️ 需要注意」块多两类
  （元数据退化原因、脚本草稿状态非 ok）；`draft_status` 多一个值 `synth_failed`。
- `scholar_main.py digest --month/--since` 在目标札记已存在时**会拒绝并退出 1**，
  要重造得加 `--overwrite-notes`（覆盖前自动备份到 `.digest_overwrite_backup/<时间戳>/`）。
  **手动多月工具** `scripts/monthly_backfill.sh` 走的就是这条路，重跑历史月会被拦；
  **launchd 的月度 job 不受影响**——它跑的是 `backfill_notes.py --prev-month`，
  那条路自有一道更早的 `--force` 闸（`run_month`：`note_md.exists() and not args.force` 即跳过）。
- `scripts/notes_index.py` 新增 `--fix-inline-citekeys [--apply]`（默认只打印计划）。
- finalize/regen 在 sidecar 写失败时回执首行变 ⚠️ 且**退出码 1**。
- 概念页刷新期间日志多三行（pid / 上限 / 耗时），中断父进程会连带终止子进程组。
- `logs/topics_refresh/` 下多出子进程 stdout 日志（14 天自动清理，已在 .gitignore 内）。

## 五、待跑的存量数据操作（都先 dry-run）

| 事项 | 命令 |
|---|---|
| 陈旧行内键 / 后缀 keeper（`lin2025Addressing`） | `scripts/notes_index.py --fix-inline-citekeys` → 加 `--apply` |
| 36 条 e-locator 书目补定位号 | 逐月 `scripts/repair_references.py --month <月> --force --email <邮箱> --dry-run` |
| 35 篇被记成 `no_output` 的额度失败 | 额度恢复后按 `2026-09-04-quota-failure-looks-like-no-fulltext.md` 文末命令点名重跑 |
| Zotero 三条未归档条目 | 人在 Zotero 里拖进 ScholarDigest |
