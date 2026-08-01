# Arsenal Transfer Alert

[English](README.md) · [Arsenal 默认版详细运维手册](docs/OPERATIONS.zh-CN.md)

一个面向球迷的低成本自托管转会提醒服务。它只读取你选定的可信 X 账号，只保留男子一线队
转会中有实质推进的内容，按配置翻译后通过 Bark 推送。

仓库开箱仍是完整的 Arsenal 版；底层现已按俱乐部参数化。要关注另一支球队，只需修改一个
TOML 文件，不需要改 Python 代码。

![Arsenal Transfer Alert 工作流程](docs/assets/arsenal-transfer-alert-flow.svg)

## 它解决什么问题

```text
可信 X 账号 → SQLite 可靠入库 → DeepSeek 严格筛选 → Bark 通知
                         ↓
                  去重、预算保护、健康检查
```

- 只通过 X 官方 API 轮询配置中的 Tier 0–2 账号；
- 过滤纯转发、二手转述、模糊兴趣、文章宣传、普通队务和没有新进展的转会讨论；
- 保留一手报道、独立确认和有实质新增的报道；
- 忠实保留措辞强度，不把“谈判”翻译成“达成协议”；
- 先入库再推进游标，重启后仍可去重和补查；
- 模型输出异常或 Bark 投递结果不确定时失败关闭；
- 记录付费 API 估算用量，并执行本地预算上限。

## 快速体验 Arsenal 默认版

需要 Python 3.11+；程序运行时没有第三方 Python 依赖。

```powershell
git clone https://github.com/nine19een/arsenal-transfer-alert.git
cd arsenal-transfer-alert
python -m pip install -e .
Copy-Item .env.example .env
arsenal-transfer-alert doctor
arsenal-transfer-alert run --once --dry-run
```

默认使用本地模拟数据，不会调用付费 API，也不会真的发送 Bark。仓库中的
[`config/sources.toml`](config/sources.toml) 始终保留为现成的 Arsenal 版：Arsenal 查询词、
简体中文、北京时间、Arsenal 通知分组/ID，以及 12 个启用且已核验的消息源。

## 换成你的球队

先复制精简模板：

```powershell
Copy-Item config/sources.example.toml config/my-club.toml
```

俱乐部只有 3 个必填项：

```toml
[club]
key = "liverpool"
name = "Liverpool"
query_terms = ["Liverpool FC", "#LFC"]
```

每个消息源只有 5 个基础必填项：

```toml
[[sources]]
key = "trusted_reporter"
name = "Trusted Reporter"
tier = 1
username = "TrustedReporter"
query_mode = "topic" # 俱乐部专门账号可用 "all"
```

换球队时应使用独立数据库，并把 Bark 分组覆盖留空，让 TOML 中的配置生效：

```dotenv
SOURCE_CONFIG_PATH=config/my-club.toml
DB_PATH=data/my-club-alert.sqlite3
BARK_GROUP=
```

然后运行 `arsenal-transfer-alert doctor`。输出语言、时区、显示标签、通知图标、分组和 ID
前缀都是可选项，已在 [`config/sources.example.toml`](config/sources.example.toml) 内逐项注释。

正式轮询前，每个启用账号还必须补齐 X 数字用户 ID、核验日期，并由你人工判断账号身份和
消息可靠度。程序不会让模型擅自推断这些内容——Tier 和账号归属本来就应该由使用者负责。

## 正式运行

正式模式需要你自己的 X API、DeepSeek 和 Bark 凭据。复制 `.env.example`，重新核对当时的
官方价格，设置预算上限，完成人工消息源审核，再打开任何付费或发送开关。

关键安全开关是显式的：

```dotenv
APP_MODE=live
PAID_API_CALLS_ENABLED=true
DRY_RUN=true
BARK_SEND_ENABLED=false
```

先用 `verify-sources --allow-paid-call` 查询数字 ID，人工审阅报告并把确认值写回 TOML；再做
真实数据 dry-run。只有确认结果无误后，才同时设置 `DRY_RUN=false` 和
`BARK_SEND_ENABLED=true`。

项目暂不提供“一键部署”。这是有意的边界：基础配置已经足够简单，但付费凭据、消息源
可信度、预算、SQLite 持久化和通知行为仍应让部署者看得见并亲自确认。

## 验证

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

测试覆盖 Arsenal 默认快照、第二支球队、v1 兼容、查询注入拦截、转会范围/一手性门、去重、
崩溃恢复、Bark 不确定投递、成本保护和仓库密钥扫描。

## 更多文档

- [Arsenal 默认版详细运维手册](docs/OPERATIONS.zh-CN.md)
- [API 调研](docs/API_RESEARCH.md)
- [测试与真实链路验证](docs/TEST_RESULTS.md)
- [MIT License](LICENSE)

这是独立球迷项目，与 Arsenal、X、DeepSeek 或 Bark 均无隶属关系。
