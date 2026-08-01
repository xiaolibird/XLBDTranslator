"""
EPUB 渲染器
负责将翻译后的 ContentSegment 列表填回原 EPUB 文件，输出翻译版本
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import ebooklib
from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub

from ..core.schema import SegmentList, Settings
from ..utils.logger import get_logger

logger = get_logger(__name__)


class EPUBRenderer:
    """
    EPUB 渲染器

    职责：
    - 将翻译后的内容填回原 EPUB 文件的对应位置
    - 继承原文件的格式和样式
    - 支持保留原文（双语模式）
    - 输出为 {filename}_translated.epub
    """

    # 需要翻译的块级标签
    BLOCK_TAGS = [
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "div",
    ]

    def __init__(self, settings: Settings):
        self.settings = settings
        self.retain_original = self._get_retain_original_setting()
        self.logger = logger

    def _get_retain_original_setting(self) -> bool:
        """从 settings 获取是否保留原文的配置"""
        try:
            val = self.settings.processing.retain_original
            # 显式处理 None 的情况
            if val is None:
                return False
            return bool(val)
        except AttributeError:
            return False

    def render_to_file(
        self,
        segments: SegmentList,
        original_epub_path: Path,
        output_path: Path,
        title: str = "Document",
        translated_title: str = "",
    ) -> None:
        """
        将翻译内容填回原 EPUB 文件，生成翻译版本

        Args:
            segments: 翻译后的片段列表
            original_epub_path: 原 EPUB 文件路径
            output_path: 输出文件路径
            title: 原始文档标题
            translated_title: 翻译后的文档标题
        """
        self.logger.info(f"📖 开始渲染 EPUB: {original_epub_path.name}")
        self.logger.info(f"   - 保留原文模式: {self.retain_original}")

        # 1. 构建文本映射表: 原文 -> 译文
        translation_map = self._build_translation_map(segments)
        self.logger.info(f"   - 构建映射表: {len(translation_map)} 个文本片段")

        # 调试：输出前3个映射示例
        if translation_map:
            self.logger.debug("   - 映射表示例（前3个）:")
            for i, (orig, trans) in enumerate(list(translation_map.items())[:3]):
                orig_preview = orig[:50] + "..." if len(orig) > 50 else orig
                trans_preview = trans[:50] + "..." if len(trans) > 50 else trans
                self.logger.debug(f"     [{i+1}] {orig_preview} → {trans_preview}")

        # 2. 读取原 EPUB
        book = epub.read_epub(str(original_epub_path))

        # 检查并记录 TOC 和 spine 信息
        self.logger.debug(f"   - 原 EPUB 有 TOC: {bool(book.toc)}")
        self.logger.debug(f"   - 原 EPUB 有 spine: {bool(book.spine)}")

        # 诊断 TOC 结构
        if book.toc:
            self._diagnose_toc(book.toc)

        # 立即检查是否有内容为 None 的文档
        none_content_items = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    content = item.get_content()
                    if content is None:
                        none_content_items.append(item.get_name())
                except:
                    pass

        if none_content_items:
            self.logger.warning(
                f"   ⚠️ 原 EPUB 中发现 {len(none_content_items)} 个内容为 None 的文档:"
            )
            for name in none_content_items:
                self.logger.warning(f"      - {name}")

        # 3. 更新元数据（标题）
        if translated_title:
            # 更新书名
            book.set_title(f"{translated_title} - {title}")

        # 4. 更新 TOC（目录结构）中的标题
        # 如果 TOC 更新失败，不影响主流程
        try:
            if book.toc:
                self._update_toc(book, translation_map)
            else:
                self.logger.warning("   ⚠️ 原 EPUB 没有 TOC 结构")
        except Exception as e:
            self.logger.warning(f"   ⚠️ 更新 TOC 失败（跳过）: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

        # 5. 统计所有资源类型
        resource_stats = defaultdict(int)
        for item in book.get_items():
            item_type = item.get_type()
            resource_stats[item_type] += 1

        # 输出资源统计
        type_names = {
            ebooklib.ITEM_UNKNOWN: "未知",
            ebooklib.ITEM_DOCUMENT: "文档",
            ebooklib.ITEM_IMAGE: "图片",
            ebooklib.ITEM_STYLE: "样式表",
            ebooklib.ITEM_SCRIPT: "脚本",
            ebooklib.ITEM_FONT: "字体",
            ebooklib.ITEM_NAVIGATION: "导航",
            ebooklib.ITEM_COVER: "封面",
        }
        self.logger.info("   📦 原 EPUB 资源统计:")
        for item_type, count in sorted(resource_stats.items()):
            type_name = type_names.get(item_type, f"类型{item_type}")
            self.logger.info(f"      - {type_name}: {count} 个")

        # 6. 遍历所有文档项，替换文本内容
        modified_count = 0
        total_replacements = 0

        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue

            item_name = item.get_name()

            # 修改 HTML 内容
            try:
                original_content = item.get_content()

                # 防护：如果内容为空，跳过
                if not original_content:
                    self.logger.debug(f"   ⊘ 跳过空内容: {item_name}")
                    continue

                modified_content, replacements = self._replace_text_in_html(
                    original_content, translation_map
                )

                # 防护：如果返回的内容为 None，保持原内容不变
                if modified_content is None:
                    self.logger.warning(f"   ⚠️ {item_name} 处理后返回 None，保持原内容")
                    # 不调用 set_content，保持原内容
                    continue

                # 只有在有替换时才更新内容
                if replacements > 0:
                    item.set_content(modified_content)
                    modified_count += 1
                    total_replacements += replacements
                    self.logger.debug(f"   ✓ 已更新: {item_name} ({replacements} 处)")
                # 如果没有替换，也不需要 set_content，保持原内容

            except Exception as e:
                self.logger.warning(f"   ⚠️ 处理 {item_name} 时出错: {e}")
                import traceback

                self.logger.debug(traceback.format_exc())
                # 发生异常时不修改 item，保持原内容
                continue

        # 7. 确保生成 NCX 和 Nav 文件
        # 检查是否已存在 NCX/Nav，如果没有则添加
        has_ncx = False
        has_nav = False

        for item in book.get_items():
            if isinstance(item, epub.EpubNcx):
                has_ncx = True
            elif isinstance(item, epub.EpubNav):
                has_nav = True

        if not has_ncx:
            self.logger.info("   + 添加 NCX 文件（EPUB 2.0 兼容）")
            book.add_item(epub.EpubNcx())

        if not has_nav:
            self.logger.info("   + 添加 Nav 文件（EPUB 3.0 兼容）")
            book.add_item(epub.EpubNav())

        # 8. 保存输出
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 确保 spine 存在（阅读顺序）
        if not book.spine:
            self.logger.warning("   ⚠️ 原 EPUB 没有 spine，可能导致阅读顺序混乱")

        # 检查所有 items 的内容是否为 None
        self.logger.debug("   🔍 检查所有 items 内容...")
        items_with_none_content = []
        for item in book.get_items():
            try:
                content = item.get_content()
                if content is None and item.get_type() == ebooklib.ITEM_DOCUMENT:
                    items_with_none_content.append(item.get_name())
                    self.logger.warning(
                        f"   ⚠️ 发现内容为 None 的文档: {item.get_name()}"
                    )
            except Exception as e:
                self.logger.debug(f"   检查 {item.get_name()} 时出错: {e}")

        if items_with_none_content:
            self.logger.warning(
                f"   ⚠️ 发现 {len(items_with_none_content)} 个内容为 None 的文档"
            )
            self.logger.warning("   尝试从 book 中移除这些文档以继续...")
            # 从 items 列表中移除这些有问题的文档
            book.items = [
                item
                for item in book.items
                if item.get_name() not in items_with_none_content
            ]

        # 写入 EPUB，确保生成 NCX 文件（EPUB 2.0 兼容）
        # ebooklib 会自动根据 book.toc 和 book.spine 生成 toc.ncx
        try:
            epub.write_epub(str(output_path), book, {})
            self.logger.info(f"✅ EPUB 渲染完成: {output_path}")
        except Exception as e:
            self.logger.error(f"❌ 写入 EPUB 失败: {e}")
            import traceback

            self.logger.error(traceback.format_exc())
            raise

        self.logger.info(
            f"   - 修改了 {modified_count} 个文档，共 {total_replacements} 处替换"
        )
        self.logger.info("   - 保留了所有非文档资源（图片、样式、字体等）")
        self.logger.info("   - 已生成 NCX 目录文件（EPUB 2.0/3.0 兼容）")

    def _diagnose_toc(self, toc_items, level=0):
        """诊断 TOC 结构，检查是否有缺失的 uid"""
        for item in toc_items:
            if isinstance(item, tuple):
                section = item[0]
                children = item[1] if len(item) > 1 else []

                # 检查必需属性
                has_uid = hasattr(section, "uid") and section.uid is not None
                title = getattr(section, "title", "No title")

                if not has_uid:
                    self.logger.warning(
                        f"   {'  ' * level}⚠️ TOC Section 缺少 uid: {title}"
                    )
                else:
                    self.logger.debug(
                        f"   {'  ' * level}✓ {title} (uid: {section.uid})"
                    )

                if children:
                    self._diagnose_toc(children, level + 1)
            else:
                # 单个 item
                has_uid = hasattr(item, "uid") and item.uid is not None
                title = getattr(item, "title", "No title")

                if not has_uid:
                    self.logger.warning(
                        f"   {'  ' * level}⚠️ TOC item 缺少 uid: {title}"
                    )
                else:
                    self.logger.debug(f"   {'  ' * level}✓ {title} (uid: {item.uid})")

    def _generate_uid(self, title: str, index: int) -> str:
        """为 TOC item 生成唯一的 uid"""
        import re

        # 清理标题，只保留字母数字和下划线
        clean_title = re.sub(r"[^\w\s-]", "", title)
        clean_title = re.sub(r"[\s-]+", "_", clean_title.strip())
        clean_title = clean_title.lower()[:50]  # 限制长度
        return f"toc_{clean_title}_{index}" if clean_title else f"toc_item_{index}"

    def _update_toc(self, book: epub.EpubBook, translation_map: Dict[str, str]) -> None:
        """
        更新 EPUB 的 TOC（目录）结构中的标题

        处理两种情况：
        1. EPUB 2.x: NCX 文件
        2. EPUB 3.x: NAV 文件

        注意：只更新标题，不修改 TOC 结构，确保 uid 等属性不丢失
        如果 item 缺少 uid，会自动生成一个
        """
        # 用于生成唯一 uid 的计数器
        uid_counter = {"count": 0}

        # 递归更新 TOC 树
        def update_toc_recursive(toc_items):
            """递归更新 TOC 项，确保保留所有原始属性，缺失的 uid 会自动补充"""
            updated = []
            for item in toc_items:
                if isinstance(item, tuple):
                    # (Section, [子项])
                    section = item[0]
                    children = item[1] if len(item) > 1 else []

                    # 如果缺少 uid，自动生成一个
                    if not hasattr(section, "uid") or section.uid is None:
                        title = getattr(section, "title", "Unknown")
                        section.uid = self._generate_uid(title, uid_counter["count"])
                        uid_counter["count"] += 1
                        self.logger.info(
                            f"   + 为 TOC item 生成 uid: {title} -> {section.uid}"
                        )

                    # 翻译标题
                    if hasattr(section, "title") and section.title:
                        original_title = section.title
                        normalized = self._normalize_text(original_title)
                        translated = translation_map.get(normalized)
                        # TOC 标题只做精确匹配：translation_map 的 key 是正文
                        # 原文，标题走模糊匹配大概率被替换成某段正文整段译文

                        if translated:
                            section.title = translated
                            self.logger.debug(
                                f"   TOC: {original_title} → {translated}"
                            )

                    # 递归处理子项
                    if children:
                        updated_children = update_toc_recursive(children)
                        updated.append((section, updated_children))
                    else:
                        updated.append(section)
                else:
                    # 单个 Section
                    # 如果缺少 uid，自动生成一个
                    if not hasattr(item, "uid") or item.uid is None:
                        title = getattr(item, "title", "Unknown")
                        item.uid = self._generate_uid(title, uid_counter["count"])
                        uid_counter["count"] += 1
                        self.logger.info(
                            f"   + 为 TOC item 生成 uid: {title} -> {item.uid}"
                        )

                    if hasattr(item, "title") and item.title:
                        original_title = item.title
                        normalized = self._normalize_text(original_title)
                        translated = translation_map.get(normalized)
                        # TOC 标题只做精确匹配：translation_map 的 key 是正文
                        # 原文，标题走模糊匹配大概率被替换成某段正文整段译文

                        if translated:
                            item.title = translated
                            self.logger.debug(
                                f"   TOC: {original_title} → {translated}"
                            )

                    updated.append(item)

            return updated

        # 更新主 TOC
        if book.toc:
            try:
                book.toc = update_toc_recursive(book.toc)
                self.logger.info("   ✓ 已更新 TOC 目录结构")
            except Exception as e:
                self.logger.error(f"   ❌ 更新 TOC 失败: {e}")
                import traceback

                self.logger.debug(traceback.format_exc())
                # 失败时不修改 TOC
                pass

    def _build_translation_map(self, segments: SegmentList) -> Dict[str, str]:
        """
        构建文本映射表

        从 segments 中提取原文到译文的映射

        Returns:
            {normalized_original_text: translated_text}
        """
        translation_map: Dict[str, str] = {}

        for seg in segments:
            # 跳过图片类型
            if seg.content_type == "image":
                continue

            original = seg.original_text.strip()
            translated = seg.translated_text.strip() if seg.translated_text else ""

            if not original or not translated:
                continue

            # 策略1: 先尝试完整匹配（优先级最高）
            # 这样可以保留完整的译文，避免段落拆分导致的丢失
            normalized_full = self._normalize_text(original)
            if normalized_full and normalized_full not in translation_map:
                translation_map[normalized_full] = translated

            # 策略2: 将长文本拆分为段落进行匹配（作为备选）
            # 因为 parser 按段落 yield，而 segment 可能合并了多个段落
            original_paragraphs = [
                p.strip() for p in original.split("\n\n") if p.strip()
            ]
            translated_paragraphs = (
                [p.strip() for p in translated.split("\n\n") if p.strip()]
                if translated
                else []
            )

            # 逐段落建立映射，处理段落数不一致的情况
            max_paras = max(len(original_paragraphs), len(translated_paragraphs))

            for i in range(max_paras):
                orig_para = (
                    original_paragraphs[i] if i < len(original_paragraphs) else ""
                )
                trans_para = (
                    translated_paragraphs[i] if i < len(translated_paragraphs) else ""
                )

                if orig_para and trans_para:
                    normalized = self._normalize_text(orig_para)
                    if normalized and normalized not in translation_map:
                        translation_map[normalized] = trans_para
                elif not orig_para and trans_para:
                    # 译文段落多于原文段落的情况（可能是翻译扩展）
                    # 将多余的译文附加到最后一个原文段落的译文中
                    if original_paragraphs:
                        last_orig_normalized = self._normalize_text(
                            original_paragraphs[-1]
                        )
                        if last_orig_normalized in translation_map:
                            # 追加到已有译文
                            translation_map[last_orig_normalized] += "\n\n" + trans_para

        return translation_map

    def _replace_text_in_html(
        self, html_content: bytes, translation_map: Dict[str, str]
    ) -> Tuple[bytes, int]:
        """
        在 HTML 内容中替换文本

        Args:
            html_content: 原始 HTML 字节内容
            translation_map: 原文到译文的映射

        Returns:
            (修改后的 HTML 字节内容, 替换次数)
        """
        # 防护：空内容直接返回
        if not html_content:
            return html_content or b"", 0

        # 解析 HTML
        soup = BeautifulSoup(html_content, "html.parser")

        # 查找 body
        body = soup.find("body") or soup

        # 记录替换次数
        replacement_count = 0

        # 只处理"叶子块"：自身不再包含任何块级子标签。
        # find_all 是文档序（父先于子），此前包裹整章的容器 <div> 先于其
        # <p> 被处理——模糊匹配一旦命中会 clear 掉整个 div 只塞一段译文，
        # 该章其余段落在成品里直接消失；原 has_processed_child 守卫在
        # 父先序下恒为 False，形同虚设。
        for tag in body.find_all(self.BLOCK_TAGS):
            if tag.find(self.BLOCK_TAGS):
                continue  # 容器标签：交给其内部叶子块处理

            result = self._process_tag(tag, translation_map, soup)
            if result:
                replacement_count += 1

        # 返回修改后的 HTML（添加防护）
        try:
            html_str = str(soup)
            if html_str is None:
                self.logger.warning("BeautifulSoup 转换为字符串返回 None")
                return html_content, 0  # 返回原内容
            return html_str.encode("utf-8"), replacement_count
        except Exception as e:
            self.logger.warning(f"HTML 序列化失败: {e}")
            return html_content, 0  # 返回原内容

    def _normalize_text(self, text: str) -> str:
        """规整化文本，用于匹配比较"""
        if not text:
            return ""
        # 去除多余空白，保留单个空格
        normalized = re.sub(r"\s+", " ", text.strip())
        return normalized

    def _markdown_to_html(self, text: str) -> str:
        """
        将文本中的 Markdown 语法转换为 HTML 标签

        支持的语法：
        - **bold** → <strong>bold</strong>
        - *italic* → <em>italic</em>
        - ~~strikethrough~~ → <del>strikethrough</del>
        - `code` → <code>code</code>
        - [link](url) → <a href="url">link</a>
        """
        if not text:
            return text

        # 1. 处理链接 [text](url)
        # 注意：需要先处理链接，避免与其他语法冲突
        text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r'<a href="\2">\1</a>', text)

        # 2. 处理行内代码 `code`
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

        # 3. 处理删除线 ~~text~~
        text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)

        # 4. 处理粗体 **text** 或 __text__
        # 注意：需要使用非贪婪匹配，避免跨段匹配
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)

        # 5. 处理斜体 *text* 或 _text_
        # 注意：需要避免匹配已经转换的 <strong> 标签中的内容
        # 使用负向前瞻和负向后顾来避免匹配星号周围的字母
        text = re.sub(r"(?<![*\w])\*([^\*]+?)\*(?![*\w])", r"<em>\1</em>", text)
        text = re.sub(r"(?<!_)\b_([^_]+?)_\b(?!_)", r"<em>\1</em>", text)

        return text

    def _process_tag(
        self, tag: Tag, translation_map: Dict[str, str], soup: BeautifulSoup
    ) -> bool:
        """
        处理单个标签，替换其文本内容

        Args:
            tag: BeautifulSoup 标签对象
            translation_map: 原文到译文的映射
            soup: BeautifulSoup 对象，用于创建新标签

        Returns:
            是否成功替换
        """
        # 获取标签的纯文本内容
        original_text = tag.get_text(separator=" ", strip=True)
        normalized_text = self._normalize_text(original_text)

        if not normalized_text:
            return False

        # 查找翻译
        translated_text = translation_map.get(normalized_text)

        if not translated_text:
            # 尝试模糊匹配（处理小差异）
            translated_text = self._fuzzy_match(normalized_text, translation_map)

        if not translated_text:
            return False

        # 调试：检查译文长度
        if len(original_text) > 50 or len(translated_text) > 50:
            self.logger.debug(
                f"   [匹配成功] 原文: {original_text[:50]}... ({len(original_text)} 字符)"
            )
            self.logger.debug(
                f"   [匹配成功] 译文: {translated_text[:50]}... ({len(translated_text)} 字符)"
            )

        # 替换内容
        if self.retain_original:
            # 保留原文模式：先译文，再原文
            self._replace_tag_content_bilingual(
                tag, translated_text, original_text, soup
            )
        else:
            # 纯译文模式
            self._replace_tag_content(tag, translated_text)

        return True

    def _fuzzy_match(self, text: str, translation_map: Dict[str, str]) -> Optional[str]:
        """
        模糊匹配翻译（处理空白/软连字符等差异导致的精确匹配失败）

        约束（防错配）：
        - 候选集合按 translation_map 预计算一次（此前每个未命中标签 × 每条
          译文现建两个 set，全书 ≈ 数百万次 set 构造）
        - 长度守卫：len 比超出 [0.6, 1.67] 的候选直接跳过（防止单段译文
          吞掉整章长文本，或反向）
        - 一次性消费：命中过的候选不再参与后续模糊匹配（同一条译文此前
          可被填进多个不同标签）
        """
        if len(text) < 10:
            return None

        cache_key = id(translation_map)
        cache = self.__dict__.setdefault('_fuzzy_index_cache', {})
        if cache_key not in cache:
            index = []
            for orig, trans in translation_map.items():
                if len(orig) >= 10:
                    index.append((orig, trans, self._sim_profile(orig)))
            cache[cache_key] = index
        index = cache[cache_key]
        consumed: Set[str] = self.__dict__.setdefault('_fuzzy_consumed', set())

        text_profile = self._sim_profile(text)
        text_len = len(text)

        best_match = None
        best_orig = None
        best_score = 0.0

        for orig, trans, orig_profile in index:
            if orig in consumed:
                continue
            ratio = text_len / len(orig)
            if ratio < 0.6 or ratio > 1.67:
                continue

            score = self._similarity_profiled(text, text_profile, orig, orig_profile)
            if score > best_score and score > 0.85:  # 相似度阈值
                best_score = score
                best_match = trans
                best_orig = orig

        if best_orig is not None:
            consumed.add(best_orig)
        return best_match

    def _sim_profile(self, s: str) -> set:
        """预计算 _similarity 用的集合表示（中文字符集 / 英文词集）。"""
        if self._contains_cjk(s):
            return set(s.replace(" ", ""))
        return set(s.lower().split())

    def _similarity_profiled(self, s1: str, set1: set, s2: str, set2: set) -> float:
        """带预计算集合的相似度（与 _similarity 语义一致）。"""
        if not s1 or not s2:
            return 0.0
        # 短文本仍用子串匹配
        if len(s1) < 50 or len(s2) < 50:
            shorter = min(s1, s2, key=len)
            longer = max(s1, s2, key=len)
            if shorter in longer:
                return len(shorter) / len(longer)
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _similarity(self, s1: str, s2: str) -> float:
        """计算两个字符串的相似度"""
        if not s1 or not s2:
            return 0.0

        # 对于短文本，使用简单的子串匹配
        if len(s1) < 50 or len(s2) < 50:
            shorter = min(s1, s2, key=len)
            longer = max(s1, s2, key=len)
            if shorter in longer:
                return len(shorter) / len(longer)

        # 使用词集合的 Jaccard 相似度
        # 对中文使用字符级别
        if self._contains_cjk(s1) or self._contains_cjk(s2):
            set1 = set(s1.replace(" ", ""))
            set2 = set(s2.replace(" ", ""))
        else:
            set1 = set(s1.lower().split())
            set2 = set(s2.lower().split())

        if not set1 or not set2:
            return 0.0

        intersection = len(set1 & set2)
        union = len(set1 | set2)

        return intersection / union if union > 0 else 0.0

    def _contains_cjk(self, text: str) -> bool:
        """检查文本是否包含 CJK 字符"""
        for char in text:
            if "\u4e00" <= char <= "\u9fff":  # CJK 统一汉字
                return True
            if "\u3040" <= char <= "\u309f":  # 平假名
                return True
            if "\u30a0" <= char <= "\u30ff":  # 片假名
                return True
        return False

    def _replace_tag_content(self, tag: Tag, new_text: str) -> None:
        """
        替换标签内容为纯译文

        保留标签本身的属性，但用新文本替换内容
        """
        # 转换 Markdown 语法为 HTML
        html_text = self._markdown_to_html(new_text)

        # 清空内容
        tag.clear()

        # 如果包含 HTML 标签，需要解析后插入
        if "<" in html_text and ">" in html_text:
            # 使用 BeautifulSoup 解析 HTML 片段
            # 注意：需要包装在一个容器标签中以正确解析
            temp_soup = BeautifulSoup(f"<div>{html_text}</div>", "html.parser")
            # 提取容器内的所有子元素
            for element in temp_soup.div.children:
                # 复制元素以避免移动问题
                if isinstance(element, NavigableString):
                    tag.append(NavigableString(str(element)))
                else:
                    tag.append(element)
        else:
            # 纯文本，直接添加
            tag.append(NavigableString(html_text))

    def _replace_tag_content_bilingual(
        self, tag: Tag, translated_text: str, original_text: str, soup: BeautifulSoup
    ) -> None:
        """
        替换标签内容为双语（先译文后原文）

        格式：
        <p>原文</p> → <p><span class="translated">译文</span><br/><span class="original">原文</span></p>
        """
        # 清空原内容
        tag.clear()

        # 转换译文的 Markdown 语法为 HTML
        translated_html = self._markdown_to_html(translated_text)

        # 调试：检查译文长度
        if len(translated_text) > 100:
            self.logger.debug(f"   [双语模式] 译文长度: {len(translated_text)} 字符")
            self.logger.debug(f"   [双语模式] 译文预览: {translated_text[:100]}...")

        # 创建译文 span
        trans_span = soup.new_tag("span")
        trans_span["class"] = "translated"

        # 如果译文包含 HTML 标签，需要解析后插入
        if "<" in translated_html and ">" in translated_html:
            temp_soup = BeautifulSoup(f"<div>{translated_html}</div>", "html.parser")
            for element in temp_soup.div.children:
                if isinstance(element, NavigableString):
                    trans_span.append(NavigableString(str(element)))
                else:
                    trans_span.append(element)
        else:
            trans_span.string = translated_html

        # 创建换行
        br = soup.new_tag("br")

        # 转换原文的 Markdown 语法为 HTML（原文可能也有格式）
        original_html = self._markdown_to_html(original_text)

        # 创建原文 span
        orig_span = soup.new_tag("span")
        orig_span["class"] = "original"
        orig_span["style"] = "color: #999; font-size: 0.9em;"

        # 如果原文包含 HTML 标签，需要解析后插入
        if "<" in original_html and ">" in original_html:
            temp_soup = BeautifulSoup(f"<div>{original_html}</div>", "html.parser")
            for element in temp_soup.div.children:
                if isinstance(element, NavigableString):
                    orig_span.append(NavigableString(str(element)))
                else:
                    orig_span.append(element)
        else:
            orig_span.string = original_html

        # 按顺序添加：先译文，再原文
        tag.append(trans_span)
        tag.append(br)
        tag.append(orig_span)

        # 调试：验证添加后的内容
        result_text = tag.get_text()
        if len(result_text) < len(translated_text) * 0.5:
            self.logger.warning(
                f"   ⚠️ [双语模式] 译文可能丢失！原译文 {len(translated_text)} 字符，结果只有 {len(result_text)} 字符"
            )


def render_epub(
    segments: SegmentList,
    original_epub_path: Path,
    output_path: Path,
    settings: Settings,
    title: str = "Document",
    translated_title: str = "",
) -> None:
    """
    便捷函数：渲染 EPUB 文件

    Args:
        segments: 翻译后的片段列表
        original_epub_path: 原 EPUB 文件路径
        output_path: 输出文件路径
        settings: 全局设置
        title: 原始文档标题
        translated_title: 翻译后的文档标题
    """
    renderer = EPUBRenderer(settings)
    renderer.render_to_file(
        segments=segments,
        original_epub_path=original_epub_path,
        output_path=output_path,
        title=title,
        translated_title=translated_title,
    )


# ============================================================================
# HTML to EPUB 转换器（用于 PDF 等非 EPUB 源文件生成 EPUB）
# ============================================================================


class HTMLToEPUBConverter:
    """
    HTML → EPUB 转换器

    职责：
    - 接收渲染好的 HTML 内容
    - 按章节分割并创建 EPUB 结构
    - 保留样式和格式
    - 生成符合 EPUB 3 标准的电子书

    适用场景：PDF/TXT 等非 EPUB 源文件翻译后输出 EPUB
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = logger

    def convert_to_epub(
        self,
        html_content: str,
        output_path: Path,
        title: str = "Document",
        translated_title: str = "",
        author: str = "Unknown",
        language: str = "zh",
    ) -> None:
        """
        将 HTML 内容转换为 EPUB 文件

        Args:
            html_content: 完整的 HTML 内容（包含 <html>, <body> 等标签）
            output_path: 输出 EPUB 文件路径
            title: 原始标题
            translated_title: 翻译后的标题
            author: 作者名
            language: 语言代码（zh, en, ja 等）
        """
        self.logger.info(f"📚 开始生成 EPUB: {output_path.name}")

        # 1. 创建 EPUB 书籍对象
        book = epub.EpubBook()

        # 2. 设置元数据
        display_title = f"{translated_title} - {title}" if translated_title else title
        book.set_identifier(f"id_{output_path.stem}")
        book.set_title(display_title)
        book.set_language(language)
        book.add_author(author)

        # 3. 提取 <body> 内容和 CSS 样式
        body_content = self._extract_body_content(html_content)
        css_content = self._extract_css_content(html_content)

        # 4. 按章节分割内容
        chapters = self._split_into_chapters(body_content)
        self.logger.info(f"   - 检测到 {len(chapters)} 个章节")

        # 5. 创建 CSS 样式文件
        css = epub.EpubItem(
            uid="style",
            file_name="style.css",
            media_type="text/css",
            content=(
                css_content.encode("utf-8")
                if css_content
                else self._get_default_css().encode("utf-8")
            ),
        )
        book.add_item(css)

        # 6. 创建章节对象
        epub_chapters = []
        spine_items = ["nav"]  # EPUB spine 顺序
        toc_items = []  # 目录项

        for i, (chapter_title, chapter_html) in enumerate(chapters, 1):
            chapter_id = f"chapter_{i}"
            chapter_file = f"chapter_{i}.xhtml"

            # 创建章节 HTML（XHTML 格式）
            chapter_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
    <title>{chapter_title}</title>
    <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
    {chapter_html}
