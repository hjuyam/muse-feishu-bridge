"""JSONL 收件箱：飞书消息的落地格式，也是 Muse 轮询的接口。

每行一条 JSON：
{
  "message_id": "om_xxx",      # 飞书消息 id，用于去重
  "chat_id": "oc_xxx",         # 回复时发回这个会话
  "chat_type": "p2p",
  "open_id": "ou_xxx",         # 发送者
  "msg_type": "text",
  "text": "帮我查一下...",
  "received_at": 1729000000.0,
  "status": "new",             # new -> done
  "reply": ""                  # Muse 处理完可回填（可选）
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append(path: str | Path, message: dict) -> bool:
    """追加一条消息；已存在的 message_id 返回 False（去重）。"""
    path = Path(path)
    _ensure_parent(path)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("message_id") == message.get("message_id"):
                        return False
                except json.JSONDecodeError:
                    continue
    record = {
        "message_id": message.get("message_id", ""),
        "chat_id": message.get("chat_id", ""),
        "chat_type": message.get("chat_type", ""),
        "open_id": message.get("open_id", ""),
        "msg_type": message.get("msg_type", ""),
        "text": message.get("text", ""),
        "received_at": time.time(),
        "status": "new",
        "reply": "",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def list_new(path: str | Path) -> list[dict]:
    """读出所有 status == new 的消息（按接收顺序）。"""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == "new":
                out.append(record)
    return out


def mark_done(path: str | Path, message_id: str, reply: str = "") -> bool:
    """把指定消息标为 done，可选回填 Muse 的回复摘要。"""
    path = Path(path)
    if not path.exists():
        return False
    changed = False
    lines = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                lines.append(line.rstrip("\n"))
                continue
            if record.get("message_id") == message_id and record.get("status") == "new":
                record["status"] = "done"
                if reply:
                    record["reply"] = reply
                changed = True
            lines.append(json.dumps(record, ensure_ascii=False))
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed
