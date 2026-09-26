# -*- coding: utf-8 -*-
"""自动探测 Zotero 数据目录与程序路径。

只读：不写任何配置、不改动 Zotero 任何文件。探测不中时返回空，由界面提示用户手填。

数据目录判据：目录里能看到 storage/ 或 zotero.sqlite。
来源优先级：
  1) 各 profile 的 prefs.js 里 extensions.zotero.dataDir（用户自定义过就在这）
  2) %USERPROFILE%\\Zotero（Zotero 默认位置）
程序路径来源优先级：
  1) 注册表 App Paths\\zotero.exe
  2) 桌面 / 开始菜单里的 Zotero 快捷方式（.lnk，直接解析字节，不依赖第三方库）
  3) Windows MuiCache 里记录的用户实际运行过的路径
  4) 各盘根目录（升级时可能把文件丢在盘根）
  5) %ProgramFiles% / %ProgramFiles(x86)% / %LOCALAPPDATA% 下的常见位置
  6) PATH 里的 zotero

**为什么要这么多来源**：实测 Zotero 10 升级会把 exe 挪到非标准位置（甚至 D:\\zotero.exe），
同时清掉注册表 App Paths 键——只靠注册表会完全找不到。快捷方式与运行记录反而最可靠。
"""
import glob
import os
import re
import shutil


def _norm(p):
    """展开环境变量与引号，规范成绝对路径；不是绝对路径就返回空（避免误判相对路径）。"""
    if not p:
        return ""
    p = os.path.expandvars(str(p).strip().strip('"').strip("'"))
    if not p:
        return ""
    p = os.path.normpath(p)
    return p if os.path.isabs(p) else ""


def looks_like_data_dir(p):
    """Zotero 数据目录的判据：含 storage/ 或 zotero.sqlite。供界面校验用户手填的路径。"""
    return bool(p) and os.path.isdir(p) and (
        os.path.isdir(os.path.join(p, "storage"))
        or os.path.exists(os.path.join(p, "zotero.sqlite")))


def profile_dirs():
    """Zotero profile 目录候选（Zotero 7 及以后在 Zotero\\Zotero\\Profiles，更早版本少一层）。"""
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


def _exe_from_lnk(path):
    """从 .lnk 快捷方式里抠出目标 exe 路径（直接读字节，不依赖第三方库）。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return ""
    # 路径可能以 ANSI 明文存在（LinkInfo），也可能以 UTF-16LE 存在，两种都试
    for text in (raw.decode("latin-1", errors="ignore"),
                 raw.decode("utf-16-le", errors="ignore")):
        for m in re.finditer(r"([A-Za-z]:\\[^\x00\r\n\"<>|]{0,240}?[Zz]otero\.exe)", text):
            p = _norm(m.group(1))
            if p and os.path.isfile(p):
                return p
    return ""


def _exes_from_shortcuts():
    """桌面 / 开始菜单里的 Zotero 快捷方式。

    Zotero 升级时可能把程序挪到非标准位置、还会清掉注册表 App Paths，
    而快捷方式通常仍指着用户实际在用的那个 exe，是最可靠的兜底线索。
    """
    dirs = []
    for env, sub in (("USERPROFILE", "Desktop"),
                     ("PUBLIC", "Desktop"),
                     ("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                     ("ProgramData", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            dirs.append(os.path.join(base, sub))
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(".lnk") and "zotero" in fn.lower():
                    p = _exe_from_lnk(os.path.join(root, fn))
                    if p and p not in out:
                        out.append(p)
    return out


def _exes_from_muicache():
    """Windows 的 MuiCache 记录用户运行过的程序完整路径。

    键名形如 `<完整路径>.FriendlyAppName`，取 "zotero.exe" 之前那段即为安装路径。
    """
    try:
        import winreg
    except Exception:
        return []
    out = []
    try:
        key = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            n = winreg.QueryInfoKey(k)[1]
            for i in range(n):
                try:
                    name = winreg.EnumValue(k, i)[0]
                except Exception:
                    continue
                idx = name.lower().find("zotero.exe")
                if idx <= 2:          # 至少要像 "C:\...\zotero.exe"
                    continue
                cand = _norm(name[:idx + len("zotero.exe")])
                if cand and cand not in out and os.path.isfile(cand):
                    out.append(cand)
    except Exception:
        return []
    return out


def _exes_from_drive_roots():
    """盘根下的 zotero.exe。

    实测遇到过 Zotero 升级后把程序文件丢到盘根（安装目录反而只剩 updated\\），
    扫一遍各盘根目录成本极低，能捞到这种非标准布局。
    """
    out = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        p = _norm("%s:\\zotero.exe" % letter)
        if p and os.path.isfile(p):
            out.append(p)
    return out


def detect():
    """返回 {data_dir: {path, candidates}, zotero_exe: {path, candidates}}。

    candidates 只含通过校验的项，path 为推荐值（无则空串）。
    """
    data_cands = []
    for p in _data_dirs_from_prefs() + [_norm(os.path.join(os.environ.get("USERPROFILE") or "", "Zotero"))]:
        if p and p not in data_cands and looks_like_data_dir(p):
            data_cands.append(p)

    # 优先找"用户实际在用的那个"：注册表 → 快捷方式 → 运行记录 → 盘根 → 默认安装位置
    exe_cands = []
    for p in ([_exe_from_registry()]
              + _exes_from_shortcuts()
              + _exes_from_muicache()
              + _exes_from_drive_roots()
              + [_norm(os.path.join(os.environ.get("ProgramFiles") or "", "Zotero", "zotero.exe")),
                 _norm(os.path.join(os.environ.get("ProgramFiles(x86)") or "", "Zotero", "zotero.exe")),
                 _norm(os.path.join(os.environ.get("LOCALAPPDATA") or "", "Zotero", "zotero.exe")),
                 _norm(shutil.which("zotero") or "")]):
        if p and p not in exe_cands and os.path.isfile(p):
            exe_cands.append(p)

    return {
        "data_dir": {"path": data_cands[0] if data_cands else "", "candidates": data_cands},
        "zotero_exe": {"path": exe_cands[0] if exe_cands else "", "candidates": exe_cands},
    }
