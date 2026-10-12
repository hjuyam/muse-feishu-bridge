"""飞书传输层：官方 lark-oapi SDK。

- 收：WebSocket 长连接（无需公网 URL），收到「发给机器人的消息」事件后归一化，
  交给调用方（cli.listen 会写入收件箱）。入站支持文本 / 富文本(post) /
  图片 / 文件 / 音频(audio) / 视频(media) / 合并转发(merge_forward)；
  图片、文件、音频、视频附件可按 key 下载（后三者 content 均为 file_key 形态）。
- 发：HTTP API 发文本消息回指定会话。
- 引用：事件自带 parent_id（被引用/被回复消息的 message_id），
  get_message 可按 id 取单条消息内容（被引用的原文）。
"""
from __future__ import annotations

import json
from typing import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    GetFileRequest,
    GetImageRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

MessageHandler = Callable[[dict], None]

# 入站支持的消息类型。超出此范围的（如表情包）由调用方明确记日志
# 跳过，不再静默丢弃——2026-10-11 前 post 消息曾被静默跳过导致用户"丢任务"。
# 2026-10-12 扩展：audio（语音条；飞书官方无 voice 类型，语音消息即 audio）、
# media/video（视频，content 为 file_key 形态，与 file 同一下载路子）、
# merge_forward（合并转发的聊天记录卡片，content 为占位符，原文保留待展开）。
SUPPORTED_MSG_TYPES = (
    "text", "post", "image", "file",
    "audio", "media", "video",
    "merge_forward", "merged_forward",
)


def _get(obj, *attrs, default=""):
    for a in attrs:
        obj = getattr(obj, a, None)
        if obj is None:
            return default
    return obj if obj != "" else default


def _post_to_text(post: dict) -> str:
    """把飞书富文本(post)转成纯文本：标题 + 各段落。

    兼容两种来源的格式（2026-10-12 实测：两者结构不一样）：
    - 事件推送：{"post": {"zh_cn": {"title":..., "content": [...]}}}
    - 历史 API：直接就是 {"title":..., "content": [...]}，无 post 包裹、无 locale
    文本/超链接保留文字，@ 保留被 @ 对象，图片记为 [图片]，
    其他元素（分割线等）忽略。locale 优先 zh_cn，其次 en_us。
    """
    if not isinstance(post, dict):
        return ""
    if isinstance(post.get("post"), dict):
        post = post["post"]  # 事件推送格式：先解一层 post
    block = None
    for locale in ("zh_cn", "en_us"):
        if isinstance(post.get(locale), dict):
            block = post[locale]
            break
    if block is None and isinstance(post.get("content"), list):
        block = post  # 历史 API 格式：本身就是 block
    if block is None:
        for v in post.values():
            if isinstance(v, dict):
                block = v
                break
    if not isinstance(block, dict):
        return ""
    lines = []
    for para in block.get("content") or []:
        segs = []
        for el in para or []:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag == "text":
                segs.append(el.get("text", ""))
            elif tag == "a":
                segs.append(el.get("text") or el.get("href") or "")
            elif tag == "at":
                segs.append("@" + str(el.get("user_id") or el.get("text") or ""))
            elif tag == "img":
                segs.append("[图片]")
        lines.append("".join(segs))
    body = "\n".join(lines).strip()
    title = (block.get("title") or "").strip()
    return f"{title}\n{body}".strip() if title else body


