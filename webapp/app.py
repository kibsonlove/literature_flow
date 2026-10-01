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
import threading

import markdown as md
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.config import (
    load_embedding, load_paths, load_settings, pdf_wait_raw, pdf_wait_timeouts, save_embedding,
    save_paths, save_settings,
)
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
    """首页。静态资源带 **mtime 版本号**：改了 app.js / agent.js / style.css 之后浏览器必定拉新版，
    彻底告别「我明明改了，页面还是旧行为」（2026-09-30 反复踩：后端加了按钮处理函数，
    用户页签里还是旧 JS，点了毫无反应）。HTML 本身也禁缓存，每次都重新校验。"""
    path = os.path.join(TEMPL, "index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    for name in ("app.js", "agent.js", "style.css"):
        try:
            v = int(os.stat(os.path.join(STATIC, name)).st_mtime)
        except Exception:
            v = 0
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


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
        raise HTTPException(400, "尚未配置知识库向量模型。请到顶栏「设置 → 知识库」填入 API Key"
                                "（默认用硅基流动 BGE-M3，免费注册约 1 分钟）")
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


# ---------------------------------------------------------------- MinerU
# ⚠ /api/mineru/key 必须定义在 /api/mineru/{item_key} 之前：
# Starlette 是按定义顺序匹配路由的，固定路径写在动态路径后面会被抢先命中，
# "key" 被当作 item_key 去查缓存，直接 404。

@app.get("/api/mineru/key")
def get_mineru_key():
    """MinerU 平台 key：现在也能在界面里填，不必去手建 txt 文件。"""
    from core import mineru
    k = ""
    try:
        with open(mineru.key_file(), encoding="utf-8") as f:
            k = f.read().strip()
    except Exception:
        pass
    return {"api_key": k, "has_key": bool(k), "path": mineru.key_file()}


@app.post("/api/mineru/key")
def post_mineru_key(payload: dict):
    """写入 config/mineru_key.txt（新位置；core/ 下的旧文件仍兼容读取）。"""
    from core.config import CONFIG_DIR
    k = (payload.get("api_key") or "").strip()
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        path = os.path.join(CONFIG_DIR, "mineru_key.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(k)
    except Exception as e:
        raise HTTPException(500, f"写入失败：{repr(e)[:120]}")
    return {"ok": True, "has_key": bool(k), "path": path}


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


@app.get("/api/lit/topics")
def lit_topics():
    """列出已保存的检索主题（config/topics/*.json，属本地用户数据）。"""
    from core import litsearch
    return litsearch.list_topics()


@app.post("/api/lit/topics")
def lit_save_topic(payload: dict):
    """保存/更新一个检索主题（同名覆盖）。"""
    from core import litsearch
    try:
        return litsearch.save_topic(payload)
    except Exception as e:
        raise HTTPException(400, f"主题保存失败：{e}")


@app.post("/api/lit/topics/delete")
def lit_delete_topic(payload: dict):
    from core import litsearch
    return {"ok": litsearch.delete_topic(payload.get("slug", ""))}


@app.post("/api/lit/search")
def lit_search(payload: dict):
    """文献检索。payload: {queries[], since, sources[], per_page}"""
    from core import litsearch
    queries = [q for q in (payload.get("queries") or []) if q and q.strip()]
    if not queries:
        raise HTTPException(400, "请至少填写一条检索式")
    sources = [s for s in (payload.get("sources") or ["openalex"]) if s in ("openalex", "arxiv")]
    if not sources:
        raise HTTPException(400, "请至少选择一个数据源")
    since = (payload.get("since") or "").strip()
    sort = payload.get("sort") or "relevance"
    logs = []
    try:
        recs, errors, status = litsearch.run_search(
            queries, since=since or None, sources=sources,
            per_page=int(payload.get("per_page") or 25),
            sort=sort, log=logs.append)
        litsearch.mark_in_library(recs)
    except Exception as e:
        raise HTTPException(500, f"检索失败：{repr(e)[:150]}")
    # 有源失败时必须显式告知（否则用户看不出结果少了一半）
    degraded = [f"{litsearch.SRC_LABEL.get(k, k)}：{v['reason']}"
                for k, v in status.items() if v.get("failed")]
    # 降级的连带影响：arXiv 无被引数，OpenAlex 又挂了 → 「被引次数」排序会失真
    if sort == "citations" and "arxiv" in sources and status.get("openalex", {}).get("failed"):
        degraded.append("arXiv 不提供被引数，被引排序已失真，建议改用「相关度」或「最新」")
    return {"count": len(recs), "results": recs, "errors": errors, "log": logs,
            "status": status, "degraded": degraded}


@app.post("/api/lit/expand")
def lit_expand(payload: dict):
    """引文追踪。payload: {record, direction: citing|referenced, limit, since}"""
    from core import litsearch
    rec = payload.get("record") or {}
    direction = payload.get("direction") or "citing"
    if direction not in ("citing", "referenced"):
        raise HTTPException(400, "direction 只能是 citing 或 referenced")
    since = (payload.get("since") or "").strip() or None
    try:
        got = litsearch.expand_citations(
            rec, direction=direction,
            limit=int(payload.get("limit") or 40), since=since)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except litsearch.RateLimited as e:
        raise HTTPException(429, "OpenAlex 正在限流，引文追踪暂时不可用；额度恢复后再试")
    except litsearch.ApiKeyInvalid:
        raise HTTPException(400, "OpenAlex API Key 无效（401），请到「设置」核对或清空该 key")
    except Exception as e:
        raise HTTPException(500, f"引文追踪失败：{repr(e)[:150]}")
    got = litsearch.mark_in_library(got)
    return {"count": len(got), "results": got, "direction": direction,
            "seed": (rec.get("title") or "")[:80]}


@app.post("/api/lit/import")
def lit_import(payload: dict):
    """把选中的检索结果写进 Zotero。payload: {records[], collection, tags[], fetch_pdf}"""
    from core import litsearch
    records = payload.get("records") or []
    if not records:
        raise HTTPException(400, "没有选中任何条目")
    logs = []
    r = litsearch.import_records(
        records,
        collection=(payload.get("collection") or "").strip(),
        tags=payload.get("tags") or [],
        fetch_pdf=bool(payload.get("fetch_pdf")),
        log=logs.append)
    r["log"] = logs
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "入库失败")
    return r


@app.post("/api/lit/gen_queries")
def lit_gen_queries(payload: dict):
    """按研究方向生成布尔检索式。

    **默认走网页端**（驱动已登录的浏览器会话，零 API 费用）；payload 里传 backend="api"
    才用「设置」里的模型 API。只生成文本、不检索、不写库；前端拿到后追加进检索式框由用户复核。

    注意这里刻意用 `def` 而不是 `async def`：网页端走 Playwright **同步** API，不能出现在
    事件循环里；Starlette 会把 `def` 路由丢进线程池，正好满足要求。
    """
    from core import litsearch
    logs = []
    try:
        qs = litsearch.generate_queries(
            payload.get("topic"), count=payload.get("count") or 4,
            backend=payload.get("backend"), log=logs.append)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.exception("gen_queries 失败")
        raise HTTPException(500, f"生成检索式失败：{repr(e)[:150]}")
    return {"count": len(qs), "queries": qs, "log": logs}


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    # 精读的挂载/卡住检测阈值（秒），供「设置 → 网页端精读」显示与修改
    s["pdf_wait"] = pdf_wait_timeouts()   # 生效值（前端用作默认提示）
    s["pdf_wait_raw"] = pdf_wait_raw()    # 用户填过的值（回填输入框，留空=用默认）
    return s


@app.post("/api/settings")
def post_settings(payload: dict):
    return save_settings(
        api_key=payload.get("api_key"),
        model=payload.get("model"),
        base_url=payload.get("base_url"),
        openalex_key=payload.get("openalex_key"),
        pdf_wait=payload.get("pdf_wait"),
        research_question=payload.get("research_question"),
    )


# ---------------------------------------------------------------- 本机路径（Zotero）

@app.get("/api/paths")
def get_paths():
    """本机路径：配置值 + 生效值 + 自动探测结果，供「设置 → 本机路径」使用。

    配置值留空表示"用默认"，界面据此显示 placeholder；effective 是实际生效的绝对路径。
    """
    from core import zotero_detect
    from core.config import (
        data_dir, playwright_browsers_dir, webchat_profile_dir,
        zotero_data_dir, zotero_exe,
    )
    cur = load_paths()
    return {
        "zotero_data_dir": (cur.get("zotero_data_dir") or "").strip(),
        "zotero_exe": (cur.get("zotero_exe") or "").strip(),
        "data_dir": (cur.get("data_dir") or "").strip(),
        "playwright_browsers_dir": (cur.get("playwright_browsers_dir") or "").strip(),
        "webchat_profile_dir": (cur.get("webchat_profile_dir") or "").strip(),
        "effective": {
            "zotero_data_dir": zotero_data_dir(),
            "zotero_exe": zotero_exe(),
            "data_dir": data_dir(),
            "playwright_browsers_dir": playwright_browsers_dir(),
            "webchat_profile_dir": webchat_profile_dir(),
        },
        "detected": zotero_detect.detect(),
    }


@app.post("/api/paths")
def post_paths(payload: dict):
    """保存本机路径，并回传逐项校验结果（界面据此提示哪里填错了）。"""
    from core import zotero_detect
    from core.config import data_dir as _data_dir

    d = save_paths(
        zotero_data_dir=payload.get("zotero_data_dir"),
        zotero_exe=payload.get("zotero_exe"),
        data_dir=payload.get("data_dir"),
        playwright_browsers_dir=payload.get("playwright_browsers_dir"),
        webchat_profile_dir=payload.get("webchat_profile_dir"),
    )
    ddir = (d.get("zotero_data_dir") or "").strip()
    exe = (d.get("zotero_exe") or "").strip()
    data = (d.get("data_dir") or "").strip()

    def _check_dir(v):
        """留空 = 用默认（通过）；填了则必须绝对路径，并试着建出来。"""
        if not v:
            return True, ""
        p = os.path.normpath(os.path.expandvars(v))
        if not os.path.isabs(p):
            return False, "要填绝对路径，例如 D:/litflow_data"
        try:
            os.makedirs(p, exist_ok=True)
        except Exception as e:
            return False, "目录建不出来：" + repr(e)[:80]
        return True, ""

    data_ok, data_msg = _check_dir(data)
    return {
        "zotero_data_dir": ddir,
        "zotero_exe": exe,
        "data_dir": data,
        "data_dir_ok": data_ok,
        "data_dir_msg": data_msg,
        "data_dir_effective": _data_dir(),
        "zotero_data_dir_ok": zotero_detect.looks_like_data_dir(ddir),
        "exe_ok": bool(exe) and os.path.isfile(exe),
    }


@app.post("/api/paths/detect")
def post_paths_detect():
    """重新自动探测（只读，不写配置）。"""
    from core import zotero_detect
    return zotero_detect.detect()


@app.post("/api/paths/pick")
def post_paths_pick(payload: dict):
    """弹系统选择框代选本机路径——浏览器拿不到完整本地路径，只能由服务端弹窗。"""
    from core import filepicker
    kind = (payload.get("kind") or "zotero_dir").lower()
    if kind == "exe":
        p = filepicker.pick_file("选择 Zotero 程序（zotero.exe）",
                                 "zotero.exe|zotero.exe|可执行文件 (*.exe)|*.exe")
    elif kind == "data_dir":
        p = filepicker.pick_folder("选择数据目录（缓存 / 向量库 / 浏览器内核都会放这里）")
    elif kind == "browsers":
        p = filepicker.pick_folder("选择浏览器内核目录（playwright 下载的 chromium）")
    elif kind == "webchat_profile":
        p = filepicker.pick_folder("选择网页端登录资料目录")
    else:
        p = filepicker.pick_folder("选择 Zotero 数据目录（含 storage 文件夹的那一层）")
    return {"path": p}


# ---------------------------------------------------------------- 浏览器内核（网页端模式用）

_PW_STATE = {"running": False, "log": [], "ok": None, "started": 0}


def _chromium_installed():
    """配置的浏览器目录里是否已经有 chromium 内核。"""
    from core.config import playwright_browsers_dir
    try:
        return any(d.startswith("chromium") for d in os.listdir(playwright_browsers_dir()))
    except Exception:
        return False


@app.get("/api/playwright/status")
def playwright_status():
    """浏览器内核安装状态（前端轮询用）。"""
    from core.config import playwright_browsers_dir
    return {
        "installed": _chromium_installed(),
        "dir": playwright_browsers_dir(),
        "running": _PW_STATE["running"],
        "ok": _PW_STATE["ok"],
        "log": _PW_STATE["log"][-40:],
    }


@app.post("/api/playwright/install")
def playwright_install():
    """下载 Playwright 的 chromium 内核到「浏览器内核目录」（约 150MB）。

    后台线程执行——下载要几分钟，同步做会把请求卡死；前端轮询 status 拿进度。
    内核会**直接下到界面上配的那个目录**，所以装完不需要再手填路径。
    """
    import subprocess
    import sys
    import threading
    import time as _time
    from core.config import playwright_browsers_dir

    if _PW_STATE["running"]:
        return {"ok": False, "error": "已经在下载了，等它跑完再点"}

    target = playwright_browsers_dir()

    def log(m):
        _PW_STATE["log"].append(m)
        _PW_STATE["log"] = _PW_STATE["log"][-60:]

    def _run():
        try:
            os.makedirs(target, exist_ok=True)
            log("目标目录：" + target)
            log("开始下载 chromium 内核（约 150MB，视网速可能要几分钟）…")
            env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=target)
            p = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=3600)
            tail = ((p.stdout or "") + (p.stderr or "")).strip()[-600:]
            if p.returncode == 0:
                log("下载完成，内核位置：" + target)
                _PW_STATE["ok"] = True
            else:
                log("下载失败（退出码 %s）：%s" % (p.returncode, tail))
                _PW_STATE["ok"] = False
        except Exception as e:
            log("下载出错：" + repr(e)[:200])
            _PW_STATE["ok"] = False
        finally:
            _PW_STATE["running"] = False

    _PW_STATE.update({"running": True, "log": [], "ok": None, "started": _time.time()})
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "dir": target}


