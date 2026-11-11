from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_overlay_module():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    for path in (ROOT / "vendor", ROOT / "overlay"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    spec = importlib.util.spec_from_file_location(
        "danmaku_overlay_test_app", ROOT / "overlay" / "app.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_overlay_style_sanitizes_untrusted_values():
    overlay = _load_overlay_module()
    style = overlay.sanitize_style({
        "font_size": "999999",
        "stroke_width": "nan",
        "text_color": "javascript:bad",
        "bg": "evil",
        "max_items": 0,
        "fill_gradient": ["#ffffff", "bad"],
    })

    assert style["font_size"] == 96
    assert style["stroke_width"] == 3.0
    assert style["text_color"] == "#ffffff"
    assert style["bg"] == "none"
    assert style["max_items"] == 1
    assert "fill_gradient" not in style


def test_overlay_style_keeps_valid_customization():
    overlay = _load_overlay_module()
    style = overlay.sanitize_style({
        "font_size": 42,
        "stroke_width": 4.5,
        "text_color": "#abcdef",
        "fill_gradient": ["#ffffff", "#123456"],
        "shadow": True,
        "glow": True,
    })

    assert style["font_size"] == 42
    assert style["stroke_width"] == 4.5
    assert style["text_color"] == "#abcdef"
    assert style["fill_gradient"] == ["#ffffff", "#123456"]
    assert style["shadow"] is True
    assert style["glow"] is True


def test_overlay_paint_path_accepts_sanitized_runtime_config(tmp_path):
    overlay = _load_overlay_module()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    state_path = tmp_path / "config" / "overlay_style.json"
    window = overlay.DanmakuOverlay(port=0, state_path=state_path)
    try:
        window.apply_config({
            "font_size": "invalid",
            "opacity": 9,
            "speed": -20,
            "max_items": 999,
            "line_spacing": "bad",
            "text_color": "not-a-color",
            "fill_gradient": ["#ffffff", "also-bad"],
        })
        assert window._opacity == 1.0
        assert window._speed == 20.0
        assert window.style["max_items"] == 200
        sprite = window._render_sprite("弹幕绘制测试", window.style)
        assert not sprite.isNull()
        assert sprite.width() > 0 and sprite.height() > 0
        assert state_path.is_file()
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["style"]["max_items"] == 200
        window._target = {"kind": "screen"}
        window._obscured = False
        assert window.push("增量重绘测试", window.style)
        item = window._items[0]
        item.entered = True
        item.lane = 0
        item.x = 100.0
        item.y = 20.0
        window._on_tick()
        app.processEvents()
        assert window._timer.interval() == overlay.FRAME_INTERVAL_MS
        assert window._tick_ms >= 0.0
    finally:
        try:
            window._http.shutdown()
            window._http.server_close()
        except Exception:
            pass
        window.close()
        app.processEvents()
