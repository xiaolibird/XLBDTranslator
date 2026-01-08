"""
TUI (Terminal User Interface) 设置面板
使用字符绘画创建交互式配置界面，类似 Lue 风格

Features:
- 预设模式选择
- 按 schema 层级分组的参数设置
- 互斥检查与验证
- 与 SettingsBuilder 集成
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum

# 尝试导入 getch (跨平台)
try:
    import termios
    import tty
    def getch():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch
except ImportError:
    # Windows fallback
    import msvcrt
    def getch():
        return msvcrt.getch().decode('utf-8', errors='ignore')


# ============================================================
# 字符绘画常量
# ============================================================
class BoxChars:
    """Box-drawing characters for TUI"""
    # 单线框
    TL = "┌"  # Top-left
    TR = "┐"  # Top-right
    BL = "└"  # Bottom-left
    BR = "┘"  # Bottom-right
    H = "─"   # Horizontal
    V = "│"   # Vertical


class BirdAnimation:
    """
    动画小鸟 - 1秒1帧，从左往右飞
    
    帧序列展示小鸟振翅飞行的效果
    """
    # 小鸟飞行帧（振翅动画）
    FRAMES = [
        # 帧1: 翅膀向上
        [
            "   ╱╲   ",
            "  /( )> ",
            "   ''   ",
        ],
        # 帧2: 翅膀中间
        [
            "   --   ",
            "  <( )> ",
            "   ''   ",
        ],
        # 帧3: 翅膀向下
        [
            "        ",
            "  <( )> ",
            "   ╲╱   ",
        ],
        # 帧4: 翅膀中间
        [
            "   --   ",
            "  <( )> ",
            "   ''   ",
        ],
    ]
    
    # 简化版小鸟（单行）
    SIMPLE_FRAMES = [
        "~\\_( o)>",
        "~~\\_( ˃)>",
        "~~~\\_( o)>",
        "~~\\_( ˂)>",
    ]
    
    # 超简洁版
    MINI_FRAMES = [
        "︵( o)>",
        "︶( ˃)>",
        "︵( o)>",
        "︶( ˂)>",
    ]
    
    def __init__(self, width: int = 70, use_simple: bool = True):
        self.width = width
        self.use_simple = use_simple
        self.position = 0  # 当前位置
        self.frame_idx = 0  # 当前帧索引
        self.bird_width = 10 if use_simple else 8
    
    def get_frame(self) -> str:
        """获取当前帧的渲染字符串"""
        if self.use_simple:
            bird = self.MINI_FRAMES[self.frame_idx % len(self.MINI_FRAMES)]
        else:
            bird_lines = self.FRAMES[self.frame_idx % len(self.FRAMES)]
            bird = "\n".join(bird_lines)
        
        # 添加位置偏移
        padding = " " * self.position
        return f"{padding}{bird}"
    
    def advance(self):
        """前进一帧"""
        self.frame_idx = (self.frame_idx + 1) % len(self.MINI_FRAMES if self.use_simple else self.FRAMES)
        self.position = (self.position + 2) % (self.width - self.bird_width)
    
    def reset(self):
        """重置到起始位置"""
        self.position = 0
        self.frame_idx = 0
    
    def render_line(self, color: str = "") -> str:
        """渲染单行小鸟（带颜色）"""
        bird = self.MINI_FRAMES[self.frame_idx % len(self.MINI_FRAMES)]
        padding = " " * self.position
        reset = "\033[0m" if color else ""
        return f"{padding}{color}{bird}{reset}"
    
    # 连接符
    T_DOWN = "┬"   # T pointing down
    T_UP = "┴"     # T pointing up
    T_RIGHT = "├"  # T pointing right
    T_LEFT = "┤"   # T pointing left
    CROSS = "┼"    # Cross
    
    # 双线框
    DTL = "╔"
    DTR = "╗"
    DBL = "╚"
    DBR = "╝"
    DH = "═"
    DV = "║"


class Colors:
    """ANSI color codes"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    # 前景色
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    # 亮色
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    
    # 背景色
    BG_BLACK = "\033[40m"
    BG_BLUE = "\033[44m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"


# ============================================================
# 数据结构
# ============================================================
class SettingType(Enum):
    """设置项类型"""
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    SELECT = "select"  # 单选
    PATH = "path"


