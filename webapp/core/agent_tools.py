# -*- coding: utf-8 -*-
"""智能体工具注册表（P0）。

把项目里**已经存在**的能力登记成一份机器可读的工具清单，供智能体编排器
（`agent_orchestrator.py`）生成计划、供前端渲染确认界面。见
`docs/智能体化改造方案.md` 第 1、2 节。

设计原则：
- **只登记、不重写**：每个工具都是对 `core/` 既有函数的薄封装，能力实现仍只有一处。
- 统一返回 `{"ok", "tool", "summary", "data"}`：`summary` 是一句给人看的结果，
  `data` 是结构化结果（供后续步骤用 `$s1.xxx` 引用）。
- **写库动作一律标 danger**：编排器遇到 danger 步骤会挂起，等用户在面板上确认。
  变更类维护脚本默认 dry-run（`apply=False` 时不产生任何写操作，也就不算危险）。
- 通用化：本文件不绑定任何领域，界面文案与参数名一律通用。

对外接口：
    TOOLS               —— 工具定义列表（唯一事实来源）
    get(name)           —— 取单个工具定义
    names()             —— 全部工具名
    is_danger(name, args) —— 结合参数判定"这一次调用"是否属于危险（写库）动作
    impact(name, args)  —— 危险步骤的影响面描述（供确认界面显示"将影响多少条目"）
    spec_text()         —— 导出「给 LLM 看的工具说明书」文本
    run(name, args, ctx) —— 执行工具，永不抛异常（失败也返回 ok=False）
"""
import json

__all__ = ["TOOLS", "get", "names", "is_danger", "impact", "spec_text", "run", "Ctx"]

MAX_SUMMARY_LIST = 20          # summary 里最多列举多少条，避免日志刷屏


class Ctx:
    """工具执行上下文：日志回调 + 任务号（供工具做少量上下文相关的处理）。"""

    def __init__(self, log=None, task_id="", backend="webchat"):
        self.log = log or (lambda m: None)
        self.task_id = task_id or ""
        self.backend = backend or "webchat"


def _ok(name, summary, data=None):
    return {"ok": True, "tool": name, "summary": summary, "data": data or {}}


def _err(name, msg, data=None):
    return {"ok": False, "tool": name, "summary": msg, "error": msg, "data": data or {}}


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _need_zotero(name):
    """只读的 Zotero 查询前先探活。

    ⚠ Zotero 没开时本地 API 的读接口会**静默返回空**而不是报错——若不先探活，
    智能体会得出"库里没有这篇文献"的错误结论。这里宁可明确报"未检测到 Zotero"。
    """
    try:
        from . import zotero_io
        if zotero_io.ping():
            return None
        return _err(name, "未检测到 Zotero，请先打开 Zotero 再试（本地读接口在 Zotero 关闭时会返回空）")
    except Exception as e:
        return _err(name, f"连接 Zotero 失败：{repr(e)[:120]}")


# ---------------------------------------------------------------- 工具实现


def _t_lit_generate_queries(args, ctx):
    """按研究方向生成英文布尔检索式（只读；默认走网页端，零 API 费用）。"""
    try:
        from . import litsearch
        qs = litsearch.generate_queries(
            args.get("topic"), count=args.get("count") or 4,
            backend=args.get("backend") or "webchat", log=ctx.log)
    except Exception as e:
        return _err("lit_generate_queries", f"生成检索式失败：{repr(e)[:150]}")
    if not qs:
        return _err("lit_generate_queries", "模型没有产出可用的检索式，请换一个更具体的研究方向重试")
    return _ok("lit_generate_queries", f"生成 {len(qs)} 条检索式", {"queries": qs})


