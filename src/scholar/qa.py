# -*- coding: utf-8 -*-
"""问答归档：把一次高质量的检索合成存回库里，让探索复合而不是用完即弃。

## 要解决的问题

`notes_search.py` / `notes_query.py` 能把散在 2000+ 篇里的句子捞出来，但答案是**一次性**的：
今天花十分钟问清楚「MNAR 诊断在纵向 EHR 上到底能不能做」，明天想再用只能重跑一遍检索、
重看一遍原文。半年下来同一个问题会被问三四次，每次的答案还不一样——**探索不积累**。

这个模块把一次问答落成 `output/scholar_notes/topics/qa/<slug>.md`：问题、答案、每条论断的
句级出处、以及"这次召回没覆盖到的部分"（**不等于库里没有**，见下）。跟概念页一样是**活文档**：同一个问题再问一次会**原地更新
那一页**（slug 由问题文本决定），而不是堆出第二份答案。

## 与概念页（topics/）的分工

概念页回答的是**策展好的、长期的**概念问题（`config/topics.yaml` 里人工定义的 8 个），
随新论文自动重合成。这里回答的是**临时冒出来的具体问题**，由人在需要时发起。
两者共用同一套证据召回与防幻觉机制，产物形态也一致（哨兵 + 「我的批注」区 + wiki 链接），
差别只在"谁决定问什么"。

## 防幻觉：与概念页同一套编号回译

喂给 LLM 的证据块只有 `E1..En` 不含 citekey；模型回来的是编号列表，由 `validate_qa`
回译成 citekey；越界编号剔除、剔空的论断整条丢弃、正文里模型自写的引用一律剥掉。
细节见 `topics.py` 模块 docstring——这里直接复用它的 `_clean_text` / `_resolve_refs`，
不重新实现一份（两份实现迟早演化出分歧，`_render_frontmatter` 就踩过这个坑）。

## 与概念页的边界（**先看有没有对应的概念页**）

同一个问题若已有一页概念页在回答，那一页几乎总是更好的选择：它的证据面更宽、
引用率更高，而且随新论文入库**自动重合成**，而问答页只在人再问一次时才更新。
所以提问前会拿问题与 `config/topics.yaml` 里各页的 `question` 算一次相似度
（`find_similar_topics`），命中就先提示；命中的 slug 也会写在归档页顶部。
三行路由表见 `docs/scholar_notes_AGENTS.md`。

## 已知局限（写在这里，也写在报告与文档里）

**归档的问答不进向量库**，因此 `notes_search.py` 搜不到它们。**这与概念页一致——
两层合成产物都暂不进向量库，是一笔已知欠账，不是这一层单独做的取舍**
（`embed_store.py` 里一个 `topic` 字样都没有，P1 就欠着）。

欠着的原因：`embed_store.sync_store` 的语义是"index 推导出完整的期望 chunk 集，
不在期望集里的旧 id 一律删除"，合成产物要进库就得让**每一个** `sync_store` 调用方
（周度 ingest、月度 backfill、`notes_embed.py`、手动 read_pdf）都把它们一起传进去，
否则任何一个没传的调用方都会在下一次同步时把这批 chunk 整批删掉。

折中方案（**只记录，本轮不实现**）：给 qa + 概念页开一个**独立的小向量库**，
几十上百页全量重建只要几秒，不必碰 `sync_store` 一行代码，也就不承担它的删除语义。

替代的发现路径有三条，都不依赖向量库：
  1. `topics/qa/INDEX.md` 目录页（按最近更新排序，带问题全文）；
  2. vault `02-主题/问答/`——Obsidian 全文搜索 + 关系图，citekey 已转成 wiki 链接，
     per-paper 笔记因此能反向看到"哪几次问答引用了我"。⚠️ 归档一次问答**不会立刻**
     出现在那里：`com.xlbd.scholar-vault` 只盯 `literature_index.json`，问答不动索引，
     所以要等下一次索引重建（周度/月度）。急用先手动跑一次 `scripts/sync_vault.py`；
  3. **问之前先查重**（`find_similar_questions`）：新问题与已归档问题算相似度，
     像的先提示"你三个月前问过这个，在 <slug>"。这条最直接地兑现了"探索复合"——
     它拦住的正是"同一个问题被问第四次"。
"""
import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .embeddings import EmbeddingError
from .notes_index import write_if_changed
from .topics import (
    DEFAULT_EXCLUDE_SECTIONS, DEFAULT_MIN_SIM, DEFAULT_USER_ZONE, GEN_BEGIN,
    GEN_END, TOPICS_DIRNAME, TopicError, TopicSpec, ValidationReport, _CITE_RE,
    _EVIDENCE_ROW_RE, _clean_text, _render_frontmatter, _resolve_refs,
    parse_synthesis, read_existing, render_evidence_block, retrieve_evidence,
    select_evidence, to_wiki_links,
)
from .vault import (
    GEN_BEGIN_RE, ROLE_LABEL, _gen_hash, extract_user_zone,
    generated_block_tampered, split_frontmatter, tokenize,
)

logger = get_logger(__name__)

# 问答页的**防线版本号**。写进哨兵（而不是沿用概念页的 TOPIC_SCHEMA_VERSION），
# `--verify` 据此报出"这一页是哪一版防线产的"。
#
# 为什么必须有：线上首页 19:01 生成，正文里的编号回译防线 19:06 才落地，于是
# `E1的流程图明确写…` 四处原封不动留在页面上，而 frontmatter 写着 `invalid_refs: 0`、
# `--verify` 打 ✅、退出码 0——**所有仪表全绿**。归档页是"生成它那一刻的代码"的快照，
# 而当时没有任何机制能发现这件事。半年后再加两层防线，`topics/qa/` 里 50 页有多少
# 是老防线产的，没人知道。
#
# 版本号**不参与生成块哈希**（`generated_block_tampered` 只认 `h=`），所以给既有页面
# 涨版本不会把它们判成 tampered——那会让每一页重跑都变成 conflict，谁也更新不了。
#   v1: 首版（编号回译只盖 evidence 字段）
#   v2: 正文编号回译 + gap 回查 + residual_refs 自检 + slug 占用校验
QA_SCHEMA_VERSION = 2
QA_DIRNAME = "qa"                       # topics/ 下的问答归档子目录
QA_GENERATOR = "scripts/ask_notes.py"   # 写进哨兵注释的「由 X 生成」
DEFAULT_PROMPT = "config/prompts/qa_synthesis_prompt.md"
VAULT_QA_DIR = "问答"                   # vault/02-主题/ 下的子目录

# 问答召回的证据上限，以及单篇论文最多贡献几条。**比概念页窄、比概念页深**：
# 概念页要横跨全库给一页综述，轮次制（每篇先取第 1 条，再取第 2 条……）是对的；
# 一个具体问题要的是把最相关那几篇挖深。
#
# 首版取 40/4 直接复用概念页的轮次制，实测产物是「40 篇各 1 句」——候选论文数远超
# max_evidence 时**第 0 轮就填满**，per_paper_cap 永远不生效。代价是真漏东西：
# 那一页 22 条未被引用的证据里，`[@little1988Test]`（答案第 1 条依据整条建立在
# 「跑 Little's MCAR 检验」上，引的却是别人转述它的流程图）与 `[@toye2025Benchmarking]`
# （原句直接反驳了那一页 gap #2 的说法）都躺在里面标着 ○。
DEFAULT_QA_MAX_EVIDENCE = 28
DEFAULT_QA_PER_PAPER_CAP = 3

ROLE_EMOJI = {"citable": "🟪", "refutable": "🟦", "method": "🟩"}


# ---------------------------------------------------------------------------
# slug：同一个问题再问一次要落在同一页上
# ---------------------------------------------------------------------------

# slug 里允许的字符。**用定界与白名单收敛，不做字符枚举式的"排除"**——
# 这是 lint.py 那条经验的正向应用（`\w` 按 Unicode 匹配 → `\b` 同样毛病 →
# 矫枉过正成 ASCII-only 把 40 个合法非 ASCII citekey 挡在门外）。这里的判断是
# "哪些字符可以安全地进文件名与 wiki 链接"，是真正的白名单场景，与那三次不同。
_SLUG_KEEP_RE = re.compile(r"[a-z0-9]+")
_QA_SLUG_RE = re.compile(r"^qa-[a-z0-9][a-z0-9-]*$")

# slug 长度上限。300 字符的 `--slug` 此前一路通过，到落盘那一步才炸成
# `OSError: File name too long`——那时 LLM 已经跑完了。
_MAX_SLUG_LEN = 72

# 零宽字符。**`\s+` 吃不掉它们**（实测 U+200B/200C/200D/2060/FEFF 全部幸存），
# 而从 PDF 或网页复制问题时带上一个并不罕见——带了就是另一个哈希、另一页归档，
# 而两页在屏幕上一模一样。
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

# 身份键要丢掉的东西：**只有空白、下划线与纯排版标点**。
#
# 首版是 `[\W_]+`（Unicode 语义的"非字母数字"），理由写着"只留下字母数字与汉字"。
# 那个理由把**承载语义的 ASCII 符号**一起丢了，实测后果不是"多一页"而是
# "**少一页 + 串题**"：
#
#   «p<0.05 时该怎么办？»  vs  «p>0.05 时该怎么办？»   → 同一页 key='p005时该怎么办'
#   «样本量 >100 够吗»    vs  «样本量 <100 够吗»     → 同一页
#   «C++ 和 C 有什么区别»  vs  «C# 和 C 有什么区别»    → 同一页
#   «A→B 还是 B→A»       vs  «A←B 还是 B←A»        → 同一页
#
# 两个 key 相等时 `write_qa_page` 的 `taken` 不触发（它正是靠 key 不等来判的），
# 于是甲的整页答案被乙覆盖，而 `previous_answer` 还会把甲的答案当"上一版"喂给
# 乙的 prompt——prompt 铁律 11 明写「把它当草稿修订」。**C1 那道防线要拦的场景，
# 从身份键这一侧原样绕了回来。**
#
# 收窄成白名单式的"排版标点"之后：「少打一个问号」仍然合并（A3 的目标不变），
# 而 `<` `>` `=` `+` `#` `%` `/` `^` `&` `→` `←` 这些承载语义的符号全部保留。
#
# ⚠️ 收窄的代价：零宽字符（U+200B 等）**不再**被这条正则顺手吃掉。它们现在只剩
# `normalize_question` 里的 `_ZERO_WIDTH_RE` 这一道——那条从此是 load-bearing 的，
# 不是死代码（见 test_zero_width_characters_are_stripped_by_normalize_question）。
_KEY_DROP_RE = re.compile(
    "[\\s_"
    "　"                                    # 全角空格
    "？?。．.！!，,、；;：:"                      # 中英句读
    "「」『』“”‘’\"'"                            # 引号（中英、直弯）
    "（）()〔〕【】〖〗《》〈〉"                    # 括号与书名号（`〈〉` 是全角，非 ASCII）
    "—–‒－\\-～~…‥·・"                           # 连接号、波浪号、省略号、间隔号
    "]+")
# 注意：上面那串里的 `〈〉` 是全角书名号（U+3008/3009），**不是** ASCII 的 `<` `>`；
# ASCII 尖括号是比较运算符，必须留在 key 里，这正是 A6 的整条理由。


def normalize_question(question: str) -> str:
    """问题文本的**展示口径**：剔零宽字符、压空白、去首尾。标点原样保留。

    这个结果会当页面标题、frontmatter 的 `question`、以及检索 query 用，所以
    绝不能把标点改掉——把「到底能不能做？」显示成「到底能不能做」是在改用户的话。
    判"是不是同一个问题"用下面的 `question_key`。
    """
    s = _ZERO_WIDTH_RE.sub("", str(question or ""))
    return re.sub(r"\s+", " ", s).strip()


