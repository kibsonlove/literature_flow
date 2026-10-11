# -*- coding: utf-8 -*-
"""literature_flow 一键卸载器（在 App 之外运行）。

它把「本项目下载过 / 缓存过」的东西清掉，清多少就报多少。分三层：

  【会删】项目内再生缓存（webapp/cache、logs、__pycache__）
  【会删】setup.bat 建的虚拟环境（webapp/venv / .venv）
  【会删】setup.bat 默认位置的浏览器内核（%LOCALAPPDATA%\\ms-playwright）
  【会删】本项目生成的网页端登录资料（paths.json 的 webchat_profile_dir）
  【会删】setup.bat 触发的 pip 下载缓存（%LOCALAPPDATA%\\pip）
  【会删】真实配置与密钥（先备份到项目内，再删除到回收站）
  【会删】setup.bat 用 winget 装出来的系统 Python（可选，默认勾选）
  【只报告】共用资产：别人/别的项目下载的浏览器内核、WorkBuddy 共享 Python 环境
  【只报告】你的成果：notebook / reports / 源码 / 期刊分区表 → 一律保留

安全约束（写死在代码里，不随菜单放开）：
  * 绝不删除盘符根、Home、AppData、项目根、.git —— 命中即拒绝
  * 缓存/内核/venv 走**永久删除**；配置/登录资料走**回收站**（可恢复）
  * 任何删除动作前必须先看到完整清单，并手工输入确认词

用法：
    python uninstall.py            # 菜单式：先扫描，再勾选，最后确认
    python uninstall.py --list     # 只扫描打印，不动任何文件
    python uninstall.py --yes      # 用默认勾选直接执行（仍会打印清单）
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))      # webapp/
ROOT = os.path.dirname(HERE)                           # 项目根
HOME = os.path.expanduser("~")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")

CONFIRM_WORD = "DELETE"
BACKUP_DIR = os.path.join(HERE, "_uninstall_backup")

# ----------------------------------------------------------------- 安全护栏
FORBIDDEN = {os.path.normcase(os.path.normpath(os.path.abspath(p))) for p in (
    os.path.splitdrive(ROOT)[0] + "\\",                   # 盘符根
    os.path.join(os.path.splitdrive(ROOT)[0] + "\\", "Windows"),
    "C:\\Program Files", "C:\\Program Files (x86)",
    os.path.join(HOME, "AppData"), HOME,
    LOCALAPPDATA, APPDATA,
    ROOT, os.path.join(ROOT, ".git"), HERE,
)}


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def is_forbidden(p):
    """这些位置一律不许删——防手滑，不提供任何绕过开关。"""
    n = _norm(p)
    if n in FORBIDDEN:
        return True
    _drive, tail = os.path.splitdrive(n)
    return tail in ("\\", "/", "")            # 盘符根


# ----------------------------------------------------------------- 基础工具
def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.1f %s" % (n, unit))
        n /= 1024.0


def du(path):
    """目录/文件体积（按磁盘实际占用算，硬链接只计一次）；权限不足的条目跳过，不炸。"""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        if not os.path.isdir(path):
            return 0
    except OSError:
        return 0
    total = 0
    seen = set()                              # pip 的 wheels/http 缓存靠硬链接复用，重复计数会虚高
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if st.st_ino and key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def load_paths():
    try:
        with open(os.path.join(HERE, "config", "paths.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def running_inside(path):
    """当前解释器是否就位于该目录内（会锁住文件，导致删不干净）。"""
    exe = _norm(sys.executable)
    return exe.startswith(_norm(path) + os.sep)


# ----------------------------------------------------------------- 删除动作
def delete_permanent(path):
    """永久删除（缓存/venv/内核用）。返回 None=成功，否则错误说明。"""
    p = os.path.abspath(path)
    if not os.path.exists(p):
        return None
    if is_forbidden(p):
        return "安全护栏拒绝删除：%s" % p
    target = p
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        target = "\\\\?\\" + p                # 绕开 MAX_PATH，浏览器/venv 目录很深
    try:
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(target)
            return None
        errs = []

        def _onerr(func, pth, _exc):
            try:
                os.chmod(pth, stat.S_IWRITE)
                func(pth)
            except Exception as e:            # noqa: BLE001
                errs.append("%s :: %s" % (pth, e))

        shutil.rmtree(target, onerror=_onerr)
        if os.path.exists(p):                 # 复核：还有残留就明确报出来，不假装成功
            errs.append("%s :: 删除后仍有残留" % p)
        return "\n".join(errs) if errs else None
    except Exception as e:                    # noqa: BLE001
        return "%s :: %s" % (p, e)


# ----------------------------------------------------------------- 回收站（原生 API）
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    _FO_DELETE = 3
    _FOF_SILENT = 0x0004
    _FOF_NOCONFIRMATION = 0x0010
    _FOF_ALLOWUNDO = 0x0040            # 关键：可撤销 = 进回收站
    _FOF_NOERRORUI = 0x0400


def _recycle_one(path):
    """把单个文件/目录送进回收站（可恢复），全程无弹窗。返回 None=成功。

    用 Windows 原生 SHFileOperationW 而不是 PowerShell 的
    Microsoft.VisualBasic.FileIO.FileSystem：
      * PowerShell 那条路在删不掉时会弹模态错误框，无人值守时会把脚本挂住；
      * 它还会在「已经成功移进回收站」之后再抛一次 "Unable to find the specified file"。
    标志位说明：ALLOWUNDO=进回收站，NOCONFIRMATION/SILENT/NOERRORUI=一次都不弹窗。
    """
    src = os.path.abspath(path)
    if not os.path.exists(src):
        return None
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = src + "\0\0"                   # SHFileOperation 要求双 NUL 结尾
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return "SHFileOperation 返回码 %d" % rc
    if op.fAnyOperationsAborted:
        return "操作被中止"
    return None


def _recycle(paths):
    """批量送回收站。返回 {path: err}，只收录**确实还残留**的条目。"""
    paths = [os.path.abspath(p) for p in paths if os.path.exists(p)]
    if not paths:
        return {}
    if os.name != "nt":
        return {p: "非 Windows 环境，无法送回收站" for p in paths}
    errs = {}
    for p in paths:
        try:
            msg = _recycle_one(p)
        except Exception as e:                # noqa: BLE001
            msg = repr(e)[:200]
        if os.path.exists(p):                 # 以最终是否消失为准
            errs[p] = msg or "命令执行完后路径仍然存在"
    return errs


def delete_items(item, log):
    """按 item 的 mode 执行删除，返回 (释放字节, [错误])。"""
    freed, errs = 0, []
    paths = [p for p in item["paths"] if os.path.exists(p)]
    if not paths:
        return 0, []
    mode = item["mode"]
    if mode == "trash":
        # 先备份：备份失败就**不删**，宁可留着一个密钥文件，也不要静默丢掉
        if item.get("backup"):
            ok, info = backup(paths)
            if not ok:
                return 0, ["备份失败，已跳过删除：%s" % info]
            log("    已备份到 %s" % info)
        freed = sum(du(p) for p in paths)
        for p, msg in _recycle(paths).items():
            errs.append("%s :: %s" % (p, msg))
    else:                                     # perm
        for p in paths:
            size = du(p)
            msg = delete_permanent(p)
            if msg:
                errs.append(msg)
            else:
                freed += size
    return freed, errs


def backup(paths):
    """把配置/密钥按原相对结构复制到 webapp/_uninstall_backup/<时间戳>/。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, stamp)
    try:
        for p in paths:
            rel = os.path.relpath(os.path.abspath(p), ROOT)
            tgt = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            if os.path.isdir(p):
                shutil.copytree(p, tgt)
            else:
                shutil.copy2(p, tgt)
        return True, dest
    except Exception as e:                    # noqa: BLE001
        return False, "%s :: %s" % (dest, e)


