"""pytest: 阻止把插件根当包收集 (插件 __init__ 导入 SDK, 本机无 plugin 包时炸)。

把插件根从 sys.path 摘掉, 测试按文件加载模块 (见 test_sources.py 顶部 _load)。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in list(sys.path):
    if p and os.path.abspath(p) == _ROOT:
        sys.path.remove(p)
