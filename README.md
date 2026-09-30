# astrbot_plugin_random_tool — 随机数生成工具集

为 AstrBot 提供 **6 个随机数 LLM 工具（Function Calling）**。AI 在对话中按需自动调用，
用户不需要记任何指令前缀。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D%204.5.7-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#设计要点)

## 特性

- **6 个工具**：整数区间、随机浮点、掷骰、随机挑选、打乱、随机字符串
- **零第三方依赖**：只用标准库 + AstrBot 自带 API，没有 `requirements.txt` 安装风险
- **取值无偏**：所有有界整数走 rejection sampling，不用 `int(u * n)` 这类有模偏差的写法
- **双随机源**：日常用 MT19937（快），抽奖/发密码可切 CSPRNG（密码学安全）
- **为 LLM 而写**：参数容错 + 错误可自愈 + 上限保护（详见[设计要点](#设计要点)）

## 提供的 6 个工具

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `random_int` | 区间内均匀随机整数（含负数） | `min`、`max`、`count`、`unique` |
| `random_float` | 区间内均匀随机浮点数 | `min`、`max`、`precision`、`count` |
| `dice` | 掷骰，支持 `2d6+3` 记法 | `notation`、`rolls` |
| `random_choice` | 从列表随机挑选 | `items`、`count`、`unique`、`weights`、`dedupe` |
| `shuffle` | 随机打乱列表（Fisher-Yates） | `items` |
| `random_string` | 随机密码 / token / 验证码 | `length`、`charset`、`count` |

实际返回示例：

```
random_int  1..100          -> 1
random_int  5个不重复 1..50   -> 44、22、41、5、7
random_int  -50..-10        -> -40
random_float 0..1 精度4      -> 0.1207
dice 2d6+3                  -> 13（2d6+3，各骰：4+6）
dice 3d8 x4次                -> 总点数：11、14、13、9（3d8，共 4 次；明细：11(1+5+5)、…）
random_choice 三选一          -> 绿
random_choice 5选3不重复       -> 丁、甲、乙
shuffle 5个                  -> E、A、C、B、D
random_string 密码20          -> Mok4MAc:d4%CARCIFV9w
```

`random_string` 支持 9 种字符集预设：

| 预设 | 字符集 |
|---|---|
| `alnum`（默认） | 大小写字母 + 数字 |
| `alpha` / `lower` / `upper` | 字母 / 小写 / 大写 |
| `digits` | 数字（适合验证码） |
| `hex` | 十六进制 |
| `urlsafe` | 字母数字 + `-_` |
| `password` | 含符号的强密码字符集 |
| `readable` | 去掉易混淆字符 `0O1lI` |

## 安装

**方式一：插件市场**（需仓库为公开）

在 AstrBot WebUI 的「插件市场」搜索 `random_tool` 安装。

**方式二：手动**

把本目录整体放到 AstrBot 的 `data/plugins/` 下，重启（或热重载）AstrBot。
启动日志应出现：

```
plugin(...) added LLM tool: random_int
...
[random_tool] 已注册 6 个 LLM 工具，随机源后端：MT19937(random)
```

装好后在 WebUI 的「工具使用」页可以看到这 6 个工具并单独开关。

## 配置

在 WebUI 的插件配置页修改：

| 配置项 | 取值 | 说明 |
|---|---|---|
| `random_backend` | `standard`（默认） | 标准库 `random`（MT19937，C 实现），约 1640 万次/秒，适合日常随机数 |
| | `secure` | `secrets`（操作系统 CSPRNG），约 360 万次/秒，密码学安全，适合抽奖、发密码、防预测 |

改完需重载插件生效。两者**取值均无偏**：所有有界整数都走 rejection sampling
（`secrets.randbelow` / `random._randbelow`），没有使用 `int(u * n)` 这类会引入模偏差的写法。

## 设计要点

- **零第三方依赖**：只用标准库 + AstrBot 自带 API，无 `requirements.txt` 安装风险。
- **参数容错**：LLM 常把整数传成字符串、把数组传成 `"a,b,c"` 或 `'["a","b"]'`，
  插件统一做规范化，避免因类型不符直接报错。
- **错误可自愈**：非法参数不抛异常，而是返回 `[参数错误] …` 文本，
  LLM 能读懂并自行修正后重试。例如传了个 10¹⁵ 的区间会得到：
  `[参数错误] 区间跨度（1000000000000001）过大，请控制在 1000000000000 以内`
- **有上限保护**：区间跨度 10¹²、列表 1000 项、字符串 4096 字符、
  单次骰子总数 200，防止 LLM 构造超大请求耗尽内存。
- **中文友好**：候选列表支持全角逗号 `，` 和顿号 `、` 分隔。

## 为什么用标准库而不是 xoshiro / wyrand

这是实测结论（容器内 Python 3.12.13，AMD Ryzen 7 255）：

| 算法 | ns/次 | 每秒次数 | 依赖 |
|---|---|---|---|
| numpy PCG64（批量） | 2.1 | 4.7 亿 | numpy |
| MT19937 `random.getrandbits(64)` | **61.0** | 1640 万 | 无 |
| `secrets.randbits`（CSPRNG） | 277.4 | 361 万 | 无 |
| wyrand | 245.0 | 408 万 | 纯 Python |
| xorshift64\* | 297.2 | 337 万 | 纯 Python |
| xoshiro256\*\* | 606.0 | 165 万 | 纯 Python |

**纯 Python 手写的"快速"算法全都比标准库慢 4–10 倍**——因为 CPython 每行字节码都有解释器
开销，而 `getrandbits` 是 Mersenne Twister 的 C 实现，一次调用就出 64 位。xoshiro 与 wyrand
只在 C/Rust 里才快。

统计质量方面也有坑：广泛的独立测试显示，被大量采用的 xorshift128+ / xoroshiro128+
在 TestU01 BigCrush 的 MatrixRank、LinearComp 上会失败（线性运算的固有弱点），
而 PCG 与 splitmix64 能通过。参考
[Lemire 的独立测试](https://lemire.me/blog/2017/08/22/testing-non-cryptographic-random-number-generators-my-results/)。

## 开发与验证

`_dev/` 目录放了开发期脚本，可用于复现结论：

| 脚本 | 用途 |
|---|---|
| `bench.py` | 各种 PRNG 算法在本机的实测基准 |
| `verify_plugin.py` | 93 项功能测试（含无偏性检验、两种后端、全部错误分支） |
| `setup_sandbox.py` + `run_sandbox.sh` | 用独立 `ASTRBOT_ROOT` 搭隔离沙箱，不影响线上实例 |
| `check_registry.py` | 经真实 `add_llm_tools` → `FunctionToolManager` 路径确认注册结果 |
| `show_output.py` | 抽查工具实际返回给 LLM 的文本格式 |
| `upload_to_github.py` | 经 GitHub REST API 提交文件（本机 git 走不通时的替代方案） |

验证记录：

1. **沙箱验证**（隔离 `ASTRBOT_ROOT`，消息平台已禁用）：插件正常加载，6 个工具全部注册，
   93 项功能测试全部通过。沙箱还抓出并修复了一个真实缺陷：`CHARSETS` 作为 pydantic
   dataclass 字段会因可变默认值导致插件导入失败。
2. **线上验证**：经 AstrBot 真实的 `add_llm_tools` → `FunctionToolManager` 路径注册，
   6/6 工具 `active=True`、JSON Schema 通过 `ToolSchema` 校验器。

## 兼容性

- AstrBot **>= 4.5.7**（使用 `context.add_llm_tools()` 与 `FunctionTool[AstrAgentContext]`）
- 开发与验证环境：AstrBot 4.28.2 / Python 3.12.13

## License

[MIT](LICENSE)
