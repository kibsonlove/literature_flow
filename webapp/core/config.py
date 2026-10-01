# -*- coding: utf-8 -*-
"""本地配置读写（密钥 / 模型 / 本机路径）。仅存本机，明文，勿提交 git。

三个文件都在 config/ 下，界面「设置」与手工编辑等价：
  settings.json   —— 大模型 API Key / 模型 / Base URL、OpenAlex Key
  paths.json      —— Zotero 数据目录、Zotero 程序路径、写入授权密钥
  embedding.json  —— 知识库 / 语义检索用的向量模型

本机路径一律走下面的函数（而非模块级常量）读取：用户在界面里改完即可生效，
不必重启服务，也不会出现「改了配置没反应」的假象。
"""
import json
import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
PATHS_PATH = os.path.join(CONFIG_DIR, "paths.json")
EMBEDDING_PATH = os.path.join(CONFIG_DIR, "embedding.json")

# 等待 Zotero 本地 API 就绪的最长秒数
ZOTERO_WAIT_SECONDS = 60

DEFAULTS = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
}

EMBEDDING_DEFAULTS = {
    "provider": "siliconflow",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "BAAI/bge-m3",
}

# 精读（网页端）的「挂载确认 / 卡住检测」阈值规格。
#   返回键 -> (settings.json 里的键, 默认值, 下限, 上限)  单位：秒
_PDF_WAIT_SPEC = {
    "idle_timeout":             ("pdf_idle_timeout",             240,  30,  7200),
    "idle_timeout_unconfirmed": ("pdf_idle_timeout_unconfirmed",  90,  30,  3600),
    "upload_wait":              ("pdf_upload_wait",               20,   5,   300),
    "job_timeout":              ("pdf_job_timeout",             1800,  60, 21600),
}


def _placeholder(v):
    """是否为"未填写"的占位值：空串、纯空白，或模板里残留的中文提示语。
    后者若不识别，会让界面误显示"已配置密钥"。"""
    s = (v or "").strip()
    if not s:
        return True
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _mask(key):
    return ("*" * max(0, len(key) - 4)) + key[-4:] if len(key) > 4 else "****"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _write_json(path, d):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 通用设置

def load_settings():
    d = dict(DEFAULTS)
    d.update(_read_json(SETTINGS_PATH))
    d.setdefault("model", DEFAULTS["model"])
    d.setdefault("base_url", DEFAULTS["base_url"])
    key = (d.get("api_key") or "").strip()
    d["has_key"] = not _placeholder(key)
    if d["has_key"]:
        d["api_key_masked"] = _mask(key)
    oa = (d.get("openalex_key") or "").strip()
    d["has_openalex_key"] = not _placeholder(oa)
    if d["has_openalex_key"]:
        d["openalex_key_masked"] = _mask(oa)
    return d


def openalex_key():
    """OpenAlex API Key：优先环境变量 OPENALEX_API_KEY，其次 settings.json。

    免费注册即得（openalex.org/settings/api，约 30 秒），每日额度从 $0.1 提到 $1（10 倍）。
    没有 key 也能正常检索，只是额度低——所以调用方必须允许它为空。
    """
    k = (os.environ.get("OPENALEX_API_KEY") or "").strip()
    if k:
        return k
    k = (_read_json(SETTINGS_PATH).get("openalex_key") or "").strip()
    return "" if _placeholder(k) else k


def pdf_wait_timeouts():
    """精读（网页端）的挂载确认与卡住检测阈值（秒）。

    返回 {idle_timeout, idle_timeout_unconfirmed, upload_wait, job_timeout}。
    缺省用 _PDF_WAIT_SPEC 的默认值；范围夹取，避免填 0 导致「永不判卡住」。
    用函数读而非模块级常量：界面里改完即生效，无需重启。
    """
    d = _read_json(SETTINGS_PATH)
    out = {}
    for name, (key, dv, lo, hi) in _PDF_WAIT_SPEC.items():
        try:
            v = int(float(str(d.get(key, "")).strip()))
        except Exception:
            v = dv
        out[name] = max(lo, min(hi, v))
    return out


def research_question():
    """用户的研究课题 / 研究问题（精读笔记里「与我课题的相关性」拿它做对照）。

    存 settings.json 的 research_question 字段；未填返回空串——调用方必须保留"占位说明"
    的行为，**绝不能让模型凭空推测相关性**（那是编造）。
    """
    return (_read_json(SETTINGS_PATH).get("research_question") or "").strip()


def pdf_wait_raw():
    """settings.json 里**用户显式填过**的精读阈值（空串 = 用默认，由 pdf_wait_timeouts 兜底）。

    界面回填用这个而不是生效值：否则"清空输入框想恢复默认"会看起来像改了没反应。
    """
    d = _read_json(SETTINGS_PATH)
    out = {}
    for name, (key, _dv, _lo, _hi) in _PDF_WAIT_SPEC.items():
        v = d.get(key)
        out[name] = "" if v is None else v
    return out


