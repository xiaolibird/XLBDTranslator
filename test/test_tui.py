#!/usr/bin/env python3
"""
TUI 动画小鸟演示
运行: python test.py           - 动画演示
运行: python test.py --frames  - 展示所有帧
运行: python test.py --tui     - 完整设置面板
"""
import os
import sys
import time

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.utils.tui import BirdAnimation, Colors


def demo_bird_animation():
    """演示小鸟从左往右飞的动画"""
    bird = BirdAnimation(width=70, use_simple=True)
    
    print("\033[2J\033[H")  # 清屏
    print(f"\n{Colors.CYAN}╔══════════════════════════════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET}              {Colors.BOLD}{Colors.YELLOW}🐦 小鸟动画演示 - 1秒1帧{Colors.RESET}                                {Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╚══════════════════════════════════════════════════════════════════════════╝{Colors.RESET}")
    print(f"\n  {Colors.DIM}按 Ctrl+C 退出{Colors.RESET}\n")
    
    try:
        frame_count = 0
        while True:
            # 保存光标位置
            print("\033[s", end="")
            
            # 移动到小鸟行
            print("\033[6;1H", end="")
            
            # 清除该行
            print("\033[K", end="")
            
            # 渲染小鸟
            bird_frame = bird.render_line(Colors.BRIGHT_YELLOW)
            print(f"  {bird_frame}")
            
            # 显示帧计数
            print(f"\n  {Colors.BRIGHT_BLACK}帧: {frame_count + 1}  位置: {bird.position}{Colors.RESET}\033[K")
            
            # 恢复光标位置
            print("\033[u", end="")
            
            sys.stdout.flush()
            
            # 等待1秒
            time.sleep(1.0)
            
            # 前进一帧
            bird.advance()
            frame_count += 1
            
    except KeyboardInterrupt:
        print(f"\n\n  {Colors.GREEN}✅ 演示结束{Colors.RESET}\n")


def demo_all_frames():
    """展示所有小鸟帧"""
    print(f"\n{Colors.CYAN}{'═' * 60}{Colors.RESET}")
    print(f"  {Colors.BOLD}小鸟动画帧展示{Colors.RESET}")
    print(f"{Colors.CYAN}{'═' * 60}{Colors.RESET}\n")
    
    print(f"  {Colors.DIM}简化版帧:{Colors.RESET}")
    for i, frame in enumerate(BirdAnimation.MINI_FRAMES):
        print(f"    帧 {i+1}: {Colors.BRIGHT_YELLOW}{frame}{Colors.RESET}")
    
    print(f"\n  {Colors.DIM}完整版帧:{Colors.RESET}")
    for i, frame_lines in enumerate(BirdAnimation.FRAMES):
        print(f"    帧 {i+1}:")
        for line in frame_lines:
            print(f"      {Colors.BRIGHT_CYAN}{line}{Colors.RESET}")
        print()


def run_tui():
    """运行完整设置面板"""
    from src.utils.tui import launch_settings_tui
    settings = launch_settings_tui()
    print(f"配置完成: {settings}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--frames":
            demo_all_frames()
        elif sys.argv[1] == "--tui":
            run_tui()
        else:
            print("用法: python test.py [--frames|--tui]")
    else:
        demo_bird_animation()