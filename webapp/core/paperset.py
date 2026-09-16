# -*- coding: utf-8 -*-
"""paper-set 批量比对：从 Zotero 子笔记抽取「维度速览」，汇总成对比表数据。

兼容两种历史格式：
  1. 规范的 HTML 表格（<table><tr><td>…）—— 新版 webapp / lfz 生成；
  2. 纯文本形式（"维度  一句话 / 遗存  …"）—— 早期用 inner_text 抓取生成的。
标签取自**父条目**的 tags（回写标签加在条目上，不在笔记上）。
"""
import html as _html
import re

from .zotero_io import _client

from .domain import load_domain as _load_domain
_DIM_KEYS = _load_domain()["dimensions"]


def _plain(s):
    t = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def _year(s):
    m = re.search(r"[（(](\d{4})[)）]", s or "")
    if m:
        return m.group(1)
    m = re.search(r"(?:19|20)\d{2}", s or "")
    return m.group(0) if m else ""


def _rows_from_table(html):
    idx = html.find("六维速览")
    if idx < 0:
        idx = html.find("维度速览")
    if idx < 0:
        return []
    seg = html[idx:]
    m = re.search(r"<table[^>]*>(.*?)</table>", seg, re.S | re.I)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        if tds:
            rows.append([_plain(t) for t in tds])
    return rows


def _rows_from_text(html):
    # 只去标签、**保留换行**（早期笔记的六维速览是 inner_text 抓来的纯文本，靠 \n 分行）
    txt = _html.unescape(re.sub(r"<[^>]+>", "", html or ""))
    idx = txt.find("六维速览")
    if idx < 0:
        idx = txt.find("维度速览")
    if idx < 0:
        return []
    seg = txt[idx:]
    for stop in ("核心结论", "讨论要点"):
        j = seg.find(stop, 4)
        if j > 0:
            seg = seg[:j]
    rows = []
    for l in seg.splitlines():
        l = re.sub(r"[ \t]{2,}", "  ", l).strip()
        if not l:
            continue
        for p in _DIM_KEYS:
            if l.startswith(p):
                val = re.sub(r"^[\s:：]+", "", l[len(p):]).strip()
                if val:
                    rows.append([p, val])
                break
    return rows


def collect():
    """返回 [{note_key, parent_key, title, year, dims, tags, parsed}, ...]"""
    z = _client()

    # 父条目 key -> tags / date
    tags_map = {}
    date_map = {}
    try:
        for it in z.top(limit=300):
            k = it["key"]
            tags_map[k] = [t.get("tag") for t in it["data"].get("tags", [])]
            date_map[k] = it["data"].get("date", "") or ""
    except Exception:
        pass

    try:
        notes = z.items(itemType="note", limit=500)
    except Exception:
        notes = []

    out = []
    for n in notes:
        d = n["data"]
        html = d.get("note") or ""
        if ("六维速览" not in html and "六维精读" not in html
                and "维度速览" not in html):
            continue
        rows = _rows_from_table(html) or _rows_from_text(html)
        dims = {}
        for r in rows:
            if len(r) >= 2 and r[0] != "维度":
                key = r[0].strip()
                # 别名归一：笔记里的维度名按前缀匹配到领域包维度（兼容简写）
                key = next((d for d in _DIM_KEYS
                            if key == d or d.split("/")[0] == key or key.startswith(d.split("/")[0])), key)
                if key not in dims:
                    dims[key] = r[1]
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        title = _plain(m.group(1)) if m else "(无标题)"
        parent = d.get("parentItem")
        out.append({
            "note_key": n.get("key"),
            "parent_key": parent,
            "title": title,
            "year": _year(title) or _year(date_map.get(parent, "")),
            "dims": dims,
            "tags": tags_map.get(parent, []),
            "parsed": bool(dims),
        })

    out.sort(key=lambda x: (x.get("year") or "", x.get("title") or ""))
    return out
