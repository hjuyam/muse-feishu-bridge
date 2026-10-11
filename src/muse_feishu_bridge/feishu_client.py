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
    CreateMessageRequest,
    CreateMessageRequestBody,
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
        """阻塞运行长连接（内部自动重连）。"""
        self._ws.start()

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
