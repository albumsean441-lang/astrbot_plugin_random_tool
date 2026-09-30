"""抽查工具真实输出格式（人眼确认 LLM 会看到什么）。"""

import asyncio
import importlib.util
import sys

sys.path.insert(0, "/AstrBot")
spec = importlib.util.spec_from_file_location(
    "rt", "/tmp/random_tool_sandbox/data/plugins/astrbot_plugin_random_tool/main.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules["rt"] = m
spec.loader.exec_module(m)


class C:
    def __init__(self):
        self.tools = []

    def add_llm_tools(self, *t):
        self.tools.extend(t)

    def __getattr__(self, i):
        raise AssertionError(f"未预期的 Context 属性: {i}")


async def go():
    c = C()
    m.RandomToolPlugin(c, config={})
    tt = {x.name: x for x in c.tools}
    rows = [
        ("random_int 1..100", "random_int", {"min": 1, "max": 100}),
        ("random_int 5个不重复 1..50", "random_int",
         {"min": 1, "max": 50, "count": 5, "unique": True}),
        ("random_int -50..-10", "random_int", {"min": -50, "max": -10}),
        ("random_float 0..1 精度4", "random_float",
         {"min": 0, "max": 1, "precision": 4}),
        ("random_float 3个 0..100", "random_float",
         {"min": 0, "max": 100, "precision": 2, "count": 3}),
        ("dice 1d20", "dice", {"notation": "1d20"}),
        ("dice 2d6+3", "dice", {"notation": "2d6+3"}),
        ("dice 3d8 x4次", "dice", {"notation": "3d8", "rolls": 4}),
        ("dice d20 x3次", "dice", {"notation": "d20", "rolls": 3}),
        ("random_choice 三选一", "random_choice", {"items": ["红", "绿", "蓝"]}),
        ("random_choice 5选3不重复", "random_choice",
         {"items": "甲,乙,丙,丁,戊", "count": 3, "unique": True}),
        ("random_choice 权重 9:1", "random_choice",
         {"items": ["中奖", "谢谢参与"], "weights": [1, 9]}),
        ("shuffle 5个", "shuffle", {"items": ["A", "B", "C", "D", "E"]}),
        ("random_string 默认16", "random_string", {}),
        ("random_string 密码20", "random_string",
         {"length": 20, "charset": "password"}),
        ("random_string 验证码6位数字", "random_string",
         {"length": 6, "charset": "digits"}),
        ("random_string 3个readable", "random_string",
         {"length": 10, "charset": "readable", "count": 3}),
    ]
    for label, tool, kwargs in rows:
        out = await tt[tool].call(context=None, **kwargs)
        print(f"  {label:<32} -> {out}")

    print()
    print("错误路径（LLM 传错参数时的返回）：")
    errs = [
        ("random_int min=abc", "random_int", {"min": "abc", "max": 10}),
        ("random_int 区间10^15", "random_int", {"min": 0, "max": 10**15}),
        ("random_choice 空列表", "random_choice", {"items": []}),
        ("random_choice unique超量", "random_choice",
         {"items": ["A", "B"], "count": 5, "unique": True}),
        ("dice 记法 abc", "dice", {"notation": "abc"}),
        ("shuffle 只有1个", "shuffle", {"items": ["A"]}),
        ("random_string 未知charset", "random_string", {"charset": "emoji"}),
    ]
    for label, tool, kwargs in errs:
        out = await tt[tool].call(context=None, **kwargs)
        print(f"  {label:<32} -> {out}")


asyncio.run(go())
