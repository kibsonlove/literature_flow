# -*- coding: utf-8 -*-
"""知识库：MinerU 结构化文本 → 分块 → BGE-M3 向量 → SQLite → 语义检索。

设计要点：
- 唯一数据源仍是 Zotero；本模块只存"可再生的派生数据"（cache/kb.sqlite）。
- 分块保留来源信息：条目 key、标题、章节路径、块类型（正文/表格），便于回原文核对。
- 表格独立成块（检索到表格时能整块给出，不被截断）。
- 增量：按条目 PDF 内容哈希判断是否需要重建。
- 向量存 float32 BLOB，检索用 numpy 暴力余弦（万级块毫秒级）。
"""
import json
import os
import re
import sqlite3
import struct
import time
import urllib.request

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.normpath(os.path.join(_DIR, "..", "cache", "kb.sqlite"))
EMB_CFG = os.path.normpath(os.path.join(_DIR, "..", "config", "embedding.json"))

CHUNK_CHARS = 700          # 正文块目标长度
CHUNK_OVERLAP = 80         # 相邻块重叠（保持语义连续）
EMB_BATCH = 16             # 单次 embedding 的块数

# ---------------------------------------------------------------- 配置

def _cfg():
    return json.load(open(EMB_CFG, encoding="utf-8"))

def embedding_available():
    try:
        return bool(_cfg().get("api_key"))
    except Exception:
        return False

# ---------------------------------------------------------------- 向量

def embed_texts(texts, timeout=90):
    """调用 embedding 接口（SiliconFlow BGE-M3）。返回 list[list[float]]。"""
    cfg = _cfg()
    out = []
    for i in range(0, len(texts), EMB_BATCH):
        batch = [t[:3000] for t in texts[i:i + EMB_BATCH]]
        body = json.dumps({"model": cfg["model"], "input": batch}).encode()
        req = urllib.request.Request(cfg["base_url"], data=body, headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.load(r)
            out.extend(d["embedding"] for d in res["data"])
        except Exception as e:
            raise RuntimeError(f"embedding 调用失败：{repr(e)[:120]}")
    return out

def _vec_to_blob(v):
    return struct.pack(f"<{len(v)}f", *v)

def _blob_to_vec(b):
    return list(struct.unpack(f"<{len(b)//4}f", b))

# ---------------------------------------------------------------- 分块

def chunk_markdown(md, title=""):
    """把 MinerU Markdown 切成语义块。

    先整块抽出表格（含多行 HTML 表格），再按标题层级与长度切正文。
    返回 [{"kind": "text"|"table", "section": 章节路径, "text": 内容}]
    """
    tables = []

    def _grab(m):
        tables.append(re.sub(r"\s+", " ", m.group(0)))
        return f"\n@@TABLE{len(tables) - 1}@@\n"

    body = re.sub(r"<table[\s\S]*?</table>", _grab, md, flags=re.I)

    chunks = []
    section_stack = []
    buf = []

    def cur_section():
        return " > ".join(s[1] for s in section_stack) or title

    def flush():
        if not buf:
            return
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        if len(text) <= CHUNK_CHARS:
            chunks.append({"kind": "text", "section": cur_section(), "text": text})
            return
        start = 0
        while start < len(text):
            chunks.append({"kind": "text", "section": cur_section(),
                           "text": text[start:start + CHUNK_CHARS]})
            if start + CHUNK_CHARS >= len(text):
                break
            start += CHUNK_CHARS - CHUNK_OVERLAP

    for raw in body.split("\n"):
        line = raw.rstrip()
        mt = re.match(r"^@@TABLE(\d+)@@$", line.strip())
        if mt:
            flush()
            chunks.append({"kind": "table", "section": cur_section(),
                           "text": tables[int(mt.group(1))]})
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level, htext = len(m.group(1)), m.group(2).strip()
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, htext))
            continue
        buf.append(line)
    flush()
    return [c for c in chunks if len(c["text"]) >= 30]


