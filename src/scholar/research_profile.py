# -*- coding: utf-8 -*-
"""研究方向单点配置的加载器。

换研究方向只改 config/research_profile.yaml 一处；本模块负责把它加载给
settings.py（ProcessingSettings.research_interests / whitelist 默认值） /
whitelist_filter_prompt.md 的注入点（经 workflow.py 填充 {{INCLUSION_DIMS}}/
{{EXCLUSION_DIMS}}） / journal_screen.py / pdf_ingest.py / vault.py 五处消费方。

加载优先级：env > yaml > 内嵌默认。
- env 覆盖不在本模块处理：各消费方自己的字段（如 ProcessingSettings.research_interests）
  用 `Field(default_factory=...)` 接到 get_profile()，pydantic-settings 的 env 源本就
  优先于 default_factory，本模块只需要管“yaml 有没有、有就用、没有/坏了就退内嵌默认”这一层。
- 内嵌默认（_DEFAULT_PROFILE）与 yaml 文件里的默认值逐字一致，是本文件重构前散落在
  settings.py / whitelist_filter_prompt.md / journal_screen.py / pdf_ingest.py /
  vault.py 里的旧方向文本，保证 yaml 缺失或损坏时 filter-v3 裁决行为不突变。
"""
import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import repo_path
from ..utils.logger import get_logger

try:
    import yaml
except ImportError:  # pragma: no cover - 兜底：环境缺 pyyaml 时不炸，直接落内嵌默认
    yaml = None

logger = get_logger("research_profile")

PROFILE_FILE = repo_path("config/research_profile.yaml")

# 内嵌默认：逐字对应 config/research_profile.yaml 的默认值。
_DEFAULT_PROFILE: Dict[str, Any] = {
    "interests": (
        "论文主题：EHR 缺失机制（MNAR）与「缺失条件依赖结构」的跨中心可迁移性；"
        "定位为 causal hypothesis generation，不做 causal identification。"
        "基础工作：MA-GCT（特征专属可学习掩码嵌入 + Guide/Prior 注意力约束）。"
    ),
    "inclusion_dims": (
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
    ),
    "exclusion_dims": (
        "# 排除维度（命中 → drop，除非同时强命中 A/C/E）\n"
        "X1. 纯影像 / 基因组 / 单细胞 / 蛋白组，无表格型 EHR。\n"
        "X2. 时序填补方法本身的增量改进（新 imputer + 更低 RMSE），无下游/机制分析。\n"
        "X3. 综述/观点文，且不提供可引用的定量证据或分类框架。\n"
        "X4. LLM/foundation model 通用能力，未涉及缺失或迁移。\n"
        "X5. RCT / 流行病学关联研究，无方法学贡献。\n"
        "X6. 因果推断纯理论（do-calculus、identification proofs），无缺失或无实证。\n"
        "X7. 会议 workshop 短文、无实验的 preprint stub。"
    ),
    "journal_screen": {
        "covid_safeguard_keywords": [
            "EHR", "electronic health record", "missingness", "MNAR",
            "causal", "multi-site", "multi-center", "external validation",
            "transportability", "missing data", "imputation",
        ],
        "omics_ehr_signal_keywords": [
            "EHR", "electronic health record", "clinical predict",
            "missing data", "missingness", "MNAR", "imputation",
            "multi-site", "multi-center", "external validation",
        ],
    },
    "pdf_ingest": {
        "contamination_example_terms": ["MNAR", "缺失指纹", "跨中心迁移"],
    },
    "whitelist_keywords": [
        # EHR / 临床预测
        "EHR", "Electronic Health Record", "EMR", "Electronic Medical Record",
        "Clinical Prediction", "Predictive Model", "Risk Prediction", "Clinical Decision Support",
        # 缺失机制方法学
        "MNAR", "MAR", "missing not at random", "missingness", "informative missingness",
        "missing data", "imputation", "mask embedding", "missingness-aware", "missingness indicator",
        # 缺失 × 因果
        "missingness graph", "m-graph", "causal discovery", "causal structure", "identifiability",
        # 跨域 / 迁移
        "transportability", "domain shift", "dataset shift", "distribution shift",
        "external validation", "LODO", "site heterogeneity", "generalization",
        # 建模
        "GNN", "Graph Neural Network", "Graph Convolutional", "attention", "feature propagation",
        "LLM", "Large Language Model", "foundation model", "Transformer",
        "Semi-supervised", "Active Learning",
        # venue 基准 / 临床场景
        "MIMIC", "eICU", "AmsterdamUMCdb", "HiRID", "ICU",
        "sepsis", "AKI", "acute kidney injury", "CKD", "mortality prediction",
    ],
    "journal_blacklist_categories": {
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
    },
    "bucket_labels": {
        "A": "缺失机制方法学", "B": "缺失感知建模", "C": "缺失与因果",
        "D": "跨域跨中心迁移", "E": "对抗性证据", "F": "多库ICU基准", "G": "临床落地场景",
    },
}


