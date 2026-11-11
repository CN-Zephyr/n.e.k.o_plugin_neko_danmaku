"""overlay/win_target: 目标窗口枚举与跟随 (Windows, ctypes 无第三方)。

提供:
  list_displays()               → 显示器列表 (工作区 rect)
  list_windows(main_only=True)  → 可见顶层窗口列表, 分类 normal/game/fullscreen
  get_window_client_rect(hwnd)  → 客户区矩形 (不含边框/标题栏; 游戏无边框=整窗)
  get_foreground_window()       → 当前前台窗口
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

# ---- 常量 ----
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

DWMWA_EXTENDED_FRAME_BOUNDS = 9

# 常见"非游戏"程序 exe —— 小写; 列表给出时标记 kind=normal (应用)
NON_GAME_EXES = {
    "explorer.exe", "msedge.exe", "chrome.exe", "firefox.exe", "opera.exe",
    "n.e.k.o.exe", "dsh-eac-shell.exe", "code.exe", "devenv.exe",
    "notepad.exe", "winword.exe", "excel.exe", "powerpnt.exe",
    "wechat.exe", "qq.exe", "telegram.exe", "discord.exe", "steam.exe",
    "textinputhost.exe", "searchhost.exe", "dwm.exe", "pwsh.exe",
    "cmd.exe", "windowsterminal.exe", "mstsc.exe", "zoom.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "applicationframehost.exe", "systemsettings.exe",
}


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wt.DWORD)]


def _declare() -> None:
    u = user32
    u.EnumWindows.restype = ctypes.c_bool
    u.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.GetWindowRect.restype = ctypes.c_bool
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.IsIconic.restype = ctypes.c_bool
    u.IsIconic.argtypes = [wt.HWND]
    u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    u.GetAncestor.restype = wt.HWND
    u.GetForegroundWindow.restype = wt.HWND
    u.GetForegroundWindow.argtypes = []
    u.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    u.MonitorFromWindow.restype = wt.HANDLE
    u.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
    u.GetMonitorInfoW.restype = ctypes.c_bool
    u.IsWindow.restype = ctypes.c_bool
    u.IsWindow.argtypes = [wt.HWND]
    u.GetClientRect.restype = ctypes.c_bool
    u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    u.ClientToScreen.restype = ctypes.c_bool
    u.IsZoomed.restype = ctypes.c_bool
    u.IsZoomed.argtypes = [wt.HWND]
    # DwmGetWindowAttribute
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]


_declare()


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid: int) -> str:
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return buf.value.rsplit("\\", 1)[-1]
    except Exception:
        return ""


def _top_visible_window(hwnd: int) -> int:
    """返回该窗口所属的顶层(无父)窗口, 0 表示无效。"""
    if not hwnd:
        return 0
    return user32.GetAncestor(hwnd, GA_ROOT)


def get_window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口客户区矩形 (屏幕坐标, 物理像素)。

    用 DWM 扩展边框(不含投影/描边)优先; 再减标题栏/边框到纯客户区。
    无边框全屏/游戏窗口 → 客户区≈整窗≈全屏。普通窗口 → 内容区。
    返回 (x, y, w, h)。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd):
        return None
    # 1) 整窗矩形 (含边框标题栏)
    wr = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        return None
    # 2) 客户区矩形 (窗口内坐标) → 屏幕坐标
    cr = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    if cr.right <= 0 or cr.bottom <= 0:
        return None
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, cr.right, cr.bottom)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """整窗矩形 (含边框)。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd):
        return None
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def get_foreground_window() -> int | None:
    """当前前台(活动)窗口的顶层句柄。"""
    h = user32.GetForegroundWindow()
    if not h:
        return None
    return _top_visible_window(h) or None


