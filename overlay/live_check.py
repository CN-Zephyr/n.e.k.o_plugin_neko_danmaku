"""全屏/窗口覆盖自检: 绑定当前前台游戏 → 推 2 条测试弹幕 → 报告 z-order 与入队结果。

用途: 游戏切成(无边框)全屏后运行, 4 秒内应能在游戏画面上看到两条测试弹幕。
输出可直接定位问题:
    - game obscured=True      → 目标被遮挡(浮层会隐藏, 弹幕被丢弃)
    - overlay above game=False → 浮层没压到游戏上(置顶/独占全屏问题)
    - items/entered=0          → 没入队
    - dropped>0                → 推弹幕被拒(目标未绑定/被遮挡)

用法:
    set QT_QPA_PLATFORM=windows
    <宿主 python> overlay\\live_check.py
退出码 0=通过(浮层在游戏之上且入队成功), 1=失败。
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.dirname(_HERE)

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.join(_PLUGIN, "vendor"))
sys.path.insert(0, _HERE)

import win_target as wt  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

spec = importlib.util.spec_from_file_location("ndm_overlay_live", os.path.join(_HERE, "app.py"))
app_mod = importlib.util.module_from_spec(spec)
sys.modules["ndm_overlay_live"] = app_mod
spec.loader.exec_module(app_mod)


def main() -> int:
    game = wt.get_foreground_window()
    if not game:
        print("FAIL: 没有前台窗口 (先把游戏切到前台再运行)")
        return 1
    covered = wt.covers_monitor(game)
    obscured = wt.is_target_obscured(game)
    print(f"game hwnd={game} covers_monitor={covered} obscured={obscured}")

    qapp = QApplication([])
    win = app_mod.DanmakuOverlay(port=49123)
    win.targetRequested.emit({"kind": "window", "hwnd": int(game), "mode": "client"})
    win.push("【自检】浮层覆盖测试 1/2", {"preset": "mix"})
    win.push("【自检】能看到这条 = 全屏覆盖正常 2/2", {"preset": "master"})
    own = int(win.winId())
    result: dict = {}

    def _check() -> None:
        above = wt._z_above(game)
        i_game = above.index(game) if game in above else -1
        i_own = above.index(own) if own in above else -1
        above_ok = i_own > i_game >= 0
        entered = sum(1 for d in win._items if d.entered)
        result.update(above_ok=above_ok, entered=entered,
                      items=len(win._items), dropped=int(getattr(win, "_dropped", 0)),
                      obscured=win._obscured, geom=win.geometry().getRect(),
                      target=win._target)
        print(f"z-order: game={i_game} overlay={i_own} → overlay above game={above_ok}")
        print(f"target={win._target} obscured={win._obscured} "
              f"items={len(win._items)} entered={entered} dropped={getattr(win, '_dropped', 0)}")
        print(f"geometry={win.geometry().getRect()} visible={win.isVisible()}")

    QTimer.singleShot(2500, _check)
    QTimer.singleShot(4000, qapp.quit)
    qapp.exec()

    ok = bool(result.get("above_ok")) and result.get("entered", 0) >= 1
    print("RESULT:", "PASS ✅ 全屏覆盖正常(4 秒内应看到两条测试弹幕)" if ok
          else "FAIL ❌ 浮层没能压到游戏上/没入队, 见上面的分项")
    if not ok:
        if not covered and not result.get("above_ok"):
            print("提示: 目标不是全屏, 或游戏为独占全屏(请改无边框全屏)")
        if result.get("obscured"):
            print("提示: 目标被判定遮挡 → 检查是否有置顶窗口盖在游戏上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
