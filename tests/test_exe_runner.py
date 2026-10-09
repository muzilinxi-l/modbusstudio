"""
Modbus Studio EXE 自动化集成与冒烟自测脚本
通过 Windows API 控制进程生命周期、探测主窗口与最大化状态、抓取界面画面验证
"""

import ctypes
import os
import subprocess
import sys
import time
from PIL import ImageGrab

user32 = ctypes.windll.user32


def test_exe_launch_and_window_zoomed():
    exe_path = os.path.abspath("dist/ModbusStudio.exe")
    assert os.path.exists(exe_path), f"EXE 文件不存在: {exe_path}"

    print(f"[1/4] 启动目标可执行程序: {exe_path}")
    proc = subprocess.Popen([exe_path])
    assert proc.pid is not None, "进程未能成功创建"
    print(f"      PID = {proc.pid}")

    try:
        # 等待界面初始化渲染
        main_hwnd = None
        main_title = ""
        for attempt in range(15):
            time.sleep(1.0)
            hwnds = []

            def enum_cb(hwnd, extra):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        title = buff.value
                        if "Modbus Studio" in title:
                            hwnds.append((hwnd, title))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

            if hwnds:
                main_hwnd, main_title = hwnds[0]
                print(f"[2/4] 成功定位到主窗口: HWND=0x{main_hwnd:X}, 标题='{main_title}'")
                break

        assert main_hwnd is not None, "未能在超时时间内检测到 Modbus Studio 主窗口"

        # 检查是否为原生最大化模式
        is_zoomed = bool(user32.IsZoomed(main_hwnd))
        print(f"[3/4] 验证窗口全屏/最大化状态: IsZoomed = {is_zoomed}")
        assert is_zoomed is True, "EXE 启动后未处于默认最大化(zoomed)状态！"

        # 尝试抓取当前主界面截屏 (若会话不支持则安全跳过)
        try:
            screenshot = ImageGrab.grab()
            shot_path = os.path.abspath("test_exe_screenshot.png")
            screenshot.save(shot_path)
            print(f"[4/4] 抓取界面运行截图成功保存至: {shot_path}")
        except Exception as ge:
            print(f"[4/4] 当前后台会话暂不支持屏幕抓取，安全跳过截图: {ge}")

        print(">>> EXE 主程序生命周期与窗口状态自测全部通过 (PASS) <<<")
    finally:
        print("清理并退出 EXE 进程...")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    test_exe_launch_and_window_zoomed()
