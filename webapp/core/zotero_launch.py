# -*- coding: utf-8 -*-
"""确保 Zotero 在运行：启动 webapp 时若 Zotero 未运行，自动拉起并等本地 API 就绪。"""
import os
import subprocess
import time

from .config import ZOTERO_EXE, ZOTERO_WAIT_SECONDS
from .zotero_io import ping


def zotero_running():
    """本地 API 是否可用（≈ Zotero 是否在运行且允许本地通信）。"""
    return ping()


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
        return True

    if not os.path.exists(ZOTERO_EXE):
        say(f"未找到 Zotero 程序：{ZOTERO_EXE}（可在 core/config.py 修改 ZOTERO_EXE）")
        return False

    try:
        subprocess.Popen([ZOTERO_EXE], close_fds=True)
        say(f"已启动 Zotero：{ZOTERO_EXE}，等待本地 API 就绪…")
    except Exception as e:
        say(f"启动 Zotero 失败：{e}")
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if zotero_running():
            say("Zotero 本地 API 已就绪")
            return True
        time.sleep(2)
    say(f"等待 Zotero 就绪超时（{wait_seconds}s）")
    return False
