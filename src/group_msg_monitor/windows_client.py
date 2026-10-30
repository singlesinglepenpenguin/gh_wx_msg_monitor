"""Windows client UI and file-usage metadata; never reads keys or messages."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import time

from . import wechat_source  # Resolve the bundled upstream package in source and frozen builds.


class UniqueProcess(ctypes.Structure):
    _fields_ = [('pid', w.DWORD), ('started', w.FILETIME)]


def file_users(path):
    """Ask Windows Restart Manager which processes hold a known account file."""
    if os.name != 'nt':
        return []
    rm = ctypes.WinDLL('rstrtmgr')

    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process', UniqueProcess), ('name', w.WCHAR * 256),
                    ('service', w.WCHAR * 64), ('kind', w.DWORD), ('status', w.ULONG),
                    ('session', w.DWORD), ('restartable', w.BOOL)]

    session = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        files = (w.LPCWSTR * 1)(str(path))
        if rm.RmRegisterResources(session, 1, files, 0, None, 0, None):
            return []
        needed, count, reason = w.UINT(), w.UINT(), w.DWORD()
        status = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reason))
        if status != 234 or not needed.value:
            return []
        processes = (ProcessInfo * needed.value)()
        count.value = needed.value
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), processes, ctypes.byref(reason)):
            return []
        return [processes[i].process.pid for i in range(count.value)]
    finally:
        rm.RmEndSession(session)


def account_in_use(accounts):
    from wechat_native.scanner_win import _list_running_weixin_pids
    running = set(_list_running_weixin_pids())
    matches = []
    for directory in accounts:
        root = Path(directory)
        files = [root / 'session/session.db', root / 'message/message_0.db']
        if any(running.intersection(file_users(path)) for path in files if path.is_file()):
            matches.append(str(root))
    return matches


def open_wechat():
    from wechat_native.scanner_win import _find_wechat_exe_and_dll, _list_running_weixin_pids
    info = _find_wechat_exe_and_dll()
    if not info:
        raise RuntimeError('WIN_CLIENT_MISSING: 未找到已安装的微信，请先安装并登录微信。')
    if not _list_running_weixin_pids():
        subprocess.Popen([info[0]])
    return info[0]


def request_normal_exit(log=lambda line: None):
    """User-requested, non-forced OS close before invoking unchanged upstream.

    Register only identified Weixin processes. Never use RmForceShutdown,
    TerminateProcess, taskkill, or Restart Manager application restart.
    """
    from wechat_native.scanner_win import _list_running_weixin_pids
    pids = _list_running_weixin_pids()
    if not pids:
        return
    main_pids = set()
    user = ctypes.WinDLL('user32')
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(window, _):
        pid = w.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in pids:
            title = ctypes.create_unicode_buffer(128)
            user.GetWindowTextW(window, title, len(title))
            if title.value == '微信':
                main_pids.add(pid.value)
        return True
    user.EnumWindows(callback_type(visit), 0)
    if not main_pids:
        raise RuntimeError('WIN_EXIT_REQUIRED: 未识别到微信主窗口，请从微信托盘菜单正常退出。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [w.HANDLE]
    registered = []
    for pid in sorted(main_pids):
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise RuntimeError('WIN_EXIT_REQUIRED: Windows 拒绝请求微信正常退出，请在微信菜单中退出后重试。')
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered.append(UniqueProcess(pid, times[0]))
        finally:
            kernel.CloseHandle(handle)
    rm = ctypes.WinDLL('rstrtmgr')
    session = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    code = rm.RmStartSession(ctypes.byref(session), 0, key)
    if code:
        raise RuntimeError(f'WIN_EXIT_REQUIRED: RmStartSession error={code}')
    try:
        targets = (UniqueProcess * len(registered))(*registered)
        code = rm.RmRegisterResources(session, 0, None, len(targets), targets, 0, None)
        if not code:
            log('正在请求微信正常退出，等待微信保存本地状态…')
            code = rm.RmShutdown(session, 0, None)  # NO force flag. Session contains only Weixin PIDs.
    finally:
        rm.RmEndSession(session)
    deadline = time.monotonic() + 15
    while _list_running_weixin_pids() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _list_running_weixin_pids():
        raise RuntimeError(f'WIN_EXIT_REQUIRED: 微信仍在保存或尚未退出，未开始初始化；Windows error={code}。请等待后重试。')
    log('微信已正常退出，开始一次性连接。')
