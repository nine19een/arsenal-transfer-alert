# 验证结果

- 验证日期：2026-08-01
- 环境：Windows / Python 3.14.4
- 本轮离线测试外部调用：0 次
- 受控集成验证：已分别完成真实 X Recent Search、DeepSeek 模型/分类和单次 Bark UTF-8
  通知验证；凭据、完整请求 URL 和运行数据库均未进入仓库

## 自动化测试

命令：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

结果：

```text
Ran 51 tests in < 3s
OK
```

通过的行为包括：

- Tier 1 原创转会、普通新闻、纯 repost、实质性引用帖；
- Post ID 永久去重、不同记者不做转会事件合并；
- X 同一逻辑 Post 的编辑版本使用 `edit_history_tweet_ids` 去重；较早版本已进入通知表后，
  后续版本在当前进程和重启后都不会再次调用 DeepSeek 或 Bark；
- 原始报道指纹优先使用被引用 Post ID，其次使用去除追踪参数的规范化文章 URL；
- 同一文章的重复分发在当前进程及重启后都被抑制，独立确认和实质性新增仍可分别通知；
- The Athletic 自有独家、gunnerblog 纯 Repost、无新增转述、独立确认和新增报价细节的
  A–E 场景全部通过；
- 重启后游标补查和旧通知去重；
- DeepSeek 无效输出失败关闭；
- Bark 明确临时失败安全重试、不确定结果停止重试；
- Bark JSON 使用 UTF-8 bytes，中文、红白 Emoji、英文和换行逐字往返；
- X 分页、最坏页成本预授权、每 UTC 日资源计数；
- X 部分响应保留已见 Post 并拒绝把该页当作完整进度；
- 免费模拟源不会污染真实 X 调用量或费用统计；
- 查询配置变化后的首次失败不会误沿用旧查询游标；
- DeepSeek V4 Flash 请求显式关闭 thinking 并使用 JSON Output；
- DeepSeek 输出必须声明 Arsenal 当前交易参与角色，本地拒绝
  `eligible=true` 与 `arsenal_participation=none` 的矛盾结果；
- DeepSeek 还必须声明 `arsenal_scope_eligible` 和严格枚举 `news_origin`；转述、评论及
  来源不明均失败关闭，白名单身份本身不会绕过一手性门；
- 富安健洋等前 Arsenal 球员在其他俱乐部间转会、普通前球员转会及仅有二次转会分成会被
  过滤；明确回归 Arsenal、现役球员离队、Arsenal 引援及 Arsenal 所有权下的外租球员变化
  可保留；
- 已完成转会后的感谢、欢迎、评价或比较即使出现 departure/signing 等词，只要没有新的
  交易事实仍会作为背景评论过滤；Post 本身的官宣或新进展不受该规则误伤；
- 仅有球员喜好、尚未触发的条件性兴趣或“免费阅读/我们听到了什么”文章宣传不再算转会
  进展；当前积极追求、接触、谈判或近期明确状态变化仍可通过；
- 官方 X 数字 ID/用户名定期复核和不匹配熔断；
- 长期运行时价格核对日期过期会阻止继续付费读取；
- Tier 0–2 配置边界、`.env.example` 空凭据、日志脱敏和仓库密钥扫描。
- 13 条来源目录记录均有固定数字 ID 和 `verified` 状态；归档禁用的 Guardian Sport 也保留
  已验证身份，12 条启用来源全部 live-ready。

## 其他验证

- `python -m compileall -q src tests`：通过；
- `python -m arsenal_alert --env-file .env.example doctor`：配置结构有效、来源目录
  live-ready，且模拟模式、Dry-run、付费调用和 Bark 发送安全开关均保持关闭；
- 免费模拟 Dry-run：读取 9 条模拟 Post，生成 3 条通知，过滤普通训练、女足、宣传、纯
  repost 和与 Arsenal 无关的前球员转会，无效模型结果没有生成通知；
- `compose.yaml`：使用 PyYAML 6.0.3 成功解析；
- `git diff --check`：通过（仅有 Windows 未来可能转换 CRLF 的提示）。

## 受控真实链路验证

- X：使用独立 SQLite 数据库执行单轮 Recent Search，确认固定数字 author ID 查询、
  `-is:retweet`、主题条件、调用量和费用账本按预期工作；
- DeepSeek：先用 `GET /models` 验证 Key 和 `deepseek-v4-flash`，再在明确费用上限内验证
  `thinking=disabled`、JSON Output、当前 Arsenal 参与方门和一手性门；
- DeepSeek 进展门：用 2026-08-01 的两条真实入库原文做隔离复测；Gunnerblog 的“喜欢球员
  + 尚未触发的续约条件 + 免费文章链接”返回 `promotion_or_link_only`，Ornstein 的“当前
  全力争取 + 球员有意 + 未来几天续约谈判”仍返回 `transfer_update`；两次合计估算
  `$0.00069818`，未调用 X 或 Bark；
- Bark：最多一次 HTTP 请求验证生产 JSON POST，确认 `🔴⚪`、中文、英文和换行以 UTF-8
  原样送达；测试后安全开关保持原值；
- 本地长运行：健康接口持续返回 `live=true`、`ready=true`，轮询成功且无健康告警。

当前机器没有安装 Docker CLI，因此未在本机实际构建镜像；Dockerfile 和 Compose 文件已做
静态/语法检查，仍建议在目标服务器首次上线前运行 `docker compose build` 和模拟模式健康检查。