@dataclass
class SettingItem:
    """设置项定义"""
    key: str
    label: str
    type: SettingType
    default: Any
    description: str = ""
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    options: List[Tuple[str, Any]] = field(default_factory=list)  # [(显示名, 值)]
    group: str = "general"
    mutually_exclusive: List[str] = field(default_factory=list)  # 互斥的其他设置项
    depends_on: Optional[Tuple[str, Any]] = None  # (设置项key, 期望值)


@dataclass
class SettingGroup:
    """设置分组"""
    key: str
    label: str
    icon: str = "📁"
    items: List[SettingItem] = field(default_factory=list)


# ============================================================
# 配置定义 (基于 schema.py 层级)
# ============================================================

def build_setting_groups() -> List[SettingGroup]:
    """构建设置分组（按 schema.py 层级）"""
    groups = []
    
    # 1. 预设模式 (快捷入口)
    groups.append(SettingGroup(
        key="presets",
        label="预设模式",
        icon="🎯",
        items=[
            SettingItem(
                key="preset",
                label="选择预设",
                type=SettingType.SELECT,
                default="balanced",
                description="快速应用预配置的参数组合",
                options=[
                    ("⚡ 快速模式 (Fast)", "fast"),
                    ("🎯 平衡模式 (Balanced)", "balanced"),
                    ("💎 高质量 (Quality)", "quality"),
                    ("� 双语对照 (Bilingual)", "bilingual"),
                    ("💰 经济模式 (Economy)", "economy"),
                    ("🐛 调试模式 (Debug)", "debug"),
                    ("🔧 自定义 (Custom)", "custom"),
                ]
            )
        ]
    ))
    
    # 2. API 设置
    groups.append(SettingGroup(
        key="api",
        label="API 配置",
        icon="🔌",
        items=[
            SettingItem(
                key="translator_provider",
                label="翻译提供商",
                type=SettingType.SELECT,
                default="gemini",
                description="选择 AI 翻译服务提供商",
                options=[
                    ("Google Gemini", "gemini"),
                    ("OpenAI Compatible", "openai-compatible"),
                    ("Ollama (本地)", "ollama"),
                ]
            ),
            SettingItem(
                key="gemini_model",
                label="Gemini 模型",
                type=SettingType.SELECT,
                default="gemini-2.5-flash",
                description="选择 Gemini 模型版本",
                depends_on=("translator_provider", "gemini"),
                options=[
                    ("Gemini 2.5 Flash (推荐)", "gemini-2.5-flash"),
                    ("Gemini 2.5 Pro", "gemini-2.5-pro"),
                    ("Gemini 1.5 Flash", "gemini-1.5-flash"),
                    ("Gemini 1.5 Pro", "gemini-1.5-pro"),
                ]
            ),
            SettingItem(
                key="openai_base_url",
                label="API Base URL",
                type=SettingType.STRING,
                default="http://localhost:11434",
                description="OpenAI 兼容 API 的基础 URL",
                depends_on=("translator_provider", "openai-compatible"),
            ),
        ]
    ))
    
    # 3. 处理设置
    groups.append(SettingGroup(
        key="processing",
        label="处理参数",
        icon="⚙️",
        items=[
            SettingItem(
                key="batch_size",
                label="批处理大小",
                type=SettingType.INT,
                default=5,
                min_val=1,
                max_val=20,
                description="每批翻译的段落数 (1-20)",
            ),
            SettingItem(
                key="enable_async",
                label="异步模式",
                type=SettingType.BOOL,
                default=True,
                description="启用异步并发翻译",
            ),
            SettingItem(
                key="async_max_workers",
                label="并发数",
                type=SettingType.INT,
                default=10,
                min_val=1,
                max_val=20,
                description="异步最大并发数 (1-20)",
                depends_on=("enable_async", True),
            ),
            SettingItem(
                key="enable_checkpoint",
                label="断点续传",
                type=SettingType.BOOL,
                default=True,
                description="启用断点续传功能",
            ),
            SettingItem(
                key="max_retries",
                label="最大重试",
                type=SettingType.INT,
                default=3,
                min_val=0,
                max_val=10,
                description="API 调用失败时的重试次数",
            ),
        ]
    ))
    
    # 4. 缓存设置
    groups.append(SettingGroup(
        key="cache",
        label="缓存配置",
        icon="💾",
        items=[
            SettingItem(
                key="enable_gemini_caching",
                label="Gemini 缓存",
                type=SettingType.BOOL,
                default=True,
                description="启用 Gemini 上下文缓存",
            ),
            SettingItem(
                key="cache_ttl_hours",
                label="缓存有效期",
                type=SettingType.INT,
                default=1,
                min_val=1,
                max_val=24,
                description="缓存有效时间 (小时)",
                depends_on=("enable_gemini_caching", True),
            ),
        ]
    ))
    
    # 5. 生成参数
    groups.append(SettingGroup(
        key="generation",
        label="生成参数",
        icon="🎲",
        items=[
            SettingItem(
                key="temperature",
                label="温度",
                type=SettingType.FLOAT,
                default=0.2,
                min_val=0.0,
                max_val=2.0,
                description="生成随机性 (0.0=确定, 2.0=随机)",
            ),
            SettingItem(
                key="top_p",
                label="Top-P",
                type=SettingType.FLOAT,
                default=0.95,
                min_val=0.0,
                max_val=1.0,
                description="核采样概率阈值",
            ),
            SettingItem(
                key="max_output_tokens",
                label="最大输出",
                type=SettingType.INT,
                default=16384,
                min_val=1024,
                max_val=65535,
                description="最大输出 token 数",
            ),
        ]
    ))
    
    # 6. 输出设置
    groups.append(SettingGroup(
        key="output",
        label="输出选项",
        icon="📤",
        items=[
            SettingItem(
                key="use_breadcrumb",
                label="面包屑导航",
                type=SettingType.BOOL,
                default=True,
                description="章节标题显示层级路径",
            ),
            SettingItem(
                key="render_page_markers",
                label="页码标记",
                type=SettingType.BOOL,
                default=True,
                description="在输出中显示原文页码",
            ),
            SettingItem(
                key="retain_original",
                label="保留原文",
                type=SettingType.BOOL,
                default=False,
                description="在译文下方保留原文",
            ),
        ]
    ))
    
    return groups


