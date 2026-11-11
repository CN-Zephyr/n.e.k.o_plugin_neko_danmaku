from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "panel.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "panel.js").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_panel_is_split_and_defaults_to_light_theme():
    assert 'data-theme="light"' in HTML
    assert '<link rel="stylesheet" href="panel.css">' in HTML
    assert '<script src="panel.js"></script>' in HTML
    assert 'data-theme-choice="dark"' in HTML
    assert 'location.search' in JS


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
    assert '[data-theme="dark"]' in CSS
    assert "@media (max-width: 680px)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "animation: drift" in CSS


def test_panel_avoids_inline_click_handlers_and_overlapping_refresh():
    assert "onclick=" not in HTML
    assert "if (refreshPromise) return refreshPromise;" in JS
    assert "if (!document.hidden) refresh();" in JS
    assert "if (formDirty)" in JS


def test_readme_is_product_presentation_not_changelog():
    assert "更新日志" not in README
    assert "核心体验" in README
    assert "docs/assets/panel-overview-light.png" in README
    assert "docs/assets/panel-appearance-dark.png" in README
    assert (ROOT / "docs" / "assets" / "panel-overview-light.png").is_file()
    assert (ROOT / "docs" / "assets" / "panel-appearance-dark.png").is_file()