# 开放式目录键：换研究方向时应当能"新增类目/新增维度"，而不是只能覆盖既有
# 类目——journal_blacklist_categories（黑名单类目，如新增 "F-新类目"）与
# bucket_labels（筛选维度，如新增 "H"）都是此类。这两个键在 _deep_merge_
# defaults 递归进入时会打开 allow_new_keys，其余嵌套字典（journal_screen /
# pdf_ingest 的子键）字段固定，不在此列，仍按未知键即丢弃处理。
_OPEN_ENDED_DICT_KEYS = frozenset({"journal_blacklist_categories", "bucket_labels"})


def _deep_merge_defaults(
    loaded: Dict[str, Any],
    defaults: Dict[str, Any],
    *,
    allow_new_keys: bool = False,
    path_prefix: str = "",
) -> Dict[str, Any]:
    """yaml 里没写的键回落内嵌默认；只做浅层字典递归，够本模块的受控配置用。

    对每个键单独兜底，而不是整份 yaml 一炸就全落默认：键值留空（None）或类型与默认值不符
    （如该是 str 写成了 list）时，只让这一个键回落默认并 warn，其余正常覆盖的键不受影响。

    额外两类静默失效在此兜底：
    - yaml 顶层出现 defaults 里没有的野键（典型是拼错了真实键名，如 interests 写成
      intrests）——本该覆盖的键其实没生效，只是没人告诉你，直接跳过并 warn。
    - 列表值里混入非字符串元素（如 yaml 手误漏了引号写成数字/布尔）——过滤掉并 warn，
      不让脏元素混进 prompt/黑名单词表。

    allow_new_keys=True（仅 _OPEN_ENDED_DICT_KEYS 两个键递归时打开）是上述"未知键即
    丢弃"策略的唯一例外：journal_blacklist_categories / bucket_labels 是开放式目录，
    yaml 新增类目/新增维度应当生效，而不是被当成拼写错误丢弃。新键的值类型按该目录内
    既有条目的类型（dict-of-list 则新键须是 List[str]；dict-of-str 则新键须是 str）
    校验，类型不符仍拒绝并 warn（不并入)。

    warn 文案统一带 path_prefix 拼出的完整路径（如
    "journal_blacklist_categories.F-新类目"），方便定位到底是哪层配置写错了。

    本模块 getter 已对 list 做副本返回；不要直接 mutate get_profile() 的返回值
    （它是路径级缓存的共享对象）。
    """
    merged = copy.deepcopy(defaults)
    for k, v in loaded.items():
        full_path = "{}.{}".format(path_prefix, k) if path_prefix else k
        if k not in defaults:
            if allow_new_keys and defaults:
                sample = next(iter(defaults.values()))
                if isinstance(sample, list):
                    if isinstance(v, list):
                        filtered = [item for item in v if isinstance(item, str)]
                        if len(filtered) != len(v):
                            logger.warning(
                                "research_profile.yaml 新增键 {} 列表含 {} 个非字符串元素，"
                                "已过滤".format(full_path, len(v) - len(filtered)))
                        merged[k] = filtered
                    else:
                        logger.warning(
                            "research_profile.yaml 新增键 {} 类型应为 list，实际 {}，"
                            "已忽略、不会生效".format(full_path, type(v).__name__))
                elif isinstance(sample, str):
                    if isinstance(v, str):
                        merged[k] = v
                    else:
                        logger.warning(
                            "research_profile.yaml 新增键 {} 类型应为 str，实际 {}，"
                            "已忽略、不会生效".format(full_path, type(v).__name__))
                else:
                    logger.warning(
                        "research_profile.yaml 新增键 {} 无法推断预期类型，已忽略、"
                        "不会生效".format(full_path))
                continue
            logger.warning(
                "research_profile.yaml 含未知键 {}（不在默认 profile 结构中，"
                "可能是拼写错误，该键已忽略、不会生效）".format(full_path))
            continue
        default_v = defaults[k]
        if v is None:
            logger.warning("research_profile.yaml 键 {} 为空，回落内嵌默认".format(full_path))
            continue
        if isinstance(default_v, dict):
            if isinstance(v, dict):
                merged[k] = _deep_merge_defaults(
                    v, default_v,
                    allow_new_keys=(k in _OPEN_ENDED_DICT_KEYS),
                    path_prefix=full_path,
                )
            else:
                logger.warning(
                    "research_profile.yaml 键 {} 类型应为 dict，实际 {}，回落内嵌默认".format(
                        full_path, type(v).__name__))
            continue
        if isinstance(default_v, list):
            if isinstance(v, list):
                filtered = [item for item in v if isinstance(item, str)]
                if len(filtered) != len(v):
                    logger.warning(
                        "research_profile.yaml 键 {} 列表含 {} 个非字符串元素，已过滤".format(
                            full_path, len(v) - len(filtered)))
                merged[k] = filtered
            else:
                logger.warning(
                    "research_profile.yaml 键 {} 类型应为 list，实际 {}，回落内嵌默认".format(
                        full_path, type(v).__name__))
            continue
        if not isinstance(v, type(default_v)):
            logger.warning(
                "research_profile.yaml 键 {} 类型应为 {}，实际 {}，回落内嵌默认".format(
                    full_path, type(default_v).__name__, type(v).__name__))
            continue
        merged[k] = v
    return merged


