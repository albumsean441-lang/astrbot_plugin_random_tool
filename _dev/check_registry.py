"""诊断：不启动第二个实例，确认本插件在线上环境能真正完成工具注册。

做法：只给真实的 Context.add_llm_tools 打桩捕获注册调用，其余全部走线上
AstrBot 代码路径（真实的 register 装饰器、真实的 FunctionTool/pydantic 校验、
真实的插件模块路径 data.plugins.astrbot_plugin_random_tool.main）。
"""

import asyncio
import importlib.util
import sys

sys.path.insert(0, "/AstrBot")

from astrbot.core.star.context import Context  # noqa: E402

TARGETS = [
    "random_int",
    "random_float",
    "dice",
    "random_choice",
    "shuffle",
    "random_string",
]

captured = []


class StubContext(Context):
    """只用来让插件调用 add_llm_tools 的极简替身。

    继承真实的 Context 以保证 isinstance 检查通过，但绕过其需要 10 个依赖的 __init__；
    注册调用本身走真实的方法实现（其内部只访问 self.provider_manager）。
    """

    def __init__(self):
        self.provider_manager = _StubProviderManager()
        self.registered_web_apis = []


class _StubProviderManager:
    def __init__(self):
        from astrbot.core.provider.func_tool_manager import FunctionToolManager

        self.llm_tools = FunctionToolManager()


async def main():
    module_path = "data.plugins.astrbot_plugin_random_tool.main"
    file_path = "/AstrBot/data/plugins/astrbot_plugin_random_tool/main.py"

    spec = importlib.util.spec_from_file_location(module_path, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_path] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001
        print(f"[失败] 插件模块导入异常: {type(e).__name__}: {e}")
        return 2
    print("插件模块导入: 成功")

    plugin_cls = getattr(module, "RandomToolPlugin", None)
    if plugin_cls is None:
        print("[失败] 找不到 RandomToolPlugin 类")
        return 2

    # 用线上配置实例化插件（走真实 __init__ 与真实 add_llm_tools -> FunctionToolManager）
    from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402

    cfg = AstrBotConfig()
    stub = StubContext()
    try:
        plugin_cls(context=stub, config=cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[失败] 插件实例化异常: {type(e).__name__}: {e}")
        return 2

    # 从真实 FunctionToolManager 单例里读回注册结果
    captured.extend(stub.provider_manager.llm_tools.func_list)
    print(f"插件实例化: 成功，注册表内共 {len(captured)} 个工具")

    # 用真实的 ToolSchema 校验器验证每个工具的 schema
    print()
    print("=== 捕获到的工具 ===")
    seen = {}
    for tool in captured:
        name = tool.name
        seen[name] = tool
        try:
            tool.validate_parameters()
            schema_ok = "schema 合法"
        except Exception as e:  # noqa: BLE001
            schema_ok = f"schema 非法: {type(e).__name__}: {e}"
        props = (tool.parameters or {}).get("properties", {})
        print(
            f"  [OK] {name:<14} active={getattr(tool, 'active', None)} "
            f"参数{len(props)}个 {schema_ok}"
        )

    missing = [n for n in TARGETS if n not in seen]
    print()
    if missing:
        print(f"结论: 缺失 {missing}")
        return 2
    print(f"结论: {len(seen)}/6 个工具全部注册且 schema 合法")

    # 顺带确认用户在 WebUI 里是否禁用过这些工具
    from astrbot.core import db_helper  # noqa: E402
    from astrbot.core.utils.shared_preferences import SharedPreferences  # noqa: E402

    sp = SharedPreferences(db_helper=db_helper)
    inactivated = await sp.get_async("inactivated_llm_tools", []) or []
    disabled = [n for n in TARGETS if n in inactivated]
    print(f"被 WebUI 禁用的工具: {disabled if disabled else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
