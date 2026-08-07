# -*- coding: utf-8 -*-
"""research_profile 单点化回归测试。

契约：
1. 默认 profile（config/research_profile.yaml 存在且未覆盖）渲染出的
   filter-v3 prompt 纳入/排除维度段落与重构前逐字等价——filter-v3 裁决行为不变；
2. 自定义 yaml 注入生效，且未覆盖的字段仍回落内嵌默认（深度合并）；
3. profile 文件缺失时整体回退内嵌默认，不炸；
4. research_interests 的 env > yaml > 内嵌默认优先级保持有效。
"""
from pathlib import Path

import pytest

from src.scholar import research_profile as rp_module
from src.scholar.paths import repo_path
from src.scholar.research_profile import (
    get_profile,
    get_default_research_interests,
    get_inclusion_dims,
    get_exclusion_dims,
    get_covid_safeguard_keywords,
    get_omics_ehr_signal_keywords,
    get_contamination_example_terms,
    get_whitelist_keywords,
    get_journal_blacklist_categories,
    get_bucket_labels,
)
from src.scholar.schema import ScholarSettings

# 重构前散落在 schema.py / whitelist_filter_prompt.md / journal_screen.py / pdf_ingest.py
# 里的旧方向文本，独立于 research_profile.py 的实现固定下来，防止实现和 fixture 一起漂移。
FIXTURE_INTERESTS = (
    "论文主题：EHR 缺失机制（MNAR）与「缺失条件依赖结构」的跨中心可迁移性；"
    "定位为 causal hypothesis generation，不做 causal identification。"
    "基础工作：MA-GCT（特征专属可学习掩码嵌入 + Guide/Prior 注意力约束）。"
)

FIXTURE_INCLUSION_DIMS = (
    "# 纳入维度（命中任一 → 进入候选池）\n"
    "A. 缺失机制方法学：MNAR / MAR、missingness mechanism diagnosis、\n"
    "   Little's test、informative missingness、missingness indicator、\n"
    "   missingness as signal / mask、imputation distortion。\n"
    "B. 缺失感知建模：missingness-aware architecture、mask embedding、\n"
    "   learnable mask token、attention with missing input、\n"
    "   graph learning with attribute missing、feature propagation。\n"
    "C. 缺失 × 因果：missingness graph (m-graph)、MNAR identifiability、\n"
    "   causal discovery under missing data、test-order / measurement confounding、\n"
    "   missingness indicators as DAG nodes。\n"
    "D. 跨域/跨中心迁移：transportability、domain shift / dataset shift、\n"
    "   external validation across databases、LODO、site heterogeneity、\n"
    "   distribution shift in EHR。\n"
    "E. 对抗性证据（**必须捕获，不得因“结论与本文相反”而排除**）：\n"
    "   声称因果特征集不提升泛化、causal features vs. all features、\n"
    "   attention ≠ causality、可解释性权重的可靠性质疑。\n"
    "F. venue 基准：多库大规模 ICU 预测验证（MIMIC-IV / eICU / AmsterdamUMCdb / HiRID）。\n"
    "G. 临床落地场景：sepsis subphenotype、AKI、CKD/透析、ICU 死亡率预测。"
)

FIXTURE_EXCLUSION_DIMS = (
    "# 排除维度（命中 → drop，除非同时强命中 A/C/E）\n"
    "X1. 纯影像 / 基因组 / 单细胞 / 蛋白组，无表格型 EHR。\n"
    "X2. 时序填补方法本身的增量改进（新 imputer + 更低 RMSE），无下游/机制分析。\n"
    "X3. 综述/观点文，且不提供可引用的定量证据或分类框架。\n"
    "X4. LLM/foundation model 通用能力，未涉及缺失或迁移。\n"
    "X5. RCT / 流行病学关联研究，无方法学贡献。\n"
    "X6. 因果推断纯理论（do-calculus、identification proofs），无缺失或无实证。\n"
    "X7. 会议 workshop 短文、无实验的 preprint stub。"
)

FIXTURE_COVID_KEYWORDS = [
    "EHR", "electronic health record", "missingness", "MNAR",
    "causal", "multi-site", "multi-center", "external validation",
    "transportability", "missing data", "imputation",
]

FIXTURE_OMICS_KEYWORDS = [
    "EHR", "electronic health record", "clinical predict",
    "missing data", "missingness", "MNAR", "imputation",
    "multi-site", "multi-center", "external validation",
]

