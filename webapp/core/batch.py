# -*- coding: utf-8 -*-
"""批量精读笔记：后台线程逐篇生成（按篇自动选领域包）→ 自动分类 → 批量回写 Zotero。

状态存内存（_STATE），前端轮询 /api/batch/status 显示进度。
分类：在精读指令末尾追加要求模型输出「建议分类 / 推荐标签」，从笔记里解析出来，
     **只在现有分类树内匹配**（精确 → 长名包含），匹配不上就不归类，绝不新建 collection。
"""
import re
import threading
import time
from datetime import datetime

from .extract import pdf_path_from_zotero_key
from .prompt import SYSTEM_PROMPT
from .render import wrap_html
from .webchat import run_webchat
from . import mineru
from .zotero_io import _client, add_note, add_tags, list_collections, ping

_CLASSIFY_INSTRUCTION = """

---
【额外要求·必须遵守】在整篇笔记的**最末尾**，另起一行，严格按下面两行格式输出（不要加解释）：
建议分类：<一个最贴切的分类名，2-8 个汉字；现有分类都不合适时给出新分类名>
推荐标签：<空格分隔的 3-6 个标签，每个以 # 开头>
"""

_NO_CAT_WORDS = {"无", "无合适分类", "无合适分类。", "不归类", "未分类", "-"}


def _classify_instruction(existing_names):
    """生成带现有分类列表的指令：模型只能从中选，选不出输出「无」。"""
    listing = "、".join(n for n in existing_names if n)
    return _CLASSIFY_INSTRUCTION.replace(
        "现有分类都不合适时给出新分类名",
        f"只能从现有分类中选一个：{listing}；都不合适则输出「无」",
    )

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "stop": False,
    "total": 0,
    "done": 0,
    "current": "",
    "log": [],
    "results": [],
    "started_at": None,
    "finished_at": None,
    "options": {},
}


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _LOCK:
        _STATE["log"].append(f"[{ts}] {msg}")
        if len(_STATE["log"]) > 800:
            _STATE["log"] = _STATE["log"][-400:]


def status():
    with _LOCK:
        s = dict(_STATE)
        s["log"] = list(_STATE["log"])
        s["results"] = list(_STATE["results"])
        return s


def stop():
    with _LOCK:
        _STATE["stop"] = True
    return True


def _extract_classify(html):
    """从笔记末尾解析「建议分类 / 推荐标签」，并从笔记中移除该两行。返回 (html, 分类, 标签)。"""
    cat, tags = "", []
    tail_plain = re.sub(r"<[^>]+>", "", html[-1500:])
    m = re.search(r"建议分类\s*[:：]\s*([^\n\r<]+?)(?:推荐标签|$)", tail_plain)
    if m:
        cat = m.group(1).strip().strip("。 .、")
        if cat in _NO_CAT_WORDS:
            cat = ""
    m2 = re.search(r"推荐标签\s*[:：]\s*([^\n\r<]+)", tail_plain)
    if m2:
        tags = [t for t in re.findall(r"#([^\s#，,、]+)", m2.group(1)) if t]

    # 从 HTML 里删掉含这两行的段落（含可能的空 <p>）
    cleaned = re.sub(
        r"<p[^>]*>(?:(?!</p>).)*?(?:建议分类|推荐标签)(?:(?!</p>).)*?</p>",
        "", html, flags=re.S | re.I,
    )
    if cleaned == html:  # 兜底：不是 <p> 包裹时，按纯文本行截断
        cleaned = re.sub(r"[\s\S]*?建议分类[\s\S]*$", "", html)
    cleaned = re.sub(r"(<p[^>]*>\s*</p>\s*)+$", "", cleaned).strip()
    return cleaned, cat, tags


def _collections_map():
    """返回 {分类名: key}。"""
    m = {}
    try:
        for n in list_collections():
            stack = [n]
            while stack:
                x = stack.pop()
                m[x["name"]] = x["key"]
                stack.extend(x.get("children", []))
    except Exception:
        pass
    return m


def _ensure_collection(z, name, cache):
    """只在现有分类树内匹配（精确 → 长名包含），绝不新建分类。匹配不上返回 None（保持未分类）。"""
    if not name:
        return None
    if name in cache:
        return cache[name]
    for existing in sorted(cache, key=len, reverse=True):
        if existing and existing in name:
            _log(f"  「{name}」未精确匹配，归入现有分类「{existing}」")
            return cache[existing]
    _log(f"  现有分类中无「{name}」，不归类（保持未分类）")
    return None


