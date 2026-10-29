"""Raise the tool's Edge window so the user can actually see the QQ login QR code.

Background: the crawler drives a dedicated Edge profile over CDP. Two separate problems can
make that window invisible to the user, and both look like "Edge never opened":

1. the window opens *behind* other windows, so nobody notices it;
2. the owning process runs in Session 0 (the services session), for example because a
   scheduled task was registered with -LogonType S4U. Windows created in Session 0 are
   never shown on the interactive desktop, no matter what you do here.

This module only fixes (1). For (2) it reports the session mismatch so the cause is visible
instead of silently looking like "nothing happened".
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
from typing import List, Tuple

# 服务跑在交互会话里（登录窗口才可见），所以它调的每个控制台程序都会在屏幕上闪黑框。
# 统一加 CREATE_NO_WINDOW，只影响控制台，不影响浏览器窗口。
_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> int:
    return _CREATE_NO_WINDOW if os.name == "nt" else 0


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def cdp_pid(port: int = 9222) -> int:
    """PID listening on the CDP port, or 0."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"],
            creationflags=_no_window(),
            capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return 0


def _session_of(pid: int) -> int:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").SessionId"],
            creationflags=_no_window(),
            capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def _current_session() -> int:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Id $PID).SessionId"],
            creationflags=_no_window(),
            capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def windows_of(pid: int) -> List[Tuple[int, str]]:
    """Visible top-level Chrome_WidgetWin windows owned by pid."""
    user32 = _user32()
    found: List[Tuple[int, str]] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "Chrome_WidgetWin" not in cls.value:
            return True
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        found.append((hwnd, title.value))
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found


def _raise(hwnd: int) -> bool:
    user32 = _user32()
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # A background process usually is not allowed to steal the foreground; temporarily
    # joining the foreground thread's input queue is the documented workaround.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    my_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != my_thread:
        attached = bool(user32.AttachThreadInput(my_thread, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(my_thread, fg_thread, False)
    return user32.GetForegroundWindow() == hwnd


def focus_cdp_window(port: int = 9222) -> Tuple[bool, str]:
    """Bring the CDP browser's window to the front.

    Returns (ok, message). `message` explains the failure instead of leaving a mystery:
    the most common real cause is a session mismatch, which no amount of focusing can fix.
    """
    pid = cdp_pid(port)
    if not pid:
        return False, f"端口 {port} 上没有 CDP 浏览器在跑"
    wins = windows_of(pid)
    if not wins:
        proc_session = _session_of(pid)
        mine = _current_session()
        if proc_session != mine and proc_session >= 0:
            return False, (f"浏览器在会话 {proc_session}，而桌面在会话 {mine} —— "
                           f"窗口不会显示在桌面上。计划任务请用 Interactive 而不是 S4U。")
        return False, f"浏览器 PID={pid} 没有可见窗口"
    for hwnd, _title in wins:
        if _raise(hwnd):
            return True, f"已置顶窗口 {hwnd}"
    return False, f"找到 {len(wins)} 个窗口但无法获得前台权限"
