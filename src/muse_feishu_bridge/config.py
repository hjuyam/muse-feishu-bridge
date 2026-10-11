"""配置加载：config.yaml + 环境变量覆盖（环境变量优先）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str


@dataclass
class BridgeConfig:
    inbox_path: Path
    poll_interval_sec: int = 20
    allow_open_ids: list[str] = field(default_factory=list)


@dataclass
class Config:
    feishu: FeishuConfig
    bridge: BridgeConfig


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {path}，请先 cp config.example.yaml config.yaml 并填写"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    feishu_raw = raw.get("feishu", {})
    app_id = os.environ.get("FEISHU_APP_ID", feishu_raw.get("app_id", "")).strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", feishu_raw.get("app_secret", "")).strip()
    if not app_id or not app_secret:
        raise ValueError("缺少飞书凭证：请在 config.yaml 或环境变量中提供 app_id / app_secret")

    bridge_raw = raw.get("bridge", {})
    return Config(
        feishu=FeishuConfig(app_id=app_id, app_secret=app_secret),
        bridge=BridgeConfig(
            inbox_path=Path(bridge_raw.get("inbox_path", "./data/inbox.jsonl")),
            poll_interval_sec=int(bridge_raw.get("poll_interval_sec", 20)),
            allow_open_ids=list(bridge_raw.get("allow_open_ids", []) or []),
        ),
    )