# ---------------------------------------------------------------- 存储

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_key TEXT, title TEXT, section TEXT, kind TEXT,
        text TEXT, vec BLOB, model TEXT, dim INTEGER, ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS indexed (
        item_key TEXT PRIMARY KEY, content_hash TEXT, n_chunks INTEGER, ts REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_key)")
    try:      # 关键词检索（BM25）；trigram 对中文与专名子串更友好
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, item_key UNINDEXED, tokenize='trigram')""")
        c.execute("CREATE TABLE IF NOT EXISTS qtrans (q TEXT PRIMARY KEY, other TEXT, ts REAL)")
    except Exception:
        pass
    return c

def stats():
    c = _conn()
    n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_items = c.execute("SELECT COUNT(*) FROM indexed").fetchone()[0]
    by_kind = dict(c.execute("SELECT kind, COUNT(*) FROM chunks GROUP BY kind").fetchall())
    c.close()
    return {"items": n_items, "chunks": n_chunks, "by_kind": by_kind,
            "db": DB_PATH, "model": (_cfg().get("model") if embedding_available() else None)}

def _cache_entries():
    """扫描 MinerU 缓存目录，返回 [(item_key, md_path)]（全在本地工作区内）。"""
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(_DIR, "..")))
    from core.mineru import CACHE_DIR
    CACHE_DIR = os.path.normpath(CACHE_DIR)
    out = []
    if not os.path.isdir(CACHE_DIR):
        return out
    for fn in os.listdir(CACHE_DIR):
        if not fn.endswith(".map"):
            continue
        key = fn[:-4]
        try:
            h = open(os.path.join(CACHE_DIR, fn), encoding="utf-8").read().strip()
        except Exception:
            continue
        md = os.path.join(CACHE_DIR, f"{h}.md")
        if h and os.path.exists(md):
            out.append((key, os.path.normpath(md)))
    return out


def unindexed_items(only_cached=True):
    """待索引的 (条目 key, MinerU 缓存路径)：缓存存在但未建索引的。"""
    c = _conn()
    done = {r[0] for r in c.execute("SELECT item_key FROM indexed").fetchall()}
    c.close()
    return [{"key": k, "title": k, "md": md}
            for k, md in _cache_entries() if k not in done]


def index_item(item_key, md_path=None, title="", force=False, log=lambda m: None):
    """索引一篇文献（数据源：MinerU 缓存 Markdown）。返回 {"ok", "chunks", "skipped"}"""
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(_DIR, "..")))
    from core.mineru import cache_file

    if not md_path:
        md_path = cache_file(item_key)
    if not md_path or not os.path.exists(md_path):
        return {"ok": False, "error": "无 MinerU 缓存（先解析该 PDF）"}
    h = os.path.splitext(os.path.basename(md_path))[0]      # 缓存名 = PDF 内容哈希

    c = _conn()
    row = c.execute("SELECT content_hash, n_chunks FROM indexed WHERE item_key=?",
                    (item_key,)).fetchone()
    if row and row[0] == h and not force:
        c.close()
        return {"ok": True, "skipped": True, "chunks": row[1]}

    md = open(md_path, encoding="utf-8").read()
    if not title or title == item_key:
        try:
            from core.zotero_io import _client
            title = _client().item(item_key)["data"].get("title", "") or item_key
        except Exception:
            title = item_key
    chunks = chunk_markdown(md, title=title)
    if not chunks:
        c.close()
        return {"ok": False, "error": "分块为空"}
    log(f"  {title[:36]}：{len(chunks)} 块，向量化中…")
    vecs = embed_texts([(ch["section"] + "\n" + ch["text"]) for ch in chunks])
    cfg = _cfg()
    c.execute("DELETE FROM chunks WHERE item_key=?", (item_key,))
    try:
        c.execute("DELETE FROM chunks_fts WHERE item_key=?", (item_key,))
    except Exception:
        pass
    c.executemany(
        "INSERT INTO chunks (item_key,title,section,kind,text,vec,model,dim,ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(item_key, title, ch["section"], ch["kind"], ch["text"],
          _vec_to_blob(v), cfg["model"], len(v), time.time())
         for ch, v in zip(chunks, vecs)])
    try:
        c.executemany("INSERT INTO chunks_fts (text, item_key) VALUES (?,?)",
                      [(ch["section"] + "\n" + ch["text"], item_key) for ch in chunks])
    except Exception:
        pass
    c.execute("INSERT OR REPLACE INTO indexed (item_key,content_hash,n_chunks,ts) VALUES (?,?,?,?)",
              (item_key, h, len(chunks), time.time()))
    c.commit()
    c.close()
    return {"ok": True, "chunks": len(chunks)}


# ---------------------------------------------------------------- 检索

_TRANS_PROMPT = """把下面的检索问题翻译成{lang}，只输出译文本身，不要解释、不要引号：
{query}"""

def _is_chinese(text):
    zh = sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")
    return zh * 3 >= max(len(text or ""), 1)

def other_language_query(query, timeout=30):
    """把查询翻译成另一种语言（中文↔英文），带本地缓存。失败返回 None。"""
    try:
        c = _conn()
        row = c.execute("SELECT other FROM qtrans WHERE q=?", (query,)).fetchone()
        if row:
            c.close()
            return row[0] or None
    except Exception:
        c = None
    target = "English" if _is_chinese(query) else "中文"
    try:
        from .read import _llm_chat
        out = _llm_chat(_TRANS_PROMPT.replace("{lang}", target).replace("{query}", query),
                        system="你是翻译助手，只输出译文。", timeout=timeout).strip()
        out = out.strip('"“” ')
        out = out if out and out.lower() != query.lower() else None
    except Exception:
        out = None
    try:
        if c is not None:
            c.execute("INSERT OR REPLACE INTO qtrans (q, other, ts) VALUES (?,?,?)",
                      (query, out or "", time.time()))
            c.commit()
            c.close()
    except Exception:
        pass
    return out


def _fts_search(queries, limit=40):
    """FTS5 关键词检索，返回 {chunk_id: 命中排名} 与 {chunk_id: bm25 分}。"""
    out = {}
    try:
        c = _conn()
        for qi, q in enumerate(queries):
            if not q or not q.strip():
                continue
            # trigram 检索需要至少 3 个字符；用引号包裹整体做子串查询
            terms = [t for t in re.split(r"[\s,，。;；:：]+", q) if len(t) >= 3][:12]
            if not terms:
                continue
            expr = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                rows = c.execute(
                    "SELECT rowid, bm25(chunks_fts) AS s FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?", (expr, limit)).fetchall()
            except Exception:
                continue
            for rank, (rid, score) in enumerate(rows):
                # bm25 越小越相关 → 转成 0..1 的增益分
                gain = 1.0 / (1.0 + rank)
                if rid not in out or gain > out[rid]:
                    out[rid] = gain
        c.close()
    except Exception:
        pass
    return out


def search(query, top_k=8, min_score=0.25, per_item=3, expand=True):
    """语义检索（向量）+ 关键词检索（FTS5 BM25）融合，支持查询双语扩展。

    评分：final = 0.75 * 向量分 + 0.25 * 关键词增益
    - 向量分：原问题与（若开启扩展）另一语言版本的最大余弦
    - 关键词增益：FTS5 命中排名折算（专名/数字靠这条兜底）
    """
    if not query.strip():
        return []
    queries = [query]
    if expand:
        alt = other_language_query(query)
        if alt:
            queries.append(alt)
    vecs = embed_texts(queries)
    qvs = [np.array(v, dtype=np.float32) for v in vecs]
    qns = [(np.linalg.norm(v) or 1.0) for v in qvs]

    c = _conn()
    rows = c.execute("SELECT id,item_key,title,section,kind,text,vec FROM chunks").fetchall()
    c.close()
    if not rows:
        return []
    fts = _fts_search(queries)

    scored = []
    for cid, item_key, title, section, kind, text, blob in rows:
        v = np.array(_blob_to_vec(blob), dtype=np.float32)
        vn = np.linalg.norm(v) or 1.0
        cos = max(float(np.dot(qv, v) / (qn * vn)) for qv, qn in zip(qvs, qns))
        gain = fts.get(cid, 0.0)
        if cos < min_score and gain <= 0:
            continue
        score = 0.75 * cos + 0.25 * gain
        src = ("向量+关键词" if (cos >= min_score and gain > 0)
               else ("关键词" if gain > 0 else "向量"))
        scored.append((score, cos, gain, src, cid, item_key, title, section, kind, text))
    scored.sort(key=lambda x: -x[0])

    def _overlap(a, b):
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        probe = re.sub(r"\s+", "", short)[:120]
        return bool(probe) and probe in re.sub(r"\s+", "", long_)

    out, per_count = [], {}
    for score, cos, gain, src, cid, item_key, title, section, kind, text in scored:
        if per_count.get(item_key, 0) >= per_item:
            continue
        if any(o["item_key"] == item_key and _overlap(text, o["full"]) for o in out):
            continue
        per_count[item_key] = per_count.get(item_key, 0) + 1
        out.append({
            "score": round(score, 3), "vec_score": round(cos, 3), "kw_gain": round(gain, 2),
            "source": src, "item_key": item_key, "title": title,
            "section": section, "kind": kind,
            "snippet": re.sub(r"\s+", " ", text)[:400] if kind != "table" else text[:6000],
            "full": text[:1500],
        })
        if len(out) >= top_k:
            break
    return out


def rebuild_fts():
    """把已有 chunks 重建进 FTS 表（新增关键词检索后的一次性迁移）。"""
    c = _conn()
    c.execute("DELETE FROM chunks_fts")
    rows = c.execute("SELECT id,item_key,section,text FROM chunks").fetchall()
    c.executemany("INSERT INTO chunks_fts (rowid, text, item_key) VALUES (?,?,?)",
                  [(r[0], (r[2] or "") + "\n" + (r[3] or ""), r[1]) for r in rows])
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    c.close()
    return n
