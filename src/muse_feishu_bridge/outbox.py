"""JSONL 发件队列：worker 只管写队列，发送循环顺序发出。

设计目标（2026-10-12 Chan 确认）：
- 发信侧异步：worker（事件 worker）不再直接调飞书 HTTP，只写队列就返回；
- 发送循环（跑在 listen 进程里的后台线程）按入队顺序逐条发出；
- 失败单独重试：某条失败只推迟它自己的下次重试时间，不堵住后面的条目；
- DONE 异步：kind=reply 的条目在飞书确认"已发送"（拿到 message_id）之后，
  才做 Get→DONE 表情切换 + inbox 标 done；kind=progress 的进度播报只管发，
  不触发任何 done 逻辑。

每行一条 JSON：
{
  "queue_id": "q_ab12cd34",   # 入队时生成，幂等键的一部分
  "chat_id": "oc_xxx",        # 发往的会话
  "inbox_message_id": "om_xxx",# kind=reply 时为对应的收件箱消息 id（做 DONE 闭环用）
  "kind": "reply" | "progress",
  "text": "...",
  "status": "queued" | "sent", # 失败不设单独状态：attempts+1 并推迟 next_retry_at
  "attempts": 0,
  "next_retry_at": 0.0,        # 下次可尝试发送的时间戳（失败退避用）
  "created_at": 1729000000.0,
  "last_error": "",
  "sent_message_id": "",       # 飞书返回的 message_id（发送凭证）
}

并发说明：listen 进程里的发送线程（写）与 worker 进程（enqueue 读/写）
可能同时操作此文件。所有读写都经 flock 加锁，与 inbox.py 同一手法。
"""
from __future__ import annotations

import json
import time
import uuid
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


def _read_records(f) -> tuple[list[str], list[dict]]:
    """返回 (原始行, 解析成功的记录)。坏行在原始行里保留，记录里跳过。"""
    f.seek(0)
    raw = [line.rstrip("\n") for line in f if line.strip()]
    records = []
    for line in raw:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return raw, records


def _retry_delay(attempts: int) -> float:
    """失败退避：60s, 120s, 240s … 上限 30 分钟。无限重试，不丢任务。"""
    return min(60 * (2 ** max(attempts - 1, 0)), 1800)


def enqueue(path: str | Path, chat_id: str, text: str, kind: str = "progress",
            inbox_message_id: str = "") -> tuple[str, bool]:
    """入队一条待发送消息。返回 (queue_id, 是否本次新入队)。

    幂等：kind=reply 且 inbox_message_id 相同的条目，若已有 status=queued
    的则直接返回旧 queue_id（避免 worker 重试唤醒时重复入队导致 double send）；
    若已有 status=sent 的也返回旧 id，调用方可直接视为已发送。
    """
    if kind not in ("reply", "progress"):
        raise ValueError(f"kind 只能是 reply/progress，收到 {kind!r}")
    path = Path(path)
    now = time.time()
    with _locked(path, "a+", exclusive=True) as f:
        raw, records = _read_records(f)
        if kind == "reply" and inbox_message_id:
            for r in records:
                if (r.get("kind") == "reply"
                        and r.get("inbox_message_id") == inbox_message_id
                        and r.get("status") in ("queued", "sent")):
                    return r.get("queue_id", ""), False
        queue_id = "q_" + uuid.uuid4().hex[:8]
        record = {
            "queue_id": queue_id,
            "chat_id": chat_id,
            "inbox_message_id": inbox_message_id,
            "kind": kind,
            "text": text,
            "status": "queued",
            "attempts": 0,
            "next_retry_at": 0.0,
            "created_at": now,
            "last_error": "",
            "sent_message_id": "",
        }
        f.seek(0, 2)
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        return queue_id, True


def get(path: str | Path, queue_id: str) -> dict | None:
    """按 queue_id 取一条记录（共享锁）。"""
    path = Path(path)
    try:
        with _locked(path, "r", exclusive=False) as f:
            _, records = _read_records(f)
    except FileNotFoundError:
        return None
    for r in records:
        if r.get("queue_id") == queue_id:
            return r
    return None


def get_by_inbox(path: str | Path, inbox_message_id: str, kind: str = "reply") -> dict | None:
    """按收件箱消息 id 取最新的同 kind 条目（共享锁）。"""
    path = Path(path)
    try:
        with _locked(path, "r", exclusive=False) as f:
            _, records = _read_records(f)
    except FileNotFoundError:
        return None
    found = None
    for r in records:
        if r.get("inbox_message_id") == inbox_message_id and r.get("kind") == kind:
            found = r
    return found


def list_due(path: str | Path, limit: int = 50) -> list[dict]:
    """取出所有到期的待发送条目（status=queued 且 next_retry_at 已过），
    按 created_at 先后排序。"""
    path = Path(path)
    now = time.time()
    try:
        with _locked(path, "r", exclusive=False) as f:
            _, records = _read_records(f)
    except FileNotFoundError:
        return []
    due = [r for r in records
           if r.get("status") == "queued" and float(r.get("next_retry_at") or 0) <= now]
    due.sort(key=lambda r: float(r.get("created_at") or 0))
    return due[:limit]


def _rewrite(path: Path, mutate) -> bool:
    """排他锁下全文件重写；mutate(record) 返回 True 表示该行被修改。"""
    with _locked(path, "r+", exclusive=True) as f:
        raw, records = _read_records(f)
        changed = False
        out_lines = []
        for line, rec in zip(raw, _align(raw, records)):
            if rec is None:
                out_lines.append(line)  # 坏行原样保留
                continue
            if mutate(rec):
                changed = True
                out_lines.append(json.dumps(rec, ensure_ascii=False))
            else:
                out_lines.append(line)
        if changed:
            f.seek(0)
            f.truncate()
            for out_line in out_lines:
                f.write(out_line + "\n")
            f.flush()
        return changed


def _align(raw: list[str], records: list[dict]) -> list[dict | None]:
    """把解析成功的记录对齐回原始行（坏行位置填 None）。"""
    aligned: list[dict | None] = []
    it = iter(records)
    for line in raw:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            aligned.append(None)
        else:
            aligned.append(next(it))
    return aligned


def mark_sent(path: str | Path, queue_id: str, feishu_message_id: str) -> bool:
    """标记发送成功，回填飞书返回的 message_id（发送凭证）。"""
    def mutate(rec):
        if rec.get("queue_id") == queue_id and rec.get("status") == "queued":
            rec["status"] = "sent"
            rec["sent_message_id"] = feishu_message_id
            rec["last_error"] = ""
            return True
        return False
    return _rewrite(Path(path), mutate)


def mark_retry(path: str | Path, queue_id: str, error: str) -> bool:
    """标记一次发送失败：attempts+1，下次重试时间按指数退避推迟。
    不阻塞队列——其他条目照常发送。"""
    def mutate(rec):
        if rec.get("queue_id") == queue_id and rec.get("status") == "queued":
            attempts = int(rec.get("attempts") or 0) + 1
            rec["attempts"] = attempts
            rec["next_retry_at"] = time.time() + _retry_delay(attempts)
            rec["last_error"] = error[:500]
            return True
        return False
    return _rewrite(Path(path), mutate)
