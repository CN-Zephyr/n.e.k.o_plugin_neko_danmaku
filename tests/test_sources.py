"""sources 与 core 纯函数单测 (Linux 可跑, 无 Qt/win 依赖)。

直接按文件 import (绕过插件包 __init__, 避免拉 SDK 依赖)。

用法: pytest tests/ 或 python -m pytest tests/
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # 插件根
_CORE = os.path.join(_ROOT, "core")
_SRC = os.path.join(_ROOT, "sources")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules: dataclass 解析注解时要按 __module__ 回查模块
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


_cleaner = _load("cleaner", os.path.join(_CORE, "cleaner.py"))
_recent_reader = _load("recent_reader", os.path.join(_CORE, "recent_reader.py"))
_recent_file = _load("recent_file", os.path.join(_SRC, "recent_file.py"))
_time_index = _load("time_index", os.path.join(_SRC, "time_index.py"))

strip_action_notes = _cleaner.strip_action_notes
content_to_text = _recent_reader.content_to_text
strip_ts_prefix = _recent_reader.strip_ts_prefix
iter_new_chat = _recent_reader.iter_new_chat
RecentFileSource = _recent_file.RecentFileSource
TimeIndexSource = _time_index.TimeIndexSource


def _write_recent(tmp, entries):
    p = os.path.join(tmp, "recent.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    return p


def test_cleaner_strips_cn_and_en_brackets():
    assert strip_action_notes("（猫耳抖了抖）你好喵") == "你好喵"
    # 删括号后残留空格由管线折叠 (" ".join(split())); cleaner 只删括号
    assert strip_action_notes("hello (world) bye") == "hello  bye"
    assert " ".join(strip_action_notes("hello (world) bye").split()) == "hello bye"
    assert strip_action_notes("（嵌套（内层）测试）正文") == "正文"
    assert strip_action_notes("无括号正文") == "无括号正文"
    assert strip_action_notes("留个(不配对") == "留个(不配对"


def test_strip_ts_prefix():
    assert strip_ts_prefix("[20260906 Sun 00:57] 你好") == "你好"
    assert strip_ts_prefix("没前缀") == "没前缀"


def test_content_to_text_str_and_list():
    assert content_to_text("纯文本") == "纯文本"
    assert content_to_text([{"type": "text", "text": "段一"},
                            {"type": "text", "text": "段二"}]) == "段一段二"
    assert content_to_text(None) == ""


def test_iter_new_chat_only_human_ai(tmp_path):
    entries = [
        {"type": "system", "data": {"content": "备忘"}},
        {"type": "human", "data": {"content": "主人话"}},
        {"type": "ai", "data": {"content": [{"type": "text", "text": "猫娘话"}]}},
    ]
    p = _write_recent(str(tmp_path), entries)
    got = list(iter_new_chat(p, 0))
    assert got == [("human", "主人话"), ("ai", "猫娘话")]
    got2 = list(iter_new_chat(p, 1))
    assert got2 == [("ai", "猫娘话")]


def test_recent_file_source_poll(tmp_path):
    entries = [
        {"type": "human", "data": {"content": "甲"}},
        {"type": "ai", "data": {"content": "乙"}},
    ]
    p = _write_recent(str(tmp_path), entries)
    src = RecentFileSource(p)
    items = src.poll_once(0)
    assert len(items) == 2
    assert items[0][0] == "human"
    assert items[1][1] == "乙"
    assert src.poll_once(99) == []
    assert src.current_count() == 2


def test_recent_file_skip_existing_count(tmp_path):
    """首启游标基线 = 当前条目数: 跳过存量, 只播之后新增。"""
    entries = [
        {"type": "human", "data": {"content": "旧话1"}},
        {"type": "ai", "data": {"content": "旧话2"}},
        {"type": "human", "data": {"content": "旧话3"}},
    ]
    p = _write_recent(str(tmp_path), entries)
    src = RecentFileSource(p)
    base = src.current_count()
    assert base == 3
    assert src.poll_once(base) == []            # 存量全部跳过
    # 新增一条 → 只返回这一条
    entries.append({"type": "ai", "data": {"content": "新话"}})
    _write_recent(str(tmp_path), entries)
    got = src.poll_once(base)
    assert got == [("ai", "新话")]


def test_recent_file_shrink_clamps(tmp_path):
    """宿主压缩/截断 recent.json 后, 超界游标收敛到尾部且不重放。"""
    entries = [
        {"type": "human", "data": {"content": "甲"}},
        {"type": "ai", "data": {"content": "乙"}},
        {"type": "human", "data": {"content": "丙"}},
    ]
    p = _write_recent(str(tmp_path), entries)
    src = RecentFileSource(p)
    assert src.current_count() == 3
    # 模拟宿主截断头部: 只剩 1 条
    _write_recent(str(tmp_path), entries[2:])
    assert src.current_count() == 1
    assert src.poll_once(3) == []   # 越界游标不产生重放


def test_recent_file_missing_returns_empty():
    src = RecentFileSource("/nonexistent/recent.json")
    assert src.poll_once(0) == []


def _make_timeindex_db(tmp, rows):
    """建一个模拟宿主 time_indexed_original 表的 sqlite。rows: [(id, type, content)]"""
    import sqlite3
    p = os.path.join(tmp, "time_indexed.db")
    if os.path.exists(p):
        os.remove(p)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE time_indexed_original "
                "(id INTEGER PRIMARY KEY, session_id TEXT, message TEXT, timestamp TEXT)")
    for mid, mtype, content in rows:
        import json
        msg = json.dumps({"type": mtype,
                          "data": {"content": content}}, ensure_ascii=False)
        con.execute("INSERT INTO time_indexed_original (id, session_id, message, timestamp)"
                    " VALUES (?,?,?,?)",
                    (mid, "s1", msg, "2026-01-01 00:00:00"))
    con.commit()
    con.close()
    return p


def test_time_index_poll_after(tmp_path):
    p = _make_timeindex_db(str(tmp_path), [
        (1, "human", "甲"),
        (2, "ai", "乙"),
        (5, "system", "忽略我"),
        (8, "ai", [{"type": "text", "text": "丙"}]),
    ])
    src = TimeIndexSource(p)
    assert src.current_max_id() == 8
    # 只取 id>2 的 human/ai (跳过 system)
    got = src.poll_after(2)
    assert got == [("ai", "丙")]
    assert src.last_id == 8
    got0 = src.poll_after(0)
    assert len(got0) == 3
    assert got0[0] == ("human", "甲")
    assert src.poll_after(99) == []
    src.close()


def test_time_index_skip_existing(tmp_path):
    """首启基线 = 当前 MAX(id): 跳过存量, 只播之后新增。"""
    p = _make_timeindex_db(str(tmp_path), [
        (1, "human", "旧话1"), (2, "ai", "旧话2"),
    ])
    src = TimeIndexSource(p)
    base = src.current_max_id()
    assert base == 2
    assert src.poll_after(base) == []
    src.close()          # 释放文件句柄后才能重写 db
    # 宿主追加新行
    _make_timeindex_db(str(tmp_path), [
        (1, "human", "旧话1"), (2, "ai", "旧话2"),
        (3, "human", "新话"), (4, "ai", "新回"),
    ])
    src2 = TimeIndexSource(p)
    assert src2.poll_after(base) == [("human", "新话"), ("ai", "新回")]
    src2.close()


def test_time_index_missing_db():
    src = TimeIndexSource("/nonexistent/time_indexed.db")
    assert src.current_max_id() == 0
    assert src.poll_after(0) == []
    src.close()


# ---- sdk_bus (宿主 conversations 通道) ----
_sdk_bus = _load("sdk_bus", os.path.join(_SRC, "sdk_bus.py"))
SdkBusSource = _sdk_bus.SdkBusSource
_role_of = _sdk_bus._role_of


class _Rec:
    """conversations 记录的轻量替身（字段对齐 ConversationRecord）。"""

    def __init__(self, content, *, ts, turn_type="", lanlan_name="", cid="", rid=None):
        self.content = content
        self.timestamp = ts
        self.turn_type = turn_type
        self.lanlan_name = lanlan_name
        self.conversation_id = cid
        self.raw = {"id": rid} if rid else {}


class _FakeConversations:
    """模拟 ctx.bus.conversations.get(since_ts=...)（宿主按到达时间过滤）。"""

    def __init__(self, records):
        self.records = list(records)
        self.calls = []

    def get(self, *, since_ts=0.0, max_count=50, timeout=5.0):
        self.calls.append(float(since_ts))
        return [r for r in self.records
                if float(r.timestamp) > float(since_ts)][:max_count]


class _FakeCtx:
    def __init__(self, conversations=None):
        self.bus = type("B", (), {})()
        if conversations is not None:
            self.bus.conversations = conversations


def test_sdk_role_mapping():
    # 宿主发布点: user_message / instruction / assistant_message / reply / proactive_reply
    assert _role_of("user_message") == "human"
    assert _role_of("instruction") == "human"
    assert _role_of("assistant_message") == "ai"
    assert _role_of("reply") == "ai"
    assert _role_of("proactive_reply") == "ai"
    assert _role_of("") == "ai"


def test_sdk_supported_detection():
    assert SdkBusSource(None).supported() is False
    assert SdkBusSource(_FakeCtx(None)).supported() is False
    assert SdkBusSource(_FakeCtx(_FakeConversations([]))).supported() is True


def test_sdk_poll_once_cursor_dedupe_and_role():
    import asyncio

    conv = _FakeConversations([
        _Rec("妈妈我回来了", ts=100.0, turn_type="user_message", lanlan_name="YUI", rid="e1"),
        _Rec("本喵才没等你", ts=100.5, turn_type="assistant_message", lanlan_name="YUI", rid="e2"),
        _Rec("主人今天又熬夜", ts=101.0, turn_type="proactive_reply", lanlan_name="YUI", rid="e3"),
    ])
    src = SdkBusSource(_FakeCtx(conv))
    got = asyncio.run(src.poll_once(99.0))
    assert got == [("human", "妈妈我回来了"), ("ai", "本喵才没等你"), ("ai", "主人今天又熬夜")]
    assert abs(src.last_ts - 101.0) < 1e-9
    # 同一批再拉: seen_ts 未变但 event_id 已记 → 不重复上屏
    assert asyncio.run(src.poll_once(99.0)) == []
    # 游标推进后只出新的
    conv.records.append(_Rec("新一句", ts=102.0, turn_type="assistant_message", rid="e4"))
    assert asyncio.run(src.poll_once(src.last_ts)) == [("ai", "新一句")]


def test_sdk_turn_type_and_lanlan_filters():
    import asyncio

    conv = _FakeConversations([
        _Rec("用户", ts=10.0, turn_type="user_message", lanlan_name="YUI"),
        _Rec("主动", ts=11.0, turn_type="proactive_reply", lanlan_name="YUI"),
        _Rec("别的角色", ts=12.0, turn_type="assistant_message", lanlan_name="Mika"),
    ])
    # 补源模式: 只推主动搭话 + 只看本角色
    src = SdkBusSource(_FakeCtx(conv), turn_types=("proactive_reply",), lanlan_name="YUI")
    assert asyncio.run(src.poll_once(0.0)) == [("ai", "主动")]


def test_sdk_empty_and_blank_records_skipped():
    import asyncio

    conv = _FakeConversations([
        _Rec("   ", ts=1.0, turn_type="user_message"),
        _Rec("", ts=2.0, turn_type="assistant_message"),
        _Rec("有效", ts=3.0, turn_type="user_message"),
    ])
    src = SdkBusSource(_FakeCtx(conv))
    assert asyncio.run(src.poll_once(0.0)) == [("human", "有效")]


# ---- core/config 默认值 (0.2.0 起默认走 sdk) ----
_config = _load("danmaku_config", os.path.join(_CORE, "config.py"))
DanmakuConfig = _config.DanmakuConfig
ConfigStore = _config.ConfigStore


def test_config_defaults_sdk():
    cfg = DanmakuConfig.from_raw({})
    assert cfg.data_source == "sdk"
    assert abs(cfg.sdk_poll_interval_s - 0.3) < 1e-9
    assert cfg.poll_interval_s > 0
    assert cfg.ui_theme == "light"
    # 显式指定仍然生效
    assert DanmakuConfig.from_raw({"data_source": "time_index"}).data_source == "time_index"
    assert DanmakuConfig.from_raw({"sdk_poll_interval_s": 0.5}).sdk_poll_interval_s == 0.5
    # to_dict/from_raw 往返不丢新字段
    d = cfg.to_dict()
    assert "sdk_poll_interval_s" in d
    assert DanmakuConfig.from_raw(d).data_source == "sdk"
    assert DanmakuConfig.from_raw({"ui_theme": "dark"}).ui_theme == "dark"
    assert DanmakuConfig.from_raw({"ui_theme": "bad"}).ui_theme == "light"


def test_config_rejects_invalid_runtime_values():
    cfg = DanmakuConfig.from_raw({
        "data_source": "unexpected",
        "poll_interval_s": "not-a-number",
        "sdk_poll_interval_s": float("nan"),
        "overlay_port": 999999,
        "role_preset": {"human": "unknown", "ai": "also-unknown"},
    })
    assert cfg.data_source == "sdk"
    assert cfg.poll_interval_s == 0.8
    assert cfg.sdk_poll_interval_s == 0.3
    assert cfg.overlay_port == 65535
    assert cfg.role_preset == {"human": "master", "ai": "mix"}


def test_config_clamps_supported_ranges():
    low = DanmakuConfig.from_raw({
        "poll_interval_s": -10,
        "sdk_poll_interval_s": 0,
        "overlay_port": 1,
    })
    high = DanmakuConfig.from_raw({
        "poll_interval_s": 999,
        "sdk_poll_interval_s": 999,
    })
    assert low.poll_interval_s == 0.1
    assert low.sdk_poll_interval_s == 0.1
    assert low.overlay_port == 1024
    assert high.poll_interval_s == 10.0
    assert high.sdk_poll_interval_s == 5.0


def test_config_store_persists_categories_and_migrates_legacy(tmp_path):
    root = Path(tmp_path)
    store = ConfigStore(root)
    migrated = store.load({
        "data_source": "time_index",
        "overlay_port": 49000,
        "role_preset": {"human": "neko", "ai": "pink"},
        "ui_theme": "dark",
    })

    assert migrated["data_source"] == "time_index"
    assert migrated["overlay_port"] == 49000
    assert migrated["ui_theme"] == "dark"
    for name in ("general", "source", "overlay", "ui"):
        assert (root / "config" / f"{name}.json").is_file()

    store.save({
        "data_source": "recent_file",
        "sdk_poll_interval_s": 0.2,
        "ui_theme": "light",
    })
    reloaded = ConfigStore(root).load()
    assert reloaded["data_source"] == "recent_file"
    assert reloaded["sdk_poll_interval_s"] == 0.2
    assert reloaded["overlay_port"] == 49000
    assert reloaded["ui_theme"] == "light"


def test_config_store_creates_defaults_on_first_run(tmp_path):
    store = ConfigStore(Path(tmp_path))
    values = store.load()

    assert values["data_source"] == "sdk"
    assert values["ui_theme"] == "light"
    assert set(store.paths()) == {
        "config_dir", "general", "source", "overlay", "ui",
    }


if __name__ == "__main__":
    # 免 pytest 直跑: python tests/test_sources.py
    import inspect
    import traceback

    tmp_root = os.path.join(os.path.dirname(_ROOT), "overlay_proto", "_ndm_test")
    os.makedirs(tmp_root, exist_ok=True)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            params = list(inspect.signature(fn).parameters)
            if params:
                d = os.path.join(tmp_root, fn.__name__)
                os.makedirs(d, exist_ok=True)
                fn(d)
            else:
                fn()
            ok += 1
            print(f"PASS {fn.__name__}")
        except Exception:
            traceback.print_exc()
            print(f"FAIL {fn.__name__}")
    print(f"{ok}/{len(fns)} passed")

