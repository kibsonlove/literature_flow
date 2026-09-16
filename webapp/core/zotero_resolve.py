# -*- coding: utf-8 -*-
"""解析"六维笔记该挂到哪个 Zotero 条目"。

设计（按用户要求）：不让用户手动填父条目 key，而是 app 自己匹配条目；
若 PDF 没有文献父条目则自动创建，最后只让用户确认。

resolve_target(zotero_key=None, filename=None) 返回：
  {
    "target_key": 父条目 key（create_new 时为 None，写回时再建）,
    "target_title": 条目标题,
    "action": "attach_existing" | "created_parent" | "create_new",
    "detail": 给用户看的状态说明
  }
"""
import re
import difflib

from pyzotero import zotero

from .zotero_io import _client, create_parent


def _clean_title(fn):
    """从 PDF 文件名清洗出标题。"""
    t = fn
    if t.lower().endswith(".pdf"):
        t = t[:-4]
    t = t.replace("+", " ").replace("%20", " ")
    t = re.sub(r"\s*\(Z-Library\)\s*$", "", t)
    return t.strip()


def _guess_item_type(fn):
    """极简启发式：出版社/书店书 → book，其余 → journalArticle。"""
    low = (fn or "").lower()
    if "z-library" in low or "出版社" in fn or "press" in low:
        return "book"
    return "journalArticle"


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[\s_\-.\u3000]+", "", s)
    return s


def _attach_to(item, parent_key):
    """把附件 item 重新挂载到 parent_key（原地修改并 update）。"""
    item["data"]["parentItem"] = parent_key
    item["data"].pop("lastRead", None)
    z = _client()
    z.update_item(item)


def resolve_target(zotero_key=None, filename=None):
    z = _client()

    # 1) 提供 Zotero key 的路径
    if zotero_key:
        item = z.item(zotero_key)
        d = item["data"]
        itype = d.get("itemType")
        if itype == "attachment":
            parent = d.get("parentItem")
            if parent:
                p = z.item(parent)
                return {
                    "target_key": parent,
                    "target_title": p["data"].get("title") or _clean_title(d.get("filename") or ""),
                    "action": "attach_existing",
                    "detail": "该 PDF 已挂在文献条目下",
                }
            # 孤儿附件：建父条目并挂载
            fn = d.get("filename") or d.get("title") or zotero_key
            title = _clean_title(fn)
            ptype = _guess_item_type(fn)
            new_key = create_parent(title, ptype)
            if new_key:
                _attach_to(item, new_key)
                return {
                    "target_key": new_key,
                    "target_title": title,
                    "action": "created_parent",
                    "detail": "PDF 原无文献条目，已自动创建并挂载",
                }
            return {
                "target_key": None,
                "target_title": title,
                "action": "create_new",
                "detail": "建父条目失败，将尝试新建",
            }
        # 已经是文献父条目
        return {
            "target_key": zotero_key,
            "target_title": d.get("title") or _clean_title(d.get("filename") or ""),
            "action": "attach_existing",
            "detail": "已是文献条目",
        }

    # 2) 上传 PDF 路径：按文件名匹配 Zotero 中已有附件
    if filename:
        norm = _norm(filename)
        atts = z.items(itemType="attachment", limit=400)
        best, best_score = None, 0.0
        for a in atts:
            fn = a["data"].get("filename") or a["data"].get("title") or ""
            score = difflib.SequenceMatcher(None, norm, _norm(fn)).ratio()
            if score > best_score:
                best_score, best = score, a
        if best and best_score >= 0.6:
            parent = best["data"].get("parentItem")
            if parent:
                p = z.item(parent)
                return {
                    "target_key": parent,
                    "target_title": p["data"].get("title") or _clean_title(best["data"].get("filename") or ""),
                    "action": "attach_existing",
                    "detail": f"按文件名匹配到 Zotero 中已有 PDF（相似度 {best_score:.0%}）",
                }
            # 匹配到的是孤儿附件：建父条目并挂载
            fn = best["data"].get("filename") or best["data"].get("title") or ""
            title = _clean_title(fn)
            ptype = _guess_item_type(fn)
            new_key = create_parent(title, ptype)
            if new_key:
                _attach_to(best, new_key)
                return {
                    "target_key": new_key,
                    "target_title": title,
                    "action": "created_parent",
                    "detail": "匹配到的 PDF 原无文献条目，已自动创建并挂载",
                }
        # 未匹配到：将新建条目
        title = _clean_title(filename)
        return {
            "target_key": None,
            "target_title": title,
            "action": "create_new",
            "detail": "Zotero 中未匹配到该 PDF，将新建文献条目",
        }

    return {
        "target_key": None,
        "target_title": None,
        "action": "create_new",
        "detail": "无法确定目标条目",
    }
