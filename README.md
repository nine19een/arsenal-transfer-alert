# Arsenal Transfer Alert

一个可长期运行的个人后台服务：仅通过 X 官方 API 读取固定 r/Gunners Tier 0–2
消息源，由 DeepSeek V4 Flash 的非思考模式筛选阿森纳男子一线队转会消息并忠实翻译，
再通过 Bark 推送到 iPhone。

项目没有网页前端。运行时只需要 Python 3.11+ 和一个可持久化的 SQLite 文件；Python
运行时没有第三方依赖。

> 当前交付状态：完整链路、测试、容器和运维文件已经完成。13 条来源目录记录的官方 X
> 数字用户 ID 均已复核；其中 12 条参与实时轮询，已归档的 Guardian Sport
> 保留记录但禁用。The Athletic 足球账号已经用户确认。项目已完成受控的 X、DeepSeek 和
> Bark 集成验证，可使用本地 `.env` 进入正式模式；仓库不包含任何真实凭据。

## 设计摘要

```text
X Recent Search（60 秒轮询，7 天补查）
        │
        ▼
先写 SQLite ──► 再提交 since_id 游标
        │
        ├─ 数字 author_id 白名单校验
        ├─ 纯 repost 硬过滤
        ├─ X 编辑链逻辑 Post 去重
        ▼
DeepSeek V4 Flash（thinking=disabled，JSON Output）
        │
        ├─ Arsenal 当前男子一线队转会范围门
        ├─ 一手性门：首发/独立确认/实质性新增
        ├─ 本地精确字段、类型、枚举和条件校验
        └─ 无效或异常：失败关闭，不推送
        ▼
同一原始报道指纹去重（引用 Post ID 优先，其次规范化文章 URL）
        │
        ▼
Bark JSON POST（稳定 id=arsenal-transfer-{Post ID}）
        │
        ├─ 明确 429/5xx：安全退避重试
        └─ 网络结果不确定：停止自动重试并报警
```

关键可靠性选择：

- X 每一页先持久化，全部分页成功后才推进游标。若中途崩溃，下次会重新读取但不会重复处理。
- `posts.post_id` 和 `notifications.post_id` 都是数据库主键；同一 X Post ID 永远只有一条通知。
- X 编辑会产生新的 Post ID；程序使用官方 `edit_history_tweet_ids` 识别同一逻辑 Post。只要
  较早版本已经进入通知表，后续编辑版本就会在调用 DeepSeek 和 Bark 前持久化抑制，重启后
  仍然有效。
- 不做转会事件级合并。Ornstein 和 Romano 各自独立报道同一交易时仍分别处理。
- DeepSeek 以白名单作者自己的正文为判断主体。数据库中已见的被引用 Post 正文只作为“是否
  新增事实”的对照材料，不会当作当前作者亲自报道的事实。
- DeepSeek 必须先返回 Arsenal 在当前事件中的固定参与角色：
  `buyer/recruiting_club`、`seller/current_club`、`contract_party`、`loan_owner` 或
  `none`。本地严格校验禁止 `arsenal_participation=none` 的结果进入通知队列。
- “前 Arsenal 球员”、履历背景、旧闻、比较以及仅有二次转会分成等间接关系都不算 Arsenal
  参与当前交易；只有明确重新加盟 Arsenal 才可按引援处理。
- 已完成转会后的感谢、欢迎、评价、表现讨论或球员比较不算新的转会事实；只有 Post 本身
  官宣、确认交易，或补充新的状态、时间、条款或后续影响时才继续判断。
- “转会相关”不等于“转会进展”。仅有喜欢、欣赏、关注、观察名单或“如果球员不续约，
  我们就会参与”一类尚未触发的条件性意向，不得通知；必须出现当前积极追求、接触、询价、
  报价、谈判、协议、体检、近期已安排决定或交易状态/条款变化等实质新进展。
