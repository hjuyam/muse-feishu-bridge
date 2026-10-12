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
  "status": "queued" | "sending" | "sent",  # 认领中 = sending（见 claim_due）
  "attempts": 0,
  "next_retry_at": 0.0,        # 下次可尝试发送的时间戳（失败退避用）
  "created_at": 1729000000.0,
  "last_error": "",
  "sent_message_id": "",       # 飞书返回的 message_id（发送凭证）
  "claimed_by": "",            # status=sending 时：认领者的 owner 标识
  "claimed_at": 0.0,           # status=sending 时：认领时间戳（过期可回收）
}

并发说明：listen 进程里的发送线程（写）与 worker 进程（enqueue 读/写）
可能同时操作此文件。所有读写都经 flock 加锁，与 inbox.py 同一手法。

双发送者防护（2026-10-12 实测事故）：曾出现两个 listen 进程并存
（run_listen.sh 自重启与看门狗 cron 在重启窗口期竞态），各自的发送
线程同时扫到同一条目、HTTP 发了两次。防护分两层：
- 发送前必须先 claim_due 原子认领（排他锁下把 status 置为 sending 并
  写下认领者）；没抢到认领的发送者直接跳过，绝不碰 HTTP。
- mark_sent / mark_retry 只认自己名下的认领（owner 匹配）；认领超过
  CLAIM_STALE_AFTER 秒未完成视为发送者已死，可被其他发送者回收。
- listen 本身另有单实例锁（cli listen 启动时抢 data/listen.lock），
  双进程原则上不会出现；认领机制是最后一道防线。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows 无 fcntl：退化为不加锁（本项目跑在 Linux）
    fcntl = None  # type: ignore[assignment]

# 认领过期时间：发送者持有认领超过这么久还没 mark，就当它死了，
# 允许其他发送者回收重发。单次 HTTP 发送通常 1~2 秒，600 秒足够宽裕。
CLAIM_STALE_AFTER = 600.0

_STATUSES = ("queued", "sending", "sent")


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

    幂等：kind=reply 且 inbox_message_id 相同的条目，若已有 status=queued/
    sending 的则直接返回旧 queue_id（避免 worker 重试唤醒时重复入队导致
    double send）；若已有 status=sent 的也返回旧 id，调用方可直接视为已发送。
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
                        and r.get("status") in ("queued", "sending", "sent")):
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
            "claimed_by": "",
            "claimed_at": 0.0,
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


def claim_due(path: str | Path, owner: str, limit: int = 50,
              stale_after: float = CLAIM_STALE_AFTER) -> list[dict]:
    """原子认领到期条目（双发送者防护的核心）。

    排他锁下一次性：挑出到期的条目（status=queued 且 next_retry_at 已过，
    或 status=sending 但认领已过期——视为发送者已死可回收），按 created_at
    先后取最多 limit 条，把它们的 status 置为 sending 并写下 owner。
    返回认领到的记录列表。没抢到的发送者必须直接跳过，绝不发 HTTP。
    """
    path = Path(path)
    now = time.time()
    claimed: list[dict] = []
    try:
        with _locked(path, "r+", exclusive=True) as f:
            raw, records = _read_records(f)
            aligned = _align(raw, records)
            # (行号, 记录) 按 created_at 排序，保证"按入队顺序发出"
            valid = [(j, a) for j, a in enumerate(aligned) if a is not None]
            valid.sort(key=lambda t: float(t[1].get("created_at") or 0))
            out_lines = list(raw)
            changed = False
            for j, rec in valid:
                if len(claimed) >= limit:
                    break
                st = rec.get("status")
                retry_at = float(rec.get("next_retry_at") or 0)
                claimable = (
                    (st == "queued" and retry_at <= now)
                    or (st == "sending"
                        and now - float(rec.get("claimed_at") or 0) > stale_after)
                )
                if not claimable:
                    continue
                rec["status"] = "sending"
                rec["claimed_by"] = owner
                rec["claimed_at"] = now
                out_lines[j] = json.dumps(rec, ensure_ascii=False)
                claimed.append(dict(rec))
                changed = True
            if changed:
                f.seek(0)
                f.truncate()
                for out_line in out_lines:
                    f.write(out_line + "\n")
                f.flush()
    except FileNotFoundError:
        return []
    return claimed


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


def mark_sent(path: str | Path, queue_id: str, feishu_message_id: str,
              owner: str = "") -> bool:
    """标记发送成功，回填飞书返回的 message_id（发送凭证）。

    只接受自己名下的认领：status 必须是 sending 且 claimed_by == owner，
    否则返回 False（说明认领已过期被回收，HTTP 可能已被别人发出，
    调用方应记录告警而不是静默吞掉）。
    """
    def mutate(rec):
        if (rec.get("queue_id") == queue_id
                and rec.get("status") == "sending"
                and rec.get("claimed_by") == owner):
            rec["status"] = "sent"
            rec["sent_message_id"] = feishu_message_id
            rec["last_error"] = ""
            rec["claimed_by"] = ""
            rec["claimed_at"] = 0.0
            return True
        return False
    return _rewrite(Path(path), mutate)


def mark_retry(path: str | Path, queue_id: str, error: str,
               owner: str = "") -> bool:
    """标记一次发送失败：attempts+1，下次重试时间按指数退避推迟。

    只接受自己名下的认领；失败后把 status 打回 queued，释放认领，
    其他发送者可按退避时间接手。不阻塞队列——其他条目照常发送。
    """
    def mutate(rec):
        if (rec.get("queue_id") == queue_id
                and rec.get("status") == "sending"
                and rec.get("claimed_by") == owner):
            attempts = int(rec.get("attempts") or 0) + 1
            rec["attempts"] = attempts
            rec["next_retry_at"] = time.time() + _retry_delay(attempts)
            rec["last_error"] = error[:500]
            rec["status"] = "queued"
            rec["claimed_by"] = ""
            rec["claimed_at"] = 0.0
            return True
        return False
    return _rewrite(Path(path), mutate)
