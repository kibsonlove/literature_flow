# -*- coding: utf-8 -*-
"""长文精读：学位论文 / 专著等超长文献的「分章精读 + 全书总纲」管线。

背景：网页端上传与模型上下文都装不下一本书（100–600 页）。本管线改为：
    MinerU 全文解析（无上传限制）→ 章节树 → 逐章精读（文本片段，不上传）
    → 汇总为「全书总纲」子笔记 + 索引进知识库（供按需检索原文）

产物：
    1. Zotero 子笔记「全书总纲（分章精读）」——结构地图 + 各章要点 + 关键数据索引
    2. 知识库索引（kb）——随时用自然语言调取原文证据
"""
import json
import os
import re
import time

_DIR = os.path.dirname(os.path.abspath(__file__))

# 不精读的章节（参考文献/致谢等：量大、信息量低）
SKIP_PAT = re.compile(r"(参考文献|引用文献|致\s*谢|附录|后记|鸣谢|目\s*录|版权|参考文献表|References|Bibliography|Acknowledg|Contents)", re.I)

MAX_CHAPTER_CHARS = 12000     # 单章上限：超出按段落切分
MIN_CHAPTER_CHARS = 1200      # 小于此长度的章节并入上一章

# ---------------------------------------------------------------- 章节树

def _noise_heading(title):
    """判断是否噪声标题（页眉单字、图表题、纯数字符号）——不是真章节。"""
    t = (title or "").strip()
    if len(t) < 2:
        return True
    if re.match(r"^[\d\W_]+$", t):
        return True
    if re.match(r"^(图|表|附录图|Figure|Table|Fig\.|Tab\.)\s*[一二三四五六七八九十\d]", t, re.I):
        return True
    if re.match(r"^[（(]?\d{1,3}[）)]?$", t):
        return True
    return False


def outline(md, drop_skipped=True):
    """从 MinerU Markdown 提取章节树。

    噪声标题（页眉单字/图表题等）不丢内容，其后的正文并入上一章。
    返回 [{"title": 标题, "text": 正文, "chars": 字数}]
    """
    lines = md.split("\n")
    chapters, cur_title, cur_buf = [], "（开头）", []
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.+?)\s*$", ln)
        if m and not _noise_heading(m.group(2)):
            text = "\n".join(cur_buf).strip()
            if text:
                chapters.append({"title": cur_title, "text": text})
            cur_title, cur_buf = m.group(2).strip(), []
        else:
            cur_buf.append(ln)
    text = "\n".join(cur_buf).strip()
    if text:
        chapters.append({"title": cur_title, "text": text})

    if len(chapters) <= 1:      # 无标题结构（扫描件/无目录）→ 按长度切
        body = "\n".join(lines)
        step = 8000
        chapters = [{"title": f"第 {k // step + 1} 段", "text": body[k:k + step]}
                    for k in range(0, len(body), step)]

    if drop_skipped:
        chapters = [c for c in chapters if not SKIP_PAT.search(c["title"])]

    # 合并过短章节
    merged = []
    for c in chapters:
        if merged and len(c["text"]) < MIN_CHAPTER_CHARS:
            merged[-1]["text"] += "\n\n" + f"### {c['title']}\n" + c["text"]
        else:
            merged.append(dict(c))

    # 拆分过长章节
    out = []
    for c in merged:
        if len(c["text"]) <= MAX_CHAPTER_CHARS:
            c["chars"] = len(c["text"])
            out.append(c)
            continue
        parts = _split_long(c["text"], MAX_CHAPTER_CHARS)
        for j, p in enumerate(parts, 1):
            out.append({"title": f"{c['title']}（{j}/{len(parts)}）", "text": p, "chars": len(p)})
    return out


def _split_long(text, limit):
    """按段落边界切分过长章节。"""
    paras, buf, cur = text.split("\n\n"), [], 0
    parts = []
    for p in paras:
        if cur + len(p) > limit and buf:
            parts.append("\n\n".join(buf))
            buf, cur = [], 0
        buf.append(p)
        cur += len(p)
    if buf:
        parts.append("\n\n".join(buf))
    return parts

# ---------------------------------------------------------------- 逐章精读

_CH_PROMPT = """下面是《{book}》的章节：{chapter}

请为这一章写结构化要点，中文输出，Markdown：

- 本章结论/贡献（2–4 条，直说结论，不要"本章介绍了…"这种空话）
- 关键证据与数据（有表格/数字就列，写明数值与单位；没有就写"无"）
- 论证方法与材料（从简，一行）
- 与整体论证的关系（这一章在全书里承担什么）

要求：只依据给定文本，不要编造数字；若文本被截断或残缺，明确写出"（本章文本不完整）"。
"""

