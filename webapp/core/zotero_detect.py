# -*- coding: utf-8 -*-
"""自动探测 Zotero 数据目录与程序路径。

只读：不写任何配置、不改动 Zotero 任何文件。探测不中时返回空，由界面提示用户手填。

数据目录判据：目录里能看到 storage/ 或 zotero.sqlite。
来源优先级：
  1) 各 profile 的 prefs.js 里 extensions.zotero.dataDir（用户自定义过就在这）
  2) %USERPROFILE%\\Zotero（Zotero 默认位置）
程序路径来源优先级：
  1) 注册表 App Paths\\zotero.exe（跟随安装位置，最稳）
  2) %ProgramFiles% / %ProgramFiles(x86)% / %LOCALAPPDATA% 下的常见位置
  3) PATH 里的 zotero
"""
import glob
import os
import re
import shutil


def _norm(p):
    """展开环境变量与引号，规范成绝对路径。"""
    if not p:
        return ""
    p = os.path.expandvars(str(p).strip().strip('"').strip("'"))
    return os.path.normpath(p) if p else ""


def looks_like_data_dir(p):
    """Zotero 数据目录的判据：含 storage/ 或 zotero.sqlite。供界面校验用户手填的路径。"""
    return bool(p) and os.path.isdir(p) and (
        os.path.isdir(os.path.join(p, "storage"))
        or os.path.exists(os.path.join(p, "zotero.sqlite")))


def profile_dirs():
    """Zotero profile 目录候选（Zotero 7 与更早版本的两种布局）。"""
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return []
    out = []
    for root in (os.path.join(appdata, "Zotero", "Zotero", "Profiles"),
                 os.path.join(appdata, "Zotero", "Profiles")):
        out.extend(d for d in sorted(glob.glob(os.path.join(root, "*"))) if os.path.isdir(d))
    return out


def _data_dirs_from_prefs():
    """从 prefs.js 读 extensions.zotero.dataDir（未自定义时该键通常不存在）。"""
    hits = []
    for d in profile_dirs():
        try:
            with open(os.path.join(d, "prefs.js"), encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except Exception:
            continue
        for m in re.finditer(r'user_pref\(\s*"extensions\.zotero\.dataDir"\s*,\s*"((?:[^"\\]|\\.)*)"', txt):
            # prefs.js 里反斜杠是转义的（C:\\Users\\...），还原成单反斜杠
            hits.append(_norm(m.group(1).replace("\\\\", "\\")))
    return [h for h in hits if h]


def _exe_from_registry():
    """注册表 App Paths：跟随实际安装位置，用户装到非默认盘也能找到。"""
    try:
        import winreg
    except Exception:
        return ""
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\zotero.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\zotero.exe"),
    ):
        try:
            with winreg.OpenKey(root, sub) as k:
                v = winreg.QueryValueEx(k, None)[0]
            if v:
                return _norm(v)
        except Exception:
            continue
    return ""


def detect():
    """返回 {data_dir: {path, candidates}, zotero_exe: {path, candidates}}。

    candidates 只含通过校验的项，path 为推荐值（无则空串）。
    """
    data_cands = []
    for p in _data_dirs_from_prefs() + [_norm(os.path.join(os.environ.get("USERPROFILE") or "", "Zotero"))]:
        if p and p not in data_cands and looks_like_data_dir(p):
            data_cands.append(p)

    exe_cands = []
    for p in (_exe_from_registry(),
              _norm(os.path.join(os.environ.get("ProgramFiles") or "", "Zotero", "zotero.exe")),
              _norm(os.path.join(os.environ.get("ProgramFiles(x86)") or "", "Zotero", "zotero.exe")),
              _norm(os.path.join(os.environ.get("LOCALAPPDATA") or "", "Zotero", "zotero.exe")),
              _norm(shutil.which("zotero") or "")):
        if p and p not in exe_cands and os.path.isfile(p):
            exe_cands.append(p)

    return {
        "data_dir": {"path": data_cands[0] if data_cands else "", "candidates": data_cands},
        "zotero_exe": {"path": exe_cands[0] if exe_cands else "", "candidates": exe_cands},
    }
