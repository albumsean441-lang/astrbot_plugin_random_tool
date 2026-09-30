"""在 AstrBot 容器内搭建一个隔离的沙箱运行根目录。

不触碰 /AstrBot/data（线上实例数据），全部操作在 /tmp/random_tool_sandbox 下完成。
"""

import json
import os
import shutil
import sys

LIVE_ROOT = "/AstrBot"
SANDBOX_ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/random_tool_sandbox"
SANDBOX_DATA = os.path.join(SANDBOX_ROOT, "data")


def log(msg):
    print(f"[sandbox] {msg}")


def main():
    if os.path.exists(SANDBOX_ROOT):
        shutil.rmtree(SANDBOX_ROOT)
    os.makedirs(SANDBOX_DATA, exist_ok=True)

    # 1. 复制配置并禁用所有消息平台适配器，避免与线上实例争抢 QQ/NapCat 连接
    src_cfg = os.path.join(LIVE_ROOT, "data", "cmd_config.json")
    with open(src_cfg, encoding="utf-8-sig") as f:
        cfg = json.load(f)

    platforms = cfg.get("platform", [])
    disabled = []
    for p in platforms:
        if isinstance(p, dict) and p.get("enable"):
            p["enable"] = False
            disabled.append(p.get("id") or p.get("type"))
    log(f"已禁用平台适配器（防止与线上实例双登录）：{disabled}")

    # 沙箱里不连任何外部服务，降低启动耗时
    cfg["log_level"] = "INFO"
    with open(os.path.join(SANDBOX_DATA, "cmd_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 2. 前端静态资源（沙箱根下同样需要 data/dist）
    src_dist = os.path.join(LIVE_ROOT, "data", "dist")
    if os.path.isdir(src_dist):
        shutil.copytree(src_dist, os.path.join(SANDBOX_DATA, "dist"))
        log("已复制 data/dist")

    # 3. 插件目录只放待验证的插件，避免无关插件的依赖问题干扰判断
    os.makedirs(os.path.join(SANDBOX_DATA, "plugins"), exist_ok=True)
    log(f"沙箱根目录：{SANDBOX_ROOT}")
    log("准备完成")


if __name__ == "__main__":
    main()