def summarize_chapter(book_title, title, text, domain_label="", timeout=180):
    """用 LLM 汇总单章。返回 Markdown 文本。"""
    from .read import _llm_chat
    prompt = _CH_PROMPT.replace("{book}", book_title[:80]).replace("{chapter}", title[:80])
    body = f"章节标题：{title}\n\n正文：\n{text[:MAX_CHAPTER_CHARS]}"
    if domain_label:
        body += f"\n\n（本篇所处领域：{domain_label}，请优先提炼该领域关心的证据）"
    return _llm_chat(body, system=prompt + "\n你是严谨的学术文本摘要助手。", timeout=timeout)

# ---------------------------------------------------------------- 总纲组装

def build_outline_note(book_title, chapters, digests, pack_label="", pack_src="",
                       mineru_url="", backend="api"):
    """把各章摘要组装成「全书总纲」HTML。"""
    import markdown as _md

    rows = "".join(
        f"<tr><td>{i+1}</td><td>{c['title']}</td><td>约 {c['chars']} 字</td></tr>"
        for i, c in enumerate(chapters))
    parts = [f"<h1>{book_title}｜全书总纲（分章精读）</h1>",
             f'<p><span style="font-size:11px">共 {len(chapters)} 章 ｜ 生成方式：分章精读（{backend}）'
             + (f' ｜ <a href="{mineru_url}">查看 MinerU 解析</a>' if mineru_url else "")
             + "</span></p>"]
    if pack_label:
        parts.append(f"<p>- 分章模板：{pack_label}（{pack_src}）</p>" if pack_src and "未匹配" not in pack_src
                 else "<p>- 分章模板：通用（章节要点式，未匹配到领域包）</p>")
    parts.append("<h2>结构地图</h2>")
    parts.append('<table style="border-collapse:collapse"><tr><th>#</th><th>章节</th><th>篇幅</th></tr>'
                 + rows + "</table>")
    parts.append("<h2>各章要点</h2>")
    for i, (c, d) in enumerate(zip(chapters, digests), 1):
        parts.append(f"<h3>{i}. {c['title']}</h3>")
        parts.append(_md.markdown(d or "（本章摘要生成失败）", extensions=["tables"]))
    parts.append("<h2>使用说明</h2>")
    parts.append("<p>本笔记由「分章精读」自动生成：MinerU 解析全书 → 按章节分别精读 → 汇总。"
                 "原文证据可在这篇文献的「MinerU 解析」子笔记中检索，或在 webapp「语义检索」里按问题调取。</p>")
    return "\n".join(parts)

# ---------------------------------------------------------------- 主流程

def read_longdoc(item_key, backend="api", max_chapters=None, log=lambda m: None,
                 write_note=True, index_kb=True, pdf_path=None, force_parse=False,
                 auto_domain=True):
    """长文精读主流程。返回结果字典（含进度信息）。"""
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(_DIR, "..")))
    from core import domain as _dom, kb, mineru
    from core.zotero_io import _client, add_note

    z = _client()
    try:
        data = z.item(item_key)["data"]
        book_title = data.get("title") or item_key
    except Exception as e:
        raise RuntimeError(f"读取条目失败：{item_key}（{repr(e)[:120]}）")

    # 领域包（按标题匹配）；API 路径允许未匹配时自动造包（auto_domain）
    pack_name, pack, pack_src = _dom.pick_domain(book_title, "", auto_generate=auto_domain,
                                                 log=log)

    from core.extract import pdf_path_from_zotero_key

    md_path = None if force_parse else mineru.cache_file(item_key)
    if not md_path:
        p = pdf_path or pdf_path_from_zotero_key(item_key)
        if not p:
            raise RuntimeError("找不到该条目的 PDF 文件，无法解析")
        log("② MinerU 解析全文（长文可能 1–5 分钟，首次解析需等待）…")
        md_path = mineru.ensure_cache(p, item_key, log=log, force=force_parse)
        if not md_path:
            raise RuntimeError("MinerU 解析失败（详见日志）")
        log(f"   解析完成：{os.path.basename(md_path)}")
    md = open(md_path, encoding="utf-8").read()
    log(f"③ 已载入全文（{len(md)} 字符）")

    chapters = outline(md)
    if max_chapters:
        chapters = chapters[:int(max_chapters)]
    log(f"章节树：{len(chapters)} 章（跳过参考文献/致谢等）")
    for i, c in enumerate(chapters, 1):
        log(f"  {i}. {c['title'][:40]}（{c['chars']} 字）")

    digests = []
    for i, c in enumerate(chapters, 1):
        try:
            log(f"[{i}/{len(chapters)}] 精读：{c['title'][:36]} …")
            digests.append(summarize_chapter(book_title, c["title"], c["text"], pack["label"]))
        except Exception as e:
            log(f"  失败（跳过本章）：{repr(e)[:80]}")
            digests.append("")

    html = build_outline_note(
        book_title, chapters, digests, pack_label=pack["label"], pack_src=pack_src,
        mineru_url=f"http://127.0.0.1:8000/api/mineru/{item_key}", backend=backend)

    if write_note:
        marker = "全书总纲（分章精读）"
        existing = None
        try:
            for ch in z.children(item_key):
                if ch["data"].get("itemType") == "note" and marker in (ch["data"].get("note") or ""):
                    existing = ch
                    break
        except Exception:
            existing = None
        if existing:
            d = existing["data"]
            d["note"] = html
            z.update_item(d)
            log("已更新子笔记「全书总纲（分章精读）」")
        else:
            add_note(item_key, html, title=f"{book_title}｜全书总纲")
            log("已写入子笔记「全书总纲（分章精读）」")

    if index_kb:
        try:
            r = kb.index_item(item_key, md_path=md_path)
            log(f"知识库索引：{r.get('chunks', r.get('error'))}")
        except Exception as e:
            log(f"索引失败（忽略）：{repr(e)[:80]}")

    return {"ok": True, "item_key": item_key, "title": book_title,
            "chapters": len(chapters), "chars": len(md), "note_chars": len(html)}


