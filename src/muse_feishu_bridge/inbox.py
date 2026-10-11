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

并发说明：listen 进程（append）与 worker（mark_done）是两个进程，可能同时
操作此文件。所有读写都经 flock 加锁：写操作拿排他锁，读操作拿共享锁，
杜绝"mark_done 全文件重写时正好 append 新消息导致丢失"的竞态。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows 无 fcntl：退化为不加锁（本项目跑在 Linux）
    fcntl = None  # type: ignore[assignment]


def _lock(f, exclusive: bool) -> None:
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _unlock(f) -> None:
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked(path: Path, mode: str, exclusive: bool):
    """按 mode 打开文件并持有 flock；退出时解锁并关闭。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open(mode, encoding="utf-8")
    try:
        _lock(f, exclusive)
        yield f
    finally:
        try:
            _unlock(f)
        finally:
            f.close()


def _read_lines(f) -> list[str]:
    """读出所有非空行（原始文本，坏行原样保留）。"""
    f.seek(0)
    return [line.rstrip("\n") for line in f if line.strip()]


def append(path: str | Path, message: dict) -> bool:
    """追加一条消息；已存在的 message_id 返回 False（去重）。"""
    path = Path(path)
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
    with _locked(path, "a+", exclusive=True) as f:
        for line in _read_lines(f):
            try:
                if json.loads(line).get("message_id") == record["message_id"]:
                    return False
            except json.JSONDecodeError:
                continue
        f.seek(0, 2)  # a+ 模式写前必须显式移到末尾
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
    return True


def list_new(path: str | Path) -> list[dict]:
    """读出所有 status == new 的消息（按接收顺序）。"""
    path = Path(path)
    try:
        with _locked(path, "r", exclusive=False) as f:
            out = []
            for line in _read_lines(f):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("status") == "new":
                    out.append(record)
            return out
    except FileNotFoundError:
        return []


def mark_done(path: str | Path, message_id: str, reply: str = "") -> bool:
    """把指定消息标为 done，可选回填 Muse 的回复摘要。"""
    path = Path(path)
    try:
        with _locked(path, "r+", exclusive=True) as f:
            lines = _read_lines(f)
            changed = False
            out_lines = []
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    out_lines.append(line)  # 坏行原样保留
                    continue
                if record.get("message_id") == message_id and record.get("status") == "new":
                    record["status"] = "done"
                    if reply:
                        record["reply"] = reply
                    changed = True
                    out_lines.append(json.dumps(record, ensure_ascii=False))
                else:
                    out_lines.append(line)
            if changed:
                f.seek(0)
                f.truncate()
                for out_line in out_lines:
                    f.write(out_line + "\n")
                f.flush()
            return changed
    except FileNotFoundError:
        return False