FIXTURE_CONTAMINATION_TERMS = ["MNAR", "缺失指纹", "跨中心迁移"]

# 重构前散落在 settings.py ProcessingSettings.whitelist 里的 52 词白名单，
# 独立于实现固定下来，防止实现和 fixture 一起漂移。
FIXTURE_WHITELIST_KEYWORDS = [
    "EHR", "Electronic Health Record", "EMR", "Electronic Medical Record",
    "Clinical Prediction", "Predictive Model", "Risk Prediction", "Clinical Decision Support",
    "MNAR", "MAR", "missing not at random", "missingness", "informative missingness",
    "missing data", "imputation", "mask embedding", "missingness-aware", "missingness indicator",
    "missingness graph", "m-graph", "causal discovery", "causal structure", "identifiability",
    "transportability", "domain shift", "dataset shift", "distribution shift",
    "external validation", "LODO", "site heterogeneity", "generalization",
    "GNN", "Graph Neural Network", "Graph Convolutional", "attention", "feature propagation",
    "LLM", "Large Language Model", "foundation model", "Transformer",
    "Semi-supervised", "Active Learning",
    "MIMIC", "eICU", "AmsterdamUMCdb", "HiRID", "ICU",
    "sepsis", "AKI", "acute kidney injury", "CKD", "mortality prediction",
]

# 重构前散落在 journal_screen.py JOURNAL_BLACKLIST_CATEGORIES 里的 A-E 五类词表。
FIXTURE_JOURNAL_BLACKLIST_CATEGORIES = {
    "A-医学影像视觉": [
        "image", "imaging", "neuroimaging",
        "vision", "visual", "segmentation",
        "radiolog", "radiograph", "radiomic",
        "CT scan", "X-ray", "mammograph", "ultrasound", "ultrasonograph",
        "echocardiograph", "fundus", "retinal", "retinopath", "ophthalm",
        "dermatoscop", "dermoscop", "dermatolog",
        "histolog", "histopatholog", "pathology image",
        "endoscop", "colonoscop", "angiograph", "tomograph",
        "MRI", "magnetic resonance", "PET/CT", "SPECT",
        "photoacoustic", "hyperspectral",
    ],
    "B-可穿戴传感器移动健康": [
        "wearable", "accelerometer", "smartwatch",
        "photoplethysmograph", "PPG",
        "motion sensor", "activity track", "gait",
        "step count", "sleep track",
        "digital biomarker", "digital phenotyp",
        "smartphone sensor", "mobile health app",
        "consumer wearable",
    ],
    "C-纯NLP_LLM无临床深度": [
        "chatbot", "conversational agent", "question answering",
        "medical dialogue", "sentiment analysis",
        "social media", "tweet", "clinical note generation",
        "note generation", "clinical text summariz",
    ],
    "D-纯组学测序": [
        "genom", "proteom", "single-cell", "single cell RNA",
        "scRNA-seq", "microbio", "metagenom", "metabolom",
        "transcriptom", "epigenom",
    ],
    "E-明显不相关": [
        "blockchain", "3D print",
    ],
}

# 重构前散落在 vault.py BUCKET_LABEL 里的 A-G 维度中文名。
FIXTURE_BUCKET_LABELS = {
    "A": "缺失机制方法学", "B": "缺失感知建模", "C": "缺失与因果",
    "D": "跨域跨中心迁移", "E": "对抗性证据", "F": "多库ICU基准", "G": "临床落地场景",
}


# ---------------------------------------------------------------- 默认 profile


def test_default_interests_matches_pre_refactor_text():
    assert get_default_research_interests() == FIXTURE_INTERESTS


def test_default_inclusion_dims_matches_pre_refactor_text():
    assert get_inclusion_dims() == FIXTURE_INCLUSION_DIMS


def test_default_exclusion_dims_matches_pre_refactor_text():
    assert get_exclusion_dims() == FIXTURE_EXCLUSION_DIMS


def test_default_journal_screen_keyword_lists_match_pre_refactor_text():
    assert get_covid_safeguard_keywords() == FIXTURE_COVID_KEYWORDS
    assert get_omics_ehr_signal_keywords() == FIXTURE_OMICS_KEYWORDS


def test_default_pdf_ingest_contamination_terms_match_pre_refactor_text():
    assert get_contamination_example_terms() == FIXTURE_CONTAMINATION_TERMS


