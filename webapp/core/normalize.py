# -*- coding: utf-8 -*-
"""网页端 LLM 返回内容的规范化后处理。

网页端（DeepSeek 等）常在回答前后夹带寒暄/客套，且偶尔用非标准结构。
本模块：去代码围栏、从首个标题行开始截取、剥离结尾客套。
"""
import re

# 结尾常见客套（行首命中即视为客套；另加长度上限防误删正文）
_TAIL_RE = re.compile(
    r"^\s*("
    r"希望"
    r"|如需"
    r"|如果需要"
    r"|(你|您)?还需要我"
    r"|需要我"
    r"|要不要我"
    r"|以上[是为]"
    r"|(如|若)有(其他|别的|更多|任何)"
    r"|(欢迎|随时)"
    r")"
)


def _strip_fence(t):
    t = t.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def normalize_webchat(text):
    """规范化网页端返回文本，返回清理后的 Markdown。"""
    if not text:
        return text
    t = _strip_fence(text)

    # 1) 从第一个 Markdown 标题行（# / ## / ###）开始，丢掉前面寒暄
    lines = t.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#{1,3}\s+\S", ln):
            start = i
            break
    lines = lines[start:]

    # 2) 从末尾删除客套行（允许末尾有若干空行）
    while lines and not lines[-1].strip():
        lines.pop()
    guard = 0
    while (
        lines
        and len(lines[-1].strip()) < 80
        and _TAIL_RE.match(lines[-1].strip())
        and guard < 20
    ):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
        guard += 1

    return "\n".join(lines).strip()
