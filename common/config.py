"""项目配置：读取项目根目录的 config.json，缺失的项使用默认值。"""

import copy
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
# 运行时产生的文件统一放在 runtime/ 下（已加入 .gitignore）
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LINKS_FILE = RUNTIME_DIR / "input" / "links.txt"              # 01 最近一次抓取的记录；可手动添加
STATE_DIR = RUNTIME_DIR / "state"                             # threads.json
REPORTS_DIR = RUNTIME_DIR / "reports"                         # 便于阅读的导出与报告
CHROME_PROFILE_DIR = RUNTIME_DIR / "profiles" / "chrome_profile"          # 自动化浏览器（保存登录状态）
DEBUG_PROFILE_DIR = RUNTIME_DIR / "profiles" / "browser_data_debug"       # 01 读取标签页用的调试浏览器

DEFAULTS: dict = {
    "paths": {
        "baidu_download": "D:/BaiduNetdiskDownload",
        "mp4_dir": "D:/Add mp4",
    },
    "browser": {
        "chromedriver_path": "chromedriver.exe",
        "page_load_timeout": 25,
    },
    "save_to_panbaidu": {
        "target_folder": "nztd",
    },
    "extractor": {
        "seven_zip_path": "C:/Software/7-Zip/7z.exe",
        "file_extensions": [".7z", ".zip", ".rar"],
        "fallback_passwords": ["www.zuxiashiguang.com"],
        "delete_after_extract": False,
    },
    "rename": {
        "video_extensions": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".ts", ".flv", ".m4v"],
    },
}


def _merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    config = copy.deepcopy(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            _merge(config, json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"⚠️ 读取 {CONFIG_FILE} 失败，使用默认配置: {e}")
    return config


_config = None


def get(path: str, default: Any = None) -> Any:
    """按 "a.b.c" 路径取配置值。"""
    global _config
    if _config is None:
        _config = load_config()
    node: Any = _config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
