# 飞书开放平台建应用（手把手）

> 目标：得到 `app_id` / `app_secret`，并让机器人能收到"发给它的消息"。
> 飞书控制台 UI 会变，以下是 2026-10 的大致路径，对不上时按字面意思找。

## 1. 创建应用

1. 打开 https://open.feishu.cn ，用你的飞书账号登录。
2. **开发者后台 → 创建企业自建应用**，填应用名称（如 `Muse`）、描述、图标。
3. 进入应用详情 → **凭证与基础信息**，记下 `App ID` / `App Secret`。

## 2. 开通机器人能力

1. 左侧 **添加应用能力 → 机器人**，启用。
2. **权限管理**，开通并申请以下权限（企业管理员需审批）：
   - `im:message` —— 发送消息
   - `im:message:send_as_bot` —— 以机器人身份发送（如需）
   - 获取"发给机器人的消息"事件：**事件订阅** → 搜索添加 `im.message.receive_v1`。
     - 本项目用**长连接**收事件，不需要填公网回调 URL，但事件仍需在这里订阅才会推送。
3. **版本管理与发布** → 创建版本并发布，应用才能在企业内被使用。

## 3. 把机器人拉进会话

- 私聊：直接在飞书搜索应用名，点开发送消息。
- 群聊：把机器人拉进群，`@机器人` 发消息（`chat_type` 为 `group` 时建议只回 @ 它的消息，逻辑可自行加）。

## 4. 填配置并验证

```bash
cp config.example.yaml config.yaml
# 填 app_id / app_secret（或 export FEISHU_APP_ID / FEISHU_APP_SECRET）
export PYTHONPATH=src
.venv/bin/python -m muse_feishu_bridge.cli doctor   # 不联网，只验配置
.venv/bin/python -m muse_feishu_bridge.cli listen   # 给机器人发句话，看 data/inbox.jsonl
```

`listen` 跑起来后，给机器人发一条消息，`data/inbox.jsonl` 里应出现一条 `status: new` 的记录。
再用 `send` 命令回一条，确认双向通畅：

```bash
.venv/bin/python -m muse_feishu_bridge.cli send --chat oc_xxxxxxxx --text "桥接测试：我能收到你"
```

`chat_id` 从 `inbox.jsonl` 那条记录里取。

## 5. 交给 Muse

双向通了之后，把 [MUSE_BRIEF.md](MUSE_BRIEF.md) 全文粘贴给 Muse，让它接管轮询与回复。
