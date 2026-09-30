"""AstrBot 插件：随机数生成工具集（注册为 LLM 工具 / Function Calling）。

向主 AI 提供 6 个随机数相关工具，AI 在对话中按需自动调用：

- random_int     整数区间随机（含负数）
- random_float   随机浮点数（可指定小数位）
- dice           掷骰子，支持 2d6+3 记法
- random_choice  从列表随机挑选（可多选、可选不重复、可选权重、可去重）
- shuffle        随机打乱列表
- random_string  随机字符串/密码/token（默认走 CSPRNG）

随机源后端可配置：
- standard  Python 标准库 random（MT19937，C 实现，约 1600 万次/秒，零依赖）
- secure    secrets 模块（操作系统 CSPRNG，约 360 万次/秒，密码学安全，零依赖）

所有有界整数取值均使用无偏算法（rejection sampling），不使用 `int(u * n)`
这类会引入模偏差的写法。
"""

from __future__ import annotations

import json
import random
import re
import secrets
import string
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

PLUGIN_NAME = "astrbot_plugin_random_tool"
PLUGIN_AUTHOR = "albumsean441-lang"
PLUGIN_DESC = "随机数生成工具集：整数/浮点/骰子/随机挑选/打乱/随机字符串，注册为 LLM 工具供 AI 自动调用"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO = "https://github.com/albumsean441-lang/astrbot_plugin_random_tool"

# ---------------------------------------------------------------- 硬性上限
MAX_INT_RANGE = 10**12  # 整数区间跨度上限，防止 LLM 传入荒谬区间
MAX_LIST_LEN = 1000  # 单次处理的列表长度上限
MAX_RANDOM_STR_LEN = 4096  # 单次生成字符串长度上限
MAX_ROLLS = 100  # 掷骰的"次数"上限
MAX_DICE_PER_ROLL = 100  # 单次掷骰的骰子个数上限
MAX_DICE_TOTAL = 200  # 一次调用所有骰子数量总和上限

# ------------------------------------------------------- 模块级随机源后端
# AstrBot 可能只实例化工具类一次，但配置是插件级共享的，
# 因此用模块级变量承载后端选择，避免每个工具各自持有一份配置。
_SECURE_RNG: bool = False


def set_secure_rng(secure: bool) -> None:
    """切换随机源后端为 CSPRNG（True）或 MT19937（False）。"""
    global _SECURE_RNG
    _SECURE_RNG = bool(secure)


def using_secure_rng() -> bool:
    """当前是否使用 CSPRNG 后端。"""
    return _SECURE_RNG


def _backend_name() -> str:
    return "CSPRNG(secrets)" if _SECURE_RNG else "MT19937(random)"


def rng_below(n: int) -> int:
    """返回 [0, n) 上的无偏均匀整数；n 必须为正。"""
    if n <= 0:
        raise ValueError("rng_below 的上界必须为正整数")
    if _SECURE_RNG:
        return secrets.randbelow(n)  # 内部即 rejection sampling，无偏
    # CPython 的 random._randbelow 使用 getrandbits + 拒绝采样，无偏。
    # 用 getattr 取以兼容不同小版本（该函数自 3.2 起存在于 CPython）。
    randbelow = getattr(random, "_randbelow", None)
    if randbelow is None:  # 理论上不会发生，保底走 randrange（同样无偏）
        return random.randrange(n)
    return randbelow(n)


def rng_choice(seq: list[Any]) -> Any:
    """无偏随机取一个元素。"""
    return seq[rng_below(len(seq))]


def rng_shuffle(seq: list[Any]) -> None:
    """原地 Fisher-Yates 洗牌。"""
    if _SECURE_RNG:
        random.SystemRandom().shuffle(seq)
    else:
        random.shuffle(seq)


def rng_uniform_float() -> float:
    """返回 [0.0, 1.0) 上的均匀浮点数，53 位精度。"""
    if _SECURE_RNG:
        return secrets.randbits(53) / (1 << 53)
    return random.random()


# ------------------------------------------------------------------ 骰子记法
_DICE_RE = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+)\s*([+-]\s*\d+)?\s*$")