@lru_cache(maxsize=None)
def _load_cached(path_str: str) -> Dict[str, Any]:
    """按路径缓存加载结果；path_str 是缓存键，测试用不同路径即可绕开旧缓存。"""
    path = Path(path_str)
    if yaml is None:
        logger.warning("pyyaml 未安装，research_profile 使用内嵌默认")
        return copy.deepcopy(_DEFAULT_PROFILE)
    if not path.exists():
        logger.warning("research_profile.yaml 缺失（{}），使用内嵌默认".format(path))
        return copy.deepcopy(_DEFAULT_PROFILE)
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("research_profile.yaml 解析失败（{}），使用内嵌默认: {}".format(path, e))
        return copy.deepcopy(_DEFAULT_PROFILE)
    # safe_load 能解析出非 dict 顶层结构（纯文本、列表……），此时结构不对，整份回落内嵌默认。
    if not isinstance(loaded, dict):
        logger.warning(
            "research_profile.yaml 顶层结构应为 dict，实际 {}（{}），使用内嵌默认".format(
                type(loaded).__name__, path))
        return copy.deepcopy(_DEFAULT_PROFILE)
    try:
        return _deep_merge_defaults(loaded, _DEFAULT_PROFILE)
    except Exception as e:
        logger.warning("research_profile.yaml 合并失败（{}），使用内嵌默认: {}".format(path, e))
        return copy.deepcopy(_DEFAULT_PROFILE)


def get_profile(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载研究方向配置（带缓存）。

    Args:
        path: 仅测试用，指定替代路径以绕开默认文件与缓存；生产代码留空即可。
    """
    p = path if path is not None else PROFILE_FILE
    return _load_cached(str(p))


def get_default_research_interests(path: Optional[Path] = None) -> str:
    return get_profile(path)["interests"]


def get_inclusion_dims(path: Optional[Path] = None) -> str:
    return get_profile(path)["inclusion_dims"]


def get_exclusion_dims(path: Optional[Path] = None) -> str:
    return get_profile(path)["exclusion_dims"]


def get_covid_safeguard_keywords(path: Optional[Path] = None) -> List[str]:
    return list(get_profile(path)["journal_screen"]["covid_safeguard_keywords"])


def get_omics_ehr_signal_keywords(path: Optional[Path] = None) -> List[str]:
    return list(get_profile(path)["journal_screen"]["omics_ehr_signal_keywords"])


def get_contamination_example_terms(path: Optional[Path] = None) -> List[str]:
    return list(get_profile(path)["pdf_ingest"]["contamination_example_terms"])


def get_whitelist_keywords(path: Optional[Path] = None) -> List[str]:
    return list(get_profile(path)["whitelist_keywords"])


def get_journal_blacklist_categories(path: Optional[Path] = None) -> Dict[str, List[str]]:
    categories = get_profile(path)["journal_blacklist_categories"]
    return {cat: list(words) for cat, words in categories.items()}


def get_bucket_labels(path: Optional[Path] = None) -> Dict[str, str]:
    return dict(get_profile(path)["bucket_labels"])
