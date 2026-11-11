"""sources/time_index: 宿主 time_indexed.db 数据源 (实时对话时间索引)。

宿主每回合把 human/ai 消息追加写入 ``memory/<角色>/time_indexed.db`` 的
``time_indexed_original`` 表 (id 自增、只追加不裁剪)。相比 recent.json
(回合末整窗重写、可能被压缩) 更适合做弹幕数据源:
  - 追加式 id 游标, 不会因窗口重写而错位/重放
  - 含完整历史窗口 (开发组口径: 可回溯数天), 但默认只播启动后的新消息

协议 (供 runtime 调度):
  current_max_id() -> int                 # 当前最大 id (首启基线)
  poll_after(after_id) -> list[(role, raw_text)]
"""
from __future__ import annotations

import json
import sqlite3

try:
    from ..core import recent_reader as _rr  # 插件内 (plugin.plugins.neko_danmaku.sources)
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import recent_reader as _rr  # 本地直跑 (sources/ 上一级 = 插件根)

_TABLE = "time_indexed_original"


class TimeIndexSource:
    def __init__(self, db_path: str) -> None:
        self.path = db_path
        self._con: sqlite3.Connection | None = None
        self._last_id = 0

    # sqlite 连接: 只读 + 每次 poll 复用; 宿主是唯一写者, 读安全。
    def _conn(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=2.0)
            self._con.row_factory = sqlite3.Row
        return self._con

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None

    def _table_exists(self) -> bool:
        try:
            row = self._conn().execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (_TABLE,)).fetchone()
            return row is not None
        except Exception:
            return False

    def current_max_id(self) -> int:
        """当前最大行 id (首启游标基线 / 进度检查)。表缺失或读失败 → 0。"""
        try:
            if not self._table_exists():
                return 0
            row = self._conn().execute(
                f"SELECT MAX(id) FROM {_TABLE}").fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def poll_after(self, after_id: int) -> list[tuple[str, str]]:
        """取 id > after_id 的新增, 返回 [(role, raw_text)] (保留时间序)。"""
        out: list[tuple[str, str]] = []
        self._last_id = int(after_id)
        try:
            if not self._table_exists():
                return out
            cur = self._conn().execute(
                f"SELECT id, message FROM {_TABLE} WHERE id > ? ORDER BY id",
                (int(after_id),))
            for row_id, msg_json in cur.fetchall():
                self._last_id = max(self._last_id, int(row_id))
                try:
                    rec = json.loads(msg_json)
                except Exception:
                    continue
                role = str(rec.get("type", "") if isinstance(rec, dict) else "")
                if role not in ("human", "ai"):
                    continue
                data = rec.get("data", {}) if isinstance(rec, dict) else {}
                raw = _rr.content_to_text(data.get("content") if isinstance(data, dict) else None)
                out.append((role, raw))
        except Exception:
            return out
        return out

    @property
    def last_id(self) -> int:
        """Highest row id covered by the latest poll, including ignored rows."""
        return self._last_id
