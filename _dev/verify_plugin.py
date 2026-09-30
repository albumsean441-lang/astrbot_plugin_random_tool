"""随机数插件功能验证（在容器沙箱内运行，不接触线上实例）。

覆盖：
1. 6 个工具的 JSON Schema 能被 AstrBot 的 ToolSchema 校验器接受
2. 每个工具的正常路径返回值
3. 边界与非法输入的错误处理（不能抛异常，要返回可读错误）
4. 区间取值的无偏性（对比朴素取模的偏差）
5. standard / secure 两种随机源后端
"""

import asyncio
import collections
import importlib.util
import json
import sys

PLUGIN_DIR = "/tmp/random_tool_sandbox/data/plugins/astrbot_plugin_random_tool"
sys.path.insert(0, "/AstrBot")

# 显式按文件路径加载，避免与 AstrBot 自带的 main.py 同名冲突
_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_random_tool_main", f"{PLUGIN_DIR}/main.py"
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


class FakeContext:
    """假的 Star Context，只记录 add_llm_tools 收到的工具。"""

    def __init__(self):
        self.tools = []
        self.provider_manager = None

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def __getattr__(self, item):
        raise AssertionError(f"插件调用了未预期的 Context 属性: {item}")


async def run_cases(tool, cases):
    """cases: [(用例名, kwargs, 断言函数)]"""
    for name, kwargs, assertion in cases:
        try:
            result = await tool.call(context=None, **kwargs)
        except Exception as e:  # noqa: BLE001
            check(name, False, f"抛异常了: {type(e).__name__}: {e}")
            continue
        try:
            ok, detail = assertion(result)
        except Exception as e:  # noqa: BLE001
            check(name, False, f"断言本身出错: {type(e).__name__}: {e}")
            continue
        check(name, ok, detail)


def build_plugin():
    ctx = FakeContext()
    inst = plugin.RandomToolPlugin(ctx, config={"random_backend": "standard"})
    return ctx, inst