def _t_lit_search(args, ctx):
    """在 OpenAlex / arXiv 按布尔检索式检索文献（只读，不写库）。"""
    queries = [str(q).strip() for q in _as_list(args.get("queries")) if str(q).strip()]
    if not queries:
        return _err("lit_search", "缺少参数 queries（至少一条检索式）")
    sources = [s for s in _as_list(args.get("sources")) if s in ("openalex", "arxiv")]
    if not sources:
        sources = ["openalex", "arxiv"]
    try:
        from . import litsearch
        recs, errors, status = litsearch.run_search(
            queries, since=(args.get("since") or "").strip() or None, sources=sources,
            per_page=int(args.get("per_page") or 50),
            sort=args.get("sort") or "relevance", log=ctx.log)
        litsearch.mark_in_library(recs)
    except Exception as e:
        return _err("lit_search", f"检索失败：{repr(e)[:150]}")
    degraded = [f"{k}: {v.get('reason')}" for k, v in (status or {}).items() if v.get("failed")]
    n_lib = sum(1 for r in recs if r.get("in_library"))
    msg = f"检索到 {len(recs)} 条（{'+'.join(sources)}，其中 {n_lib} 条已在库）"
    if degraded:
        msg += "；部分数据源失败：" + "；".join(degraded)
    return _ok("lit_search", msg,
               {"count": len(recs), "results": recs, "queries": queries,
                "in_library": n_lib, "degraded": degraded})


def _t_lit_import(args, ctx):
    """把选中的检索结果写进 Zotero（**写库**）。"""
    records = args.get("records")
    if isinstance(records, dict):
        records = [records]
    records = [r for r in _as_list(records) if isinstance(r, dict) and (r.get("title") or "").strip()]
    if not records:
        return _err("lit_import", "没有可入库的条目（records 为空或没有标题）")
    total = len(records)
    # 上限：一次检索轻松几百条，全量写库既慢又多半不是用户想要的。让它能只取前 N 条。
    try:
        cap = int(args.get("max") or 0)
    except Exception:
        cap = 0
    capped = 0
    if cap > 0 and total > cap:
        capped = total - cap
        records = records[:cap]
    collection = (args.get("collection") or "").strip()
    tags = [t for t in _as_list(args.get("tags")) if str(t).strip()]
    try:
        from . import litsearch
        r = litsearch.import_records(
            records, collection=collection, tags=tags,
            fetch_pdf=bool(args.get("fetch_pdf")), log=ctx.log)
    except Exception as e:
        return _err("lit_import", f"入库失败：{repr(e)[:150]}")
    if not r.get("ok"):
        return _err("lit_import", r.get("error") or "入库失败")
    where = f"到分类「{collection}」" if collection else "到 Zotero 库"
    msg = (f"入库{where}：新建 {r.get('created', 0)} 条，跳过重复 {r.get('skipped', 0)} 条"
           f"，抓到全文 {r.get('pdf_ok', 0)} 篇")
    if capped:
        msg += f"（共检索到 {total} 条，按上限只取前 {cap} 条，其余 {capped} 条未处理）"
    return _ok("lit_import", msg, {"created": r.get("created"), "skipped": r.get("skipped"),
                                   "pdf_ok": r.get("pdf_ok"), "pdf_fail": r.get("pdf_fail"),
                                   "total_found": total, "capped": capped,
                                   "collection": collection, "items": r.get("items") or []})


def _t_zotero_search_items(args, ctx):
    """在 Zotero 库里按关键词检索文献条目（只读）。"""
    q = (args.get("query") or "").strip()
    if not q:
        return _err("zotero_search_items", "缺少参数 query")
    bad = _need_zotero("zotero_search_items")
    if bad:
        return bad
    try:
        from . import zotero_io
        items = zotero_io.search_items(q, limit=int(args.get("limit") or 40))
    except Exception as e:
        return _err("zotero_search_items", f"检索条目失败：{repr(e)[:150]}")
    return _ok("zotero_search_items", f"在库中匹配到 {len(items)} 条",
               {"count": len(items), "items": items[:100]})


def _t_zotero_recent_items(args, ctx):
    """列出最近加入/修改的文献条目（只读）。"""
    bad = _need_zotero("zotero_recent_items")
    if bad:
        return bad
    try:
        from . import zotero_io
        items = zotero_io.list_recent(limit=int(args.get("limit") or 40))
    except Exception as e:
        return _err("zotero_recent_items", f"读取失败：{repr(e)[:150]}")
    has_pdf = sum(1 for i in items if i.get("has_pdf"))
    return _ok("zotero_recent_items", f"最近 {len(items)} 条（含 PDF 的 {has_pdf} 条）",
               {"count": len(items), "items": items})


