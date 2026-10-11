# 给 Muse 的接入指令（全文复制粘贴给 Muse 即可）

> 把下面整段发给你的 Muse，它会照着做。

---

我clone 了 `muse-feishu-bridge` 仓库（目录：`./muse-feishu-bridge`），请帮我把飞书接入你，
跑起来并保持在线。按以下步骤做，遇到问题停下来问我：

1. **读文档**：先读仓库里的 `README.md`、`docs/SETUP.md`、`docs/ARCHITECTURE.md`，
   理解"agent 驱动桥"的架构：你负责轮询收件箱、处理消息、发回飞书。
2. **装依赖**：`bash scripts/install.sh`（需要 Python 3.10+）。
3. **配凭证**：`cp config.example.yaml config.yaml`，然后问我要飞书应用的 `app_id` /
   `app_secret`（我来提供，不要自己编）。把我的飞书 `open_id` 加进
   `allow_open_ids` 白名单。
4. **自检**：跑 `PYTHONPATH=src .venv/bin/python -m muse_feishu_bridge.cli doctor`，
   确认通过。
5. **联调**：后台启动 `listen`（`nohup` 或等价方式，日志落到文件），然后让我在飞书里
   给机器人发一条测试消息，确认 `data/inbox.jsonl` 出现记录；再用 `send` 命令给我回
   一条，确认我能收到。
6. **接管循环**：双向通了之后，你建立一个定时任务（每 `poll_interval_sec` 秒或每 1 分钟），
   循环做：`history` 看未处理消息 → 逐条理解并执行（能用你的全部能力：上网、跑代码、
   查资料等）→ `send` 回复到对应 `chat_id` → 标记 done。
   注意：只处理白名单用户的消息；执行高风险操作前先在回复里跟我确认。
7. **保活**：`listen` 进程掉了要能自己拉起来；每天向我汇报一次运行状态（在线/异常）。

开始吧，先从第 1 步读文档起。
