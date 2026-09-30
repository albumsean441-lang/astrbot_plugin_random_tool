#!/bin/sh
# 在隔离的 ASTRBOT_ROOT 下启动一次沙箱 AstrBot 实例并记录日志
export ASTRBOT_ROOT=/tmp/random_tool_sandbox
cd /AstrBot || exit 1
timeout 75 python main.py > /tmp/sandbox_run.log 2>&1
echo "exit=$?"