</body>
</html>"""

            # 创建 EPUB 章节对象
            chapter = epub.EpubHtml(
                title=chapter_title,
                file_name=chapter_file,
                lang=language,
                uid=chapter_id,
            )
            # 必须编码为 bytes：lxml 拒绝解析带 encoding 声明的 unicode 字符串，
            # str 会导致 get_body_content() 返回空、write_epub 报 "Document is empty"
            chapter.content = chapter_content.encode("utf-8")

            book.add_item(chapter)
            epub_chapters.append(chapter)
            spine_items.append(chapter)
            toc_items.append(chapter)

        # 7. 设置 TOC（目录）
        book.toc = tuple(toc_items)

        # 8. 添加默认的 NCX 和 Nav 文件
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # 9. 设置 spine（阅读顺序）
        book.spine = spine_items

        # 10. 保存 EPUB
        output_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(output_path), book, {})

        self.logger.info(f"✅ EPUB 生成完成: {output_path}")

    def _extract_body_content(self, html_content: str) -> str:
        """提取 <body> 标签内的内容"""
        match = re.search(
            r"<body[^>]*>(.*?)</body>", html_content, re.DOTALL | re.IGNORECASE
        )
        if match:
            return match.group(1)
        return html_content  # 如果没有 body 标签，返回全部内容

    def _extract_css_content(self, html_content: str) -> str:
        """提取 <style> 标签内的 CSS 内容"""
        css_parts = []
        for match in re.finditer(
            r"<style[^>]*>(.*?)</style>", html_content, re.DOTALL | re.IGNORECASE
        ):
            css_parts.append(match.group(1))
        return "\n\n".join(css_parts)

    def _split_into_chapters(self, html_content: str) -> list:
        """
        按标题分割 HTML 内容为章节

        Returns:
            [(chapter_title, chapter_html), ...]
        """
        # 查找所有 h2-h5 标题（章节分隔符）
        chapter_pattern = r"(<h[2-5][^>]*>.*?</h[2-5]>)"

        # 分割内容
        parts = re.split(chapter_pattern, html_content, flags=re.DOTALL | re.IGNORECASE)

        chapters = []
        current_title = "前言"  # 默认第一章标题
        current_content = []

        for i, part in enumerate(parts):
            # 检查是否为标题
            if re.match(r"<h[2-5]", part, re.IGNORECASE):
                # 保存上一章节
                if current_content:
                    chapters.append((current_title, "".join(current_content)))
                    current_content = []

                # 提取新标题文本（去除 emoji 和标签）
                title_text = re.sub(r"<.*?>", "", part)
                title_text = re.sub(r"[📚📖📄📝📌🧭🔖]", "", title_text).strip()
                current_title = title_text or f"章节 {len(chapters) + 1}"

                # 标题本身也加入内容
                current_content.append(part)
            else:
                # 普通内容
                if part.strip():
                    current_content.append(part)

        # 添加最后一章
        if current_content:
            chapters.append((current_title, "".join(current_content)))

        # 如果没有检测到任何章节，将全部内容作为一章
        if not chapters:
            chapters.append(("全文", html_content))

        return chapters

    def _get_default_css(self) -> str:
        """获取默认的 EPUB CSS 样式"""
        return """