def save_settings(api_key=None, model=None, base_url=None, openalex_key=None, pdf_wait=None,
                  research_question=None):
    d = _read_json(SETTINGS_PATH)
    for k in ("has_key", "api_key_masked", "has_openalex_key", "openalex_key_masked"):
        d.pop(k, None)
    if api_key is not None:
        d["api_key"] = api_key.strip()
    if model is not None:
        d["model"] = model.strip() or DEFAULTS["model"]
    if base_url is not None:
        d["base_url"] = base_url.strip() or DEFAULTS["base_url"]
    if openalex_key is not None:
        d["openalex_key"] = openalex_key.strip()
    if research_question is not None:
        # 空串 = 清空课题，回到占位说明（不是"忽略"）
        d["research_question"] = research_question.strip()
    if pdf_wait:
        for name, (key, dv, lo, hi) in _PDF_WAIT_SPEC.items():
            raw = pdf_wait.get(name)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                d[key] = max(lo, min(hi, int(float(str(raw).strip()))))
            except Exception:
                continue
    _write_json(SETTINGS_PATH, d)
    return load_settings()


# ---------------------------------------------------------------- 本机路径

_auto_cache = {}


def load_paths():
    """读取 config/paths.json 的原始内容（不含兜底与探测）。"""
    return _read_json(PATHS_PATH)


def save_paths(**fields):
    """只覆盖传入的字段，其余保持原样（含模板里的 _comment）。"""
    d = load_paths()
    for k, v in fields.items():
        if v is None:
            continue
        d[k] = v.strip() if isinstance(v, str) else v
    _write_json(PATHS_PATH, d)
    _auto_cache.clear()
    return d


def _detect(key):
    if key not in _auto_cache:
        try:
            from . import zotero_detect
            _auto_cache[key] = zotero_detect.detect()[key].get("path") or ""
        except Exception:
            _auto_cache[key] = ""
    return _auto_cache[key]


def zotero_data_dir():
    """Zotero 数据目录（含 storage/ 与 zotero.sqlite 的文件夹）。

    未配置时回退到自动探测结果（只读，不写文件），尽量做到开箱可用。
    """
    v = (load_paths().get("zotero_data_dir") or "").strip()
    return v or _detect("data_dir")


def zotero_exe():
    """Zotero 程序路径；未配置时回退自动探测。"""
    v = (load_paths().get("zotero_exe") or "").strip()
    return v or _detect("zotero_exe")


def data_dir():
    """数据目录：缓存、向量库、MinerU 解析结果、浏览器内核等**可再生数据**都放这里。

    默认在项目内（webapp/cache）——删掉项目目录就全清干净，不留孤儿文件。
    想省系统盘空间或换到大容量磁盘时，可在页面「设置 → 本机路径」改到别处。
    """
    v = (load_paths().get("data_dir") or "").strip()
    if v:
        p = os.path.normpath(os.path.expandvars(v))
        if os.path.isabs(p):
            return p
    return os.path.join(os.path.dirname(CONFIG_DIR), "cache")


def data_path(*parts):
    """数据目录下的某个文件 / 子目录。"""
    return os.path.join(data_dir(), *parts)


def playwright_browsers_dir():
    """浏览器内核目录：环境变量 > 设置里的路径 > 数据目录下的默认位置。"""
    v = (os.environ.get("PLAYWRIGHT_BROWSERS_DIR")
         or (load_paths().get("playwright_browsers_dir") or "").strip())
    if v:
        p = os.path.normpath(os.path.expandvars(v))
        if os.path.isabs(p):
            return p
    return os.path.normpath(data_path("playwright_browsers"))


def webchat_profile_dir():
    """网页端登录资料目录（存 cookie / 登录态）：优先级同上。"""
    v = (os.environ.get("WEBCHAT_PROFILE_DIR")
         or (load_paths().get("webchat_profile_dir") or "").strip())
    if v:
        p = os.path.normpath(os.path.expandvars(v))
        if os.path.isabs(p):
            return p
    return os.path.normpath(data_path("webchat_profile"))


# ---------------------------------------------------------------- 向量模型（知识库）

def load_embedding():
    d = dict(EMBEDDING_DEFAULTS)
    d.update(_read_json(EMBEDDING_PATH))
    key = (d.get("api_key") or "").strip()
    d["has_key"] = not _placeholder(key)
    if d["has_key"]:
        d["api_key_masked"] = _mask(key)
    return d


def save_embedding(api_key=None, base_url=None, model=None, provider=None):
    d = _read_json(EMBEDDING_PATH)
    for k in ("has_key", "api_key_masked"):
        d.pop(k, None)
    if api_key is not None:
        d["api_key"] = api_key.strip()
    if base_url is not None:
        d["base_url"] = base_url.strip() or EMBEDDING_DEFAULTS["base_url"]
    if model is not None:
        d["model"] = model.strip() or EMBEDDING_DEFAULTS["model"]
    if provider is not None:
        d["provider"] = provider.strip() or EMBEDDING_DEFAULTS["provider"]
    _write_json(EMBEDDING_PATH, d)
    return load_embedding()