def _extract_content(msg_type: str, raw_content: str) -> dict:
    """按消息类型从 content JSON 里提取 text / image_key / file_key。

    text：content.text；post：转纯文本；image：image_key；
    file/audio/media/video：file_key（file_name 事件里通常没有，下载时再补；
    audio 即语音条，飞书官方无 voice 类型；media/video 为视频）；
    merge_forward：合并转发的聊天记录卡片，content 为占位符，
    这里只给占位文本，原文由调用方保留（raw_content）待展开。
    """
    try:
        content = json.loads(raw_content)
    except (json.JSONDecodeError, AttributeError, TypeError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    out = {"text": "", "image_key": "", "file_key": "", "file_name": ""}
    if msg_type == "text":
        out["text"] = content.get("text", "") or ""
    elif msg_type == "post":
        out["text"] = _post_to_text(content)  # _post_to_text 内部兼容事件/历史两种格式
    elif msg_type == "image":
        out["image_key"] = content.get("image_key", "") or ""
    elif msg_type in ("file", "audio", "media", "video"):
        out["file_key"] = content.get("file_key", "") or ""
        out["file_name"] = content.get("file_name", "") or ""
    elif msg_type in ("merge_forward", "merged_forward"):
        out["text"] = "[合并转发的聊天记录]"
    return out


class FeishuBridge:
    def __init__(self, app_id: str, app_secret: str,
                 on_message: MessageHandler,
                 log_level=lark.LogLevel.INFO) -> None:
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(log_level)
            .build()
        )
        dispatcher = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(
                lambda data: on_message(self._normalize(data))
            )
            .build()
        )
        # ws 长连接客户端懒加载：只有 listen 需要它，send/react 等纯 HTTP 调用不创建
        self._app_id = app_id
        self._app_secret = app_secret
        self._log_level = log_level
        self._dispatcher = dispatcher
        self._ws = None

    @staticmethod
    def _normalize(data: P2ImMessageReceiveV1) -> dict:
        """把 SDK 事件转成 inbox.py 的消息格式。

        parent_id：引用/回复场景下被引用消息的 message_id（无引用时为空串），
        worker 可凭它调 get_message 取被引用原文。
        merge_forward 的 content 为占位符，原文 JSON 存 raw_content 供后续展开。
        """
        event = _get(data, "event", default=None)
        msg = _get(event, "message", default=None)
        sender = _get(event, "sender", default=None)
        sender_id = _get(sender, "sender_id", default=None)
        msg_type = _get(msg, "message_type")
        raw_content = _get(msg, "content", default="{}")
        extracted = _extract_content(msg_type, raw_content)
        record = {
            "message_id": _get(msg, "message_id"),
            "chat_id": _get(msg, "chat_id"),
            "chat_type": _get(msg, "chat_type"),
            "msg_type": msg_type,
            "text": extracted["text"],
            "image_key": extracted["image_key"],
            "file_key": extracted["file_key"],
            "file_name": extracted["file_name"],
            "open_id": _get(sender_id, "open_id"),
            "parent_id": _get(msg, "parent_id"),
        }
        if msg_type in ("merge_forward", "merged_forward"):
            record["raw_content"] = raw_content
        return record

    def start_listening(self) -> None:
        """阻塞运行长连接。注意：SDK 在某些断线场景（如 no close frame）会直接返回，
        不会自动重连——调用方（cli listen）负责捕获返回/异常并重连。"""
        if self._ws is None:
            self._ws = lark.ws.Client(
                self._app_id, self._app_secret,
                event_handler=self._dispatcher, log_level=self._log_level,
            )
        self._ws.start()

    @staticmethod
    def _normalize_history(item, chat_id: str) -> dict:
        """把「获取历史消息」API 的 Message 转成 inbox 格式。

        注意：历史 API 的 Message 模型字段名与事件推送不同（msg_type 而非
        message_type），不要复用 _normalize。
        """
        sender = _get(item, "sender", default=None)
        body = _get(item, "body", default=None)
        msg_type = _get(item, "msg_type")
        extracted = _extract_content(msg_type, _get(body, "content", default="{}"))
        # 只取 user 发的消息；bot 自己的回复 sender_type 为 app，会被上层白名单滤掉
        open_id = _get(sender, "id") if _get(sender, "sender_type") == "user" else ""
        return {
            "message_id": _get(item, "message_id"),
            "chat_id": _get(item, "chat_id", default=chat_id),
            "chat_type": "p2p",
            "msg_type": msg_type,
            "text": extracted["text"],
            "image_key": extracted["image_key"],
            "file_key": extracted["file_key"],
            "file_name": extracted["file_name"],
            "open_id": open_id,
            "parent_id": _get(item, "parent_id"),
        }

    def list_recent_messages(self, chat_id: str, page_size: int = 50) -> list[dict]:
        """拉取指定会话最近的历史消息（按时间从旧到新），用于断线重连后的补拉。

        飞书长连接不补发断线期间的消息，靠这个找回。失败抛 RuntimeError。
        """
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(page_size)
            .build()
        )
        resp = self._client.im.v1.message.list(request)
        if not resp.success():
            raise RuntimeError(f"拉取历史消息失败: code={resp.code} msg={resp.msg}")
        items = (resp.data.items or []) if resp.data else []
        # API 返回新→旧，反转为旧→新，方便按序写入收件箱
        return [self._normalize_history(it, chat_id) for it in reversed(items)]

    def get_message(self, message_id: str) -> dict:
        """按 message_id 取单条消息，返回归一化后的消息 dict（与 _normalize 同形）。

        用途：引用/回复场景——事件只带 parent_id（被引用消息 id），
        用这个接口把被引用的原文取回来拼进上下文。
        失败抛 RuntimeError（含飞书 code/msg，缺权限时按 code 处理）。
        """
        request = (
            GetMessageRequest.builder()
            .message_id(message_id)
            .build()
        )
        resp = self._client.im.v1.message.get(request)
        if not resp.success():
            raise RuntimeError(f"获取单条消息失败: code={resp.code} msg={resp.msg}")
        items = (resp.data.items or []) if resp.data else []
        if not items:
            raise RuntimeError(f"获取单条消息失败: 消息不存在 (message_id={message_id})")
        item = items[0]
        body = _get(item, "body", default=None)
        msg_type = _get(item, "msg_type")
        extracted = _extract_content(msg_type, _get(body, "content", default="{}"))
        sender = _get(item, "sender", default=None)
        return {
            "message_id": _get(item, "message_id"),
            "chat_id": _get(item, "chat_id"),
            "msg_type": msg_type,
            "text": extracted["text"],
            "image_key": extracted["image_key"],
            "file_key": extracted["file_key"],
            "file_name": extracted["file_name"],
            "open_id": _get(sender, "id") if _get(sender, "sender_type") == "user" else "",
            "parent_id": _get(item, "parent_id"),
        }

    def send_text(self, chat_id: str, text: str) -> str:
        """往指定会话发文本消息，返回飞书 message_id。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.create(request)
        if not resp.success():
            raise RuntimeError(f"飞书发送失败: code={resp.code} msg={resp.msg}")
        return resp.data.message_id

    def add_reaction(self, message_id: str, emoji_type: str = "Get") -> str:
        """给一条消息加表情回复（Get=已收到，DONE=处理完），返回 reaction_id。

        emoji_type 大小写敏感（本项目约定：Get 首字母大写，DONE 全大写）。
        失败抛 RuntimeError；表情是"尽力而为"的反馈，不应阻塞主流程。
        """
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message_reaction.create(request)
        if not resp.success():
            raise RuntimeError(f"加表情失败: code={resp.code} msg={resp.msg}")
        return resp.data.reaction_id

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """删除之前加的表情回复（需要 add_reaction 返回的 reaction_id）。"""
        request = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = self._client.im.v1.message_reaction.delete(request)
        if not resp.success():
            raise RuntimeError(f"删表情失败: code={resp.code} msg={resp.msg}")

    # 附件下载上限：单文件超过此值拒绝下载，防止磁盘被撑爆
    MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

    @staticmethod
    def _read_download(resp, kind: str, key: str) -> tuple[bytes, str]:
        """从下载响应里读出 (bytes, file_name)，带大小上限保护。"""
        if not resp.success():
            raise RuntimeError(f"下载{kind}失败: code={resp.code} msg={resp.msg}")
        if resp.file is None:
            raise RuntimeError(f"下载{kind}失败: 响应无文件内容 (key={key})")
        chunks = []
        total = 0
        while True:
            chunk = resp.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > FeishuBridge.MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"下载{kind}失败: 文件超过 {FeishuBridge.MAX_DOWNLOAD_BYTES // 1024 // 1024}MB 上限 (key={key})"
                )
            chunks.append(chunk)
        name = resp.file_name or f"{key}.bin"
        return b"".join(chunks), name

    def download_message_resource(self, message_id: str, key: str,
                                  resource_type: str) -> tuple[bytes, str]:
        """按 (message_id, file_key/image_key) 下载消息中的资源，返回 (文件字节, 文件名)。

        飞书规定旧接口 im/v1/images|files/:key 只能下载应用自己上传的资源，
        用户发来的附件必报 234008（The app is not the resource sender）；
        必须走消息资源接口 im/v1/messages/:message_id/resources/:file_key
        （需要应用开通 im:resource 权限），resource_type 只能是 image 或 file。
        失败抛 RuntimeError。
        """
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(key)
            .type(resource_type)
            .build()
        )
        resp = self._client.im.v1.message_resource.get(request)
        return self._read_download(resp, {"image": "图片"}.get(resource_type, "文件"), key)

    def download_image(self, image_key: str, message_id: str = "") -> tuple[bytes, str]:
        """按 image_key 下载图片，返回 (文件字节, 文件名)。失败抛 RuntimeError。

        message_id 传入时优先走消息资源接口（用户发来的图片）；
        失败回退旧接口（应用自己上传的图片）。
        """
        if message_id:
            try:
                return self.download_message_resource(message_id, image_key, "image")
            except RuntimeError as e:
                print(f"消息资源接口下载图片失败，回退旧接口: {e}")
        request = GetImageRequest.builder().image_key(image_key).build()
        resp = self._client.im.v1.image.get(request)
        return self._read_download(resp, "图片", image_key)

    def download_file(self, file_key: str, message_id: str = "") -> tuple[bytes, str]:
        """按 file_key 下载文件，返回 (文件字节, 文件名)。失败抛 RuntimeError。

        message_id 传入时优先走消息资源接口（用户发来的文件/语音/视频）；
        失败回退旧接口（应用自己上传的文件）。
        """
        if message_id:
            try:
                return self.download_message_resource(message_id, file_key, "file")
            except RuntimeError as e:
                print(f"消息资源接口下载文件失败，回退旧接口: {e}")
        request = GetFileRequest.builder().file_key(file_key).build()
        resp = self._client.im.v1.file.get(request)
        return self._read_download(resp, "文件", file_key)