# ============================================================
# TUI 渲染器
# ============================================================

class TUIRenderer:
    """TUI 渲染引擎"""
    
    def __init__(self, width: int = 80):
        self.width = width
        self.term_width, self.term_height = self._get_terminal_size()
    
    def _get_terminal_size(self) -> Tuple[int, int]:
        """获取终端尺寸"""
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except OSError:
            return 80, 24
    
    def clear_screen(self):
        """清屏"""
        print("\033[2J\033[H", end="")
    
    def move_cursor(self, row: int, col: int):
        """移动光标"""
        print(f"\033[{row};{col}H", end="")
    
    def draw_box(self, title: str, content: List[str], 
                 width: Optional[int] = None, 
                 style: str = "single") -> List[str]:
        """绘制带标题的框"""
        w = width or self.width
        inner_width = w - 4
        
        if style == "double":
            tl, tr, bl, br, h, v = BoxChars.DTL, BoxChars.DTR, BoxChars.DBL, BoxChars.DBR, BoxChars.DH, BoxChars.DV
        else:
            tl, tr, bl, br, h, v = BoxChars.TL, BoxChars.TR, BoxChars.BL, BoxChars.BR, BoxChars.H, BoxChars.V
        
        lines = []
        
        # 顶边 + 标题
        if title:
            title_display = f" {title} "
            left_padding = (inner_width - len(title_display)) // 2
            right_padding = inner_width - len(title_display) - left_padding
            top_line = f"{tl}{h * left_padding}{title_display}{h * right_padding}{tr}"
        else:
            top_line = f"{tl}{h * inner_width}{tr}"
        lines.append(top_line)
        
        # 内容行
        for line in content:
            # 去掉 ANSI 颜色计算实际长度
            visible_len = len(self._strip_ansi(line))
            padding = inner_width - visible_len
            lines.append(f"{v} {line}{' ' * max(0, padding)} {v}")
        
        # 底边
        lines.append(f"{bl}{h * inner_width}{br}")
        
        return lines
    
    def _strip_ansi(self, text: str) -> str:
        """移除 ANSI 颜色代码"""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)
    
    def colorize(self, text: str, color: str) -> str:
        """给文本添加颜色"""
        return f"{color}{text}{Colors.RESET}"
    
    def render_header(self, title: str) -> str:
        """渲染标题头"""
        w = min(self.width, self.term_width - 2)
        padding = (w - len(title) - 4) // 2
        
        lines = [
            f"{Colors.CYAN}{BoxChars.DTL}{BoxChars.DH * (w - 2)}{BoxChars.DTR}{Colors.RESET}",
            f"{Colors.CYAN}{BoxChars.DV}{Colors.RESET}{' ' * padding}{Colors.BOLD}{Colors.YELLOW}{title}{Colors.RESET}{' ' * (w - padding - len(title) - 2)}{Colors.CYAN}{BoxChars.DV}{Colors.RESET}",
            f"{Colors.CYAN}{BoxChars.DBL}{BoxChars.DH * (w - 2)}{BoxChars.DBR}{Colors.RESET}",
        ]
        return "\n".join(lines)
    
    def render_menu(self, items: List[Tuple[str, str]], 
                    selected_idx: int,
                    title: str = "") -> str:
        """渲染菜单列表"""
        lines = []
        for i, (label, key) in enumerate(items):
            prefix = "▸ " if i == selected_idx else "  "
            color = Colors.BRIGHT_CYAN if i == selected_idx else Colors.WHITE
            bg = Colors.BG_BLUE if i == selected_idx else ""
            lines.append(f"{bg}{color}{prefix}{label}{Colors.RESET}")
        
        box_lines = self.draw_box(title, lines)
        return "\n".join(box_lines)
    
    def render_setting_item(self, item: SettingItem, 
                            value: Any, 
                            is_selected: bool,
                            is_disabled: bool = False) -> str:
        """渲染单个设置项"""
        prefix = "▸ " if is_selected else "  "
        
        # 颜色
        if is_disabled:
            label_color = Colors.BRIGHT_BLACK
            value_color = Colors.BRIGHT_BLACK
        elif is_selected:
            label_color = Colors.BRIGHT_CYAN
            value_color = Colors.BRIGHT_YELLOW
        else:
            label_color = Colors.WHITE
            value_color = Colors.GREEN
        
        # 值显示
        if item.type == SettingType.BOOL:
            value_display = "✓ 开启" if value else "✗ 关闭"
            value_color = Colors.GREEN if value else Colors.RED
        elif item.type == SettingType.SELECT:
            # 查找选项标签
            for opt_label, opt_val in item.options:
                if opt_val == value:
                    value_display = opt_label
                    break
            else:
                value_display = str(value)
        else:
            value_display = str(value)
        
        label_text = f"{label_color}{prefix}{item.label}:{Colors.RESET}"
        value_text = f"{value_color}{value_display}{Colors.RESET}"
        
        return f"{label_text:40s} {value_text}"
    
    def render_status_bar(self, message: str) -> str:
        """渲染状态栏"""
        w = min(self.width, self.term_width - 2)
        visible_msg = self._strip_ansi(message)
        padding = w - len(visible_msg) - 2
        return f"{Colors.BG_BLUE}{Colors.WHITE} {message}{' ' * max(0, padding)}{Colors.RESET}"
    
    def render_help_bar(self) -> str:
        """渲染帮助栏"""
        help_items = [
            ("↑↓", "选择"),
            ("←→", "调整"),
            ("Enter", "确认"),
            ("Tab", "切换分组"),
            ("q", "退出"),
        ]
        parts = [f"{Colors.BRIGHT_BLACK}[{Colors.YELLOW}{key}{Colors.BRIGHT_BLACK}]{Colors.WHITE}{desc}" 
                 for key, desc in help_items]
        return " ".join(parts) + Colors.RESET


