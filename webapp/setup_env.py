# -*- coding: utf-8 -*-
"""首次安装的最后一步：探测 Zotero 路径并写入 config/paths.json。

只补空缺，**不覆盖**你已经填好的值。由 setup.bat 调用，也可以单独运行：
    venv\\Scripts\\python.exe setup_env.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config, zotero_detect


def main():
    print("      探测中…")
    d = zotero_detect.detect()
    data_dir = d["data_dir"]["path"]
    exe = d["zotero_exe"]["path"]

    print("        Zotero 数据目录 :", data_dir or "（没找到）")
    print("        Zotero 程序路径 :", exe or "（没找到）")

    cur = config.load_paths()
    fields = {}
    if data_dir and not (cur.get("zotero_data_dir") or "").strip():
        fields["zotero_data_dir"] = data_dir
    if exe and not (cur.get("zotero_exe") or "").strip():
        fields["zotero_exe"] = exe

    if fields:
        config.save_paths(**fields)
        print("        已写入 webapp/config/paths.json：", "、".join(fields))
    else:
        print("        无需写入（已配置，或都没探测到）")

    if not data_dir:
        print("        ⚠ 数据目录没探测到——启动后在页面「设置 → 本机路径」")
        print("          点「浏览数据目录…」选一次即可（就是含 storage 文件夹的那一层）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
