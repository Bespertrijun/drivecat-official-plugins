"""
通知系统 — 配置存储。

配置通过 FileProxy 持久化到 plugin_data/{plugin_id}/config.json。
结构（默认值即 DEFAULT_CONFIG）：

    {
      "channels": {
        "telegram": {
          "enabled": false,
          "bot_token": "",
          "chat_id": "",
          "parse_mode": "HTML"
        }
      },
      "events": {
        "after_upload": true,
        "after_sync":   true,
        "on_error":     true,
        "on_startup":   false
      }
    }

新增渠道时，只需在 channels 下加一段，并在 notify_channels.REGISTRY 注册对应类。
"""

import copy
import json
from typing import Any, Dict

CONFIG_FILE = "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "channels": {
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "parse_mode": "HTML",
        },
    },
    "events": {
        "after_upload": True,
        "after_sync": True,
        "on_error": True,
        "on_startup": False,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 递归合并进 base（就地修改并返回 base）。"""
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(fs) -> Dict[str, Any]:
    """读取配置；缺失/损坏时回落到默认配置（并补全缺省字段）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if fs.exists(CONFIG_FILE):
            saved = json.loads(fs.read_text(CONFIG_FILE))
            if isinstance(saved, dict):
                _deep_merge(cfg, saved)
    except Exception:
        # 配置损坏不应让插件崩溃——退回默认值即可
        pass
    return cfg


def save_config(fs, new_config: Dict[str, Any]) -> Dict[str, Any]:
    """把新配置合并进默认结构后写盘，返回落盘后的完整配置。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    _deep_merge(cfg, new_config or {})
    fs.write_text(
        CONFIG_FILE,
        json.dumps(cfg, ensure_ascii=False, indent=2),
    )
    return cfg
