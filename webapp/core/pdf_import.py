# -*- coding: utf-8 -*-
"""PDF 迁移入库：浏览器下载的 PDF → Zotero 条目 + 附件，可验证后删除原文件。

入库机制（已实测）：父条目 create_items + imported_file 附件条目 + PDF 复制进 storage/<附件key>/。
元数据：PDF 前两页提取 DOI → Crossref 免费查询补全标题/作者/期刊/年份；失败用文件名。
原文件清理：入库成功记录到 imports_log（文件名+大小）；删除前必须与 log 匹配，防误删。
"""
import os
import re
import json
import shutil
import datetime

import fitz  # PyMuPDF
import urllib.request

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "imports_log.json")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>,；;]+")

def _log_path():
    return _LOG

def _load_log():
    try:
        return json.load(open(_log_path(), encoding="utf-8"))
    except Exception:
        return []

def _save_log(entries):
    json.dump(entries, open(_log_path(), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def extract_doi(path):
    """PDF 前两页文本提取 DOI。"""
    try:
        doc = fitz.open(path)
        text = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
        doc.close()
        m = DOI_RE.search(text)
        return m.group(0).rstrip(".、，,") if m else None
    except Exception:
        return None

def crossref_metadata(doi, timeout=20):
    """Crossref 查询元数据（免费，无需 key）。返回 dict 或 None。"""
    try:
        req = urllib.request.Request(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": "lit-import/1.0 (mailto:user@example.com)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json
            msg = json.load(r)["message"]
        authors = [{"creatorType": "author", "lastName": a.get("family", ""),
                    "firstName": a.get("given", "")}
                   for a in msg.get("author", [])[:8] if a.get("family") or a.get("name")]
        year = ""
        for k in ("published-print", "published-online", "issued"):
            parts = (msg.get(k) or {}).get("date-parts") or []
            if parts and parts[0]:
                year = str(parts[0][0]); break
        return {
            "title": (msg.get("title") or [""])[0],
            "creators": authors,
            "journalAbbreviation": (msg.get("container-title") or [""])[0],
            "date": year,
            "doi": doi,
        }
    except Exception:
        return None

JUNK_PAT = re.compile(
    r"\s*[\(（\[]\s*(?:z-?library|z-?lib|1lib|libgen|annas?-?archive|sci-hub)[^\)）\]]*[\)）\]]"
    r"|\s*[\(（\[]\s*www\.[^\)）\]]*[\)）\]]"
    r"|\s*\(\s*([^()]*\.pdf)\s*\)", re.I)


def guess_title_from_filename(path):
    """从文件名生成标题：清理下载站标记（z-library/1lib…）、重复的 .pdf 括号、结尾序号。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = JUNK_PAT.sub("", stem)
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    return stem.replace("_", " ").strip() or os.path.splitext(os.path.basename(path))[0]


def clean_filename(name):
    """（供外部调用）清理文件名里的下载站垃圾。"""
    return guess_title_from_filename(name)

def build_item(path, log=None):
    """构造父条目元数据：DOI→Crossref 优先，文件名兜底。"""
    meta = {"itemType": "journalArticle", "tags": [{"tag": "lit_import"}]}
    doi = extract_doi(path)
    cr = crossref_metadata(doi) if doi else None
    if cr and cr.get("title"):
        meta.update({k: v for k, v in cr.items() if v})
        if log:
            log(f"DOI {doi} → Crossref 元数据")
    else:
        meta["title"] = guess_title_from_filename(path)
        if log:
            log(f"无 DOI/Crossref，用文件名做标题：{meta['title'][:40]}")
    meta.setdefault("title", guess_title_from_filename(path))
    return meta


def import_pdf(z, path, log=None, auto_mineru=True):
    """入库单个 PDF：父条目 + imported_file 附件 + 文件落 storage。返回 dict。"""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    meta = build_item(path, log=log)
    r1 = z.create_items([meta])
    if not r1.get("successful"):
        return {"ok": False, "file": name, "error": "父条目创建失败"}
    parent = list(r1["successful"].values())[0]["key"]
    r2 = z.create_items([{"itemType": "attachment", "linkMode": "imported_file",
                          "parentItem": parent, "filename": name,
                          "contentType": "application/pdf", "tags": []}])
    if not r2.get("successful"):
        z.delete_item(z.item(parent))
        return {"ok": False, "file": name, "error": "附件条目创建失败"}
    att = list(r2["successful"].values())[0]["key"]
    from core.config import ZOTERO_DATA_DIR
    dst = os.path.join(ZOTERO_DATA_DIR, "storage", att)
    os.makedirs(dst, exist_ok=True)
    shutil.copy(path, os.path.join(dst, name))
    if not os.path.exists(os.path.join(dst, name)):
        z.delete_item(z.item(parent))
        return {"ok": False, "file": name, "error": "附件落盘失败"}
    entries = _load_log()
    entries.append({"name": name, "size": size, "item": parent, "att": att,
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "deleted": False})
    _save_log(entries)
    if log:
        log(f"✓ 入库：{name[:50]} → 条目 {parent}")
    # MinerU 后台预解析（表格保真，写入 cache/mineru/<条目key>.md；批量笔记直接命中缓存）
    import threading
    stored = os.path.join(dst, name)
    def _bg():
        try:
            from core import mineru
            if mineru.mineru_available():
                mineru.get_tables_markdown(stored, item_key=parent,
                                           log=lambda m: print(f"[mineru] {m}", flush=True))
        except Exception:
            pass
    if auto_mineru:
        threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "file": name, "item": parent, "att": att,
            "title": meta["title"][:60], "mineru": "后台解析已启动"}

# ---------- 下载文件夹清理 ----------

def _downloads_dir():
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "downloads_dir.txt")
    try:
        d = open(cfg, encoding="utf-8").read().strip()
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")

def set_downloads_dir(d):
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "downloads_dir.txt")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    open(cfg, "w", encoding="utf-8").write(d)

def scan_downloads():
    d = _downloads_dir()
    log = {e["name"] + "|" + str(e["size"]): e for e in _load_log() if not e.get("deleted")}
    out = []
    try:
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith(".pdf"):
                continue
            p = os.path.join(d, f)
            if not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            hit = log.get(f + "|" + str(size))
            out.append({"name": f, "size": size, "mtime": datetime.datetime.fromtimestamp(
                os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M"), "imported": bool(hit),
                "item": (hit or {}).get("item", "")})
    except FileNotFoundError:
        pass
    return out

def delete_downloaded(name, size):
    """删除下载夹里已确认入库的 PDF（名称+大小与 imports_log 匹配才执行）。"""
    d = _downloads_dir()
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在"}
    if os.path.getsize(p) != size:
        return {"ok": False, "error": "大小不匹配，拒绝删除"}
    log = _load_log()
    hit = next((e for e in log if e["name"] == name and e["size"] == size and not e.get("deleted")), None)
    if not hit:
        return {"ok": False, "error": "无入库记录，拒绝删除（防误删）"}
    os.remove(p)
    hit["deleted"] = True
    _save_log(log)
    return {"ok": True}


def imported_list():
    """返回已入库且未清理的文件记录 [{name,size}]，供客户端匹配。"""
    return [{"name": e["name"], "size": e["size"]}
            for e in _load_log() if not e.get("deleted")]

def mark_deleted(name, size):
    """客户端已删除原文件后，更新入库记录（校验名称+大小）。"""
    log = _load_log()
    hit = next((e for e in log if e["name"] == name and e["size"] == size and not e.get("deleted")), None)
    if not hit:
        return {"ok": False, "error": "无入库记录，无法标记"}
    hit["deleted"] = True
    _save_log(log)
    return {"ok": True}
