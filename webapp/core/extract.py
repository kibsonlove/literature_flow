# -*- coding: utf-8 -*-
"""PDF 全文抽取（PyMuPDF，按页切分）。复用 zot_read.py 思路。"""
import os
import glob

import pymupdf as fitz  # PyMuPDF（pymupdf 是推荐的入口，避免 fitz 弃用警告）

from .config import zotero_data_dir


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


def pdf_health(path, min_bytes=512):
    """PDF 可用性体检：拦掉「必然挂载失败」的文件（0 字节 / 下载中断 / 损坏 / 假 PDF）。

    背景：storage 里存在 .pdf 不代表它是好 PDF——下载中断会留下几十字节的残片，
    网页错误页被"另存为 .pdf"也很常见。这类文件传到网页端必然失败，且会白等一整个超时窗口。
    判据刻意从严到宽、且不误杀：
      ① 能读到文件、大小 ≥ min_bytes（残片通常只有几十~几百字节）
      ② 文件头含 %PDF-（真 PDF 铁证；错误页存成的 .pdf 是 <html>，一票否决且零误杀）
      ③ PyMuPDF 能打开且页数 ≥ 1
    扫描件（无文字层）算**可用**，只在 scanned 上给提示（有 MinerU/OCR 兜底）。

    返回 dict：{ok, reason, size, pages, chars, scanned}
    """
    info = {"ok": False, "reason": "", "size": 0, "pages": 0, "chars": 0, "scanned": False}
    if not path:
        info["reason"] = "未找到 PDF 路径"
        return info
    if not os.path.isfile(path):
        info["reason"] = "文件不存在"
        return info
    try:
        size = os.path.getsize(path)
    except Exception as e:
        info["reason"] = f"无法读取文件大小：{repr(e)[:60]}"
        return info
    info["size"] = size
    if size < min_bytes:
        info["reason"] = f"文件仅 {size} 字节（疑似下载中断的残片或占位文件）"
        return info
    # 文件头校验：%PDF- 允许出现在前 1024 字节内，真 PDF 一定命中，网页/错误页一定不命中
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except Exception as e:
        info["reason"] = f"无法读取文件：{repr(e)[:60]}"
        return info
    if b"%PDF-" not in head:
        info["reason"] = "缺少 %PDF- 文件头（疑似网页/错误页被另存为 .pdf）"
        return info
    try:
        doc = fitz.open(path)
    except Exception as e:
        info["reason"] = f"无法打开（疑似损坏）：{repr(e)[:80]}"
        return info
    try:
        n = doc.page_count
        info["pages"] = n
        if n < 1:
            info["reason"] = "页数为 0（文件可能未下载完）"
            return info
        chars = 0
        for i in range(min(3, n)):
            try:
                chars += len(doc[i].get_text() or "")
            except Exception:
                continue
        info["chars"] = chars
        if chars == 0:
            info["scanned"] = True  # 无文字层：仍可用，但需 MinerU/OCR
    finally:
        try:
            doc.close()
        except Exception:
            pass
    info["ok"] = True
    info["reason"] = "OK" if not info["scanned"] else "OK（无文字层，疑似扫描件，依赖 MinerU/OCR）"
    return info


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
    cand = glob.glob(os.path.join(zotero_data_dir(), "storage", ak, "*.pdf"))
    return cand[0] if cand else None