# ==================== 网页端（零费用）分章精读 ====================

BATCH_CHARS = 35000        # 单个对话承载的章节原文上限（约 25–35K token，留足回答空间）

def _strip_html(h):
    import re as _re
    t = _re.sub(r"<(script|style)[\s\S]*?</\1>", " ", h or "", flags=_re.I)
    t = _re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _re.sub(r"[ \t]+", " ", t).strip()


def _batch_chapters(chapters, budget=BATCH_CHARS):
    """把章节按字符预算分批（保证单会话不超上下文）。"""
    batches, cur, size = [], [], 0
    for c in chapters:
        if cur and size + c["chars"] > budget:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += c["chars"]
    if cur:
        batches.append(cur)
    return batches


def read_longdoc_webchat(item_key, log=lambda m: None, batch_chars=BATCH_CHARS,
                         max_chapters=None, write_note=True, index_kb=True, headless=False,
                         pdf_path=None, force_parse=False):
    """网页端分章精读（零费用）。

    流程：分批 → 每批一个对话（投喂该批章节原文 → 要求逐章要点 → 同一对话内
    汇总批次摘要）→ 最后开总纲对话，把各批摘要汇总为全书总纲。
    """
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(_DIR, "..")))
    from core import domain as _dom, kb, mineru
    from core.webchat import WebChatTextSession
    from core.zotero_io import _client

    z = _client()
    try:
        book_title = z.item(item_key)["data"].get("title") or item_key
    except Exception as e:
        raise RuntimeError(f"读取条目失败：{item_key}（{repr(e)[:100]}）")

    # 长文流程的提示词是通用的“章节要点”式，不依赖领域维度；
    # 网页端路径保持零费用：不额外调 LLM 造包，未匹配就用通用分章模板
    pack_name, pack, pack_src = _dom.pick_domain(book_title, "", auto_generate=False, log=log)
    # MinerU 全文：无缓存时自动触发解析（超长 PDF 会自动分片），与 API 版行为一致
    md_path = None if force_parse else mineru.cache_file(item_key)
    if not md_path:
        from core.extract import pdf_path_from_zotero_key
        p = pdf_path or pdf_path_from_zotero_key(item_key)
        if not p:
            raise RuntimeError("找不到该条目的 PDF 文件，无法解析")
        log("② MinerU 解析全文（长文自动分片，可能数分钟）…")
        md_path = mineru.ensure_cache(p, item_key, log=log, force=force_parse)
        if not md_path:
            raise RuntimeError("MinerU 解析失败（详见日志）")
        log(f"   解析完成：{os.path.basename(md_path)}")
    md = open(md_path, encoding="utf-8").read()
    log(f"③ 已载入全文（{len(md)} 字符）")
    chapters = outline(md)
    if max_chapters:
        chapters = chapters[:int(max_chapters)]
    batches = _batch_chapters(chapters, batch_chars)
    log(f"全书 {len(chapters)} 章 → 分 {len(batches)} 批（每批 ≤{batch_chars} 字符，网页端零费用）")

    batch_summaries = []
    for bi, batch in enumerate(batches, 1):
        titles = "、".join(c["title"][:18] for c in batch)
        log(f"[批 {bi}/{len(batches)}] 共 {len(batch)} 章：{titles}")
        s = WebChatTextSession(headless=headless, log=lambda m: log("    " + m))
        try:
            s.open()
            body = "\n\n".join(
                f"【第{k}章】{c['title']}\n{c['text']}" for k, c in enumerate(batch, 1))
            ask1 = (f"下面是《{book_title}》的 {len(batch)} 个章节原文，请**逐章**给出要点，"
                    f"每章 150–300 字，格式：\n### 章节标题\n- 核心内容\n- 关键概念/数据\n\n"
                    f"只依据原文，不要编造；原文残缺处注明。\n\n{body}")
            s.ask(ask1, timeout=1800)
            log("    批内逐章要点完成，开始批内汇总…")
            ask2 = ("请把上面各章要点汇总成一份**批次摘要**（400–600 字），"
                    "保留章节顺序与关键概念，不要新增原文没有的内容。")
            summary_html = s.summarize_here(ask2, timeout=900)
            batch_summaries.append(_strip_html(summary_html))
            log(f"    批次摘要 {len(batch_summaries[-1])} 字")
        except Exception as e:
            log(f"    批次失败（跳过）：{repr(e)[:100]}")
            batch_summaries.append("")
        finally:
            try:
                s.close(delete_chat=True)
            except Exception:
                pass

    # 总纲对话：把各批摘要投进去
    log("开始全书总纲（新对话，投喂各批摘要）…")
    joined = "\n\n".join(f"【第{i}批要点】\n{t}" for i, t in enumerate(batch_summaries, 1) if t)
    outline_html = ""
    if joined:
        s2 = WebChatTextSession(headless=headless, log=lambda m: log("    " + m))
        try:
            s2.open()
            outline_html = s2.ask(
                f"以下是《{book_title}》全书各批次要点汇总。请产出**全书总纲**，用 Markdown：\n"
                f"1) 结构地图：本书讲了什么、怎么组织（200 字内）\n"
                f"2) 分章要点：按批次顺序列出，每章一行要点\n"
                f"3) 核心概念与关键数据：术语表与量化信息（有则列）\n"
                f"4) 值得留意的争议或局限\n\n{joined}", timeout=1800)
        except Exception as e:
            log(f"    总纲生成失败：{repr(e)[:100]}")
        finally:
            try:
                s2.close(delete_chat=True)
            except Exception:
                pass

    import markdown as _md
    parts = [f"<h1>{book_title}｜全书总纲（分章精读·网页端）</h1>",
             f'<p><span style="font-size:11px">共 {len(chapters)} 章 / {len(batches)} 批 ｜ 生成方式：'
             f'网页端分章精读（零费用）'
             + (f' ｜ <a href="http://127.0.0.1:8000/api/mineru/{item_key}">查看 MinerU 解析</a>' if True else "")
             + "</span></p>",
             (f"<p>- 分章模板：{pack['label']}（{pack_src}）</p>" if "未匹配" not in (pack_src or "")
         else "<p>- 分章模板：通用（章节要点式，未匹配到领域包）</p>"),
             "<h2>结构地图与总纲</h2>",
             _md.markdown(_strip_html(outline_html) or "（总纲生成失败）", extensions=["tables"]) if outline_html else "<p>（总纲生成失败）</p>",
             "<h2>各批要点（原文摘要）</h2>"]
    for i, t in enumerate(batch_summaries, 1):
        parts.append(f"<h3>第 {i} 批（第 {i} 组章节）</h3>")
        parts.append("<p>" + (t or "（本批失败）") + "</p>")

    html = "\n".join(parts)
    if write_note:
        marker = "全书总纲（分章精读·网页端）"
        existing = None
        try:
            for ch in z.children(item_key):
                if ch["data"].get("itemType") == "note" and marker in (ch["data"].get("note") or ""):
                    existing = ch
                    break
        except Exception:
            existing = None
        if existing:
            d = existing["data"]; d["note"] = html; z.update_item(d)
            log("已更新子笔记「全书总纲（网页端）」")
        else:
            from core.zotero_io import add_note
            add_note(item_key, html, title=f"{book_title}｜全书总纲（网页端）")
            log("已写入子笔记「全书总纲（网页端）」")
    if index_kb:
        try:
            r = kb.index_item(item_key, md_path=md_path)
            log(f"知识库索引：{r.get('chunks', r.get('error'))}")
        except Exception as e:
            log(f"索引失败（忽略）：{repr(e)[:80]}")
    return {"ok": True, "item_key": item_key, "title": book_title,
            "chapters": len(chapters), "batches": len(batches), "note_chars": len(html)}
