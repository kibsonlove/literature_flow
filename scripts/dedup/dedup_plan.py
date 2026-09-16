#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成条目去重方案（只读）：按 DOI + 规范化标题分组，每组选最完整的一份保留，其余标记删除。
输出 dedup_plan.json + dedup_plan.md。不写库。"""
import json
import os
import re
import urllib.parse
import urllib.request

# 输入输出都定位到脚本自身所在目录，从任意工作目录运行都有效
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(_HERE, "dedup_plan.json")
OUT_MD = os.path.join(_HERE, "dedup_plan.md")

BASE = "http://localhost:23119"


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "dedup/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_all_top():
    out, start = [], 0
    while True:
        chunk = get("/api/users/0/items/top", {"format": "json", "limit": 100, "start": start})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        start += 100
    return out


def norm_title(t):
    if not t:
        return ""
    t = t.lower()
    t = re.sub(r"[\s\-–—:.,;()\[\]{}'\"]+", "", t)
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", t)
    return t


def pick_canonical(group):
    # 评分：子项最多 > 有DOI > 标签最多 > 有日期
    def score(r):
        s = 0
        s += r["nchild"] * 100
        if r["doi"]:
            s += 10
        s += r["ntags"] * 2
        if r["date"]:
            s += 1
        return s
    return max(group, key=score)


def main():
    items = fetch_all_top()
    recs = []
    for it in items:
        d = it.get("data", {})
        if d.get("itemType") in ("attachment", "note"):
            continue
        meta = it.get("meta", {}) or {}
        recs.append({
            "key": it["key"],
            "type": d.get("itemType"),
            "title": (d.get("title") or "").strip(),
            "nt": norm_title(d.get("title")),
            "doi": (d.get("DOI") or "").lower().strip(),
            "date": (d.get("date") or "").strip(),
            "nchild": int(meta.get("numChildren") or 0),
            "ntags": len(d.get("tags") or []),
        })

    groups = {}  # frozenset(keys) -> (label, list of recs)
    # DOI 组（优先）
    by_doi = {}
    for r in recs:
        if r["doi"]:
            by_doi.setdefault(r["doi"], []).append(r)
    for doi, g in by_doi.items():
        if len(g) > 1:
            groups[frozenset(x["key"] for x in g)] = ("DOI:" + doi, g)
    # 标题组（规范化后）；仅当成员集合未已被 DOI 组覆盖时才加入
    by_t = {}
    for r in recs:
        if r["nt"]:
            by_t.setdefault(r["nt"], []).append(r)
    for nt, g in by_t.items():
        if len(g) > 1:
            ks = frozenset(x["key"] for x in g)
            if ks not in groups:
                groups[ks] = ("TITLE:" + nt[:40], g)

    plan = []
    for ks, (gid, g) in groups.items():
        keep = pick_canonical(g)
        deletes = [r for r in g if r["key"] != keep["key"]]
        plan.append({
            "group": gid,
            "keep": keep["key"],
            "keep_title": keep["title"],
            "keep_nchild": keep["nchild"],
            "delete": [{"key": r["key"], "title": r["title"], "nchild": r["nchild"]} for r in deletes],
        })

    json.dump(plan, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lines = ["# 条目去重方案 — 待确认\n",
             "> 只读生成。每组保留最完整一份，其余标记删除。确认后由 `zot_dedup.py` 执行。\n",
             "> 执行前请先备份 Zotero 数据目录，以便回滚。\n",
             "\n## 重复组总览：%d 组，将删除 %d 条\n" % (len(plan), sum(len(p["delete"]) for p in plan))]
    for p in plan:
        lines.append("\n### 组：%s" % p["group"])
        lines.append("- **保留**：`%s`  「%s」(子项%d)" % (p["keep"], p["keep_title"][:70], p["keep_nchild"]))
        for d in p["delete"]:
            lines.append("- 删除：`%s`  「%s」(子项%d)" % (d["key"], d["title"][:70], d["nchild"]))
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))

    print("重复组: %d | 将删除条目: %d" % (len(plan), sum(len(p["delete"]) for p in plan)))
    print("方案已写:", OUT_JSON, "/", OUT_MD)


if __name__ == "__main__":
    main()