def question_key(question: str) -> str:
    """问题的**身份口径**：归一化后再丢掉全部标点/空白并小写。

    `qa_slug`、`find_similar_questions` 的排除、`write_qa_page` 的 slug 占用校验、
    `previous_answer` 的串题防护，四处必须走同一个函数——否则「同一个问题再问一次
    原地更新」这个承诺会被一个多余的问号破坏。实测确实会：
    `«…能不能做»` 与 `«…能不能做？»` 曾经是两页不同文件，查重提示「100% 像」，
    然后接一句"确认是新问题就继续，本次会另存一页"，把人径直引去另存。
    """
    return _KEY_DROP_RE.sub("", normalize_question(question).lower())


def _legacy_digest(norm: str) -> str:
    """v1 口径的 digest：直接哈希**展示口径**（标点、大小写原样参与）。

    2026-08-17 之前 `qa_slug` 用的就是它。留在这里只为一件事：认领存量页。
    """
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def qa_slug(question: str, explicit: Optional[str] = None, *, qa_dir=None) -> str:
    """问题 -> 稳定 slug。给了 `explicit` 就用它（仍要过合法性检查）。

    ## digest 口径迁移（2026-08-17）

    哈希的输入从 `normalize_question`（**展示口径**，标点大小写都算）改到
    `question_key`（**身份口径**），好处是「少打一个问号」不再另开一页。
    代价是**所有 v1 口径生成的 slug 全部作废**，而当时没有任何迁移、任何检测——
    CLI 实测同一个问题、逐字节相同的文本，查重说「100% 像 `qa-mnar-ehr-1f424623`」，
    下一行 dry-run 却说归档路径会是 `qa-mnar-ehr-82bb128f`：磁盘上那页从此是孤儿，
    不再被任何自动路径更新，而 `--verify` 全绿。这是 MEMORY 里
    `scholar-identity-keys` 那条教训的复刻——**改身份键必须扫派生物**。

    迁移办法（两条，都已落地，不需要人工干预）：
      1. 给了 `qa_dir` 时**新键的文件不存在、旧键的文件存在且问题相同**就沿用旧 slug
         （同 `scholar-pub-date-precision` 里「regen 沿用键」的手法）。存量页因此
         自动回到自动路径上，重问一次就是原地更新；
      2. `audit_qa_pages` 有 `slug == qa_slug(question)` 的不变式，扫得出任何
         这一类的孤儿页并给出改名命令。
    想手动收编也行：`mv 旧.md 新.md` 再把 frontmatter 的 `qa:` 改成新 slug。

    ## 其余约定

    自动 slug **只由问题文本决定，不掺日期**：这样同一个问题再问一次会原地更新
    那一页，而不是每天堆一份新的。这是"活文档"与"流水账"的分界——
    如果掺了日期，`topics/qa/` 三个月后就会变成一堆互相矛盾的历史快照，
    而这个模块存在的全部理由是让答案**积累**而不是**堆积**。

    哈希取 `sha1[:8]`（32 bit）：n=1000 页时任意碰撞约 0.012%，而碰撞的后果是
    两个不相干的问题共用一页——`write_qa_page` 的 slug 占用校验兜住了这一层，
    但宽度本身仍是第一道。

    问题里若含 ASCII 单词（中英混排的技术问题几乎总会有，如 "MNAR"、"EHR"），
    取前几个拼进 slug 让文件名可读；纯中文问题就只剩哈希，靠 frontmatter 的
    `title`（问题全文）在 INDEX 与 Obsidian 里显示。
    """
    if explicit:
        # 首尾连字符按**归一化**处理（同大小写、同 qa- 前缀）：`qa-abc-` 与 `qa-abc`
        # 是同一页，让它通过只会得到两页内容相同、名字差一个字符的归档。
        s = str(explicit).strip().lower().strip("-")
        if not s.startswith("qa-"):
            s = "qa-" + s
        s = s.strip("-")
        if len(s) > _MAX_SLUG_LEN:
            raise TopicError(
                "slug '{}…' 太长（{} 字符，上限 {}）：它要当文件名用，"
                "过长会在落盘那一步才炸成 OSError。".format(
                    str(explicit)[:24], len(s), _MAX_SLUG_LEN))
        if not _QA_SLUG_RE.match(s):
            raise TopicError(
                "slug '{}' 非法：只允许小写 ASCII 字母数字与连字符（它要同时当文件名与"
                "wiki 链接用）。留空则按问题自动生成。".format(explicit))
        return s
    norm = normalize_question(question)
    if not norm:
        raise TopicError("问题为空，无法归档")
    # 哈希算在身份键上（标点/零宽不影响），可读词干仍取自展示口径。
    # `or norm` 兜住「!!!???」这种整句都是标点的问题：身份键为空但问题本身不空。
    words = _SLUG_KEEP_RE.findall(norm.lower())[:4]
    stem = "-".join(w for w in words if w)

    def _mk(digest: str) -> str:
        return "qa-{}-{}".format(stem, digest) if stem else "qa-{}".format(digest)

    key = question_key(norm) or norm
    slug = _mk(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8])
    if qa_dir is None:
        return slug
    d = Path(qa_dir)
    if (d / "{}.md".format(slug)).exists():
        return slug            # 新口径那页已经在了，以它为准

    # 目录里已经有一页**身份相同**的问答就沿用它的 slug，不论那页的文件名是按哪一版
    # digest 口径起的。这里故意**不是**"重算一遍旧 digest 看文件在不在"——那只覆盖
    # 「逐字相同的原问题」这一种情形：v1 页 `…能不能做？` 的旧 digest 认得出它自己，
    # 却认不出 `…能不能做`（少一个问号）。而"少打一个问号不该另开一页"正是身份键
    # 这次改动的**全部目的**，那种修法会把它修掉一半（实测：原问题回到旧页、
    # 变体另开一页，同一个问题最后仍是两页）。
    #
    # 按身份扫一遍则两种情形一起覆盖，规则也更简单：**一个身份一页**。
    # 代价是读 N 份 frontmatter，几十上百页的量级可以忽略，且只在给了 qa_dir 时才走。
    for page in list_qa_pages(d):
        if page.question and question_key(page.question) == question_key(norm):
            if page.slug != slug:
                logger.info("qa 页 {} 与本问题身份相同（文件名按旧 digest 口径起的），"
                            "本次沿用它，不另存一页", page.slug)
            return page.slug
    return slug


def update_command(slug: str, question: str) -> str:
    """「要更新那一页」的可执行命令。查重提示与 slug 占用报错都用它。

    **`question` 必须是那一页自己的问题，没有默认值。**首版的默认值是
    `<你的新问法>`，而 `write_qa_page` 的占用检查是拿 `question_key` 比对的——
    **任何真正的"新问法"都是另一个键，粘进去必然被拒**，实测：

        $ ask_notes.py "纵向 EHR 数据里的 MNAR 到底可不可检验？" --slug qa-mnar-ehr-1f424623
        ❌ slug 'qa-mnar-ehr-1f424623' 已经属于另一个问题：«MNAR 诊断在纵向 EHR 上到底能不能做？»
        EXIT=2

    也就是代码保证了那条"可直接粘的更新命令"会失败。`--verify` 的错误文案里印的
    一直是那一页自己的问题，是对的；这里统一到那个口径，并把默认值整个去掉，
    免得下一个调用点又漏传。

    （`--slug` 唯一能干的事是给一页**全新**问答起个好读的文件名。「换个问法覆盖
    那一页」这件事代码里没有路径，也不该有——那会让 frontmatter 的 `question`
    与页面内容悄悄脱钩。两份文档里"用 --slug 合并"那条自相矛盾的说法已经删掉。）

    用 `shlex.quote` 而不是手写引号：问题里含 `"` 时打出来的命令直接是坏的，
    含反引号或 `$(…)` 时粘进 shell 还会**命令替换**——这条命令是印给人复制粘贴的。
    """
    import shlex
    return "PYTHONPATH=. python scripts/ask_notes.py {} --slug {}".format(
        shlex.quote(str(question)), shlex.quote(str(slug)))


def is_migration_orphan(hits: Sequence[Tuple["ArchivedQA", float]], question: str) -> bool:
    """查重命中的第一条是不是「digest 口径迁移留下的孤儿页」。

    判据是**身份键相同**（高分只是它被摆到用户眼前的原因，真正决定"是不是同一个
    问题"的一直是 `question_key`）。命中它时提示语要说「用 `--slug <旧>` 更新它」，
    而不是那句会把归档翻倍的「确认是新问题就继续，本次会另存一页」。
    """
    if not hits:
        return False
    page, score = hits[0]
    return (score >= 0.99
            and question_key(page.question or page.title) == question_key(question))


# ---------------------------------------------------------------------------
# 证据召回：复用概念页那一套
# ---------------------------------------------------------------------------

def build_qa_spec(question: str, *, extra_queries: Sequence[str] = (),
                  roles: Sequence[str] = (),
                  max_evidence: int = DEFAULT_QA_MAX_EVIDENCE,
                  exclude_sections: Optional[Sequence[str]] = None) -> TopicSpec:
    """把一个问题包装成临时 `TopicSpec`，直接喂给 `topics.retrieve_evidence`。

    复用而不是另写一套召回的理由：那一套已经被多轮审核加固过——`min_sim` 的经验值
    （0.55 而不是 notes_search 的 0.65，见 `topics.DEFAULT_MIN_SIM` 的实测依据）、
    单篇证据配额的轮次制、「对我研究的联想」这类主观批注按前缀排除，每一条都是踩出来的。
    另写一份必然要把这些坑重踩一遍。

    `extra_queries` 存在是因为 `TopicSpec.queries` 是**多查询各检索一次后合并**，
    而不是拼成一句（拼接会把几个概念平均成一个语义中点，谁也召不准）。
    问题本身是第一个 query，用户可以再给几个关键术语的不同写法——概念页配置里
    那句「中文查询能命中英文原文，但概念换述的召回率实测只有 ~30%，所以关键术语的
    中英两种写法都要给」在这里同样成立。
    """
    norm = normalize_question(question)
    if not norm:
        raise TopicError("问题为空")
    queries = [norm] + [str(q).strip() for q in extra_queries if str(q).strip()]
    bad_roles = [r for r in roles if r not in ROLE_LABEL]
    if bad_roles:
        raise TopicError("roles 含非法值 {}（合法：{}）".format(bad_roles, sorted(ROLE_LABEL)))
    return TopicSpec(
        slug=qa_slug(question), title=norm[:80], question=norm, queries=queries,
        buckets=[], roles=list(roles), max_evidence=max_evidence,
        exclude_sections=(list(exclude_sections) if exclude_sections is not None
                          else list(DEFAULT_EXCLUDE_SECTIONS)),
    )


# 取候选池时用的"不设限"参数。`retrieve_evidence` 内部一定会调一次 `select_evidence`，
# 想拿到未截断的候选池就只能把两个上限都放到不可能触及的值。per_paper_cap 不能直接给
# 一个天文数字——`select_evidence` 的轮次循环是 `range(per_paper_cap)`，给 10**6
# 会空转一百万轮；200 已经远超任何一篇论文的 highlight 数。
_WIDE_MAX_EVIDENCE = 10 ** 6
_WIDE_PER_PAPER_CAP = 200