/* EPUB 默认样式 */
body {
    font-family: "Source Han Serif", "Noto Serif CJK", serif;
    line-height: 1.8;
    margin: 1em;
    text-align: justify;
}

h1, h2, h3, h4, h5, h6 {
    font-weight: bold;
    margin-top: 1em;
    margin-bottom: 0.5em;
    page-break-after: avoid;
}

h2 { font-size: 1.8em; }
h3 { font-size: 1.5em; }
h4 { font-size: 1.3em; }
h5 { font-size: 1.1em; }

p {
    margin: 0.8em 0;
    text-indent: 2em;
}

.content-block {
    margin: 1.5em 0;
    padding: 1em;
    border-left: 3px solid #3498db;
    background-color: #f8f9fa;
}

.page-marker {
    display: inline-block;
    font-size: 0.85em;
    color: #7f8c8d;
    margin-right: 0.5em;
}

/* 双语模式样式 */
.translated {
    display: block;
    margin-bottom: 0.5em;
}

.original {
    display: block;
    color: #666;
    font-size: 0.9em;
    font-style: italic;
    border-left: 2px solid #ddd;
    padding-left: 1em;
    margin-top: 0.5em;
}

/* 引用块样式 */
blockquote {
    border-left: 3px solid #ccc;
    padding-left: 1em;
    margin: 1em 0;
    color: #666;
    font-style: italic;
}

/* 图片 */
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}

/* 代码块 */
pre, code {
    font-family: 'Courier New', monospace;
    background-color: #f4f4f4;
    padding: 0.2em 0.4em;
    border-radius: 3px;
}

pre {
    padding: 1em;
    overflow-x: auto;
}
"""


def render_html_to_epub(
    html_content: str,
    output_path: Path,
    settings: Settings,
    title: str = "Document",
    translated_title: str = "",
    author: str = "Unknown",
) -> None:
    """
    便捷函数：将 HTML 内容转换为 EPUB

    Args:
        html_content: 完整的 HTML 内容
        output_path: 输出路径
        settings: 全局设置
        title: 原始标题
        translated_title: 翻译后的标题
        author: 作者名
    """
    converter = HTMLToEPUBConverter(settings)
    converter.convert_to_epub(
        html_content=html_content,
        output_path=output_path,
        title=title,
        translated_title=translated_title,
        author=author,
    )