# ============================================================
# TUI 控制器
# ============================================================

class TUISettingsPanel:
    """
    TUI 设置面板主控制器
    
    Usage:
        panel = TUISettingsPanel()
        settings = panel.run()  # 返回 SettingsBuilder 配置好的 Settings
    """
    
    def __init__(self):
        self.renderer = TUIRenderer(width=76)
        self.groups = build_setting_groups()
        self.values: Dict[str, Any] = {}
        
        # 初始化默认值
        for group in self.groups:
            for item in group.items:
                self.values[item.key] = item.default
        
        # UI 状态
        self.current_group_idx = 0
        self.current_item_idx = 0
        self.running = True
        self.message = "使用 ↑↓ 选择，←→/Enter 修改，Tab 切换分组"
        
        # 动画小鸟
        self.bird = BirdAnimation(width=70, use_simple=True)
        self.last_frame_time = 0
    
    def _get_current_group(self) -> SettingGroup:
        """获取当前分组"""
        return self.groups[self.current_group_idx]
    
    def _get_current_item(self) -> Optional[SettingItem]:
        """获取当前选中项"""
        group = self._get_current_group()
        if 0 <= self.current_item_idx < len(group.items):
            return group.items[self.current_item_idx]
        return None
    
    def _is_item_enabled(self, item: SettingItem) -> bool:
        """检查设置项是否启用（依赖检查）"""
        if item.depends_on is None:
            return True
        dep_key, dep_val = item.depends_on
        return self.values.get(dep_key) == dep_val
    
    def _apply_preset(self, preset_name: str):
        """应用预设"""
        from ..workflow.builder import PRESETS
        
        if preset_name == "custom":
            self.message = "🔧 已切换到自定义模式"
            return
        
        if preset_name not in PRESETS:
            self.message = f"❌ 未知预设: {preset_name}"
            return
        
        preset = PRESETS[preset_name]
        for key, value in preset.items():
            if key != "description" and key in self.values:
                self.values[key] = value
        
        self.message = f"✅ 已应用预设: {preset.get('description', preset_name)}"
    
    def _handle_value_change(self, item: SettingItem, delta: int):
        """处理值变更"""
        current_value = self.values[item.key]
        
        if item.type == SettingType.BOOL:
            self.values[item.key] = not current_value
            
        elif item.type == SettingType.INT:
            new_val = current_value + delta
            if item.min_val is not None:
                new_val = max(int(item.min_val), new_val)
            if item.max_val is not None:
                new_val = min(int(item.max_val), new_val)
            self.values[item.key] = new_val
            
        elif item.type == SettingType.FLOAT:
            step = 0.1 if item.max_val and item.max_val <= 2.0 else 1.0
            new_val = round(current_value + delta * step, 2)
            if item.min_val is not None:
                new_val = max(item.min_val, new_val)
            if item.max_val is not None:
                new_val = min(item.max_val, new_val)
            self.values[item.key] = new_val
            
        elif item.type == SettingType.SELECT:
            # 循环选择
            current_idx = 0
            for i, (_, val) in enumerate(item.options):
                if val == current_value:
                    current_idx = i
                    break
            new_idx = (current_idx + delta) % len(item.options)
            self.values[item.key] = item.options[new_idx][1]
            
            # 特殊处理：预设变更
            if item.key == "preset":
                self._apply_preset(self.values[item.key])
    
    def _update_bird_animation(self):
        """更新小鸟动画（1秒1帧）"""
        import time
        current_time = time.time()
        if current_time - self.last_frame_time >= 1.0:
            self.bird.advance()
            self.last_frame_time = current_time
    
    def _render(self):
        """渲染整个界面"""
        self.renderer.clear_screen()
        
        # 更新小鸟动画
        self._update_bird_animation()
        
        # 标题
        print(self.renderer.render_header("📋 XLBDTranslator 设置面板"))
        
        # 小鸟动画行
        bird_line = self.bird.render_line(Colors.BRIGHT_YELLOW)
        print(bird_line)
        
        # 分组标签页
        tabs = []
        for i, group in enumerate(self.groups):
            if i == self.current_group_idx:
                tabs.append(f"{Colors.BG_CYAN}{Colors.BLACK} {group.icon} {group.label} {Colors.RESET}")
            else:
                tabs.append(f"{Colors.BRIGHT_BLACK} {group.icon} {group.label} {Colors.RESET}")
        print(" ".join(tabs))
        print(f"{Colors.CYAN}{'─' * 74}{Colors.RESET}")
        print()
        
        # 当前分组的设置项
        group = self._get_current_group()
        for i, item in enumerate(group.items):
            is_selected = i == self.current_item_idx
            is_disabled = not self._is_item_enabled(item)
            value = self.values[item.key]
            
            line = self.renderer.render_setting_item(item, value, is_selected, is_disabled)
            print(f"  {line}")
            
            # 显示描述（仅选中项）
            if is_selected and item.description:
                desc_color = Colors.BRIGHT_BLACK if is_disabled else Colors.DIM
                print(f"    {desc_color}└─ {item.description}{Colors.RESET}")
        
        # 填充空行
        padding_lines = max(0, 10 - len(group.items) * 2)
        for _ in range(padding_lines):
            print()
        
        # 状态栏
        print(f"{Colors.CYAN}{'─' * 74}{Colors.RESET}")
        print(self.renderer.render_status_bar(self.message))
        print(self.renderer.render_help_bar())
    
    def _handle_input(self, key: str):
        """处理键盘输入"""
        group = self._get_current_group()
        item = self._get_current_item()
        
        if key == 'q' or key == '\x1b':  # q 或 ESC
            self.running = False
            
        elif key == '\t':  # Tab - 切换分组
            self.current_group_idx = (self.current_group_idx + 1) % len(self.groups)
            self.current_item_idx = 0
            self.message = f"切换到: {self.groups[self.current_group_idx].label}"
            
        elif key == '\x1b':  # 转义序列开头 (方向键)
            # 需要再读两个字符
            pass
            
        elif key == 'A' or key == 'k':  # 上
            self.current_item_idx = max(0, self.current_item_idx - 1)
            
        elif key == 'B' or key == 'j':  # 下
            self.current_item_idx = min(len(group.items) - 1, self.current_item_idx + 1)
            
        elif key == 'C' or key == 'l':  # 右
            if item and self._is_item_enabled(item):
                self._handle_value_change(item, 1)
                
        elif key == 'D' or key == 'h':  # 左
            if item and self._is_item_enabled(item):
                self._handle_value_change(item, -1)
                
        elif key == '\r' or key == '\n':  # Enter
            if item and self._is_item_enabled(item):
                if item.type == SettingType.BOOL:
                    self._handle_value_change(item, 1)
                elif item.type == SettingType.SELECT:
                    self._handle_value_change(item, 1)
    
    def run(self) -> 'Settings':
        """
        运行 TUI 面板
        
        Returns:
            配置好的 Settings 对象
        """
        from ..workflow.builder import SettingsBuilder
        from ..core.schema import Settings
        
        try:
            while self.running:
                self._render()
                
                # 读取按键
                key = getch()
                
                # 处理转义序列（方向键）
                if key == '\x1b':
                    key2 = getch()
                    if key2 == '[':
                        key3 = getch()
                        key = key3  # A=上, B=下, C=右, D=左
                    else:
                        self.running = False
                        continue
                
                self._handle_input(key)
            
            # 构建 Settings
            self.renderer.clear_screen()
            print(f"\n{Colors.GREEN}✅ 配置完成！正在构建设置...{Colors.RESET}\n")
            
            builder = SettingsBuilder()
            
            # 应用预设（如果不是 custom）
            preset = self.values.get("preset", "balanced")
            if preset != "custom":
                builder.use_preset(preset)
            
            # 应用自定义值
            for key, value in self.values.items():
                if key == "preset":
                    continue
                if hasattr(builder, key) and callable(getattr(builder, key)):
                    getattr(builder, key)(value)
                else:
                    # 直接设置到 _modifications
                    builder._modifications[key] = value
            
            return builder.build()
            
        except KeyboardInterrupt:
            self.renderer.clear_screen()
            print(f"\n{Colors.YELLOW}⚠️ 用户取消操作{Colors.RESET}\n")
            raise
    
    def preview_config(self) -> Dict[str, Any]:
        """预览当前配置（不构建 Settings）"""
        return self.values.copy()