def select_qa_evidence(candidates: Sequence[Any], *,
                       max_evidence: int = DEFAULT_QA_MAX_EVIDENCE,
                       per_paper_cap: int = DEFAULT_QA_PER_PAPER_CAP) -> List[Any]:
    """候选池 -> 定稿证据，**按论文最高分贪心累加名额，再走轮次制**。

    这一层存在的唯一理由：`topics.select_evidence` 的轮次制在"候选论文数远超
    max_evidence"时会退化成「每篇各 1 句」——第 0 轮就把名额填满，`per_paper_cap`
    一次都轮不到。概念页要横扫全库，那正是想要的；一个具体问题要的是把最相关那
    几篇挖深，退化的结果是 `[@little1988Test]`（答案依据的那条检验的原文）
    与 `[@toye2025Benchmarking]`（直接反驳本页 gap 的那一条）都标着 ○ 躺在表里。

    **不改 `select_evidence` 本身**：概念页依赖它、已过多轮审核。这里只在调用它
    之前决定"让哪几篇进来"。

    ## 为什么是贪心累加而不是 `ceil(max_evidence / cap)`

    首版拿 `max_papers = ceil(max_evidence / cap)` 拍一个论文数，那是**预截断**：
    它假设留下的每一篇都能填满自己的 cap。真实候选池不是这个形状——线上那个问题
    实测 **41 条 / 33 篇，每篇平均只有 1.24 条**，于是先砍到 10 篇之后，
    空出来的 12 个格子**不回补**：

        宽召回候选池：              41 条 / 33 篇
        预截断口径 (28, 3)：        16 条 / 10 篇   ← 静默欠填 43%
        旧口径 select_evidence(28,3)：28 条 / 28 篇

    被砍掉的 18 篇里有三条**直接回答那个问题**、分数只比第 10 名低 0.006~0.010
    （`hagag2026Association` / `qiao2023Identification` / `getzen2022Mining`）。
    更糟的是 `--per-paper-cap` 是 CLI 暴露的参数、帮助文案写着"单篇最多贡献几条"，
    用户按字面理解调大以求挖深，拿到的却是**单调递减**（实测 cap 1 → 28 条 / 28 篇，
    cap 3 → 16 条 / 10 篇，cap 28 → **1 条 / 1 篇**）。

    贪心累加按论文最高分逐篇加进来、累加 `min(len(group), cap)` 直到够 `max_evidence`：
    池子深时行为与预截断一致（前几篇就把名额吃满 = 挖深），池子浅时自动多铺几篇
    回补到 28 条。cap 调大只会让**同一篇**多贡献，绝不会让总数变少。

    边界：`per_paper_cap >= max_evidence` 时这一层没有意义（一篇就装得下全部名额），
    直接跳过截断 = 等价旧的 `select_evidence`；`<= 0` **报错**而不是静默当 1
    ——打 0 的人多半想的是"不限制"，拿到最窄那一档且毫无提示是最坏的一种。
    """
    if per_paper_cap <= 0:
        raise TopicError(
            "per_paper_cap 必须 ≥ 1（收到 {}）。想「不限制」就给一个 ≥ max_evidence 的值，"
            "那会等价于不做单篇配额；静默当成 1 反而是最窄的一档。".format(per_paper_cap))
    if max_evidence <= 0:
        return []
    cap = per_paper_cap
    if cap >= max_evidence:
        return select_evidence(candidates, max_evidence, cap)

    groups: Dict[str, int] = {}
    best: Dict[str, float] = {}
    for c in candidates:
        s = c.score + c.bucket_boost
        groups[c.citekey] = groups.get(c.citekey, 0) + 1
        if c.citekey not in best or s > best[c.citekey]:
            best[c.citekey] = s
    keep: set = set()
    budget = 0
    for ck in sorted(best, key=lambda k: (-best[k], k)):
        keep.add(ck)
        budget += min(groups[ck], cap)
        if budget >= max_evidence:
            break
    if len(keep) < len(best):
        candidates = [c for c in candidates if c.citekey in keep]
    return select_evidence(candidates, max_evidence, cap)


def nearby_papers(candidates: Sequence[Any], selected: Sequence[Any], *,
                  top_n: int = 5) -> List[str]:
    """"差一点就进来了"的那几篇 citekey，按最高分降序。

    B2：窄而深让 11 名之后的论文**从页面上彻底消失**。旧版至少让漏网证据以 ○ 的
    身份躺在证据表里——上一轮验收方逮到 `little1988Test` 靠的正是这一点。
    把这几篇列在页面上是零成本的可见性补偿：不是引用，只是"你可能还想看看"。
    """
    chosen = {getattr(e, "citekey", None) for e in selected}
    best: Dict[str, float] = {}
    for c in candidates:
        if c.citekey in chosen:
            continue
        s = c.score + c.bucket_boost
        if c.citekey not in best or s > best[c.citekey]:
            best[c.citekey] = s
    return sorted(best, key=lambda k: (-best[k], k))[:top_n]


def retrieve_qa_evidence(spec: TopicSpec, store, embed_client, *,
                         min_sim: float = DEFAULT_MIN_SIM,
                         per_paper_cap: int = DEFAULT_QA_PER_PAPER_CAP,
                         ) -> Tuple[List[Any], List[str]]:
    """问答场景的证据召回：复用 `topics.retrieve_evidence` 拿候选池，再窄而深地定稿。

    返回 `(证据, 差一点没进来的论文 citekey)`——后者是 B2 的可见性补偿，
    渲染成页面上那一行「本次未纳入的近邻论文」。**它不是引用**，见 `nearby_papers`。
    """
    wide = replace(spec, max_evidence=_WIDE_MAX_EVIDENCE)
    candidates = retrieve_evidence(wide, store, embed_client, min_sim=min_sim,
                                   per_paper_cap=_WIDE_PER_PAPER_CAP)
    picked = select_qa_evidence(candidates, max_evidence=spec.max_evidence,
                                per_paper_cap=per_paper_cap)
    return picked, nearby_papers(candidates, picked)


# ---------------------------------------------------------------------------
# prompt 与校验
# ---------------------------------------------------------------------------

def load_prompt_template(path) -> str:
    p = Path(path)
    if not p.exists():
        raise TopicError("问答 prompt 模板不存在：{}".format(p))
    text = p.read_text(encoding="utf-8")
    missing = [ph for ph in ("{{QUESTION}}", "{{EVIDENCE_BLOCK}}") if ph not in text]
    if missing:
        raise TopicError("prompt 模板 {} 缺占位符 {}".format(p, missing))
    return text


def build_qa_prompt(question: str, evidences, template: str,
                    research_interests: str = "", previous_answer: str = "") -> str:
    prev = (previous_answer or "").strip()
    if len(prev) > 6000:
        prev = prev[:6000] + "\n……（上一版过长，此处截断）"
    return (template
            .replace("{{QUESTION}}", normalize_question(question))
            .replace("{{RESEARCH_INTERESTS}}", research_interests.strip() or "（未配置）")
            .replace("{{EVIDENCE_BLOCK}}", render_evidence_block(evidences))
            .replace("{{PREVIOUS_ANSWER}}", prev or "（无上一版，这是首次归档这个问题）"))


# 正文里模型顺手写出的证据编号（"E1 的流程图明确写……"、"E31 描述的算法"）。
#
# **左侧后顾断言是真防线**：它保证 `AE1`、`MICE1` 这类词尾不被误当成编号。用的是显式
# ASCII 字符类而不是 `\b`——`\b` 与 `\w` 按 Unicode 判定、汉字算单词字符，中文行文里
# 「见E31描述」这种紧贴写法会判不到边界而整条漏过（lint.py 那三次教训的直接应用：
# `\w` → `\b` → 矫枉过正成 ASCII-only）。
#
# **右侧的 `(?![0-9])` 是冗余的**，写出来只为把意图摆明：`\d+` 本身贪婪，`E312` 一定
# 整体匹配成 `E312`（越界 → 剔除），不可能被截成 `E31` 再错误回译到第 31 条证据上。
# 变异测试实测把这个前瞻删掉行为完全不变——记在这里，免得后人以为它在挡什么。
#
# `\[?` / `\]?` 在匹配体里（后顾断言仍在最前，不受影响）：喂给模型的证据块首字段
# 就是 `[E1]（可引用证据 · 实验方法 · 2024）`，模仿这个形状的概率不低。不吃掉那对
# 方括号就会得到 `[[@a2024X]]`，再过 `to_wiki_links` 变成 `[[[a2024X|a2024X]]]`——
# 不是死链，但多一对字面方括号，粘进 pandoc 也多一层。
_INLINE_REF_RE = re.compile(r"(?<![A-Za-z0-9])\[?[Ee](\d+)\]?(?![0-9])")


def backtranslate_inline_refs(text: str, ev_map: Dict[str, Any],
                              report: ValidationReport,
                              used: Optional[set] = None) -> str:
    """把正文里模型顺手写的证据编号回译成 `[@citekey]`，越界的直接剔除。

    实测（首次真实运行，2026-08-17）模型确实会这么写：
    「E1 的流程图明确写 NMAR 确认需要外部数据」「E31 描述的条件独立检验搜索父集合的算法」。
    这些编号**不经过 `evidence` 字段的校验**——它们是自由文本的一部分。放着不管有两种坏法：

      1. 编号越界（模型写 E99 而只召回 40 条）→ 页面上留下一个指向不存在证据的交叉引用，
         而读者会以为自己没找到；
      2. 编号有效但页面被摘去写稿后，`E31` 脱离了本页的证据表就完全失去意义——
         它是**本次召回的内部编号**，不是任何持久标识。

    回译成 `[@citekey]` 一次解决两者：越界的剔除（计入 `invalid_refs`，与 `_resolve_refs`
    同一个计数口径），有效的变成真正可追溯、可粘进 pandoc 的引用。这不是新的防线，
    是把已有的编号回译防线**补到自由文本这条此前没盖住的通道上**——同 `_clean_text`
    之于模型自写的 citekey。

    `used` 给了就把**本次成功回译的编号**塞进去（出参而不是改返回类型：调用点全都在
    拿字符串用）。没有它的话，只在 `answer` 或 `gaps` 里被提到的那条证据会在证据表里
    标 ○，而表头写着"未被引用的证据标 ○"——页面上明明有 `[@key]`，两个数字互相自洽
    却都与页面事实不符，CLI 的"引用率"也跟着少算。
    """
    def _sub(m):
        ref = "E{}".format(int(m.group(1)))
        ev = ev_map.get(ref)
        if ev is None:
            report.invalid_refs += 1
            return ""
        if used is not None:
            used.add(ref)
        return "[@{}]".format(ev.citekey)

    out = _INLINE_REF_RE.sub(_sub, text)
    # 剔除越界编号后可能留下 "……（ 的流程图" 之类的空档
    return re.sub(r"\s{2,}", " ", out).strip()


