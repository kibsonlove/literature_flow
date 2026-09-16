#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 Zotero 文库按 itemKey 抽取 PDF 全文（按页切分），输出到 .cache/。
绕过 Zotero 10 失效的 /fulltext 本地 API，直接用 PyMuPDF 读 storage 下的 PDF。
只读，不写文库。
"""
import sys, os, glob, json, urllib.request, urllib.parse
import fitz  # PyMuPDF

BASE = "http://localhost:23119"
# Zotero 数据目录（含 storage/ 子目录）：从 webapp/config/paths.json 读取（本地文件，不入库）
_paths = {}
try:
    _paths = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "webapp", "config", "paths.json"), encoding="utf-8"))
except Exception:
    _paths = {}
DATA_DIR = _paths.get("zotero_data_dir", "")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
os.makedirs(CACHE, exist_ok=True)


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "zot/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_pdf_path(itemkey):
    """返回 (pdf_attachment_key, 本地pdf路径)。优先取有 storage 文件的附件。"""
    ch = get(f"/api/users/0/items/{itemkey}/children", {"format": "json"})
    for c in ch:
        d = c.get("data", {})
        if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
            ak = c["key"]
            cand = glob.glob(os.path.join(DATA_DIR, "storage", ak, "*.pdf"))
            if cand:
                return ak, cand[0]
    return None, None


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: zot_read.py <itemKey>"}, ensure_ascii=False))
        return
    itemkey = sys.argv[1]
    ak, path = get_pdf_path(itemkey)
    if not path:
        print(json.dumps({"error": "no pdf found", "itemKey": itemkey}, ensure_ascii=False))
        return
    doc = fitz.open(path)
    meta = doc.metadata or {}
    out_path = os.path.join(CACHE, f"{itemkey}_fulltext.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# {meta.get('title','')}\n")
        f.write(f"# itemKey={itemkey} pdfKey={ak} pages={doc.page_count} file={os.path.basename(path)}\n\n")
        for i in range(doc.page_count):
            page = doc[i]
            f.write(f"\n===== PAGE {i+1} =====\n")
            f.write(page.get_text())
    print(json.dumps({
        "ok": True, "itemKey": itemkey, "pdfKey": ak, "pdfPath": path,
        "title": meta.get("title"), "npages": doc.page_count, "out": out_path,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
