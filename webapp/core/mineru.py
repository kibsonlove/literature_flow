# -*- coding: utf-8 -*-
"""MinerU 云 API 接入：PDF → 高保真 Markdown（表格保真）→ 提取表格块供精读管线使用。

流程（MinerU v4 API）：
    1. POST /api/v4/file-urls/batch   申请上传链接（含 batch_id）
    2. PUT  PDF 到上传链接
    3. GET  /api/v4/extract-results/batch/{batch_id}  轮询解析状态
    4. 下载 full_zip_url → 解出 full.md → 存缓存 → 抽取表格块

缓存：<数据目录>/mineru/<item_key 或文件名哈希>.md，命中即不再调用 API。
     数据目录默认在项目内 webapp/cache，可在页面「设置 → 本机路径」改到别的盘。
Key：config/mineru_key.txt（不入 git）；也可以在页面「设置」里直接填。
"""
import io
import os
import re
import tempfile
import time
import zipfile
import hashlib
import urllib.request
import urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = "https://mineru.net/api/v4"
POLL_INTERVAL = 8        # 秒
POLL_TIMEOUT = 600       # 单篇最长等待 10 分钟


def key_file():
    """MinerU key 文件：config/mineru_key.txt。

    早期版本把它放在 core/（代码目录）里——用户数据不该混进代码目录，已改到 config/；
    旧位置若还存在仍然读，避免已配好的用户失效。
    """
    new = os.path.normpath(os.path.join(_DIR, "..", "config", "mineru_key.txt"))
    old = os.path.join(_DIR, "mineru_key.txt")
    if os.path.isfile(new):
        return new
    return old if os.path.isfile(old) else new


def cache_dir():
    """MinerU 解析缓存目录（跟随「数据目录」设置）。"""
    from .config import data_path
    return os.path.normpath(data_path("mineru"))