# ----------------------------------------------------------------- 清单构建
def _pycache_targets():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT, onerror=lambda e: None):
        if ".git" in dirpath.split(os.sep):
            dirnames[:] = []
            continue
        for d in list(dirnames):
            if d == "__pycache__":
                out.append(os.path.join(dirpath, d))
        for fn in filenames:
            if fn.endswith(".pyc"):
                fp = os.path.join(dirpath, fn)
                if fp not in out:
                    out.append(fp)
    return out


def _winget_pythons():
    """setup.bat 用 winget 装出来的 Python（落在 %LOCALAPPDATA%\\Programs\\Python）。"""
    base = os.path.join(LOCALAPPDATA, "Programs", "Python")
    out = []
    try:
        for name in os.listdir(base):
            if not name.startswith("Python3"):
                continue
            ver = name[len("Python"):]
            if len(ver) < 3:
                continue
            pkg = "Python.Python.%s.%s" % (ver[0], ver[1:])
            out.append((pkg, os.path.join(base, name)))
    except Exception:
        pass
    return out


def build_items():
    cfg = load_paths()
    items = []

    def add(**kw):
        kw.setdefault("note", "")
        kw.setdefault("backup", False)
        kw.setdefault("warn", "")
        kw.setdefault("paths", [])
        items.append(kw)

    # ---- 项目内再生缓存 -------------------------------------------------
    cache_default = os.path.join(HERE, "cache")
    configured_data = (cfg.get("data_dir") or "").strip()
    cache_paths = [cache_default]
    if configured_data and os.path.isabs(configured_data):
        if _norm(configured_data) != _norm(cache_default):
            cache_paths.append(os.path.normpath(configured_data))
    add(gid="cache", group="① 项目内缓存（可再生）",
        name="检索/精读缓存、向量库、智能体任务记录",
        paths=[p for p in cache_paths if os.path.exists(p)],
        mode="perm", default=True,
        note="webapp/cache + 你在设置里改过的 data_dir")
    add(gid="logs", group="① 项目内缓存（可再生）",
        name="运行日志 webapp/logs",
        paths=[os.path.join(HERE, "logs")], mode="perm", default=True)
    add(gid="pyc", group="① 项目内缓存（可再生）",
        name="Python 字节码缓存 __pycache__ / *.pyc",
        paths=_pycache_targets(), mode="perm", default=True)

    # ---- 虚拟环境 -------------------------------------------------------
    for name, p in (("webapp/venv", os.path.join(HERE, "venv")),
                    ("webapp/.venv", os.path.join(HERE, ".venv"))):
        if not os.path.exists(p):
            continue
        note = ""
        if running_inside(p):
            note = "当前解释器就在这个环境里，删不干净——请改用系统 Python 再跑一次"
        add(gid="venv-" + name, group="② 虚拟环境（setup.bat 第 2 步）",
            name=name + " — 依赖包都装在这里面",
            paths=[p], mode="perm", default=not running_inside(p), note=note)

    # ---- setup.bat 下载的浏览器内核（默认位置）--------------------------
    ms = os.path.join(LOCALAPPDATA, "ms-playwright")
    if os.path.isdir(ms):
        add(gid="pw-default", group="③ 浏览器内核（setup.bat 可选下载）",
            name="%LOCALAPPDATA%\\ms-playwright — setup.bat 的默认落点",
            paths=[ms], mode="perm", default=True)

    # ---- 本项目自己的登录资料 -------------------------------------------
    prof = (cfg.get("webchat_profile_dir") or "").strip()
    if not prof:
        prof = os.path.join(os.path.normpath(configured_data or cache_default), "webchat_profile")
    if prof and os.path.isdir(prof):
        add(gid="profile", group="④ 网页端登录资料（本项目生成）",
            name="%s — DeepSeek 等网页端登录态" % prof,
            paths=[os.path.normpath(prof)], mode="trash", default=True,
            note="删掉只影响下次要重新登录网页端")

    # ---- pip 下载缓存 ---------------------------------------------------
    pipcache = os.environ.get("PIP_CACHE_DIR") or os.path.join(LOCALAPPDATA, "pip")
    if os.path.isdir(pipcache):
        add(gid="pipcache", group="⑤ pip 下载缓存（setup.bat 装包时产生）",
            name="%s — 删掉只影响下次装包要重新下载" % pipcache,
            paths=[pipcache], mode="perm", default=True)

    # ---- 配置与密钥（先备份，再进回收站）--------------------------------
    cfg_files = [os.path.join(HERE, "config", n) for n in
                 ("settings.json", "embedding.json", "paths.json", "domain.json",
                  "mineru_key.txt")]
    for d in ("domains", "topics"):
        cfg_files.append(os.path.join(HERE, "config", d))
    cfg_files.append(os.path.join(HERE, "core", "mineru_key.txt"))
    cfg_files = [p for p in cfg_files if os.path.exists(p)]
    if cfg_files:
        add(gid="config", group="⑥ 配置与密钥（含 API Key）",
            name="settings / embedding / paths / domain / 领域包 / 检索配置 / MinerU key",
            paths=cfg_files, mode="trash", default=True, backup=True,
            note="会先备份到 webapp/_uninstall_backup/ 再删")

    # ---- 系统级：winget 装的 Python -------------------------------------
    for pkg, path in _winget_pythons():
        if not os.path.isdir(path):
            continue
        add(gid="winget-" + pkg, group="⑦ 系统级（setup.bat 第 1 步 winget 装的）",
            name="%s（%s）" % (pkg, path),
            paths=[path], mode="winget", default=True, payload=pkg,
            warn="这是系统级 Python，别的程序/项目也可能在用")

    return items


