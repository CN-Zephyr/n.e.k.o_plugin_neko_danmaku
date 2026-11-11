"""sources/sdk_bus: 通过 SDK ctx.bus.conversations 读宿主对话回合（含 AI 主动搭话）。

为什么用 conversations 而不是 time_indexed.db / recent.json
------------------------------------------------------------
宿主对**每一回合**都发布 `conversation_turn_observed`
(`main_logic/agent_event_bus.publish_conversation_turn_observed_best_effort`)，
由 agent_server 写入消息平面的 conversations store
(`app/agent_server/api_runtime._forward_conversation_turn`)。覆盖三种回合：

    user_message / instruction      → 主人原话        role=human
    assistant_message / reply       → 猫娘回复        role=ai
    proactive_reply                 → 猫娘**主动搭话** role=ai

`time_indexed.db` / `recent.json` 只落「用户回合」，拿不到自主搭话（proactive_reply），
所以想完整还原聊天框内容必须走这条通道。

实时性（0.9.0.x / 0.9.1.0 双重核实）
------------------------------------
- conversations store 是**拉取式**：只有 `ctx.bus.conversations.get(since_ts=...)` 增量读；
- `BusList.watch()` 的 bus 白名单只有 messages/events/lifecycle，conversations 不在其中
  (`plugin/core/bus/watchers.py`) → 不能推送订阅；
- `conversation_turn_observed` 也不进 events 总线（只进 conversations store）。

所以本模块用「短间隔 + 时间游标」实现准实时：默认 0.3s 轮询，一回合落地就被下一轮读到。
注意 `record["timestamp"]` 是**到达 store 的时间**（`since_ts` 游标按它过滤），
台词实际发生时间在 `metadata.ts`。

协议（供 runtime 调度）
-----------------------
    poll_once(seen_ts) -> list[(role, text)]
    last_ts            -> 已见过的最大到达时间（游标推进依据）
    supported()        -> 宿主是否提供 conversations 通道
"""
from __future__ import annotations

from typing import Any

# turn_type 里含这些子串 → 主人说的话；其余（reply/assistant/proactive）算猫娘
_HUMAN_TOKENS = ("instruction", "user_message", "user_input", "human")
_MAX_DEDUPE = 512