def _flatten_tree(nodes, out, depth=0):
    for n in nodes or []:
        out.append({"key": n.get("key"), "name": n.get("name"), "depth": depth})
        _flatten_tree(n.get("children"), out, depth + 1)


def _t_zotero_collections(args, ctx):
    """读取 Zotero 分类树（只读）。"""
    bad = _need_zotero("zotero_collections")
    if bad:
        return bad
    try:
        from . import zotero_io
        tree = zotero_io.list_collections()
    except Exception as e:
        return _err("zotero_collections", f"读取分类失败：{repr(e)[:150]}")
    flat = []
    _flatten_tree(tree, flat)
    if not flat:
        return _ok("zotero_collections",
                   "分类树为空（若 Zotero 未打开，本地接口会返回空树，请确认 Zotero 已启动）",
                   {"count": 0, "collections": []})
    return _ok("zotero_collections", f"共 {len(flat)} 个分类",
               {"count": len(flat), "collections": flat})


def _t_zotero_collection_items(args, ctx):
    """列出某个分类下的文献条目（只读）。"""
    key = (args.get("collection_key") or "").strip()
    if not key:
        return _err("zotero_collection_items", "缺少参数 collection_key（可先用 zotero_collections 取）")
    bad = _need_zotero("zotero_collection_items")
    if bad:
        return bad
    try:
        from . import zotero_io
        items = zotero_io.list_collection_items(
            key, include_children=bool(args.get("include_children", True)),
            limit=int(args.get("limit") or 300))
    except Exception as e:
        return _err("zotero_collection_items", f"读取分类条目失败：{repr(e)[:150]}")
    return _ok("zotero_collection_items", f"该分类下 {len(items)} 条",
               {"count": len(items), "items": items[:200]})


def _t_kb_search(args, ctx):
    """在本地知识库（已索引全文）做语义检索（只读）。"""
    q = (args.get("query") or "").strip()
    if not q:
        return _err("kb_search", "缺少参数 query")
    try:
        from . import kb
        if not kb.embedding_available():
            return _err("kb_search", "尚未配置向量模型（设置 → 知识库向量模型），语义检索不可用")
        hits = kb.search(q, top_k=int(args.get("top_k") or 8))
    except Exception as e:
        return _err("kb_search", f"语义检索失败：{repr(e)[:150]}")
    if not hits:
        return _ok("kb_search", "没有命中片段（可能相关文献还没「更新索引」）",
                   {"count": 0, "hits": []})
    preview = [{"title": h.get("title"), "section": h.get("section"),
                "score": h.get("score"), "snippet": h.get("snippet", "")[:400]} for h in hits]
    return _ok("kb_search", f"命中 {len(hits)} 段（来自 "
                            + "、".join(sorted({(h.get('title') or '')[:28] for h in hits})[:3]) + "）",
               {"count": len(hits), "hits": preview})


def _t_kb_stats(args, ctx):
    """知识库索引概况（只读）：已索引条目数、片段数、待索引数。"""
    try:
        from . import kb
        s = kb.stats()
    except Exception as e:
        return _err("kb_stats", f"读取失败：{repr(e)[:150]}")
    return _ok("kb_stats", f"已索引 {s.get('items', 0)} 篇 / {s.get('chunks', 0)} 段，"
                           f"另有 {s.get('pending', 0)} 篇已解析待索引", s)


def _t_reading_candidates(args, ctx):
    """列出「含 PDF 且还没有笔记」的条目——批量精读的候选（只读）。"""
    bad = _need_zotero("reading_candidates")
    if bad:
        return bad
    try:
        from . import batch
        rows = batch.candidates(collection_key=(args.get("collection_key") or "").strip() or None,
                                skip_thesis=bool(args.get("skip_thesis", True)),
                                limit=int(args.get("limit") or 300))
    except Exception as e:
        return _err("reading_candidates", f"读取候选失败：{repr(e)[:150]}")
    return _ok("reading_candidates", f"待精读候选 {len(rows)} 篇",
               {"count": len(rows), "items": rows[:200]})


