# -*- coding: utf-8 -*-
"""Zotero 回写封装（复用 zot_write.py 的本地 API 逻辑）。"""
import os

from pyzotero import zotero

KEY_DIR = r"C:\Users\PC\.workbuddy\skills\zotero-research-assistant"


def _client():
    key = open(os.path.join(KEY_DIR, ".local_key"), encoding="utf-8").read().strip()
    sid_path = os.path.join(KEY_DIR, ".server_id")
    sid = open(sid_path, encoding="utf-8").read().strip() if os.path.exists(sid_path) else None
    return zotero.Zotero("0", "user", None, local=True, local_api_key=key, server_id=sid)


def add_note(parent, html, title=""):
    """给父条目添加子笔记（HTML）。返回创建成功的 note key 列表。

    注意：Zotero 本地 API 的 note 类型没有 `title` 字段，标题通过 HTML
    内容开头自动提取。若创建失败会抛出 RuntimeError。
    """
    z = _client()
    res = z.create_items([{"itemType": "note", "note": html}], parent)
    succ = res.get("successful", {})
    if not succ:
        failed = res.get("failed", {})
        msgs = [f"{k}: {v.get('message', '')}" for k, v in failed.items()]
        raise RuntimeError(
            "Zotero 笔记创建失败：" + "; ".join(msgs)
            if msgs else "Zotero 笔记创建失败（未知错误）"
        )
    keys = []
    for v in succ.values():
        if isinstance(v, dict) and v.get("key"):
            keys.append(v["key"])
    return keys


def add_tags(key, tags):
    """给条目追加标签（跳过已存在的）。返回实际新增的标签。"""
    z = _client()
    item = z.item(key)
    existing = {t.get("tag") for t in item["data"].get("tags", [])}
    added = [t.strip() for t in tags if t and t.strip() not in existing]
    if added:
        item["data"]["tags"] = item["data"].get("tags", []) + [{"tag": t} for t in added]
        z.update_item(item)
    return added


def create_parent(title, itype="journalArticle"):
    """新建一个顶层文献父条目，返回 key（失败返回 None）。"""
    z = _client()
    parent = {"itemType": itype, "title": title, "creators": [], "tags": []}
    res = z.create_items([parent])
    succ = res.get("successful", {})
    for k in ("0", 0):
        if k in succ:
            return succ[k]["key"]
    return None


def delete_note(note_key):
    """删除一条子笔记。"""
    z = _client()
    item = z.item(note_key)
    z.delete_item(item)
    return True


_SKIP_TYPES = ("attachment", "note")


def _pdf_parents_set(z):
    """一次批量拉取全部 PDF 附件，建立 parentItem -> 含 PDF 集合（避免逐条 children 慢查询）。"""
    parents = set()
    start = 0
    while True:
        try:
            batch = z.items(itemType="attachment", limit=100, start=start)
        except Exception:
            break
        if not batch:
            break
        for a in batch:
            d = a["data"]
            if d.get("contentType") == "application/pdf" and d.get("parentItem"):
                parents.add(d["parentItem"])
        if len(batch) < 100:
            break
        start += len(batch)
    return parents


def list_recent(limit=60):
    """列出最近修改的顶层文献条目（排除附件/笔记），标注是否含 PDF。"""
    z = _client()
    try:
        items = z.top(limit=limit)
    except Exception:
        items = []
    pdf_parents = _pdf_parents_set(z)
    out = []
    for it in items:
        d = it["data"]
        if d.get("itemType") in _SKIP_TYPES:
            continue
        key = it["key"]
        out.append({
            "key": key,
            "title": d.get("title") or "(无标题)",
            "itemType": d.get("itemType"),
            "dateModified": d.get("dateModified", ""),
            "has_pdf": key in pdf_parents,
        })
    out.sort(key=lambda x: x.get("dateModified", ""), reverse=True)
    return out


def search_items(q, limit=40):
    """按关键词搜索文献条目（排除附件/笔记），标注是否含 PDF。"""
    z = _client()
    out = []
    if not q or not q.strip():
        return out
    try:
        items = z.items(q=q.strip(), limit=limit)
    except Exception:
        items = []
    pdf_parents = _pdf_parents_set(z)
    for it in items:
        d = it["data"]
        if d.get("itemType") in _SKIP_TYPES:
            continue
        key = it["key"]
        out.append({
            "key": key,
            "title": d.get("title") or "(无标题)",
            "itemType": d.get("itemType"),
            "dateModified": d.get("dateModified", ""),
            "has_pdf": key in pdf_parents,
        })
    return out


def list_collections():
    """返回分类树（含子分类），供前端展示为文件夹树。

    节点结构：{key, name, parent, children: [...]}
    注意：Zotero 本地 API 的 /collections 有时会漏掉个别分类（如"导入"），
    故再从条目引用的 collection key 反向补齐。
    """
    z = _client()
    nodes = {}
    try:
        for c in z.collections():
            d = c["data"]
            nodes[c["key"]] = {
                "key": c["key"],
                "name": d.get("name") or "(未命名分类)",
                "parent": d.get("parentCollection") or None,
                "children": [],
            }
    except Exception:
        pass

    # 反向补齐：条目引用了、但 /collections 未返回的分类
    try:
        tops = z.everything(z.top()) if hasattr(z, "everything") else z.top(limit=300)
        ref = set()
        for it in tops:
            ref.update(it["data"].get("collections", []) or [])
        for k in ref - set(nodes.keys()):
            try:
                c = z.collection(k)
                d = c["data"]
                nodes[k] = {
                    "key": k,
                    "name": d.get("name") or "(未命名分类)",
                    "parent": d.get("parentCollection") or None,
                    "children": [],
                }
            except Exception:
                pass
    except Exception:
        pass

    roots = []
    for n in nodes.values():
        p = n["parent"]
        if p and p in nodes:
            nodes[p]["children"].append(n)
        else:
            roots.append(n)

    def _sort(ns):
        ns.sort(key=lambda x: x["name"])
        for x in ns:
            _sort(x["children"])

    _sort(roots)
    return roots


def _subtree_keys(z, root_key):
    """返回 root_key 及其所有子分类的 key 集合。"""
    try:
        cols = z.collections()
    except Exception:
        return {root_key}
    children = {}
    for c in cols:
        d = c["data"]
        children.setdefault(d.get("parentCollection"), []).append(c["key"])
    out = set()
    stack = [root_key]
    while stack:
        k = stack.pop()
        if k in out:
            continue
        out.add(k)
        stack.extend(children.get(k, []))
    return out


def list_collection_items(collection_key, include_children=True, limit=300):
    """列出某分类下的顶层文献条目（默认含子分类），标注是否含 PDF。"""
    z = _client()
    keys = _subtree_keys(z, collection_key) if include_children else {collection_key}
    pdf_parents = _pdf_parents_set(z)
    seen = set()
    out = []
    for ck in keys:
        try:
            items = z.collection_items(ck, limit=limit)
        except Exception:
            continue
        for it in items:
            d = it["data"]
            if d.get("itemType") in _SKIP_TYPES:
                continue
            if d.get("parentItem"):
                continue
            key = it["key"]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "key": key,
                "title": d.get("title") or "(无标题)",
                "itemType": d.get("itemType"),
                "dateModified": d.get("dateModified", ""),
                "has_pdf": key in pdf_parents,
            })
    out.sort(key=lambda x: x.get("title", ""))
    return out


def ping():
    """检测 Zotero 本地 API 是否可用（Zotero 是否已打开并允许本地通信）。"""
    try:
        z = _client()
        z.items(limit=1)
        return True
    except Exception:
        return False