def parse_dice_notation(spec: str) -> tuple[int, int, int]:
    """解析骰子记法，如 "2d6+3" → (2, 6, 3)、"d20" → (1, 20, 0)。

    Returns:
        (骰子个数, 每颗面数, 固定加值)

    Raises:
        ValueError: 记法非法或数值越界。
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("骰子记法不能为空，正确格式如 2d6+3、d20、4d6-1")
    m = _DICE_RE.match(spec)
    if not m:
        raise ValueError(f"无法解析骰子记法 {spec!r}，正确格式如 2d6+3、d20、4d6-1")
    count_s, faces_s, mod_s = m.groups()
    count = int(count_s) if count_s else 1
    faces = int(faces_s)
    mod = int(mod_s.replace(" ", "")) if mod_s else 0

    if count < 1:
        raise ValueError("骰子个数至少为 1")
    if count > MAX_DICE_PER_ROLL:
        raise ValueError(f"单次骰子个数不能超过 {MAX_DICE_PER_ROLL}，收到 {count}")
    if faces < 2:
        raise ValueError("骰子面数至少为 2")
    if faces > MAX_INT_RANGE:
        raise ValueError(f"骰子面数不能超过 {MAX_INT_RANGE}，收到 {faces}")
    if abs(mod) > MAX_INT_RANGE:
        raise ValueError(f"加值绝对值不能超过 {MAX_INT_RANGE}，收到 {mod}")
    return count, faces, mod


# ----------------------------------------------------------- 参数规范化工具
def as_int(value: Any, name: str) -> int:
    """把 LLM 传来的参数稳妥地转成 int（兼容字符串与浮点）。"""
    if isinstance(value, bool):
        raise ValueError(f"参数 {name} 必须是整数，收到布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"参数 {name} 必须是整数，收到 {value}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                raise ValueError(f"参数 {name} 必须是整数，收到 {value!r}") from None
            if not f.is_integer():
                raise ValueError(f"参数 {name} 必须是整数，收到 {value!r}")
            return int(f)
    raise ValueError(f"参数 {name} 必须是整数，收到类型 {type(value).__name__}")


def as_float(value: Any, name: str) -> float:
    """把 LLM 传来的参数稳妥地转成 float。"""
    if isinstance(value, bool):
        raise ValueError(f"参数 {name} 必须是数字，收到布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ValueError(f"参数 {name} 必须是数字，收到 {value!r}") from None
    raise ValueError(f"参数 {name} 必须是数字，收到类型 {type(value).__name__}")


def as_str_list(value: Any, name: str) -> list[str]:
    """把 LLM 传来的参数规范成字符串列表。

    兼容三种形态：真正的 list、JSON 数组字符串、逗号/顿号分隔的字符串。
    """
    if value is None:
        raise ValueError(f"参数 {name} 不能为空")
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                value = parsed
            else:
                value = [p for p in re.split(r"[,，、]", text) if p.strip()]
        else:
            value = [p for p in re.split(r"[,，、\n]", text) if p.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"参数 {name} 必须是列表，收到类型 {type(value).__name__}")
    items = [str(item) for item in value]
    if not items:
        raise ValueError(f"参数 {name} 是空列表，至少需要一个元素")
    if len(items) > MAX_LIST_LEN:
        raise ValueError(f"参数 {name} 的元素个数不能超过 {MAX_LIST_LEN}，收到 {len(items)}")
    return items


def as_bool(value: Any, default: bool = False) -> bool:
    """把 LLM 传来的参数稳妥地转成 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "y", "是"):
            return True
        if text in ("false", "0", "no", "n", "否", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def format_number(value: float | int, precision: int = 0) -> str:
    """按指定精度格式化数字，去掉多余的尾随 0。"""
    if precision <= 0:
        return str(int(value))
    text = f"{value:.{precision}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def tool_error(message: str) -> str:
    """统一的错误返回：让 LLM 能读懂并自行修正参数后重试。"""
    return f"[参数错误] {message}"


# 随机字符串预设字符集（模块级常量：pydantic dataclass 不允许可变默认值作为字段）
CHARSETS: dict[str, str] = {
    "alnum": string.ascii_letters + string.digits,
    "alpha": string.ascii_letters,
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "digits": string.digits,
    "hex": "0123456789abcdef",
    "urlsafe": string.ascii_letters + string.digits + "-_",
    "password": string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{};:,.?",
    "readable": "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789",
}


# ==================================================================== 工具
@pydantic_dataclass
class RandomIntTool(FunctionTool[AstrAgentContext]):
    """在闭区间内生成均匀分布的随机整数。"""

    name: str = "random_int"
    description: str = (
        "生成指定区间 [min, max] 内的均匀随机整数（闭区间，两端都可能取到，支持负数）。"
        "何时使用：用户要求「随机数」「随机选个数」「1到100之间随机」等需要单个或多个随机整数时。"
        "返回值：随机整数；count>1 时返回多个，用逗号分隔。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "min": {
                    "type": "integer",
                    "description": "区间下界（含），可为负数，默认 1",
                },
                "max": {
                    "type": "integer",
                    "description": "区间上界（含），必须大于等于 min，默认 100",
                },
                "count": {
                    "type": "integer",
                    "description": "要生成的随机数个数，默认 1，最大 100",
                },
                "unique": {
                    "type": "boolean",
                    "description": "是否要求多个结果互不重复（相当于不重复抽样），默认 false",
                },
            },
            "required": ["min", "max"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            low = as_int(kwargs.get("min", 1), "min")
            high = as_int(kwargs.get("max", 100), "max")
            count = as_int(kwargs.get("count", 1) or 1, "count")
            unique = as_bool(kwargs.get("unique"), False)
        except ValueError as e:
            return tool_error(str(e))

        if low > high:
            low, high = high, low
        span = high - low + 1
        if span > MAX_INT_RANGE:
            return tool_error(
                f"区间跨度（{span}）过大，请控制在 {MAX_INT_RANGE} 以内"
            )
        if count < 1:
            return tool_error("count 至少为 1")
        if count > 100:
            return tool_error("count 不能超过 100")

        if unique:
            if count > span:
                return tool_error(
                    f"要求 {count} 个互不重复的整数，但区间 [low, high] 只有 {span} 个可用值"
                )
            values = random.sample(range(low, high + 1), count)
        else:
            values = [low + rng_below(span) for _ in range(count)]

        if count == 1:
            return str(values[0])
        return "、".join(str(v) for v in values)


@pydantic_dataclass
class RandomFloatTool(FunctionTool[AstrAgentContext]):
    """在区间内生成均匀分布的随机浮点数。"""

    name: str = "random_float"
    description: str = (
        "生成指定区间 [min, max) 内的均匀随机浮点数（支持负数与小数边界）。"
        "何时使用：用户要求随机小数、随机概率、带小数位的随机数值时。"
        "返回值：随机浮点数；precision 指定保留的小数位。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "min": {
                    "type": "number",
                    "description": "区间下界（含），可为负数，默认 0",
                },
                "max": {
                    "type": "number",
                    "description": "区间上界（不含），必须大于 min，默认 1",
                },
                "precision": {
                    "type": "integer",
                    "description": "保留的小数位数，0-12，默认 6",
                },
                "count": {
                    "type": "integer",
                    "description": "要生成的随机数个数，默认 1，最大 100",
                },
            },
            "required": ["min", "max"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            low = as_float(kwargs.get("min", 0), "min")
            high = as_float(kwargs.get("max", 1), "max")
            precision = as_int(kwargs.get("precision", 6) or 0, "precision")
            count = as_int(kwargs.get("count", 1) or 1, "count")
        except ValueError as e:
            return tool_error(str(e))

        if low > high:
            low, high = high, low
        if not (low < high):
            return tool_error("max 必须大于 min，否则区间内无有效取值")
        if precision < 0 or precision > 12:
            return tool_error("precision 必须在 0 到 12 之间")
        if count < 1 or count > 100:
            return tool_error("count 必须在 1 到 100 之间")

        # u ∈ [0,1)，precision==0 时按四舍五入取整整数
        if precision == 0:
            values = [low + rng_uniform_float() * (high - low) for _ in range(count)]
            out = [str(int(round(v))) for v in values]
        else:
            values = [low + rng_uniform_float() * (high - low) for _ in range(count)]
            out = [format_number(v, min(precision, 12)) for v in values]

        if count == 1:
            return out[0]
        return "、".join(out)


@pydantic_dataclass
class DiceTool(FunctionTool[AstrAgentContext]):
    """掷骰子，支持标准 TRPG 记法。"""

    name: str = "dice"
    description: str = (
        "掷骰子，支持标准 TRPG 记法如 2d6+3、d20、4d6-1（NdM±K：N 颗 M 面骰加 K）。"
        "何时使用：用户说「掷骰子」「roll 个 d20」「2d6+3」等桌游/跑团场景。"
        "返回值：总点数；点数明细与每次掷骰结果。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "notation": {
                    "type": "string",
                    "description": "骰子记法，如 2d6+3、d20、4d6-1、3d8；不传时默认 1d6",
                },
                "rolls": {
                    "type": "integer",
                    "description": "掷骰次数，默认 1，最大 100",
                },
            },
            "required": [],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        notation = kwargs.get("notation") or "1d6"
        if not isinstance(notation, str):
            notation = str(notation)
        # 兼容 LLM 传入 "2d6 + 3" 这类带空格的写法
        notation = notation.replace(" ", "")
        try:
            count, faces, mod = parse_dice_notation(notation)
            rolls = as_int(kwargs.get("rolls", 1) or 1, "rolls")
        except ValueError as e:
            return tool_error(str(e))

        if rolls < 1 or rolls > MAX_ROLLS:
            return tool_error(f"rolls 必须在 1 到 {MAX_ROLLS} 之间")
        if count * rolls > MAX_DICE_TOTAL:
            return tool_error(
                f"骰子总数（{count}×{rolls}={count * rolls}）超过上限 {MAX_DICE_TOTAL}，请减少骰子数或掷骰次数"
            )

        totals: list[int] = []
        rolls_detail: list[list[int]] = []
        for _ in range(rolls):
            dice = [1 + rng_below(faces) for _ in range(count)]
            totals.append(sum(dice) + mod)
            rolls_detail.append(dice)

        if rolls == 1:
            if count == 1:
                return f"{totals[0]}（{notation}）"
            dice_text = "+".join(str(d) for d in rolls_detail[0])
            return f"{totals[0]}（{notation}，各骰：{dice_text}）"

        detail_parts = [
            str(total) if count == 1 else f"{total}({'+'.join(map(str, dice))})"
            for total, dice in zip(totals, rolls_detail)
        ]
        mod_text = f"{mod:+d}" if mod else ""
        return (
            f"总点数：{'、'.join(str(t) for t in totals)}"
            f"（{notation}{mod_text}，共 {rolls} 次；明细：{'、'.join(detail_parts)}）"
        )


