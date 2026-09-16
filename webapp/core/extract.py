# -*- coding: utf-8 -*-
"""PDF 全文抽取（PyMuPDF，按页切分）。复用 zot_read.py 思路。"""
import os
import glob

import pymupdf as fitz  # PyMuPDF（pymupdf 是推荐的入口，避免 fitz 弃用警告）

from .config import ZOTERO_DATA_DIR


def extract_pdf(path):
    """返回 (meta, pages)。pages = [{'page': int, 'text': str}, ...]"""
    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc):
        pages.append({"page": i + 1, "text": page.get_text()})
    meta = {
        "title": (doc.metadata or {}).get("title", ""),
        "author": (doc.metadata or {}).get("author", ""),
        "pages": len(pages),
    }
    doc.close()
    return meta, pages


def pdf_to_text(path, max_chars=None):
    """抽取并按 PDF 页码拼成带标记的全文，供 LLM 阅读。"""
    meta, pages = extract_pdf(path)
    parts = [f"\n\n[PDF 第 {p['page']} 页]\n{p['text']}" for p in pages]
    text = "".join(parts).strip()
    if max_chars:
        text = text[:max_chars]
    return meta, text


def pdf_path_from_zotero_key(key):
    """给定 Zotero key（附件或父条目），返回本地 PDF 路径；找不到返回 None。"""
    from .zotero_io import _client
    z = _client()
    item = z.item(key)
    d = item["data"]
    if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
        ak = key
    else:
        ak = None
        for c in z.children(key):
            cd = c["data"]
            if cd.get("itemType") == "attachment" and cd.get("contentType") == "application/pdf":
                ak = cd["key"]
                break
        if not ak:
            return None
    cand = glob.glob(os.path.join(ZOTERO_DATA_DIR, "storage", ak, "*.pdf"))
    return cand[0] if cand else None
