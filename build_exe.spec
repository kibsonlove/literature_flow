# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：literature_flow 绿色免安装包（onedir）。

构建：cd 项目根 && _build\\venv\\Scripts\\pyinstaller build_exe.spec --noconfirm
产物：dist/literature_flow/（配 build_exe.bat 使用）
"""
import os

PROJ = os.path.abspath(".")
APP = os.path.join(PROJ, "webapp")

datas = [
    # 前端资源（app.py 按模块相对路径定位，打进 _internal 同层）
    (os.path.join(APP, "templates"), "templates"),
    (os.path.join(APP, "static"), "static"),
]
# 配置模板（真实 config 由用户在包内 config/ 下自行创建）
for fn in ("settings.example.json", "embedding.example.json", "paths.example.json"):
    p = os.path.join(APP, "config", fn)
    if os.path.exists(p):
        datas.append((p, "config"))

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]

a = Analysis(
    [os.path.join(APP, "run_app.py")],
    pathex=[APP],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.tests"],
    noarchive=False,
)
# playwright 的 node 驱动是数据+二进制混合体，需要整体收集
from PyInstaller.utils.hooks import collect_all
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="literature_flow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="literature_flow",
)