def test_default_whitelist_keywords_match_pre_refactor_text():
    """settings.py ProcessingSettings.whitelist 外置后，默认值须与重构前逐字一致
    （包括顺序——keyword 模式预筛命中优先级依赖顺序）。"""
    assert get_whitelist_keywords() == FIXTURE_WHITELIST_KEYWORDS
    assert len(get_whitelist_keywords()) == 52


def test_default_journal_blacklist_categories_match_pre_refactor_text():
    """journal_screen.py JOURNAL_BLACKLIST_CATEGORIES 外置后，A-E 五类词表须逐字一致。"""
    assert get_journal_blacklist_categories() == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES


def test_default_bucket_labels_match_pre_refactor_text():
    """vault.py BUCKET_LABEL 外置后，A-G 维度中文名须逐字一致。"""
    assert get_bucket_labels() == FIXTURE_BUCKET_LABELS


def test_default_profile_renders_filter_prompt_verbatim_equivalent():
    """把 whitelist_filter_prompt.md 的占位符按默认 profile 渲染，与重构前的整段
    纳入/排除维度文本逐字等价（含标题行、缩进、顿号，一个字符都不能差）。"""
    template = repo_path("config/prompts/whitelist_filter_prompt.md").read_text(encoding="utf-8")
    rendered = template.replace("{{INCLUSION_DIMS}}", get_inclusion_dims())
    rendered = rendered.replace("{{EXCLUSION_DIMS}}", get_exclusion_dims())

    # 重构前 lines 16-42 的整段原文（含标题行与中间空行），逐字重建后核对
    pre_refactor_block = FIXTURE_INCLUSION_DIMS + "\n\n" + FIXTURE_EXCLUSION_DIMS
    rendered_block_start = rendered.index("# 纳入维度")
    rendered_block_end = rendered.index("# 危险信号标记")
    rendered_block = rendered[rendered_block_start:rendered_block_end].rstrip("\n")
    assert rendered_block == pre_refactor_block


# ---------------------------------------------------------------- 自定义 yaml 注入


def test_custom_yaml_overrides_take_effect(tmp_path):
    custom = tmp_path / "custom_profile.yaml"
    custom.write_text(
        "interests: 换了个新方向：只关心多模态时序对齐。\n"
        "journal_screen:\n"
        "  covid_safeguard_keywords:\n"
        "    - \"foo\"\n"
        "    - \"bar\"\n"
        "pdf_ingest:\n"
        "  contamination_example_terms:\n"
        "    - \"新术语A\"\n",
        encoding="utf-8",
    )

    profile = get_profile(path=custom)
    assert profile["interests"] == "换了个新方向：只关心多模态时序对齐。"
    assert get_covid_safeguard_keywords(path=custom) == ["foo", "bar"]
    assert get_contamination_example_terms(path=custom) == ["新术语A"]

    # 未在自定义 yaml 里覆盖的字段（inclusion_dims / omics_ehr_signal_keywords）
    # 深度合并回落内嵌默认，不因为部分覆盖而整体丢失
    assert get_inclusion_dims(path=custom) == FIXTURE_INCLUSION_DIMS
    assert get_omics_ehr_signal_keywords(path=custom) == FIXTURE_OMICS_KEYWORDS

    # 新三键（whitelist_keywords / journal_blacklist_categories / bucket_labels）
    # 完全未在自定义 yaml 里出现，同样要整体回落内嵌默认
    assert get_whitelist_keywords(path=custom) == FIXTURE_WHITELIST_KEYWORDS
    assert get_journal_blacklist_categories(path=custom) == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES
    assert get_bucket_labels(path=custom) == FIXTURE_BUCKET_LABELS


