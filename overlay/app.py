"""overlay_danmaku v2 —— Qt 弹幕浮层（样式系统版）。

在 v1(穿透/热键/轨道/HTTP) 基础上:
- 渲染: 实心正文 + QPainterPathStroker 均匀外描边 (无背景默认, 可选极淡底条)
- 样式系统: 每条弹幕可带 style 预设; 全局默认样式可热调
- HTTP:
    POST /push  {text, color?, style?}            推弹幕 (style 覆盖该条)
    POST /config {preset?, text_color?, font_size?, opacity?, bg?, speed?, ...}  热调默认
    GET  /presets                                  列出可用预设
- 参数化: 字体/字号/文字色/描边色宽/底条(关/淡/柔和)/透明度/速度/轨道
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QWidget

try:
    import win_target as _wt  # 同目录模块 (Windows); 失败则目标绑定不可用
except Exception:
    _wt = None

# ── Win32 原生窗口助手 (穿透/置顶, 避免 Qt setWindowFlags 隐藏闪烁) ──
def _win32():
    import ctypes
    return ctypes.windll.user32, ctypes.windll.kernel32, ctypes


def _native_hwnd(win) -> int:
    try:
        return int(win.winId())
    except Exception:
        return 0


def _set_passthrough_native(win, on: bool) -> None:
    """原生切换鼠标穿透 (WS_EX_TRANSPARENT), 不重建窗口。"""
    try:
        user32, _k, ctypes = _win32()
        hwnd = _native_hwnd(win)
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_LAYERED = 0x00080000
        WS_EX_NOACTIVATE = 0x08000000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if on:
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        ex |= WS_EX_LAYERED | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        # 强制重绘 (Qt 透明层需刷新才生效)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0020 | 0x0100)  # NOSIZE|NOMOVE|NOACTIVATE|FRAMECHANGED
    except Exception:
        pass


def _keep_topmost(win) -> None:
    """原生置顶保持: 每 500ms 置 HWND_TOPMOST, 抵抗其它置顶窗口/宿主前端。"""
    try:
        user32, _k, _ctypes = _win32()
        hwnd = _native_hwnd(win)
        if hwnd:
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0008)  # HWND_TOPMOST, NOSIZE|NOMOVE|NOACTIVATE
    except Exception:
        pass

# ── 默认参数 ─────────────────────────────────────────────
DEFAULT_SPEED_PX_S = 190.0
DEFAULT_FONT_SIZE = 30
DEFAULT_OPACITY = 1.0
MAX_TRACKS = 8
TOP_MARGIN = 12
TRACK_HEIGHT = 52
TRACK_GAP = 6
SCREEN_MARGIN_RIGHT = 40
WINDOW_STATE_FILE = "overlay_state.json"
DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
SPAWN_INTERVAL = 0.07        # 两条弹幕最小入场间隔(秒): 按时间节奏逐条入场
COLLIDE_GAP = 24.0           # 同轨道弹幕之间最小间距(px), 防碰撞缓冲
FRAME_INTERVAL_MS = 16       # 绘制帧间隔
PROCESS_LOOKUP_TTL_S = 0.75  # 按进程找主窗口的全量枚举频率
OBSCURE_CHECK_TTL_S = 0.5    # 遮挡检测频率

# ── 视觉观感参数 (0.2.x 起) ─────────────────────────────
FADE_IN_MS = 130             # 入场淡入时长
FADE_OUT_PX = 150            # 左边缘最后 150px 内淡出
MAX_ITEMS = 42               # 同屏条数上限(超出排队, 防止糊屏)
WRAP_RATIO = 0.62            # 自动折行宽度 = 屏宽 × 该比例
MAX_LINES = 3                # 自动折行最多行数(超出加省略号)
BASE_SCREEN_W = 1920         # 字号自适应基准屏宽

# ── 样式预设 ─────────────────────────────────────────────
# 每项字段: text_color / stroke_color / stroke_width / font_size / font_family
#           bold / bg(kind: none|light|soft, 透明度) / shadow(bool)
# 附加全局: opacity / speed (通过 /config 或 state 热调)
PRESETS: dict[str, dict[str, Any]] = {
    # 纯实心字 + 细描边, 无背景 (当前验证 OK 的干净风格)
    "clean": {
        "text_color": "#ffffff", "stroke_color": "#101018",
        "stroke_width": 3.0, "font_size": 30, "bold": True,
        "bg": "none", "shadow": False,
    },
    # 主人: 暖白字 + 深描边 + 轻阴影, 无底条 (干净利落)
    "master": {
        "text_color": "#fff6e5", "stroke_color": "#161821",
        "stroke_width": 3.0, "font_size": 30, "bold": True,
        "bg": "none", "shadow": True, "shadow_blur": 5, "shadow_dy": 2,
        "shadow_alpha": 150, "letter_spacing": 0.4,
    },
    # 猫娘: 亮蓝 + 半透明圆角气泡 (和主人一眼区分, 有"对话感")
    "neko": {
        "text_color": "#a8e2ff", "stroke_color": "#08243a",
        "stroke_width": 3.0, "font_size": 30, "bold": True,
        "bg": "light", "bg_alpha": 70, "bg_radius": 12,
        "shadow": True, "shadow_blur": 5, "shadow_dy": 2, "shadow_alpha": 140,
        "letter_spacing": 0.5,
    },
    # 香槟: 强调/彩蛋
    "champagne": {
        "text_color": "#ffd78c", "stroke_color": "#4a2a08",
        "stroke_width": 3.0, "font_size": 32, "bold": True,
        "bg": "none", "shadow": True,
    },
    # 柔和深底 (可选 "有背景" 的观感)
    "softbar": {
        "text_color": "#ffffff", "stroke_color": "#101018",
        "stroke_width": 2.5, "font_size": 30, "bold": True,
        "bg": "soft", "shadow": False,
    },
    # 粉色强调
    "pink": {
        "text_color": "#ffb3d9", "stroke_color": "#3a0a22",
        "stroke_width": 3.0, "font_size": 30, "bold": True,
        "bg": "none", "shadow": True,
    },
    # 彩色混合: 每条从 color_pool 随机取一色 + 半透明气泡 (猫娘默认, 弹幕感最强)
    "mix": {
        "text_color": "#ffffff", "stroke_color": "#101018",
        "stroke_width": 3.0, "font_size": 30, "bold": True,
        "bg": "light", "bg_alpha": 58, "bg_radius": 12,
        "shadow": True, "shadow_blur": 5, "shadow_dy": 2, "shadow_alpha": 140,
        "letter_spacing": 0.5,
        "color_pool": ["#9adcff", "#ff9ad5", "#ffd78c",
                       "#b3ff8f", "#c9a7ff", "#ffb36b"],
    },
}

# mix 池里每一色搭配的深描边 (保证浅色字在深/浅背景都可读)
_MIX_STROKE = {
    "#ffffff": "#101018", "#9adcff": "#0a2438", "#ff9ad5": "#3a0a22",
    "#ffd78c": "#4a2a08", "#b3ff8f": "#123a0a", "#c9a7ff": "#24103a",
    "#ffb36b": "#3a1c08",
}


def _pick_mix_color(style: dict) -> dict:
    """若 style 带 color_pool, 随机取一色并配深描边 (每条弹幕固定一次)。"""
    pool = style.get("color_pool")
    if not pool:
        return style
    import random
    col = random.choice(list(pool))
    out = dict(style)
    out["text_color"] = col
    if col in _MIX_STROKE:
        out["stroke_color"] = _MIX_STROKE[col]
    out.pop("color_pool", None)
    return out


def merge_style(base: dict, patch: dict | None) -> dict:
    out = dict(base)
    if patch:
        out.update({k: v for k, v in patch.items() if v is not None})
    return out


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _bounded_number(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    return int(round(_bounded_number(value, default, minimum, maximum)))


def _color(value, default: str) -> str:
    text = str(value or "")
    return text if _COLOR_RE.fullmatch(text) else default


def sanitize_style(raw: dict | None, base: dict | None = None) -> dict:
    """Clamp untrusted style values before they reach the paint path."""
    style = merge_style(dict(PRESETS["clean"] if base is None else base), raw)
    style["font_size"] = _bounded_int(style.get("font_size"), DEFAULT_FONT_SIZE, 12, 96)
    style["stroke_width"] = _bounded_number(style.get("stroke_width"), 3.0, 0.0, 16.0)
    style["shadow_blur"] = _bounded_number(style.get("shadow_blur"), 0.0, 0.0, 40.0)
    style["shadow_dx"] = _bounded_number(style.get("shadow_dx"), 0.0, -40.0, 40.0)
    style["shadow_dy"] = _bounded_number(style.get("shadow_dy"), 3.0, -40.0, 40.0)
    style["shadow_alpha"] = _bounded_int(style.get("shadow_alpha"), 180, 0, 255)
    style["glow_blur"] = _bounded_number(style.get("glow_blur"), 0.0, 0.0, 60.0)
    style["bg_alpha"] = _bounded_int(style.get("bg_alpha"), 80, 0, 255)
    style["bg_radius"] = _bounded_int(style.get("bg_radius"), 10, 0, 40)
    style["max_items"] = _bounded_int(style.get("max_items"), MAX_ITEMS, 1, 200)
    style["max_lines"] = _bounded_int(style.get("max_lines"), MAX_LINES, 1, 10)
    style["fade_in_ms"] = _bounded_number(style.get("fade_in_ms"), FADE_IN_MS, 0, 1000)
    style["line_spacing"] = _bounded_number(style.get("line_spacing"), 1.12, 0.8, 3.0)
    style["letter_spacing"] = _bounded_number(style.get("letter_spacing"), 0.0, -5.0, 20.0)
    style["text_color"] = _color(style.get("text_color"), "#ffffff")
    style["font_family"] = str(style.get("font_family") or DEFAULT_FONT_FAMILY)[:120]
    style["stroke_color"] = _color(style.get("stroke_color"), "#101018")
    style["shadow_color"] = _color(style.get("shadow_color"), "#000000")
    style["glow_color"] = _color(style.get("glow_color"), "#ffd78c")
    style["bg_color"] = _color(style.get("bg_color"), "#0a0a14")
    if style.get("bg") not in ("none", "light", "soft", "custom"):
        style["bg"] = "none"
    gradient = style.get("fill_gradient")
    if gradient is not None:
        if not isinstance(gradient, (list, tuple)) or len(gradient) < 2:
            style.pop("fill_gradient", None)
        else:
            top = _color(gradient[0], "")
            bottom = _color(gradient[1], "")
            if top and bottom:
                style["fill_gradient"] = [top, bottom]
            else:
                style.pop("fill_gradient", None)
    style["bold"] = bool(style.get("bold", True))
    style["shadow"] = bool(style.get("shadow", False))
    style["glow"] = bool(style.get("glow", False))
    style["wrap"] = bool(style.get("wrap", True))
    style["auto_scale"] = bool(style.get("auto_scale", True))
    return style


@dataclass
class Danmaku:
    """一条待显示/显示中的弹幕。"""
    text: str
    style: dict = field(default_factory=dict)
    # 渲染期字段
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    speed: float = DEFAULT_SPEED_PX_S
    speed_factor: float = 1.0    # 每条速度抖动系数(弹幕感: 快慢不一)
    y_jitter: float = 0.0        # 垂直抖动(px, 避免整整齐齐的"字幕感")
    born: float = field(default_factory=time.time)
    lane: int | None = None
    entered: bool = False   # True=已分配轨道并入场; False=仍在排队等待轨道
    sprite: object | None = None   # 预渲染 RGBA QImage (push 时生成)


class DanmakuOverlay(QWidget):
    """透明置顶浮层。整窗绘制弹幕，穿透/交互由窗口标志切换。"""

    stateChanged = Signal(str)
    # (text, style_dict)
    pushRequested = Signal(str, object)
    configRequested = Signal(object)   # HTTP 线程 → Qt 主线程应用配置
    targetRequested = Signal(object)   # HTTP 线程 → Qt 主线程执行目标切换
    interactRequested = Signal(bool)   # 热键线程 → Qt 主线程切换穿透/交互
    exitRequested = Signal()           # 热键线程 → 退出

    def __init__(
        self, port: int = 48777, state_path: str | Path | None = None
    ) -> None:
        super().__init__()
        self._port = port
        self._state_file = Path(state_path or WINDOW_STATE_FILE)
        self._items: list[Danmaku] = []
        self._paused = False
        self._passthrough = True
        self._dragging = False
        self._drag_offset = (0, 0)
        # 退出协调 (run_overlay 清理时置位; 热键线程轮询退出)
        self._hotkey_stop = threading.Event()

        # 目标绑定 (None=默认全主屏)
        self._target = None          # dict: {kind: display|window|screen|process, ...}
        self._target_hwnd = None
        self._target_pid = None
        self._last_target_rect = None
        # 目标被遮挡/最小化 → 暂停弹幕渲染, 恢复后继续
        self._obscured = False

        # 全局当前样式 (默认 clean; /config 与 state 热调)
        self.style = dict(PRESETS["clean"])
        self._speed = DEFAULT_SPEED_PX_S
        self._opacity = DEFAULT_OPACITY
        # 弹幕区位置 (top/center/bottom + 像素偏移)
        self._area = "top"
        self._area_offset = 0

        # 交互超时自动回穿透 (秒; 0=手动保持)
        self._interact_timeout_s = 6.0
        self._last_activity = 0.0
        self._last_process_lookup = 0.0
        self._last_obscure_check = 0.0
        self._fps = 0.0
        self._tick_ms = 0.0
        self._fps_frames = 0
        self._fps_started = time.perf_counter()

        self._load_state()
        self.style = sanitize_style(self.style)
        self._setup_window()
        self._apply_passthrough()
        self.pushRequested.connect(self.push)
        self.configRequested.connect(self.apply_config)
        self.targetRequested.connect(self._apply_target)
        self.interactRequested.connect(self.set_interactive)
        self.exitRequested.connect(self.close)
        self._start_http_server()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(FRAME_INTERVAL_MS)

        self._follow_timer = QTimer(self)      # 目标跟随 (窗口移动/缩放自动贴合)
        self._follow_timer.timeout.connect(self._follow_once)
        self._follow_timer.start(100)   # 窗口 rect 高频读取; 进程枚举另做节流

        # 原生置顶保持: 每 500ms 强制 HWND_TOPMOST, 保证弹幕盖在目标之上
        # (抗宿主前端/其它置顶窗口; 独占全屏游戏仍受 Windows 限制需无边框全屏)
        self._top_timer = QTimer(self)
        self._top_timer.timeout.connect(lambda: _keep_topmost(self))
        self._top_timer.start(1000)

        self._demo_seq = 0

    # ── HTTP ──────────────────────────────────────────────
    def _start_http_server(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        win = self

        class _Handler(BaseHTTPRequestHandler):
            def _read_json(self) -> dict:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

            def _reply(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                try:
                    data = self._read_json()
                    if self.path == "/push":
                        text = str(data.get("text", "")).strip()
                        # 未选目标 → 不收; 目标被遮挡(窗口盖住/最小化) → 暂不收
                        if win._target is None:
                            self._reply(200, {"ok": True, "accepted": False,
                                              "reason": "no_target"})
                            return
                        if win._obscured:
                            self._reply(200, {"ok": True, "accepted": False,
                                              "reason": "obscured"})
                            return
                        if text:
                            preset = str(data.get("preset") or "")
                            if preset in PRESETS:
                                # 角色预设隔离: 以 clean 为底叠加该预设, 不继承全局样式残留
                                # (否则全局切过 mix 之后, 主人条目也会被塞 color_pool, 两边就一样了)
                                st = merge_style(dict(PRESETS["clean"]), PRESETS[preset])
                            else:
                                st = dict(win.style)
                            if data.get("style"):
                                st = merge_style(st, data["style"])
                            st = sanitize_style(st)
                            st["_isolated"] = True
                            win.pushRequested.emit(text, st)
                        self._reply(200, {"ok": True, "accepted": True})
                    elif self.path == "/config":
                        win.configRequested.emit(data)
                        self._reply(200, {"ok": True})
                    elif self.path == "/target":
                        res = win.set_target(data)
                        self._reply(200 if res.get("ok") else 400, res)
                    elif self.path == "/quit":
                        # 线程安全退出 Qt 事件循环 (HTTP 线程 → Qt 线程)
                        from PySide6.QtCore import QMetaObject
                        app = QApplication.instance()
                        if app is not None:
                            QMetaObject.invokeMethod(
                                app, "quit", Qt.ConnectionType.QueuedConnection)
                        self._reply(200, {"ok": True})
                    else:
                        self._reply(404, {"ok": False})
                except Exception as ex:
                    self._reply(400, {"ok": False, "error": str(ex)})

            def do_GET(self):  # noqa: N802
                try:
                    if self.path == "/presets":
                        self._reply(200, {"presets": {k: v for k, v in PRESETS.items()}})
                    elif self.path == "/windows":
                        self._reply(200, {"windows": win.list_windows()})
                    elif self.path == "/processes":
                        self._reply(200, {"processes": win.list_processes()})
                    elif self.path == "/target":
                        self._reply(200, win.target_status())
                    else:
                        self._reply(200, {"ok": True, "style": win.style,
                                          "speed": win._speed, "opacity": win._opacity,
                                          "presets": list(PRESETS),
                                          "target": win._target,
                                          "target_hwnd": win._target_hwnd,
                                          "target_rect": win._last_target_rect,
                                          "obscured": win._obscured,
                                          "pending_target": bool(getattr(win, "_pending_spec", None)),
                                          "dropped": int(getattr(win, "_dropped", 0)),
                                          "items": len(win._items),
                                          "entered": sum(1 for d in win._items if d.entered),
                                          "fps": round(float(getattr(win, "_fps", 0.0)), 1),
                                          "tick_ms": round(float(getattr(win, "_tick_ms", 0.0)), 2),
                                          "area": win._area,
                                          "area_offset": win._area_offset,
                                          "tracks": win._max_tracks()})
                except Exception as ex:
                    self._reply(400, {"ok": False, "error": str(ex)})

            def log_message(self, *a):  # noqa: N802
                pass

        self._http = HTTPServer(("127.0.0.1", self._port), _Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        print(f"[overlay] http://127.0.0.1:{self._port}/push|/config|/presets", flush=True)

    def apply_config(self, data: dict) -> None:
        """热调默认样式/全局参数 (任何线程安全调用即可, 实际只写字段)。"""
        if data.get("preset") and data["preset"] in PRESETS:
            self.style = merge_style(self.style, PRESETS[data["preset"]])
        # 显式字段覆盖 (与渲染引擎支持的键一致)
        # 语义: 值=null → 删除该键(回到默认/关闭该效果); 值≠null → 设置。
        null_defaults = {
            "fill_gradient": None,   # None=无渐变
            "shadow": False,         # False=关阴影
            "shadow_blur": 0, "shadow_dx": 0, "shadow_dy": 3,
            "shadow_color": "#000000", "shadow_alpha": 180,
            "glow": False,           # False=关辉光
            "glow_blur": 0, "glow_color": "#ffd78c",
            "bg_color": "#0a0a14", "bg_alpha": 80, "bg_radius": 10,
            # 观感类参数: null → 回到默认
            "max_items": MAX_ITEMS, "wrap": True, "max_lines": MAX_LINES,
            "auto_scale": True, "fade_in_ms": FADE_IN_MS,
            "line_spacing": 1.12, "letter_spacing": 0,
        }
        for key in (
            "text_color", "stroke_color", "stroke_width", "font_size",
            "font_family", "bold", "bg", "bg_color", "bg_alpha", "bg_radius",
            "shadow", "shadow_blur", "shadow_dx", "shadow_dy", "shadow_color",
            "shadow_alpha", "glow", "glow_blur", "glow_color", "fill_gradient",
            "max_items", "wrap", "max_lines", "auto_scale", "fade_in_ms",
            "line_spacing", "letter_spacing",
        ):
            if key in data:
                if data[key] is None:
                    # null → 删除该键; 若预设缺省已含此键, 切回其默认
                    if key in PRESETS.get("clean", {}):
                        self.style[key] = PRESETS["clean"][key]
                    elif key in null_defaults:
                        if null_defaults[key] is None:
                            self.style.pop(key, None)
                        else:
                            self.style[key] = null_defaults[key]
                else:
                    self.style[key] = data[key]
        if data.get("opacity") is not None:
            self._opacity = _bounded_number(data["opacity"], self._opacity, 0.05, 1.0)
        if data.get("speed") is not None:
            self._speed = _bounded_number(data["speed"], self._speed, 20.0, 1000.0)
        # 弹幕区位置 / 交互超时
        if data.get("area") in ("top", "center", "bottom"):
            self._area = data["area"]
        if data.get("area_offset") is not None:
            self._area_offset = _bounded_int(
                data["area_offset"], self._area_offset, -4000, 4000
            )
        if data.get("interact_timeout") is not None:
            self._interact_timeout_s = _bounded_number(
                data["interact_timeout"], self._interact_timeout_s, 0.0, 60.0
            )
        if data.get("passthrough") is not None:
            self.set_interactive(not bool(data["passthrough"]))
        self.style = sanitize_style(self.style)
        self.setWindowOpacity(max(0.05, min(1.0, self._opacity)))
        self._reflow_all()
        self._save_state()
        self.stateChanged.emit("config")

    # ── 窗口 ──────────────────────────────────────────────
    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(scr)
        self.setWindowTitle("overlay_danmaku")

    def _apply_passthrough(self) -> None:
        # 原生切换穿透 (不重建窗口, 避免闪烁/掉置顶)
        _set_passthrough_native(self, self._passthrough)
        self.stateChanged.emit("passthrough" if self._passthrough else "interactive")

    def set_interactive(self, on: bool) -> None:
        """切换 穿透 ⇄ 交互。交互是临时态: 超时(或 F5 关闭)自动回穿透。"""
        if on == (not self._passthrough):
            return
        self._passthrough = not on
        self._last_activity = time.time()
        self._apply_passthrough()

    def _note_activity(self) -> None:
        self._last_activity = time.time()

    # 交互态事件 (仅非穿透时收到; 穿透态事件落给下层窗口)
    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if self._passthrough:
            return
        self._note_activity()
        # 点击: 命中弹幕 → 暂停该条
        if ev.button() == Qt.LeftButton:
            for d in reversed(self._items):
                if d.entered and d.sprite is not None and \
                   d.x <= ev.position().x() <= d.x + d.sprite.width() and \
                   d.y <= ev.position().y() <= d.y + d.sprite.height():
                    d.paused = not getattr(d, "paused", False)
                    self.stateChanged.emit("click-pause")
                    return

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if self._passthrough:
            return
        self._note_activity()
        delta = ev.angleDelta().y()
        fs = int(self.style.get("font_size", 34))
        fs = max(12, min(80, fs + (2 if delta > 0 else -2)))
        self.style["font_size"] = fs
        self._reflow_all()
        self.stateChanged.emit(f"fontsize={fs}")

    def _area_y0(self) -> float:
        """弹幕区起始 y: 依 area(顶/中/底) + area_offset。"""
        if self._area == "center":
            total = self._max_tracks() * self._track_pitch()
            return max(0.0, (self.height() - total) / 2 + self._area_offset)
        if self._area == "bottom":
            total = self._max_tracks() * self._track_pitch()
            return max(0.0, self.height() - total - 24 + self._area_offset)
        return TOP_MARGIN + self._area_offset   # top

    # ── 目标绑定 (OBS 式: 显示器/窗口, 自动跟随) ──────────
    def set_target(self, spec: dict) -> dict:
        """设置浮层覆盖目标 (线程安全: 校验后发信号到主线程执行)。"""
        if not _wt:
            return {"ok": False, "error": "目标绑定不可用 (win_target 加载失败)"}
        kind = spec.get("kind", "screen")
        if kind == "window":
            hwnd = int(spec.get("hwnd", 0))
            getter = (_wt.get_window_client_rect
                      if spec.get("mode", "client") == "client"
                      else _wt.get_window_rect)
            if not hwnd or getter(hwnd) is None:
                return {"ok": False, "error": f"窗口 {hwnd} 无效或已关闭"}
        elif kind == "process":
            pid = int(spec.get("pid", 0))
            if pid <= 0 or _wt.get_pid_main_window(pid) is None:
                return {"ok": False, "error": f"进程 {pid} 无可见窗口"}
        elif kind == "display":
            did = int(spec.get("id", 0))
            displays = _wt.list_displays()
            if not any(d["id"] == did for d in displays):
                return {"ok": False, "error": f"显示器 {did} 不存在"}
        elif kind != "screen":
            return {"ok": False, "error": f"未知目标 kind={kind}"}
        # 排队到主线程执行 (HTTP 线程不能碰 Qt)
        self.targetRequested.emit(dict(spec))
        return {"ok": True, "pending": kind}

    def _to_logical(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        """win32 物理像素 rect → Qt 逻辑像素 (按中心点所在屏换算, 支持多屏不同 dpr)。

        物理坐标系(win32 全局): 每屏物理尺寸 = 逻辑尺寸 × dpr, 原点按布局排列。
        Qt 逻辑坐标系: 每屏 geometry() 为逻辑尺寸, 原点可能带缩放间隙。
        换算: 先定位物理中心在哪块屏 (用该屏物理范围 = geometry×dpr 平移到
        物理布局), 再 (物理 - 屏物理原点)/dpr + 屏逻辑原点。
        """
        try:
            screens = QGuiApplication.screens()
            cx, cy = x + w // 2, y + h // 2
            # 第一遍定位: 用 win32 显示器物理 rect 定位屏 → 逻辑原点映射
            if _wt:
                for s, d in zip(screens, _wt.list_displays()):
                    dpr = s.devicePixelRatio()
                    px0, py0 = d["x"], d["y"]
                    pw, ph = d["w"], d["h"]
                    if px0 <= cx < px0 + pw and py0 <= cy < py0 + ph:
                        lx0, ly0 = s.geometry().x(), s.geometry().y()
                        return (lx0 + round((x - px0) / dpr),
                                ly0 + round((y - py0) / dpr),
                                round(w / dpr), round(h / dpr))
        except Exception:
            pass
        return x, y, w, h

    def _apply_target(self, spec: dict) -> None:
        """主线程真正执行目标切换。"""
        if not _wt:
            return
        kind = spec.get("kind", "screen")
        self._target_hwnd = None
        self._target_pid = None
        self._obscured = False
        self._last_obscure_check = 0.0
        self._last_process_lookup = 0.0
        self._pending_spec = None      # 绑定意图 (目标窗口未就绪时用于重试)
        self.show()
        self.raise_()
        self._apply_passthrough()
        _keep_topmost(self)
        if kind == "screen":
            self._target = {"kind": "screen"}
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.setGeometry(scr)
            self._last_target_rect = None
        elif kind == "display":
            did = int(spec.get("id", 0))
            displays = _wt.list_displays()
            d = next((x for x in displays if x["id"] == did), None)
            if d is not None:
                self._target = {"kind": "display", "id": did}
                lx, ly, lw, lh = self._to_logical(d["x"], d["y"], d["w"], d["h"])
                self.setGeometry(lx, ly, lw, lh)
                self._last_target_rect = None
        elif kind == "process":
            # OBS 式: 绑定进程 pid, 每次跟随自动取该进程当前主窗口
            # (窗口重建/切全屏/开新窗口都自动重捕获, 不粘死旧 hwnd)
            pid = int(spec.get("pid", 0))
            mode = spec.get("mode", "client")
            getter = (_wt.get_window_client_rect if mode == "client"
                      else _wt.get_window_rect)
            hwnd = _wt.get_pid_main_window(pid) if pid else None
            rect = getter(hwnd) if hwnd else None
            if rect is not None:
                self._target = {"kind": "process", "pid": pid, "mode": mode}
                self._target_pid = pid
                self._target_hwnd = hwnd
                self._last_process_lookup = time.perf_counter()
                x, y, w, h = rect
                lx, ly, lw, lh = self._to_logical(x, y, w, h)
                self.setGeometry(lx, ly, lw, lh)
                self._last_target_rect = rect
                self._target_getter = getter
            else:
                # 窗口/进程还没就绪(游戏启动中、切全屏重建窗口) → 记住意图, 跟随循环重试
                self._pending_spec = dict(spec)
        elif kind == "window":
            hwnd = int(spec.get("hwnd", 0))
            mode = spec.get("mode", "client")   # client=客户区(不盖标题栏) | window=整窗
            getter = (_wt.get_window_client_rect if mode == "client"
                      else _wt.get_window_rect)
            rect = getter(hwnd) if hwnd else None
            if rect is not None:
                self._target = {"kind": "window", "hwnd": hwnd, "mode": mode}
                self._target_hwnd = hwnd
                x, y, w, h = rect
                lx, ly, lw, lh = self._to_logical(x, y, w, h)
                self.setGeometry(lx, ly, lw, lh)
                self._last_target_rect = rect
                self._target_getter = getter
            else:
                # 固定窗口还没就绪/已关闭 → 记住意图, 跟随循环重试(窗口重建后自动接上)
                self._pending_spec = dict(spec)
        self._reflow_all()
        self.stateChanged.emit(f"target={self._target}")

    def _update_obscured(self, hwnd: int | None, *, force: bool = False) -> bool:
        """检测目标是否被遮挡/最小化; 变化时 hide/show 并返回当前是否遮挡。

        遮挡 (被大窗口盖住/最小化/消失) → 隐藏浮层并清空已有弹幕;
        恢复可见 → 重新显示, 之后的弹幕照常播。
        """
        now = time.perf_counter()
        if (not force and hwnd is not None
                and now - self._last_obscure_check < OBSCURE_CHECK_TTL_S):
            return self._obscured
        self._last_obscure_check = now
        if not _wt or hwnd is None:
            was = self._obscured
            self._obscured = True
            if not was:
                self._items.clear()
                self.hide()
            return True
        obscured = _wt.is_target_obscured(hwnd)
        if obscured and not self._obscured:
            self._items.clear()
            self.hide()
        elif not obscured and self._obscured:
            self.show()
            self._apply_passthrough()
            _keep_topmost(self)
        self._obscured = obscured
        return obscured

    def _follow_once(self) -> None:
        """目标跟随: 高频轮询, 变化即贴合。process 目标每轮按 pid 重取主窗口。"""
        if not _wt:
            return
        # 绑定时目标窗口还没就绪(游戏启动中 / 切全屏重建窗口) → 持续重试, 就绪后自动接上
        if self._target is None and getattr(self, "_pending_spec", None):
            self._apply_target(self._pending_spec)
            if self._target is None:
                return
        # kind=process: 动态解析 pid 当前主窗口 (窗口重建自动换新 hwnd)
        if self._target and self._target.get("kind") == "process":
            pid = int(self._target.get("pid", 0))
            now = time.perf_counter()
            hwnd = self._target_hwnd
            if (not hwnd
                    or now - self._last_process_lookup >= PROCESS_LOOKUP_TTL_S):
                hwnd = _wt.get_pid_main_window(pid) if pid else None
                self._last_process_lookup = now
            if hwnd is None or hwnd != self._target_hwnd:
                self._target_hwnd = hwnd
                self._last_obscure_check = 0.0
                if hwnd is None:
                    # 进程暂无窗口 (刚启动/最小化) → 遮挡暂停
                    self._update_obscured(None)
                    return
            if self._update_obscured(hwnd):
                return        # 被遮挡 → 不贴合 (等恢复)
            getter = (self._target_getter if getattr(self, "_target_getter", None)
                      else _wt.get_window_client_rect)
            rect = getter(hwnd) if hwnd else None
            if rect is None:
                return
            if rect != self._last_target_rect:
                self._last_target_rect = rect
                x, y, w, h = rect
                lx, ly, lw, lh = self._to_logical(x, y, w, h)
                self.setGeometry(lx, ly, lw, lh)
                self._reflow_all()
            return
        # 固定 hwnd 跟随 (window 目标)
        if not self._target_hwnd:
            return
        if self._target and self._target.get("kind") == "window":
            if self._update_obscured(self._target_hwnd):
                return        # 被遮挡 → 不贴合
        getter = getattr(self, "_target_getter", None) or _wt.get_window_client_rect
        rect = getter(self._target_hwnd)
        if rect is None:
            # 目标窗口消失/最小化 → 遮挡暂停
            self._update_obscured(None)
            return
        if rect != self._last_target_rect:
            self._last_target_rect = rect
            x, y, w, h = rect
            lx, ly, lw, lh = self._to_logical(x, y, w, h)
            self.setGeometry(lx, ly, lw, lh)
            self._reflow_all()

    def target_status(self) -> dict:
        st = {"target": self._target}
        if _wt:
            st["displays"] = _wt.list_displays()
            if self._target_hwnd:
                st["follow_rect"] = self._last_target_rect
        return st

    def list_windows(self) -> list[dict]:
        return _wt.list_windows(main_only=True) if _wt else []

    def list_processes(self) -> list[dict]:
        return _wt.list_processes() if _wt else []

    # ── 弹幕入口 ──────────────────────────────────────────
    def push(self, text: str, style: dict | None = None) -> bool:
        """推一条弹幕。未选目标/目标被遮挡(窗口盖住/最小化)时丢弃。"""
        if not text or not text.strip():
            return False
        if self._target is None or self._obscured:
            # 没选目标或弹幕区被遮挡 → 不显示 (计数 + 节流打印, 便于定位"全屏不显示")
            self._dropped = int(getattr(self, "_dropped", 0)) + 1
            if self._dropped in (1, 10, 50) or self._dropped % 200 == 0:
                print(f"[overlay] push dropped: target={self._target} "
                      f"obscured={self._obscured} "
                      f"pending={bool(getattr(self, '_pending_spec', None))} "
                      f"count={self._dropped}", flush=True)
            return False
        style = dict(style or {})
        isolated = bool(style.pop("_isolated", False))
        st = dict(style) if isolated else merge_style(self.style, style)
        st = _pick_mix_color(st)   # 若带 color_pool → 本条随机定色
        # 弹幕感: 每条速度/字号/垂直位置轻微抖动, 避免"整整齐齐的字幕感"
        jitter = bool(st.get("jitter", True))
        d = Danmaku(text=text.strip(), style=st)
        if jitter:
            import random
            d.speed_factor = random.uniform(0.86, 1.14)
            d.y_jitter = random.uniform(-4.0, 4.0)
            if st.get("font_size"):
                st["font_size"] = max(12, int(st["font_size"]) + random.choice((-1, 0, 0, 1)))
        d.sprite = self._render_sprite(d.text, st)   # 主线程预渲染精灵
        d.w, d.h = float(d.sprite.width()), float(d.sprite.height())
        self._items.append(d)
        self.update()
        return True

    def clear(self) -> None:
        region = self._visible_region()
        self._items.clear()
        if not region.isEmpty():
            self.update(region)

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.stateChanged.emit("paused" if self._paused else "resumed")

    # ── 渲染引擎 (离屏精灵, 样式全能力) ────────────────────
    def _render_sprite(self, text: str, st: dict) -> "QPixmap":
        """把一条弹幕预渲染成显示优化的 QPixmap。"""
        from PySide6.QtCore import QPointF

        font = self._resolve_font_text(st)
        fm = QFontMetricsF(font)
        lines = self._wrap_lines(text, st, fm)
        spacing = max(1.0, float(st.get("line_spacing", 1.12) or 1.12))
        line_h = fm.height() * spacing
        text_w = max(fm.horizontalAdvance(ln) for ln in lines)
        text_h = fm.height() + line_h * (len(lines) - 1)

        stroke_w = float(st.get("stroke_width", 0) or 0)
        glow_blur = float(st.get("glow_blur", 0) or 0)
        shadow_blur = float(st.get("shadow_blur", 0) or 0)
        # 外边距: 描边/辉光/阴影模糊要留空间
        margin = max(stroke_w + 2, glow_blur, shadow_blur) + 6
        shadow_dx = float(st.get("shadow_dx", 0) or 0)
        shadow_dy = float(st.get("shadow_dy", 0) or 3)

        W = int(text_w + margin * 2 + abs(shadow_dx)) + 2
        H = int(text_h + margin * 2 + abs(shadow_dy)) + 2
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)

        # 文字绘制起点
        origin_x = margin + max(shadow_dx, 0)
        origin_y = margin + max(shadow_dy, 0)

        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)

        def _text_paths():
            for i, line in enumerate(lines):
                path = QPainterPath()
                path.addText(QPointF(origin_x, origin_y + fm.ascent()
                                     + i * line_h), font, line)
                yield path

        # 1) 底条 (画在文字外围, 最底层)
        bg = str(st.get("bg") or "none")
        if bg != "none":
            pad_x = 16.0
            box = QRectF(origin_x - pad_x, origin_y - 3,
                         text_w + pad_x * 2, text_h + 6)
            if bg == "light":
                bq = QColor(st.get("bg_color") or "#0a0a14")
                bq.setAlpha(int(st.get("bg_alpha", 46)))
            elif bg == "soft":
                bq = QColor(st.get("bg_color") or "#080810")
                bq.setAlpha(int(st.get("bg_alpha", 96)))
            else:  # custom
                bq = QColor(st.get("bg_color") or "#000000")
                bq.setAlpha(int(st.get("bg_alpha", 80)))
            radius = float(st.get("bg_radius", 9))
            bp = QPainterPath()
            bp.addRoundedRect(box, radius, radius)
            p.fillPath(bp, bq)

        # 2) 辉光 (多层描边模拟柔光, 无模糊 → 文字保持锐利)
        if glow_blur > 0 and st.get("glow"):
            glow_col = QColor(st.get("glow_color") or "#ffd78c")
            for path in _text_paths():
                self._layered_halo(p, path, glow_blur, glow_col,
                                   base_w=stroke_w + 1, origin=(0, 0))

        # 3) 阴影 (多层描边偏移, 深色, 无模糊)
        if shadow_blur > 0 and st.get("shadow"):
            sh_col = QColor(st.get("shadow_color") or "#000000")
            sh_col.setAlpha(int(st.get("shadow_alpha", 180)))
            for path in _text_paths():
                self._layered_halo(p, path, shadow_blur, sh_col,
                                   base_w=0, origin=(shadow_dx, shadow_dy))

        # 4) 描边 + 正文
        stroker = QPainterPathStroker()
        stroker.setWidth(stroke_w * 2)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        text_col = QColor(str(st.get("text_color", "#ffffff")))
        grad = st.get("fill_gradient")
        if grad and isinstance(grad, (list, tuple)) and len(grad) == 2:
            lg = QLinearGradient(0, origin_y, 0, origin_y + text_h)
            lg.setColorAt(0, QColor(grad[0]))
            lg.setColorAt(1, QColor(grad[1]))
            fill_brush = lg
        else:
            fill_brush = text_col
        stroke_col = QColor(str(st.get("stroke_color", "#101018")))
        for path in _text_paths():
            if stroke_w > 0:
                outline = stroker.createStroke(path)
                p.fillPath(outline, stroke_col)
            p.fillPath(path, fill_brush)
        p.end()
        return QPixmap.fromImage(img)

    @staticmethod
    def _layered_halo(p: QPainter, path: "QPainterPath", radius: float,
                      color: QColor, *, base_w: float, origin: tuple) -> None:
        """用多层描边模拟柔和光晕/阴影 (无模糊 → 文字边缘保持锐利)。

        radius 决定光晕总半径; 从外到内画 ~6 层 stroke, 层宽从 radius 递减,
        alpha 递增 → 近似高斯衰减。origin 用于阴影偏移。
        """
        if radius <= 0:
            return
        rad = float(radius)
        dx, dy = origin
        n_layers = 6
        base_alpha = color.alpha()
        for i in range(n_layers, 0, -1):
            t = i / n_layers                       # 外层 0.16 → 内层 1.0
            w = max(1.0, (rad * t + base_w) * 2)   # stroke 直径
            alpha = int(base_alpha * (0.25 + 0.75 * t * t))
            c = QColor(color)
            c.setAlpha(alpha)
            st = QPainterPathStroker()
            st.setWidth(w)
            st.setCapStyle(Qt.RoundCap)
            st.setJoinStyle(Qt.RoundJoin)
            halo = st.createStroke(path)
            p.save()
            p.translate(dx, dy)
            p.fillPath(halo, c)
            p.restore()

    def _resolve_font_text(self, st: dict) -> QFont:
        f = QFont(str(st.get("font_family") or DEFAULT_FONT_FAMILY))
        f.setPixelSize(self._eff_font_size(st))
        f.setBold(bool(st.get("bold", True)))
        try:
            ls = float(st.get("letter_spacing", 0) or 0)
        except (TypeError, ValueError):
            ls = 0.0
        if ls:
            f.setLetterSpacing(QFont.AbsoluteSpacing, ls)
        return f

    # ── 自适应 / 折行 / 轨道几何 ─────────────────────────
    def _eff_font_size(self, st: dict) -> int:
        """字号自适应: 按浮层宽度相对 1920 缩放 (auto_scale=false 可关)。"""
        base = int(st.get("font_size", 34) or 34)
        if not st.get("auto_scale", True):
            return max(12, base)
        scale = max(0.72, min(1.55, float(self.width() or BASE_SCREEN_W) / BASE_SCREEN_W))
        return max(12, int(round(base * scale)))

    def _wrap_limit(self) -> float:
        """自动折行的像素宽度上限; 0 表示不折行。"""
        if not self.style.get("wrap", True):
            return 0.0
        return max(240.0, float(self.width() or BASE_SCREEN_W) * WRAP_RATIO)

    def _wrap_lines(self, text: str, st: dict, fm: "QFontMetricsF") -> list[str]:
        """按像素宽度自动折行(中英文混排逐字符累加), 超行数尾部加省略号。"""
        limit = self._wrap_limit()
        max_lines = max(1, int(st.get("max_lines", MAX_LINES) or MAX_LINES))
        raw_lines = str(text).split("\n")
        if not limit:
            return raw_lines or [""]
        out: list[str] = []
        for para in raw_lines:
            if fm.horizontalAdvance(para) <= limit:
                out.append(para)
                continue
            buf = ""
            for ch in para:
                if buf and fm.horizontalAdvance(buf + ch) > limit:
                    out.append(buf)
                    buf = ch
                else:
                    buf += ch
            if buf:
                out.append(buf)
        if len(out) > max_lines:
            out = out[:max_lines]
            tail = out[-1]
            while tail and fm.horizontalAdvance(tail + "…") > limit:
                tail = tail[:-1]
            out[-1] = tail + "…"
        return out or [""]

    def _lane_base_h(self) -> float:
        """单行轨道基础高度(px): 随字号自适应。"""
        return max(float(TRACK_HEIGHT), self._eff_font_size(self.style) * 1.7)

    def _lane_heights(self) -> list[float]:
        """各轨道当前占用高度 = 该轨最高条目; 空轨回落到基础高度。"""
        base = self._lane_base_h()
        heights = [base] * self._max_tracks()
        for d in self._items:
            if d.entered and d.lane is not None and 0 <= d.lane < len(heights):
                heights[d.lane] = max(heights[d.lane], float(d.h or 0.0))
        return heights

    def _lane_y(self, lane: int) -> float:
        """按「上方各轨实际高度 + 间距」累计出该轨的 y (紧凑排布, 不浪费空间)。"""
        y = self._area_y0()
        heights = self._lane_heights()
        for i in range(min(int(lane), len(heights))):
            y += heights[i] + TRACK_GAP
        return y

    def _reflow_y(self) -> None:
        """按当前轨道高度重排已入场条目: 多行条目出现/消失时也不会压线。"""
        heights = self._lane_heights()
        lane_y = [self._area_y0()]
        for height in heights:
            lane_y.append(lane_y[-1] + height + TRACK_GAP)
        for d in self._items:
            if d.entered and d.lane is not None:
                index = min(max(int(d.lane), 0), len(lane_y) - 1)
                d.y = lane_y[index] + float(getattr(d, "y_jitter", 0.0) or 0.0)

    def _track_pitch(self) -> float:
        """典型轨道间距(状态展示用): 基础高度 + 间距。"""
        return self._lane_base_h() + TRACK_GAP

    def _max_tracks(self) -> int:
        """可用轨道数: 不超过 MAX_TRACKS, 也不超出窗口可见高度。"""
        pitch = self._lane_base_h() + TRACK_GAP
        by_height = int(max(1.0, (float(self.height() or 1080) * 0.9) // pitch))
        return max(1, min(MAX_TRACKS, by_height))

    def _max_items(self) -> int:
        return max(1, int(self.style.get("max_items", MAX_ITEMS) or MAX_ITEMS))

    def _item_alpha(self, d: "Danmaku", now: float) -> float:
        """入场淡入 × 左缘淡出 (让弹幕出现/消失更柔和)。"""
        fade = max(0.0, float(self.style.get("fade_in_ms", FADE_IN_MS) or FADE_IN_MS)) / 1000.0
        alpha = 1.0
        if fade > 0 and getattr(d, "born", 0):
            alpha = min(1.0, (now - d.born) / fade)
        if FADE_OUT_PX > 0:
            right = d.x + d.w
            if right < FADE_OUT_PX:
                alpha *= max(0.0, right / FADE_OUT_PX)
        return max(0.0, min(1.0, alpha))

    def _measure(self, d: Danmaku) -> tuple[float, float]:
        if d.sprite is not None:
            return float(d.sprite.width()), float(d.sprite.height())
        fm = QFontMetricsF(self._resolve_font_text(d.style))
        lines = d.text.split("\n")
        w = max(fm.horizontalAdvance(ln) for ln in lines)
        h = fm.height() * len(lines)
        return w, h

    def _on_tick(self) -> None:
        tick_started = time.perf_counter()
        now = time.time()
        if not self._items:
            self._prev_tick = now
            self._record_tick_metrics(tick_started, painted=False)
            return
        scr_w = self.width()
        entry_x = float(scr_w + SCREEN_MARGIN_RIGHT)
        region = QRegion()
        if not self._paused:
            for d in self._items:
                region = self._with_item_region(region, d)

        # 入场节流: 距上次入场未满 SPAWN_INTERVAL 则本帧一条都不放
        last = getattr(self, "_last_spawn_ts", 0.0)
        if now - last >= SPAWN_INTERVAL:
            onscreen = sum(1 for x in self._items if x.entered)
            for d in self._items:
                if d.entered:
                    continue
                if onscreen >= self._max_items():
                    break      # 同屏上限: 剩余排队等待, 不硬塞糊屏
                d.w, d.h = self._measure(d)
                lane = self._find_lane(entry_x, d.w, self._speed)
                if lane is not None:
                    d.speed = self._speed * float(getattr(d, "speed_factor", 1.0) or 1.0)
                    d.x = entry_x
                    d.born = now
                    d.lane = lane
                    d.entered = True
                    self._reflow_y()    # 按各轨实际高度落位(多行条目也不会压线)
                    region = self._with_item_region(region, d)
                    onscreen += 1
                    self._last_spawn_ts = now
                    break   # 每帧至多放一条 → 时间上均匀逐条入场

        # 交互超时自动回穿透 (穿透是常态; 交互只是临时)
        if not self._passthrough and self._interact_timeout_s > 0:
            if now - self._last_activity > self._interact_timeout_s:
                self._passthrough = True
                self._apply_passthrough()
                self.stateChanged.emit("auto-passthrough")

        # 推进 / 清理 (按真实时间差)
        if not self._paused:
            dt = self._last_tick_dt(now)
            alive = []
            for d in self._items:
                if d.entered:
                    d.x -= d.speed * dt
                    region = self._with_item_region(region, d)
                if not d.entered or d.x + d.w + 40 > 0:
                    alive.append(d)   # 未入场继续等; 入场未出屏保留
            self._items = alive
        painted = not region.isEmpty()
        if painted:
            self.update(region)
        self._record_tick_metrics(tick_started, painted=painted)

    def _find_lane(self, entry_x: float, w_new: float, v_new: float) -> int | None:
        """选轨道: 解析预测新弹幕(宽 w_new, 速 v_new, 从 entry_x 入场)是否会在
        屏内追尾该轨道任意已有弹幕。所有弹幕速度各自随机 → 必须逐条算碰撞。

        一维匀速模型: 弹幕 i 此刻头部 xi、速度 vi (向左)。新弹幕头部从 entry_x
        以 v_new 向左。若 v_new > vi, 相对追近速度 = v_new - vi, 追尾发生在
        t_catch = (xi + wi + GAP - entry_x) / (v_new - vi)   [两弹幕间距 GAP]
        只要 t_catch > 0 且新弹幕追到前, i 已出屏(其尾部离左缘还需
        t_out = (xi + wi) / vi), 则安全。若 v_new <= vi 永不追尾。

        选"最不挤"的安全轨道; 全不安全 → None (排队)。
        """

        def lane_safe(lane: int) -> bool:
            for d in self._items:
                if not d.entered or d.x + d.w <= 0:
                    continue
                if d.lane != lane:
                    continue
                xi, wi, vi = d.x, d.w, max(d.speed, 1e-6)
                # 两弹幕右边缘间距 (新在 entry, i 在左侧)
                gap_now = entry_x - (xi + wi)
                if gap_now < COLLIDE_GAP:
                    return False                 # 当前就太近
                if v_new <= vi:
                    continue                      # 新不比 i 快, 永不追上
                # i 出屏(尾部离左缘)还需时间
                t_out_i = (xi + wi) / vi
                # 新弹幕追上 i 尾部所需时间 (追近速度差)
                t_catch = gap_now / (v_new - vi)
                if t_catch < t_out_i:
                    return False                 # i 还没出屏就被追上 → 撞
            return True

        # 收集轨道: 先完全空轨, 再评估占用轨
        used = {d.lane for d in self._items if d.entered and d.lane is not None}
        tracks = self._max_tracks()
        for lane in range(tracks):
            if lane not in used:
                return lane
        # 占用轨: 各轨道都要安全; 选"最右弹幕最靠左"(最早清空)的轨道
        best_lane, best_right = None, 1e18
        for lane in range(tracks):
            if lane not in used:
                continue
            if not lane_safe(lane):
                continue
            rightmost = max((d.x for d in self._items
                             if d.entered and d.lane == lane), default=0.0)
            if rightmost < best_right:
                best_lane, best_right = lane, rightmost
        return best_lane

    def _last_tick_dt(self, now: float) -> float:
        dt = now - getattr(self, "_prev_tick", now)
        self._prev_tick = now
        return min(max(dt, 0.0), 0.05)

    @staticmethod
    def _with_item_region(region: QRegion, item: "Danmaku") -> QRegion:
        if not item.entered or item.w <= 0 or item.h <= 0:
            return region
        rect = QRectF(item.x, item.y, item.w, item.h).toAlignedRect()
        return region.united(rect.adjusted(-3, -3, 3, 3))

    def _visible_region(self) -> QRegion:
        region = QRegion()
        for item in self._items:
            region = self._with_item_region(region, item)
        return region

    def _record_tick_metrics(self, started: float, *, painted: bool) -> None:
        cost_ms = (time.perf_counter() - started) * 1000.0
        self._tick_ms = (self._tick_ms * 0.9 + cost_ms * 0.1) if self._tick_ms else cost_ms
        if painted:
            self._fps_frames += 1
        elapsed = time.perf_counter() - self._fps_started
        if elapsed >= 1.0:
            self._fps = self._fps_frames / elapsed
            self._fps_frames = 0
            self._fps_started = time.perf_counter()

    # ── 绘制 ──────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:  # noqa: N802
        from PySide6.QtCore import QPointF

        p = QPainter(self)
        now = time.time()
        for d in self._items:
            if not d.entered or d.sprite is None:
                continue   # 排队中(未分配轨道)不画
            alpha = self._item_alpha(d, now)
            if alpha <= 0.01:
                continue
            p.setOpacity(alpha)
            # 浮点位置 + 平滑变换: 低速时不再抖(原来 int() 截断会跳像素)
            p.drawImage(QPointF(round(d.x, 2), round(d.y, 2)), d.sprite)
        p.setOpacity(1.0)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        # 跟随窗口/显示器尺寸变化: 字号自适应与折行都依赖宽度 → 节流重渲染
        timer = getattr(self, "_reflow_timer", None)
        if timer is None:
            from PySide6.QtCore import QTimer
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._reflow_all)
            self._reflow_timer = timer
        timer.start(180)

    # ── 持久化 ────────────────────────────────────────────
    def _load_state(self) -> None:
        try:
            candidates = [self._state_file]
            legacy = Path(WINDOW_STATE_FILE)
            if legacy not in candidates:
                candidates.append(legacy)
            source = next(path for path in candidates if path.is_file())
            with open(source, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("style"):
                self.style = merge_style(self.style, st["style"])
            self._speed = float(st.get("speed", self._speed))
            self._opacity = float(st.get("opacity", self._opacity))
            if st.get("area") in ("top", "center", "bottom"):
                self._area = st["area"]
            if st.get("area_offset") is not None:
                self._area_offset = int(st["area_offset"])
            if st.get("interact_timeout") is not None:
                self._interact_timeout_s = float(st["interact_timeout"])
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({"style": self.style, "speed": self._speed,
                           "opacity": self._opacity,
                           "area": self._area,
                           "area_offset": self._area_offset,
                           "interact_timeout": self._interact_timeout_s},
                          f, ensure_ascii=False, indent=2)
            self.stateChanged.emit("state-saved")
        except Exception:
            pass

    def _reflow_all(self) -> None:
        """字号/样式/弹幕区变化后: 重渲染已入场弹幕并按新区重定位。"""
        for d in self._items:
            if d.entered:
                d.sprite = self._render_sprite(d.text, d.style)
                d.w, d.h = float(d.sprite.width()), float(d.sprite.height())
        self._reflow_y()
        self.update()


# ── 热键 ─────────────────────────────────────────────────
WM_QUIT = 0x0012


def install_global_hotkeys(win: DanmakuOverlay) -> None:
    import ctypes
    from ctypes import wintypes

    MOD_ALT = 0x1
    MOD_CONTROL = 0x2
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 记录本线程 OS tid, 供 run_overlay 结束时 PostThreadMessage(WM_QUIT) 唤醒
    win._hotkey_tid = int(kernel32.GetCurrentThreadId())
    # Ctrl+Alt+D → id=1 切 穿透⇄交互;  Ctrl+Alt+F5 → id=2 演示;  Ctrl+Alt+X → id=3 退出
    user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, ord("D"))
    user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT, 0x74)  # F5
    user32.RegisterHotKey(None, 3, MOD_CONTROL | MOD_ALT, ord("X"))

    class Msg(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

    msg = Msg()
    WM_HOTKEY = 0x0312
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r <= 0:
            break   # WM_QUIT (0) 或错误 (-1)
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:
                # 进入交互(临时); 超时自动回穿透由主线程处理
                win.interactRequested.emit(True)
            elif msg.wParam == 2:
                win._demo_seq += 1
                win.pushRequested.emit(f"[演示] {win._demo_seq} 号弹幕", dict(win.style))
            elif msg.wParam == 3:
                win.exitRequested.emit()
    user32.UnregisterHotKey(None, 1)
    user32.UnregisterHotKey(None, 2)
    user32.UnregisterHotKey(None, 3)
    win._hotkey_stop.set()


def _qt_teardown(app, win: DanmakuOverlay) -> None:
    """Qt 创建线程内的全序清理 (进程退出/线程结束前调用, 防跨线程析构崩溃)。

    顺序: 停 HTTP → 唤醒热键线程 → join 两个 daemon 线程 → 删窗口 → 删 QApplication。
    必须在创建 QApplication 的同一个线程里执行 (run_overlay 的 finally)。
    """
    import ctypes

    # 1) 停 HTTP server (daemon 线程 serve_forever)
    try:
        win._http.shutdown()
        win._http.server_close()
    except Exception:
        pass
    # 2) 唤醒热键线程: 置位 + PostThreadMessage(WM_QUIT) 令 GetMessageW 返回
    try:
        win._hotkey_stop.set()
        tid = getattr(win, "_hotkey_tid", None)
        if tid:
            ctypes.windll.user32.PostThreadMessageW(int(tid), WM_QUIT, 0, 0)
    except Exception:
        pass
    # 3) 关窗口 + 事件循环 flush (删除挂起的 deleteLater)
    try:
        win._save_state()
    except Exception:
        pass
    try:
        win.close()
    except Exception:
        pass
    try:
        app.processEvents()
    except Exception:
        pass
    # 4) 在本线程显式析构 Qt C++ 对象, 避免解释器退出时跨线程销毁导致
    #    0xC0000005 (python.exe 应用程序错误弹窗)
    try:
        import shiboken6  # noqa: PLC0415
        shiboken6.delete(win)
    except Exception:
        pass
    try:
        app.processEvents()
    except Exception:
        pass
    try:
        import shiboken6  # noqa: PLC0415
        if QApplication.instance() is app:
            shiboken6.delete(app)
    except Exception:
        pass


def run_overlay(
    port: int = 48777,
    *,
    state_path: str | Path | None = None,
    on_ready=None,
) -> int:
    """在【当前线程】创建 Qt 浮层并进入事件循环 (供线程内运行)。

    独立脚本 (main) 与插件进程内嵌线程 (runtime.OverlayHost) 共用。
    事件循环退出后立即在本线程做完整 Qt 清理 (_qt_teardown)。
    """
    app = QApplication.instance() or QApplication([])
    win = DanmakuOverlay(port=port, state_path=state_path)
    win.show()
    win._apply_passthrough()
    # 延迟到 winId 就绪后原生置顶 (Qt WindowStaysOnTopHint + Win32 TOPMOST 双保险)
    QTimer.singleShot(120, lambda: (win._apply_passthrough(), _keep_topmost(win)))
    win.stateChanged.connect(lambda s: print(f"[overlay] {s}", flush=True))
    print(f"[overlay] v2 样式系统版启动 port={port}", flush=True)
    print("[overlay] 穿透默认; Ctrl+Alt+D 交互; Ctrl+Alt+F5 演示", flush=True)
    if on_ready:
        QTimer.singleShot(0, on_ready)   # 窗口显示后回调
    threading.Thread(target=install_global_hotkeys, args=(win,),
                     daemon=True).start()
    try:
        return app.exec()
    finally:
        print("[overlay] 事件循环退出", flush=True)
        _qt_teardown(app, win)
        print("[overlay] 已清理", flush=True)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=48777)
    ap.add_argument("--state-path", default="")
    args, _ = ap.parse_known_args()
    return run_overlay(
        port=args.port,
        state_path=args.state_path or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