# ---------------------------------------------------------------- 知识库向量模型

@app.get("/api/embedding")
def get_embedding():
    return load_embedding()


@app.post("/api/embedding")
def post_embedding(payload: dict):
    return save_embedding(
        api_key=payload.get("api_key"),
        base_url=payload.get("base_url"),
        model=payload.get("model"),
        provider=payload.get("provider"),
    )


@app.get("/api/setup/status")
def setup_status():
    """首次运行体检：逐项判断配置是否可用，供界面显示引导横幅。"""
    from core import kb, zotero_detect
    from core.config import zotero_data_dir, zotero_exe
    from core.zotero_launch import last_launch

    s = load_settings()
    ddir, exe = zotero_data_dir(), zotero_exe()
    launch = last_launch()

    def _item(key, label, ok, where, hint, required=False, value=""):
        return {"key": key, "label": label, "ok": bool(ok), "where": where,
                "hint": hint, "required": required, "value": value}

    items = [
        _item("llm_key", "大模型 API Key", s["has_key"], "设置 → 大模型",
              "用 API 后端才必需；走网页端（默认）可留空"),
        _item("zotero_data_dir", "Zotero 数据目录",
              zotero_detect.looks_like_data_dir(ddir), "设置 → 本机路径",
              "填错会读不到 PDF", required=True, value=ddir),
        _item("zotero_exe", "Zotero 程序路径", bool(exe) and os.path.isfile(exe),
              "设置 → 本机路径", "只影响自动拉起 Zotero，可留空", value=exe),
        # 自动拉起 Zotero 的结果：不阻塞使用，但失败时必须说出来——
        # 版本不匹配这类原因的报错只在 Zotero 自己的弹窗里，日志里看不到（2026-09-30 踩过）。
        _item("zotero_launch", "Zotero 自动启动", launch["state"] != "failed",
              "设置 → 本机路径",
              launch["message"] or "启动时若 Zotero 未运行会自动拉起，暂无结论",
              value=launch.get("exe") or ""),
        _item("embedding", "知识库向量模型", kb.embedding_available(), "设置 → 知识库",
              "只影响语义检索，可留空"),
    ]
    try:
        running = zotero_ping()
    except Exception:
        running = False
    return {
        "items": items,
        "missing": [i["key"] for i in items if not i["ok"]],
        "needs_setup": any(not i["ok"] and i["required"] for i in items),
        "zotero_running": running,
        "zotero_launch_failed": launch["state"] == "failed",
    }


