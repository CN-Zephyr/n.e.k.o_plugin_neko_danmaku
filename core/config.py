"""Persist and validate danmaku settings under ``data/config``."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

_CONFIG_SECTION = "neko_danmaku"
_DATA_SOURCES = {"sdk", "time_index", "recent_file"}
_ROLE_PRESETS = {
    "master", "clean", "neko", "mix", "pink", "champagne", "softbar",
}


def _number(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(max(parsed, minimum), maximum)


def _integer(value, default: int, minimum: int, maximum: int) -> int:
    return int(round(_number(value, default, minimum, maximum)))


def _role_preset(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    human = str(raw.get("human", "master"))
    ai = str(raw.get("ai", "mix"))
    return {
        "human": human if human in _ROLE_PRESETS else "master",
        "ai": ai if ai in _ROLE_PRESETS else "mix",
    }


@dataclass
class DanmakuConfig:
    enabled: bool = True
    # sdk(宿主 conversations 通道, 推荐: 含 AI 主动搭话) | time_index | recent_file
    data_source: str = "sdk"
    recent_path: str = ""                   # 留空自动探测
    db_path: str = ""                       # time_indexed.db 显式路径 (留空自动探测)
    poll_interval_s: float = 0.8            # 文件源轮询间隔 / SDK 间隔上限
    sdk_poll_interval_s: float = 0.3        # SDK 拉取间隔 (拉取式通道, 越短越接近实时)
    overlay_port: int = 48777
    auto_start_overlay: bool = True
    role_preset: dict = field(default_factory=lambda: {"human": "master", "ai": "mix"})
    ui_theme: str = "light"

    @classmethod
    def from_raw(cls, raw: dict) -> "DanmakuConfig":
        raw = dict(raw or {})
        data_source = str(raw.get("data_source", "sdk"))
        if data_source not in _DATA_SOURCES:
            data_source = "sdk"
        ui_theme = str(raw.get("ui_theme", "light"))
        if ui_theme not in {"light", "dark"}:
            ui_theme = "light"
        return cls(
            enabled=bool(raw.get("enabled", True)),
            data_source=data_source,
            recent_path=str(raw.get("recent_path") or ""),
            db_path=str(raw.get("db_path") or ""),
            poll_interval_s=_number(raw.get("poll_interval_s"), 0.8, 0.1, 10.0),
            sdk_poll_interval_s=_number(
                raw.get("sdk_poll_interval_s"), 0.3, 0.1, 5.0
            ),
            overlay_port=_integer(raw.get("overlay_port"), 48777, 1024, 65535),
            auto_start_overlay=bool(raw.get("auto_start_overlay", True)),
            role_preset=_role_preset(raw.get("role_preset")),
            ui_theme=ui_theme,
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "data_source": self.data_source,
            "recent_path": self.recent_path, "db_path": self.db_path,
            "poll_interval_s": self.poll_interval_s,
            "sdk_poll_interval_s": self.sdk_poll_interval_s,
            "overlay_port": self.overlay_port,
            "auto_start_overlay": self.auto_start_overlay,
            "role_preset": self.role_preset,
            "ui_theme": self.ui_theme,
        }


_CATEGORY_KEYS = {
    "general": ("enabled",),
    "source": (
        "data_source",
        "recent_path",
        "db_path",
        "poll_interval_s",
        "sdk_poll_interval_s",
    ),
    "overlay": (
        "overlay_port",
        "auto_start_overlay",
        "role_preset",
    ),
    "ui": ("ui_theme",),
}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class ConfigStore:
    """Persist runtime and panel settings under the plugin's data directory."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.config_dir = self.data_root / "config"
        self.paths_by_category = {
            name: self.config_dir / f"{name}.json"
            for name in _CATEGORY_KEYS
        }

    def _defaults(self) -> Dict[str, Any]:
        return DanmakuConfig().to_dict()

    def load(self, legacy: Dict[str, Any] | None = None) -> Dict[str, Any]:
        defaults = self._defaults()
        output = dict(defaults)
        missing_file = False
        present_keys: set[str] = set()
        for category, path in self.paths_by_category.items():
            if not path.is_file():
                missing_file = True
                continue
            raw = _read_json(path)
            for key in _CATEGORY_KEYS[category]:
                if key in raw and raw[key] is not None:
                    output[key] = raw[key]
                    present_keys.add(key)

        migrated = False
        for key, value in dict(legacy or {}).items():
            if key in defaults and value is not None and key not in present_keys:
                output[key] = value
                migrated = True

        if migrated or missing_file:
            self._write_categories(output)
        return output

    def save(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.load()
        defaults = self._defaults()
        for key, value in dict(patch or {}).items():
            if key in defaults and value is not None:
                cfg[key] = value
        normalized = DanmakuConfig.from_raw(cfg).to_dict()
        self._write_categories(normalized)
        return normalized

    def _write_categories(self, values: Dict[str, Any]) -> None:
        for category, keys in _CATEGORY_KEYS.items():
            payload = {key: values[key] for key in keys}
            _write_json(self.paths_by_category[category], payload)

    def paths(self) -> Dict[str, str]:
        return {
            "config_dir": str(self.config_dir),
            **{
                name: str(path)
                for name, path in self.paths_by_category.items()
            },
        }


def resolve_recent_path(hint: str = "") -> str | None:
    """定位宿主 recent.json: 显式路径优先, 否则扫宿主 memory/ 目录。"""
    if hint:
        return hint if os.path.isfile(hint) else None
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "N.E.K.O" / "memory",
        Path(os.environ.get("APPDATA", "")) / "N.E.K.O" / "memory",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for char_dir in sorted(root.iterdir()):
                cand = char_dir / "recent.json"
                if cand.is_file():
                    return str(cand)
        except Exception:
            continue
    return None


def resolve_timeindex_db(hint: str = "") -> str | None:
    """定位宿主 time_indexed.db (回合级对话时间索引, 追加式, 实时源)。

    与 recent.json 同目录: memory/<角色>/time_indexed.db。显式路径优先。
    """
    if hint:
        return hint if os.path.isfile(hint) else None
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "N.E.K.O" / "memory",
        Path(os.environ.get("APPDATA", "")) / "N.E.K.O" / "memory",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for char_dir in sorted(root.iterdir()):
                cand = char_dir / "time_indexed.db"
                if cand.is_file():
                    return str(cand)
        except Exception:
            continue
    return None
