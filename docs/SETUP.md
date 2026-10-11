# 飞书开放平台建应用（手把手）

> 目标：得到 `app_id` / `app_secret`，并让机器人能收到"发给它的消息"。
> 飞书控制台 UI 会变，以下是 2026-10 的大致路径，对不上时按字面意思找。

## 0. 先决定：这座桥跑在哪台机器上

本项目的设计是"agent 驱动桥"：Muse 自己跑轮询循环。所以：

- **方案 A（推荐）：让 Muse 来跑。** 你只需要提供飞书应用的 `app_id` / `app_secret`
  （secret 不要直接发在聊天里，问 Muse 要安全的方式），剩下的安装、联调、保活
  都由 Muse 做。代价：应用凭证放在 Muse 的机器上。
- **方案 B：你自己跑。** 凭证不出你的电脑，但 `listen` 需要长期开着
  （关机/合盖即断线），保活你自己负责。

下面第 4 步是方案 B 的操作；选方案 A 的话直接跳到第 5 步。

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

## 4. 填配置并验证（方案 B：自己跑）

以下所有命令都在**仓库根目录**的终端里执行，且已经跑过 `bash scripts/install.sh`。

```bash
# 1) 复制配置模板
cp config.example.yaml config.yaml
# 2) 用文本编辑器打开 config.yaml，把 app_id / app_secret 填进去并保存
#    （也可以不写文件，改为 export FEISHU_APP_ID=... FEISHU_APP_SECRET=...）
# 3) 告诉 Python 去 src/ 目录下找代码（每新开一个终端都要执行一次）
export PYTHONPATH=src
# 4) 自检：只检查配置和依赖是否齐全，不联网
.venv/bin/python -m muse_feishu_bridge.cli doctor
```

看到 "doctor 通过" 后，验证收发双向通畅。注意 `listen` 会**占住当前终端**
（按 Ctrl+C 才能退出），所以需要**再开第二个终端**
（同样先 `cd` 到仓库根目录、同样执行 `export PYTHONPATH=src`）：

终端一：
```bash
.venv/bin/python -m muse_feishu_bridge.cli listen
# 看到"启动飞书长连接监听"后就别动它
```

然后在飞书里给机器人发一句话。回到终端一，应该出现一行"收件"日志；
打开 `data/inbox.jsonl`（仓库根目录下），里面应多了一条 `"status": "new"` 的记录，
记下它的 `chat_id`（形如 `oc_xxxxxx`）。

终端二（把 `oc_xxxxxx` 换成你刚记下的真实 id）：
```bash
.venv/bin/python -m muse_feishu_bridge.cli send --chat oc_xxxxxx --text "桥接测试：我能收到你"
```

飞书里收到这条消息 = 收发双向通畅，可以进入第 5 步了。

## 5. 交给 Muse

双向通了之后，把 [MUSE_BRIEF.md](MUSE_BRIEF.md) 全文粘贴给 Muse，让它接管轮询与回复。
