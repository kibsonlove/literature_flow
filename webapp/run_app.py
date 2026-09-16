# -*- coding: utf-8 -*-
"""打包启动器：PyInstaller 入口。

冻结模式下：
  - 数据文件在 dist/literature_flow/_internal/
  - config/cache/reports 等可写目录定位到 exe 所在目录（而不是 _internal）
"""
import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))

if getattr(sys, "frozen", False):
    # onedir：模块资源在 _internal；把"可写目录"锚定到 exe 所在目录
    _EXE_DIR = os.path.dirname(sys.executable)
    os.chdir(_EXE_DIR)
    # 网页端内核：打包时随包分发（dist/literature_flow/webchat_browsers/）
    _KB = os.path.join(_EXE_DIR, "webchat_browsers")
    if os.path.isdir(_KB) and any(
        d.startswith("chromium") for d in os.listdir(_KB)
    ):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _KB)

import uvicorn  # noqa: E402

sys.path.insert(0, _APP_DIR)
from app import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