def _t_batch_notes(args, ctx):
    """对选中的条目批量生成精读笔记（**写库**：会回写 Zotero 并可能自动分类）。"""
    keys = [str(k).strip() for k in _as_list(args.get("keys")) if str(k).strip()]
    if not keys:
        return _err("batch_notes", "缺少参数 keys（要精读的条目 key 列表，可先用 reading_candidates 取）")
    try:
        from . import batch
        r = batch.start(keys, {"skip_thesis": bool(args.get("skip_thesis", True)),
                               "auto_classify": bool(args.get("auto_classify", True)),
                               "writeback": bool(args.get("writeback", True))})
    except Exception as e:
        return _err("batch_notes", f"启动批量笔记失败：{repr(e)[:150]}")
    if not r.get("ok"):
        return _err("batch_notes", r.get("msg") or "启动失败")
    return _ok("batch_notes", f"已启动批量精读，共 {r.get('total', len(keys))} 篇；"
                              "这是长任务（网页端每篇约数分钟），进度在「批量笔记」面板查看",
               {"total": r.get("total"), "keys": keys, "background": True})


def _t_reading_status(args, ctx):
    """查询精读/批量任务当前状态与知识库概况（只读）。"""
    out = {}
    try:
        from . import batch
        s = batch.status()
        out["batch"] = {"running": s.get("running"), "done": s.get("done"),
                        "total": s.get("total"), "current": s.get("current"),
                        "log_tail": (s.get("log") or [])[-5:]}
    except Exception as e:
        out["batch"] = {"error": repr(e)[:120]}
    try:
        from . import kb
        out["kb"] = {"items": kb.stats().get("items"), "pending": kb.stats().get("pending")}
    except Exception:
        out["kb"] = {}
    b = out.get("batch") or {}
    if b.get("running"):
        msg = f"批量精读进行中：{b.get('done')}/{b.get('total')}"
    else:
        msg = f"当前没有在跑的批量任务；知识库已索引 {out.get('kb', {}).get('items', 0)} 篇"
    return _ok("reading_status", msg, out)


def _t_list_reports(args, ctx):
    """列出 reports/ 目录里的产物报告（只读）。"""
    try:
        from . import tools as _tools
        rows = _tools.list_reports()
    except Exception as e:
        return _err("list_reports", f"读取失败：{repr(e)[:150]}")
    named = [{"name": r["name"], "size": r["size"]} for r in rows[:MAX_SUMMARY_LIST]]
    return _ok("list_reports", f"reports/ 下共 {len(rows)} 个产物",
               {"count": len(rows), "reports": named})


def _t_run_maintenance(args, ctx):
    """运行白名单维护脚本（变更类默认 dry-run；apply=True 才真正改动，属**写库**）。"""
    tool = (args.get("tool") or "").strip()
    try:
        from . import tools as _tools
        if tool not in _tools.TOOLS:
            return _err("run_maintenance", f"未知维护脚本：{tool}（可选：{'、'.join(_tools.TOOLS)}）")
        mutating = _tools.TOOLS[tool][1]
        apply = bool(args.get("apply")) and mutating
        r = _tools.run_tool(tool, apply=apply, log=ctx.log)
    except Exception as e:
        return _err("run_maintenance", f"运行失败：{repr(e)[:150]}")
    if not r.get("ok"):
        return _err("run_maintenance", (r.get("error") or r.get("output") or "运行失败")[:400])
    mode = "已应用变更" if r.get("apply") else ("dry-run（未改动任何数据）" if mutating else "已运行")
    return _ok("run_maintenance", f"{tool} {mode}",
               {"tool": tool, "apply": bool(r.get("apply")), "output": (r.get("output") or "")[-1500:],
                "output_file": r.get("output_file"), "output_label": r.get("output_label")})


def _maintenance_danger(args):
    """只有"变更类脚本 + apply=True"才危险；dry-run 不写任何数据。"""
    tool = (args.get("tool") or "").strip()
    try:
        from . import tools as _tools
        mutating = _tools.TOOLS.get(tool, ("", False))[1]
    except Exception:
        mutating = False
    return bool(mutating and args.get("apply"))


