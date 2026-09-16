# -*- coding: utf-8 -*-
"""工具箱：把根目录/notes 的维护脚本统一收编为 webapp 白名单运行器。

两层安全：
1. 只能运行白名单里的脚本；
2. 变更类脚本（dedup/tag_merge）默认 dry-run，只有 apply=True 才加 --apply，
   且前端必须带 confirm。
"""
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PYTHON = sys.executable
REPORTS = os.path.join(ROOT, "reports")

# tool → (脚本相对路径, 是否变更类)
TOOLS = {
    "lit_watch":    ("lit_watch.py", False),
    "coverage":     (r"notes\build_coverage.py", False),
    "digest":       (r"notes\build_digest.py", False),
    "pack":         (r"notes\build_pack.py", False),
    "tag_analysis": ("tag_analysis.py", False),
    "verify_tags":  ("verify_tags.py", False),
    "dedup":        ("zot_dedup.py", True),
    "tag_merge":    ("zot_tag_merge.py", True),
}

# 运行成功后值得展示/打开的产物
OUTPUTS = {
    "lit_watch": ("候选清单", os.path.join(REPORTS, "文献订阅_候选清单.md")),
    "coverage":  ("覆盖矩阵报告", os.path.join(REPORTS, "覆盖矩阵与选题机会.html")),
    "pack":      ("写作素材包", os.path.join(ROOT, "notes", "material_pack.md")),
}

def run_tool(tool, apply=False, timeout=600, log=None):
    if tool not in TOOLS:
        return {"ok": False, "error": f"未知工具：{tool}"}
    script, mutating = TOOLS[tool]
    if mutating and not apply:
        pass  # dry-run 即默认行为
    args = [PYTHON, os.path.join(ROOT, script)]
    if mutating and apply:
        args.append("--apply")
    # 子进程统一 UTF-8 输出（否则 Windows 下 GBK 输出经 utf-8 解码即乱码）
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        out = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.stderr.strip() else "")
        # 常见故障翻译：产物文件被占用（正被预览/编辑器打开）
        if "PermissionError" in out:
            out += "\n\n⚠ 产物文件被占用（可能正在浏览器/编辑器中打开）。\n  请关闭已打开的清单/报告文件后重试。"
        tail = "\n".join(out.strip().split("\n")[-25:])
        info = {"ok": p.returncode == 0, "tool": tool, "apply": bool(apply and mutating),
                "code": p.returncode, "output": tail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "error": "运行超时"}
    except Exception as e:
        return {"ok": False, "tool": tool, "error": repr(e)[:150]}
    if tool in OUTPUTS:
        label, path = OUTPUTS[tool]
        info["output_file"] = path
        info["output_label"] = label
    if log:
        log(f"[tool:{tool}] code={info.get('code')}")
    return info

def list_reports():
    out = []
    if os.path.isdir(REPORTS):
        for f in sorted(os.listdir(REPORTS), reverse=True):
            p = os.path.join(REPORTS, f)
            if os.path.isfile(p):
                out.append({"name": f, "size": os.path.getsize(p),
                            "mtime": int(os.path.getmtime(p) * 1000)})
    return out
