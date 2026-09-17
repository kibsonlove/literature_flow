# -*- coding: utf-8 -*-
"""调用系统原生选择框（Windows），供网页端「浏览…」按钮回填本机路径。

网页出于安全限制拿不到本机完整路径，所以由服务端弹系统对话框代选，再把结果回填输入框。
实现要点：
- 用 -EncodedCommand（UTF-16LE + base64）传脚本，绕开命令行参数的编码问题，中文提示不乱码。
- 结果写进临时文件再读，避免依赖 PowerShell 控制台输出编码。
- 用户取消或环境不支持时返回空串，不抛异常。
"""
import base64
import os
import subprocess
import tempfile
import uuid


def _run_ps(script, timeout=300):
    """执行 PowerShell 脚本；非 Windows 或失败时返回 False。"""
    if os.name != "nt":
        return False
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-STA", "-EncodedCommand", enc],
                       capture_output=True, timeout=timeout)
        return True
    except Exception:
        return False


def _q(s):
    """转义进 PowerShell 单引号字符串。"""
    return str(s).replace("'", "''")


def pick_folder(description="请选择文件夹"):
    """弹文件夹选择框，返回绝对路径；取消/失败返回空串。"""
    out = os.path.join(tempfile.gettempdir(), "lf_pick_%s.txt" % uuid.uuid4().hex)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '%s';"
        "$d.ShowNewFolderButton = $false;"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ Set-Content -LiteralPath '%s' -Value $d.SelectedPath -Encoding UTF8 }"
    ) % (_q(description), out)
    _run_ps(script)
    return _read_out(out)


def pick_file(description="请选择文件", filter_text="所有文件 (*.*)|*.*"):
    """弹文件选择框，返回绝对路径；取消/失败返回空串。"""
    out = os.path.join(tempfile.gettempdir(), "lf_pick_%s.txt" % uuid.uuid4().hex)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title = '%s';"
        "$d.Filter = '%s';"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ Set-Content -LiteralPath '%s' -Value $d.FileName -Encoding UTF8 }"
    ) % (_q(description), _q(filter_text), out)
    _run_ps(script)
    return _read_out(out)


def _read_out(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            v = f.read().strip()
    except Exception:
        return ""
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    return v