- “免费阅读”“我们听到了什么”、文章标题、播客或链接宣传不能借助作者 Tier、共同署名或
  外链内容补足正文中不存在的进展；宣传正文自身没有上述实质变化时按
  `promotion_or_link_only` 或 `no_new_facts` 过滤。
- 白名单身份不等于一手报道。模型必须返回 `first_hand_report`、
  `independent_confirmation`、`substantive_new_detail`、`attributed_relay`、
  `commentary_only` 或 `unclear_origin`；只有前三种可能进入通知。
- 原始报道指纹优先采用被引用 Post ID，其次采用 X API `entities` 中的规范化外部文章
  URL。SQLite 永久保存已接受的指纹；同一文章的重复分发会被抑制，独立确认和实质性新增
  则可单独通知。不会用球员姓名、事件关键词或模型摘要做广义去重。
- 服务启动时把中断在 `sending` 的 Bark 请求改为 `uncertain`，不擅自重发。
- X 中断超过 7 天时健康状态变为 critical，因为 Recent Search 无法保证补回更早缺口。

## 目录

```text
.
├─ config/sources.toml              # 唯一消息源/Tier 配置
├─ fixtures/                        # 免费模拟 X 数据和模型结果
├─ src/arsenal_alert/
│  ├─ x_api.py                      # X Recent Search、分页、重试
│  ├─ deepseek.py                   # 非思考 JSON 分类和翻译
│  ├─ origin.py                     # 原始报道指纹和文章 URL 规范化
│  ├─ bark.py                       # Bark 投递语义
│  ├─ db.py                         # SQLite 状态、去重和用量账本
│  ├─ identity.py                   # 官方 X 数字 ID 定期复核
│  ├─ pipeline.py                   # 处理状态机
│  └─ health.py                     # 健康检查和成本指标
├─ tests/                           # 标准库 unittest
├─ Dockerfile
├─ compose.yaml
└─ .env.example
```

## 固定消息源

Tier 由 `config/sources.toml` 静态提供，DeepSeek 看不到修改 Tier 的入口。配置只接受 Tier
0、1、2。

| Tier | 配置名称 | X 用户名 | 数字 user_id | 查询方式 | 当前状态 |
|---:|---|---|---:|---|---|
| 0 | Arsenal Official | `@Arsenal` | `34613288` | 除 repost 外全部 | 已验证、启用 |
| 1 | David Ornstein | `@David_Ornstein` | `46875124` | Arsenal 主题候选 | 已验证、启用 |
| 1 | BBC Sport | `@BBCSport` | `265902729` | Arsenal 主题候选 | 已验证、启用 |
| 1 | Sami Mokbel | `@SamiMokbel_BBC` | `193221420` | Arsenal 主题候选 | 已验证、启用 |
| 1 | Fabrizio Romano | `@FabrizioRomano` | `330262748` | Arsenal 主题候选 | 已验证、启用 |
| 2 | Charles Watts | `@charles_watts` | `305734622` | 除 repost 外全部 | 已验证、启用 |
| 2 | Amy Lawrence | `@amylawrence71` | `957528097` | 除 repost 外全部 | 已验证、启用 |
| 2 | James McNicholas / Gunnerblog | `@gunnerblog` | `14016912` | 除 repost 外全部 | 已验证、启用 |
| 2 | The Guardian | `@guardian_sport` | `46403451` | 不参与实时查询 | 已验证、归档禁用 |
| 2 | The Athletic | `@TheAthleticFC` | `970939705629069312` | Arsenal 主题候选 | 已验证、用户已确认、启用 |
| 2 | Art de Roché | `@ArtdeRoche` | `779610333145104384` | Arsenal 主题候选 | 已验证、启用 |
| 2 | David Hytner | `@DaveHytner` | `595406077` | Arsenal 主题候选 | 已验证、启用 |
| 2 | Jacob Steinberg | `@JacobSteinberg` | `43984593` | Arsenal 主题候选 | 已验证、启用 |