@app.get("/api/lit/quota")
def lit_quota():
    """查询 OpenAlex 当前额度（需在设置里填 API Key）。未配 key 时告知额度差异。"""
    from core import litsearch
    q = litsearch.quota()
    if q is None:
        return {"has_key": False,
                "hint": "当前无 Key：每日额度 $0.1（约 100 次请求 / 20 轮检索）。"
                        "免费注册后提升到 $1/天（10 倍），注册地址 openalex.org/settings/api"}
    return q


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


@app.get("/api/zotero/write_status")
def zotero_write_status():
    """Zotero 写权限状态。读（GET）不需要授权，写（POST/PUT/DELETE）必须授权。"""
    from core import zotero_io
    return {"authorized": zotero_io.write_authorized()}


@app.post("/api/zotero/authorize")
def zotero_authorize(payload: dict):
    """触发 Zotero 授权弹窗并保存写入密钥。用户需在弹窗里选「Always Allow」。"""
    from core import zotero_io
    try:
        r = zotero_io.authorize_local(payload.get("app_name") or "literature_flow")
    except Exception as e:
        hint = zotero_io.write_error_hint(e)
        raise HTTPException(400, f"授权失败：{repr(e)[:160]}" + (f"｜{hint}" if hint else ""))
    if not r.get("ok"):
        raise HTTPException(400, "Zotero 没有返回密钥（可能被拒绝）。请重试，并在弹窗里选「Always Allow」")
    if not r.get("saved"):
        raise HTTPException(400, "只拿到了「一次性」授权。请在 Zotero 弹窗里改选「Always Allow」再试一次。")
    return r


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