def test_custom_yaml_overrides_new_keys_take_effect(tmp_path):
    """whitelist_keywords / journal_blacklist_categories（嵌套 dict-of-list）/
    bucket_labels（嵌套 dict-of-str）三种不同形状都要能被自定义 yaml 覆盖，
    且未覆盖的子键/同级键仍深度合并回落内嵌默认。"""
    custom = tmp_path / "custom_new_keys.yaml"
    custom.write_text(
        "whitelist_keywords:\n"
        "  - \"新方向关键词A\"\n"
        "  - \"新方向关键词B\"\n"
        "journal_blacklist_categories:\n"
        "  \"A-医学影像视觉\":\n"
        "    - \"仅覆盖A类\"\n"
        "bucket_labels:\n"
        "  A: \"新维度A名\"\n",
        encoding="utf-8",
    )

    assert get_whitelist_keywords(path=custom) == ["新方向关键词A", "新方向关键词B"]

    categories = get_journal_blacklist_categories(path=custom)
    assert categories["A-医学影像视觉"] == ["仅覆盖A类"]
    # 未覆盖的 B-E 类仍回落内嵌默认（dict-of-list 的深度合并）
    assert categories["B-可穿戴传感器移动健康"] == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES["B-可穿戴传感器移动健康"]
    assert categories["D-纯组学测序"] == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES["D-纯组学测序"]

    labels = get_bucket_labels(path=custom)
    assert labels["A"] == "新维度A名"
    # 未覆盖的 B-G 仍回落内嵌默认（dict-of-str 的深度合并）
    for k in "BCDEFG":
        assert labels[k] == FIXTURE_BUCKET_LABELS[k]


def test_journal_blacklist_categories_list_filters_non_str_and_warns(tmp_path, monkeypatch):
    """journal_blacklist_categories 是 dict-of-list：某一类词表混入非字符串元素时，
    只过滤该元素并 warn，其它类目不受影响——与 journal_screen 顶层 dict-of-list 键同款兜底。"""
    warnings = []

    class _FakeLogger:
        def warning(self, msg, *a, **kw):
            warnings.append(str(msg))

        def info(self, *a, **kw):
            pass

    monkeypatch.setattr(rp_module, "logger", _FakeLogger())

    custom = tmp_path / "bad_category_list.yaml"
    custom.write_text(
        "journal_blacklist_categories:\n"
        "  \"E-明显不相关\":\n"
        "    - \"blockchain\"\n"
        "    - 999\n",
        encoding="utf-8",
    )

    categories = get_journal_blacklist_categories(path=custom)
    assert categories["E-明显不相关"] == ["blockchain"]
    assert any("E-明显不相关" in w for w in warnings)


def test_bucket_labels_type_mismatch_falls_back_for_that_key_only(tmp_path):
    """bucket_labels 是 dict-of-str：某一维度写成非字符串类型时，只让该维度回落默认，
    其余维度正常覆盖——与顶层标量键同款兜底逻辑在嵌套一层后仍生效。"""
    custom = tmp_path / "bad_bucket_label.yaml"
    custom.write_text(
        "bucket_labels:\n"
        "  A:\n"
        "    - 不该是列表\n"
        "  B: \"新维度B名\"\n",
        encoding="utf-8",
    )

    labels = get_bucket_labels(path=custom)
    assert labels["A"] == FIXTURE_BUCKET_LABELS["A"]
    assert labels["B"] == "新维度B名"


def test_journal_blacklist_categories_new_category_takes_effect(tmp_path):
    """开放式目录例外：journal_blacklist_categories 允许 yaml 新增黑名单类目
    （如换研究方向后新增的 "F-新类目"），不当成拼写错误的野键丢弃——与顶层
    "未知键即丢弃"策略的唯一例外。未覆盖的 A-E 类目仍深度合并回落内嵌默认。"""
    custom = tmp_path / "new_category.yaml"
    custom.write_text(
        "journal_blacklist_categories:\n"
        "  \"F-新类目\":\n"
        "    - \"foo\"\n"
        "    - \"bar\"\n",
        encoding="utf-8",
    )

    categories = get_journal_blacklist_categories(path=custom)
    assert categories["F-新类目"] == ["foo", "bar"]
    assert categories["A-医学影像视觉"] == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES["A-医学影像视觉"]
    assert categories["E-明显不相关"] == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES["E-明显不相关"]


def test_bucket_labels_new_dimension_takes_effect(tmp_path):
    """开放式目录例外：bucket_labels 允许 yaml 新增筛选维度（如换研究方向后
    新增的 "H"），不当成拼写错误的野键丢弃。未覆盖的 A-G 维度仍回落内嵌默认。"""
    custom = tmp_path / "new_dimension.yaml"
    custom.write_text(
        "bucket_labels:\n"
        "  H: \"新维度H名\"\n",
        encoding="utf-8",
    )

    labels = get_bucket_labels(path=custom)
    assert labels["H"] == "新维度H名"
    for k in "ABCDEFG":
        assert labels[k] == FIXTURE_BUCKET_LABELS[k]


