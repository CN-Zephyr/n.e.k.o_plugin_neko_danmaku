# -*- coding: utf-8 -*-
"""共享文本清洗: 剔除猫娘回复里的演出注释 (旁白/动作描写)。

规则: 所有括号内文字【连同括号本身】全部过滤 —— 全角（）与半角() 都算,
支持嵌套与同串多个括号段。桥接源头(插件侧)就清洗, 浮层/播报等消费端
收到的永远是干净台词。

边界: 只删"成对括号及其内容"; 不成对的裸露括号视为正文保留。
"""
import re

# 一次删除一段"内部不含任何括号"的最内层成对括号块; 循环直到不再变化,
# 从而支持嵌套如: （她（小声）说） → 先删 (小声) → 再删 (她说)
_PAIR_BLOCK = re.compile(r"[（(][^（()）]*[)）]")


def strip_action_notes(text: str) -> str:
    """去掉所有 ()/（） 包裹的演出注释, 返回清理后的文本。"""
    if not text:
        return text
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = _PAIR_BLOCK.sub("", cur)
    return cur.strip()


if __name__ == "__main__":
    cases = [
        # (原文, 期望)
        ("（猫耳兴奋地抖了抖，尾巴翘得老高）会吐弹幕的蓝色大肥鱼？好有意思！那它吐的弹幕里，会有我的rap歌词吗？",
         "会吐弹幕的蓝色大肥鱼？好有意思！那它吐的弹幕里，会有我的rap歌词吗？"),
        ("（指着衣摆，声音软乎乎的）视频里能不能多放一点，你说喜欢我的画面呀？",
         "视频里能不能多放一点，你说喜欢我的画面呀？"),
        ("（她（小声嘀咕）了一下）然后开口了。",
         "然后开口了。"),
        ("（笑）今天就到这吧（挥手）。",
         "今天就到这吧。"),
        ("普通的一句话没有括号。",
         "普通的一句话没有括号。"),
        ("中文（english note）混排也要删。",
         "中文混排也要删。"),
        ("半角(abc)与全角（中文）都删，剩个裸括号(不删。",
         "半角与全角都删，剩个裸括号(不删。"),
        ("", ""),
    ]
    ok = True
    for raw, want in cases:
        got = strip_action_notes(raw)
        flag = "PASS" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[{flag}] {raw!r}\n      -> {got!r}  (期望 {want!r})")
    print("\nALL PASS" if ok else "\nSOME FAILED")
