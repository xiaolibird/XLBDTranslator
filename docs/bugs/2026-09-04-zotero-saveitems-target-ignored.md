# Zotero connector 的 saveItems 吞掉 target：条目静默落进「未归档条目」

日期：2026-09-04
状态：**已修（2026-09-04 随台账批提交）**；次生发现的非 ASCII citekey 已加告警，见文末
严重度：低——不丢数据，条目写进去了，只是分类错；但静默失败（HTTP 201）
发现者：xiaolibird / Claude（2026-09-03 付费墙三篇补入会话）

---

## 现象

把三篇论文写入 Zotero，`POST /connector/saveItems` 返回 **201**，条目确实进了库，
但回查本地 API：

```
F4H7ZSXD | Analysis of Longitudinal Data with Irregular, Outcom | coll [] | 2026-09-03T02:37:20Z
8SWNLFZJ | Longitudinal data subject to irregular observation:  | coll [] | 2026-09-03T02:37:20Z
UFR69LIG | Testing Covariates Effects on Bivariate Reference Re | coll [] | 2026-09-03T02:37:20Z
```

`collections: []`＝落在库根的未归档条目。既不在我要求的 ScholarDigest，也不在
Zotero 左栏当时选中的 P4。

## 根因

`saveItems` 的 payload **不认 `target` 字段**，多余的键被静默忽略。分类归属要靠
第二步：`POST /connector/updateSession {sessionID, target}`——即浏览器插件
「保存到…」下拉的机制。端点存在性已探明（假 sessionID 返回
`400 {"error":"SESSION_NOT_FOUND"}`，而不存在的端点返回 404）。

顺带排除的两条歧路（省得下次再试）：

- **docker:1969 的 translation-server 不能写库**。它只有 `/search`、`/web`、`/export`、
  `/import`，是无状态翻译器（标识符 → Zotero item JSON），没有库的概念。
- **Zotero 本地 `/api` 只读**，BBT 的 json-rpc 也全是读方法（`item.collections`
  是查条目属于哪些分类，没有 add）。所以**已经写进去的条目无法程序化移动**，
  只能人在 Zotero 里拖。

## 已做的修复（`src/scholar/zotero_sync.py`，未提交）

- 分类名 → 连接器 target id 的解析在 `sync_segments_to_zotero` 内联完成（从 `getSelectedCollection`
  的 `targets` 列表按名查；实测 `ScholarDigest → C19`；注意本地 API 的 8 位 key 在连接器这边不认，别混用）。
  曾另有一个 `resolve_target()` 方法，是没有调用方的死代码，2026-09-04 审计后删除。
- `save_items(..., target="")`：自己持有 sessionID，写入成功后调 `updateSession` 归类；
  失败只告警不影响写入结果。
- `sync_segments_to_zotero` 的 `require_collection` 语义升级：**优先按名解析 target
  自动归类**（不必先在 Zotero 左栏选中），解析不到才退回原来的防呆（选中项不匹配就拒写）。
- `config/scholar.env` 新增 `PROCESSING__ZOTERO_COLLECTION=ScholarDigest`
  （`zotero_enabled` 默认 False，所以这行平时不生效）。
- 测试：`test/test_zotero_sync.py` 的 `_FakeClient.save_items` 补了 `target` 形参并记录它，新增
  `test_sync_require_collection_resolves_target_without_manual_select`
  （选中的是 P4、要求 ScholarDigest → 仍写入且带 `C19`）。当时 31 passed / 2 skipped（该文件此后随本批继续增补，以实际运行为准）。

## 次生发现：BBT 的 citekey 含非 ASCII 连字符

写进 Zotero 后 BBT 分配的键是 `lado‐baleato2025Testing`——中间是 **U+2010**，
不是 ASCII `-`。源头是 Crossref 的作者姓氏本身就带 U+2010（`"family": "Lado‐Baleato"`），
而我们的兜底键生成会剥掉非字母数字，于是得到干净的 `ladobaleato2025Testing`。

后果：**库内键与 Zotero/BBT 键不一致**。目前无害——`scripts/read_pdf.py:_reuse_citekeys`
按 dedup_key 沿用上一轮的键，不会被 BBT 顶回去（这一点已核过代码）。但只要有人从
Zotero 侧导书目，两边就对不上，而且 U+2010 在 BibTeX/pandoc 里是个雷。

建议：`resolve_citekey` 回查到含非 ASCII 字符的 citekey 时**打个告警**（当前静默采纳）；
本例的手动出路是在该条目的 Extra 里钉 `Citation Key: ladobaleato2025Testing`。

## 遗留

Zotero 里那三条仍在未归档条目（`F4H7ZSXD` / `8SWNLFZJ` / `UFR69LIG`），
需要人在 Zotero 里拖进 ScholarDigest，或直接删掉——本仓库的引用链路完全走本地
`all_references.json`，不读 Zotero，删了不影响任何东西。

---

## 修复（2026-09-04 台账批）

> **返回值语义变更（第 4、5 轮审计）**：`save_items` 从 `bool` 改成三态字符串
> `"ok"` / `"unfiled"` / `"failed"`。`"unfiled"` = saveItems 成功但 updateSession 没挪成，
> 即**条目已经在库里、只是没归进目标分类**。`SyncResult` 相应拆成 `saved`（在不在库里）与
> `filed`（归没归对类）。此前把半成功写成 `saved=False`，收尾汇总会打出
> 「写库 0 篇 / citekey 1 篇」这种自相矛盾的行，而看见「写库 0 篇」的人最自然的下一步是重跑，
> 重跑会写出重复条目。

本条「已做的修复」那段改动（`save_items(..., target)` + `updateSession` / `require_collection` 自动归类 / fake 补形参 + 1 个新测试）随 2026-09-04 台账批一并提交；
第 1 轮审计后另补 `test_zotero_save_items_calls_update_session_with_same_session_and_target`（此前 updateSession 零断言）、删掉无调用方的 `resolve_target`、
updateSession 失败文案改为「已写入当前选中处，未挪入 target」（saveItems 先落在当前选中分类，不一定是未归档）。
次生发现按建议落地：`ZoteroConnectorClient.resolve_citekey` 回查到含非 ASCII 字符的 citekey 时 `logger.warning`（只告警不改——键是 Zotero 侧权威，
手动出路是在该条目 Extra 里钉 `Citation Key: <ascii 键>`）。测试：`test/test_bugs_batch_2026_09.py::test_resolve_citekey_warns_on_non_ascii_key`。
遗留那三条未归档条目仍需人在 Zotero 里拖。