def validate_qa(data: dict, evidences) -> Tuple[dict, ValidationReport]:
    """LLM 输出 -> 编号回译后的结构。丢弃而非修补，同 `topics.validate_synthesis`。

    `answer` 是总述，**允许没有编号**（它是对下面各条论断的概括），但一样要过
    `_clean_text` 把模型自写的引用剥掉——那是唯一能绕开编号校验的编造通道。
    `points` 与 `caveats` 则**每条都必须有至少一个有效编号**，凑不出的整条丢弃：
    一条没有出处的论断混在有出处的论断里，比少一条危险得多，因为读者会默认它们同质。
    """
    report = ValidationReport()
    ev_map = {e.ref: e for e in evidences}
    used: set = set()
    inline: set = set()          # 正文里回译出来的编号（只统计**留在页面上**的）

    def _refs_to_keys(refs: List[str]) -> List[str]:
        keys: List[str] = []
        for r in refs:
            used.add(r)
            ck = ev_map[r].citekey
            if ck not in keys:
                keys.append(ck)
        return keys

    def _items(raw) -> List[dict]:
        out: List[dict] = []
        for it in (raw or []):
            if not isinstance(it, dict):
                continue
            # 先剥模型自写的 citekey，再回译正文里的证据编号——两条通道都要盖住，
            # 顺序不能反：先回译会引入 `[@key]`，再剥就把刚回译出来的也剥掉了。
            mine: set = set()
            text = backtranslate_inline_refs(
                _clean_text(it.get("text"), report), ev_map, report, used=mine)
            refs = _resolve_refs(it.get("evidence"), ev_map, report)
            if not text or not refs:
                report.dropped_claims += 1
                continue
            # 整条被丢弃时它正文里的编号也随之消失，不能算"被引用过"
            inline.update(mine)
            out.append({"text": text, "refs": refs, "citekeys": _refs_to_keys(refs)})
            report.kept_claims += 1
        return out

    points = _items(data.get("points"))
    caveats = _items(data.get("caveats"))
    gaps: List[str] = []
    for g in (data.get("gaps") or []):
        mine = set()
        t = backtranslate_inline_refs(_clean_text(g, report), ev_map, report, used=mine)
        if t:
            gaps.append(t)
            inline.update(mine)
    # `answer` 没有 `evidence` 字段（它是概括，不是单条论断），所以正文里的编号
    # 是它**唯一**的可追溯线索——更要回译，否则摘出去就是一段无出处的断言。
    mine = set()
    answer = backtranslate_inline_refs(
        _clean_text(data.get("answer"), report), ev_map, report, used=mine)
    if answer:
        inline.update(mine)
    used |= inline
    report.used_refs = len(used)
    return {
        "answer": answer,
        "points": points,
        "caveats": caveats,
        "gaps": gaps,
        # 只在自由文本里出现过的编号：证据表要据此标 ●，否则页面上明明有 `[@key]`
        # 而表里写着"未被引用"。
        "inline_refs": sorted(inline, key=lambda r: int(r[1:])),
    }, report


def is_archivable(qa: dict) -> bool:
    """这份校验结果值不值得落成一页。

    判据是 **`points` 或 `caveats` 至少剩下一条**，`answer` 不单独作为放行条件：
    `answer` 被允许没有编号的**前提**是"它是下面各条论断的概括"，两节全空时这个前提
    就不成立，落盘得到的是一句纯 LLM 断言配一张全 ○ 的证据表——形态上与一页正常归档
    毫无区别，而它恰恰是这个模块最想拦住的东西。

    反过来 `points` 空、`caveats` 非空是**合法结果**（"这个问题库里答不了，但有三条
    你必须知道的限制"），旧判据把它当成"什么都没剩下"而退出 2，丢掉的是真东西。
    """
    return bool(qa.get("points") or qa.get("caveats"))


def synthesize_qa(llm, prompt: str, *, model: Optional[str] = None,
                  max_tokens: int = 6000, retries: int = 1) -> dict:
    """调 LLM + 解析。与 `topics.synthesize_topic` 同一套容错。

    异常统一包成 `TopicError` 再上抛：回退链整条耗尽时 `llm_client` 抛的是
    `RuntimeError("所有 LLM provider 均不可用…")`，本项目实际踩过且是最常见的失败模式；
    不包一层的话调用方就分不清"召回没捞到东西"与"合成端挂了"，会被引去调错的旋钮。

    解析失败重试一次：claude-agent 没有原生 json_mode，实测会偶发输出
    「已完成合成，要点：……」这类元描述，整段没有花括号、首尾花括号兜底也救不回来。
    只重试解析失败，provider 层的错误交给 llm_client 自己的重试与回退链。
    """
    last: Optional[TopicError] = None
    for attempt in range(retries + 1):
        p = prompt
        if attempt:
            p += ("\n\n【上一次输出格式不合规】你上次没有直接输出 JSON。这一次**第一个字符**"
                  "必须是 `{`、**最后一个字符**必须是 `}`，中间是完整的结果。"
                  "不要写任何说明、前言、要点总结或代码围栏。")
        try:
            raw = llm.call(p, model=model, max_tokens=max_tokens,
                           temperature=0.2, json_mode=True)
        except TopicError:
            raise
        except Exception as e:
            raise TopicError("LLM 调用失败（{}）：{}".format(type(e).__name__, e)) from e
        try:
            return parse_synthesis(raw)
        except TopicError as e:
            last = e
            if attempt < retries:
                logger.warning("问答合成输出不是合法 JSON，追加格式指令重试一次：{}",
                               str(e)[:120])
    raise last


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _cite_str(citekeys: Sequence[str]) -> str:
    return " ".join("[@{}]".format(k) for k in citekeys)


def _gap_hit_lines(hits: Sequence["GapHit"]) -> List[str]:
    """gap 命中渲染成**逐条一行**，带标题与原句（见 `GapHit` 的 docstring）。

    首版把它们挤成一行裸 citekey（`[@a]（0.72）、[@b]（0.71）`）。实测那一行里六成
    是噪音，而读者从 citekey 完全看不出哪条值得点——只能一条条点开，
    于是一年后整节被跳过。逐条一行 + 标题 + 原句之后，扫过去就能筛。
    """
    out: List[str] = []
    for h in hits:
        if h.kind == "topic":
            out.append("    - `topics/{}.md`（{:.2f}）{}".format(
                h.key, h.score, h.title or ""))
            continue
        head = "    - `[@{}]`（{:.2f}）{}".format(h.key, h.score, (h.title or "")[:80])
        out.append(head)
        if h.snippet:
            out.append("      > {}".format(h.snippet[:100]))
    return out


def render_qa_block(question: str, qa: dict, evidences, *,
                    related_topics: Sequence[str] = (),
                    nearby_papers: Sequence[str] = (),
                    titles: Optional[Dict[str, str]] = None) -> str:
    """哨兵之间的正文：问题 → 相关概念页 → 直接回答 → 论断 → 注意事项 → 没覆盖到的 → 证据表。

    「本次召回没覆盖到的」（gaps）**排在证据表之前而不是文末**：它是这一页最容易被
    跳过、却最该被看到的一节——一个答案的边界在哪里，比答案本身更决定它能不能
    被直接写进稿子。

    那一节的措辞是本轮改得最重的地方，见下面的注释。
    """
    used_refs = {r for it in (qa["points"] + qa["caveats"]) for r in it["refs"]}
    used_refs |= set(qa.get("inline_refs") or ())
    L: List[str] = ["# {}".format(normalize_question(question)), ""]
    if related_topics:
        # A4：同一个问题，库里可能已经有一页更大、更全、随新论文自动重合成的答案，
        # 而首版没有任何一处告诉用户。挂在最顶上而不是文末——读到文末才知道
        # "其实该去读另一页"，那 90 秒已经花掉了。
        L += ["> 📎 **相关概念页**：{} —— 它证据面更宽、随新论文**自动重合成**，"
              "概念级的问题先读那一页。".format(
                  "、".join("[[{}]]".format(s) for s in related_topics)), ""]
    if qa["answer"]:
        L += ["> {}".format(qa["answer"]), ""]

    if qa["points"]:
        L += ["## 依据", ""]
        L += ["- {} {}".format(p["text"], _cite_str(p["citekeys"])) for p in qa["points"]]
        L.append("")
    if qa["caveats"]:
        L += ["## ⚠️ 用之前要知道的", ""]
        L += ["- {} {}".format(c["text"], _cite_str(c["citekeys"])) for c in qa["caveats"]]
        L.append("")
    if qa["gaps"]:
        # A1【本轮最严重】首版这一节叫「本次没答上来的」，正文写着
        # 「这些是**库里现在没有证据**的部分……是下一步该去补的文献」。
        # 那是把**本次召回的召回率缺口**印成了对 2300+ 篇文献库的事实断言——
        # 模型判断"库里有没有"的唯一依据就是这次给它的那几十条证据。
        # 实测产物据此写下「库内证据显示这类讨论几乎完全缺失」，而
        # `topics/missingness-causal.md` 整整一页 302 行都在讲那件事，
        # 那一页的核心文献当时就躺在本页自己的证据表里。
        # **答错了用户还能核对，指错方向他不会去核对**——这一节按设计没有出处可点。
        hits_by_gap = qa.get("gap_hits") or {}
        L += ["## 本次召回没覆盖到的", "",
              "这是**本次这 {} 条证据**没能回答的部分，**不等于库里没有**——"
              "召回是按单个问题从全库切出来的窄切片，召回不到 ≠ 不存在。"
              "决定要不要去补文献之前，先查 `topics/INDEX.md` 与 `notes_search.py`。".format(
                  len(evidences)), ""]
        for g in qa["gaps"]:
            L.append("- {}".format(g))
            hits = hits_by_gap.get(g) or []
            if hits:
                # 这一行是"回查"的兑现物：拿这条空白**自己的文本**再问一次库，
                # 命中就当场把反例贴在它旁边，而不是等用户自己去发现。
                L.append("  - ⚠️ **但库里可能有**（回查这条空白时命中的；"
                         "概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，"
                         "尤其注意有些原句本身是否定句，说的是「那篇里没有」）：")
                L += _gap_hit_lines(hits)
        L.append("")

    n_papers = len({e.citekey for e in evidences})
    L += ["## 本页证据（{} 条 · {} 篇）".format(len(evidences), n_papers), ""]
    L.append("每条可回溯到札记原文；未被引用的证据标 ○。**要把具体数字写进稿子，"
             "先点开原句核对**——防线保证 citekey 与原句真实存在，不保证转述没有失真。")
    L.append("")
    for e in evidences:
        mark = "●" if e.ref in used_refs else "○"
        meta = [ROLE_EMOJI.get(e.role or "", "") + ROLE_LABEL.get(e.role or "", e.role or "")]
        if e.section:
            meta.append(e.section)
        loc = ""
        if e.note_file:
            loc = " · {}{}".format(e.note_file, ":{}".format(e.note_line) if e.note_line else "")
        L.append("- {} **{}** `[@{}]` {}{}".format(
            mark, e.ref, e.citekey, " · ".join(m for m in meta if m), loc))
        if e.title:
            L.append("  <small>{}</small>".format(e.title))
        L.append("  > {}".format(e.text.strip().replace("\n", " ")))
    if nearby_papers:
        # B2：窄而深让 11 名之后的论文从页面上**彻底消失**，连被肉眼逮到的机会都没有。
        # 旧版至少让它们以 ○ 的身份躺在证据表里（上一轮验收方逮到 `little1988Test`
        # 靠的就是这一点）。
        #
        # 这里的 citekey **故意不写成 `[@key]`**：它们不是引用，是"你可能还想看看"。
        # 写成 `[@key]` 会被 `_CITE_RE` 当成本页引用，进 `n_cites` 与死键检测——
        # 而死键检测的语义是"粘进 pandoc 会渲染成 (key?)"，对一条从没被引用过的
        # 建议键报警就是假信号。用反引号包着，`audit_qa_pages` 与 `to_wiki_links`
        # 都自然看不见它们，不需要在任一侧再加一条"跳过这一行"的特判。
        # 带标题：这一行是给"差一点没进来"的论文补可见性的，而三个不认识的裸 citekey
        # 排在那里没有人会去查——它替代的是 v1 里带整句原文的 ○ 行，补偿不能比被
        # 牺牲的东西还便宜。
        L += ["", "**本次未纳入的近邻论文**（分数就差在门槛下，不是引用，"
                  "想挖深就调大 `--max-evidence`）："]
        L += ["    - `{}` {}".format(k, (titles or {}).get(k, "")[:80])
              for k in nearby_papers]
    return "\n".join(L)