# ============================================================
# 便捷入口
# ============================================================

def launch_settings_tui() -> 'Settings':
    """
    启动 TUI 设置面板
    
    Returns:
        配置好的 Settings 对象
    
    Usage:
        from src.utils.tui import launch_settings_tui
        settings = launch_settings_tui()
    """
    panel = TUISettingsPanel()
    return panel.run()


def show_quick_preset_selector() -> str:
    """
    快速预设选择器（简化版）
    
    Returns:
        选中的预设名称
    """
    from ..workflow.builder import PRESETS
    
    print(f"\n{Colors.CYAN}{'═' * 50}{Colors.RESET}")
    print(f"{Colors.BOLD}  🎯 快速预设选择{Colors.RESET}")
    print(f"{Colors.CYAN}{'═' * 50}{Colors.RESET}\n")
    
    options = list(PRESETS.keys())
    icons = {"fast": "⚡", "balanced": "🎯", "quality": "💎", "debug": "🐛", "economy": "💰"}
    
    for i, (name, config) in enumerate(PRESETS.items(), 1):
        icon = icons.get(name, "📋")
        print(f"  [{i}] {icon} {name:10s} - {config['description']}")
    
    print()
    choice = input(f"  选择预设 (1-{len(options)}, 默认 2): ").strip()
    
    try:
        idx = int(choice) - 1 if choice else 1  # 默认 balanced
        if 0 <= idx < len(options):
            selected = options[idx]
            print(f"\n  {Colors.GREEN}✅ 已选择: {selected}{Colors.RESET}\n")
            return selected
    except ValueError:
        pass
    
    print(f"\n  {Colors.YELLOW}⚠️ 无效选择，使用默认: balanced{Colors.RESET}\n")
    return "balanced"


if __name__ == "__main__":
    # 测试运行
    try:
        settings = launch_settings_tui()
        print(f"配置完成: {settings}")
    except KeyboardInterrupt:
        print("已取消")
