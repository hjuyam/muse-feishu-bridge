"""命令行入口：

    python -m muse_feishu_bridge.cli doctor          # 只检查配置与依赖，不联网
    python -m muse_feishu_bridge.cli listen          # 长连接收消息 → 写收件箱（阻塞）
    python -m muse_feishu_bridge.cli send --chat oc_xxx --text "你好"
    python -m muse_feishu_bridge.cli history         # 查看收件箱未处理消息
    python -m muse_feishu_bridge.cli react --message-id om_xxx --emoji Get
    python -m muse_feishu_bridge.cli unreact --message-id om_xxx --reaction-id xxx
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import inbox
from .config import load_config


def cmd_doctor(args) -> int:
    try:
        import lark_oapi  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as e:
        print(f"缺少依赖: {e}，请先 bash scripts/install.sh")
        return 1
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置问题: {e}")
        return 1
    print(f"muse-feishu-bridge v{__version__}")
    print(f"app_id: {cfg.feishu.app_id[:6]}*** (已就绪，未联网验证)")
    print(f"收件箱: {cfg.bridge.inbox_path}")
    print(f"轮询间隔: {cfg.bridge.poll_interval_sec}s")
    print(f"白名单: {cfg.bridge.allow_open_ids or '(空 = 不限制，不推荐)'}")
    print("doctor 通过。下一步：python -m muse_feishu_bridge.cli listen")
    return 0


def _load_known_chats(inbox_path) -> set[str]:
    """从收件箱历史里收集所有见过的 chat_id（断线补拉时用）。"""
    from pathlib import Path

    chats: set[str] = set()
    p = Path(inbox_path)
    if not p.exists():
        return chats
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cid = json.loads(line).get("chat_id")
                except json.JSONDecodeError:
                    continue
                if cid:
                    chats.add(cid)
    except OSError:
        pass
    return chats


def _backfill_recent(cfg, known_chats: set[str], on_message) -> None:
    """重连前补拉各已知会话的近期历史。

    飞书长连接不补发断线期间的消息，靠这个找回。inbox.append 按 message_id
    去重，所以补拉是幂等的——重复跑也无害。单个会话失败只记日志，不影响其他。
    """
    if not known_chats:
        return
    from .feishu_client import FeishuBridge

    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret,
                          on_message=lambda m: None)
    for chat_id in sorted(known_chats):
        try:
            msgs = bridge.list_recent_messages(chat_id)
        except Exception as e:  # noqa: BLE001 -- 补拉失败不能拖死监听
            print(f"历史补拉 {chat_id} 失败，已跳过: {e}")
            continue
        added = sum(1 for m in msgs if on_message(m))
        if added:
            print(f"历史补拉 {chat_id}: 找回 {added} 条断线期间的消息")


def cmd_listen(args) -> int:
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    allow = set(cfg.bridge.allow_open_ids or [])
    known_chats = _load_known_chats(cfg.bridge.inbox_path)

    def on_message(msg: dict) -> bool:
        """返回 True 表示成功写入收件箱（新消息）。"""
        if allow and msg.get("open_id") not in allow:
            print(f"忽略非白名单消息 from {msg.get('open_id')}")
            return False
        if not msg.get("text") and msg.get("msg_type") != "text":
            print(f"暂只处理文本消息，已跳过 {msg.get('msg_type')}")
            return False
        if msg.get("chat_id"):
            known_chats.add(msg["chat_id"])
        ok = inbox.append(cfg.bridge.inbox_path, msg)
        print(f"{'收件' if ok else '重复跳过'}: {msg.get('open_id')} → {msg.get('text')[:40]}")
        return ok

    print("启动飞书长连接监听（Ctrl+C 退出）…")
    import time

    backoff = 5
    while True:
        # 每次（重）连前先补拉：覆盖上次断线到现在的窗口；去重保证幂等
        try:
            _backfill_recent(cfg, set(known_chats), on_message)
        except Exception as e:  # noqa: BLE001 -- 补拉失败绝不能拖死监听
            print(f"历史补拉异常，已跳过: {e}")
        start_ts = time.time()
        try:
            FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message).start_listening()
            print("监听连接结束（非主动退出），准备重连…")
        except KeyboardInterrupt:
            print("收到退出信号，停止监听。")
            return 0
        except Exception as e:  # noqa: BLE001 -- 任何异常都不应杀死监听进程
            print(f"监听异常退出: {e}")
        # 一次跑得够久（>60s）说明不是配置问题，重置退避；否则指数退避
        backoff = 5 if (time.time() - start_ts) > 60 else min(backoff * 2, 300)
        print(f"{backoff} 秒后重连…")
        time.sleep(backoff)


def cmd_send(args) -> int:
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message=lambda m: None)
    mid = bridge.send_text(args.chat, args.text)
    print(f"已发送，message_id={mid}")
    return 0


def cmd_react(args) -> int:
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message=lambda m: None)
    try:
        rid = bridge.add_reaction(args.message_id, args.emoji)
    except RuntimeError as e:
        print(str(e))
        return 1
    print(f"已加表情 {args.emoji}，reaction_id={rid}")
    return 0


def cmd_unreact(args) -> int:
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message=lambda m: None)
    try:
        bridge.remove_reaction(args.message_id, args.reaction_id)
    except RuntimeError as e:
        print(str(e))
        return 1
    print("已删除表情")
    return 0


def cmd_history(args) -> int:
    cfg = load_config(args.config)
    items = inbox.list_new(cfg.bridge.inbox_path)
    if not items:
        print("收件箱为空（没有未处理消息）。")
        return 0
    for it in items:
        print(f"- [{it['message_id']}] {it['open_id']}: {it['text'][:60]}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="muse-feishu-bridge", description="把 Meta Muse 接入飞书的传输桥")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="检查配置与依赖（不联网）")
    sub.add_parser("listen", help="长连接收消息并写入收件箱")
    s = sub.add_parser("send", help="发一条文本消息")
    s.add_argument("--chat", required=True, help="chat_id，如 oc_xxx")
    s.add_argument("--text", required=True)
    r = sub.add_parser("react", help="给一条消息加表情反馈（Get=已收到，DONE=处理完）")
    r.add_argument("--message-id", required=True, help="飞书 message_id，如 om_xxx")
    r.add_argument("--emoji", default="Get", help="表情类型，默认 Get（已收到），大小写敏感")
    u = sub.add_parser("unreact", help="删除之前加的表情反馈")
    u.add_argument("--message-id", required=True)
    u.add_argument("--reaction-id", required=True)
    sub.add_parser("history", help="查看未处理消息")
    args = p.parse_args(argv)
    return {"doctor": cmd_doctor, "listen": cmd_listen,
            "send": cmd_send, "react": cmd_react,
            "unreact": cmd_unreact, "history": cmd_history}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
