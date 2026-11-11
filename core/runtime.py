"""core/runtime: 弹幕编排 —— 浮层管理(进程内线程) + 桥调度。

生命周期 (由 __init__.py 的 lifecycle 回调驱动):
  start    → OverlayHost 起 Qt 浮层(进程内 daemon 线程) → 桥轮询 → POST 浮层
  shutdown → 停桥 → 停浮层    apply_config → 热更新

为什么进程内线程而非独立子进程?
  宿主是 PyInstaller frozen 打包版, 插件进程内 sys.executable 指向 server exe
  (无独立 python.exe), 无法 spawn overlay.py。改为在插件进程内开 daemon 线程
  跑 QApplication —— frozen 解释器完整(py311 + 全套 .pyd), 可加载 vendor PySide6。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import DanmakuConfig, resolve_recent_path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
VENDOR_DIR = _PLUGIN_DIR / "vendor"


def _port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


class OverlayHost:
    """Qt 浮层宿主: 在插件进程内开 daemon 线程跑 QApplication + DanmakuOverlay。

    打包宿主(frozen)无独立 python 可 spawn → in-process 线程是唯一可行路径。线程内:
      1) sys.path 注入 vendor(供 PySide6) + overlay 目录(供 import app/win_target)
      2) import overlay.app.run_overlay → QApplication + DanmakuOverlay
      3) app.exec() 事件循环常驻; 由 _stop() 经 HTTP /quit 或窗口关闭退出
    """

    def __init__(self, plugin: Any, port: int) -> None:
        self.plugin = plugin
        self.log = plugin.logger
        self.port = port
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._started = False

    # -- 路径注入 --
    def _env_paths(self) -> list[str]:
        """线程内需要的 sys.path 根目录 (不重复)。

        - vendor/        : 供 `import PySide6` (宿主 frozen 无 PySide6)
        - 插件根/overlay : 供 overlay/app.py 内顶层 `import win_target`
        """
        roots = []
        if (VENDOR_DIR / "PySide6").is_dir():
            roots.append(str(VENDOR_DIR))
        overlay_dir = _PLUGIN_DIR / "overlay"
        if overlay_dir.is_dir():
            roots.append(str(overlay_dir))
        return roots

    def _load_overlay_app(self):
        """按文件路径加载 overlay/app.py (独立于宿主如何命名插件包)。

        app.py 内部只做顶层 import (PySide6 / win_target), 无相对导入, 所以
        以唯一模块名加载即可; vendor 与 overlay 目录已注入 sys.path。
        """
        import importlib.util

        app_file = _PLUGIN_DIR / "overlay" / "app.py"
        spec = importlib.util.spec_from_file_location(
            "neko_danmaku_overlay_app", app_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载浮层模块: {app_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def start(self) -> bool:
        """启动浮层线程 (幂等)。返回是否本次真正启动。"""
        if self._started and self._thread and self._thread.is_alive():
            return False
        if not _port_free(self.port):
            self.log.warning("端口 %s 已被占用, 假定浮层已在运行", self.port)
            return False

        def _run() -> None:
            try:
                for p in self._env_paths():
                    if p not in sys.path:
                        sys.path.insert(0, p)
                self._load_overlay_app().run_overlay(port=self.port)
            except Exception as exc:  # noqa: BLE001
                import traceback
                self.log.error("浮层线程异常: %s\n%s", exc,
                               traceback.format_exc())

        self._thread = threading.Thread(target=_run, name="danmaku-overlay",
                                        daemon=True)
        self._thread.start()
        self._started = True
        # 等浮层 HTTP 就绪 (最多 ~4s; 线程起来 + Qt 初始化 + server bind)
        self._wait_ready()
        self.log.info("浮层线程已启动(port=%s)", self.port)
        return True

    def _wait_ready(self) -> bool:
        """轮询本地端口直到浮层 HTTP 可连。"""
        import urllib.request
        for _ in range(40):
            if not (self._thread and self._thread.is_alive()):
                return False
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/", timeout=0.4) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.1)
        return False

    def stop(self) -> None:
        """停浮层: 通过 HTTP /quit 优雅退出, 线程 join 短超时。"""
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/quit", method="POST")
            urllib.request.urlopen(req, timeout=1)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._started = False

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


class DanmakuRuntime:
    """编排: OverlayHost + 桥(轮询 recent.json → POST 浮层)。"""

    def __init__(self, plugin: Any, cfg: DanmakuConfig) -> None:
        self.plugin = plugin
        self.log = plugin.logger
        self.cfg = cfg
        self.host = OverlayHost(plugin, cfg.overlay_port)
        self._bridge_task: asyncio.Task | None = None
        self._bridge_source = None      # 数据源实例 (shutdown 时释放)
        self._bridge_thread: threading.Thread | None = None
        self._bridge_stop = threading.Event()
        # 实际生效的数据源 (配置要 sdk 但宿主没有该通道时会回退, 见 _make_source)
        self._effective_source = cfg.data_source

    # ---- 游标持久化 (按数据源隔离, 防止 recent_file 旧游标污染 time_index) ----
    def _seen_file(self) -> Path:
        return _PLUGIN_DIR / "state" / "bridge_cursor.json"

    def _load_seen(self, ds: str | None = None) -> int:
        """读取游标。旧格式 {seen} 视为无效(不跨源复用); 新格式带 source。"""
        ds = ds or self.cfg.data_source
        try:
            with open(self._seen_file(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        if isinstance(data, dict) and data.get("source") != ds:
            return 0          # 数据源切换/旧格式 → 重新基线
        try:
            return int(data.get("seen", 0) if isinstance(data, dict) else 0)
        except Exception:
            return 0

    def _save_seen(self, n: int, ds: str | None = None) -> None:
        ds = ds or self.cfg.data_source
        try:
            p = self._seen_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"seen": n, "source": ds, "ts": time.time()}, f)
        except Exception:
            pass

    # ---- 桥(数据源轮询 → 浮层) ----
    def _make_source(self):
        """按配置造数据源 (结果同时存 _bridge_source 供 shutdown 释放)。"""
        ds = self.cfg.data_source
        if ds == "sdk":
            from ..sources.sdk_bus import SdkBusSource
            src = SdkBusSource(getattr(self.plugin, "ctx", None))
            if src.supported():
                self._bridge_source = src
                self._effective_source = "sdk"
                return src
            # 宿主太旧 / 用户插件门禁关闭 → 退回文件源, 别让弹幕直接哑掉
            self.log.warning("SDK conversations 通道不可用 → 回退 time_index")
            ds = "time_index"
        if ds == "time_index":
            from ..sources.time_index import TimeIndexSource
            from .config import resolve_timeindex_db
            db = resolve_timeindex_db(self.cfg.db_path)
            if not db:
                self.log.warning("未找到 time_indexed.db, 桥停用")
                self._bridge_source = None
                return None
            self._bridge_source = TimeIndexSource(db)
            self._effective_source = "time_index"
            return self._bridge_source
        # recent_file (旧数据源, 兼容)
        from ..sources.recent_file import RecentFileSource
        recent = resolve_recent_path(self.cfg.recent_path)
        if not recent:
            self.log.warning("未找到 recent.json, 桥停用 (配置 recent_path 可指定)")
            self._bridge_source = None
            return None
        self._bridge_source = RecentFileSource(recent)
        self._effective_source = "recent_file"
        return self._bridge_source

    async def _push_one(self, url: str, role: str, text: str) -> None:
        """清洗后 POST 一条到浮层。text 已清洗, 空跳过。"""
        import urllib.request

        from .cleaner import strip_action_notes
        from .recent_reader import strip_ts_prefix

        text = strip_ts_prefix(text)
        text = strip_action_notes(text)
        text = " ".join(text.split())
        if not text:
            return
        preset = self.cfg.role_preset.get(role, "clean")
        payload = json.dumps({"text": text, "preset": preset},
                             ensure_ascii=False).encode("utf-8")
        try:
            def _post() -> tuple[int, str]:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            status, body = await asyncio.to_thread(_post)
            if status == 200:
                try:
                    obj = json.loads(body) if body else {}
                except Exception:
                    obj = {}
                if obj.get("accepted") is False:
                    reason = str(obj.get("reason") or "unknown")
                    # 节流: 同一原因每 30s 只告警一次, 避免刷日志
                    now = time.time()
                    last = getattr(self, "_drop_warn", 0.0)
                    if now - last > 30:
                        self._drop_warn = now
                        self.log.warning(
                            "浮层未接收弹幕 (reason=%s): 目标未绑定或被遮挡, 检查面板目标/全屏设置",
                            reason)
                    return            # 未选目标, 浮层丢弃(游标照常推进)
                self.log.info("弹幕 → %s %s", preset, text[:30])
        except Exception as exc:  # noqa: BLE001
            self.log.warning("push_one 失败: %r", exc)

    @property
    def effective_source(self) -> str:
        """实际生效的数据源（配置为 sdk 但宿主无通道时会回退）。"""
        return self._effective_source

    def _sdk_interval(self) -> float:
        """SDK conversations 是拉取式通道(无 watch 推送): 用更短间隔逼近实时。

        sdk_poll_interval_s 默认 0.3s；同时不超过 poll_interval_s 上限（用户把
        poll 调小则跟随），下限 0.1s 防止打爆消息平面。
        """
        base = float(getattr(self.cfg, "sdk_poll_interval_s", 0.3) or 0.3)
        cap = float(self.cfg.poll_interval_s or 0.8)
        return max(0.1, min(base, cap))

    async def _bridge_loop(self, source=None) -> None:
        url = f"http://127.0.0.1:{self.cfg.overlay_port}/push"
        source = source if source is not None else self._make_source()
        if source is None:
            return
        ds = self._effective_source
        if ds == "time_index":
            # time_indexed.db: 追加式 id 游标。启动基线 = 当前 MAX(id) →
            # 无论上次游标在哪, 一律跳过存量/重启积压, 只播启动之后的新聊天。
            # (用户明确: 不要补历史, 只推启动后最新的)
            seen = source.current_max_id()
            self._save_seen(seen)
            self.log.info("桥启动: time_index → %s (基线 id %s, 跳过存量)",
                          url, seen)
            while True:
                try:
                    max_id = source.current_max_id()
                    if seen > max_id:          # 库被重建/回退 → 收敛
                        seen = max_id
                        self._save_seen(seen)
                        continue
                    items = source.poll_after(seen)      # [(role, text)]
                    if items:
                        self.log.info("桥探测: %d 条新消息 (id %s→%s)",
                                      len(items), seen, seen + len(items))
                    for role, text in items:
                        await self._push_one(url, role, text)
                        seen += 1
                        # 逐条入场间隔: 避免一回复就"爆发"整批弹幕
                        if len(items) > 1:
                            await asyncio.sleep(0.35)
                    if seen > self._load_seen():
                        self._save_seen(seen)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("桥 time_index 循环异常: %r", exc)
                await asyncio.sleep(self.cfg.poll_interval_s)
        elif ds == "recent_file":
            seen = self._load_seen()
            if seen <= 0:
                # 首次启动: 游标基线 = recent.json 当前条目数, 跳过存量历史,
                # 只播「启动之后」新增的宿主聊天 (不重放旧对话刷屏)。
                seen = source.current_count()
                self._save_seen(seen)
            self.log.info("桥启动: recent_file → %s (游标 %s)", url, seen)
            while True:
                try:
                    total = source.current_count()
                    # 宿主压缩/截断 recent.json 使游标越界 → 收敛到当前尾部,
                    # 绝不回退重放旧消息。
                    if seen > total:
                        seen = total
                        self._save_seen(seen)
                        continue
                    items = source.poll_once(seen)      # [(role, text)]
                    for role, text in items:
                        await self._push_one(url, role, text)
                        seen += 1
                    if seen > self._load_seen():
                        self._save_seen(seen)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(self.cfg.poll_interval_s)
        else:  # sdk
            # conversations 是拉取式通道(无 watch 推送): 短间隔 + 到达时间游标 = 准实时。
            # 游标按 source="sdk" 独立持久化, 与补源路径共用, 避免重复/回退重放。
            seen_ts = float(self._load_seen("sdk"))
            if seen_ts <= 0:
                # 首启: 基线 = 现在, 跳过存量历史(不把整段旧对话刷上屏)
                seen_ts = time.time()
                self._save_seen(seen_ts, "sdk")
            interval = self._sdk_interval()
            self.log.info(
                "桥启动: sdk conversations → %s (自 %s, 间隔 %.2fs)",
                url, time.strftime("%H:%M:%S", time.localtime(seen_ts)), interval)
            while True:
                try:
                    items = await source.poll_once(seen_ts)
                    if items:
                        self.log.info("桥探测: sdk %d 条新回合", len(items))
                    for role, text in items:
                        await self._push_one(url, role, text)
                        if len(items) > 1:
                            await asyncio.sleep(0.35)   # 逐条入场, 避免整批爆发
                    if source.last_ts > seen_ts:
                        seen_ts = source.last_ts
                        self._save_seen(seen_ts, "sdk")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("桥 sdk 循环异常: %r", exc)
                await asyncio.sleep(interval)

    # ---- UI 转发 (hosted-ui → 浮层 HTTP) ----
    async def overlay_request(self, method: str, path: str,
                              body: dict | None = None) -> dict:
        """转发请求到浮层本地 HTTP (供 @ui.action/@ui.context 使用)。"""
        import urllib.request
        url = f"http://127.0.0.1:{self.cfg.overlay_port}{path}"
        try:
            def _call() -> dict:
                if method == "GET":
                    req = urllib.request.Request(url)
                else:
                    data = (body and json.dumps(body, ensure_ascii=False)
                            or "{}").encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data,
                        headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3) as r:
                    raw = r.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw else {}
            return await asyncio.to_thread(_call)
        except Exception as exc:
            return {"ok": False, "error": f"浮层不可达: {exc}"}

    async def overlay_status(self) -> dict:
        """面板 dashboard 聚合状态。"""
        from .config import resolve_timeindex_db
        overlay_running = self.host.alive or not _port_free(self.cfg.overlay_port)
        st = {"running": overlay_running,
              "port": self.cfg.overlay_port,
              "data_source": self.cfg.data_source,
              "effective_source": self._effective_source,
              "sdk_interval_s": self._sdk_interval(),
              "role_preset": dict(self.cfg.role_preset or {}),
              "db": resolve_timeindex_db(self.cfg.db_path),
              "recent": resolve_recent_path(self.cfg.recent_path)}
        base = await self.overlay_request("GET", "/")
        st["overlay"] = base
        # 显示器/目标来自 /target (GET / 不含 displays)
        tgt = await self.overlay_request("GET", "/target")
        if isinstance(tgt, dict):
            ov = dict(base or {})
            if tgt.get("displays"):
                ov["displays"] = tgt["displays"]
            if "target" in tgt and tgt.get("target") is not None:
                ov["target"] = tgt["target"]
            st["overlay"] = ov
        w = await self.overlay_request("GET", "/windows")
        st["windows"] = w.get("windows", [])
        p = await self.overlay_request("GET", "/processes")
        st["processes"] = p.get("processes", [])
        return st

    # ---- 生命周期 ----
    async def start(self) -> dict:
        from .config import resolve_timeindex_db
        if self.cfg.auto_start_overlay:
            self.host.start()    # 内部已等待 HTTP 就绪
        if self.cfg.data_source in ("recent_file", "time_index", "sdk"):
            self._start_bridge_thread()
        else:
            self.log.warning("data_source=%s 未知", self.cfg.data_source)
        return {"status": "running",
                "overlay": self.host.alive,
                "port": self.cfg.overlay_port,
                "db": resolve_timeindex_db(self.cfg.db_path),
                "recent": resolve_recent_path(self.cfg.recent_path)}

    # ---- 桥后台线程 (宿主 lifecycle loop 是临时的; 照官方插件用独立 daemon 线程跑) ----
    def _start_bridge_thread(self) -> None:
        """桥必须跑在独立 daemon 线程的自己的事件循环里。

        宿主 lifecycle startup 用 asyncio.run()(临时 loop), create_task 的后台
        任务在 startup 返回后会被取消 —— 官方 neko_pawpilot/fishpower 均为此
        改造: threading.Thread + asyncio.run(self._bg_loop())。
        """
        if self._bridge_thread and self._bridge_thread.is_alive():
            return
        self._bridge_stop = threading.Event()
        self._bridge_thread = threading.Thread(
            target=self._bridge_runner, daemon=True, name="danmaku-bridge")
        self._bridge_thread.start()

    def _bridge_runner(self) -> None:
        try:
            asyncio.run(self._bridge_entry())
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.log.error("桥线程异常退出: %s\n%s", exc, traceback.format_exc())

    async def _bridge_entry(self) -> None:
        """桥线程主协程: 跑主源循环 + SDK conversations 补源, 直到 stop 事件。"""
        source = self._make_source()
        if source is None:
            self.log.warning("桥: 无可用数据源, 弹幕停用")
            return
        tasks = [asyncio.create_task(self._bridge_loop(source))]
        if self._effective_source in ("time_index", "recent_file"):
            # 补源: SDK conversations (覆盖 AI 自主回复——db 轮询拿不到)
            sdk = asyncio.create_task(self._bridge_loop_conversations())
            tasks.append(sdk)
        try:
            while not self._bridge_stop.is_set():
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            self._bridge_task = None
            if getattr(self, "_bridge_source", None) is not None:
                close = getattr(self._bridge_source, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                self._bridge_source = None

    async def _bridge_loop_conversations(self) -> None:
        """补源: 轮询 SDK ctx.bus.conversations 增量 (AI 自主回复等 db 缺失回合)。

        纯增量 (时间游标), 不重放。SDK 通道不可用/宿主无该能力 → 静默退出。
        """
        url = f"http://127.0.0.1:{self.cfg.overlay_port}/push"
        ctx = getattr(self.plugin, "ctx", None)
        if ctx is None:
            return
        try:
            from ..sources.sdk_bus import SdkBusSource
        except Exception:
            return
        source = SdkBusSource(ctx, turn_types=("proactive_reply",))
        if not source.supported():
            # 宿主无 conversations 通道(旧版 / 用户插件门禁关闭) → 静默不补源
            return
        seen_ts = float(self._load_seen("sdk"))
        if seen_ts <= 0:
            seen_ts = time.time()          # 首启: 跳过存量, 只收之后的自主回合
            self._save_seen(seen_ts, "sdk")
        interval = self._sdk_interval()
        self.log.info("桥补源: sdk proactive_reply → %s (自 %s, 间隔 %.2fs)", url,
                      time.strftime("%H:%M:%S", time.localtime(seen_ts)), interval)
        while True:
            try:
                items = await source.poll_once(seen_ts)
                if items:
                    self.log.info("桥补源: %d 条自主搭话", len(items))
                for role, text in items:
                    await self._push_one(url, role, text)
                    if len(items) > 1:
                        await asyncio.sleep(0.35)
                if source.last_ts > seen_ts:
                    seen_ts = source.last_ts
                    self._save_seen(seen_ts, "sdk")
            except asyncio.CancelledError:
                raise
            except Exception:
                # SDK 通道不可用 → 静默退 (不影响主源)
                return
            await asyncio.sleep(interval)

    async def shutdown(self) -> None:
        if getattr(self, "_bridge_stop", None) is not None:
            self._bridge_stop.set()
        if self._bridge_thread and self._bridge_thread.is_alive():
            self._bridge_thread.join(timeout=2.0)
        self._bridge_thread = None
        if getattr(self, "_bridge_source", None) is not None:
            close = getattr(self._bridge_source, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self._bridge_source = None
        self.host.stop()

    async def apply_config(self, raw: dict) -> None:
        new_cfg = DanmakuConfig.from_raw(raw)
        old_cfg = self.cfg
        changed_port = new_cfg.overlay_port != old_cfg.overlay_port
        changed_source = new_cfg.data_source != old_cfg.data_source
        self.cfg = new_cfg
        self.host.port = new_cfg.overlay_port
        if changed_port and new_cfg.auto_start_overlay:
            self.host.stop()
            self.host.start()
        if changed_source:
            # 数据源切换必须重建桥: 主源循环在启动时就选定并持有 source 实例。
            # 游标按 source 隔离持久化, 重建不会重放历史。
            if self._bridge_stop is not None:
                self._bridge_stop.set()
            if self._bridge_thread and self._bridge_thread.is_alive():
                self._bridge_thread.join(timeout=2.0)
            self._bridge_thread = None
            if new_cfg.enabled and new_cfg.data_source in ("sdk", "time_index", "recent_file"):
                self._start_bridge_thread()
            self.log.info("数据源切换: %s → %s, 桥已重建", old_cfg.data_source, new_cfg.data_source)
        self.log.info("配置已更新: src=%s poll=%s sdk_poll=%s port=%s",
                      self.cfg.data_source, self.cfg.poll_interval_s,
                      self.cfg.sdk_poll_interval_s, self.cfg.overlay_port)
