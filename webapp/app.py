# -*- coding: utf-8 -*-
"""literature_flow WebApp —— FastAPI 后端。

流程（按用户要求，不让手动填父条目 key）：
  1) 用户提供 Zotero key 或上传 PDF
  2) /api/process[_key] 抽取全文 → 六维出笔记 → 自动解析目标父条目
  3) 前端展示"将挂到：标题(key)[状态]"，用户点确认
  4) /api/writeback 把笔记挂到已解析的父条目（无则自动建）
"""
import logging
import os
import re
import tempfile

import markdown as md
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.config import load_settings, save_settings
from core.extract import pdf_to_text, pdf_path_from_zotero_key
from core.read import read_paper, read_paper_webchat
from core.batch import (
    candidates as batch_candidates,
    start as batch_start,
    status as batch_status,
    stop as batch_stop,
)
from core.paperset import collect as collect_paperset
from core.render import to_html as _to_html, wrap_html as _wrap_html
from core.zotero_io import (
    add_note, add_tags, create_parent, list_recent, search_items,
    list_collections, list_collection_items, ping as zotero_ping,
)
from core.zotero_resolve import resolve_target

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPL = os.path.join(BASE, "templates")
STATIC = os.path.join(BASE, "static")

app = FastAPI(title="literature_flow")


@app.get("/api/domain")
def get_domain():
    """领域包：界面标题、维度表等由 config/domain.json 驱动。"""
    from core.domain import load_domain
    d = load_domain()
    from core.domain import list_domains
    return {"app_title": d["app_title"], "domain": d["domain"], "dimensions": d["dimensions"],
            "label": d.get("label", ""), "domains": list_domains()}


@app.on_event("startup")
def _on_startup():
    """启动时确保 Zotero 在运行：未运行则自动拉起（后台线程，不阻塞服务启动）。"""
    import threading

    from core.zotero_launch import ensure_zotero

    def _run():
        try:
            ensure_zotero(log=lambda m: logging.info("[zotero] %s", m))
        except Exception:
            logging.exception("ensure_zotero 失败")

    threading.Thread(target=_run, daemon=True).start()

@app.get("/")
def index():
    return FileResponse(os.path.join(TEMPL, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/reports", StaticFiles(directory=os.path.normpath(os.path.join(BASE, "..", "reports"))), name="reports")


@app.get("/api/kb/stats")
def kb_stats():
    """知识库统计：已索引条目/块数。"""
    from core import kb
    return kb.stats()


@app.post("/api/kb/index")
def kb_index(payload: dict):
    """为已有 MinerU 缓存的文献建/更新索引。payload: {force?}"""
    from core import kb
    if not kb.embedding_available():
        raise HTTPException(400, "未配置 embedding（config/embedding.json）")
    out = {"indexed": 0, "failed": 0, "chunks": 0, "details": []}
    for t in kb.unindexed_items():
        r = kb.index_item(t["key"], md_path=t["md"], log=lambda m: logging.info("[kb] %s", m))
        if r.get("ok"):
            out["indexed"] += 1
            out["chunks"] += r.get("chunks", 0)
            out["details"].append(f"✓ {t['key']}（{r.get('chunks')} 块）")
        else:
            out["failed"] += 1
            out["details"].append(f"✗ {t['key']}：{r.get('error')}")
    out["stats"] = kb.stats()
    return out


@app.post("/api/kb/search")
def kb_search(payload: dict):
    """语义检索文献段落。payload: {query, top_k?}"""
    from core import kb
    q = (payload.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "请输入检索内容")
    try:
        return {"query": q, "results": kb.search(q, top_k=int(payload.get("top_k") or 8))}
    except Exception as e:
        raise HTTPException(500, f"检索失败：{repr(e)[:150]}")


_LONGDOC_STATE = {"running": False, "log": [], "result": None, "started": 0}


@app.post("/api/longdoc/start")
def post_longdoc_start(payload: dict):
    """长文精读（学位论文/专著）：分章精读 + 全书总纲。payload: {item_key, max_chapters?}"""
    import threading
    if _LONGDOC_STATE["running"]:
        raise HTTPException(400, "已有长文精读任务在运行")
    key = (payload.get("item_key") or "").strip()
    if not key:
        raise HTTPException(400, "缺少 Zotero key")
    max_ch = payload.get("max_chapters")
    backend = (payload.get("backend") or "api").strip().lower()
    _LONGDOC_STATE.update({"running": True, "log": [], "result": None})
    _LONGDOC_STATE["started"] = __import__("time").time()

    def _run():
        try:
            from core import longdoc
            log = lambda m: _LONGDOC_STATE["log"].append(str(m))
            if backend == "webchat":
                r = longdoc.read_longdoc_webchat(key, max_chapters=max_ch, log=log)
            else:
                r = longdoc.read_longdoc(key, backend="api", max_chapters=max_ch, log=log)
            _LONGDOC_STATE["result"] = r
        except Exception as e:
            _LONGDOC_STATE["log"].append(f"失败：{e}")
            _LONGDOC_STATE["result"] = {"ok": False, "error": str(e)[:200]}
        finally:
            _LONGDOC_STATE["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/longdoc/upload")
async def post_longdoc_upload(file: UploadFile = File(...), max_chapters: str = Form(""),
                               backend: str = Form("api")):
    """上传 PDF → 自动入库 Zotero → MinerU 解析 → 分章精读 + 全书总纲。"""
    import threading, time as _t
    if _LONGDOC_STATE["running"]:
        raise HTTPException(400, "已有长文精读任务在运行")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    safe = os.path.basename(file.filename)
    tmp_dir = os.path.join(tempfile.gettempdir(), "lf_longdoc")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, safe)          # 保留原始文件名，条目/附件名才正确
    with open(tmp, "wb") as w:
        w.write(await file.read())
    max_ch = int(max_chapters) if str(max_chapters).strip().isdigit() else None
    _LONGDOC_STATE.update({"running": True, "log": [], "result": None, "started": _t.time()})

    def _run():
        log = lambda m: _LONGDOC_STATE["log"].append(str(m))
        try:
            from core.zotero_io import _client
            from core import pdf_import, longdoc
            log(f"① 存入 Zotero：{safe}")
            imp = pdf_import.import_pdf(_client(), tmp, log=log, auto_mineru=False)
            if not imp.get("ok"):
                raise RuntimeError(f"入库失败：{imp.get('error')}")
            key = imp["item"]
            log(f"   已建条目+附件（key={key}）")
            if (backend or "api").strip().lower() == "webchat":
                r = longdoc.read_longdoc_webchat(key, max_chapters=max_ch, log=log)
            else:
                r = longdoc.read_longdoc(key, max_chapters=max_ch, log=log)
            _LONGDOC_STATE["result"] = r
        except Exception as e:
            log(f"失败：{e}")
            _LONGDOC_STATE["result"] = {"ok": False, "error": str(e)[:200]}
        finally:
            _LONGDOC_STATE["running"] = False
            try:
                os.remove(tmp)          # 失败也无所谓（%TEMP% 由系统清理）
            except BaseException:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True, "file": safe}


@app.get("/api/longdoc/status")
def get_longdoc_status():
    return {"running": _LONGDOC_STATE["running"], "log": _LONGDOC_STATE["log"][-40:],
            "result": _LONGDOC_STATE["result"]}


@app.post("/api/mineru/snapshot_all")
def post_snapshot_all():
    """为所有已有 MinerU 缓存的文献补建/更新 Zotero 子笔记快照。"""
    from core import mineru, kb
    out = {"created": 0, "updated": 0, "failed": 0, "details": []}
    for key, md in kb._cache_entries():
        r = mineru.snapshot_note(key, md_path=md)
        if r.get("ok"):
            out[r["action"]] = out.get(r["action"], 0) + 1
        else:
            out["failed"] += 1
    return out


@app.get("/api/mineru/{item_key}")
def get_mineru_cache(item_key: str):
    """查看某条目的 MinerU 解析缓存（高保真 Markdown，表格/公式保留）。"""
    from core import mineru
    path = mineru.cache_file(item_key)
    if not path:
        raise HTTPException(404, f"该条目暂无 MinerU 缓存：{item_key}")
    raw = open(path, encoding="utf-8").read()
    try:
        body = md.markdown(raw, extensions=["tables", "fenced_code"])
    except Exception:
        body = f"<pre>{raw}</pre>"
    return HTMLResponse(
        f"<html><head><meta charset='utf-8'><title>MinerU 解析：{item_key}</title>"
        f"<style>body{{max-width:900px;margin:2em auto;font-family:system-ui,'Microsoft YaHei';line-height:1.6}}"
        f"table{{border-collapse:collapse;margin:1em 0}}td,th{{border:1px solid #bbb;padding:4px 8px;font-size:.9em}}</style>"
        f"</head><body><h2>MinerU 解析缓存：{item_key}</h2>{body}</body></html>")


@app.get("/api/tool/reports")
def tool_reports():
    from core import tools
    return tools.list_reports()


@app.post("/api/tool/run")
def post_tool_run(payload: dict):
    """运行白名单维护脚本。payload: {tool, apply?}——变更类脚本需 apply=True 且前端已确认。"""
    from core import tools
    res = tools.run_tool(payload.get("tool", ""), apply=bool(payload.get("apply")),
                         log=lambda m: logging.info("[tool] %s", m))
    if not res.get("ok") and res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
def post_settings(payload: dict):
    return save_settings(
        api_key=payload.get("api_key"),
        model=payload.get("model"),
        base_url=payload.get("base_url"),
    )


@app.get("/api/zotero_items")
def get_zotero_items(limit: int = 60):
    """列出最近修改的文献条目，供前端"从 Zotero 选择"面板使用。"""
    try:
        return list_recent(limit=limit)
    except Exception as e:
        raise HTTPException(500, f"读取 Zotero 失败：{e}")


@app.get("/api/zotero_search")
def get_zotero_search(q: str = "", limit: int = 40):
    """按关键词搜索 Zotero 文献条目。"""
    try:
        return search_items(q, limit=limit)
    except Exception as e:
        raise HTTPException(500, f"搜索 Zotero 失败：{e}")


@app.get("/api/zotero_ping")
def get_zotero_ping():
    """检测 Zotero 本地 API 是否可用，供前端提示"请检查 Zotero 是否打开"。"""
    return {"ok": zotero_ping()}


@app.get("/api/zotero_collections")
def get_zotero_collections():
    """返回 Zotero 分类树（文件夹），供前端"从 Zotero 选择"左侧树展示。"""
    try:
        return list_collections()
    except Exception as e:
        raise HTTPException(500, f"读取 Zotero 分类失败：{e}")


@app.get("/api/zotero_collection_items")
def get_zotero_collection_items(key: str, include_children: bool = True):
    """列出某分类下的文献条目（默认含子分类）。"""
    try:
        return list_collection_items(key, include_children=include_children)
    except Exception as e:
        raise HTTPException(500, f"读取分类条目失败：{e}")


@app.get("/api/paperset")
def get_paperset():
    """paper-set 批量比对：汇总所有六维笔记的「六维速览」+ 标签。"""
    try:
        return collect_paperset()
    except Exception as e:
        raise HTTPException(500, f"生成 paper-set 失败：{e}")


@app.get("/api/batch/candidates")
def get_batch_candidates(collection: str = "", skip_thesis: bool = True):
    """批量候选：含 PDF 且尚无六维笔记的条目（可限定分类）。"""
    try:
        return batch_candidates(collection or None, skip_thesis=skip_thesis)
    except Exception as e:
        raise HTTPException(500, f"读取批量候选失败：{e}")


# ---------- PDF 迁移入库 ----------

@app.get("/api/pdf_import/imported")
def get_pdf_imported():
    """已入库且未清理的文件记录（客户端与所选文件夹逐个比对）。"""
    from core import pdf_import
    return pdf_import.imported_list()


@app.post("/api/pdf_import/upload")
async def post_pdf_upload(files: list[UploadFile] = File(...)):
    """上传 PDF（可多选）→ 建条目+附件入库 Zotero。"""
    from core import pdf_import
    from core.zotero_io import _client
    z = _client()
    results = []
    for f in files:
        if not (f.filename or "").lower().endswith(".pdf"):
            results.append({"ok": False, "file": f.filename, "error": "仅支持 PDF"})
            continue
        tmp = os.path.join(tempfile.gettempdir(), f"pdfimport_{os.path.basename(f.filename)}")
        with open(tmp, "wb") as w:
            w.write(await f.read())
        try:
            results.append(pdf_import.import_pdf(z, tmp, log=lambda m: None))
        except Exception as e:
            results.append({"ok": False, "file": f.filename, "error": repr(e)[:120]})
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return {"results": results}


@app.post("/api/pdf_import/mark_deleted")
def post_pdf_mark_deleted(payload: dict):
    """客户端已删除原文件后记账（校验名称+大小）。"""
    from core import pdf_import
    res = pdf_import.mark_deleted(payload.get("name", ""), payload.get("size", -1))
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "标记失败")
    return res


@app.post("/api/batch/start")
def post_batch_start(payload: dict):
    """启动批量任务。payload: {keys:[...], options:{skip_thesis,auto_classify,writeback}}"""
    keys = payload.get("keys") or []
    opts = payload.get("options") or {}
    res = batch_start(keys, opts)
    if not res.get("ok"):
        raise HTTPException(400, res.get("msg") or "启动失败")
    return res


@app.get("/api/batch/status")
def get_batch_status():
    return batch_status()


@app.post("/api/batch/stop")
def post_batch_stop():
    return {"ok": batch_stop()}


# _to_html / _wrap_html 见 core.render（process 与批量任务共用）


def _core_process(key, backend="api", webchat_url=None, auto_domain=True):
    """单篇核心流程：取 PDF → 选领域包 → MinerU 表格增强 → 出笔记 → 解析回写目标。"""
    path = pdf_path_from_zotero_key(key)
    if not path:
        raise HTTPException(400, f"未在 storage 找到该 key 的 PDF：{key}")
    # 领域包：先取文本样本用于匹配（未命中则自动造包）
    try:
        _meta0, _text0 = pdf_to_text(path)
    except Exception:
        _meta0, _text0 = {"title": ""}, ""
    from core import domain as _dom
    pack_name, pack, pack_src = _dom.pick_domain(
        _meta0.get("title") or "", _text0,
        auto_generate=auto_domain,
        log=lambda m: logging.info("[domain] %s", m))
    sys_prompt = _dom.system_prompt(pack_name)
    mineru_hint = ""
    extra = ""
    tables_len = 0
    try:
        from core import mineru
        if mineru.mineru_available():
            tables = mineru.get_tables_markdown(
                path, item_key=key,
                log=lambda m: logging.info("[mineru] %s", m))
            if tables:
                tables_len = len(tables)
                extra = ("\n\n---\n【附·MinerU 结构化表格数据（原文量化表格，"
                         "笔记中的数字必须与这些表格一致）】\n" + tables)
                mineru_hint = f"（已附 MinerU 表格 {len(tables)} 字符）"
    except Exception as e:
        logging.warning("MinerU 表格获取失败（回退纯文本）: %s", e)
    if backend == "webchat":
        webchat_url = (webchat_url or "").strip() or None
        note = read_paper_webchat(path, title="", headless=False,
                                  url=webchat_url, extra_prompt=extra, system=sys_prompt)
        note_format = "html"
        meta = {"title": "", "pages": None}
    else:
        meta, text = pdf_to_text(path)
        note = read_paper(text, title=meta.get("title", ""), extra_system=extra, system=sys_prompt)
        note_format = "markdown"
    target = resolve_target(zotero_key=key)
    from core import mineru as _m
    if tables_len:
        n_tab, heads = _m.tables_info(extra)
        mk = f"已应用 {n_tab} 个表格（{tables_len} 字符）"
        if heads:
            mk += "；表头：" + " / ".join(heads[:3])[:60]
        note = _m.note_meta(note, "MinerU 表格增强", mk,
                            url=f"http://127.0.0.1:8000/api/mineru/{key}",
                            url_text="查看 MinerU 解析")
    else:
        note = _m.note_meta(note, "MinerU 表格增强", "未应用（该 PDF 无表格或解析失败）")
    note = _m.note_meta(note, "领域包", f"{pack['label']}（{pack_src}）")
    if tables_len:
        _m.snapshot_note(key, log=lambda m: logging.info("[mineru] %s", m))
    # 保险：校验笔记标题与目标条目是否一致（防上下文错配）
    warning = ""
    t_title = (target.get("target_title") or "").strip()
    is_html = bool(re.search(r"<h1[^>]*>", note or "")) or "<span" in (note or "")[:200]
    note_title = ""
    if is_html:
        m2 = re.search(r"<h1[^>]*>(.*?)</h1>", note or "", re.S)
        if m2:
            note_title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m2.group(1))).strip()
    else:
        m1 = re.search(r"^#\s+(.+)$", note or "", re.M)
        if m1:
            note_title = m1.group(1).strip()
    note_title = re.sub(r"[（(].*?[)）]", "", note_title).strip()
    if t_title and note_title:
        head = t_title[:10]
        if head not in note_title and note_title[:10] not in t_title:
            warning = f"⚠ 笔记标题（{note_title[:40]}…）与目标条目（{t_title[:40]}…）不一致，回写前请检查。"
    return {
        "warning": warning,
        "title": target.get("target_title") or meta.get("title") or key,
        "pages": meta.get("pages"),
        "markdown": note,
        "note_format": note_format,
        "target": target,
        "backend": backend,
        "mineru_hint": mineru_hint,
        "domain_pack": pack["label"],
        "domain_source": pack_src,
    }