def build_reports():
    """只报告不删的条目。"""
    cfg = load_paths()
    out = []
    pw = (cfg.get("playwright_browsers_dir") or "").strip()
    if pw and os.path.isdir(pw):
        out.append(("共用/外部浏览器内核（未纳入删除）", os.path.normpath(pw),
                    "paths.json 指向了它，但它不是 setup.bat 装的（setup.bat 只装到 "
                    "%LOCALAPPDATA%\\ms-playwright），而且早于本项目存在，"
                    "疑似其它工具共用 → 保留，需你自己确认后再手工处理"))
    jr = os.path.join(HERE, "config", "journal_ranks.csv")
    if os.path.exists(jr):
        out.append(("期刊分区表（授权数据）", jr, "你自备的授权数据，一律保留"))
    for label, p, why in (
        ("源码与脚本", os.path.join(ROOT, "scripts"), "项目源码，保留"),
        ("文档", os.path.join(ROOT, "docs"), "项目文档，保留"),
        ("精读笔记", os.path.join(ROOT, "notes"), "你的研究笔记，保留"),
        ("成果报告", os.path.join(ROOT, "reports"), "你的产出，保留"),
    ):
        if os.path.exists(p):
            out.append((label, p, why))
    out.append(("整个源码目录", ROOT,
                "本工具不删源码。确认过不再需要时，你自己删这个目录即可"))
    return out