BBC Sport、Sami Mokbel 的 BBC 账号和其他来源均已通过官方 X API 固定数字 ID。
`@guardian_sport` 是已验证的 Guardian 官方体育账号，但该账号已经归档，不再参与实时
轮询。The Athletic 的足球官方账号 `@TheAthleticFC` 已完成人工确认；配置保留
`confirmation_required=true` 作为审计记录，同时设置 `confirmed=true`。

2026-08-01 的扩容仍以已定稿的 [r/Gunners 2025 社区投票](https://www.reddit.com/r/Gunners/comments/1lcgtey/2025_rgunners_tier_list_review_results/)
和 [Arsenal Mania 2025 来源榜](https://arsenal-mania.com/forum/threads/source-tier-list.37059/)
为 Tier 2 下限，并用记者的雇主档案交叉核验身份。Art de Roché 是 The Athletic 的 Arsenal
跟队作者；David Hytner 和 Jacob Steinberg 是 Guardian 的具名足球记者。三者均使用主题
查询，只补足媒体总号没有转发个人原创报道时的缺口。社区仍列为 Tier 3、评级有冲突或匿名
ITK 的候选没有因本次扩容被擅自升级。

### 成本和召回率的明确取舍

X 当前按返回的 Post 资源计费。若完整读取 BBC Sport、The Athletic、Romano
等账号的每一条内容，10 美元上限很可能无法维持一个月。因此默认配置：

- Arsenal 官方、Charles Watts、Amy Lawrence、Gunnerblog：读取其全部非 repost 内容；
- 其他高流量/多俱乐部账号：由 X 端先要求正文包含 `Arsenal OR #AFC OR Gunners`。

这样能显著降低付费读取量，但可能漏掉完全依赖上下文、正文又没有任何 Arsenal 标识的短回复。
这是硬预算下无法完全消除的召回风险。可在 `sources.toml` 把某个来源改为
`query_mode = "all"`，但必须先做付费 Dry-run；应用达到预算时仍会停止读取，不能用配置绕过
10 美元上限。

## 免费本地运行

Windows PowerShell：

```powershell
Copy-Item .env.example .env
$env:PYTHONPATH = "src"
python -m arsenal_alert doctor
python -m arsenal_alert run --once --dry-run
python -m arsenal_alert cost-report
```

Linux/macOS：

```bash
cp .env.example .env
PYTHONPATH=src python -m arsenal_alert doctor
PYTHONPATH=src python -m arsenal_alert run --once --dry-run
PYTHONPATH=src python -m arsenal_alert cost-report
```

`.env.example` 默认是 `APP_MODE=mock`、`DRY_RUN=true`、两个外部发送开关均关闭，所以这些
命令不会联网、不会付费、不会发送 Bark。模拟运行也使用持久化去重；若想再次看到相同模拟
通知，请为演示指定一个新的 `DB_PATH`，不要删除生产数据库。

### 自动化测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试覆盖：

1. Tier 1 原创转会消息只推送一次；
2. 普通球队新闻不推送；
3. 纯 repost 在模型前被拒绝；
4. 自己补充重要事实的引用帖推送；
5. 同一 Post 重复读取仍只推送一次；
6. 两位记者分别原创同一事件时推送两次；
7. 重启后旧消息不重复，停机期新消息可补查；
8. DeepSeek 无效结果失败关闭；
9. Bark 明确临时失败安全重试且不产生正常重复；
10. `.env.example`、仓库内容和日志不泄露密钥。

另有 X 分页、最坏情况预算预授权、DeepSeek `thinking=disabled`、严格 JSON、本地 Tier
边界和 Arsenal 当前交易参与门测试。参与门覆盖前球员转会其他俱乐部、明确回归、现役球员
离队、引援、外租球员变化、二次转会分成、女足、青训和普通新闻。一手性测试覆盖媒体自有
独家、纯 Repost、无新增转述、独立确认、同链接实质性新增、同原始报道持久化去重，以及
Ornstein/Romano 独立报道不合并。最近一次完整结果见
[docs/TEST_RESULTS.md](docs/TEST_RESULTS.md)。

## 正式配置

以下步骤会涉及外部服务。只有设置 `PAID_API_CALLS_ENABLED=true` 后，代码才允许 X 和
DeepSeek 正式调用；只有同时满足 `DRY_RUN=false` 与 `BARK_SEND_ENABLED=true` 才可能发送
真实 Bark。

### 1. X Developer Console

1. 在 [X Developer Console](https://console.x.com/) 创建 Project/App，获取 App-only
   Bearer Token。
2. 打开当前项目的 Billing/Credits 或 Usage & billing 页面。按照
   [X 官方定价说明](https://docs.x.com/x-api/getting-started/pricing) 找到
   **Spending limit**，将每个 billing cycle 的硬上限设为 **$10.00**。
3. 为避免充值策略绕过预期，检查并按自己的风险偏好关闭 auto-recharge。
4. 在 Console 再次确认 Post Read 和 User Read 的即时单价；更新
   `X_POST_READ_UNIT_USD`、`X_USER_READ_UNIT_USD` 和当天的 `X_PRICE_VERIFIED_AT`。
5. 根据实际账单周期设置 `X_BILLING_CYCLE_DAY`。应用侧默认按 UTC 每月 1 日估算，
   Console 才是最终计费权威。

官方文档在 2026-07-29 显示 Post Read 为 `$0.005/资源`、User Read 为
`$0.010/资源`，同一资源在 UTC 24 小时日窗口内通常只计费一次，但官方称其为软保证。
价格会变化，正式模式要求价格核对日期在 7 天内；已运行服务到期后也会停止轮询，更新
`.env` 日期和价格并重启才会继续。

### 2. 用官方 X API 复核数字用户 ID

当前目录的 13 条记录均已固定数字 ID；Guardian Sport 因归档而禁用，其余 12 条是正式轮询
来源。以下命令仅在用户名、雇主或来源配置变化时重新核验。先保持 Bark 关闭；当前命令只
读取启用来源，按 12 个 User 资源和上述单价估算约 `$0.12`，运行前仍需明确批准付费调用：

```powershell
$env:PYTHONPATH = "src"
python -m arsenal_alert verify-sources `
  --allow-paid-call `
  --output data/source-verification.json
```

命令还要求 `.env` 中存在：

```dotenv
PAID_API_CALLS_ENABLED=true
X_API_BASE_URL=https://api.x.com
X_BEARER_TOKEN=...
X_USER_READ_UNIT_USD=0.010
X_PRICE_VERIFIED_AT=YYYY-MM-DD
```

它只生成复核报告，不会自动信任账号或改写配置。人工检查：

- API 返回的 `id`、`username`、名称、affiliation、parody 等字段；
- `sources.toml` 内的雇主/官方证据链接；
- 需要人工确认的媒体账号是否仍与审计记录一致。

如账号发生变化，人工确认后再更新对应的 `user_id`、`identity_status` 和 `verified_at`；
命令只生成报告，不会自动改写来源配置或 Tier。

正式服务每 168 小时按数字 ID 通过官方 X API 复查一次当前用户名，默认约
`9 × $0.010 × 4.35 = $0.39/月`。若用户名变化、ID 缺失或账号被标为 parody，所有 X
轮询都会停止，不会自行换账号。

### 3. DeepSeek

在 [DeepSeek API 控制台](https://platform.deepseek.com/) 创建 API Key。项目使用
OpenAI 兼容的 `/chat/completions`，所有连接参数都来自环境变量：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=...
DEEPSEEK_PRICE_VERIFIED_AT=YYYY-MM-DD
```

服务启动时会先调用官方 `GET /models`，确认环境变量中的模型 ID 确实可用。2026-07-29
官方文档列出 `deepseek-v4-flash` 与 `deepseek-v4-pro`；Flash 当前更便宜，且支持
非思考模式和 JSON Output。代码显式发送：

```json
{
  "thinking": {"type": "disabled"},
  "response_format": {"type": "json_object"}
}
```

官方 JSON Output 仍可能返回空内容，因此代码不会只相信“合法 JSON”：字段集合、布尔类型、
原因枚举、翻译是否为空和条件关系都要再次本地校验。无效结果绝不进入 Bark。

参考：

- [DeepSeek 当前模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek 模型列表 API](https://api-docs.deepseek.com/api/list-models/)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/)

### 4. Bark

从 Bark iPhone App 复制 device key：

```dotenv
BARK_BASE_URL=https://api.day.app
BARK_DEVICE_KEY=...
BARK_GROUP=Arsenal Transfer Alert
BARK_LEVEL=active
DRY_RUN=true
BARK_SEND_ENABLED=false
```

先保持 Dry-run。Bark 正式使用 `/push` JSON API，点击通知跳到稳定的
`https://x.com/i/web/status/{Post ID}`。相同 Post 始终使用相同 Bark `id`；当前 Bark
客户端/服务端会更新相同 ID 的通知，但这不是本项目可以完全证明的事务幂等，所以网络结果
不确定时仍以避免重复为优先。

参考：

- [Bark 官方发送说明](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)
- [bark-server API V2](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)

## 两种 Dry-run

### 免费模拟 Dry-run

`APP_MODE=mock`。完全离线，使用 `fixtures/`，适合开发和回归测试。

### 真实数据 Dry-run

`APP_MODE=live`、`APP_ENV=development`、`DRY_RUN=true`、
`PAID_API_CALLS_ENABLED=true`。会产生 X 和 DeepSeek 费用，但 Bark 永远不发送。
开发预算闸门默认为 `$2.00`。

建议为试运行使用独立数据库：

```dotenv
DB_PATH=data/live-dry-run.sqlite3
```

运行至少 24 小时后：

```powershell
$env:PYTHONPATH = "src"
python -m arsenal_alert cost-report
```

报告给出样本小时数、返回资源数、已估算费用和 30.44 天线性投影。小于 24 小时的样本会标为
低置信度；转会窗和非转会窗应分别取样。Dry-run 已处理的 Post 不会在同一数据库切换正式
模式后补发，因此生产应使用独立数据库。

## 成本保护

在 2026-07-29 的 `$0.005/Post` 价格下：

```text
预计 X 月费
= 每月按 UTC 日去重后的 Post 资源数 × Post 单价
+ 官方身份复查 User 资源数 × User 单价
```

示例：

- 1,000 个 Post 资源约 `$5.00`；
- 12 个启用账号每周复核约 `$0.52/月`；
- 在 `$10` 内可留给 Post 的理论余量约 1,900 条，实际以 Console 为准。

应用保护：

- development 使用 `$2` 预算，production 使用 `$10`；
- production 的当前估算或至少 24 小时样本的月度投影达到 `$8` 时产生明显 warning；
- 每个 X 请求前按 `X_MAX_RESULTS × 单价` 预留最坏费用，宁可提前停止；
- 默认最多 25 条/页、5 页/查询、130 次 X 请求/小时；
- 数据库按 `UTC 日 + 资源类型 + 资源 ID` 记录估计计费资源；
- 高频请求账本默认保留 45 天并每日整理，Post ID/通知去重记录则永久保留；
- X、DeepSeek、Bark 的请求/Token/费用分别统计，不混入同一个数字；
- Developer Console 的 `$10` spending limit 是最终硬保护。

## Bark 通知示例

模拟数据生成的示例见 [docs/mock-bark-example.md](docs/mock-bark-example.md)：

```text
标题：🔴⚪ [Tier 1] David Ornstein

据 David Ornstein 报道，阿森纳已就可能引进马特奥·席尔瓦一事与
Northbridge FC 开启谈判。目前尚未达成协议。

来源：David Ornstein
时间：北京时间 2026-07-29 09:01:00
点击通知打开 X 原帖。
```

正文不含模型分析、背景扩写、可信度百分比或自行推断。

## 容器化和 24 小时运行

先完成 `.env` 和 `sources.toml`。`compose.yaml` 默认有：

- `restart: unless-stopped`；
- SQLite 数据卷；
- 只读根文件系统、无 Linux capabilities、非 root 用户；
- 30 秒健康检查；
- 只在本机 `127.0.0.1:8080` 暴露健康接口。

```bash
mkdir -p data
# Linux 主机若使用 bind mount：
sudo chown 10001:10001 data

docker compose build
docker compose up -d
docker compose logs -f --tail=100
```

正式推送前最后修改：

```dotenv
APP_ENV=production
APP_MODE=live
DRY_RUN=false
PAID_API_CALLS_ENABLED=true
BARK_SEND_ENABLED=true
DB_PATH=/data/arsenal-alert.sqlite3
```

实际 24 小时运行仍需要：一台持续在线且能访问 X、DeepSeek、Bark 的主机、持久磁盘，
以及三个服务的有效凭据/余额。来源目录已经身份就绪；仓库不绑定特定云平台，也不会提交
本地凭据、运行数据库或日志。

## 健康检查和日志

- `GET /health/live`：进程和 HTTP 线程存活；
- `GET /health/ready` 或 `/health`：数据库、X 新鲜度、预算和故障状态；
- `GET /metrics`：JSON 成本/调用摘要；
- `python -m arsenal_alert healthcheck`：容器健康命令。

日志是一行一个 JSON 对象。不会记录 Authorization、API Key、Bark device key、完整请求
Header 或 `.env` 内容。

常见 critical/warning：

| 标志/状态 | 含义 | 操作 |
|---|---|---|
| `x_identity_check` | 数字 ID、用户名或 parody 状态不匹配 | 停止正式推送，重新做官方复核 |
| `x_budget_guard` | 最坏情况会越过应用预算 | 查 Console 和 `cost-report`，等待下个周期 |
| `x_gap_*` | 停机超过 7 天 | 接受并记录可能缺口；Recent Search 无法补得更早 |
| `classification_error` | 模型多次无效/异常 | 检查模型 ID、余额、响应格式；没有推送 |
| `notification_uncertain` | Bark 可能收到但响应丢失 | 先看手机，再人工选择“视为已送达”或承担重复风险重试 |

Bark 不确定通知的人工处理：

```powershell
# 手机上已看到：
python -m arsenal_alert resolve-notification 123456789 `
  --action assume-delivered

# 手机上未看到，并明确接受可能重复：
python -m arsenal_alert resolve-notification 123456789 `
  --action retry `
  --acknowledge-duplicate-risk
```

## 常见故障

### `source catalog is not live-ready`

当前提交的来源目录已经 live-ready。如果出现此错误，说明某个启用来源的数字 ID、
`identity_status` 或人工确认状态被改成了未就绪值。先运行离线 `doctor` 定位条目；确需
重新核验时只使用官方 X source verification，不要使用第三方 ID 查询站。

### X 401/403

检查 Bearer Token、Project/App 权限和 Base URL。日志不会显示 Token。

### X 429

客户端遵守 `Retry-After` 并指数退避。若应用自己的每小时请求闸门触发，会等后续轮询。

### DeepSeek 配置模型不在 `/models`

重新查看官方模型列表，更新 `.env` 中 `DEEPSEEK_MODEL` 和价格；不要在代码内猜新模型名。

### Bark 5xx

明确的服务端失败会使用同一 Bark ID 退避重试。连接超时、连接重置、HTTP 成功但响应 JSON
无效都视为“可能已送达”，不会自动重试。

### SQLite

不要把 `DB_PATH` 放在临时文件系统。备份时同时保留主数据库；最稳妥做法是在停止容器后复制
`data/arsenal-alert.sqlite3`。不要同时运行两个生产实例指向同一 Bark key 和不同数据库。

## 官方 API 调研

接口选择、计费快照和核对日期集中记录在
[docs/API_RESEARCH.md](docs/API_RESEARCH.md)。正式启用前必须重新打开官方页面核对。