async def main():
    print("=" * 70)
    print("第 1 部分：插件注册与 Schema 校验")
    print("=" * 70)
    ctx, inst = build_plugin()
    check("注册了 6 个工具", len(ctx.tools) == 6, f"实际 {len(ctx.tools)}")
    names = [t.name for t in ctx.tools]
    check(
        "工具名齐全",
        names
        == [
            "random_int",
            "random_float",
            "dice",
            "random_choice",
            "shuffle",
            "random_string",
        ],
        str(names),
    )
    for t in ctx.tools:
        try:
            t.validate_parameters()
            check(f"schema 合法: {t.name}", True)
        except Exception as e:  # noqa: BLE001
            check(f"schema 合法: {t.name}", False, f"{type(e).__name__}: {e}")
        check(f"description 非空: {t.name}", bool(t.description.strip()))
        check(f"active 为 True: {t.name}", getattr(t, "active", None) is True)

    tools = {t.name: t for t in ctx.tools}

    # ---------------------------------------------------------------- random_int
    print()
    print("=" * 70)
    print("第 2 部分：random_int")
    print("=" * 70)

    def in_range(lo, hi):
        def _a(r):
            v = int(r)
            return (lo <= v <= hi), f"得到 {r}"

        return _a

    await run_cases(
        tools["random_int"],
        [
            ("区间内取值", {"min": 1, "max": 100}, in_range(1, 100)),
            ("负数区间", {"min": -50, "max": -10}, in_range(-50, -10)),
            ("单点区间 min==max", {"min": 7, "max": 7}, lambda r: (r == "7", r)),
            ("跨界区间", {"min": -5, "max": 5}, in_range(-5, 5)),
            ("min>max 自动交换", {"min": 100, "max": 1}, in_range(1, 100)),
            (
                "count=3 返回 3 个",
                {"min": 1, "max": 10, "count": 3},
                lambda r: (len(r.split("、")) == 3, r),
            ),
            (
                "unique 不重复",
                {"min": 1, "max": 10, "count": 5, "unique": True},
                lambda r: (
                    len(set(r.split("、"))) == len(r.split("、")) == 5,
                    r,
                ),
            ),
            (
                "字符串参数（LLM 常见）",
                {"min": "1", "max": "6"},
                in_range(1, 6),
            ),
            (
                "count 超过区间却要求 unique",
                {"min": 1, "max": 3, "count": 5, "unique": True},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "min 非数字",
                {"min": "abc", "max": 10},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "区间跨度过大",
                {"min": 0, "max": 10**15},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "count 超上限",
                {"min": 1, "max": 100, "count": 1000},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "小数 min 报错",
                {"min": 1.5, "max": 10},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
        ],
    )

    # 无偏性：朴素取模在 n=3 时会有明显偏差，验证我们的实现没有
    print()
    print("  无偏性检验（100000 次 min=0,max=2，理想各 1/3 ≈ 33333）")
    counts = collections.Counter()
    for _ in range(100000):
        counts[await tools["random_int"].call(context=None, min=0, max=2)] += 1
    expected = 100000 / 3
    max_dev = max(abs(c - expected) / expected for c in counts.values())
    check(
        "三值分布偏差 < 3%（朴素取模会偏差 >10%）",
        max_dev < 0.03,
        f"分布={dict(counts)} 最大偏差={max_dev:.2%}",
    )

    # -------------------------------------------------------------- random_float
    print()
    print("=" * 70)
    print("第 3 部分：random_float")
    print("=" * 70)
    await run_cases(
        tools["random_float"],
        [
            (
                "默认区间 [0,1)",
                {"min": 0, "max": 1},
                lambda r: (0 <= float(r) < 1, r),
            ),
            (
                "负数区间",
                {"min": -10, "max": -5},
                lambda r: (-10 <= float(r) < -5, r),
            ),
            (
                "precision=2 只保留 2 位",
                {"min": 0, "max": 100, "precision": 2},
                lambda r: (
                    len(r.split(".")[1]) <= 2 if "." in r else True,
                    r,
                ),
            ),
            (
                "precision=0 取整",
                {"min": 0, "max": 10, "precision": 0},
                lambda r: ("." not in r, r),
            ),
            (
                "min==max 报错",
                {"min": 5, "max": 5},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "precision 越界",
                {"min": 0, "max": 1, "precision": 99},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "count=3",
                {"min": 0, "max": 1, "count": 3},
                lambda r: (len(r.split("、")) == 3, r),
            ),
        ],
    )

    # -------------------------------------------------------------------- dice
    print()
    print("=" * 70)
    print("第 4 部分：dice")
    print("=" * 70)

    def dice_total(r):
        # "12（2d6+3，各骰：4+5）" → 12
        return int(r.split("（")[0])

    await run_cases(
        tools["dice"],
        [
            ("1d6 范围 1-6", {"notation": "1d6"}, lambda r: (1 <= dice_total(r) <= 6, r)),
            ("d20 省略个数", {"notation": "d20"}, lambda r: (1 <= dice_total(r) <= 20, r)),
            (
                "2d6+3 范围 5-15",
                {"notation": "2d6+3"},
                lambda r: (5 <= dice_total(r) <= 15, r),
            ),
            (
                "4d6-1 范围 3-23",
                {"notation": "4d6-1"},
                lambda r: (3 <= dice_total(r) <= 23, r),
            ),
            (
                "带空格 2d6 + 3",
                {"notation": "2d6 + 3"},
                lambda r: (5 <= dice_total(r) <= 15, r),
            ),
            (
                "大写 2D6",
                {"notation": "2D6"},
                lambda r: (2 <= dice_total(r) <= 12, r),
            ),
            (
                "不传 notation 默认 1d6",
                {},
                lambda r: (1 <= dice_total(r) <= 6, r),
            ),
            (
                "rolls=5 返回 5 个总点数",
                {"notation": "1d6", "rolls": 5},
                lambda r: (len(r.split("总点数：")[1].split("（")[0].split("、")) == 5, r),
            ),
            (
                "脏记法 abc 报错",
                {"notation": "abc"},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            ("2d1 面数过小", {"notation": "2d1"}, lambda r: (r.startswith("[参数错误]"), r)),
            (
                "骰子总数超上限",
                {"notation": "100d6", "rolls": 100},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
        ],
    )

    # 骰子点数边界覆盖：3d6 应能取到 3 和 18
    seen = set()
    for _ in range(4000):
        seen.add(dice_total(await tools["dice"].call(context=None, notation="3d6")))
    check(
        "3d6 能取到最小 3 与最大 18",
        min(seen) == 3 and max(seen) == 18,
        f"实际范围 {min(seen)}-{max(seen)}",
    )

    # ----------------------------------------------------------- random_choice
    print()
    print("=" * 70)
    print("第 5 部分：random_choice")
    print("=" * 70)
    await run_cases(
        tools["random_choice"],
        [
            (
                "从三选一",
                {"items": ["A", "B", "C"]},
                lambda r: (r in ("A", "B", "C"), r),
            ),
            (
                "真列表多选",
                {"items": ["A", "B", "C", "D"], "count": 2},
                lambda r: (len(r.split("、")) == 2, r),
            ),
            (
                "逗号分隔字符串（LLM 常见）",
                {"items": "苹果,香蕉,橘子"},
                lambda r: (r in ("苹果", "香蕉", "橘子"), r),
            ),
            (
                "JSON 数组字符串",
                {"items": '["x","y","z"]'},
                lambda r: (r in ("x", "y", "z"), r),
            ),
            (
                "unique 不重复",
                {"items": ["A", "B", "C", "D"], "count": 4, "unique": True},
                lambda r: (len(set(r.split("、"))) == 4, r),
            ),
            (
                "允许重复时 count 可超元素数",
                {"items": ["A", "B"], "count": 5, "unique": False},
                lambda r: (len(r.split("、")) == 5, r),
            ),
            (
                "unique 超元素数报错",
                {"items": ["A", "B"], "count": 5, "unique": True},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "dedupe 去重后超量报错",
                {"items": ["A", "A", "B"], "count": 3, "unique": True, "dedupe": True},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "权重抽样（B 权重 0 永不出现）",
                {"items": ["A", "B"], "weights": [1, 0], "count": 10, "unique": False},
                lambda r: (set(r.split("、")) == {"A"}, r),
            ),
            (
                "权重长度不匹配报错",
                {"items": ["A", "B", "C"], "weights": [1, 2]},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "空列表报错",
                {"items": []},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
        ],
    )

    # ----------------------------------------------------------------- shuffle
    print()
    print("=" * 70)
    print("第 6 部分：shuffle")
    print("=" * 70)
    await run_cases(
        tools["shuffle"],
        [
            (
                "元素不增不减",
                {"items": ["A", "B", "C", "D", "E"]},
                lambda r: (sorted(r.split("、")) == ["A", "B", "C", "D", "E"], r),
            ),
            (
                "逗号分隔字符串",
                {"items": "1,2,3,4"},
                lambda r: (sorted(r.split("、")) == ["1", "2", "3", "4"], r),
            ),
            (
                "元素不足两个报错",
                {"items": ["A"]},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
        ],
    )
    # 洗牌确实会变序（5! = 120 种排列，20 次全同的概率可忽略）
    results = {
        await tools["shuffle"].call(context=None, items=["A", "B", "C", "D", "E"])
        for _ in range(20)
    }
    check("多次洗牌结果不总是同一排列", len(results) > 1, f"出现 {len(results)} 种排列")

    # ------------------------------------------------------------ random_string
    print()
    print("=" * 70)
    print("第 7 部分：random_string")
    print("=" * 70)
    await run_cases(
        tools["random_string"],
        [
            ("默认长度 16", {}, lambda r: (len(r) == 16, f"长度 {len(r)}: {r}")),
            (
                "length=8",
                {"length": 8},
                lambda r: (len(r) == 8, r),
            ),
            (
                "digits 只含数字",
                {"length": 20, "charset": "digits"},
                lambda r: (r.isdigit() and len(r) == 20, r),
            ),
            (
                "hex 只含十六进制字符",
                {"length": 32, "charset": "hex"},
                lambda r: (
                    all(c in "0123456789abcdef" for c in r) and len(r) == 32,
                    r,
                ),
            ),
            (
                "upper 只含大写",
                {"length": 12, "charset": "upper"},
                lambda r: (r.isupper() and len(r) == 12, r),
            ),
            (
                "readable 不含易混淆字符",
                {"length": 200, "charset": "readable"},
                lambda r: (not (set(r) & set("0O1lI")), r),
            ),
            (
                "urlsafe 只含 URL 安全字符",
                {"length": 64, "charset": "urlsafe"},
                lambda r: (
                    all(c.isalnum() or c in "-_" for c in r),
                    r,
                ),
            ),
            (
                "count=3 返回 3 个互不相同",
                {"length": 12, "count": 3},
                lambda r: (
                    len(r.split("、")) == 3 and len(set(r.split("、"))) == 3,
                    r,
                ),
            ),
            (
                "未知 charset 报错",
                {"charset": "emoji"},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
            (
                "长度超上限报错",
                {"length": 99999},
                lambda r: (r.startswith("[参数错误]"), r),
            ),
        ],
    )

    # 随机性冒烟：100 个 16 位串不应重复，字符分布应覆盖大小写与数字
    samples = [
        await tools["random_string"].call(context=None, length=16) for _ in range(100)
    ]
    check("100 个随机串互不相同", len(set(samples)) == 100)
    all_chars = set("".join(samples))
    check(
        "字符集覆盖大小写字母与数字",
        any(c.islower() for c in all_chars)
        and any(c.isupper() for c in all_chars)
        and any(c.isdigit() for c in all_chars),
        f"出现字符种类 {len(all_chars)}",
    )
    check("长度全部正确", all(len(s) == 16 for s in samples))

    # ------------------------------------------------------------- secure 后端
    print()
    print("=" * 70)
    print("第 8 部分：secure (CSPRNG) 后端")
    print("=" * 70)
    plugin.set_secure_rng(True)
    check("后端已切到 CSPRNG", plugin.using_secure_rng() is True)
    r = await tools["random_int"].call(context=None, min=1, max=6)
    check("secure 下 random_int 正常", r.isdigit() and 1 <= int(r) <= 6, r)
    r = await tools["random_float"].call(context=None, min=0, max=1)
    check("secure 下 random_float 正常", 0 <= float(r) < 1, r)
    r = await tools["random_string"].call(context=None, length=24, charset="password")
    check("secure 下 random_string 正常", len(r) == 24, r)
    r = await tools["dice"].call(context=None, notation="2d6")
    check("secure 下 dice 正常", 2 <= dice_total(r) <= 12, r)
    r = await tools["random_choice"].call(
        context=None, items=["A", "B", "C", "D"], count=2, unique=True
    )
    check("secure 下 random_choice 正常", len(set(r.split("、"))) == 2, r)
    r = await tools["shuffle"].call(context=None, items=["A", "B", "C", "D"])
    check("secure 下 shuffle 正常", sorted(r.split("、")) == ["A", "B", "C", "D"], r)

    counts = collections.Counter()
    for _ in range(30000):
        counts[await tools["random_int"].call(context=None, min=0, max=2)] += 1
    expected = 30000 / 3
    max_dev = max(abs(c - expected) / expected for c in counts.values())
    check(
        "secure 下分布无偏（偏差 < 5%）",
        max_dev < 0.05,
        f"分布={dict(counts)} 最大偏差={max_dev:.2%}",
    )
    plugin.set_secure_rng(False)

    # ------------------------------------------------------------ 配置项解析
    print()
    print("=" * 70)
    print("第 9 部分：配置项")
    print("=" * 70)
    ctx2 = FakeContext()
    plugin.RandomToolPlugin(ctx2, config={"random_backend": "secure"})
    check("配置 secure 生效", plugin.using_secure_rng() is True)
    ctx3 = FakeContext()
    plugin.RandomToolPlugin(ctx3, config={"random_backend": "不存在的值"})
    check("非法配置回退为 standard", plugin.using_secure_rng() is False)
    ctx4 = FakeContext()
    plugin.RandomToolPlugin(ctx4, config={})
    check("缺省配置为 standard", plugin.using_secure_rng() is False)
    check("重复构造不会重复注册（每次 6 个）", all(len(c.tools) == 6 for c in (ctx2, ctx3, ctx4)))

    # ---------------------------------------------------------------- 收尾
    print()
    print("=" * 70)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 70)
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