@pydantic_dataclass
class RandomChoiceTool(FunctionTool[AstrAgentContext]):
    """从候选列表中等概率随机挑选元素。"""

    name: str = "random_choice"
    description: str = (
        "从给定的候选列表中随机挑选一个或多个元素。"
        "何时使用：用户要求「从这些里选一个」「抽奖」「随机点名」「随机挑几个」等场景。"
        "返回值：被选中的元素；多选时用逗号分隔。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "候选元素列表，至少一个",
                },
                "count": {
                    "type": "integer",
                    "description": "要挑选的个数，默认 1",
                },
                "unique": {
                    "type": "boolean",
                    "description": "是否不重复抽取（每个元素最多被选中一次），默认 true",
                },
                "weights": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "可选，与 items 等长的权重列表；不传则等概率",
                },
                "dedupe": {
                    "type": "boolean",
                    "description": "是否先对候选列表去重，默认 false",
                },
            },
            "required": ["items"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            items = as_str_list(kwargs.get("items"), "items")
            count = as_int(kwargs.get("count", 1) or 1, "count")
            unique = as_bool(kwargs.get("unique"), True)
            dedupe = as_bool(kwargs.get("dedupe"), False)
        except ValueError as e:
            return tool_error(str(e))

        if count < 1 or count > MAX_LIST_LEN:
            return tool_error(f"count 必须在 1 到 {MAX_LIST_LEN} 之间")

        if dedupe:
            seen: set[str] = set()
            deduped: list[str] = []
            for item in items:
                if item not in seen:
                    seen.add(item)
                    deduped.append(item)
            items = deduped

        if unique and count > len(items):
            return tool_error(
                f"要求不重复抽取 {count} 个，但候选只有 {len(items)} 个不同元素"
            )

        weights_raw = kwargs.get("weights")
        weights: list[float] | None = None
        if weights_raw not in (None, "", []):
            try:
                if isinstance(weights_raw, str):
                    weights = [as_float(p, "weights") for p in re.split(r"[,，、\s]+", weights_raw.strip()) if p]
                elif isinstance(weights_raw, (list, tuple)):
                    weights = [as_float(w, "weights") for w in weights_raw]
                else:
                    raise ValueError("weights 必须是数字列表")
            except ValueError as e:
                return tool_error(str(e))
            if len(weights) != len(items):
                return tool_error(
                    f"weights 长度（{len(weights)}）必须与 items 长度（{len(items)}）一致"
                )
            if any(w < 0 for w in weights):
                return tool_error("weights 不能包含负数")
            if sum(weights) <= 0:
                return tool_error("weights 之和必须大于 0")

        if weights is None:
            if unique:
                picked = random.sample(items, count)
            else:
                picked = [rng_choice(items) for _ in range(count)]
        else:
            # 加权抽样：无放回时每次抽完把该权重置 0
            pool = list(items)
            pool_weights = list(weights)
            picked = []
            for _ in range(count):
                total_w = sum(pool_weights)
                if total_w <= 0:
                    break
                target = rng_uniform_float() * total_w
                acc = 0.0
                idx = len(pool) - 1
                for i, w in enumerate(pool_weights):
                    acc += w
                    if target < acc:
                        idx = i
                        break
                picked.append(pool[idx])
                if unique:
                    pool_weights[idx] = 0.0

        if not picked:
            return tool_error("没有可抽取的元素（权重之和为 0）")
        if len(picked) == 1:
            return picked[0]
        return "、".join(picked)


@pydantic_dataclass
class ShuffleTool(FunctionTool[AstrAgentContext]):
    """随机打乱给定列表的顺序。"""

    name: str = "shuffle"
    description: str = (
        "随机打乱一个列表的顺序（Fisher-Yates 洗牌，每种排列等概率）。"
        "何时使用：用户要求「打乱顺序」「洗牌」「随机排序」时。"
        "返回值：打乱后的列表，按新顺序用逗号分隔。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待打乱的元素列表，至少两个",
                },
            },
            "required": ["items"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            items = as_str_list(kwargs.get("items"), "items")
        except ValueError as e:
            return tool_error(str(e))

        if len(items) < 2:
            return tool_error("至少需要两个元素才能打乱顺序")
        rng_shuffle(items)
        return "、".join(items)


@pydantic_dataclass
class RandomStringTool(FunctionTool[AstrAgentContext]):
    """生成随机字符串、密码或 token。"""

    name: str = "random_string"
    description: str = (
        "生成随机字符串，可用于随机密码、验证码、token、随机 ID。"
        "何时使用：用户要求「生成个随机密码」「随机字符串」「随机验证码」时。"
        "返回值：生成的随机字符串。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "length": {
                    "type": "integer",
                    "description": "字符串长度，默认 16，最大 4096",
                },
                "charset": {
                    "type": "string",
                    "description": (
                        "字符集预设：alnum=大小写字母+数字（默认）、"
                        "alpha=大小写字母、lower=小写字母、upper=大写字母、"
                        "digits=数字、hex=十六进制、urlsafe=URL 安全字符、"
                        "password=含符号的强密码字符集、"
                        "readable=去掉易混淆字符(0O1lI)的字母数字"
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "生成个数，默认 1，最大 100",
                },
            },
            "required": [],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            length = as_int(kwargs.get("length", 16) or 0, "length")
            count = as_int(kwargs.get("count", 1) or 1, "count")
        except ValueError as e:
            return tool_error(str(e))

        charset_name = str(kwargs.get("charset") or "alnum").strip().lower()
        if charset_name not in CHARSETS:
            return tool_error(
                f"未知字符集 {charset_name!r}，可选：{'、'.join(CHARSETS)}"
            )
        charset = CHARSETS[charset_name]

        if length < 1 or length > MAX_RANDOM_STR_LEN:
            return tool_error(f"length 必须在 1 到 {MAX_RANDOM_STR_LEN} 之间")
        if count < 1 or count > 100:
            return tool_error("count 必须在 1 到 100 之间")

        results = [
            "".join(charset[rng_below(len(charset))] for _ in range(length))
            for _ in range(count)
        ]
        if count == 1:
            return results[0]
        return "、".join(results)


# ================================================================ 插件主体
@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class RandomToolPlugin(Star):
    """随机数生成工具集插件主类。

    在 __init__ 中把 6 个 FunctionTool 注册到 AstrBot，
    AI 在对话中按需自动调用，无需用户输入特定指令。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._apply_config()
        self.context.add_llm_tools(
            RandomIntTool(),
            RandomFloatTool(),
            DiceTool(),
            RandomChoiceTool(),
            ShuffleTool(),
            RandomStringTool(),
        )
        logger.info(
            f"[random_tool] 已注册 6 个 LLM 工具，随机源后端：{_backend_name()}"
        )

    def _apply_config(self) -> None:
        """把插件配置应用到模块级随机源后端。"""
        mode = str(self.config.get("random_backend", "standard")).strip().lower()
        if mode not in ("standard", "secure"):
            logger.warning(
                f"[random_tool] 未知的 random_backend 配置值 {mode!r}，回退为 standard"
            )
            mode = "standard"
        set_secure_rng(mode == "secure")

    async def terminate(self) -> None:
        """插件卸载/重载时调用。"""
        logger.info("[random_tool] 插件已卸载")
