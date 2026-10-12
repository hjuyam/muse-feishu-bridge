# hooks/ —— 事件触发器模板（"完整解决方案"的一部分）

本目录是把桥从"能跑"变成"长期在线"的关键一环：**轮询 + 保活 + 唤醒**。
原理：一个定时运行的脚本每 30 秒扫一次收件箱（`data/inbox.jsonl`），
有 `status=new` 的消息才唤醒一个 Muse worker 来处理；平时零开销。
同时它兼做 `listen` 长连接进程的保活：进程掉了自动拉起，拉不起来才告警。

## 文件

| 文件 | 说明 |
|---|---|
| `feishu-inbox-watch.sh` | 轮询脚本模板（bash + jq）。路径用环境变量配置，默认 `$HOME/workspace/muse-feishu-bridge`；无密钥，只读本地文件。 |
| `feishu-inbox-watch.json` | 事件定义模板：轮询间隔、脚本路径、worker 提示词。`delivery` 两处是占位符，**必须按你的 agent 运行时改**。 |

## 接入方式（两种）

**A. 让 Muse 代办（推荐）。** 把 [docs/MUSE_BRIEF.md](../docs/MUSE_BRIEF.md) 粘贴给 Muse，
说"把 hooks/ 里的触发器在我的机器上注册并启用"。Muse 会自己复制脚本、
按你的运行时改好 `delivery` 占位符并注册——这一步不需要你手工操作。

**B. 手工注册。** 把脚本放到你的 hook 脚本目录（如 `~/hooks/scripts/`），
按 `feishu-inbox-watch.json` 的结构在你的调度器里建一条定义：
每 30 秒运行脚本，脚本调用 `wake` 唤醒 worker，把 JSON payload 传过去。

## 运行时契约

脚本需要你的调度环境提供三个 shell 函数（见脚本头部的"运行时契约"注释）：

- `log "文字"` —— 记日志
- `silent "文字" '{}'` —— 本次无事件，静默结束
- `wake "理由" '{"event":"new_messages","message_ids":[...]}'` —— 唤醒 worker

没有提供这些函数的环境也能跑：脚本会退化为只打印到 stdout（方便手工测试逻辑）。

## worker 收到什么

`wake` 传过去的事件只有两种（见 JSON 模板里的 `prompt`，那就是 worker 的指令）：

1. `{"event":"new_messages","message_ids":[...]}` —— 处理收件箱里的新消息并回飞书。
   worker 对每条消息还要做表情反馈：开始处理前加 `Get` 表情（已收到），
   处理完并经发件队列确认"已发送"后删掉 `Get`、换上 `DONE`（处理完）——
   `cli send --inbox-message-id om_xxx` 会在确认后自动完成这一步。命令见主 README。
2. `{"event":"listen_restart_failed"}` —— listen 进程掉了且脚本重启失败，需要人工介入

## 重建（VM 重置后）

密钥不在仓库里，重置后按 [docs/RECOVERY.md](../docs/RECOVERY.md) 重建：
拉代码 → 填 `config.yaml` → 装依赖 → 重新注册本目录的 hook → 手动起一次 listen。
