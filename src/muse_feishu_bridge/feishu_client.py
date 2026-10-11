"""飞书传输层：官方 lark-oapi SDK。

- 收：WebSocket 长连接（无需公网 URL），收到「发给机器人的消息」事件后归一化，
  交给调用方（cli.listen 会写入收件箱）。
- 发：HTTP API 发文本消息回指定会话。

已对照 lark-oapi 1.7.x 的 ws.Client / EventDispatcherHandler / im.v1.message
编写，未经真实飞书联调，首次使用请先跑 `python -m muse_feishu_bridge.cli doctor`。
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
    ListMessageRequest,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

MessageHandler = Callable[[dict], None]


def _get(obj, *attrs, default=""):
    for a in attrs:
        obj = getattr(obj, a, None)
        if obj is None:
            return default
    return obj if obj != "" else default


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
        self._ws = lark.ws.Client(
            app_id, app_secret, event_handler=dispatcher, log_level=log_level
        )

    @staticmethod
    def _normalize(data: P2ImMessageReceiveV1) -> dict:
        """把 SDK 事件转成 inbox.py 的消息格式。"""
        event = _get(data, "event", default=None)
        msg = _get(event, "message", default=None)
        sender = _get(event, "sender", default=None)
        sender_id = _get(sender, "sender_id", default=None)
        raw_content = _get(msg, "content", default="{}")
        try:
            text = json.loads(raw_content).get("text", "")
        except (json.JSONDecodeError, AttributeError):
            text = ""
        return {
            "message_id": _get(msg, "message_id"),
            "chat_id": _get(msg, "chat_id"),
            "chat_type": _get(msg, "chat_type"),
            "msg_type": _get(msg, "message_type"),
            "text": text,
            "open_id": _get(sender_id, "open_id"),
        }

    def start_listening(self) -> None:
        """阻塞运行长连接。注意：SDK 在某些断线场景（如 no close frame）会直接返回，
        不会自动重连——调用方（cli listen）负责捕获返回/异常并重连。"""
        self._ws.start()

    @staticmethod
    def _normalize_history(item, chat_id: str) -> dict:
        """把「获取历史消息」API 的 Message 转成 inbox 格式。

        注意：历史 API 的 Message 模型字段名与事件推送不同（msg_type 而非
        message_type），不要复用 _normalize。
        """
        sender = _get(item, "sender", default=None)
        body = _get(item, "body", default=None)
        raw_content = _get(body, "content", default="{}")
        try:
            text = json.loads(raw_content).get("text", "")
        except (json.JSONDecodeError, AttributeError):
            text = ""
        # 只取 user 发的消息；bot 自己的回复 sender_type 为 app，会被上层白名单滤掉
        open_id = _get(sender, "id") if _get(sender, "sender_type") == "user" else ""
        return {
            "message_id": _get(item, "message_id"),
            "chat_id": _get(item, "chat_id", default=chat_id),
            "chat_type": "p2p",
            "msg_type": _get(item, "msg_type"),
            "text": text,
            "open_id": open_id,
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

        emoji_type 大小写敏感（飞书官方枚举：Get 全小写 get、DONE 全大写）。
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
