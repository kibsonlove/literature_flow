# -*- coding: utf-8 -*-
"""本地设置（API key / 模型）存储。仅存本机，明文，勿提交 git。"""
import json
import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

# 本机路径（Zotero 数据目录 / 程序）存于 config/paths.json（本地文件，不入库 git）
_paths = {}
try:
    _paths = json.load(open(os.path.join(CONFIG_DIR, "paths.json"), encoding="utf-8"))
except Exception:
    _paths = {}
ZOTERO_DATA_DIR = _paths.get("zotero_data_dir", "")
ZOTERO_EXE = _paths.get("zotero_exe", "")

# 等待 Zotero 本地 API 就绪的最长秒数
ZOTERO_WAIT_SECONDS = 60

DEFAULTS = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
}


def _placeholder(v):
    """是否为"未填写"的占位值：空串、纯空白，或模板里残留的中文提示语。
    后者若不识别，会让界面误显示"已配置密钥"。"""
    s = (v or "").strip()
    if not s:
        return True
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        d = dict(DEFAULTS)
    else:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = dict(DEFAULTS)
    d.setdefault("model", DEFAULTS["model"])
    d.setdefault("base_url", DEFAULTS["base_url"])
    key = (d.get("api_key") or "").strip()
    d["has_key"] = not _placeholder(key)
    if d["has_key"]:
        d["api_key_masked"] = ("*" * max(0, len(key) - 4)) + key[-4:] if len(key) > 4 else "****"
    return d


def save_settings(api_key=None, model=None, base_url=None):
    d = load_settings()
    d.pop("has_key", None)
    d.pop("api_key_masked", None)
    if api_key is not None:
        d["api_key"] = api_key
    if model is not None:
        d["model"] = model
    if base_url is not None:
        d["base_url"] = base_url
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return load_settings()
