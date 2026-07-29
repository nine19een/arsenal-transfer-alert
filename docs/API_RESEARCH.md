# 官方 API 调研记录

核对日期：2026-07-29（UTC+8）

本文件记录实现依据，不是永久价格承诺。正式运行前应重新打开链接和 Developer Console。

## X API

采用 `GET /2/tweets/search/recent`，而不是网页抓取、第三方资讯源或账号 Home Timeline。

依据：

- [Recent Search 介绍](https://docs.x.com/x-api/posts/search/introduction)：所有开发者可读取
  最近 7 天；最多 100 Posts/请求；支持 `from:` 和 `-is:retweet`。
- [Recent Search API](https://docs.x.com/x-api/posts/search-recent-posts)：支持
  `start_time`、`since_id`、`next_token`、`tweet.fields`；`max_results` 为 10–100。
- [官方分页说明](https://docs.x.com/x-api/posts/search/integrate/paginate)：轮询时保留第一页
  `newest_id`，完成所有 `next_token` 分页后把它作为下一轮 `since_id`。
- [官方 Rate Limits](https://docs.x.com/x-api/fundamentals/rate-limits)：Recent Search 提供足够
  的 app-only 限流空间；应用仍自行限制为 130 请求/小时。

选择理由：

- 60 秒轮询通常可把总延迟控制在两分钟内；
- Recent Search 的 7 天窗口支持服务重启补查；
- `from:` 白名单和主题词在 X 端先减少高流量媒体账号的付费读取；
- 纯 repost 由 `-is:retweet` 和本地 `referenced_tweets.type=retweeted` 双重拒绝；
- Replies 和 Quote Posts 没有被查询排除，DeepSeek 以作者自己的正文为判断主体；
- `tweet.fields` 包含 `entities`，仅使用 X API 已返回的 `expanded_url`/`unwound_url`
  规范化外部文章链接和生成原始报道指纹，不访问或抓取文章网页。

没有采用 Filtered Stream，是因为仍需 Recent Search 做停机补查；单独维护两套实时数据路径会
增加故障面，60 秒轮询已经满足目标。

### 计费快照

[X 官方 pay-per-usage 定价](https://docs.x.com/x-api/getting-started/pricing) 在核对日显示：

- Post Read：`$0.005/返回资源`；
- User Read：`$0.010/返回资源`；
- 同一资源在 24 小时 UTC 日窗口通常去重计费，官方说明这是软保证；
- Console 可设置每 billing cycle 的 Spending limit；
- 价格可能变化，Console 和官方页面是实时权威。

因此价格和核对日期全部放在环境变量；live 模式拒绝使用超过 7 天的 X 价格快照。

## DeepSeek API

采用 OpenAI-compatible Base URL 和 `POST /chat/completions`。

核对日官方资料：

- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)：
  `deepseek-v4-flash` 与 `deepseek-v4-pro`；Flash 输入/输出更便宜；两者都支持非思考和
  JSON Output。
- [GET /models](https://api-docs.deepseek.com/api/list-models/) 示例返回
  `deepseek-v4-flash`、`deepseek-v4-pro`。
- [Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/)：
  `thinking.type=disabled` 切换非思考模式；`response_format.type=json_object` 启用 JSON。
- [JSON Output](https://api-docs.deepseek.com/guides/json_mode/)：提示词必须出现 JSON 并给
  格式示例；仍可能偶发空内容。

当前 `.env.example` 的价格快照：

- Flash input cache hit：`$0.0028 / 1M tokens`；
- Flash input cache miss：`$0.14 / 1M tokens`；
- Flash output：`$0.28 / 1M tokens`。

live 模式要求 Base URL、Model ID、Key、价格和核对日期均来自环境变量。启动时再调用
`GET /models`，避免硬编码一个已经下线或不存在的模型。

## Bark

采用官方项目说明中的 JSON `POST /push`：

- [Bark tutorial](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)
- [bark-server API V2](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)

请求字段包括 `device_key`、`title`、`body`、`url`、`group`、`level` 和稳定 `id`。官方
说明相同 `id` 会更新相应通知（需要 Bark 1.5.2 / bark-server 2.2.5+），但没有提供一个
服务器端“同一请求只执行一次”的事务幂等承诺。

因此：

- HTTP 429/5xx 是明确未成功响应，可安全退避重试；
- 网络超时/重置、HTTP 2xx 后响应无法确认等结果标记为 `uncertain`；
- `uncertain` 不自动重试，需要用户先检查手机并人工决定。