class SdkBusSource:
    """SDK conversations 数据源：增量拉取宿主对话回合文本。"""

    def __init__(
        self,
        ctx: Any,
        *,
        lanlan_name: str | None = None,
        turn_types: tuple[str, ...] | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> None:
        self.ctx = ctx
        self.lanlan_name = lanlan_name
        # turn_types: 只收含这些子串的回合(如 ("proactive_reply",) 用于"补源"模式,
        #             避免和 time_index/recent 已有回合重复上屏)
        self.turn_types = tuple(t.lower() for t in (turn_types or ()))
        self.roles = tuple(r.lower() for r in (roles or ()))
        self._last_ts: float = 0.0
        self._seen_ids: set[str] = set()
        self._seen_order: list[str] = []

    # ---- 能力探测 ----
    def _conversations(self) -> Any | None:
        if self.ctx is None:
            return None
        bus = getattr(self.ctx, "bus", None)
        if bus is None:
            return None
        conv = getattr(bus, "conversations", None)
        if conv is None or not callable(getattr(conv, "get", None)):
            return None
        return conv

    def supported(self) -> bool:
        """宿主是否提供 conversations 通道（决定 runtime 是否回退其它数据源）。"""
        return self._conversations() is not None

    # ---- 主接口 ----
    async def poll_once(self, seen_ts: float) -> list[tuple[str, str]]:
        """取 seen_ts 之后新增的回合文本。返回 [(role, text), ...]。"""
        conv = self._conversations()
        if conv is None:
            return []
        items: list[tuple[str, str]] = []
        max_ts = float(seen_ts or 0.0)
        try:
            result = conv.get(since_ts=float(seen_ts or 0.0), max_count=100, timeout=3.0)
            if hasattr(result, "__await__"):
                result = await result
            records = _unwrap(result)
            for rec in records:
                try:
                    ts = float(getattr(rec, "timestamp", 0) or 0)
                    if ts > max_ts:
                        max_ts = ts
                    content = getattr(rec, "content", None)
                    if not content or not str(content).strip():
                        continue
                    ln = str(getattr(rec, "lanlan_name", "") or "")
                    if self.lanlan_name and ln and ln != self.lanlan_name:
                        continue
                    rec_id = _record_id(rec, ts, str(content))
                    if rec_id and rec_id in self._seen_ids:
                        continue
                    tt = str(getattr(rec, "turn_type", "") or "")
                    role = _role_of(tt)
                    if self.turn_types and not any(t in tt.lower() for t in self.turn_types):
                        continue
                    if self.roles and role not in self.roles:
                        continue
                    # 游标语义: timestamp 是到达时间, 严格大于 seen_ts 才算新回合
                    if ts > seen_ts:
                        items.append((role, str(content)))
                        if rec_id:
                            self._remember(rec_id)
                except Exception:
                    continue
        except Exception:
            # SDK 通道不可用/宿主未实现 → 静默（runtime 可回退其它源）
            return []
        self._last_ts = max_ts
        return items

    def _remember(self, rec_id: str) -> None:
        if rec_id in self._seen_ids:
            return
        self._seen_ids.add(rec_id)
        self._seen_order.append(rec_id)
        while len(self._seen_order) > _MAX_DEDUPE:
            old = self._seen_order.pop(0)
            self._seen_ids.discard(old)

    @property
    def last_ts(self) -> float:
        return self._last_ts


def _role_of(turn_type: str) -> str:
    """conversations 回合类型 → human | ai。

    宿主发布点：
      - `main_logic/core/turn.py`        : user_message / assistant_message / proactive_reply
      - `main_logic/omni_offline_client` : instruction / reply（offline 配对回合）

    含 human 关键词的算主人，其余（reply / assistant / proactive_reply / unknown）算猫娘，
    与「优先不漏自主发言」的原则一致。
    """
    t = (turn_type or "").lower()
    if any(token in t for token in _HUMAN_TOKENS):
        return "human"
    return "ai"


def _record_id(rec: Any, ts: float, content: str) -> str:
    """稳定去重键：优先宿主给的 event_id（写入时放在 record['id']）。"""
    raw = getattr(rec, "raw", None)
    if isinstance(raw, dict):
        for key in ("id", "event_id"):
            val = raw.get(key)
            if isinstance(val, str) and val:
                return val
    cid = getattr(rec, "conversation_id", "") or ""
    tt = getattr(rec, "turn_type", "") or ""
    return f"{ts:.6f}|{cid}|{tt}|{content[:64]}" if (ts or content) else ""


def _unwrap(result: Any) -> list:
    """解 Ok/Err/BusList/列表包装 → 记录列表。"""
    if result is None:
        return []
    is_err = getattr(result, "is_err", None)
    if callable(is_err) and is_err():
        return []
    for field in ("value", "items", "_items", "records"):
        if hasattr(result, field):
            inner = getattr(result, field)
            if isinstance(inner, list):
                return inner
    if isinstance(result, list):
        return result
    # 迭代器/生成器
    try:
        out = list(result)
        return out if out and all(not isinstance(x, tuple) for x in out) else []
    except Exception:
        return []


if __name__ == "__main__":
    # 离线冒烟: 无真实 ctx 时 poll_once 应返回空且不抛
    import asyncio

    class _FakeCtx:
        bus = None

    async def _main() -> None:
        src = SdkBusSource(_FakeCtx())
        print("supported:", src.supported())
        print("poll result:", await src.poll_once(0.0))

    asyncio.run(_main())
    print("sdk_bus conversations smoke OK")