def build_qa_frontmatter(question: str, slug: str, qa: dict, evidences,
                         report: ValidationReport, generated_at: str,
                         preserved: Optional[Dict[str, Any]] = None) -> str:
    """受管键由生成器覆盖，用户自加键原样保留（同 topics/vault 的既有约定）。

    `first_asked_at` 是**唯一一个"存在就不覆盖"的受管键**：同一个问题再问一次是
    原地更新，`generated_at` 要前进，但"我第一次是什么时候问的"是这一页的历史，
    重新生成不该把它抹掉。
    """
    preserved = preserved or {}
    # **不要求它是 `str`**：YAML 把 `first_asked_at: 2026-01-01`（没引号）解析成
    # `datetime.date`，把 `20260101` 解析成 int，两者此前都被静默重置成"今天"，
    # 状态还照样是 `merged`、没有任何提示。而裸日期正是最现实的一种——用户在
    # Obsidian 属性面板里手动修一下就会去掉引号。空串保持"重置"（那是坏值不是历史）。
    prev_first = preserved.get("first_asked_at")
    first_asked_at = generated_at
    if prev_first not in (None, ""):
        first_asked_at = str(prev_first)
    elif prev_first == "":
        logger.info("qa 页 {} 的 first_asked_at 是空串，按新建重置为 {}", slug, generated_at)
    managed: Dict[str, Any] = {
        "qa": slug,
        "title": normalize_question(question)[:120],
        "question": normalize_question(question),
        "type": "qa",
        "qa_schema": QA_SCHEMA_VERSION,
        "first_asked_at": first_asked_at,
        "generated_at": generated_at,
        "n_evidence": len(evidences),
        "n_papers": len({e.citekey for e in evidences}),
        "n_points": len(qa["points"]),
        "n_caveats": len(qa["caveats"]),
        "n_gaps": len(qa["gaps"]),
        "evidence_used": report.used_refs,
        "dropped_claims": report.dropped_claims,
        "invalid_refs": report.invalid_refs,
        "stripped_cites": report.stripped_cites,
        "tags": ["札记/问答归档"],
    }
    items = dict(managed)
    for k, v in preserved.items():
        if k not in managed:
            items[k] = v
    return _render_frontmatter(items)


# ---------------------------------------------------------------------------
# 回查：这条"空白"是真的吗
# ---------------------------------------------------------------------------

@dataclass
class GapHit:
    """一条 gap 被回查时命中的东西。`kind` ∈ `topic`（概念页）/ `evidence`（句级证据）。

    `title` 与 `snippet` **不是装饰**。句级通道的精确率实测只有约四成
    （5 条 gap 的 18 条命中里，明确相关的 4 条、明确无关的 9~11 条：
    「儿童 ECMO 治疗低氧性呼吸衰竭」「数学肿瘤学的机制学习综述」「隐私保护合成表格数据」
    这类混在里面）。只印裸 citekey 的话，读者无从判断，只能一条条点开——
    上千条提示累积下来的结果是整节被跳过，而这一节恰恰是 P4 相对 `notes_search`
    唯一的增量。带上标题一眼就能筛掉，噪音的代价从"点开一篇论文"降到"扫过一行"。

    `snippet`（匹配到的那句原文）还兼管另一件事：札记库里「局限与可质疑点」那一档
    highlight 大量是**否定句**（"全文 0 次出现 identifiability…"），而 embedding
    不编码否定——它们会被"库里有没有 X"的查询系统性捞上来，读起来像"库里有"，
    实际说的是"那篇里没有"。看到原句才分得清"有"和"没有"。
    """
    kind: str
    key: str
    score: float
    title: str = ""
    snippet: str = ""


# gap 的**否定脚手架**。逐级剥掉之后剩下的才是"库里到底有没有 X"里的那个 X。
#
# 为什么必须剥：gap 文本是**否定句**，而 embedding 不编码否定。
# 「本次证据没有…」这段共有脚手架 + 学术中文语域，对任意 highlight chunk 的余弦
# 本来就在 0.6 左右——拿原句回查等于在问"哪些句子长得像这句否定句"。
# 实测（bge-m3，8 条真实 gap + 8 条构造成"库里绝对没有"的 gap，全库 17775 条 highlight）：
#
#   原句回查：  真 gap top1 0.686~0.727 | 假 gap top1 0.533~0.683   ← **重叠，无解**
#   剥完回查：  真 gap top1 0.649~0.753 | 假 gap top1 0.334~0.641   ← 分开了
#
# 分级用**整词**而不是单字：`缺` `未` `到` 这种单字会啃掉领域词（`缺失机制` →
# `失机制`、`未观测混杂` → `观测混杂`），检索出来的东西比不回查还糟。
# 所以 `缺少|缺乏|欠缺` 在列而 `缺失` 不在，`没有|未见|未` 在列但只在句首位置生效。
_GAP_SCAFFOLD_STAGES = [
    re.compile(r"^(?:本次|这次|这一次|本轮|上面|上述|以上)(?:的)?"),
    re.compile(r"^(?:这\s*\d+\s*条|这批|这几条|该批|这些)"),
    re.compile(r"^(?:召回|检索|给出)(?:到)?(?:的)?"),
    re.compile(r"^(?:证据|材料|文献|结果|内容)(?:里|中|里面|当中|之中)?(?:的)?"),
    # 否定词。**裸「未」必须要求后面紧跟一个动词**，否则会把领域词 `未观测混杂`
    # 啃成 `观测混杂`（意思正好反过来，拿去检索是在找它的反面）。同理 `不含` 之类
    # 也只在句首独立成词时才算脚手架。这是剥离器最容易做歪的方向：
    # 它的每一条都在删字，删错一个字就把 query 变成了另一个问题。
    re.compile(r"^(?:没有一条|没有任何|没有|未见|未曾|从未|不曾|未能|没能|尚未|不含)"),
    re.compile(r"^未(?=讨论|涉及|覆盖|给出|提及|提到|包含|回答|说明|评估|报告|"
               r"考察|分析|检验|探讨|论及|研究|描述|做|作)"),
    re.compile(r"^(?:缺少|缺乏|欠缺)"),
    # 同上：裸「系统」会把 `系统性偏差` 啃成 `性偏差`。副词形式无歧义可以直接剥；
    # 裸「系统」用与「未」相同的手法——只在后面紧跟动词时才算脚手架
    # （`系统讨论` 是脚手架，`系统性偏差` 不是）。
    re.compile(r"^(?:专门|系统性地|系统地|具体|直接|明确|充分|真正|任何|详细)"),
    re.compile(r"^系统(?=讨论|涉及|覆盖|给出|提及|提到|包含|回答|说明|评估|报告|"
               r"考察|分析|检验|探讨|论及|研究|描述|做|作)"),
    re.compile(r"^(?:讨论|涉及|覆盖|给出|提及|提到|包含|回答|说明|评估|报告|"
               r"考察|分析|检验|探讨|论及|研究|描述)(?:到|过|了)?"),
    re.compile(r"^(?:关于|对于|针对)"),
    re.compile(r"^[，,、：:；;的\s]+"),
]
_GAP_TAIL_RE = re.compile(r"[。．.!！?？\s]+$")
# 剥到只剩两三个字（甚至空）就回退到原文：拿一个太短的 query 去检索会命中一堆噪音，
# 比不回查还糟，而 gap 写成「本次证据没有。」这种废话本来就不该产生任何提示。
_GAP_MIN_LEN = 4


def strip_gap_scaffold(gap: str) -> str:
    """剥掉 gap 的否定脚手架，留下它真正在问的那个正向内容。

    「本次证据没有专门系统讨论「MNAR 在观测数据上不可识别」这一根本问题。」
      → 「MNAR 在观测数据上不可识别」这一根本问题

    逐级、可重复地剥（`本次` → `证据` → `没有` → `专门` → `系统` → `讨论`），
    而不是写一条大正则：一条大正则改起来必然会滑向"再加一个单字"，
    而单字正是会啃掉领域词的那一档。剥完什么都不剩就原样返回。
    """
    raw = str(gap or "").strip()
    s = raw
    for _ in range(12):
        before = s
        for rx in _GAP_SCAFFOLD_STAGES:
            s = rx.sub("", s, count=1)
        if s == before:
            break
    s = _GAP_TAIL_RE.sub("", s).strip()
    return s if len(s) >= _GAP_MIN_LEN else raw


# 概念页通道的命中阈值。取 `topics.DEFAULT_MIN_SIM`（0.55，句级口径的实测分界，
# **不是** notes_search 的 0.65 那个 paper 级口径）。
#
# 实测（bge-m3，剥完脚手架的 8 真 + 8 假 gap × config/topics.yaml 的 8 页 question）：
#   真 gap 与概念页 top1： min 0.532  中位 0.708  max 0.816
#   假 gap 与概念页 top1： min 0.239  中位 0.454  max 0.499
#   阈值 0.50: 真 8/8 | 假 0/8
#   阈值 0.55: 真 7/8 | 假 0/8      ← 现状，保持
#   阈值 0.65: 真 6/8 | 假 0/8
# 两档之间完全不重叠——**这条通道本来就是干净的，本轮一个字不改**。
DEFAULT_GAP_TOPIC_MIN_SIM = DEFAULT_MIN_SIM

# 句级证据通道的命中阈值。**必须比概念页通道高**，而且必须配合脚手架剥离才成立。
#
# 首版与概念页通道共用 0.55，实测在"库里绝对没有"的 gap 上假阳性 **5/5**：
# 「AlphaFold 置信度校准」命中 0.651、「量子退火加速比」0.634、「中世纪欧洲农业史」
# 0.590、「NICU 喂养耐受性量表」0.673、「机器人抓取样本效率」0.675——
# 而真实 gap 的命中分是 0.699~0.728。**重叠，没有任何阈值能分开。**
# 这条防线存在的理由恰恰是"答错了还能核对，指错方向不会去核对"，
# 假阳性 5/5 等于用户按它去看 5 次白跑 5 次，比没有这条还坏。
#
# 剥掉否定脚手架（`strip_gap_scaffold`）之后重新标定
# （bge-m3，8 条真实 gap + 8 条构造成"库里绝对没有"的 gap，全库 17775 条 highlight）：
#   真 gap 句级 top1： min 0.649  中位 0.695  max 0.753
#   假 gap 句级 top1： min 0.334  中位 0.575  max 0.641
#   阈值 0.55: 真 8/8 | 假 5/8
#   阈值 0.60: 真 8/8 | 假 3/8
#   阈值 0.65: 真 7/8 | 假 0/8      ← 取这个
#   阈值 0.69: 真 4/8 | 假 0/8
#   阈值 0.75: 真 1/8 | 假 0/8
# 两档的实测边界是 0.641/0.649，中间只有 0.008 宽；取 0.65 是**偏向精确率**的选择：
# 这一节的价值全在"它响的时候值得去看"，多漏一条真 gap 只是少一行提示，
# 多响一条假 gap 会把整条防线的可信度打掉。
#
# ⚠️ 与 notes_search 的 0.65 数值相同**纯属巧合**：那个是 paper 级口径（标题+一句话），
# 这个是 highlight 级 + 剥完脚手架的名词短语。不要因为"都是 0.65"就把两处并到一起。
DEFAULT_GAP_EVIDENCE_MIN_SIM = 0.65

# 每条 gap 最多贴几条命中。从 2 提到 4：实测某条 gap 的**第 3 名**命中是
# `toye2025Benchmarking` 0.722，原句「全文 0 次出现 identifiability、ignorability、
# shadow variable、sensitivity analysis」逐字回答了那条 gap，也正是上一轮点名的
# 漏网证据——被 `top_n=2` 扔了。这是纯提示，多两行的代价可以忽略。
DEFAULT_GAP_TOP_N = 4


def _questioned(topic_specs: Sequence[TopicSpec]) -> List[TopicSpec]:
    """有 `question` 的那些 spec，**顺序保持不变**。

    `topic_vectors` 与 `recheck_gaps` 必须用**同一个**筛选谓词，否则第 j 行向量对不上
    第 j 个 spec——那种错不会抛异常，只会安安静静地把「相关概念页」指到隔壁那一页上。
    收敛成一个函数就是为了让这件事不可能发生。
    """
    return [s for s in (topic_specs or []) if getattr(s, "question", "")]


