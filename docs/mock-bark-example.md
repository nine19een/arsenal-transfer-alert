# 模拟 Bark 推送示例

该内容由 `fixtures/mock_posts.json` 和 `fixtures/mock_classifications.json` 经真实通知格式化
代码生成。没有调用 X、DeepSeek 或 Bark。

```text
标题：🔴⚪ [Tier 1] David Ornstein

据 David Ornstein 报道，阿森纳已就可能引进马特奥·席尔瓦一事与
Northbridge FC 开启谈判。目前尚未达成协议。

来源：David Ornstein
时间：北京时间 2026-07-29 09:01:00
点击通知打开 X 原帖。

点击 URL：https://x.com/i/web/status/1001
Bark id：arsenal-transfer-1001
```

这个示例特意保留了原文的“不确定”和“尚未达成协议”，没有把谈判改写为完成交易。
该模拟结果同时声明 `club_scope_eligible=true`、
`club_participation=buyer/recruiting_club` 和 `news_origin=first_hand_report`；
缺少其中任何资格门都不会生成通知。

同一组模拟数据还包含一条“前阿森纳球员富安健洋考虑加盟水晶宫”的边界样例。该事件中
Arsenal 只出现在履历里，模拟分类返回
`club_participation=none` 和 `former_target_club_player_unrelated`，因此不会生成 Bark
通知。
