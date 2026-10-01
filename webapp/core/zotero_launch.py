# -*- coding: utf-8 -*-
"""确保 Zotero 在运行：启动 webapp 时若 Zotero 未运行，自动拉起并等本地 API 就绪。"""
import os
import subprocess
import threading
import time

from .config import ZOTERO_WAIT_SECONDS, zotero_exe
from .zotero_io import ping

# 最近一次自动拉起的结果，供 /api/setup/status 反馈到界面横幅。
#   state: idle（还没试过/正在试）| ready | failed
_last = {"state": "idle", "exe": "", "message": ""}
_lock = threading.Lock()


def _set(state, exe="", message=""):
    with _lock:
        _last.update(state=state, exe=exe, message=message)


def last_launch():
    """最近一次自动拉起 Zotero 的状态（给界面用）。"""
    with _lock:
        return dict(_last)


def zotero_running():
    """本地 API 是否可用（≈ Zotero 是否在运行且允许本地通信）。"""
    return ping()


# 「启动即失败」最常见的原因是版本不匹配，而报错只出现在 Zotero 自己的弹窗里、日志里看不到，
# 用户很容易以为"我早就更新过了"。所以这里主动把话说透。
_VERSION_HINT = (
    "若 Zotero 弹出「此 Zotero 数据库需要 Zotero X 或更高版本，当前版本 Y」，"
    "说明配置里的 zotero.exe 是旧版本（常见于 Zotero 自动更新后旧安装目录被留下）——"
    "请到「设置 → 本机路径」把 Zotero 程序路径改成当前在用的那个；"
    "最快的对照办法是右键桌面 Zotero 快捷方式，看它指向哪个 exe。"
)


def ensure_zotero(wait_seconds=ZOTERO_WAIT_SECONDS, log=None):
    """若 Zotero 未运行则启动它，并等待本地 API 就绪。返回是否就绪。"""
    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    if zotero_running():
        say("Zotero 已在运行")
        _set("ready", zotero_exe(), "Zotero 已在运行")
        return True

    exe = zotero_exe()
    if not exe or not os.path.exists(exe):
        msg = (f"未找到 Zotero 程序：{exe or '（未配置）'}。"
               "请在网页顶栏「设置 → 本机路径」填入 zotero.exe 位置，或点「自动探测」")
        say(msg)
        _set("failed", exe, msg)
        return False

    try:
        proc = subprocess.Popen([exe], close_fds=True)
        say(f"已启动 Zotero：{exe}，等待本地 API 就绪…")
    except Exception as e:
        msg = f"启动 Zotero 失败：{e}"
        say(msg)
        _set("failed", exe, msg)
        return False

    deadline = time.time() + wait_seconds
    exited = None
    while time.time() < deadline:
        if zotero_running():
            say("Zotero 本地 API 已就绪")
            _set("ready", exe, "Zotero 本地 API 已就绪")
            return True
        # 注意：**启动进程退出 ≠ 失败**。Zotero 的 exe 是个 launcher 壳，交接给主进程后会立刻
        # 以退出码 0 退出（2026-09-30 实测：壳 4 秒退出，而 Zotero 正常启动、本地 API 200）。
        # 所以这里只记一笔继续等，判定失败一律交给超时分支。
        if exited is None and proc.poll() is not None:
            exited = proc.returncode
            say(f"启动进程已退出（退出码 {exited}）——Zotero 常会这样交接给主进程，继续等本地 API…")
        time.sleep(2)

    if exited is not None:
        msg = (f"等待 Zotero 就绪超时（{wait_seconds}s），且启动进程已退出（退出码 {exited}）。"
               + _VERSION_HINT)
    else:
        msg = (f"等待 Zotero 就绪超时（{wait_seconds}s）：它可能卡在弹窗上，或本地 API 未开启"
               f"（编辑 → 设置 → 高级 → 允许其他应用与 Zotero 通信）。" + _VERSION_HINT)
    say(msg)
    _set("failed", exe, msg)
    return False
