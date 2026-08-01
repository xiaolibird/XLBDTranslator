#!/usr/bin/env python3
"""
Prompt 拼装回归测试（零网络）

验证 P0 修复：
1. 正式翻译阶段（begin_formal_translation 之后）mode + glossary 进入 system instruction
2. 预翻译阶段不含 mode/glossary（原设计不被改坏）
3. "N/A" 术语表不再泄漏
4. DeepSeek 长文本模式：system 并入单条 user message，且前缀（system+mode+glossary）
   跨 batch 字节级恒定（前缀缓存可命中）
5. fallback 中途切换 provider 后阶段状态被重放
6. Gemini 非缓存路径 base config 重建后含 mode + glossary
"""
import json
import sys
from pathlib import Path
from unittest import mock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.schema import Settings, ContentSegment
from src.utils.ui import load_modes_config
from src.translator import engine as engine_mod
from src.translator.engine import OpenAICompatibleTranslator, GeminiTranslator
from src.translator.fallback import FallbackTranslator

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def make_settings() -> Settings:
    s = Settings.from_env_file(project_root / 'config' / 'config.env')
    modes = load_modes_config(project_root / 'config' / 'modes.json')
    s.processing.translation_mode_entity = modes['3']  # Critical Theory
    s.processing.enable_gemini_caching = False
    return s


def make_segments():
    return [
        ContentSegment(segment_id=1, original_text="The Real resists symbolization."),
        ContentSegment(segment_id=2, original_text="Ideology interpellates individuals."),
    ]


def run_openai_tests():
    print("\n== OpenAI 兼容路径（DeepSeek 长文本模式） ==")
    s = make_settings()
    t = OpenAICompatibleTranslator(s)
    assert t.use_long_text_mode, "config.env 应指向 deepseek"

    captured = []

    def fake_post(self, url, data, headers, timeout):
        captured.append(data.decode('utf-8'))
        return json.dumps({
            "choices": [{"message": {"content": json.dumps(
                {"translations": [{"id": 1, "translation": "译一"}, {"id": 2, "translation": "译二"}]}
            )}}]
        })

    glossary = {"the Real": "实在界", "interpellation": "询唤"}

    with mock.patch.object(OpenAICompatibleTranslator, '_http_post_json', fake_post):
        # --- 预翻译阶段（未调钩子）---
        t._translate_text_batch(make_segments(), context="prev source text")
        pre_payload = json.loads(captured[-1])
        pre_msg = pre_payload["messages"][0]["content"]
        check("预翻译: 单条 user message", len(pre_payload["messages"]) == 1)
        check("预翻译: 不含 ACTIVE TRANSLATION MODE", "ACTIVE TRANSLATION MODE" not in pre_msg)
        check("预翻译: 不含 glossary 块", "MANDATORY GLOSSARY" not in pre_msg)
        check("预翻译: system instruction 已并入", "Academic Translation Specialist" in pre_msg
              or "translator" in pre_msg.lower())
        check("预翻译: 无 N/A 泄漏", "\nN/A\n" not in pre_msg)
        check("预翻译: 无 No Context 字面量", "No Context" not in pre_msg)

        # --- 旁路兜底：未进正式阶段但直接传 glossary ---
        t._translate_text_batch(make_segments(), context="", glossary=glossary)
        bypass_msg = json.loads(captured[-1])["messages"][0]["content"]
        check("旁路兜底: glossary 落入 user message", "- **the Real**: 实在界" in bypass_msg)

        # --- 正式阶段 ---
        t.begin_formal_translation(glossary)
        t._translate_text_batch(make_segments(), context="ctx batch A", glossary=glossary)
        m1 = json.loads(captured[-1])["messages"][0]["content"]
        t._translate_text_batch(make_segments(), context="ctx batch B totally different", glossary=glossary)
        m2 = json.loads(captured[-1])["messages"][0]["content"]

        check("正式: 含 ACTIVE TRANSLATION MODE", "ACTIVE TRANSLATION MODE" in m1)
        check("正式: 含模式名 Critical Theory", "Critical Theory" in m1)
        check("正式: 含术语行", "- **the Real**: 实在界" in m1)
        check("正式: glossary 仅出现一次", m1.count("- **the Real**: 实在界") == 1)
        check("正式: glossary 在动态 context 内容之前",
              m1.find("- **the Real**: 实在界") < m1.find("ctx batch A"))
        check("正式: 无 N/A 泄漏", "\nN/A\n" not in m1)

        # 前缀缓存断言：两个 batch 的公共前缀必须覆盖 system+mode+glossary
        glossary_end = m1.find("</glossary>")
        common = 0
        for a, b in zip(m1, m2):
            if a != b:
                break
            common += 1
        check("正式: 跨 batch 公共前缀覆盖 glossary 段",
              common > glossary_end > 0, f"common={common}, glossary_end={glossary_end}")

        # --- 标题翻译不丢 system（长文本模式合并） ---
        captured.clear()
        try:
            t.translate_titles(["Chapter One"])
        except Exception:
            pass  # 解析失败无所谓，只看请求
        if captured:
            title_msg = json.loads(captured[-1])["messages"][0]["content"]
            check("标题: system instruction 未被丢弃", "TRANSLATION PROMPT TEMPLATE" in title_msg)
            check("标题: 含 mode（正式阶段）", "ACTIVE TRANSLATION MODE" in title_msg)

        # --- 术语抽取不丢 system ---
        captured.clear()
        segs = make_segments()
        for seg in segs:
            seg.translated_text = "已译"
        try:
            t.extract_glossary(segs)
        except Exception:
            pass
        if captured:
            gl_msg = json.loads(captured[-1])["messages"][0]["content"]
            check("术语抽取: You output JSON only 未被丢弃", "You output JSON only." in gl_msg)