def _monitor_of(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在显示器的 rcMonitor (物理)。"""
    mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return (0, 0, 0, 0)


def _classify(exe: str, w: int, h: int, mw: int, mh: int) -> str:
    """窗口分类: fullscreen(占满所在显示器) / game(近满屏非白名单) / normal。

    fullscreen: 尺寸≈所在屏 → 覆盖整屏(游戏无边框全屏常见)。
    game: exe 非白名单且窗口 ≥ 屏 60% 面积(窗口化游戏)。
    normal: 其余(普通应用/小窗)。
    """
    if mw > 0 and w >= mw - 4 and h >= mh - 4:
        return "fullscreen"
    exe_l = (exe or "").lower()
    if exe_l and exe_l not in NON_GAME_EXES and mw > 0 and \
       w >= mw * 0.6 and h >= mh * 0.6:
        return "game"
    return "normal"


def list_windows(main_only: bool = True) -> list[dict]:
    """枚举可见顶层窗口, 分类 normal/game/fullscreen。

    每条含: hwnd/title/exe/w/h/fullscreen(整窗近全屏)/kind(分类)/foreground(是否前台)
    """
    out: list[dict] = []
    results = []
    fg = get_foreground_window()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lp):
        results.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    seen = set()
    for hwnd in results:
        root = _top_visible_window(hwnd)
        if not root or root in seen:
            continue
        if not user32.IsWindowVisible(root):
            continue
        if user32.IsIconic(root):
            continue
        n = user32.GetWindowTextLengthW(root)
        if n <= 0:
            continue
        title_buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(root, title_buf, n + 1)
        title = title_buf.value.strip()
        if not title:
            continue
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(root, cls_buf, 64)
        cls = cls_buf.value
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "XamlExplorerHostIslandWindow"):
            continue
        ex = user32.GetWindowLongW(root, -20)
        if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
            continue
        rect = RECT()
        if not user32.GetWindowRect(root, ctypes.byref(rect)):
            continue
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < 80 or h < 60:
            continue
        pid = _window_pid(root)
        exe = _process_name(pid)
        seen.add(root)
        _mx, _my, mw, mh = _monitor_of(root)
        is_fullscreen = (mw > 0 and w >= mw - 4 and h >= mh - 4)
        kind = _classify(exe, w, h, mw, mh)
        # 客户区 (用于绑定; 可能比整窗小)
        client = get_window_client_rect(root)
        entry = {
            "hwnd": root,
            "title": title,
            "cls": cls,
            "pid": pid,
            "exe": exe,
            "x": rect.left, "y": rect.top, "w": w, "h": h,
            "client": list(client) if client else [rect.left, rect.top, w, h],
            "fullscreen": is_fullscreen,
            "kind": kind,
            "foreground": root == fg,
        }
        out.append(entry)
    # 分组排序: 前台→全屏→游戏→普通(面积)
    rank = {"fullscreen": 0, "game": 1, "normal": 2}
    out.sort(key=lambda e: (not e["foreground"], rank.get(e["kind"], 9),
                            -(e["w"] * e["h"])))
    return out


# ---- 进程聚合 (OBS 式: 按 pid 选目标, 窗口重建/切全屏自动重捕获) ----
def _enum_top_windows() -> list[tuple[int, int]]:
    """枚举所有可见顶层窗口 → [(hwnd, pid)] (去重, 已过滤托盘/桌面)。"""
    results: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lp):
        results.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    seen: set[int] = set()
    out: list[tuple[int, int]] = []
    for hwnd in results:
        root = _top_visible_window(hwnd)
        if not root or root in seen:
            continue
        if not user32.IsWindowVisible(root) or user32.IsIconic(root):
            continue
        n = user32.GetWindowTextLengthW(root)
        if n <= 0:
            continue
        title_buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(root, title_buf, n + 1)
        if not title_buf.value.strip():
            continue
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(root, cls_buf, 64)
        if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                             "XamlExplorerHostIslandWindow"):
            continue
        ex = user32.GetWindowLongW(root, -20)
        if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
            continue
        rect = RECT()
        if not user32.GetWindowRect(root, ctypes.byref(rect)):
            continue
        if (rect.right - rect.left) < 80 or (rect.bottom - rect.top) < 60:
            continue
        pid = _window_pid(root)
        seen.add(root)
        out.append((root, pid))
    return out


def _describe_window(hwnd: int) -> dict | None:
    """把顶层窗口 hwnd 描述成列表条目 (复用 list_windows 逻辑)。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, title_buf, n + 1)
    title = title_buf.value.strip()
    cls_buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls_buf, 64)
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    pid = _window_pid(hwnd)
    exe = _process_name(pid)
    _mx, _my, mw, mh = _monitor_of(hwnd)
    client = get_window_client_rect(hwnd)
    return {
        "hwnd": hwnd, "title": title, "cls": cls_buf.value,
        "pid": pid, "exe": exe,
        "x": rect.left, "y": rect.top, "w": w, "h": h,
        "client": list(client) if client else [rect.left, rect.top, w, h],
        "fullscreen": bool(mw > 0 and w >= mw - 4 and h >= mh - 4),
        "kind": _classify(exe, w, h, mw, mh),
        "foreground": hwnd == get_foreground_window(),
    }


def get_pid_main_window(pid: int) -> int | None:
    """某 pid 当前的"主窗口"(可见顶层中面积最大, 前台优先)。窗口重建后自动换新句柄。"""
    if pid <= 0:
        return None
    cands = [(hwnd, wpid) for hwnd, wpid in _enum_top_windows() if wpid == pid]
    if not cands:
        return None
    # 前台窗口优先, 其次面积最大
    fg = get_foreground_window()
    best = None
    best_area = -1
    for hwnd, _wpid in cands:
        area = 0
        if not user32.IsIconic(hwnd):
            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if hwnd == fg:
            return hwnd
        if area > best_area:
            best_area = area
            best = hwnd
    return best