# ================================================================ 智能体（agent）
# 见 docs/智能体化改造方案.md。形态：开头一次 LLM 出计划 → 确定性代码逐步执行 →
# 危险（写库）步骤一律挂起等用户确认 → 失败才回问 LLM → 结束一次总结。
#
# 为什么路由都用 `def` 且把重活丢后台线程：网页端通道走 Playwright **同步** API，
# 绝不能出现在事件循环里；而且一次 LLM 调用要开浏览器、约 30 秒+，同步返回会把
# 请求挂死。所以：路由立刻返回任务号，前端轮询 /api/agent/status 拿进度。
#
# 串行约束：网页端开的是同一个浏览器 profile，同一时刻只能跑一个任务，用 _AGENT 闸门保护。

_AGENT = {"busy": False}
_AGENT_GATE = threading.Lock()


def _agent_bg(fn, *args):
    """把一段 agent 工作丢到后台线程跑；同时只允许一个在跑（串行闸门）。"""
    with _AGENT_GATE:
        if _AGENT["busy"]:
            raise HTTPException(409, "已有智能体任务正在执行（网页端浏览器通道只能串行），请先等它结束")
        _AGENT["busy"] = True

    def _work():
        try:
            fn(*args)
        except Exception:
            logging.exception("智能体后台任务失败")
        finally:
            with _AGENT_GATE:
                _AGENT["busy"] = False

    threading.Thread(target=_work, daemon=True).start()


