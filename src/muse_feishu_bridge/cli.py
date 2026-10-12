"""命令行入口：

    python -m muse_feishu_bridge.cli doctor          # 只检查配置与依赖，不联网
    python -m muse_feishu_bridge.cli listen          # 长连接收消息 → 写收件箱（阻塞）
    python -m muse_feishu_bridge.cli send --chat oc_xxx --text "你好"
    python -m muse_feishu_bridge.cli send --chat oc_xxx --text "你好" --inbox-message-id om_xxx
    python -m muse_feishu_bridge.cli enqueue --chat oc_xxx --text "处理中…" [--kind progress]
    python -m muse_feishu_bridge.cli flush            # 手动跑一次发件队列
    python -m muse_feishu_bridge.cli history         # 查看收件箱未处理消息
    python -m muse_feishu_bridge.cli react --message-id om_xxx --emoji Get
    python -m muse_feishu_bridge.cli react-batch om_a om_b [--emoji Get]
    python -m muse_feishu_bridge.cli unreact --message-id om_xxx --reaction-id xxx
    python -m muse_feishu_bridge.cli get-message --message-id om_xxx  # 取单条消息（引用取原文）
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import inbox
from . import outbox
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


def _sender_pass(cfg, bridge) -> tuple[int, int]:
    """发送循环的一次扫描：按入队顺序逐条发出到期的队列条目。

    返回 (成功数, 失败数)。某条失败只推迟它自己的下次重试（指数退避，
    上限 30 分钟），不阻塞后面的条目。发送线程只负责 HTTP 发出 + 回填
    发送凭证；Get→DONE 表情切换与 inbox 标 done 由 cli send 的等待端
    （或下一次 worker 唤醒）完成，保证"确认发送成功才 done"的硬约束。
    """
    from pathlib import Path

    outbox_path = Path(cfg.bridge.inbox_path).parent / "outbox.jsonl"
    sent_ok = sent_fail = 0
    for entry in outbox.list_due(outbox_path):
        qid = entry.get("queue_id", "")
        try:
            mid = bridge.send_text(entry["chat_id"], entry["text"])
        except Exception as e:  # noqa: BLE001 -- 单条失败不影响其他条目
            outbox.mark_retry(outbox_path, qid, str(e))
            sent_fail += 1
            print(f"outbox 发送失败 {qid}（{entry.get('attempts', 0)}+1 次），"
                  f"已推迟重试: {e}")
            continue
        outbox.mark_sent(outbox_path, qid, mid)
        sent_ok += 1
        print(f"outbox 已发送 {qid} → 飞书 message_id={mid}")
    return sent_ok, sent_fail


def _sender_loop(cfg, bridge, interval: float = 5.0) -> None:
    """发送循环：常驻线程，每 interval 秒扫一次发件队列。"""
    import time

    while True:
        try:
            _sender_pass(cfg, bridge)
        except Exception as e:  # noqa: BLE001 -- 发送循环绝不能死
            print(f"发送循环异常，已跳过本轮: {e}")
        time.sleep(interval)


def cmd_listen(args) -> int:
    from pathlib import Path

    from .feishu_client import FeishuBridge, SUPPORTED_MSG_TYPES

    import threading

    cfg = load_config(args.config)
    allow = set(cfg.bridge.allow_open_ids or [])
    known_chats = _load_known_chats(cfg.bridge.inbox_path)
    chats_lock = threading.Lock()  # on_message 跑在 SDK 事件线程，加锁保护 known_chats
    attach_root = Path(cfg.bridge.inbox_path).parent / "attachments"
    # 附件下载专用桥（只做 HTTP，不建长连接；FeishuBridge 构造本身不联网）
    media_bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret,
                                on_message=lambda m: None)
    # 发件队列发送循环：worker 只写队列，由这个常驻线程顺序发出、
    # 失败单独退避重试。listen 进程由看门狗保活，队列文件落盘持久化，
    # 进程重启不丢任务。
    sender_bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret,
                                 on_message=lambda m: None)
    sender_thread = threading.Thread(target=_sender_loop,
                                     args=(cfg, sender_bridge),
                                     name="outbox-sender", daemon=True)
    sender_thread.start()

    def _save_attachment(msg: dict) -> None:
        """下载图片/文件/音频/视频附件落盘 data/attachments/<message_id>/，回填 msg。

        audio（语音条）、media/video（视频）的 content 均为 file_key 形态，
        与 file 走同一下载路子（飞书官方无 voice 类型，语音消息即 audio）。
        下载失败不抛异常：任务不能丢，失败原因写进 text，worker 会如实转告用户。
        """
        msg_type = msg.get("msg_type")
        kind = "image" if msg_type == "image" else ("audio" if msg_type == "audio" else "file")
        label = {"image": "图片", "audio": "语音", "media": "视频",
                 "video": "视频", "file": "文件"}.get(msg_type or "", "附件")
        key = msg.get("image_key") or msg.get("file_key")
        if not key:
            msg["text"] = (msg.get("text") or "") + f"[{label}消息，无附件 key]"
            return
        try:
            if kind == "image":
                data, name = media_bridge.download_image(key)
            else:
                data, name = media_bridge.download_file(key)
        except Exception as e:  # noqa: BLE001 -- 下载失败也要收件，不能丢任务
            msg["text"] = (msg.get("text") or "") + f"\n[附件下载失败：{e}]"
            print(f"附件下载失败 {msg.get('message_id')}: {e}")
            return
        dest_dir = attach_root / (msg.get("message_id") or "unknown")
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c for c in Path(name).name if c not in "/\\") or f"{key}.bin"
        dest = dest_dir / safe_name
        n = 2
        while dest.exists():  # 同名文件加序号
            dest = dest_dir / f"{dest.stem}-{n}{dest.suffix}"
            n += 1
        dest.write_bytes(data)
        msg["file_name"] = name
        msg["attachments"] = [{"kind": kind, "name": name, "path": str(dest)}]
        if not msg.get("text"):
            msg["text"] = {"image": "[图片]", "audio": "[语音]"} \
                .get(kind, f"[{'视频' if (msg.get('msg_type') in ('media', 'video')) else '文件'}：{name}]")
        print(f"附件已下载: {dest} ({len(data)} 字节)")

    def on_message(msg: dict) -> bool:
        """返回 True 表示成功写入收件箱（新消息）。"""
        if not msg.get("open_id"):
            # 防御：无发送者标识的消息（bot 自己的历史消息等）一律不收，
            # 否则白名单为空时补拉会把机器人自己的回复也写进收件箱
            return False
        if allow and msg.get("open_id") not in allow:
            print(f"忽略非白名单消息 from {msg.get('open_id')}")
            return False
        msg_type = msg.get("msg_type")
        if msg_type not in SUPPORTED_MSG_TYPES:
            # 明确记日志跳过（如语音/视频/表情包），不再静默丢弃
            print(f"不支持的消息类型，已跳过: {msg_type} message_id={msg.get('message_id')}")
            return False
        if inbox.exists(cfg.bridge.inbox_path, msg.get("message_id") or ""):
            print(f"重复跳过: {msg.get('message_id')}")
            return False
        # Get 移入 Listen（2026-10-12）：长连接收到消息的瞬间就加 Get 表情，
        # 不再等 hook 的 30 秒轮询。写在收件箱之前：成功则 reaction_id 直接
        # 回填进记录，hook 的 react-batch 会跳过已有 reaction_id 的记录，
        # 天然幂等不重复。尽力而为：失败不阻塞收件，hook 侧会兜底补 Get。
        try:
            msg["reaction_id"] = media_bridge.add_reaction(
                msg.get("message_id") or "", "Get")
            print(f"Get 已加: {msg.get('message_id')}")
        except Exception as e:  # noqa: BLE001
            print(f"Get 表情失败（hook 将兜底） {msg.get('message_id')}: {e}")
        if msg_type in ("image", "file", "audio", "media", "video"):
            _save_attachment(msg)
        elif msg_type == "post" and not msg.get("text"):
            msg["text"] = "[富文本消息]"
        if msg.get("chat_id"):
            with chats_lock:
                known_chats.add(msg["chat_id"])
        ok = inbox.append(cfg.bridge.inbox_path, msg)
        print(f"{'收件' if ok else '重复跳过'}: {msg.get('open_id')} → {msg.get('text')[:40]}")
        return ok

    print("启动飞书长连接监听（Ctrl+C 退出）…")
    import time

    backoff = 5
    while True:
        # 每次（重）连前先补拉：覆盖上次断线到现在的窗口；去重保证幂等
        # 快照 known_chats（on_message 跑在 SDK 线程，加锁防并发修改）
        with chats_lock:
            chats_snapshot = set(known_chats)
        try:
            _backfill_recent(cfg, chats_snapshot, on_message)
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
    """发一条文本消息（发信异步化后的入口，语义保持兼容）。

    流程：写发件队列（kind=reply，幂等去重）→ 等待发送循环确认发出 →
    拿到飞书 message_id 后做 Get→DONE 表情切换 + inbox 标 done →
    打印"已发送，message_id="。超时或失败则 exit 1，绝不标 done，
    条目留在队列里由发送循环继续重试，下次 worker 唤醒可重新闭环。
    """
    import time
    from pathlib import Path

    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    outbox_path = Path(cfg.bridge.inbox_path).parent / "outbox.jsonl"
    queue_id, is_new = outbox.enqueue(
        outbox_path, args.chat, args.text,
        kind="reply", inbox_message_id=args.inbox_message_id or "")
    if not is_new:
        print(f"队列已存在同条目（幂等复用），queue_id={queue_id}")

    deadline = time.time() + args.timeout
    entry = None
    while time.time() < deadline:
        entry = outbox.get(outbox_path, queue_id)
        if entry and entry.get("status") == "sent":
            break
        time.sleep(2)
        entry = None
    if not entry or entry.get("status") != "sent":
        print(f"发送超时（{args.timeout}s），条目 {queue_id} 仍在队列中稍后重试，"
              f"未标 done", file=sys.stderr)
        return 1

    mid = entry.get("sent_message_id", "")
    # DONE 闭环：只有飞书确认"已发送"之后，才换表情、标 done。
    if args.inbox_message_id:
        bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret,
                              on_message=lambda m: None)
        record = inbox.get(cfg.bridge.inbox_path, args.inbox_message_id) or {}
        rid = record.get("reaction_id") or ""
        if rid:
            try:
                bridge.remove_reaction(args.inbox_message_id, rid)
            except Exception as e:  # noqa: BLE001 -- 表情失败不影响 done
                print(f"删除 Get 表情失败（不影响 done）: {e}")
        try:
            bridge.add_reaction(args.inbox_message_id, "DONE")
        except Exception as e:  # noqa: BLE001
            print(f"加 DONE 表情失败（不影响 done）: {e}")
        summary = args.text[:40].replace("\n", " ")
        inbox.mark_done(cfg.bridge.inbox_path, args.inbox_message_id,
                        f"已发送 {mid}：{summary}")
    print(f"已发送，message_id={mid}")
    return 0


def cmd_enqueue(args) -> int:
    """只写发件队列、立即返回（进度播报用，不阻塞、不触发 done）。"""
    from pathlib import Path

    cfg = load_config(args.config)
    outbox_path = Path(cfg.bridge.inbox_path).parent / "outbox.jsonl"
    queue_id, is_new = outbox.enqueue(
        outbox_path, args.chat, args.text,
        kind=args.kind, inbox_message_id=args.inbox_message_id or "")
    print(f"已入队，queue_id={queue_id}{'（幂等复用）' if not is_new else ''}")
    return 0


def cmd_flush(args) -> int:
    """手动跑一次发送循环（调试/兜底用，listen 里的常驻线程平时会自动跑）。"""
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret,
                          on_message=lambda m: None)
    ok, fail = _sender_pass(cfg, bridge)
    print(f"flush 完成：成功 {ok} 条，失败 {fail} 条")
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


def cmd_react_batch(args) -> int:
    """批量加表情：一个进程内复用同一个客户端，分摊 ~16s 冷启动。

    stdout 逐行打印 TSV `message_id\\treaction_id`（只含成功的）；失败条目打
    到 stderr。整体恒 exit 0——部分失败不阻断上游（上游按 stdout 决定回填，
    失败的留给 worker 兜底 react）。
    """
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message=lambda m: None)
    for mid in args.message_ids:
        try:
            rid = bridge.add_reaction(mid, args.emoji)
        except Exception as e:  # noqa: BLE001 -- 单条失败不影响其他
            print(f"react 失败 {mid}: {e}", file=sys.stderr, flush=True)
            continue
        print(f"{mid}\t{rid}", flush=True)
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


def cmd_get_message(args) -> int:
    """按 message_id 取单条消息（引用场景：凭 parent_id 取被引用原文）。"""
    from .feishu_client import FeishuBridge

    cfg = load_config(args.config)
    bridge = FeishuBridge(cfg.feishu.app_id, cfg.feishu.app_secret, on_message=lambda m: None)
    try:
        m = bridge.get_message(args.message_id)
    except RuntimeError as e:
        print(str(e))
        return 1
    print(f"message_id: {m['message_id']}")
    print(f"msg_type: {m['msg_type']}")
    print(f"chat_id: {m['chat_id']}")
    if m.get("parent_id"):
        print(f"parent_id: {m['parent_id']}")
    if m.get("file_name"):
        print(f"file_name: {m['file_name']}")
    print(f"text: {m['text'][:500]}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="muse-feishu-bridge", description="把 Meta Muse 接入飞书的传输桥")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="检查配置与依赖（不联网）")
    sub.add_parser("listen", help="长连接收消息并写入收件箱")
    s = sub.add_parser("send", help="发一条文本消息（经发件队列，确认发出后才 done）")
    s.add_argument("--chat", required=True, help="chat_id，如 oc_xxx")
    s.add_argument("--text", required=True)
    s.add_argument("--inbox-message-id", default="",
                   help="对应的收件箱消息 id（om_xxx）：用于 Get→DONE 表情切换与标 done")
    s.add_argument("--timeout", type=int, default=180,
                   help="等待发送确认的最长秒数，默认 180")
    e = sub.add_parser("enqueue", help="只写发件队列、立即返回（进度播报用，不阻塞）")
    e.add_argument("--chat", required=True, help="chat_id，如 oc_xxx")
    e.add_argument("--text", required=True)
    e.add_argument("--kind", default="progress", choices=("reply", "progress"),
                   help="reply=最终回复（发出后走 DONE 闭环），progress=进度播报（只管发）")
    e.add_argument("--inbox-message-id", default="",
                   help="kind=reply 时对应的收件箱消息 id，用于幂等去重与 DONE 闭环")
    sub.add_parser("flush", help="手动跑一次发件队列发送（调试/兜底）")
    r = sub.add_parser("react", help="给一条消息加表情反馈（Get=已收到，DONE=处理完）")
    r.add_argument("--message-id", required=True, help="飞书 message_id，如 om_xxx")
    r.add_argument("--emoji", default="Get", help="表情类型，默认 Get（已收到），大小写敏感")
    rb = sub.add_parser("react-batch", help="批量加表情（一次进程复用客户端，分摊冷启动）")
    rb.add_argument("message_ids", nargs="+", help="飞书 message_id 列表，如 om_a om_b")
    rb.add_argument("--emoji", default="Get", help="表情类型，默认 Get（已收到），大小写敏感")
    u = sub.add_parser("unreact", help="删除之前加的表情反馈")
    u.add_argument("--message-id", required=True)
    u.add_argument("--reaction-id", required=True)
    sub.add_parser("history", help="查看未处理消息")
    g = sub.add_parser("get-message", help="按 id 取单条消息（引用场景：凭 parent_id 取被引用原文）")
    g.add_argument("--message-id", required=True, help="飞书 message_id，如 om_xxx")
    args = p.parse_args(argv)
    return {"doctor": cmd_doctor, "listen": cmd_listen,
            "send": cmd_send, "enqueue": cmd_enqueue, "flush": cmd_flush,
            "react": cmd_react,
            "react-batch": cmd_react_batch,
            "unreact": cmd_unreact, "history": cmd_history,
            "get-message": cmd_get_message}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