def _assign_collection(z, item_key, col_key):
    try:
        it = z.item(item_key)
        cs = it["data"].get("collections", []) or []
        if col_key not in cs:
            cs.append(col_key)
            it["data"]["collections"] = cs
            z.update_item(it)
        return True
    except Exception as e:
        _log(f"  归入分类失败：{repr(e)[:100]}")
        return False


def candidates(collection_key=None, skip_thesis=True, limit=300):
    """列出「含 PDF 且尚无六维笔记」的条目（可指定分类）。"""
    z = _client()
    from .zotero_io import _SKIP_TYPES, _pdf_parents_set

    if collection_key:
        from .zotero_io import list_collection_items
        items = list_collection_items(collection_key, include_children=True, limit=limit)
        ids = {it["key"]: it for it in items}
        tops = []
        for k in ids:
            try:
                tops.append(z.item(k))
            except Exception:
                pass
    else:
        tops = z.everything(z.top()) if hasattr(z, "everything") else z.top(limit=limit)

    pdf_parents = _pdf_parents_set(z)
    noted = set()
    try:
        for n in z.items(itemType="note", limit=500):
            txt = n["data"].get("note") or ""
            if ("六维速览" in txt or "六维精读" in txt or "维度速览" in txt) and n["data"].get("parentItem"):
                noted.add(n["data"]["parentItem"])
    except Exception:
        pass

    out = []
    for it in tops:
        d = it["data"]
        if d.get("itemType") in _SKIP_TYPES:
            continue
        if skip_thesis and d.get("itemType") == "thesis":
            continue
        if it["key"] not in pdf_parents:
            continue
        if it["key"] in noted:
            continue
        out.append({
            "key": it["key"],
            "title": d.get("title") or "(无标题)",
            "itemType": d.get("itemType"),
            "year": (d.get("date") or "")[:4],
        })
    out.sort(key=lambda x: x.get("title") or "")
    return out