def topic_vectors(topic_specs: Sequence[TopicSpec], embed_client):
    """概念页 question 的向量矩阵，**算一次传下去**；拿不到返回 None。

    B7：一次运行里这 8 个 question 被 embed 三遍（查重一次、概念页比对一次、
    gap 回查一次）。不贵，但没有理由。

    返回的是**裸向量矩阵**而不是 `(specs, vecs)` 元组：调用方拿到元组之后极容易
    整个塞进 `recheck_gaps(topic_vecs=...)`，而那里要的是矩阵——`tvecs[j] @ v` 会
    拿 specs 列表去做矩阵乘，报一句看不懂的 `matmul: core dimension mismatch`。
    行序与 `_questioned(topic_specs)` 一致，两边共用那个谓词。
    """
    specs = _questioned(topic_specs)
    if not specs or embed_client is None:
        return None
    try:
        return embed_client.embed([s.question for s in specs])
    except EmbeddingError as exc:
        logger.warning("概念页问题 embedding 不可用：{}", exc)
        return None


def recheck_gaps(gaps: Sequence[str], *, store=None, embed_client=None,
                 topic_specs: Sequence[TopicSpec] = (),
                 topic_vecs=None,
                 min_sim: float = DEFAULT_GAP_TOPIC_MIN_SIM,
                 evidence_min_sim: float = DEFAULT_GAP_EVIDENCE_MIN_SIM,
                 exclude_citekeys: Optional[set] = None,
                 titles: Optional[Dict[str, str]] = None,
                 top_n: int = DEFAULT_GAP_TOP_N) -> Dict[str, List[GapHit]]:
    """每条 gap 拿**剥掉否定脚手架之后的正向内容**回查向量库与概念页：这个空白是真的吗？

    A1 的第二件事，也是真正拦住"指错方向"的那道。改口径（把「库里没有」改成
    「本次召回没覆盖到」）只是不再说谎；回查才回答用户真正要问的那个问题——
    「那我到底该不该去补这篇文献」。实测那一页的 gap #2 写着「库内证据显示这类讨论
    几乎完全缺失」，而反例 `chen2026Partial` 就躺在同一页的证据表里当 E12。

    **回查的 query 是 `strip_gap_scaffold(gap)` 而不是 gap 原句**：原句是否定句，
    embedding 不编码否定，拿它检索问的是"哪些句子长得像这句否定句"（实测假阳性
    5/5，见 `DEFAULT_GAP_EVIDENCE_MIN_SIM` 旁边的标定表）。剥完才是在问"库里有没有 X"。

    两条通道各有各的阈值：概念页 0.55（实测干净）、句级证据 0.65（剥完才成立）。
    **只提阈值不剥脚手架仍然分不开**，只剥不提阈值也一样——两件必须一起做。

    `exclude_citekeys` 排除本页证据表里已有的 citekey：线上那页 gap#2 留下的两条里，
    `zhang2026Newonset` 就是本页自己的 E4/E18/E20，「库里可能有」指回同一页下面 30 行。

    命中判定**同时覆盖句级证据与概念页**：只查句级会漏掉"库里有一整页在讲"这种
    最该被拦住的情形（概念页不在向量库里，见模块 docstring 的已知欠账）。

    成本：全部 gap **一次批量 embedding**（概念页向量可由 `topic_vectors` 预先算好
    传进来），不到一秒。拿不到 embedding 就**不回查**（返回空），绝不拿词面重合
    冒充——那正是 A3 里被实测证伪的那一档。
    """
    import numpy as np

    texts = [str(g) for g in (gaps or []) if str(g).strip()]
    if not texts or embed_client is None:
        return {}
    specs = _questioned(topic_specs)      # 与 topic_vectors 共用谓词，行序必须一致
    queries = [strip_gap_scaffold(t) for t in texts]
    need_topics = specs and topic_vecs is None
    try:
        vecs = embed_client.embed(
            queries + ([s.question for s in specs] if need_topics else []))
    except EmbeddingError as exc:
        logger.warning("gap 回查跳过（embedding 不可用）：{}", exc)
        return {}
    tvecs = vecs[len(queries):] if need_topics else topic_vecs
    mask = None
    if store is not None and getattr(store, "records", None):
        # 只认 highlight 级：paper 级 chunk 是"标题 + 一句话"，不能当"库里有证据"的凭据
        mask = np.array([r.get("level") == "highlight" for r in store.records], dtype=bool)
    skip = set(exclude_citekeys or ())

    out: Dict[str, List[GapHit]] = {}
    for i, gap in enumerate(texts):
        v = vecs[i]
        topics_hit: List[GapHit] = []
        if tvecs is not None:
            for j, spec in enumerate(specs):
                score = float(tvecs[j] @ v)
                if score >= min_sim:
                    topics_hit.append(GapHit("topic", spec.slug, score,
                                             title=spec.title))
        ev_hit: List[GapHit] = []
        if mask is not None and bool(mask.any()):
            try:
                # 本页已有的 citekey 也要排除，所以 top_k 要留够余量
                rows = store.search(v, mask=mask, top_k=max(16, top_n * 8))
            except Exception as exc:
                # B7：`store.search` 此前没被包住，而这个函数位于 LLM 调用**之后**——
                # 真抛了就是把已经付过费的整轮合成丢掉、什么都不落盘。
                logger.warning("gap 回查跳过（向量库检索失败 {}）：{}",
                               type(exc).__name__, exc)
                return {}
            seen: set = set()
            for idx, score in rows:
                if score < evidence_min_sim:
                    continue
                ck = store.records[idx].get("citekey")
                if not ck or ck in seen or ck in skip:
                    continue
                seen.add(ck)
                rec = store.records[idx]
                ev_hit.append(GapHit(
                    "evidence", ck, float(score),
                    title=(titles or {}).get(ck, ""),
                    # 匹配到的那句原文：句级通道会系统性地捞上「局限与可质疑点」
                    # 那一档的否定句，看到原句才分得清"库里有"还是"那篇里没有"。
                    snippet=(rec.get("text") or "").strip().replace("\n", " ")))
        topics_hit.sort(key=lambda h: -h.score)
        ev_hit.sort(key=lambda h: -h.score)
        # 概念页排在前面：「库里有一整页在讲这件事」比「有一句相关的话」更能改变决定
        out[gap] = topics_hit[:top_n] + ev_hit[:top_n]
    return out


# 概念页命中的阈值。这条会改变用户**要不要跑这次合成**的决定，提示错了就是白白劝退。
#
# 首版取 0.80，理由写着"问题与问题是同型文本，余弦分布比问题 vs 句级证据紧得多"。
# 方向对了，但被当成"所以阈值也要抬到 0.80"用——**地板抬高的是噪声，不是信号**。
# 实测（bge-m3，17 个真实提问 × config/topics.yaml 的 8 页 question + 7 个反例提问）：
#
#   正例 gold 分数： min 0.470  p25 0.681  中位 0.727  max 0.872   （gold 排名第一 16/17）
#   反例 top1 分数： min 0.296  中位 0.445  max 0.493
#
#   阈值 0.50: 正例 16/17 | 反例误报 0/7 | 误指错页 1
#   阈值 0.55: 正例 16/17 | 反例误报 0/7 | 误指错页 1
#   阈值 0.60: 正例 15/17 | 反例误报 0/7 | 误指错页 0   ← 取这个
#   阈值 0.70: 正例 10/17 | 反例误报 0/7 | 误指错页 0
#   阈值 0.80: 正例  4/17 | 反例误报 0/7 | 误指错页 0   ← 首版，整档打偏
#
# 关键洞察：**反例的地板不是 0**——bge-m3 对同语种中文提问的余弦下限在 0.30~0.50，
# 连「翻译质量怎么自动评估？」对 `mnar-diagnosis` 都有 0.468。0.80 把 13/17 个
# 真命中一起滤掉了，而它挡住的反例在 0.60 上本来就一个都不会响。
# 0.55 与 0.60 之间那一档差别是「掩码嵌入在 ICU 预测上能提升多少 AUROC？」
# 会被误指到 `icu-benchmarks`（0.572）而不是 `missingness-aware-modeling`（0.561）——
# 取 0.60 就是为了让"指错页"归零：指错页比不提示更坏，用户会去读错的那一页。
DEFAULT_TOPIC_MATCH_THRESHOLD = 0.60


def find_similar_topics(topic_specs: Sequence[TopicSpec], question: str, embed_client,
                        *, threshold: float = DEFAULT_TOPIC_MATCH_THRESHOLD,
                        top_n: int = 2, topic_vecs=None) -> List[Tuple[TopicSpec, float]]:
    """这个问题是不是已经有一页概念页在回答？按相似度降序返回。

    实测撞车的那次：问答页 40 条证据 / 45% 引用率 / 7 条论断，而 `mnar-diagnosis`
    是 70 条 / 100% / 34 条论断且随新论文自动重合成，23/40 个 citekey 是重合的——
    **同一个问题，库里已经有一页更好的答案，而首版没有任何一处告诉用户**。
    """
    specs = _questioned(topic_specs)      # 与 topic_vectors 共用谓词，行序必须一致
    if not specs or embed_client is None:
        return []
    # `topic_vecs` 给了就只 embed 问题本身（B7：这 8 个 question 一次运行里被 embed 三遍）
    want = [normalize_question(question)] + (
        [] if topic_vecs is not None else [s.question for s in specs])
    try:
        vecs = embed_client.embed(want)
    except EmbeddingError as exc:
        logger.warning("概念页比对跳过（embedding 不可用）：{}", exc)
        return []
    tvecs = topic_vecs if topic_vecs is not None else vecs[1:]
    scored = [(s, float(tvecs[i] @ vecs[0])) for i, s in enumerate(specs)]
    scored = [t for t in scored if t[1] >= threshold]
    scored.sort(key=lambda t: -t[1])
    return scored[:top_n]


# ---------------------------------------------------------------------------
# 查重：问之前先看看是不是问过了
# ---------------------------------------------------------------------------

@dataclass
class ArchivedQA:
    slug: str
    question: str
    title: str
    path: Path
    generated_at: str = ""
    first_asked_at: str = ""
    n_points: int = 0
    n_evidence: int = 0


def is_qa_page_file(path) -> bool:
    """`topics/qa/` 下这个文件是不是一页归档问答（按文件名判，不读内容）。

    与 `topics.is_topic_page_file` 同一条约定：`INDEX.md` 是目录页，`_` 前缀留给
    派生产物。这层是冗余防线——下面的扫描还会要求 frontmatter 里有 `qa` 键——
    但保留它，因为 lint 侧那次教训表明"纯文本扫 `[@key]` 的地方，文件名判据是唯一防线"。
    """
    name = Path(path).name
    return name.endswith(".md") and name != "INDEX.md" and not name.startswith("_")


def list_qa_pages(qa_dir) -> List[ArchivedQA]:
    """已归档的问答，按 `generated_at` 从新到旧。读不出/解析不出的跳过，绝不抛异常。"""
    out: List[ArchivedQA] = []
    for path in sorted(Path(qa_dir).glob("*.md")):
        if not is_qa_page_file(path):
            continue
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not isinstance(fm, dict) or not fm.get("qa"):
            continue
        out.append(ArchivedQA(
            slug=str(fm["qa"]), question=str(fm.get("question") or ""),
            title=str(fm.get("title") or fm.get("question") or fm["qa"]), path=path,
            generated_at=str(fm.get("generated_at") or ""),
            first_asked_at=str(fm.get("first_asked_at") or ""),
            n_points=int(fm["n_points"]) if isinstance(fm.get("n_points"), int) else 0,
            n_evidence=int(fm["n_evidence"]) if isinstance(fm.get("n_evidence"), int) else 0,
        ))
    out.sort(key=lambda q: q.generated_at, reverse=True)
    return out


# 词面重合度的告警下限。这是**提示**不是拦截，所以取得低一些：漏提示（问了个其实
# 问过的问题）的代价是白跑一次合成，误提示（提示了但其实不是同一个问题）的代价只是
# 多看一行字。两者不对称，所以宁可多提示。
DEFAULT_SIMILAR_THRESHOLD = 0.35