def _process_upload_sync(tmp, backend, webchat_url):
    """同步版上传一条龙（在 ThreadPool 中跑：WebChat 后端用同步 Playwright，
    不能出现在 asyncio 事件循环里）。"""
    from core.zotero_io import _client
    from core import pdf_import
    imp = pdf_import.import_pdf(_client(), tmp,
                                log=lambda m: logging.info("[import] %s", m),
                                auto_mineru=False)  # 表格在下方同步解析，避免重复调用
    if not imp.get("ok"):
        raise RuntimeError(f"存入 Zotero 失败：{imp.get('error')}")
    result = _core_process(imp["item"], backend=backend, webchat_url=webchat_url)
    result["imported_item"] = imp["item"]
    result["imported_note"] = "PDF 已存入 Zotero（含附件）"
    return result


@app.post("/api/process")
async def process(file: UploadFile = File(...), backend: str = Form("webchat"), webchat_url: str = Form("")):
    """上传 PDF 单篇一条龙：自动存入 Zotero（建条目+挂附件）→ MinerU 表格 → 出笔记 → 回写目标。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    safe_name = os.path.basename(file.filename or "upload.pdf")
    tmp = os.path.join(tempfile.gettempdir(), f"lfupload_{safe_name}")
    try:
        with open(tmp, "wb") as w:
            w.write(await file.read())
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(
            _process_upload_sync, tmp, backend or "webchat", webchat_url)
        return result
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.exception("process(上传PDF) 失败")
        raise HTTPException(500, f"生成失败：{e}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)  # PDF 已复制进 storage，临时文件删除


@app.post("/api/process_key")
def process_key(payload: dict):
    """给定 Zotero key（附件或父条目）→ 从 storage 取 PDF → 出笔记 → 解析目标。
    backend='api'（默认）用 DeepSeek API；backend='webchat' 用网页端 LLM（省 token）。
    """
    key = (payload.get("zotero_key") or "").strip()
    if not key:
        raise HTTPException(400, "缺少 Zotero key")
    backend = (payload.get("backend") or "api").strip().lower()
    try:
        return _core_process(key, backend=backend,
                             webchat_url=payload.get("webchat_url"))
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.exception("process_key 失败 (key=%s, backend=%s)", key, backend)
        raise HTTPException(500, f"生成失败：{e}")


@app.post("/api/writeback")
def writeback(payload: dict):
    """把笔记挂到已解析的父条目（无则自动建）。空笔记拦截。"""
    note_md = (payload.get("markdown") or "").strip()
    if not note_md:
        raise HTTPException(400, "笔记内容为空，无法回写")
    target_key = (payload.get("target_key") or "").strip()
    target_title = payload.get("target_title") or ""
    action = payload.get("action") or "attach_existing"
    tags = payload.get("tags", [])

    if action == "create_new" or not target_key:
        new_key = create_parent(target_title, _guess_type(target_title))
        if not new_key:
            raise HTTPException(500, "新建文献条目失败，无法回写")
        target_key = new_key

    note_format = (payload.get("note_format") or "markdown").strip().lower()
    if note_format == "html":
        html = _wrap_html(note_md, title=target_title)
    else:
        html = _to_html(note_md, title=target_title)
    created = add_note(target_key, html)
    if not created:
        raise HTTPException(500, "笔记回写失败：Zotero 未返回创建结果")
    added = add_tags(target_key, tags) if tags else []
    return {
        "ok": True,
        "target_key": target_key,
        "target_title": target_title,
        "createdKeys": created,
        "addedTags": added,
    }


def _guess_type(title):
    low = (title or "").lower()
    if "z-library" in low or "出版社" in title or "press" in low:
        return "book"
    return "journalArticle"
