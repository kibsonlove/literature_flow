# -*- coding: utf-8 -*-
"""Zotero 回写封装（本地 API，localhost:23119）。"""
import json
import os
import re
import shutil

from pyzotero import zotero

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # webapp/


def _paths_cfg():
    try:
        with open(os.path.join(_APP_DIR, "config", "paths.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_paths_cfg(**kv):
    """把键值写回 config/paths.json（本机文件，已 gitignore）。"""
    p = os.path.join(_APP_DIR, "config", "paths.json")
    d = _paths_cfg()
    d.update({k: v for k, v in kv.items() if v})
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def local_api_key():
    """Zotero 本地 API 的写入密钥（没授权则为空串）。

    ⚠ Zotero 本地 API 的读（GET）不需要密钥，**写（POST/PUT/DELETE）必须带**：
    缺 Server-ID 报 428，缺/错密钥报 401。该密钥由 Zotero 弹窗授权后发放，
    与 zotero.org 账号的 API key 完全是两回事。
    """
    return (os.environ.get("ZOTERO_LOCAL_API_KEY")
            or _paths_cfg().get("zotero_local_api_key") or "")


def write_authorized():
    """是否已保存过写入授权（只表示"配过"，真正有效性要到调用时才知道）。"""
    return bool(local_api_key())


def authorize_local(app_name="literature_flow"):
    """触发 Zotero 的授权弹窗，拿到写入密钥并持久化。

    Zotero 会弹出对话框让用户选 Allow（一次性）/ Always Allow（永久）/ Deny。
    **只有「Always Allow」拿到的密钥能长期用**——一次性密钥用完即废。
    """
    cfg = _paths_cfg()
    z = zotero.Zotero("0", "user", None, local=True,
                      server_id=(os.environ.get("ZOTERO_SERVER_ID")
                                 or cfg.get("zotero_server_id") or None))
    res = z.authorize_local(app_name) or {}
    key = res.get("key")
    remember = bool(res.get("remember"))
    if key and remember:
        _save_paths_cfg(zotero_local_api_key=key,
                        zotero_server_id=getattr(z, "server_id", None))
    return {"ok": bool(key), "remember": remember, "saved": bool(key and remember)}


def _client():
    """连接 Zotero 本地 API。

    读操作零配置即可；写操作需要 local_api_key（见 authorize_local 的说明）。
    过去这里把密钥兜底成字符串 "local"，写操作只会返回含糊的
    "Invalid or expired API key"；现在留空，Zotero 会给出更明确的
    "API key required -- POST /api/local/authorize"，错误更好定位。
    """
    cfg = _paths_cfg()
    sid = (os.environ.get("ZOTERO_SERVER_ID")
           or cfg.get("zotero_server_id") or None)
    return zotero.Zotero("0", "user", None, local=True,
                         local_api_key=(local_api_key() or None), server_id=sid)


WRITE_HINT = ("Zotero 写权限未授权。请到「设置 → Zotero 写入授权」点一下授权按钮，"
              "Zotero 会弹窗，选择「Always Allow」（永久允许）后再重试。")


def write_error_hint(err):
    """把 Zotero 的 401/428 鉴权错误翻译成可操作的中文提示；其它错误返回 None。"""
    s = repr(err) + str(err)
    if ("LocalAPIKeyRequired" in s or "API key required" in s
            or "Invalid or expired API key" in s):
        return WRITE_HINT
    if "428" in s or "Zotero-Server-ID" in s:
        return "Zotero 本地 API 要求 Server-ID 头，请重启 Zotero 后重试；若仍失败请重新授权。"
    return None


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


# ---------- 检索入库：分类 / 按元数据建条目 / 挂 PDF 附件 ----------

def title_key(s):
    """标题指纹：去掉非字母数字中文的字符并小写，用于跨源与库内比对。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def library_index():
    """库内已有的 DOI 集合 + 标题指纹集合，供检索结果查重。"""
    z = _client()
    dois, titles = set(), set()
    try:
        items = z.everything(z.top())
    except Exception:
        return {"dois": dois, "titles": titles}
    for it in items:
        d = it["data"]
        if d.get("itemType") in _SKIP_TYPES:
            continue
        if d.get("DOI"):
            dois.add(d["DOI"].strip().lower())
        if d.get("title"):
            titles.add(title_key(d["title"]))
    return {"dois": dois, "titles": titles}


def ensure_collection(name, parent=None):
    """按名称查分类，没有则新建，返回 collection key。"""
    z = _client()
    want = (name or "").strip()
    if not want:
        raise ValueError("分类名不能为空")
    try:
        for c in z.collections():
            if (c["data"].get("name") or "").strip() == want:
                return c["key"]
    except Exception:
        pass
    payload = {"name": want}
    if parent:
        payload["parentCollection"] = parent
    res = z.create_collections([payload])
    succ = res.get("successful") or {}
    for k in ("0", 0):
        if k in succ:
            return succ[k]["key"]
    raise RuntimeError(f"分类创建失败：{res.get('failed')}")


# 各条目类型允许写入的字段。Zotero 会拒绝不属于该类型的字段，故按类型白名单过滤。
_META_FIELDS = {
    "journalArticle": ("title", "abstractNote", "date", "publicationTitle",
                       "DOI", "url", "extra", "language"),
    "conferencePaper": ("title", "abstractNote", "date", "proceedingsTitle",
                        "conferenceName", "DOI", "url", "extra", "language"),
    "preprint": ("title", "abstractNote", "date", "repository", "archiveID",
                 "DOI", "url", "extra", "language"),
}


def _split_name(name):
    """把「姓, 名」或「名 姓」拆成 Zotero 的 lastName / firstName。"""
    nm = (name or "").strip()
    if not nm:
        return None
    if "," in nm:
        last, first = nm.split(",", 1)
        return {"lastName": last.strip(), "firstName": first.strip()}
    parts = nm.split()
    if len(parts) == 1:
        return {"lastName": parts[0], "firstName": ""}
    return {"lastName": parts[-1], "firstName": " ".join(parts[:-1])}


def create_item_from_meta(rec, collection_keys=None, tags=None):
    """按一条检索结果建顶层条目，返回新条目 key。

    rec 为 litsearch 的归一化记录（title / authors / year / venue / doi /
    url / abstract / type / venue_type / uid / source）。
    """
    z = _client()
    title = (rec.get("title") or "").strip()
    if not title:
        raise ValueError("缺少标题，无法建条目")

    vtype = (rec.get("venue_type") or "").strip()
    if (rec.get("type") or "") == "preprint" or rec.get("source") == "arxiv":
        itype = "preprint"
    elif vtype == "conference" or (rec.get("type") or "") == "proceedings-article":
        itype = "conferencePaper"
    else:
        itype = "journalArticle"

    creators = []
    for a in (rec.get("authors") or [])[:20]:
        c = _split_name(a)
        if c:
            c["creatorType"] = "author"
            creators.append(c)

    uid = rec.get("uid") or ""
    extra = "literature_flow 检索入库" + (f"\n{uid}" if uid else "")

    meta = {
        "title": title,
        "abstractNote": (rec.get("abstract") or "")[:4000],
        "date": str(rec.get("year") or ""),
        "DOI": (rec.get("doi") or "").strip(),
        "url": (rec.get("url") or "").strip(),
        "extra": extra,
        "language": "en",
    }
    venue = (rec.get("venue") or "").strip()
    if itype == "journalArticle":
        meta["publicationTitle"] = venue
    elif itype == "conferencePaper":
        meta["proceedingsTitle"] = venue
        meta["conferenceName"] = venue
    else:                                   # preprint
        meta["repository"] = "arXiv" if rec.get("source") == "arxiv" else (venue or "")
        if uid.startswith("arxiv:"):
            meta["archiveID"] = "arXiv:" + uid.split(":", 1)[1]

    keep = _META_FIELDS[itype]
    item = {k: v for k, v in meta.items() if k in keep and v not in ("", None)}
    item["itemType"] = itype
    item["creators"] = creators
    item["tags"] = [{"tag": t.strip()} for t in (tags or []) if t and t.strip()]
    if collection_keys:
        item["collections"] = list(collection_keys)

    res = z.create_items([item])
    succ = res.get("successful") or {}
    for k in ("0", 0):
        if k in succ:
            return succ[k]["key"]
    raise RuntimeError(f"条目创建失败：{res.get('failed')}")


def attach_pdf(parent, path, filename=None):
    """把本地 PDF 挂到指定条目：建 imported_file 附件条目 + 复制文件进 storage。"""
    from core.config import zotero_data_dir
    z = _client()
    name = filename or os.path.basename(path)
    res = z.create_items([{"itemType": "attachment", "linkMode": "imported_file",
                           "parentItem": parent, "filename": name,
                           "contentType": "application/pdf", "tags": []}])
    succ = res.get("successful") or {}
    att = None
    for k in ("0", 0):
        if k in succ:
            att = succ[k]["key"]
            break
    if not att:
        return {"ok": False, "error": f"附件条目创建失败：{res.get('failed')}"}
    dst = os.path.join(zotero_data_dir(), "storage", att)
    os.makedirs(dst, exist_ok=True)
    shutil.copy(path, os.path.join(dst, name))
    if not os.path.exists(os.path.join(dst, name)):
        try:
            z.delete_item(z.item(att))
        except Exception:
            pass
        return {"ok": False, "error": "附件落盘失败"}
    return {"ok": True, "att": att}
