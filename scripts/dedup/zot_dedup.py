#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行条目去重（C）：每组保留 canonical，把其余副本的子附件/笔记改挂到保留项，再删除副本。
默认 dry-run（只报告）；--apply 才真写。结果写 dedup_result.log。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_ROOT, "webapp"))

BASE = "http://localhost:23119"
PLAN = os.path.join(_HERE, "dedup_plan.json")
LOG = os.path.join(_HERE, "dedup_result.log")


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "zot-dedup/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def get_children(key):
    return get("/api/users/0/items/%s/children" % key, {"format": "json"})


def client():
    """复用 webapp 的连接封装：Zotero 本地 API 无需鉴权，零配置可用。"""
    from core.zotero_io import _client
    return _client()


def main():
    apply = "--apply" in sys.argv
    logf = open(LOG, "w", encoding="utf-8")
    errors = []
    try:
        plan = json.load(open(PLAN, encoding="utf-8"))
        z = client()
        total_reparent = 0
        total_delete = 0
        for p in plan:
            keep = p["keep"]
            for d in p["delete"]:
                dk = d["key"]
                try:
                    # 1) 把副本的子附件/笔记改挂到保留项（仅当有保留项时）
                    if keep:
                        children = get_children(dk)
                        for ch in children:
                            ck = ch["key"]
                            item = z.item(ck)
                            item["data"]["parentItem"] = keep
                            if apply:
                                z.update_item(item)
                            total_reparent += 1
                            logf.write("REPARENT %s -> parent %s\n" % (ck, keep)); logf.flush()
                    # 2) 删除副本（delete_item 需传条目字典，含 version）
                    if apply:
                        del_item = z.item(dk)
                        z.delete_item(del_item)
                    total_delete += 1
                    logf.write("DELETE %s (keep=%s)\n" % (dk, keep)); logf.flush()
                except Exception as e:
                    errors.append((dk, str(e)))
                    logf.write("ERROR on %s: %s\n" % (dk, e)); logf.flush()
        logf.write(json.dumps({"apply": apply, "reparented": total_reparent,
                                "deleted": total_delete, "errors": len(errors)},
                               ensure_ascii=False) + "\n")
        logf.flush()
    except Exception:
        logf.write("FATAL:\n" + traceback.format_exc())
    finally:
        logf.close()


if __name__ == "__main__":
    main()