def run_gemini_tests():
    print("\n== Gemini 路径（非缓存） ==")
    s = make_settings()
    try:
        g = GeminiTranslator(s)
    except Exception as e:
        print(f"  ⚠️ Gemini 初始化失败，跳过: {e}")
        return
    si_before = g._base_generation_config.system_instruction
    check("钩子前: base config 不含 mode", "ACTIVE TRANSLATION MODE" not in (si_before or ""))
    g.begin_formal_translation({"gaze": "凝视"})
    si_after = g._base_generation_config.system_instruction
    check("钩子后: base config 含 mode", "ACTIVE TRANSLATION MODE" in si_after)
    check("钩子后: base config 含 glossary", "- **gaze**: 凝视" in si_after)
    check("钩子后: 含 MANDATORY GLOSSARY 段", "MANDATORY GLOSSARY" in si_after)


def run_fallback_tests():
    print("\n== Fallback 链阶段重放 ==")
    s = make_settings()
    fb = FallbackTranslator(s, ['deepseek', 'gemini'])
    fb.begin_formal_translation({"suture": "缝合"})
    idx0, t0 = fb._get_current()
    check("当前 provider 已进正式阶段", t0._formal_phase is True)
    # 模拟 hard 失败切换
    fb._dead.add(idx0)
    fb._current_idx = idx0 + 1
    try:
        idx1, t1 = fb._get_current()
        check("切换后新 provider 重放正式阶段", t1._formal_phase is True)
        check("切换后新 provider 带 glossary", t1._formal_glossary.get("suture") == "缝合")
    except Exception as e:
        print(f"  ⚠️ 第二 provider 初始化失败，跳过: {e}")


def run_parser_tests():
    print("\n== JSON 契约解析器 ==")
    from src.translator.engine import _normalize_translation_list
    obj = {"translations": [{"id": 1, "translation": "甲"}]}
    check("对象格式解包", _normalize_translation_list(obj) == obj["translations"])
    arr = [{"id": 1, "translation": "甲"}]
    check("旧数组格式直通", _normalize_translation_list(arr) == arr)
    degen = {"1": "甲", "2": "乙"}
    check("退化 map 兼容", len(_normalize_translation_list(degen)) == 2)

    s = make_settings()
    t = OpenAICompatibleTranslator(s)
    complete_obj = '{"translations": [{"id": 1, "translation": "完整句子。"}, {"id": 2, "translation": "第二句"}]}'
    out = t._regex_fallback_for_list(complete_obj)
    check("对象响应不误判截断", out and "翻译被截断" not in out[-1]["translation"],
          str(out[-1] if out else None))
    truncated = '{"translations": [{"id": 1, "translation": "断在半'
    out2 = t._regex_fallback_for_list(truncated)
    check("真截断仍被标记", out2 and "翻译被截断" in out2[-1]["translation"])


def run_template_hygiene_tests():
    print("\n== 模板卫生 ==")
    prompts_dir = project_root / 'config' / 'prompts'
    # P1 之后收紧到全部文件；当前先检查云端完整版
    for name in ['system_instruction.md', 'text_translation_prompt.md']:
        text = (prompts_dir / name).read_text(encoding='utf-8')
        check(f"{name}: 无未替换占位符字面量",
              '{context}' not in text and '{input_json}' not in text)


if __name__ == '__main__':
    run_openai_tests()
    run_gemini_tests()
    run_fallback_tests()
    run_parser_tests()
    run_template_hygiene_tests()
    print(f"\n{'=' * 50}\n通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
