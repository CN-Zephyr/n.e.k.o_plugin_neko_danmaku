"""core/recent_reader: 宿主 recent.json 的读取与解析 (纯函数, 可单测)。"""

from __future__ import annotations

import json
import re
from typing import Iterator

# ai 条目前缀: "[20260906 Sun 00:57]" (recent_file 通道需要剥)
_TS_PREFIX = re.compile(r"^\s*\[\d{8}[^\]]*\]\s*")


def strip_ts_prefix(text: str) -> str:
    return _TS_PREFIX.sub("", text)


def content_to_text(content) -> str:
    """recent.json 的 content 可能是 str 或 [{type:text,text:...}] list。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def load_entries(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def iter_new_chat(path: str, seen: int) -> Iterator[tuple[str, str]]:
    """从游标位置起, 产出新增的 (role, raw_text)。role ∈ human|ai。"""
    entries = load_entries(path)
    chat = [e for e in entries if e.get("type") in ("human", "ai")]
    for e in chat[seen:]:
        role = e.get("type", "")
        text = content_to_text(e.get("data", {}).get("content"))
        yield role, text