def _run(keys, opts):
    with _LOCK:
        _STATE.update({
            "running": True, "stop": False, "total": len(keys), "done": 0,
            "current": "", "log": [], "results": [],
            "started_at": time.time(), "finished_at": None, "options": opts,
        })

    z = _client()
    col_cache = _collections_map()
    classify_instr = _classify_instruction(col_cache.keys())

    # 二次保险：开始前再查一遍已有六维笔记的条目（防止面板里挂着旧候选列表导致重复生成）
    noted = set()
    try:
        for n in z.items(itemType="note", limit=500):
            txt = n["data"].get("note") or ""
            if ("六维速览" in txt or "六维精读" in txt or "维度速览" in txt) and n["data"].get("parentItem"):
                noted.add(n["data"]["parentItem"])
    except Exception:
        pass

    _log(f"开始批量：{len(keys)} 篇（跳过学位论文={opts.get('skip_thesis')}，自动分类={opts.get('auto_classify')}，回写={opts.get('writeback')}）；现有分类：{'、'.join(col_cache) or '无'}")

    # 复用同一个浏览器上下文（避免每篇重启导致登录态丢失 / 被风控）
    from .webchat import WebChatSession

    sess = None
    try:
        sess = WebChatSession(headless=False, log=lambda m: _log("    " + m))
        sess.open()
    except Exception as e:
        _log(f"✗ 浏览器/登录失败：{e}")
        _log("提示：请先在命令行运行 `python login_deepseek.py`，用可见浏览器登录一次。")
        with _LOCK:
            _STATE["running"] = False
            _STATE["current"] = ""
            _STATE["finished_at"] = time.time()
        return

    for i, k in enumerate(keys, 1):
        # ⚠ 必须先出锁再调 _log：_log 内部会再次 _LOCK.acquire()，
        # 而 threading.Lock 不可重入 —— 在锁内调用会永久死锁，
        # 导致 /api/batch/status 与 /api/batch/stop 一起挂死（实测 HTTP 000）。
        with _LOCK:
            stopped = _STATE["stop"]
        if stopped:
            _log("收到停止指令，中断。")
            break
        title = k
        try:
            it = z.item(k)
            d = it["data"]
            title = d.get("title") or k

            if opts.get("skip_thesis", True) and d.get("itemType") == "thesis":
                _log(f"[{i}/{len(keys)}] 跳过学位论文：{title[:36]}")
                with _LOCK:
                    _STATE["results"].append({"key": k, "title": title, "status": "skipped"})
                    _STATE["done"] += 1
                continue

            if k in noted:
                _log(f"[{i}/{len(keys)}] 已有笔记，跳过：{title[:36]}")
                with _LOCK:
                    _STATE["results"].append({"key": k, "title": title, "status": "already_noted"})
                    _STATE["done"] += 1
                continue

            with _LOCK:
                _STATE["current"] = f"[{i}/{len(keys)}] {title[:40]}"

            path = pdf_path_from_zotero_key(k)
            if not path:
                _log(f"[{i}/{len(keys)}] 无 PDF，跳过：{title[:36]}")
                with _LOCK:
                    _STATE["results"].append({"key": k, "title": title, "status": "no_pdf"})
                    _STATE["done"] += 1
                continue

            _log(f"[{i}/{len(keys)}] 生成中：{title[:36]}")
            t0 = time.time()
            # MinerU 表格增强：优先读缓存，缺失则调云 API（有 key 才启用）
            # 领域包：按本篇匹配（未命中则自动生成新包），提示词随之切换
            try:
                from core.extract import pdf_to_text as _p2t
                _meta0, _text0 = _p2t(path)
            except Exception:
                _meta0, _text0 = {"title": title}, ""
            from core import domain as _dom
            _pack_name, _pack, _pack_src = _dom.pick_domain(
                title or _meta0.get("title", ""), _text0,
                log=lambda m: _log("    " + m))
            prompt = _dom.system_prompt(_pack_name) + classify_instr
            tables_len = 0
            if mineru.mineru_available():
                try:
                    tables = mineru.get_tables_markdown(
                        path, item_key=k, log=lambda m: _log("    " + m))
                    if tables:
                        tables_len = len(tables)
                        prompt += ("\n\n---\n【附·MinerU 结构化表格数据（原文量化表格，"
                                   "笔记中的数字必须与这些表格一致）】\n" + tables)
                        _log(f"[{i}/{len(keys)}] 已附 MinerU 表格（{len(tables)} 字符）")
                except Exception as e:
                    _log(f"    MinerU 表格获取失败（忽略，回退纯文本）：{repr(e)[:80]}")
            html = sess.ask(
                path, prompt, title=title,
                timeout=1800, stop_check=lambda: _STATE["stop"],
            )
            if tables_len:
                n_tab, heads = mineru.tables_info(tables)
                mk = f"已应用 {n_tab} 个表格（{tables_len} 字符）"
                if heads:
                    mk += "；表头：" + " / ".join(heads[:3])[:60]
                mk += f" ｜缓存：/api/mineru/{k}"
            else:
                mk = "未应用（该 PDF 无表格或解析失败）"
            html = mineru.note_marker(html, mk)
            secs = int(time.time() - t0)
            html, cat, tags = _extract_classify(html)
            _log(f"[{i}/{len(keys)}] 生成完成（{len(html)} 字符，用时 {secs // 60}分{secs % 60:02d}秒）；建议分类={cat or '—'}；标签={tags}")

            if opts.get("writeback", True):
                add_note(k, wrap_html(html, title=title))
                if tags:
                    add_tags(k, ["#" + t for t in tags])
                _log(f"[{i}/{len(keys)}] 已回写 Zotero")

            if opts.get("auto_classify", True) and cat:
                ck = _ensure_collection(z, cat, col_cache)
                if ck and _assign_collection(z, k, ck):
                    _log(f"[{i}/{len(keys)}] 已归入分类「{cat}」")

            with _LOCK:
                _STATE["results"].append({
                    "key": k, "title": title, "status": "ok",
                    "category": cat, "tags": tags, "chars": len(html), "secs": secs,
                })
                _STATE["done"] += 1

        except Exception as e:
            _log(f"[{i}/{len(keys)}] ✗ 失败：{repr(e)[:180]}")
            with _LOCK:
                _STATE["results"].append({"key": k, "title": title, "status": "error", "error": str(e)[:200]})
                _STATE["done"] += 1

    if sess:
        sess.close()
    with _LOCK:
        _STATE["running"] = False
        _STATE["current"] = ""
        _STATE["finished_at"] = time.time()
    _log("批量任务结束。")


def start(keys, opts=None):
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "msg": "已有批量任务在运行，请先停止或等待完成"}
    if not keys:
        return {"ok": False, "msg": "没有待处理的条目"}
    if not ping():
        return {"ok": False, "msg": "未检测到 Zotero，请先打开 Zotero"}
    opts = opts or {}
    opts.setdefault("skip_thesis", True)
    opts.setdefault("auto_classify", True)
    opts.setdefault("writeback", True)
    threading.Thread(target=_run, args=(list(keys), opts), daemon=True).start()
    return {"ok": True, "total": len(keys)}