def _agent_mod():
    from core import agent_orchestrator as AO
    return AO


@app.get("/api/agent/tools")
def agent_tools_spec():
    """给前端展示的工具清单（只读，供「能力」面板渲染）。"""
    from core import agent_tools as A
    return {"tools": A.spec_json(), "busy": _AGENT["busy"]}


@app.post("/api/agent/plan")
def agent_plan(payload: dict):
    """生成执行计划并落盘；立即返回任务号，计划在后台线程里出（前端轮询状态）。"""
    AO = _agent_mod()
    text = (payload.get("task") or "").strip()
    if not text:
        raise HTTPException(400, "请先输入任务内容")
    try:
        t = AO.new_task(text, backend=payload.get("backend") or "webchat")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _agent_bg(AO.plan_task, t["id"])
    return {"ok": True, "id": t["id"], "task": t}


@app.post("/api/agent/run")
def agent_run(payload: dict):
    """开始/继续执行任务；跑到「下一个危险步骤」或结束。"""
    AO = _agent_mod()
    tid = (payload.get("id") or "").strip()
    if not AO.get_task(tid):
        raise HTTPException(404, "任务不存在")
    _agent_bg(AO.advance, tid)
    return {"ok": True, "id": tid}


@app.post("/api/agent/confirm")
def agent_confirm(payload: dict):
    """用户确认某个危险步骤：放行并继续执行。"""
    AO = _agent_mod()
    tid = (payload.get("id") or "").strip()
    sid = (payload.get("step_id") or "").strip()
    if not AO.get_task(tid):
        raise HTTPException(404, "任务不存在")
    if not sid:
        raise HTTPException(400, "缺少 step_id")
    _agent_bg(AO.approve, tid, sid)
    return {"ok": True, "id": tid}