def test_open_ended_dict_new_key_type_mismatch_still_rejected(tmp_path, monkeypatch):
    """开放式目录新增键仍须过值类型校验：journal_blacklist_categories 的新
    类目值须是 List[str]，bucket_labels 的新维度值须是 str；类型不符时一律
    拒绝并 warn（带完整路径），不并入结果，不能因为"允许新增键"就放松类型检查。"""
    warnings = []

    class _FakeLogger:
        def warning(self, msg, *a, **kw):
            warnings.append(str(msg))

        def info(self, *a, **kw):
            pass

    monkeypatch.setattr(rp_module, "logger", _FakeLogger())

    custom = tmp_path / "bad_new_keys.yaml"
    custom.write_text(
        "journal_blacklist_categories:\n"
        "  \"F-新类目\": \"不该是字符串\"\n"
        "bucket_labels:\n"
        "  H:\n"
        "    - \"不该是列表\"\n",
        encoding="utf-8",
    )

    categories = get_journal_blacklist_categories(path=custom)
    assert "F-新类目" not in categories

    labels = get_bucket_labels(path=custom)
    assert "H" not in labels

    assert any("journal_blacklist_categories.F-新类目" in w for w in warnings)
    assert any("bucket_labels.H" in w for w in warnings)


def test_unknown_top_level_key_warns_and_is_ignored(tmp_path, monkeypatch):
    """拼错键名（如 interests 误写成 intrests）：真实键 interests 仍回落默认（本来就该如此，
    没人覆盖它），但拼错的野键必须 warn 出来——否则用户以为自己改了研究方向，实际静默无效。"""
    warnings = []

    class _FakeLogger:
        def warning(self, msg, *a, **kw):
            warnings.append(str(msg))

        def info(self, *a, **kw):
            pass

    monkeypatch.setattr(rp_module, "logger", _FakeLogger())

    custom = tmp_path / "typo_profile.yaml"
    custom.write_text("intrests: 拼错键名，本该覆盖 interests\n", encoding="utf-8")

    profile = get_profile(path=custom)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert "intrests" not in profile
    assert any("intrests" in w for w in warnings)


def test_list_value_filters_non_str_elements_and_warns(tmp_path, monkeypatch):
    """列表值里混入非字符串元素（yaml 手误漏引号写成数字）：过滤掉该元素并 warn，
    其余正常的字符串元素照常生效。"""
    warnings = []

    class _FakeLogger:
        def warning(self, msg, *a, **kw):
            warnings.append(str(msg))

        def info(self, *a, **kw):
            pass

    monkeypatch.setattr(rp_module, "logger", _FakeLogger())

    custom = tmp_path / "nonstr_list_profile.yaml"
    custom.write_text(
        "journal_screen:\n"
        "  covid_safeguard_keywords:\n"
        "    - \"EHR\"\n"
        "    - 123\n"
        "    - \"missingness\"\n",
        encoding="utf-8",
    )

    assert get_covid_safeguard_keywords(path=custom) == ["EHR", "missingness"]
    assert any("covid_safeguard_keywords" in w for w in warnings)


# ---------------------------------------------------------------- 结构损坏回退


def test_non_dict_top_level_text_falls_back_to_builtin_default(tmp_path):
    """yaml 能被 safe_load 解析但顶层不是 dict（纯文本）——safe_load 不炸，
    但结构不对，整份回落内嵌默认，不能让 _deep_merge_defaults 对着字符串调 .items() 炸。"""
    broken = tmp_path / "broken_text.yaml"
    broken.write_text("只是一段纯文本，不是 key: value 结构\n", encoding="utf-8")

    profile = get_profile(path=broken)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert profile["inclusion_dims"] == FIXTURE_INCLUSION_DIMS
    assert get_covid_safeguard_keywords(path=broken) == FIXTURE_COVID_KEYWORDS


def test_non_dict_top_level_list_falls_back_to_builtin_default(tmp_path):
    """yaml 顶层写成列表，同样绕过异常但结构不对，整份回落内嵌默认。"""
    broken = tmp_path / "broken_list.yaml"
    broken.write_text("- foo\n- bar\n", encoding="utf-8")

    profile = get_profile(path=broken)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert get_exclusion_dims(path=broken) == FIXTURE_EXCLUSION_DIMS