# 语义查重的下限。首版与 `DEFAULT_TOPIC_MATCH_THRESHOLD` 同取 0.80，同样没有实测依据。
# 实测（bge-m3，13 组人工标注的问题对）：
#
#   0.997 同题只差标点      0.974 同题加限定      0.872 / 0.816 同题换说法
#   0.756 同题换说法
#   0.661 同题换说法  ← 0.80 漏；**这正是 `question_similarity` docstring 拿来论证
#                        「必须换 embedding」的旗舰案例**（词面 Jaccard 0.107）
#   0.813 / 0.665 / 0.621 同主题不同角度（0.813 那条 docstring 说词面 0.421 是"误报"，
#                        要避免的就是它——换了 embedding 仍然误报）
#   0.980 语义相反（«缺失机制可检验吗» vs «…不可检验吗»；embedding 不编码否定）
#   0.475 / 0.431 / 0.408 完全不同
#
#   阈值 0.65: 同题 6/6 | 同主题不同角度误报 2/3 | 完全不同误报 1/4   ← 取这个
#   阈值 0.70: 同题 5/6 | 同主题不同角度误报 1/3 | 完全不同误报 1/4
#   阈值 0.80: 同题 4/6 | 同主题不同角度误报 1/3 | 完全不同误报 1/4   ← 首版
#   阈值 0.85: 同题 3/6 | 同主题不同角度误报 0/3 | 完全不同误报 1/4
#
# 真重问区间（0.661~0.997）与真不同角度（0.813）**重叠**，"完全不同"里那条 0.980
# 是否定句（结构性的，任何阈值都拦不住）——**不存在能干净分开的阈值**。
# 所以这一节本来就是**提示不是闸门**：取召回优先的 0.65，并且在一条都没过阈值时
# 仍打印 top-1 作为「参考（未达提示阈值）」（`DedupResult.below`）。
# 误提示的代价只是多看一行字；漏提示的代价是同一个问题被问第四次，
# 而这个模块存在的全部理由就是拦住那件事。
DEFAULT_SIMILAR_EMBED_THRESHOLD = 0.65


@dataclass
class DedupResult:
    """查重结果 + **它是怎么算出来的**。

    `mode` / `degraded` 不是装饰：静默降级会让用户以为语义查重跑过了，
    而词面档漏的正是三个月后真正会发生的那种重问。

    `below`：一条都没过阈值时的 top-1（带分数）。真重问与真不同角度的分数区间
    重叠、不存在干净的分界，所以过不过阈值都要让人看见最像的那一条——
    只是措辞上分成「你问过意思接近的」与「参考（未达提示阈值）」两档。
    过阈值时 `below` 为空：两份名单同时出现只会让人分不清哪条要紧。
    """
    hits: List[Tuple[ArchivedQA, float]] = field(default_factory=list)
    mode: str = "lexical"                 # embedding / lexical
    degraded: str = ""                    # 非空 = 本该走语义档但降级了，这是原因
    below: List[Tuple[ArchivedQA, float]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.hits)


def question_similarity(a: str, b: str) -> float:
    """两个问题的词面重合度（Jaccard，分词复用 `vault.tokenize`：英文词 + 中文 2-gram）。

    **这是降级兜底，不是首选**。实测过的盲区，一条都别指望它：

      - 真正的重问（换个说法问同一件事）分数 0.10~0.17，**全漏**：
        「在纵向电子健康记录上能否诊断非随机缺失？」vs
        「MNAR 诊断在纵向 EHR 上到底能不能做？」= 0.107；
      - 不同的问题反而高分：「EHR 上到底能不能做 MNAR 的敏感性分析？」= 0.421（误报）；
      - 短问题最糟，中文 2-gram 让「什么是」「取多少」这类脚手架吃掉大半 token：
        «什么是MAR» vs «什么是MNAR» = 0.500；
        «缺失机制可检验吗» vs «缺失机制不可检验吗» = **0.67**（语义相反却高分）；
      - **纯英文短问题分词后为空**（如 `AI vs ML?`），此时一律返回 0.0，
        查重**永远静默不响**。

    首选走 embedding（`find_similar_questions` 传 `embed_client` 即可）：问答召回
    本来就要连 Ollama，对已归档的几十个问题多算一次相似度几乎不加成本。
    """
    ta, tb = set(tokenize(a or "")), set(tokenize(b or ""))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_similar_questions(qa_dir, question: str, *,
                           threshold: Optional[float] = None,
                           top_n: int = 3,
                           exclude_slug: str = "",
                           embed_client=None) -> DedupResult:
    """已归档问答里与 `question` 接近的几条，按相似度降序。

    给了 `embed_client` 就走**语义**档（一次批量 embedding：新问题 + 全部已归档问题），
    拿不到就降级回词面 Jaccard 并在 `DedupResult.degraded` 里说明原因——
    降级本身不是问题，静默降级才是。

    `exclude_slug` 用来排除"这就是同一个问题、本来就要原地更新的那一页"——
    没有它的话，每次更新一页都会提示"你问过一个 100% 相似的问题"，纯噪音。
    """
    norm = normalize_question(question)
    pages = [p for p in list_qa_pages(qa_dir)
             if not (exclude_slug and p.slug == exclude_slug)]
    if not pages:
        return DedupResult(mode="embedding" if embed_client is not None else "lexical")

    degraded = ""
    if embed_client is not None:
        try:
            vecs = embed_client.embed([norm] + [(p.question or p.title) for p in pages])
        except EmbeddingError as exc:
            degraded = "Ollama embedding 不可用（{}），查重已降级为词面重合".format(exc)
        else:
            th = DEFAULT_SIMILAR_EMBED_THRESHOLD if threshold is None else threshold
            scored = [(p, float(vecs[i + 1] @ vecs[0])) for i, p in enumerate(pages)]
            scored.sort(key=lambda t: -t[1])
            hits = [t for t in scored if t[1] >= th]
            # 一条都没过阈值时仍给出最像的那一条当参考：两个区间实测重叠，
            # 阈值只能决定措辞的分量，不该决定"给不给看"。
            return DedupResult(hits=hits[:top_n], mode="embedding",
                               below=[] if hits else scored[:1])

    th = DEFAULT_SIMILAR_THRESHOLD if threshold is None else threshold
    scored = [(p, question_similarity(norm, p.question or p.title)) for p in pages]
    scored.sort(key=lambda t: -t[1])
    hits = [t for t in scored if t[1] >= th]
    return DedupResult(hits=hits[:top_n], mode="lexical", degraded=degraded,
                       below=[] if hits else [t for t in scored[:1] if t[1] > 0])


# ---------------------------------------------------------------------------
# 落盘与目录页
# ---------------------------------------------------------------------------

def qa_dir_for(notes_dir) -> Path:
    return Path(notes_dir) / TOPICS_DIRNAME / QA_DIRNAME


def qa_assemble(fm: str, gen_block: str, user_zone: str, *,
                generator: str = QA_GENERATOR) -> str:
    """三段拼装，哨兵里写的是 **qa 自己的** `QA_SCHEMA_VERSION`。

    与 `topics.assemble` 逐字同形（`vault.GEN_BEGIN_RE` 两边通吃），只差版本号那个位置：
    首版沿用 `TOPIC_SCHEMA_VERSION`，于是"这一页是哪一版 qa 防线产的"这个信息
    根本没被记下来过。
    """
    body = gen_block.rstrip("\n")
    return "{}\n\n{}\n{}\n{}\n\n{}\n".format(
        fm, GEN_BEGIN.format(QA_SCHEMA_VERSION, _gen_hash(body), generator), body,
        GEN_END, user_zone.strip("\n"))


def merge_qa_page(fm: str, gen_block: str, existing: Optional[str], *,
                  generator: str = QA_GENERATOR) -> Tuple[Optional[str], str]:
    """合并生成块与用户批注。conflict 时返回 None，调用方不得覆盖（同 vault/topics 约定）。

    与 `topics.merge_topic_page` 唯一的差别是走 `qa_assemble`。判 conflict 的三条
    （frontmatter 坏了 / 哨兵没了 / 生成块被手改）逐条复用 vault 那三个函数本身，
    不另写一份判据——`GEN_END` 两边各维护一份字面量那次（G6）就是这么栽的。
    """
    if existing is None:
        return qa_assemble(fm, gen_block, DEFAULT_USER_ZONE, generator=generator), "new"
    _fm, body = split_frontmatter(existing)
    if _fm is None:
        return None, "conflict"
    user_zone = extract_user_zone(body)
    if user_zone is None:
        return None, "conflict"
    if generated_block_tampered(body):
        return None, "conflict"
    return qa_assemble(fm, gen_block, user_zone, generator=generator), "merged"


def archived_question(qa_dir, slug: str) -> str:
    """某个 slug 上已归档的那个问题（没有就空串）。CLI 报"这个 slug 已属于别人"时要用。"""
    _text, fm, _prev = read_existing(Path(qa_dir) / "{}.md".format(slug))
    return str((fm or {}).get("question") or "")


def write_qa_page(qa_dir, slug: str, question: str, qa: dict, evidences,
                  report: ValidationReport, *, dry_run: bool = False,
                  related_topics: Sequence[str] = (),
                  nearby_papers: Sequence[str] = (),
                  titles: Optional[Dict[str, str]] = None,
                  now: Optional[datetime] = None) -> Tuple[Path, str]:
    """落盘 `topics/qa/<slug>.md`，走与概念页同一套哨兵合并。返回 `(路径, 状态)`。

    状态：`new` / `merged` / `conflict` / `unchanged` / `taken`。
    走哨兵而不是直接覆盖：这一页天然会被人在旁边写批注（"这个答案我核对过，第 3 条不对"），
    那正是它的用法。生成块被手改时报 conflict 而不是强行覆盖，同 vault 约定。

    落盘前**重读一次**再合并（同 `topics.build_topic` 的 W1/X1）：LLM 合成要几十秒到
    几分钟，用户完全可能在这期间往 Obsidian 里写批注或加 frontmatter 键；用调用前的
    旧快照去合并会把这段时间的改动静默覆盖，而状态照样显示正常的 `merged`。

    **`taken`**：这个 slug 上已经躺着**另一个问题**的整页答案。哈希碰撞概率很低，
    但显式 `--slug` 复用完全可能——而 `--verify` 的报错文案恰恰在教用户手打 `--slug`。
    实测后果：状态 `merged`、图标 ✅、退出码 0，甲的整块答案被替换，`first_asked_at`
    却继承自甲，页面看起来像一段连续历史。所以这里宁可拒绝也不合并。
    """
    now = now or datetime.now()
    qa_dir = Path(qa_dir)
    path = qa_dir / "{}.md".format(slug)
    fresh_existing, fresh_fm, _prev = read_existing(path)
    prev_q = (fresh_fm or {}).get("question")
    if prev_q and question_key(prev_q) != question_key(question):
        return path, "taken"
    gen_block = render_qa_block(question, qa, evidences, related_topics=related_topics,
                                titles=titles,
                                nearby_papers=nearby_papers)
    fm = build_qa_frontmatter(question, slug, qa, evidences, report,
                              now.isoformat(timespec="seconds"), preserved=fresh_fm)
    content, status = merge_qa_page(fm, gen_block, fresh_existing, generator=QA_GENERATOR)
    if content is None:
        return path, "conflict"
    if dry_run:
        return path, status
    qa_dir.mkdir(parents=True, exist_ok=True)
    return path, (status if write_if_changed(path, content) else "unchanged")


