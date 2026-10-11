"""muse-feishu-bridge：把 Meta 个人助理 Muse 接入飞书 / Lark 的传输层。

智能在 Muse 那一侧：本包只负责「收飞书消息 → 写收件箱」和
「把 Muse 的回复发回飞书」。Muse 自己轮询收件箱、自己处理、自己回。
"""

__version__ = "0.1.0"