def test_blank_scalar_key_falls_back_to_default_for_that_key_only(tmp_path):
    """`interests:` / `covid_safeguard_keywords:` 留空 → yaml.safe_load 得到 None，
    不能直接进 merged（否则 research_interests 变 None 触发 pydantic 校验错误，
    get_covid_safeguard_keywords 对 None 调 list() 抛 TypeError）；只回落对应键的默认值，
    同一份 yaml 里其它正常覆盖的键不受影响。"""
    partial = tmp_path / "partial_blank.yaml"
    partial.write_text(
        "interests:\n"
        "journal_screen:\n"
        "  covid_safeguard_keywords:\n"
        "pdf_ingest:\n"
        "  contamination_example_terms:\n"
        "    - \"覆盖生效术语\"\n",
        encoding="utf-8",
    )

    profile = get_profile(path=partial)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert get_covid_safeguard_keywords(path=partial) == FIXTURE_COVID_KEYWORDS
    # 未被空值污染的同级键仍按 yaml 覆盖生效
    assert get_contamination_example_terms(path=partial) == ["覆盖生效术语"]


def test_type_mismatched_key_falls_back_to_default_for_that_key_only(tmp_path):
    """键类型与默认值不符（该是 str 写成 list，该是 list 写成 str）时，只让该键
    回落默认，不牵连其它键、也不整体抛异常。"""
    mismatched = tmp_path / "mismatched_type.yaml"
    mismatched.write_text(
        "interests:\n"
        "  - 不该是列表\n"
        "journal_screen:\n"
        "  covid_safeguard_keywords: 不该是字符串\n"
        "pdf_ingest:\n"
        "  contamination_example_terms:\n"
        "    - \"正常覆盖\"\n",
        encoding="utf-8",
    )

    profile = get_profile(path=mismatched)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert get_covid_safeguard_keywords(path=mismatched) == FIXTURE_COVID_KEYWORDS
    assert get_contamination_example_terms(path=mismatched) == ["正常覆盖"]


# ---------------------------------------------------------------- 缺文件回退


def test_missing_profile_file_falls_back_to_builtin_default(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    profile = get_profile(path=missing)
    assert profile["interests"] == FIXTURE_INTERESTS
    assert profile["inclusion_dims"] == FIXTURE_INCLUSION_DIMS
    assert profile["exclusion_dims"] == FIXTURE_EXCLUSION_DIMS
    assert profile["journal_screen"]["covid_safeguard_keywords"] == FIXTURE_COVID_KEYWORDS
    assert profile["pdf_ingest"]["contamination_example_terms"] == FIXTURE_CONTAMINATION_TERMS
    assert profile["whitelist_keywords"] == FIXTURE_WHITELIST_KEYWORDS
    assert profile["journal_blacklist_categories"] == FIXTURE_JOURNAL_BLACKLIST_CATEGORIES
    assert profile["bucket_labels"] == FIXTURE_BUCKET_LABELS


# ---------------------------------------------------------------- env 覆盖优先级


MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def test_research_interests_env_override_beats_profile_default(tmp_path):
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        MINIMAL_ENV + "PROCESSING__RESEARCH_INTERESTS=env 覆盖的研究方向\n",
        encoding="utf-8",
    )
    settings = ScholarSettings.from_env_file(env_file)
    assert settings.processing.research_interests == "env 覆盖的研究方向"


def test_research_interests_without_env_falls_back_to_profile_default(tmp_path):
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    assert settings.processing.research_interests == FIXTURE_INTERESTS


def test_whitelist_without_env_falls_back_to_profile_default(tmp_path):
    """settings.py ProcessingSettings.whitelist 的 default_factory 外置后，
    无 env 覆盖时须逐字等于 research_profile.yaml 的 whitelist_keywords 默认值。"""
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    assert settings.processing.whitelist == FIXTURE_WHITELIST_KEYWORDS


def test_whitelist_env_override_beats_profile_default(tmp_path):
    """env 覆盖优先级 env > yaml > 内嵌默认，对 List[str] 字段同样有效
    （pydantic-settings 对复杂类型走 JSON 解析，需双引号 JSON 数组语法）。"""
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        MINIMAL_ENV + 'PROCESSING__WHITELIST=["env覆盖词A", "env覆盖词B"]\n',
        encoding="utf-8",
    )
    settings = ScholarSettings.from_env_file(env_file)
    assert settings.processing.whitelist == ["env覆盖词A", "env覆盖词B"]
