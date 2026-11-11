"""sources/recent_file: recent.json 落盘数据源 (M1 主用)。

产出新增 (role, raw_text); 清洗由 runtime 统一做 (单点清洗, 幂等)。
"""

from __future__ import annotations

try:
    from ..core import recent_reader as _rr  # 插件内 (plugin.plugins.neko_danmaku.sources)
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import recent_reader as _rr  # 本地直跑 (sources/ 上一级 = 插件根)


class RecentFileSource:
    def __init__(self, path: str) -> None:
        self.path = path

    def _chat_entries(self) -> list[dict]:
        try:
            entries = _rr.load_entries(self.path)
            return [e for e in entries if e.get("type") in ("human", "ai")]
        except Exception:
            return []

    def current_count(self) -> int:
        """当前 human/ai 条目总数 (供首启游标基线/压缩容错)。"""
        return len(self._chat_entries())

    def poll_once(self, seen: int) -> list[tuple[str, str]]:
        """从游标 seen 起取新增, 返回 [(role, raw_text)]。"""
        items: list[tuple[str, str]] = []
        chat = self._chat_entries()
        for e in chat[seen:]:
            role = e.get("type", "")
            raw = _rr.content_to_text(e.get("data", {}).get("content"))
            items.append((role, raw))
        return items
