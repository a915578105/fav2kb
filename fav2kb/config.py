"""路径与配置。

程序数据统一放在用户目录（与安装方式无关）：
    ~/.fav2kb/browser/       登录态（内含 Cookie，绝不外传）
    ~/.fav2kb/raw/           采集到的列表与媒体直链
    ~/.fav2kb/models/        语音转写模型
    ~/.fav2kb/config.json    默认知识库位置

每个知识库自带工具目录 <知识库>/_fav2kb/：
    config.json      同步哪些收藏夹、是否转写、转写后是否保留音频（CLI 读）
    preferences.md   用户的整理偏好（agent 读写，CLI 不解析）
    manifest.json    每条作品的状态
    INDEX.md, items/ 原始素材
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("FAV2KB_HOME") or Path.home() / ".fav2kb")
PROFILE = HOME / "browser"
RAW_DIR = HOME / "raw"
MODEL_DIR = HOME / "models"
USER_CONFIG = HOME / "config.json"

TOOL_DIR = "_fav2kb"


class CaptureError(RuntimeError):
    """采集阶段的可预期失败（未登录、安全验证、找不到收藏夹等）。"""


def write_atomic(path: Path, text: str) -> None:
    """先写临时文件再替换，中途崩溃不会留下半个文件。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))


def resolve_kb(kb: Path | None) -> Path:
    """命令行指定的知识库，否则用 init 记下的默认知识库。"""
    if kb is None:
        default = read_json(USER_CONFIG, {}).get("default_kb")
        if not default:
            raise SystemExit("还没有知识库。先运行：fav2kb init --kb <目录> …")
        kb = Path(default)
    if not (kb / TOOL_DIR / "config.json").exists():
        raise SystemExit(f"{kb} 不是 fav2kb 知识库。先运行：fav2kb init --kb {kb} …")
    return kb


def material_dir(kb: Path) -> Path:
    return kb / TOOL_DIR


def load_kb_config(kb: Path) -> dict:
    return read_json(material_dir(kb) / "config.json", {})


def init_kb(kb: Path, folders: list[str] | None, asr: bool, keep_audio: bool = True) -> Path:
    """创建或更新知识库配置，并设为默认知识库。folders 为 None 表示全部。"""
    kb = kb.resolve()
    config = {"folders": folders, "asr": asr, "keep_audio": keep_audio}
    write_json(material_dir(kb) / "config.json", config)
    user = read_json(USER_CONFIG, {})
    user["default_kb"] = str(kb)
    write_json(USER_CONFIG, user)
    return kb