# ----------------------------------------------------------------- 执行
def run_winget(pkg, log):
    log("    winget uninstall --id %s" % pkg)
    try:
        r = subprocess.run(["winget", "uninstall", "--id", pkg, "-e",
                            "--accept-source-agreements"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1800)
    except Exception as e:                    # noqa: BLE001
        return [("winget", str(e))]
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    for line in tail[-6:]:
        log("      " + line.strip())
    if r.returncode != 0:
        return [(pkg, "winget 退出码 %s" % r.returncode)]
    return []


def execute(items, log):
    freed, errs = 0, []
    for it in items:
        log("  · %s  [%s]" % (it["name"], human(sum(du(p) for p in it["paths"]))))
        if it["mode"] == "winget":
            errs += run_winget(it["payload"], log)
        else:
            f, e = delete_items(it, log)
            freed += f
            errs += e
    return freed, errs


# ----------------------------------------------------------------- 界面
BAR = "=" * 68


def scan(items):
    for it in items:
        it["size"] = sum(du(p) for p in it["paths"])
    return [it for it in items if it["size"] > 0 or it["mode"] == "winget"]


def render(items, reports):
    print()
    print(BAR)
    print("  literature_flow 卸载器 —— 扫描结果")
    print(BAR)
    total = 0
    group = None
    for i, it in enumerate(items, 1):
        if it["group"] != group:
            group = it["group"]
            print("\n%s" % group)
        mark = "[x]" if it["default"] else "[ ]"
        print("  %s %2d. %-14s %s" % (mark, i, human(it["size"]), it["name"]))
        if it["note"]:
            print("        · %s" % it["note"])
        if it["warn"]:
            print("        ! %s" % it["warn"])
        if it["size"]:
            total += it["size"]
    print("\n  可删除合计约 %s" % human(total))
    if reports:
        print("\n  —— 以下内容只报告、不删除 ——")
        for label, path, why in reports:
            print("  * %s：%s" % (label, path))
            print("      %s" % why)
    return total


def choose(items):
    if not items:
        return []
    print()
    print("  勾选：直接回车 = 按上面的默认；输入编号(空格/逗号分隔) = 在这些项之间取反；")
    print("        all = 全选；none = 一个都不删；q = 退出")
    sel = {i for i, it in enumerate(items) if it["default"]}
    while True:
        try:
            raw = input("  你的选择：").strip().lower()
        except EOFError:
            return None
        if raw in ("q", "quit", "exit"):
            return None
        if raw == "":
            break
        if raw == "all":
            sel = set(range(len(items)))
            break
        if raw == "none":
            sel = set()
            break
        try:
            nums = [int(x) for x in raw.replace(",", " ").split()]
        except ValueError:
            print("  看不懂，试试 1 3 5 或 all / none / 回车")
            continue
        for n in nums:
            if 1 <= n <= len(items):
                sel ^= {n - 1}
            else:
                print("  没有编号 %s" % n)
        marks = " ".join(str(i + 1) for i in sorted(sel)) or "（空）"
        print("  当前勾选：%s   —— 再回车确认，或继续输入编号" % marks)
    return [items[i] for i in sorted(sel)]


def main(argv):
    force_utf8()
    list_only = "--list" in argv or "--dry-run" in argv
    assume_yes = "--yes" in argv or "-y" in argv
    no_trash = "--no-trash" in argv

    print(BAR)
    print("  literature_flow 卸载器")
    print("  项目目录：%s" % ROOT)
    print("  正在扫描…（目录大的话要几秒）")
    items = scan(build_items())
    reports = build_reports()
    render(items, reports)

    if list_only:
        print("\n  （只扫描，未删除任何文件）\n")
        return 0
    if assume_yes:
        chosen = [it for it in items if it["default"]]      # --yes = 直接用默认勾选
    else:
        chosen = choose(items)
    if chosen is None:
        print("\n  已退出，未删除任何文件。\n")
        return 0
    if not chosen:
        print("\n  没有勾选任何项，退出。\n")
        return 0

    if no_trash:
        for it in chosen:
            if it["mode"] == "trash":
                it["mode"] = "perm"

    print()
    print(BAR)
    print("  即将删除以下内容：")
    print(BAR)
    freed = 0
    for it in chosen:
        size = sum(du(p) for p in it["paths"])
        freed += size
        how = {"trash": "回收站（可恢复）", "perm": "永久删除",
               "winget": "winget 卸载"}[it["mode"]]
        print("  - %s  %s  [%s]" % (human(size), it["name"], how))
        for p in it["paths"]:
            print("      %s" % p)
        if it["mode"] == "winget":
            print("      %s" % it["payload"])
    print("\n  预计释放约 %s" % human(freed))
    print("  注意：缓存/虚拟环境/内核走永久删除，不进回收站。")
    print("\n  确认请原样输入 %s（其它任意输入 = 放弃）：" % CONFIRM_WORD)
    try:
        if input("  > ").strip() != CONFIRM_WORD:
            print("\n  已放弃，什么都没删。\n")
            return 0
    except EOFError:
        print("\n  非交互环境，已放弃。加 --yes 可直接执行。\n")
        return 1

    print("\n  开始执行…")
    log = lambda s: print(s)                  # noqa: E731
    freed, errs = execute(chosen, log)

    print()
    print(BAR)
    print("  完成。已释放约 %s" % human(freed))
    if errs:
        print("  以下条目没删干净（多半是被占用或权限不足）：")
        for e in errs:
            print("   ! %s" % e)
    if any(it.get("backup") for it in chosen):
        print("  配置已备份在：%s" % BACKUP_DIR)
        print("  （确认不再需要后，自己把这个备份目录删掉即可）")
    print("  源码/文档/笔记/报告/期刊分区表 均未改动。")
    print(BAR)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