# ---------------------------------------------------------------- 注册表


TOOLS = [
    # —— 文献情报 ——
    {
        "name": "lit_generate_queries",
        "title": "生成检索式",
        "category": "文献情报",
        "description": "按一个研究方向/主题，生成若干条英文布尔检索式（供 lit_search 使用）。"
                       "默认走网页端大模型，零 API 费用，但会打开浏览器、约需 30-60 秒。",
        "params": {
            "topic": {"type": "string", "required": True, "desc": "研究方向或主题，中文即可，越具体越好"},
            "count": {"type": "int", "required": False, "desc": "生成条数，2-8，默认 4"},
            "backend": {"type": "string", "required": False, "enum": ["webchat", "api"],
                        "desc": "留空即 webchat（零费用）"},
        },
        "danger": False,
        "fn": _t_lit_generate_queries,
    },
    {
        "name": "lit_search",
        "title": "文献检索",
        "category": "文献情报",
        "description": "按布尔检索式在 OpenAlex（期刊/会议）与 arXiv（预印本）检索文献，"
                       "返回标题/作者/年份/DOI/摘要/是否已在库等。只读，不写库。",
        "params": {
            "queries": {"type": "array<string>", "required": True,
                        "desc": "检索式列表；支持 AND/OR/NOT 与双引号短语；可写 \"$s1.queries\" 引用上一步结果"},
            "since": {"type": "string", "required": False, "desc": "起始年，如 2021"},
            "sources": {"type": "array<string>", "required": False, "enum": ["openalex", "arxiv"],
                        "desc": "默认两个都查"},
            "per_page": {"type": "int", "required": False, "desc": "每条检索式取多少条，默认 50"},
            "sort": {"type": "string", "required": False, "enum": ["relevance", "citations", "date"]},
        },
        "danger": False,
        "fn": _t_lit_search,
    },
    {
        "name": "lit_import",
        "title": "检索结果入库",
        "category": "文献情报",
        "description": "把检索结果写进 Zotero（建条目 + 可选抓开放获取全文 + 归入分类）。"
                       "**写库动作**，会暂停等用户确认。",
        "params": {
            "records": {"type": "array<object>", "required": True,
                        "desc": "要入库的检索结果对象列表，通常写 \"$s1.results\""},
            "max": {"type": "int", "required": False,
                    "desc": "最多入库多少条（可选）。检索结果动辄几百条，全量入库又慢又多半不是想要的，"
                            "建议按相关度取前 N 条，例如 50"},
            "collection": {"type": "string", "required": False,
                           "desc": "入库到哪个 Zotero 分类；不存在会新建；留空则只进库不分类"},
            "tags": {"type": "array<string>", "required": False},
            "fetch_pdf": {"type": "bool", "required": False,
                          "desc": "是否抓开放获取全文，默认 true（注意：条数多时会很慢，每篇都要下载）"},
        },
        "danger": True,
        "fn": _t_lit_import,
    },
    # —— Zotero 查询 ——
    {
        "name": "zotero_search_items",
        "title": "库内条目检索",
        "category": "Zotero",
        "description": "按关键词在 Zotero 库里搜索文献条目（标题/作者等），返回 key、标题、类型、是否含 PDF。只读。",
        "params": {
            "query": {"type": "string", "required": True},
            "limit": {"type": "int", "required": False, "desc": "默认 40"},
        },
        "danger": False,
        "fn": _t_zotero_search_items,
    },
    {
        "name": "zotero_recent_items",
        "title": "最近条目",
        "category": "Zotero",
        "description": "列出最近加入/修改的文献条目，标注是否含 PDF。只读。",
        "params": {"limit": {"type": "int", "required": False, "desc": "默认 40"}},
        "danger": False,
        "fn": _t_zotero_recent_items,
    },
    {
        "name": "zotero_collections",
        "title": "分类树",
        "category": "Zotero",
        "description": "读取 Zotero 的分类树（含子分类），返回 key 与名称。只读。",
        "params": {},
        "danger": False,
        "fn": _t_zotero_collections,
    },
    {
        "name": "zotero_collection_items",
        "title": "分类内条目",
        "category": "Zotero",
        "description": "列出某个分类（含子分类）下的文献条目。只读。",
        "params": {
            "collection_key": {"type": "string", "required": True, "desc": "分类 key，可先用 zotero_collections 取"},
            "include_children": {"type": "bool", "required": False, "desc": "默认 true"},
            "limit": {"type": "int", "required": False, "desc": "默认 300"},
        },
        "danger": False,
        "fn": _t_zotero_collection_items,
    },
    # —— 知识库 ——
    {
        "name": "kb_search",
        "title": "知识库语义检索",
        "category": "知识库",
        "description": "在已索引的本地文献全文里做语义检索（向量 + 关键词混合，自动中英双语扩展），"
                       "返回最相关的段落与出处。只读。适合回答\"哪篇文献讲过某个现象\"。",
        "params": {
            "query": {"type": "string", "required": True, "desc": "自然语言问题"},
            "top_k": {"type": "int", "required": False, "desc": "返回段数，默认 8"},
        },
        "danger": False,
        "fn": _t_kb_search,
    },
    {
        "name": "kb_stats",
        "title": "知识库概况",
        "category": "知识库",
        "description": "查看知识库索引情况：已索引条目数、片段数、已解析但待索引的篇数。只读。",
        "params": {},
        "danger": False,
        "fn": _t_kb_stats,
    },
    # —— 精读 ——
    {
        "name": "reading_candidates",
        "title": "待精读候选",
        "category": "精读",
        "description": "列出「含 PDF 且还没有笔记」的条目，就是批量精读的候选清单。只读。",
        "params": {
            "collection_key": {"type": "string", "required": False, "desc": "限定某个分类，留空=全库"},
            "skip_thesis": {"type": "bool", "required": False, "desc": "跳过学位论文，默认 true"},
            "limit": {"type": "int", "required": False, "desc": "默认 300"},
        },
        "danger": False,
        "fn": _t_reading_candidates,
    },
    {
        "name": "batch_notes",
        "title": "批量生成笔记",
        "category": "精读",
        "description": "对选中的条目批量生成精读笔记并回写 Zotero。**写库动作**，会暂停等用户确认。"
                       "启动后是后台长任务，本工具立即返回；一次任务最多安排一个批量精读。",
        "params": {
            "keys": {"type": "array<string>", "required": True,
                     "desc": "要精读的条目 key 列表，通常写 \"$s1.keys\"（用 reading_candidates 取）"},
            "skip_thesis": {"type": "bool", "required": False, "desc": "默认 true"},
            "auto_classify": {"type": "bool", "required": False, "desc": "默认 true（只在现有分类内匹配）"},
            "writeback": {"type": "bool", "required": False, "desc": "默认 true（回写 Zotero）"},
        },
        "danger": True,
        "fn": _t_batch_notes,
    },
    {
        "name": "reading_status",
        "title": "精读状态",
        "category": "精读",
        "description": "查询当前是否有批量精读在跑、进度如何，以及知识库索引概况。只读。",
        "params": {},
        "danger": False,
        "fn": _t_reading_status,
    },
    # —— 维护与产物 ——
    {
        "name": "run_maintenance",
        "title": "维护工具箱",
        "category": "维护",
        "description": "运行白名单维护脚本（订阅候选清单、覆盖矩阵、素材包、标签扫描/核验、去重、标签归并）。"
                       "变更类脚本默认 dry-run，只有 apply=true 才真正改动数据（此时属**写库**）。",
        "params": {
            "tool": {"type": "string", "required": True,
                     "enum": ["lit_watch", "coverage", "digest", "pack", "tag_analysis",
                              "verify_tags", "dedup", "tag_merge"]},
            "apply": {"type": "bool", "required": False,
                      "desc": "仅对变更类脚本（dedup / tag_merge）有意义；默认 false 即只预览"},
        },
        "danger": _maintenance_danger,
        "fn": _t_run_maintenance,
    },
    {
        "name": "list_reports",
        "title": "报告产物",
        "category": "维护",
        "description": "列出 reports/ 目录下已生成的报告产物（覆盖矩阵、候选清单、素材包等）。只读。",
        "params": {},
        "danger": False,
        "fn": _t_list_reports,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def get(name):
    return _BY_NAME.get((name or "").strip())


def names():
    return [t["name"] for t in TOOLS]


def is_danger(name, args=None):
    """结合参数判定这一次调用是否属于危险（写库）动作。"""
    t = get(name)
    if not t:
        return False
    d = t.get("danger", False)
    if callable(d):
        try:
            return bool(d(args or {}))
        except Exception:
            return True          # 判定失败时宁严勿松
    return bool(d)


def impact(name, args):
    """危险步骤的影响面：给用户看的"这一步会动到什么、动多少"。

    ⚠ 拿不到准确条数时**必须说"不确定"**，绝不能糊一个数字上去。踩过的坑：
    参数里 `records` 缺失（模型漏写）时，原来会显示"将写入 1 条文献"——
    用户看到 347 条检索结果却被告知只写 1 条，完全被误导。
    """
    args = args or {}
    if name == "lit_import":
        recs = args.get("records")
        where = (args.get("collection") or "").strip()
        tail = f"，归入分类「{where}」" if where else "（不改变分类）"
        try:
            cap = int(args.get("max") or 0)
        except Exception:
            cap = 0
        if not isinstance(recs, list):
            return ("将把上一步检索到的文献写入 Zotero" + tail
                    + "（具体条数要等这一步真的取到上一步结果才知道）")
        n = len(recs)
        if cap > 0 and n > cap:
            return f"将向 Zotero 写入 {cap} 条文献{tail}（共检索到 {n} 条，已按上限取前 {cap} 条）"
        return f"将向 Zotero 写入 {n} 条文献" + tail
    if name == "batch_notes":
        keys = args.get("keys")
        back = "并回写 Zotero 笔记" if args.get("writeback", True) else "（不回写）"
        if not isinstance(keys, list):
            return f"将对选中的文献批量生成精读笔记{back}（具体篇数要等这一步取到候选清单才知道）"
        return f"将为 {len(keys)} 篇文献生成精读笔记{back}"
    if name == "run_maintenance":
        return f"将执行变更脚本「{args.get('tool')}」并应用改动（不可逆，请先在面板里 dry-run 预览）"
    return "该步骤会修改本地数据"


# ---------------------------------------------------------------- 说明书 / 执行


def _param_line(p):
    bits = [p.get("type", "any")]
    if p.get("enum"):
        bits.append("取值：" + " / ".join(str(e) for e in p["enum"]))
    if p.get("required"):
        bits.append("必填")
    else:
        bits.append("可选")
    if p.get("desc"):
        bits.append(p["desc"])
    return "；".join(bits)


# 每个工具的输出字段（`data` 里的键）。这份表既给 LLM 看（写进说明书，让它知道
# 引用时该写哪个字段名），也给前端看（把 `$s2.results` 翻成人话："第 2 步的结果列表"）。
RETURNS = {
    "lit_generate_queries": "queries（字符串数组，生成的检索式）",
    "lit_search": "count（条数）；results（文献对象数组，每条含 title/authors/year/doi/abstract/url/in_library）；in_library（已在库条数）",
    "lit_import": "created（新建条数）；skipped（跳过重复）；pdf_ok（抓到全文的篇数）；items（新建条目的 key 与标题）",
    "zotero_search_items": "count；items（数组，每项含 key/title/itemType/has_pdf）",
    "zotero_recent_items": "count；items（同上结构）",
    "zotero_collections": "count；collections（数组，每项含 key/name/depth）",
    "zotero_collection_items": "count；items（同上结构）",
    "kb_search": "count；hits（数组，每项含 title/section/score/snippet）",
    "kb_stats": "items（已索引篇数）；chunks（片段数）；pending（已解析待索引篇数）",
    "reading_candidates": "count；items（数组，每项含 key/title/itemType/year）",
    "batch_notes": "total（本次安排精读的篇数）；keys（条目 key 数组）；background（true 表示后台长任务）",
    "reading_status": "batch（含 running/done/total/log_tail）；kb（含 items/pending）",
    "run_maintenance": "tool；apply（是否真正应用了改动）；output（脚本输出尾部）",
    "list_reports": "count；reports（数组，每项含 name/size）",
}

# 上面那段的机器可读版本：只列字段名，供"计划体检"判断 `$sN.字段` 是否真的存在。
# 与 RETURNS 同源（改一处记得改另一处；加字段时两边都要加）。
RETURN_FIELDS = {
    "lit_generate_queries": ["queries"],
    "lit_search": ["count", "results", "in_library"],
    "lit_import": ["created", "skipped", "pdf_ok", "items"],
    "zotero_search_items": ["count", "items"],
    "zotero_recent_items": ["count", "items"],
    "zotero_collections": ["count", "collections"],
    "zotero_collection_items": ["count", "items"],
    "kb_search": ["count", "hits"],
    "kb_stats": ["items", "chunks", "pending"],
    "reading_candidates": ["count", "items"],
    "batch_notes": ["total", "keys", "background"],
    "reading_status": ["batch", "kb"],
    "run_maintenance": ["tool", "apply", "output"],
    "list_reports": ["count", "reports"],
}


def spec_text():
    """导出「给 LLM 看的工具说明书」文本（规划 prompt 里直接用）。

    每条都写明**输出字段**——模型必须知道上一步的输出里到底有哪些字段可以引用，
    否则它会瞎写（实测踩过：把 `$s2.results` 写成 `s2.results`，或干脆给个空数组）。
    """
    lines = []
    for i, t in enumerate(TOOLS, 1):
        danger = t.get("danger", False)
        tag = "【写库·需用户确认】" if (danger is True or callable(danger)) else "【只读】"
        lines.append(f"{i}. {t['name']} {tag} {t['description']}")
        ps = t.get("params") or {}
        if ps:
            for k, p in ps.items():
                lines.append(f"     - 入参 {k}: {_param_line(p)}")
        else:
            lines.append("     - 入参：（无）")
        ret = RETURNS.get(t["name"])
        if ret:
            lines.append(f"     - 输出字段（可被后续步骤用 $sN.字段 引用）：{ret}")
    return "\n".join(lines)


def run(name, args, ctx=None):
    """执行工具。**永不抛异常**：任何失败都归一成 {ok: False, ...}。"""
    ctx = ctx or Ctx()
    t = get(name)
    if not t:
        return _err(name, f"未知工具：{name}（可用：{'、'.join(names())}）")
    args = args if isinstance(args, dict) else {}
    # 必填校验：缺必填时直接给模型一句可读的错，省一次无谓的脚本执行
    missing = [k for k, p in (t.get("params") or {}).items() if p.get("required") and not args.get(k)]
    if missing:
        return _err(name, f"缺少必填参数：{'、'.join(missing)}")
    t0 = __import__("time").time()
    try:
        res = t["fn"](args, ctx)
    except Exception as e:
        res = _err(name, f"执行异常：{repr(e)[:200]}")
    if not isinstance(res, dict):
        res = _err(name, "工具返回了非法结果")
    res.setdefault("ok", False)
    res["tool"] = name
    res["elapsed"] = round(__import__("time").time() - t0, 2)
    res.setdefault("summary", "")
    res.setdefault("data", {})
    try:
        ctx.log(f"[{name}] {res.get('summary') or res.get('error')}")
    except Exception:
        pass
    return res


def spec_json():
    """结构化说明书（供前端渲染工具清单）。"""
    out = []
    for t in TOOLS:
        out.append({"name": t["name"], "title": t.get("title", t["name"]),
                    "category": t.get("category", ""), "description": t.get("description", ""),
                    "params": {k: {"type": v.get("type"), "required": bool(v.get("required")),
                                   "desc": v.get("desc", "")}
                               for k, v in (t.get("params") or {}).items()},
                    "danger": (t.get("danger") is True or callable(t.get("danger"))),
                    "danger_kind": ("always" if t.get("danger") is True
                                    else ("conditional" if callable(t.get("danger")) else "never"))})
    return json.loads(json.dumps(out, ensure_ascii=False))
