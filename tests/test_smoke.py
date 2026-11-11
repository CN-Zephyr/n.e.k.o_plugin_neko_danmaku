"""Repository smoke tests for the standalone danmaku plugin package."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))


def _load_config_module():
    spec = importlib.util.spec_from_file_location(
        "danmaku_smoke_config", ROOT / "core" / "config.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_entry_runtime_and_version_are_consistent():
    manifest = _manifest()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert manifest["plugin"]["id"] == "neko_danmaku"
    assert manifest["plugin"]["entry"] in {
        "plugin.plugins.neko_danmaku:NekoDanmakuPlugin",
        "plugins.neko_danmaku:NekoDanmakuPlugin",
    }
    assert manifest["plugin_runtime"]["enabled"] is True
    assert manifest["plugin"]["version"] == project["project"]["version"]


def test_hosted_ui_and_short_guide_are_complete():
    manifest = _manifest()
    ui = manifest["plugin"]["ui"]

    assert ui["enabled"] is True
    assert ui["panel"][0]["entry"] == "static/index.html"
    assert ui["guide"][0]["entry"] == "docs/ui/guide.md"
    assert (ROOT / ui["panel"][0]["entry"]).is_file()
    guide = ROOT / ui["guide"][0]["entry"]
    guide_text = guide.read_text(encoding="utf-8")
    assert guide.is_file() and len(guide_text) < 2400
    assert "快速开始" in guide_text and "常见问题" in guide_text


def test_ui_uses_standard_single_file_static_layout():
    static_dir = ROOT / "static"
    assert [path.name for path in static_dir.iterdir()] == ["index.html"]
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    assert "<style>" in html and "</style>" in html
    assert "<script>" in html and "</script>" in html
    assert "panel.css" not in html and "panel.js" not in html
    assert 'data-theme="light"' in html
    assert 'data-theme-choice="dark"' in html


def test_repository_support_files_and_packaging_rules():
    for relative in (
        ".vscode/settings.json",
        ".vscode/tasks.json",
        ".gitignore",
        "README.md",
        "LICENSE",
        "plugin.toml",
        "pyproject.toml",
    ):
        assert (ROOT / relative).is_file(), f"missing repository support file: {relative}"

    for relative in (".vscode/settings.json", ".vscode/tasks.json"):
        json.loads((ROOT / relative).read_text(encoding="utf-8"))

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("__pycache__/", ".pytest_cache/", "data/", "store.db"):
        assert pattern in gitignore

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = project["tool"]["neko"]["build"]
    assert "data" in build["exclude_dirs"]
    assert "state" in build["exclude_dirs"]
    assert "tests" in build["exclude_dirs"]


def test_entry_module_declares_neko_plugin_class():
    manifest = _manifest()
    module_path, _, class_name = manifest["plugin"]["entry"].partition(":")
    assert module_path.endswith("neko_danmaku")
    assert class_name == "NekoDanmakuPlugin"

    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "@neko_plugin" in source
    assert f"class {class_name}" in source


def test_plugin_runtime_modules_compile():
    modules = [ROOT / "__init__.py"]
    for folder in ("core", "overlay", "sources"):
        modules.extend(sorted((ROOT / folder).glob("*.py")))

    for path in modules:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_overlay_uses_incremental_paint_and_throttled_window_probes():
    source = (ROOT / "overlay" / "app.py").read_text(encoding="utf-8")
    assert "QRegion()" in source
    assert "self.update(region)" in source
    assert "PROCESS_LOOKUP_TTL_S" in source
    assert "OBSCURE_CHECK_TTL_S" in source
    assert "self._apply_passthrough()" in source


def test_config_is_persisted_under_categorized_data_files():
    config_module = _load_config_module()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = config_module.ConfigStore(root)
        values = store.load({
            "data_source": "time_index",
            "overlay_port": 49001,
            "ui_theme": "dark",
        })

        assert values["data_source"] == "time_index"
        assert values["overlay_port"] == 49001
        assert values["ui_theme"] == "dark"
        for name in ("general", "source", "overlay", "ui"):
            assert (root / "config" / f"{name}.json").is_file()

        store.save({"data_source": "recent_file", "ui_theme": "light"})
        reloaded = config_module.ConfigStore(root).load()
        assert reloaded["data_source"] == "recent_file"
        assert reloaded["ui_theme"] == "light"
        assert reloaded["overlay_port"] == 49001


def test_readme_is_product_page_not_changelog_or_screenshot_gallery():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "核心体验" in readme
    assert "配置持久化" in readme
    assert "更新日志" not in readme
    assert "![" not in readme
    assert "<img" not in readme
