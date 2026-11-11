from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = HTML
JS = HTML
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_panel_uses_standard_single_file_static_layout():
    assert 'data-theme="light"' in HTML
    assert "<style>" in HTML and "</style>" in HTML
    assert "<script>" in HTML and "</script>" in HTML
    assert '<link rel="stylesheet"' not in HTML
    assert '<script src=' not in HTML
    assert 'data-theme-choice="dark"' in HTML
    assert 'location.search' in JS
    assert 'id="prevlabel" class="text-input" value="会吐弹幕的可爱猫娘YUI~喵"' in HTML
    assert [path.name for path in (ROOT / "static").iterdir()] == ["index.html"]


def test_host_ui_guide_is_short_and_declared():
    manifest = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))
    guides = manifest["plugin"]["ui"]["guide"]
    guide = guides[0]
    path = ROOT / guide["entry"]
    text = path.read_text(encoding="utf-8")

    assert guide["id"] == "guide"
    assert guide["entry"] == "docs/ui/guide.md"
    assert path.is_file()
    assert len(text) < 2400
    assert "快速开始" in text and "常见问题" in text
    assert manifest["plugin"]["store"]["enabled"] is False
    assert "neko_danmaku" not in manifest


def test_all_static_element_ids_are_unique():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))


def test_javascript_direct_id_selectors_exist_in_markup():
    ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    selectors = set(re.findall(r'\$\("#([A-Za-z][A-Za-z0-9_-]*)"\)', JS))
    missing = selectors - ids
    assert not missing, f"JavaScript references missing panel ids: {sorted(missing)}"


def test_navigation_pages_match_panel_sections():
    nav_pages = set(re.findall(r'data-page="([^"]+)"', HTML))
    panel_pages = set(re.findall(r'data-page-panel="([^"]+)"', HTML))
    meta_pages = set(re.findall(r'^\s{2}([a-z]+): \["', JS, flags=re.MULTILINE))

    assert nav_pages == panel_pages == meta_pages


def test_liquid_glass_and_responsive_layout_contract():
    assert "backdrop-filter: blur(30px) saturate(150%)" in CSS
    assert "filter: blur(8px)" in CSS
    assert "mix-blend-mode: soft-light" in CSS
    assert '[data-theme="dark"]' in CSS
    assert "@media (max-width: 680px)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "animation: drift" in CSS
    assert "animation: preview-sweep" in CSS
    assert "@keyframes preview-sweep" in CSS
    assert "will-change: transform" in CSS


def test_panel_avoids_inline_click_handlers_and_overlapping_refresh():
    assert "onclick=" not in HTML
    assert "if (refreshPromise) return refreshPromise;" in JS
    assert "if (!document.hidden) refresh();" in JS
    assert "if (formDirty)" in JS


def test_color_presets_are_transparent_text_styles():
    assert 'neko: { color: "#9adcff", stroke: "#08243a", width: 3, bg: "none"' in JS
    assert 'softbar: { color: "#e8f7ff", stroke: "#102838", width: 2.5, bg: "none"' in JS
    assert 'mix: { color: "#ffffff", stroke: "#101018", width: 3, bg: "none"' in JS
    assert 'const alpha = Number($("#p-bg_alpha").value);' in JS


def test_mix_preview_randomizes_text_color_per_selection():
    assert "const MIX_COLORS = [" in JS
    assert "function pickMixPreviewColor()" in JS
    assert 'next === "mix" ? pickMixPreviewColor()' in JS


def test_readme_is_product_presentation_not_changelog():
    assert "更新日志" not in README
    assert "核心体验" in README
    assert "![" not in README
    assert "<img" not in README
    assert not (ROOT / "assets").exists()
