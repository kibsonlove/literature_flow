# -*- coding: utf-8 -*-
"""笔记 HTML 渲染：Markdown / 已抓取的 HTML → Zotero 笔记 HTML。"""
import re

import markdown as md

NOTE_CSS = (
    "<style>body{font-family:-apple-system,Segoe UI,sans-serif;line-height:1.65;"
    "max-width:900px;margin:2em auto;padding:0 1em}"
    "table{border-collapse:collapse;margin:1em 0}"
    "td,th{border:1px solid #ccc;padding:4px 8px}"
    "h2{border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:1.6em}"
    "blockquote{color:#666}</style>"
)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(note_md, title=""):
    """Markdown 笔记 → 完整 HTML 文档。"""
    html = md.markdown(note_md, extensions=["tables", "fenced_code"])
    header = f"<h1>精读笔记：{_esc(title)}</h1>" if title else ""
    # 样式置于末尾：Zotero 取笔记首段文本作列表标题，<style> 在前会被误当标题。
    return (
        "<html><head><meta charset='utf-8'></head><body>"
        + header + html + NOTE_CSS + "</body></html>"
    )


def wrap_html(note_html, title=""):
    """已抓取的 HTML 片段 → 完整 HTML 文档（不再走 Markdown 转换）。

    片段开头若已有 <h1> 则不重复注入标题。
    """
    frag = (note_html or "").strip()
    header = ""
    if title and not re.match(r"^\s*<h1[\s>]", frag, flags=re.I):
        header = f"<h1>精读笔记：{_esc(title)}</h1>"
    return (
        "<html><head><meta charset='utf-8'></head><body>"
        + header + frag + NOTE_CSS + "</body></html>"
    )