def previous_answer(qa_dir, slug: str, question: Optional[str] = None) -> str:
    """上一版的生成块正文，喂给 prompt 当修订参考。不存在给空串。

    `question` 给了就先校验这一页确实是**同一个问题**的历史。不校验的后果是最坏的
    那一条：prompt 铁律写着「有上一版答案时把它当草稿修订：保留仍被证据支持的」，
    于是显式复用 slug 时模型会去**融合两个不相干的问题**。`write_qa_page` 的
    `taken` 早退救不了这个——prompt 在那之前就已经拼好并发出去了。
    """
    _text, fm, prev = read_existing(Path(qa_dir) / "{}.md".format(slug))
    if question is not None:
        prev_q = (fm or {}).get("question")
        if prev_q and question_key(prev_q) != question_key(question):
            return ""
    return prev


def render_qa_index(qa_dir) -> str:
    """`topics/qa/INDEX.md`：归档问答目录，按最近更新排序。

    **扫描磁盘现状而不是只看本次运行的结果**（同 `topics.render_topics_index` 的教训）：
    单跑一个问题时若只把这一条写进目录，其余问答会从目录里整体消失——文件还在，
    但没人找得到，而这个模块存在的全部理由就是让答案找得到。
    """
    pages = list_qa_pages(qa_dir)
    L = ["# 问答归档", "",
         "由 `scripts/ask_notes.py` 生成。每页是一次检索合成的问答，论断均锚定到札记库的"
         "句级证据；同一个问题再问一次会**原地更新**对应页面，不会堆出第二份答案。", "",
         "⚠️ 归档问答**不在向量库里**，`notes_search.py` 搜不到它们（原因见 "
         "`src/scholar/qa.py` 模块 docstring 的「已知局限」）。找旧问答就看这一页、"
         "或在 Obsidian 的 `02-主题/问答/` 里全文搜。", ""]
    if not pages:
        L += ["（还没有归档过任何问答。）", ""]
        return "\n".join(L)
    L += ["共 {} 条。".format(len(pages)), "",
          "| 问题 | 依据 | 证据 | 首次问 | 最近更新 |", "|---|---|---|---|---|"]
    for q in pages:
        L.append("| [{}]({}.md) | {} | {} | {} | {} |".format(
            (q.title or q.slug).replace("|", "\\|"), q.slug,
            q.n_points, q.n_evidence,
            (q.first_asked_at or "—")[:10], (q.generated_at or "—")[:10]))
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# vault 同步
# ---------------------------------------------------------------------------

def sync_qa_to_vault(notes_dir, vault_dir, filenames: Dict[str, str], *,
                     dry_run: bool = False) -> Dict[str, Any]:
    """`topics/qa/*.md` -> `vault/02-主题/问答/*.md`（citekey 转 wiki 链接）。

    独立于 `topics.sync_topics_to_vault` 而不是塞进它：那个函数已经过多轮加固
    （prune 的"读不出 ≠ 已下线"、退役页标记、生成块为空的处理），为 qa 再加一层
    分支只会让它更难读，而两者的规则并不完全一样——**qa 页没有"退役"概念**
    （问题不会从配置里被删掉），也**不做 prune**：一次问答归档之后就是历史，
    源文件被人删掉不代表 vault 那份该跟着消失（那可能正是用户手动整理的结果）。
    """
    src_dir = Path(notes_dir) / TOPICS_DIRNAME / QA_DIRNAME
    out_dir = Path(vault_dir) / "02-主题" / VAULT_QA_DIR
    report: Dict[str, Any] = {"synced": 0, "new": 0, "merged": 0, "unchanged": 0,
                              "conflicts": [], "skipped": []}
    if not src_dir.exists():
        return report
    for src in sorted(src_dir.glob("*.md")):
        if not is_qa_page_file(src):
            continue
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as exc:
            report["skipped"].append("{}（读取失败：{}）".format(src.name, type(exc).__name__))
            continue
        fm, body = split_frontmatter(raw)
        if not isinstance(fm, dict) or not fm.get("qa") or not isinstance(body, str):
            report["skipped"].append(src.name)
            continue
        end = body.find(GEN_END)
        gen = body[:end] if end >= 0 else body
        gen = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", gen.strip(), flags=re.S)
        if not gen:
            report["skipped"].append(src.name)
            continue
        slug = str(fm["qa"])
        fm_v = dict(fm)
        fm_v["cssclasses"] = fm.get("cssclasses") or ["qa-page"]
        content, status = merge_qa_page(
            _render_frontmatter(fm_v), to_wiki_links(gen, filenames),
            (out_dir / "{}.md".format(slug)).read_text(encoding="utf-8")
            if (out_dir / "{}.md".format(slug)).exists() else None,
            generator=QA_GENERATOR)
        if content is None:
            report["conflicts"].append(slug)
            continue
        if dry_run:
            report[status if status in ("new", "merged") else "unchanged"] += 1
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        if write_if_changed(out_dir / "{}.md".format(slug), content):
            report["synced"] += 1
            report["new" if status == "new" else "merged"] += 1
        else:
            report["unchanged"] += 1
    return report


# ---------------------------------------------------------------------------
# 自检：归档页上的引用还追得回去吗
# ---------------------------------------------------------------------------

@dataclass
class QAAudit:
    slug: str
    n_cites: int = 0
    dead_keys: List[str] = field(default_factory=list)
    n_evidence: int = 0
    unanchored: List[str] = field(default_factory=list)
    # 生成块正文里残留的证据编号（`E31 描述的算法`）。见下面 `audit_qa_pages`。
    residual_refs: List[str] = field(default_factory=list)
    # 哨兵里记的防线版本号。老页面（首版写的是 TOPIC_SCHEMA_VERSION）会是 1。
    schema_version: Optional[int] = None
    # 这一页的问题原文。**不是装饰**：`--verify` 给的重跑命令原来要人手填
    # `<那一页的问题>`，而问题就在 frontmatter 里躺着；100 页时这一步直接劝退。
    question: str = ""
    # 非空 = 文件名与问题对不上，值是这个问题**该有的** slug。见 `audit_qa_pages`。
    slug_mismatch: str = ""

    @property
    def ok(self) -> bool:
        return (not self.dead_keys and not self.unanchored and not self.residual_refs
                and not self.slug_mismatch)

    @property
    def outdated(self) -> bool:
        """这一页是**旧防线**产的。不是错误，是一条事实。"""
        return (self.schema_version or 0) < QA_SCHEMA_VERSION

    @property
    def needs_rerun(self) -> bool:
        """真的值得花 90 秒重跑一次 LLM 吗。

        B6：首版拿 `outdated` 当告警轴，而"代码变了"是**错的陈旧轴**——
        v3 一涨，100 页全标 ⚠️，其中绝大多数内容毫无问题；反过来一页今天生成的
        答案基于 8 篇论文，半年内库里进了几十篇同主题文献、它实质上早就过时了，
        `--verify` 却会一直显示 `防线 v2 ✅`。**它会在不该响时全响，在该响时全绿。**

        成本这一侧也不允许乱响：回退链实测只剩订阅那一路（`LLM__GEMINI_API_KEY`
        是空的、DeepSeek 欠费），100 页 × 90 秒 ≈ 2.5 小时且会触顶。

        所以真正的判据是「**本地扫不出来**的差异」：`audit_qa_pages` 已经能本地
        扫出死键/失锚/残留编号/文件名不符——凡是能本地判定的缺陷，就不该靠
        "重跑一次 LLM"来修（前三样重跑确实能修，但那是顺带，不是理由）。
        本地自检干净的旧页只报一行信息，不催重跑。

        真正的陈旧轴（"自 generated_at 以来该主题新增了 N 篇"）是零 LLM 的、
        可以复用向量库 + index 的 year/month 算出来——**本轮没做，记在这里**。
        """
        return self.outdated and not self.ok


# 证据表那一段的小标题。残留编号只扫**它之前**的正文：证据表每一行本身就长成
# `- ● **E1** …`，连表一起扫会把每一页都报成满页残留。
_EVIDENCE_HEADING = "## 本页证据"


def audit_qa_pages(qa_dir, index: dict) -> List[QAAudit]:
    """与 `topics.audit_topic_pages` 同口径的自检：死键 + 失锚 + **残留编号** + 防线版本。

    问答归档比概念页更需要这个：概念页随新论文自动重合成，改过的 citekey 迟早会被
    刷掉；而问答页只在人再问一次时才更新，**一页三个月前的问答可能一直挂着一个
    半年前就改过名的 citekey**，粘进 pandoc 会渲染成 `(key?)`。

    `residual_refs` 是 `topics.PageAudit.bare_cites`（G1）的同型防线，换了个通道：
    那一条盯的是"逃过回译的引用"，这一条盯的是"逃过回译的**编号**"。归档页是
    **生成它那一刻的代码**的快照——线上首页正文里那四处 `E1/E9/E31/E36` 就是因为
    页面比防线早生成五分钟，而当时所有仪表都是绿的。
    """
    alive = {p["citekey"] for p in (index.get("papers") or [])
             if isinstance(p, dict) and p.get("citekey") and not p.get("duplicate_of")}
    hl_by_key: Dict[str, set] = {}
    for p in (index.get("papers") or []):
        if isinstance(p, dict) and p.get("citekey"):
            hl_by_key.setdefault(p["citekey"], set()).update(
                (h.get("text") or "").strip().replace("\n", " ")
                for h in (p.get("highlights") or []) if isinstance(h, dict))
    out: List[QAAudit] = []
    for path in sorted(Path(qa_dir).glob("*.md")):
        if not is_qa_page_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("qa"):
            continue
        a = QAAudit(slug=str(fm["qa"]), question=str(fm.get("question") or ""))
        # A1：`--verify` 此前检查了死键/失锚/残留编号/防线版本四件事，**唯独没检查
        # 「这一页的文件名还是不是它自己问题该有的名字」**——于是上一轮改 digest 口径
        # 亲手造出来的孤儿页在所有仪表上都是绿的（磁盘 `qa-mnar-ehr-1f424623`，
        # 按它自己的 question 重算是 `qa-mnar-ehr-82bb128f`）。孤儿页不再被任何自动
        # 路径更新：重问一次会另存一份，`--list` 并排列出两条一模一样的问题。
        #
        # 这里传 `qa_dir` 是关键：`qa_slug` 的旧键回退会先认领存量页，所以被回退
        # 收编回来的 v1 页面**不该**再被报一次（那是纯噪音）。剩下会红的只有真正
        # 脱轨的那种——多半是手打 `--slug` 起的名字，重问时不带 `--slug` 就会分叉。
        if a.question:
            try:
                want = qa_slug(a.question, qa_dir=qa_dir)
            except TopicError:
                want = ""
            if want and want != a.slug:
                a.slug_mismatch = want
        keys = _CITE_RE.findall(text)
        a.n_cites = len(keys)
        a.dead_keys = sorted({k for k in keys if k not in alive})
        rows = _EVIDENCE_ROW_RE.findall(text)
        a.n_evidence = len(rows)
        a.unanchored = ["{}({})".format(ref, key) for _m, ref, key, quote in rows
                        if quote.strip() not in hl_by_key.get(key, set())]
        # 只看生成块（`GEN_END` 之前）、且只看证据表之前的正文：用户在「我的批注」里
        # 写「E1 那条我核对过」完全正常，拿它去判生成块有问题是误报。
        gen = body if isinstance(body, str) else ""
        end = gen.find(GEN_END)
        if end >= 0:
            gen = gen[:end]
        cut = gen.find(_EVIDENCE_HEADING)
        prose = gen[:cut] if cut >= 0 else gen
        a.residual_refs = sorted(
            {"E{}".format(int(m.group(1))) for m in _INLINE_REF_RE.finditer(prose)},
            key=lambda r: int(r[1:]))
        for line in gen.splitlines():
            m = GEN_BEGIN_RE.match(line.strip())
            if m:
                a.schema_version = int(m.group(1))
                break
        out.append(a)
    return out