def list_processes() -> list[dict]:
    """按进程聚合的候选列表 (OBS 式): 每 pid 一条主窗口。

    返回每条含 pid/exe/title/client/w/h/kind/fullscreen/foreground ——
    绑定用 pid, 浮层每次跟随自动取该 pid 当前主窗口 (不粘死 hwnd)。
    """
    by_pid: dict[int, dict] = {}
    for hwnd, pid in _enum_top_windows():
        # 同 pid 多窗口: 保留当前主窗口 (fg 优先/面积最大), 用 _describe 竞争
        cur = by_pid.get(pid)
        desc = _describe_window(hwnd)
        if desc is None:
            continue
        if cur is None:
            by_pid[pid] = desc
            continue
        cur_area = cur["w"] * cur["h"]
        new_area = desc["w"] * desc["h"]
        # 前台 > 面积更大 > 全屏优先
        if desc["foreground"] and not cur["foreground"]:
            by_pid[pid] = desc
        elif cur["foreground"] == desc["foreground"] and new_area > cur_area:
            by_pid[pid] = desc
    out = list(by_pid.values())
    rank = {"fullscreen": 0, "game": 1, "normal": 2}
    out.sort(key=lambda e: (not e["foreground"], rank.get(e["kind"], 9),
                            -(e["w"] * e["h"])))
    return out


# ---- 目标遮挡检测 (被大窗口盖住/最小化 → 暂停弹幕) ----
def _rect_center(rect: RECT) -> tuple[int, int]:
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def _is_self_overlay(hwnd: int) -> bool:
    """是否是我们自己的浮层 (overlay_danmaku 或同进程 Qt 窗口)。"""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return False
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return "overlay_danmaku" in buf.value.lower()


def _z_above(hwnd: int) -> list[int]:
    """z-order 中位于 hwnd 之上的兄弟顶层窗口列表 (含 hwnd 自身)。"""
    out: list[int] = []
    w = hwnd
    while w:
        out.append(w)
        w = user32.GetWindow(w, 3)   # GW_HWNDPREV
    return out


def _window_rect(hwnd: int) -> RECT | None:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect


def covers_monitor(hwnd: int, rect: RECT | None = None) -> bool:
    """窗口是否铺满所在显示器 (全屏游戏 / 无边框全屏)。

    容差 8px: 无边框全屏常比显示器小几像素, 或带 -8,8 的隐藏边框。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    r = rect or _window_rect(hwnd)
    if r is None:
        return False
    mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    if not mon:
        return False
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return ((r.right - r.left) >= (m.right - m.left) - 8 and
            (r.bottom - r.top) >= (m.bottom - m.top) - 8)


def is_target_obscured(hwnd: int) -> bool:
    """目标窗口是否被遮挡 (中心点被 z-order 更高的可见窗口盖住) 或不可见。

    返回 True 表示应暂停弹幕渲染: 最小化/销毁, 或被别的窗口盖住弹幕区。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return True
    if user32.IsIconic(hwnd):
        return True
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return True
    # 全屏游戏(无边框全屏)只要它在前台就视为可见: 全屏时其它置顶/透明小窗
    # 常被 z-order 误判成"遮挡", 会把弹幕整层隐藏掉。
    if covers_monitor(hwnd, rect) and get_foreground_window() == hwnd:
        return False
    cx, cy = _rect_center(rect)
    for above in _z_above(hwnd):
        if above == hwnd:
            continue
        # 自己浮层 / 桌面 / 输入法透明窗不算遮挡
        if _is_self_overlay(above):
            continue
        if not user32.IsWindowVisible(above) or user32.IsIconic(above):
            continue
        ar = RECT()
        if not user32.GetWindowRect(above, ctypes.byref(ar)):
            continue
        if ar.left <= cx <= ar.right and ar.top <= cy <= ar.bottom:
            return True
    return False


# ---- 显示器 ----
def list_displays() -> list[dict]:
    """枚举显示器: idx, name 缺省(取 'DISPLAY<i>'), work=(x,y,w,h), is_primary。"""
    monitors: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HANDLE, wt.HDC,
                        ctypes.POINTER(RECT), wt.LPARAM)
    def _cb(handle, _hdc, _rect, _lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(handle, ctypes.byref(mi))
        r = mi.rcWork
        monitors.append({
            "x": r.left, "y": r.top,
            "w": r.right - r.left, "h": r.bottom - r.top,
        })
        return True

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    if not monitors:
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        monitors = [{"x": x, "y": y, "w": w, "h": h}]
    # 主显示器排第一 (含 (0,0) 的)
    monitors.sort(key=lambda m: not (m["x"] == 0 and m["y"] == 0))
    for i, m in enumerate(monitors):
        m["id"] = i
        m["name"] = f"显示器 {i + 1}"
    return monitors
