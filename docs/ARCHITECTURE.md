# 架构设计

## 一句话

智能在 Muse 那一侧，本仓库只做传输：**飞书长连接收消息 → JSONL 收件箱 → hook 检测到新消息才唤醒 Muse → 飞书 API 回消息**。

```
飞书用户 ──私聊/@──▶ 自建应用(机器人)
                           │  im.message.receive_v1（长连接，无需公网 URL）
                           ▼
                    cli listen（本仓库）
                           │  append → data/inbox.jsonl
                           ▼
                    hook 脚本每 30s 扫收件箱（被动，无消息时零开销）
                           │  有新消息才 wake Muse
                           ▼
                    Muse 理解 / 执行任务
                           ▼
                    cli send ──▶ 飞书 API ──▶ 用户收到回复
```

## 为什么是 agent 驱动桥，而不是桥驱动 agent

同类项目有两种架构，选型取决于 agent 有没有"可被外部调用的入口"：

|                    | 桥驱动 agent                     | agent 驱动桥（本项目）              |
|--------------------|----------------------------------|-------------------------------------|
| 例子               | Claude Code / Codex 接飞书       | 微信版 wechat-bridge、本项目        |
| 前提               | agent 是本地 CLI，有 stdio/ACP   | agent 是云端服务，无公开调用入口    |
| 消息流             | 桥进程每条消息 spawn 一次 CLI    | 有飞书消息才唤醒 agent，处理 + 回    |
| 延迟特点           | 低（事件驱动）                   | 取决于 hook 检测间隔（默认 30s），无消息时零开销 |

Meta 个人助理 Muse 目前没有公开的"发一条消息、调一次"的 API/CLI（官方消息通道只有 WhatsApp），
所以本项目只能走右列：Muse 自己托管桥接循环。这也是微信版 wechat-bridge 的路子——
"把一句话丢给 Muse，扫码即完成"，慢是因为轮询，适合汇总、提醒、轻任务类场景。

> 如果未来 Meta 开放个人助理 Muse 的 API，本项目可以无痛切换到左列：
> 只需把 `inbox.py` 换成"收到事件即调 API"，传输层（`feishu_client.py`）不用动。

## 收件箱协议

`data/inbox.jsonl` 每行一条，字段见 `src/muse_feishu_bridge/inbox.py` 头部注释。
Muse 侧约定：

1. `list_new()` 取出未处理消息；
2. 逐条处理（理解意图、执行任务）；
3. 用 `cli send --chat <chat_id> --text ...` 回复；
4. `mark_done(message_id, reply=...)` 标记，避免重复处理。

去重键是飞书 `message_id`，`listen` 写入时已做过去重；Muse 重启后从收件箱继续即可。

## 可靠性

- **断线重连**：`cli listen` 主循环捕获任何返回/异常后指数退避重连（5s→300s；
  单次运行超 60s 则重置退避）。SDK 在某些场景（如 no close frame）会直接返回，
  不靠它内部重连。
- **进程保活**：两层。① listen 自重启：`scripts/run_listen.sh` 在 python 进程
  退出后 10 秒重拉（连续 5 次 30 秒内快崩则等 5 分钟再试）；② 外部看门狗：
  `scripts/listen-watchdog.sh` 挂在平台 cron 上每 2 分钟跑一次，pgrep 发现
  listen 不在就拉起。hook 脚本每 30 秒只做检测与告警——它的运行时不允许拉起
  常驻进程（超时后会杀掉本轮进程树），worker 会话结束同样会回收子进程。
- **断线补拉**：飞书长连接不补发断线期间的消息。每次（重）连前调
  `im.v1.message.list` 拉取各已知会话最近 50 条历史，按 `message_id` 去重写入
  收件箱（幂等）。已知会话来自收件箱历史里的 `chat_id`。
- **收件箱并发**：`listen`（append）与 worker（mark_done 全文件重写）是两个进程，
  `inbox.py` 所有读写经 `fcntl.flock` 加锁（写排他、读共享），杜绝重写撕裂丢消息。

## 安全边界

- 凭证只走 `config.yaml` / 环境变量，`.gitignore` 已排除。
- `allow_open_ids` 白名单：默认只处理你自己的消息，防止陌生人使唤你的 Muse。
- 飞书应用最小权限：`im:message`（发）+ `im.message.receive_v1`（收）。
- 长连接断线重连由 SDK 内部处理；Muse 侧用 hook 脚本兼做保活：每 30 秒检查 `listen`
  进程，掉了自动拉起（`scripts/run_listen.sh`），重启失败才唤醒 Muse 人工介入。
