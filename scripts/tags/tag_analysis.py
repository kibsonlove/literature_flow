#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性分析脚本：拉取本机 Zotero 全部标签，做归并前诊断。只读，不写库。"""
import json
import os
import urllib.request
import urllib.parse

# 输出定位到脚本自身目录，从任意工作目录运行都有效
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_RAW = os.path.join(_HERE, "tag_analysis_raw.json")

BASE = "http://localhost:23119"


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "tag-analysis/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_all_tags():
    out = []
    start = 0
    while True:
        chunk = get("/api/users/0/tags", {"format": "json", "limit": 100, "start": start})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        start += 100
    return out


def main():
    raw = fetch_all_tags()
    tags = [{"tag": t.get("tag"),
             "numItems": (t.get("meta") or {}).get("numItems", 0)} for t in raw]

    total = len(tags)
    n1 = sum(1 for t in tags if t["numItems"] == 1)
    print("=== 总览 ===")
    print("标签总数:", total)
    print("numItems==1（噪声嫌疑最高）:", n1)
    print("numItems>=2:", total - n1)

    # 归一化查重（小写 + 去首尾空白），捕捉大小写/空白近重复
    norm = {}
    for t in tags:
        k = t["tag"].strip().lower()
        norm.setdefault(k, []).append(t)
    dups = {k: v for k, v in norm.items() if len(v) > 1}
    print("\n=== 归一化后重复标签（同名/近名合并后>1条）===")
    print("重复组数:", len(dups))
    for k, v in sorted(dups.items()):
        print(" ·", repr(k), "->", [ (t["tag"], t["numItems"]) for t in v ])

    # 完全相同显示名却 numItems 不同的（可能是大小写但渲染一致）
    exact = {}
    for t in tags:
        exact.setdefault(t["tag"], []).append(t["numItems"])
    exact_dups = {k: v for k, v in exact.items() if len(v) > 1}
    print("\n=== 显示名完全一致但 numItems 不同（真重复）===")
    if exact_dups:
        for k, v in exact_dups.items():
            print(" ·", repr(k), v)
    else:
        print("（无）")

    # 按 numItems 降序输出全部，便于设计归并映射
    print("\n=== 全部标签（按 numItems 降序，再按名排序）===")
    for t in sorted(tags, key=lambda x: (-x["numItems"], x["tag"].lower())):
        print("%4d  %s" % (t["numItems"], t["tag"]))

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=1)
    print("\n原始数据已存:", OUT_RAW)


if __name__ == "__main__":
    main()