@app.post("/api/agent/skip")
def agent_skip(payload: dict):
    """跳过某一步（含危险步骤）并继续。"""
    AO = _agent_mod()
    tid = (payload.get("id") or "").strip()
    sid = (payload.get("step_id") or "").strip()
    if not AO.get_task(tid):
        raise HTTPException(404, "任务不存在")
    if not sid:
        raise HTTPException(400, "缺少 step_id")
    _agent_bg(AO.skip_step, tid, sid)
    return {"ok": True, "id": tid}


@app.post("/api/agent/abort")
def agent_abort(payload: dict):
    """中止任务（同步，立即生效）。"""
    AO = _agent_mod()
    tid = (payload.get("id") or "").strip()
    if not AO.get_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "task": AO.abort(tid)}


@app.post("/api/agent/delete")
def agent_delete(payload: dict):
    AO = _agent_mod()
    return {"ok": AO.delete_task((payload.get("id") or "").strip())}


@app.get("/api/agent/status")
def agent_status(id: str = ""):
    """任务详情（带 id）或任务列表（不带 id）。前端轮询用。"""
    AO = _agent_mod()
    if not (id or "").strip():
        from core import agent_tools as A
        return {"tasks": AO.list_tasks(), "busy": _AGENT["busy"], "tools": len(A.TOOLS)}
    t = AO.get_task(id.strip())
    if not t:
        raise HTTPException(404, "任务不存在")
    t["busy"] = _AGENT["busy"]
    return t