def _key():
    try:
        with open(key_file(), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def _headers():
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lit-collect/1.0"}

def mineru_available():
    return bool(_key())

def _content_hash(pdf_path):
    """PDF 内容哈希（前 1MB + 文件大小）：同一份 PDF 无论挂在哪个条目下都算同一份。"""
    h = hashlib.md5()
    h.update(str(os.path.getsize(pdf_path)).encode())
    with open(pdf_path, "rb") as f:
        h.update(f.read(1024 * 1024))
    return h.hexdigest()[:16]


def _cache_path(item_key, pdf_path):
    """缓存文件按内容哈希命名；<条目key>.map 记录条目 → 哈希 的映射。"""
    h = _content_hash(pdf_path)
    if item_key:
        try:
            cd = cache_dir()
            os.makedirs(cd, exist_ok=True)
            open(os.path.join(cd, f"{item_key}.map"), "w", encoding="utf-8").write(h)
        except Exception:
            pass
    return os.path.normpath(os.path.join(cache_dir(), f"{h}.md"))

def _post(url, payload):
    req = urllib.request.Request(url, data=json_dumps(payload), headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json_loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return {"code": -1, "msg": f"HTTP {e.code}: {body}"}

def _get(url):
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json_loads(r.read())

def json_dumps(obj):
    import json
    return json.dumps(obj).encode("utf-8")

def json_loads(b):
    import json
    return json.loads(b.decode("utf-8"))

def _call_api(pdf_path, log):
    """走完整 v4 流程，返回 markdown 全文或 None。"""
    key = _key()
    if not key:
        return None
    name = os.path.basename(pdf_path)
    # 1. 申请上传链接
    res = _post(f"{API_BASE}/file-urls/batch", {
        "enable_formula": True, "enable_table": True, "language": "ch",
        "files": [{"name": name, "is_ocr": True}],
    })
    if res.get("code") != 0:
        log(f"  ✗ MinerU 申请上传链接失败：{res}")
        return None
    batch_id = res["data"]["batch_id"]
    upload_url = res["data"]["file_urls"][0]
    # 2. PUT 上传（签名 URL 对请求头敏感：用 http.client 裸传，不带 Content-Type/UA）
    pdf_bytes = open(pdf_path, "rb").read()
    import http.client
    from urllib.parse import urlparse
    u = urlparse(upload_url)
    conn = http.client.HTTPSConnection(u.netloc, timeout=300)
    path = u.path + (("?" + u.query) if u.query else "")
    conn.request("PUT", path, body=pdf_bytes)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    if resp.status not in (200, 203):
        log(f"  ✗ 上传失败 HTTP {resp.status}")
        return None
    log(f"  ↥ 已上传 {name}（{len(pdf_bytes)//1024}KB），等待 MinerU 解析…")
    # 3. 轮询
    deadline = time.time() + POLL_TIMEOUT
    zip_url = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = _get(f"{API_BASE}/extract-results/batch/{batch_id}")
        if st.get("code") != 0:
            log(f"  ✗ MinerU 状态查询失败：{st}")
            return None
        arr = (st.get("data") or {}).get("extract_result") or []
        if not arr:
            continue
        state = arr[0].get("state")
        if state == "done":
            zip_url = arr[0].get("full_zip_url")
            break
        if state == "failed":
            log(f"  ✗ MinerU 解析失败：{arr[0].get('err_msg')}")
            return None
        log(f"  … MinerU 解析中（{state}）")
    if not zip_url:
        log("  ✗ MinerU 解析超时。")
        return None
    # 4. 下载 zip → 取最大的 .md（full.md）
    with urllib.request.urlopen(zip_url, timeout=120) as r:
        zf = zipfile.ZipFile(io.BytesIO(r.read()))
    mds = [n for n in zf.namelist() if n.endswith(".md")]
    if not mds:
        log("  ✗ MinerU 压缩包内无 markdown。")
        return None
    md_name = max(mds, key=lambda n: zf.getinfo(n).file_size)
    md = zf.read(md_name).decode("utf-8", errors="replace")
    # markdown 里的图片引用在文本场景无用，清掉
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    return md

def extract_tables(md):
    """从 markdown 抽取表格块：MinerU 输出 HTML <table>，也兼容 pipe 表格。"""
    blocks = []
    for m in re.finditer(r"<table[\s\S]*?</table>", md, re.I):
        blocks.append(m.group(0))
    cur = []
    for line in md.split("\n"):
        if "|" in line and line.strip() and not re.search(r"<table", line, re.I):
            cur.append(line.strip())
        else:
            if len(cur) >= 2:
                blocks.append("\n".join(cur))
            cur = []
    if len(cur) >= 2:
        blocks.append("\n".join(cur))
    return blocks

PAGE_LIMIT = 180          # MinerU 单文件 200 页上限，留余量

def _pdf_page_count(pdf_path):
    try:
        import pymupdf as fitz
    except Exception:
        import fitz
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def _parse_split(pdf_path, log=lambda m: None, part_pages=PAGE_LIMIT):
    """超过 MinerU 页数上限时：切分 PDF → 逐片解析 → 合并 Markdown。"""
    try:
        import pymupdf as fitz
    except Exception:
        import fitz
    n = _pdf_page_count(pdf_path)
    parts = []
    tmp_files = []
    log(f"   文件共 {n} 页，超过 MinerU 单文件上限 → 分片解析（每片 {part_pages} 页）")
    try:
        with fitz.open(pdf_path) as src:
            for start in range(0, n, part_pages):
                end = min(start + part_pages, n) - 1
                out = fitz.open()
                out.insert_pdf(src, from_page=start, to_page=end)
                tmp = os.path.join(tempfile.gettempdir(), f"mineru_part{len(parts) + 1}_{os.getpid()}.pdf")
                out.save(tmp)
                out.close()
                tmp_files.append(tmp)
                parts.append((tmp, start + 1, end + 1))
        mds = []
        for i, (tmp, p1, p2) in enumerate(parts, 1):
            log(f"   解析分片 {i}/{len(parts)}（第 {p1}–{p2} 页）…")
            md = _call_api(tmp, log)
            if not md:
                log(f"   ✗ 分片 {i} 解析失败")
                continue
            mds.append(f"\n\n<!-- ===== 第 {p1}–{p2} 页 ===== -->\n\n" + md)
        return "\n".join(mds) if mds else None
    finally:
        for f in tmp_files:
            try:
                os.remove(f)
            except BaseException:
                pass


def ensure_cache(pdf_path, item_key=None, log=print, force=False):
    """确保该 PDF 的 MinerU 全文缓存存在，返回缓存路径（失败返回 None）。

    与 get_tables_markdown 的区别：只关心"全文 Markdown 是否落盘"，
    供长文精读等需要完整章节结构的流程使用（无表格的文献也能拿到全文）。
    """
    cp = _cache_path(item_key, pdf_path)
    if os.path.exists(cp) and not force:
        return cp
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    # 解析前先看页数：超过 MinerU 单文件上限（200 页）直接分片，省一次失败的调用
    n_pages = 0
    try:
        n_pages = _pdf_page_count(pdf_path)
    except Exception:
        n_pages = 0
    md = None
    if n_pages > PAGE_LIMIT:
        md = _parse_split(pdf_path, log=log)
    else:
        try:
            md = _call_api(pdf_path, log)
        except Exception as e:
            msg = repr(e)
            if "pages exceeds limit" in msg or "split the file" in msg:
                log(f"   MinerU 拒绝：{msg[:120]}")
                md = _parse_split(pdf_path, log=log)
            else:
                raise
    if not md:
        return None
    open(cp, "w", encoding="utf-8").write(md)
    return cp


def get_tables_markdown(pdf_path, item_key=None, log=print, force=False):
    """供精读管线调用：返回该 PDF 的表格结构化 markdown（无表格/失败返回 None）。"""
    cp = _cache_path(item_key, pdf_path)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    md = None
    if os.path.exists(cp) and not force:
        md = open(cp, encoding="utf-8").read()
    else:
        try:
            md = _call_api(pdf_path, log)
        except Exception as e:
            log(f"  ✗ MinerU 调用异常（回退 PyMuPDF）：{repr(e)[:100]}")
            return None
        if md is not None:
            open(cp, "w", encoding="utf-8").write(md)
    if not md:
        return None
    blocks = extract_tables(md)
    if not blocks:
        return None
    out = "\n\n".join(blocks)
    # 控制体量：表格最多 12000 字符
    return out[:12000]


def tables_info(tables_text):
    """从抽取出的表格文本里返回 (表格数, 表头列表)。"""
    headers = []
    for m in re.finditer(r"<table[\s\S]*?</table>", tables_text or "", re.I):
        first_row = re.search(r"<tr>([\s\S]*?)</tr>", m.group(0), re.I)
        if not first_row:
            continue
        cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", first_row.group(1), re.I)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        cells = [c for c in cells if c][:6]
        if cells:
            headers.append("·".join(cells))
    return len(headers), headers


def cache_file(item_key):
    """该条目的 MinerU 缓存文件路径（不存在返回 None）。

    命名：内容哈希 <hash>.md（经 <item_key>.map 映射）；优先按 PDF 实时内容哈希定位，
    map 仅作兜底。
    """
    if not item_key:
        return None
    try:
        from .extract import pdf_path_from_zotero_key
        pp = pdf_path_from_zotero_key(item_key)
        if pp:
            cand = os.path.normpath(os.path.join(cache_dir(), f"{_content_hash(pp)}.md"))
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    mp = os.path.join(cache_dir(), f"{item_key}.map")
    try:
        h = open(mp, encoding="utf-8").read().strip()
        if h:
            cand = os.path.normpath(os.path.join(cache_dir(), f"{h}.md"))
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    old = os.path.normpath(os.path.join(cache_dir(), f"{item_key}.md"))
    return old if os.path.exists(old) else None


def note_meta(note, label, text, url=None, url_text="查看解析"):
    """在笔记元信息区插入一行「- {label}：{text}（可选链接）」。

    - HTML 笔记：插成 <p> 行，URL 变成 <a> 可点击链接；
    - Markdown 笔记：插成列表行，URL 变成 [text](url)。
    """
    is_html = "</h1>" in note or "<h1" in note or "<p>" in note
    if url and is_html:
        body = f'{text} ｜ <a href="{url}">{url_text}</a>'
        line = f"<p>- {label}：{body}</p>"
    elif url:
        line = f"- {label}：{text} ｜ [{url_text}]({url})"
    else:
        line = f"<p>- {label}：{text}</p>" if is_html else f"- {label}：{text}"

    m = re.match(r"\s*# [^\n]+\n", note)
    if m:  # markdown：插到标题行之后
        i = m.end()
        return note[:i] + line + "\n" + note[i:]
    if "</h1>" in note:  # html：插到 </h1> 之后
        return note.replace("</h1>", "</h1>\n" + line, 1)
    return line + "\n" + note


def note_marker(note, text):
    """兼容旧调用：MinerU 表格增强标注。"""
    return note_meta(note, "MinerU 表格增强", text)


SNAPSHOT_TITLE = "MinerU 解析（表格保真）"

def snapshot_note(item_key, md_path=None, max_chars=200000, log=lambda m: None):
    """把 MinerU 解析存为该条目的 Zotero 子笔记（已存在则更新，不重复创建）。

    Zotero 内可直接阅读/搜索/离线可用；表格用 HTML 表格呈现。
    """
    import markdown as _md
    from .zotero_io import _client

    path = md_path or cache_file(item_key)
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "无 MinerU 缓存"}
    raw = open(path, encoding="utf-8").read()
    truncated = len(raw) > max_chars
    body = _md.markdown(raw[:max_chars], extensions=["tables", "fenced_code"])
    url = f"http://127.0.0.1:8000/api/mineru/{item_key}"
    head = (f'<h2>{SNAPSHOT_TITLE}</h2>'
            f'<p><span style="font-size:11px">源文件：{os.path.basename(path)}；'
            f'<a href="{url}">在浏览器中打开</a>'
            + ("；内容过长已截断，完整内容见上方链接" if truncated else "")
            + '</span></p>')
    html = head + body
    try:
        z = _client()
        existing = None
        for c in z.children(item_key):
            if c["data"].get("itemType") == "note" and SNAPSHOT_TITLE in (c["data"].get("note") or ""):
                existing = c
                break
        if existing:
            d = existing["data"]
            d["note"] = html
            z.update_item(d)
            log(f"已更新 MinerU 子笔记（{len(html)} 字符）")
            return {"ok": True, "action": "updated", "size": len(html)}
        z.create_items([{"itemType": "note", "parentItem": item_key, "note": html,
                         "tags": [{"tag": "mineru"}]}])
        log(f"已创建 MinerU 子笔记（{len(html)} 字符）")
        return {"ok": True, "action": "created", "size": len(html)}
    except Exception as e:
        log(f"MinerU 子笔记写入失败：{repr(e)[:80]}")
        return {"ok": False, "error": repr(e)[:120]}
