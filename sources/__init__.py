"""sources: 宿主聊天数据源接入。

- sdk_bus     : SDK ctx.bus.conversations 通道（默认/推荐，覆盖 AI 主动搭话；
                拉取式，按 sdk_poll_interval_s 增量轮询）
- time_index  : 宿主 time_indexed.db（回合级时间索引，只有用户回合）
- recent_file : recent.json 落盘轮询（旧数据源）

协议 (供 runtime 调度):
  poll_once(seen) -> list[(role, text)]
"""
