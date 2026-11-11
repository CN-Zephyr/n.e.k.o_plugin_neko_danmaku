"""弹幕猫娘：把宿主聊天变成目标画面上的弹幕浮层。

入口职责:
- startup : 拉起 Qt 浮层子进程(独立进程, 宿主 venv python + vendor PySide6),
            并在插件进程内启动桥(轮询 recent.json → POST 浮层)
- shutdown: 终止浮层子进程与桥任务
- config_change: 热更新桥/浮层配置
"""

from __future__ import annotations

from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    ui,
)

_CONFIG_SECTION = "neko_danmaku"

_UI_WRAP = {
    "action": "call",
    "apply": "call",
    "dashboard": "query",
}


@neko_plugin
class NekoDanmakuPlugin(NekoPluginBase):
    """弹幕猫娘 —— 宿主聊天 → 游戏/窗口弹幕浮层。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.logger = self.enable_file_logging(log_level="INFO")
        self.rt: Any = None
        from .core.config import ConfigStore

        self._config_store = ConfigStore(self.data_path())

    async def _load_config(self) -> dict:
        from .core.config import DanmakuConfig

        raw = await self.config.dump()
        section = raw.get(_CONFIG_SECTION, {}) if isinstance(raw, dict) else {}
        persisted = self._config_store.load(
            section if isinstance(section, dict) else {}
        )
        return DanmakuConfig.from_raw(persisted)

    @lifecycle(id="startup")
    async def startup(self, **_) -> Any:
        from .core.runtime import DanmakuRuntime

        try:
            # 静态 UI 面板 (M2: 目标选择 + 样式预览)
            if (self.config_dir / "static").exists():
                self.register_static_ui("static", index_file="index.html",
                                        cache_control="no-cache, no-store, must-revalidate")
            cfg = await self._load_config()
            if not cfg.enabled:
                return Ok({"status": "disabled"})
            self.rt = DanmakuRuntime(self, cfg)
            status = await self.rt.start()
            return Ok(status)
        except Exception as exc:
            self.logger.exception("startup failed")
            return Err(SdkError(f"启动失败: {exc}"))

    @lifecycle(id="shutdown")
    async def shutdown(self, **_) -> Any:
        if self.rt:
            await self.rt.shutdown()
            self.rt = None
        return Ok({"status": "shutdown"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_) -> Any:
        """配置热更新: 桥轮询间隔/角色预设/浮层开关/端口。"""
        try:
            cfg = await self._load_config()
            if not cfg.enabled:
                if self.rt:
                    await self.rt.shutdown()
                    self.rt = None
                return Ok({"status": "disabled"})
            if self.rt is None:
                from .core.runtime import DanmakuRuntime

                self.rt = DanmakuRuntime(self, cfg)
                status = await self.rt.start()
                return Ok({"status": "started", **status})
            await self.rt.apply_config(cfg.to_dict())
            return Ok({"status": "reloaded"})
        except Exception as exc:
            self.logger.warning("config_change failed: %s", exc)
            return Err(SdkError(f"配置更新失败: {exc}"))

    # ---- UI 面板 (hosted-ui: dashboard context + actions) ----
    @ui.context(id="dashboard")
    async def ui_dashboard(self) -> dict:
        """面板状态: 浮层/数据源/窗口列表。"""
        if not self.rt:
            return {"running": False}
        try:
            return await self.rt.overlay_status()
        except Exception as exc:
            return {"running": False, "error": str(exc)}

    @plugin_entry(id="get_panel_state", name="获取面板状态",
                  description="供面板轮询的完整状态入口 (running/数据源/目标/显示器/窗口/样式)。",
                  input_schema={"type": "object", "properties": {}},
                  metadata={"agent_hidden": True})
    async def entry_get_panel_state(self, **_) -> Any:
        if not self.rt:
            return Ok({"running": False, "connected": False})
        try:
            st = await self.rt.overlay_status()
            st["ui_theme"] = getattr(self.rt.cfg, "ui_theme", "light")
            st["config_paths"] = self._config_store.paths()
            self.logger.info(
                "面板轮询: running=%s src=%s wins=%d tgt=%s",
                st.get("running"), st.get("data_source"),
                len(st.get("windows") or []),
                ((st.get("overlay") or {}).get("target") or {}).get("kind"),
            )
            return Ok(st)
        except Exception as exc:
            self.logger.warning("get_panel_state failed: %s", exc)
            return Ok({"running": False, "connected": False,
                       "error": str(exc)})

    @ui.action(id="apply_target", label="应用目标", tone="primary",
               group="runtime", order=10, refresh_context=True)
    @plugin_entry(
        id="apply_target",
        name="切换弹幕目标",
        description="把弹幕浮层绑定到显示器/窗口/进程/全屏。kind=screen|display|window|process; 窗口/进程默认绑客户区(不盖标题栏), mode=window 绑整窗; process 按 pid 绑定并自动跟随其主窗口。",
        input_schema={"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["screen", "display", "window", "process"]},
            "id": {"type": "integer"},
            "hwnd": {"type": "integer"},
            "pid": {"type": "integer"},
            "mode": {"type": "string", "enum": ["client", "window"]},
        }},
    )
    async def ui_apply_target(self, kind: str = "screen",
                              id: int | None = None,
                              hwnd: int | None = None,
                              pid: int | None = None,
                              mode: str = "client", **_) -> Any:
        if not self.rt:
            return Err(SdkError("弹幕猫娘还没启动"))
        spec = {"kind": kind}
        if id is not None:
            spec["id"] = int(id)
        if hwnd is not None:
            spec["hwnd"] = int(hwnd)
        if pid is not None:
            spec["pid"] = int(pid)
        spec["mode"] = mode if mode in ("client", "window") else "client"
        res = await self.rt.overlay_request("POST", "/target", spec)
        return Ok(res if res.get("ok") else {"ok": False, **res})

    @ui.action(id="apply_style", label="应用样式", tone="primary",
               group="runtime", order=20, refresh_context=True)
    @plugin_entry(
        id="apply_style",
        name="调整弹幕样式",
        description=(
            "热调浮层样式/显示参数。样式: preset/text_color/stroke_color/font_size/bold/"
            "shadow*/glow*/bg*/fill_gradient; 显示: area(top|center|bottom)/area_offset/"
            "max_items/wrap/max_lines/auto_scale/fade_in_ms; 全局: opacity/speed。"
        ),
        input_schema={"type": "object", "properties": {
            "preset": {"type": "string"},
            "font_size": {"type": "integer"},
            "font_family": {"type": "string"},
            "bold": {"type": "boolean"},
            "text_color": {"type": "string"},
            "stroke_color": {"type": "string"},
            "stroke_width": {"type": "number"},
            "opacity": {"type": "number"},
            "speed": {"type": "number"},
            "area": {"type": "string", "enum": ["top", "center", "bottom"]},
            "area_offset": {"type": "integer"},
            "max_items": {"type": "integer"},
            "wrap": {"type": "boolean"},
            "max_lines": {"type": "integer"},
            "auto_scale": {"type": "boolean"},
            "fade_in_ms": {"type": "number"},
            "line_spacing": {"type": "number"},
            "letter_spacing": {"type": "number"},
        }},
    )
    async def ui_apply_style(self, preset: str | None = None,
                             font_size: int | None = None,
                             opacity: float | None = None,
                             speed: float | None = None, **rest) -> Any:
        if not self.rt:
            return Err(SdkError("弹幕猫娘还没启动"))
        body: dict[str, Any] = {}
        if preset:
            body["preset"] = preset
        if font_size is not None:
            body["font_size"] = int(font_size)
        if opacity is not None:
            body["opacity"] = float(opacity)
        if speed is not None:
            body["speed"] = float(speed)
        # 其余显示/样式键原样透传给浮层 /config (null → 该效果关闭)
        for key, val in rest.items():
            if key.startswith("_"):
                continue
            if val is not None:
                body[key] = val
        res = await self.rt.overlay_request("POST", "/config", body)
        return Ok(res if res.get("ok") else {"ok": False, **res})

    # ---- 数据源切换 (面板 / agent / 聊天都能调用) ----
    @ui.action(id="apply_source", label="应用数据源", tone="primary",
               group="runtime", order=30, refresh_context=True)
    @plugin_entry(
        id="set_data_source",
        name="切换聊天数据源",
        description=(
            "切换弹幕数据源并即时生效。sdk=宿主 conversations 通道（推荐，含 AI 主动搭话）；"
            "time_index=宿主 time_indexed.db（只有用户回合）；recent_file=recent.json（旧数据源）。"
            "可同时设置 SDK 拉取间隔（拉取式通道，0.2~0.3 秒接近实时）。"
        ),
        input_schema={"type": "object", "properties": {
            "data_source": {"type": "string", "enum": ["sdk", "time_index", "recent_file"]},
            "sdk_poll_interval_s": {"type": "number"},
            "poll_interval_s": {"type": "number"},
        }, "required": ["data_source"]},
    )
    async def ui_set_data_source(self, data_source: str = "sdk",
                                 sdk_poll_interval_s: float | None = None,
                                 poll_interval_s: float | None = None, **_) -> Any:
        from .core.config import DanmakuConfig

        if not self.rt:
            return Err(SdkError("弹幕猫娘还没启动"))
        ds = str(data_source or "").strip()
        if ds not in ("sdk", "time_index", "recent_file"):
            return Err(SdkError(f"未知数据源: {ds}"))

        patch: dict[str, Any] = {"data_source": ds}
        if sdk_poll_interval_s is not None:
            patch["sdk_poll_interval_s"] = max(0.1, min(float(sdk_poll_interval_s), 5.0))
        if poll_interval_s is not None:
            patch["poll_interval_s"] = max(0.1, min(float(poll_interval_s), 10.0))

        # 1) 持久化到 data/config/source.json
        saved = False
        try:
            self._config_store.save(patch)
            saved = True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("保存数据源配置失败(仍会即时生效): %s", exc)

        # 2) 立即生效: 重建桥 (runtime.apply_config 检测到 data_source 变化会重建)
        try:
            cfg = DanmakuConfig.from_raw(self._config_store.load())
            await self.rt.apply_config(cfg.to_dict())
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("数据源即时生效失败: %s", exc)
            return Err(SdkError(f"数据源已切换但桥重建失败: {exc}"))

        return Ok({"ok": True, "saved": saved, "data_source": ds,
                   "effective_source": self.rt.effective_source,
                   "sdk_interval_s": self.rt._sdk_interval()})

    # ---- 角色样式（主人/猫娘分别选预设） ----
    @ui.action(id="set_role_style", label="应用角色样式", tone="primary",
               group="runtime", order=40, refresh_context=True)
    @plugin_entry(
        id="set_role_style",
        name="设置主人/猫娘弹幕样式",
        description=(
            "分别指定主人(人类)与猫娘(AI)的弹幕样式预设，即时生效并持久化。"
            "主人推荐 master(暖白无底)；猫娘推荐 mix(随机彩色+气泡, 与主人一眼区分)；"
            "也可用 clean/neko/pink/champagne/softbar。"
        ),
        input_schema={"type": "object", "properties": {
            "human": {"type": "string",
                      "enum": ["master", "clean", "neko", "mix", "pink", "champagne", "softbar"]},
            "ai": {"type": "string",
                   "enum": ["mix", "neko", "pink", "clean", "master", "champagne", "softbar"]},
        }},
    )
    async def ui_set_role_style(self, human: str | None = None,
                                ai: str | None = None, **_) -> Any:
        if not self.rt:
            return Err(SdkError("弹幕猫娘还没启动"))
        preset = {"human": str(human) if human else self.rt.cfg.role_preset.get("human", "master"),
                  "ai": str(ai) if ai else self.rt.cfg.role_preset.get("ai", "mix")}
        ok_presets = {"master", "clean", "neko", "mix", "pink", "champagne", "softbar"}
        for role, name in preset.items():
            if name not in ok_presets:
                return Err(SdkError(f"未知样式预设: {name}"))
        saved = False
        try:
            self._config_store.save({"role_preset": preset})
            saved = True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("保存角色样式失败(仍会即时生效): %s", exc)
        # 即时生效: role_preset 由 runtime 每条推送时读取
        self.rt.cfg.role_preset = dict(preset)
        return Ok({"ok": True, "saved": saved, "role_preset": preset})

    @plugin_entry(
        id="save_ui_preferences",
        name="保存面板偏好",
        description="持久化面板主题等界面偏好到 data/config/ui.json。",
        input_schema={
            "type": "object",
            "properties": {
                "theme": {"type": "string", "enum": ["light", "dark"]},
            },
        },
        metadata={"agent_hidden": True},
    )
    async def entry_save_ui_preferences(self, theme: str = "light", **_) -> Any:
        normalized = theme if theme in {"light", "dark"} else "light"
        try:
            values = self._config_store.save({"ui_theme": normalized})
        except Exception as exc:  # noqa: BLE001
            return Err(SdkError(f"保存面板偏好失败: {exc}"))
        if self.rt:
            self.rt.cfg.ui_theme = normalized
        return Ok({
            "ok": True,
            "ui_theme": values["ui_theme"],
            "path": self._config_store.paths_by_category["ui"].as_posix(),
        })

    # ---- 全链路自检: 走真实桥推 2 条测试弹幕 ----
    @plugin_entry(
        id="self_test",
        name="弹幕自检",
        description=(
            "走真实链路（数据源→桥→浮层）推送主人/猫娘各一条测试弹幕，"
            "用于确认全屏/窗口绑定时弹幕是否真的能上屏。"
        ),
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_hidden": True},
    )
    async def entry_self_test(self, **_) -> Any:
        if not self.rt:
            return Err(SdkError("弹幕猫娘还没启动"))
        url = f"http://127.0.0.1:{self.rt.cfg.overlay_port}/push"
        try:
            await self.rt._push_one(url, "human", "【自检】主人这一条能上屏吗喵？")
            await self.rt._push_one(url, "ai", "【自检】我在呢，猫娘这条也能上屏喵～")
        except Exception as exc:  # noqa: BLE001
            return Err(SdkError(f"推送失败: {exc}"))
        try:
            st = await self.rt.overlay_status()
        except Exception:  # noqa: BLE001
            st = {}
        ov = st.get("overlay") or {}
        return Ok({"ok": True,
                   "target": ov.get("target"),
                   "obscured": ov.get("obscured"),
                   "pending_target": ov.get("pending_target"),
                   "dropped": ov.get("dropped"),
                   "overlay_running": st.get("running"),
                   "effective_source": self.rt.effective_source})
