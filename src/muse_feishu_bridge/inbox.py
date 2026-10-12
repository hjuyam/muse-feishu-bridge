"""JSONL 收件箱：飞书消息的落地格式，也是 Muse 轮询的接口。

每行一条 JSON：
{
  "message_id": "om_xxx",      # 飞书消息 id，用于去重
  "chat_id": "oc_xxx",         # 回复时发回这个会话
  "chat_type": "p2p",
  "open_id": "ou_xxx",         # 发送者
  "msg_type": "text",        # text / post(富文本，已转纯文本) / image / file /
                            # audio(语音条) / media、video(视频) / merge_forward(合并转发)
  "text": "帮我查一下...",
  "image_key": "",          # 图片消息的飞书 key（image 类型有）
  "file_key": "",           # 文件/语音/视频消息的飞书 key
  "file_name": "",          # 文件名（下载响应返回）
  "attachments": [],        # 已下载到本地的附件 [{kind, name, path}]，path 为仓库相对路径
  "received_at": 1729000000.0,
  "status": "new",             # new -> done
  "reply": "",                 # Muse 处理完可回填（可选）
  "reaction_id": "on_xxx",     # 可选：listen 收件时同步加的 Get 表情 id；
                               # 无则由轮询脚本抢加后回填，worker 沿用
  "parent_id": "om_xxx",       # 可选：引用/回复场景下被引用消息的 id
  "raw_content": "{...}",      # 可选：merge_forward 的原文 JSON
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


def _maybe_rotate(path: Path) -> None:
    """按月归档：如果文件里最后一条消息是上个月及以前的，整文件移走。

    归档名：inbox-YYYYMM.jsonl（YYYYMM 取文件第一条记录的月份）。
    在 append() 开头调用，无锁检查；并发时 rename 冲突由 FileNotFoundError 兜底。
    """
    from datetime import datetime

    if not path.exists() or path.stat().st_size == 0:
        return
    first_ts = last_ts = 0.0
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(json.loads(line).get("received_at") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    return  # 读到坏行就不动，下次再说
                if not first_ts:
                    first_ts = ts
                last_ts = ts
    except OSError:
        return
    if last_ts <= 0:
        return
    cur_month = datetime.now().strftime("%Y%m")
    if datetime.fromtimestamp(last_ts).strftime("%Y%m") == cur_month:
        return  # 本月还有消息，不归档
    tag = datetime.fromtimestamp(first_ts).strftime("%Y%m") if first_ts > 0 else cur_month
    archive = path.with_name(f"{path.stem}-{tag}{path.suffix}")
    n = 2
    while archive.exists():  # 极小概率重名，加序号
        archive = path.with_name(f"{path.stem}-{tag}-{n}{path.suffix}")
        n += 1
    try:
        path.rename(archive)
    except FileNotFoundError:
        pass  # 并发时另一进程已归档


def append(path: str | Path, message: dict) -> bool:
    """追加一条消息；已存在的 message_id 返回 False（去重）。

    除基础字段外，还透传以下上游字段（有则写，无则略）：
    - reaction_id：listen 收到消息时同步加的 Get 表情 id，供 hook 的
      react-batch 跳过、worker 的 Get→DONE 切换沿用；
    - parent_id：引用/回复场景下被引用消息的 message_id；
    - raw_content：merge_forward 的原文 JSON（content 为占位符时）。
    """
    path = Path(path)
    _maybe_rotate(path)
    record = {
        "message_id": message.get("message_id", ""),
        "chat_id": message.get("chat_id", ""),
        "chat_type": message.get("chat_type", ""),
        "open_id": message.get("open_id", ""),
        "msg_type": message.get("msg_type", ""),
        "text": message.get("text", ""),
        "image_key": message.get("image_key", ""),
        "file_key": message.get("file_key", ""),
        "file_name": message.get("file_name", ""),
        "attachments": message.get("attachments", []),
        "received_at": time.time(),
        "status": "new",
        "reply": "",
    }
    for key in ("reaction_id", "parent_id", "raw_content"):
        if message.get(key):
            record[key] = message[key]
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


def exists(path: str | Path, message_id: str) -> bool:
    """收件箱里是否已有该 message_id（任何 status）。共享锁读，用于下载附件前
    的预去重，避免重连补拉时重复下载已收过的图片/文件。"""
    path = Path(path)
    try:
        with _locked(path, "r", exclusive=False) as f:
            for line in _read_lines(f):
                try:
                    if json.loads(line).get("message_id") == message_id:
                        return True
                except json.JSONDecodeError:
                    continue
            return False
    except FileNotFoundError:
        return False


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


def get(path: str | Path, message_id: str) -> dict | None:
    """按 message_id 取一条收件箱记录（共享锁），不存在返回 None。"""
    path = Path(path)
    try:
        with _locked(path, "r", exclusive=False) as f:
            for line in _read_lines(f):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("message_id") == message_id:
                    return record
    except FileNotFoundError:
        return None
    return None


def set_reaction_id(path: str | Path, message_id: str, reaction_id: str) -> str | None:
    """排他锁下的原子 check-and-set：给 status=new 的记录回填 reaction_id。

    仅当记录 status == "new" 且尚无 reaction_id（无或空）时写入；返回生效的
    reaction_id（已有的或新写入的）。记录不存在或 status 非 new 返回 None。
    供轮询脚本抢先把"已收到"Get 表情记进收件箱：即使两次轮询并发到同一消息，
    也只有一次写入生效；worker 据此跳过自加、沿用记录的值。
    """
    path = Path(path)
    try:
        with _locked(path, "r+", exclusive=True) as f:
            lines = _read_lines(f)
            changed = False
            effective: str | None = None
            out_lines = []
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    out_lines.append(line)  # 坏行原样保留
                    continue
                if (record.get("message_id") == message_id
                        and record.get("status") == "new"
                        and not (record.get("reaction_id") or "")):
                    record["reaction_id"] = reaction_id
                    effective = reaction_id
                    changed = True
                    out_lines.append(json.dumps(record, ensure_ascii=False))
                    continue
                if (record.get("message_id") == message_id
                        and record.get("status") == "new"):
                    effective = record.get("reaction_id") or ""  # 已有：不覆盖
                out_lines.append(line)
            if changed:
                f.seek(0)
                f.truncate()
                for out_line in out_lines:
                    f.write(out_line + "\n")
                f.flush()
            return effective
    except FileNotFoundError:
        return None


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
