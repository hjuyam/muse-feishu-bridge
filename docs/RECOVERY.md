# 灾难恢复手册（RECOVERY）

> 本文件不含任何密钥，可安全公开。密钥只存在于
> `config.yaml`（gitignored、仅磁盘）和飞书开放平台控制台。

## 重启 vs 重置

| 场景 | 配置会丢吗 | 恢复方式 |
|---|---|---|
| VM 重启（reboot） | **不丢**。`config.yaml`、`.venv/`、`data/` 都在磁盘上 | 全自动：平台 cron 看门狗（`scripts/listen-watchdog.sh`）每 2 分钟检测到 `listen` 不在就用 `scripts/run_listen.sh` 拉起；hook 脚本只做存活检测，拉起失败才唤醒 worker 告警 |
| VM 重置 / 重装镜像 | **全丢**。`config.yaml`（app_id/app_secret、白名单）、`.venv/`、`data/inbox.jsonl`、`~/hooks/` 下的脚本与定义都不在 git 里 | 按下面步骤手动重建 |

## 重置后的重建步骤（约 10 分钟）

```bash
# 1. 拉代码
git clone git@github.com:hjuyam/muse-feishu-bridge.git ~/workspace/muse-feishu-bridge
cd ~/workspace/muse-feishu-bridge

# 2. 重建配置（密钥去飞书开放平台 → 你的应用 → 凭证与基础信息 复制）
cp config.example.yaml config.yaml
# 用编辑器填好 feishu.app_id / feishu.app_secret / bridge.allow_open_ids

# 3. 装依赖
bash scripts/install.sh

# 4. 自检
export PYTHONPATH=src
.venv/bin/python -m muse_feishu_bridge.cli doctor

# 5. 重建 hooks（轮询脚本 + 事件定义），让 Muse 按 SETUP.md 第 5 步接管；
#    模板就在本仓库 hooks/ 目录（feishu-inbox-watch.sh + feishu-inbox-watch.json），
#    Muse 会按你的运行时改好 delivery 占位符并注册
bash hooks/feishu-inbox-watch.sh   # 无 hook runtime 时只打印到 stdout，可先手工验证逻辑

# 6. 手动起一次 listen（之后由 run_listen.sh 进程自重启 + cron 看门狗保活）
setsid nohup scripts/run_listen.sh >/dev/null 2>&1 < /dev/null &
```

## 飞书授权说明

- 本桥用的是**企业自建应用**的 `app_id` / `app_secret`（应用身份），
  不是用户扫码 OAuth 授权，没有"授权过期要重新扫码"这回事。
- `app_access_token` 有效期 2 小时，由 `lark-oapi` SDK 自动申请、缓存、续期，
  日常零维护。
- 真正会导致中断的情况与处理：

| 情况 | 处理 |
|---|---|
| 在开放平台**重置了 App Secret** | 更新 `config.yaml`，重启 listen（kill 旧进程，cron 看门狗 2 分钟内自动拉起新的） |
| 应用被停用 / 删除 | 在开放平台恢复或重建应用，按上面重建步骤走 |
| 管理员收回机器人权限 / 取消事件订阅 | 在开放平台重新开通权限、订阅 `im.message.receive_v1`，然后**重新发布版本** |
| 发布了新版本但未发布到线上 | 版本管理 → 发布，线上版本才生效 |
