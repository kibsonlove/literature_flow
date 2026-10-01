# -*- coding: utf-8 -*-
"""文献检索：OpenAlex + arXiv 双源检索、引文追踪、跨源去重、库内查重。

通用模块——不绑定任何学科。检索式、时间窗、数据源、条数全部由调用方传入；
主题配置（检索式 + 时间窗 + 目标分类）落在 config/topics/<slug>.json，属用户数据。

数据源能力对照：
  OpenAlex  免费无 Key，期刊/会议正式收录，含被引数、OA 链接、完整引文图
            （referenced_works 给后向引用；filter=cites:<id> 给前向引用）
  arXiv     免费无 Key，CS 预印本（HCI / 可视化方向的工作多先发 arXiv），无被引数

不做的事：不抓 Google Scholar。它没有官方 API，ToS 禁止自动查询，会弹验证码封 IP，
且结果不可复现（不支持检索式与保存检索），不适合做系统化检索的稳定数据源。

检索式语法：每行一条，支持 AND / OR / NOT 与双引号短语（与 OpenAlex 一致）。
传给 arXiv 时按同一套语义翻译（空白分隔视为 AND）。
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # webapp/
TOPICS_DIR = os.path.join(_APP_DIR, "config", "topics")

OA_BASE = "https://api.openalex.org"
ARXIV_BASE = "https://export.arxiv.org/api/query"
UA = "literature-flow/1.0 (mailto:litflow@example.com)"
_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# 抓开放全文时用的 UA/头：与调 API 用的 UA 分开——API 那边按 OpenAlex 的礼仪带邮箱，
# 抓 PDF 这边要尽量像一个普通浏览器（很多出版社直接拒非浏览器请求）。
# ⚠ 实测：光换 UA 不足以过 MDPI / ACM / OUP（仍是 403），它们看的是指纹而非 UA 字符串，
# 所以那几家必须走下面的「浏览器兜底」，别指望改 UA 就能解决。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_PDF_HEADERS = {
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}

ABSTRACT_MAX = 700


def normalize_since(s):
    """把用户输入的起始时间归一成 OpenAlex 要求的 YYYY-MM-DD。

    「2021」→「2021-01-01」；不合法（长度不是 4/7/10）时返回空串。
    放在模块里统一做，避免各调用点各写一套、漏掉一处就报 400。
    """
    raw = (s or "").strip()
    if not raw:
        return ""
    d = re.sub(r"[^0-9-]", "", raw)
    if re.fullmatch(r"\d{4}", d):
        return d + "-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", d):
        return d + "-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    return ""


# ---------- HTTP ----------

class RateLimited(Exception):
    """数据源限流（HTTP 429）。retry_after 为服务端建议的等待秒数（可能为 None）。"""

    def __init__(self, msg="请求过频被限流", retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class ApiKeyInvalid(Exception):
    """API Key 无效（HTTP 401）。填错 key 比不填更糟——匿名还能用，错的 key 直接全挂，
    所以要单独识别并明确提示用户去检查或清空。"""


def _http(url, timeout=40, retries=2, backoff=1.5):
    """GET 文本。429 / 5xx 时指数退避重试，尊重 Retry-After；仍失败则抛异常。

    关键判断：若 Retry-After 很大，说明是**每日额度**用尽而非瞬时限速，
    此时立刻抛 RateLimited 交给上层熔断 —— 对着"9 小时后再来"的响应重试毫无意义，
    只会让用户白等一分钟。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            ra = e.headers.get("Retry-After") if e.headers else None
            ra_s = int(ra) if (ra and str(ra).isdigit()) else None
            if e.code == 429:
                # 有明确且很大的 Retry-After → 额度型限流，重试也没用，直接交给上层熔断
                if ra_s is not None and ra_s > 120:
                    raise RateLimited("HTTP 429 请求过频", ra_s) from e
                # 无 Retry-After 或很短 → 可能只是瞬时限速，短暂等待后重试
                if attempt < retries:
                    time.sleep(min(max(backoff * (2 ** attempt), ra_s or 0), 30))
                    continue
                raise RateLimited("HTTP 429 请求过频", ra_s) from e
            if e.code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            if e.code == 401:
                raise ApiKeyInvalid("HTTP 401 API Key 无效") from e
            raise
        except Exception as e:                       # 网络抖动也重试
            last = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    raise last


def _oa_key():
    """OpenAlex API Key（环境变量优先，其次 settings.json）。没有也照常工作。"""
    try:
        from core.config import openalex_key
        return openalex_key()
    except Exception:
        return ""


def _oa_url(path, params=None):
    """拼 OpenAlex 请求地址，自动注入 api_key（若已配置）。

    OpenAlex 按**请求**计费（$0.001/次），与 per_page 无关 —— 所以默认取满 100 条，
    同样价钱拿到 4 倍结果。免费 key 把日额度从 $0.1 提到 $1（10 倍）。
    """
    p = dict(params or {})
    k = _oa_key()
    if k:
        p["api_key"] = k
    qs = ("?" + urllib.parse.urlencode(p)) if p else ""
    return OA_BASE + path + qs


def _oa_works(params):
    data = json.loads(_http(_oa_url("/works", params)))
    return data.get("results", [])


def quota():
    """查询 OpenAlex 当前额度（/rate-limit，需 API Key）。返回 dict 或 None（未配 key）。"""
    k = _oa_key()
    if not k:
        return None
    try:
        d = json.loads(_http(_oa_url("/rate-limit", {"api_key": k})))
    except RateLimited:
        return {"has_key": True, "limited": True, "error": "当前已被限流，额度已用尽"}
    except ApiKeyInvalid:
        return {"has_key": True, "bad_key": True,
                "error": "API Key 无效（401）—— 请核对或清空「设置」里的 OpenAlex API Key"}
    except Exception as e:
        return {"has_key": True, "error": repr(e)[:120]}
    rl = d.get("rate_limit") or {}
    return {
        "has_key": True,
        "credits_limit": rl.get("credits_limit"),
        "credits_used": rl.get("credits_used"),
        "credits_remaining": rl.get("credits_remaining"),
        "resets_at": rl.get("resets_at"),
        "resets_in_seconds": rl.get("resets_in_seconds"),
    }


# ---------- 归一化 ----------

def _invert_to_text(inv):
    """OpenAlex 摘要以「词 → 位置列表」的倒排索引给出，还原成连续文本。"""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[k] for k in sorted(pos))


def _oa_candidates(w):
    """一篇论文所有「可能有全文」的链接，按可信度排序并去重。

    只看 `best_oa_location` 是不够的：它指向的那个地址常常是落地页、或者早就失效，
    而 OpenAlex 同时还给了其它开放位置（仓库版、预印本…）。挨个试能多救回一批。
    顺序 = 直接 PDF 优先，其次落地页；best 的排在最前。
    """
    out = []

    def add(u):
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)

    best = w.get("best_oa_location") or {}
    locs = [x for x in (w.get("locations") or []) if isinstance(x, dict)]
    add(best.get("pdf_url"))
    for l in locs:
        add(l.get("pdf_url"))
    add(best.get("landing_page_url"))
    add((w.get("open_access") or {}).get("oa_url"))
    for l in locs:
        add(l.get("landing_page_url"))
    return out


def _norm_openalex(w):
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    cands = _oa_candidates(w)
    oa_url = cands[0] if cands else ""
    abstract = _invert_to_text(w.get("abstract_inverted_index"))[:ABSTRACT_MAX]
    return {
        "uid": "openalex:" + (w.get("id") or "").rsplit("/", 1)[-1],
        "source": "openalex",
        "also": [],
        "title": (w.get("title") or w.get("display_name") or "(无标题)").strip(),
        "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])[:12]
                    if (a.get("author") or {}).get("display_name")],
        "year": w.get("publication_year"),
        "venue": src.get("display_name") or "",
        "venue_type": src.get("type") or "",
        "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
        "url": loc.get("landing_page_url") or w.get("doi") or "",
        "oa_url": oa_url,
        "oa_urls": cands,
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "cited_by": w.get("cited_by_count") or 0,
        "abstract": abstract,
        "type": w.get("type") or "",
        "via": "",
        "in_library": False,
    }


def _norm_arxiv(entry):
    eid = entry.findtext("a:id", "", _NS) or ""
    short = eid.rsplit("/abs/", 1)[-1]
    pdf = ""
    for link in entry.findall("a:link", _NS):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf = link.get("href") or ""
    published = entry.findtext("a:published", "", _NS) or ""
    year = int(published[:4]) if published[:4].isdigit() else None
    return {
        "uid": "arxiv:" + short,
        "source": "arxiv",
        "also": [],
        "title": re.sub(r"\s+", " ", entry.findtext("a:title", "", _NS) or "").strip(),
        "authors": [a.findtext("a:name", "", _NS) or "" for a in entry.findall("a:author", _NS)][:12],
        "year": year,
        "venue": "arXiv",
        "venue_type": "repository",
        "doi": (entry.findtext("arxiv:doi", "", _NS) or "").strip(),
        "url": eid,
        "oa_url": pdf,
        "oa_urls": [pdf] if pdf else [],
        "is_oa": True,
        "cited_by": 0,
        "abstract": re.sub(r"\s+", " ", entry.findtext("a:summary", "", _NS) or "").strip()[:ABSTRACT_MAX],
        "type": "preprint",
        "via": "",
        "in_library": False,
    }


# ---------- 单源检索 ----------

def search_openalex(query, since=None, per_page=100, sort=None):
    """OpenAlex 检索。query 走 title_and_abstract.search（比全文字段精确）。

    per_page 默认 100（官方上限）—— 计费按请求次数，与条数无关，所以取满最划算。
    """
    expr = (query or "").strip()
    if not expr:
        return []
    flt = f"title_and_abstract.search:{expr}"
    if since:
        flt += f",from_publication_date:{since}"
    params = {"filter": flt, "per-page": str(max(1, min(int(per_page), 200)))}
    if sort:
        params["sort"] = sort
    return [_norm_openalex(w) for w in _oa_works(params)]


_TOK_RE = re.compile(r'"[^"]*"|\(|\)|\bANDNOT\b|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+', re.I)


def to_arxiv_query(expr, since=None):
    """把同一条检索式翻译成 arXiv 语法。

    arXiv 的字段前缀检索（all:）必须逐词加前缀，且不支持把整个布尔表达式
    当一个短语——所以要先按括号/引号/布尔算子切词，再逐词还原结构。
    相邻的操作数（无算子连接）按 AND 处理，与 OpenAlex 的语义保持一致。
    """
    src = (expr or "").strip()
    if not src:
        return ""
    out, prev_operand = [], False
    for tok in _TOK_RE.findall(src):
        if tok in ("(", ")"):
            if tok == "(" and prev_operand:
                out.append("AND")
            out.append(tok)
            prev_operand = tok == ")"
            continue
        up = tok.upper()
        if up in ("AND", "OR", "ANDNOT", "NOT"):
            out.append("ANDNOT" if up == "NOT" else up)
            prev_operand = False
            continue
        if prev_operand:
            out.append("AND")
        if len(tok) > 1 and tok.startswith('"') and tok.endswith('"'):
            inner = tok[1:-1].strip()
            if inner:
                out.append(f'all:"{inner}"')
        else:
            out.append(f'all:"{tok}"' if " " in tok else f"all:{tok}")
        prev_operand = True
    body = " ".join(x for x in out if x)
    if not body:
        return ""
    if since:
        d = re.sub(r"[^0-9]", "", since)
        if len(d) == 4:
            d += "0101"
        elif len(d) == 6:
            d += "01"
        if len(d) != 8:
            d = "19000101"
        body = f"({body}) AND submittedDate:[{d}0000 TO 999912312359]"
    return body


def search_arxiv(query, since=None, max_results=25):
    """arXiv 检索，返回 Atom 解析后的记录。"""
    q = to_arxiv_query(query, since=since)
    if not q:
        return []
    params = {"search_query": q, "start": "0",
              "max_results": str(max(1, min(int(max_results), 100))),
              "sortBy": "relevance", "sortOrder": "descending"}
    root = ET.fromstring(_http(ARXIV_BASE + "?" + urllib.parse.urlencode(params)))
    return [_norm_arxiv(e) for e in root.findall("a:entry", _NS)]


# ---------- 去重合并 ----------

def _title_key(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def _merge(dst, src):
    if src["source"] not in ([dst["source"]] + dst["also"]):
        dst["also"].append(src["source"])
    for k in ("oa_url", "doi", "venue", "abstract"):
        if not dst.get(k) and src.get(k):
            dst[k] = src[k]
    # 候选链接要合并，不是"缺了才补"：两个源各自找到的开放位置都值得留着试
    merged = list(dst.get("oa_urls") or [])
    if dst.get("oa_url") and dst["oa_url"] not in merged:
        merged.insert(0, dst["oa_url"])
    for u in (src.get("oa_urls") or ([src["oa_url"]] if src.get("oa_url") else [])):
        if u and u not in merged:
            merged.append(u)
    if merged:
        dst["oa_urls"] = merged
        dst.setdefault("oa_url", merged[0])
    dst["cited_by"] = max(dst.get("cited_by") or 0, src.get("cited_by") or 0)
    dst["is_oa"] = bool(dst.get("is_oa") or src.get("is_oa"))
    if src.get("uid", "").startswith("openalex:") and not dst["uid"].startswith("openalex:"):
        dst["uid"] = src["uid"]          # 优先留 OpenAlex id（引文追踪要用）
    if src.get("via") and src["via"] not in (dst.get("via") or ""):
        dst["via"] = (dst.get("via") + " | " + src["via"]) if dst.get("via") else src["via"]


def dedupe(recs):
    """跨源去重：DOI 精确匹配优先，标题归一化兜底。"""
    out, by_doi, by_title = [], {}, {}
    for r in recs:
        if not (r.get("title") or "").strip():
            continue
        doi = (r.get("doi") or "").strip().lower()
        tk = _title_key(r["title"])
        hit = (by_doi.get(doi) if doi else None) or (by_title.get(tk) if tk else None)
        if hit is not None:
            _merge(hit, r)
            continue
        out.append(r)
        if doi:
            by_doi[doi] = r
        if tk:
            by_title[tk] = r
    return out


# ---------- 编排 ----------

SORT_MODES = {
    "relevance": None,                     # OpenAlex 默认相关度排序
    "citations": "cited_by_count:desc",
    "date": "publication_date:desc",
}


SRC_LABEL = {"openalex": "OpenAlex", "arxiv": "arXiv"}


# 各源的最小请求间隔（秒）。arXiv 官方 API 指南明确要求"每 3 秒不超过 1 次请求"，
# 而 OpenAlex 是每秒 100 次的宽松限制 —— 用同一个间隔要么拖慢 OpenAlex，要么撞死 arXiv。
SRC_GAP = {"openalex": 0.5, "arxiv": 3.0}


def run_search(queries, since=None, sources=("openalex", "arxiv"), per_page=100,
               sort="relevance", log=None, gap=None):
    """跑一轮完整检索：多检索式 × 多源 → 去重 → 排序。

    返回 (records, errors, status)。status 每个源带 hits / failed / reason / skipped，
    失败时 failed=True —— **上层必须把 failed 显式告知用户**，否则会静默少一半结果。

    限流对策：按源留间隔（见 SRC_GAP：arXiv 3s / OpenAlex 0.5s，各自的官方要求不同）；
    遇 429 立即熔断该源，不再对剩下的检索式硬撞；其它错误连续两条也熔断。
    """
    if sort not in SORT_MODES:
        sort = "relevance"
    since = normalize_since(since) or None
    oa_sort = SORT_MODES[sort]

    qs = [x.strip() for x in (queries or []) if x and x.strip()]
    status = {s: {"hits": 0, "failed": False, "reason": "", "skipped": 0} for s in sources}
    live = {s: True for s in sources}
    recs, errors = [], []

    def _call(src, q):
        if src == "openalex":
            return search_openalex(q, since=since, per_page=per_page, sort=oa_sort)
        return search_arxiv(q, since=since, max_results=per_page)

    for qi, q in enumerate(qs):
        for src in sources:
            if not live[src]:
                status[src]["skipped"] += 1
                continue
            wait = gap if isinstance(gap, (int, float)) else SRC_GAP.get(src, 0.5)
            if qi and wait:
                time.sleep(wait)                # 检索式之间按源留间隔，别连发
            try:
                got = _call(src, q)
                recs += [dict(r, via=q) for r in got]
                status[src]["hits"] += len(got)
                status[src]["fails"] = 0
                if log:
                    log(f"{SRC_LABEL[src]}「{q[:40]}」命中 {len(got)} 条")
            except RateLimited as e:
                if e.retry_after and e.retry_after > 120:
                    until = time.strftime("%m-%d %H:%M",
                                          time.localtime(time.time() + e.retry_after))
                    wait = f"（额度预计 {until} 前后恢复）"
                elif e.retry_after:
                    wait = f"（约 {e.retry_after} 秒后恢复）"
                else:
                    wait = "（稍后再试）"
                live[src] = False
                status[src].update(failed=True,
                                   reason=f"请求额度用尽被限流{wait}，本次已停止该源后续查询")
                errors.append(f"{SRC_LABEL[src]} 被限流，本次其余检索式未再查询该源")
                if log:
                    log(f"{SRC_LABEL[src]} ← 限流，已熔断{wait}")
            except ApiKeyInvalid:
                live[src] = False
                status[src].update(
                    failed=True,
                    reason="API Key 无效（401）—— 请到「设置」核对或清空 OpenAlex API Key"
                           "（清空后自动回退匿名额度）")
                errors.append(f"{SRC_LABEL[src]} API Key 无效，本次不再查询该源")
                if log:
                    log(f"{SRC_LABEL[src]} ← API Key 无效，已熔断")
            except Exception as e:
                n = status[src].get("fails", 0) + 1
                status[src]["fails"] = n
                status[src].update(failed=True, reason=f"查询失败：{repr(e)[:100]}")
                errors.append(f"{SRC_LABEL[src]}「{q[:40]}」失败：{repr(e)[:120]}")
                if n >= 2:                      # 连续两条失败 → 熔断，别把剩下的都撞一遍
                    live[src] = False
                    status[src]["reason"] += "（连续失败，已停止该源后续查询）"

    for s in status:
        status[s].pop("fails", None)

    merged = dedupe(recs)
    if sort == "citations":
        merged.sort(key=lambda r: (-(r.get("cited_by") or 0), -(r.get("year") or 0)))
    elif sort == "date":
        merged.sort(key=lambda r: (-(r.get("year") or 0), -(r.get("cited_by") or 0)))
    # relevance 模式保留检索顺序：OpenAlex 已按相关度给出，再去重合并 arXiv
    return merged, errors, status


# ---------- 引文追踪 ----------

def resolve_openalex_id(rec):
    """取记录对应的 OpenAlex work id：优先自带 id，否则用 DOI 反查。"""
    uid = rec.get("uid") or ""
    if uid.startswith("openalex:"):
        return uid.split(":", 1)[1] or None
    doi = (rec.get("doi") or "").strip()
    if not doi:
        return None
    try:
        data = json.loads(_http(_oa_url(f"/works/https://doi.org/{urllib.parse.quote(doi)}")))
    except Exception:
        return None
    return (data.get("id") or "").rsplit("/", 1)[-1] or None


def expand_citations(rec, direction="citing", limit=50, since=None):
    """引文追踪。direction=citing 找引用它的后续工作；referenced 找它的参考文献。"""
    wid = resolve_openalex_id(rec)
    if not wid:
        raise ValueError("该条没有 DOI、也不在 OpenAlex 中，无法做引文追踪")
    since = normalize_since(since) or None
    n = max(1, min(int(limit), 200))
    if direction == "citing":
        flt = f"cites:{wid}"
        if since:
            flt += f",from_publication_date:{since}"
        return [_norm_openalex(w) for w in
                _oa_works({"filter": flt, "per-page": str(n), "sort": "cited_by_count:desc"})]
    refs = (json.loads(_http(_oa_url(f"/works/{wid}"))).get("referenced_works") or [])[:n]
    if not refs:
        return []
    ids = [r.rsplit("/", 1)[-1] for r in refs if r]
    if not ids:
        return []
    out = [_norm_openalex(w) for w in
           _oa_works({"filter": "openalex_id:" + "|".join(ids), "per-page": str(len(ids))})]
    out.sort(key=lambda r: -(r.get("cited_by") or 0))
    return out


# ---------- 库内查重 ----------

def mark_in_library(recs):
    """标注哪些已在 Zotero 库中（DOI 精确匹配优先，标题归一化兜底）。查不到库时不标注。"""
    try:
        from core import zotero_io
        idx = zotero_io.library_index()
    except Exception:
        return recs
    dois, titles = idx["dois"], idx["titles"]
    for r in recs:
        doi = (r.get("doi") or "").strip().lower()
        tk = _title_key(r.get("title"))
        r["in_library"] = bool((doi and doi in dois) or (tk and tk in titles))
    return recs


# ---------- 主题存取（config/topics/<slug>.json，用户数据不出库） ----------

def _slugify(s):
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", (s or "").strip()).strip("-").lower()
    return s or "topic"


def topics_dir():
    os.makedirs(TOPICS_DIR, exist_ok=True)
    return TOPICS_DIR


def list_topics():
    out = []
    for f in sorted(os.listdir(topics_dir())):
        if not f.endswith(".json"):
            continue
        try:
            t = json.load(open(os.path.join(topics_dir(), f), encoding="utf-8"))
        except Exception:
            continue
        t["slug"] = f[:-5]
        out.append(t)
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def save_topic(topic):
    """保存/更新一个检索主题。同名覆盖。返回落盘后的主题。"""
    import datetime
    name = (topic.get("name") or "").strip()
    if not name:
        raise ValueError("主题名不能为空")
    slug = (topic.get("slug") or _slugify(name)).strip()
    rec = {
        "name": name,
        "slug": slug,
        "queries": [q.strip() for q in (topic.get("queries") or []) if q and q.strip()],
        "since": (topic.get("since") or "").strip(),
        "sources": [s for s in (topic.get("sources") or ["openalex"]) if s in ("openalex", "arxiv")],
        "sort": topic.get("sort") if topic.get("sort") in SORT_MODES else "relevance",
        "per_page": int(topic.get("per_page") or 100),
        "collection": (topic.get("collection") or "").strip(),
        "tags": [t.strip() for t in (topic.get("tags") or []) if t and t.strip()],
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(topics_dir(), slug + ".json"), "w", encoding="utf-8") as w:
        json.dump(rec, w, ensure_ascii=False, indent=2)
    return rec


def delete_topic(slug):
    p = os.path.join(topics_dir(), _slugify(slug) + ".json")
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


# ---------- OA 全文抓取：三级递进 ----------
#
# ① 直链：把候选链接当 PDF 直接 GET。arxiv / aclanthology / AAAI 这类真直链一步到手。
# ② 落地页解析：拿回来是网页时，读这张网页**自己声明**的 PDF 地址
#    （citation_pdf_url / 页内 .pdf 链接）。doi.org 系列多属此类——你手动点开能下全文，
#    程序却只看到一张网页，旧版就在这里直接放弃了。
# ③ 浏览器兜底：被反爬拦下（HTTP 403）时，用无头浏览器先过一次挑战、落下 cookie 再要文件。
#    实测：**光换浏览器 UA 没用**（MDPI / ACM / OUP 依旧 403，它们看的是指纹），必须真浏览器。
#    它慢，所以**只在明确被拒时才动用**，不做无差别尝试。
#
# 每篇都记下"卡在哪一级、为什么"，写进入库清单，方便判断哪些值得手动补。

_PDF_MAX_MB = 40
_PDF_SOCK_TIMEOUT = 20
_PDF_TOTAL_TIMEOUT = 90


def _hdrs(referer=""):
    h = dict(_PDF_HEADERS)
    h["User-Agent"] = BROWSER_UA
    if referer:
        h["Referer"] = referer
    return h


def _rm(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _dest(dest_dir, url):
    """给下载文件起个安全的名字（带链接指纹，避免不同论文同名互相覆盖）。"""
    os.makedirs(dest_dir, exist_ok=True)
    base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    base = re.sub(r"[^0-9A-Za-z._-]+", "_", base)[:70]
    if not base:
        base = "paper"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    tag = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return os.path.join(dest_dir, f"{tag}_{base}")


def download_pdf(url, dest_dir, timeout=_PDF_SOCK_TIMEOUT, max_mb=_PDF_MAX_MB,
                 total_timeout=_PDF_TOTAL_TIMEOUT, referer=""):
    """①级：把 url 当 PDF 直接 GET。返回 (本地路径, 原因)，成功时原因为 ""。

    原因里的 "HTTP 403" 是**可重试**信号（表示该上浏览器兜底），其余基本可以直接放弃。
    ⚠ `timeout` 只管每次 socket 读写，另有 `total_timeout` 限制总时长——服务端只要每 20 秒
    吐一点字节，就能把整条任务无限拖住（踩过：347 条卡在一篇上，把服务整个拖死）。
    """
    if not url:
        return None, "无 OA 链接"
    deadline = time.time() + total_timeout
    try:
        req = urllib.request.Request(url, headers=_hdrs(referer))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = r.read(4096)
            if not (head.startswith(b"%PDF") or "application/pdf" in ctype):
                return None, "网页而非 PDF"
            path = _dest(dest_dir, url)
            total = len(head)
            with open(path, "wb") as w:
                w.write(head)
                while True:
                    if time.time() > deadline:
                        w.close()
                        _rm(path)
                        return None, f"下载超过 {total_timeout} 秒"
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_mb * 1024 * 1024:
                        w.close()
                        _rm(path)
                        return None, f"超过 {max_mb}MB"
                    w.write(chunk)
        return path, ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, repr(e)[:90]


def _pdf_url_in_html(html, base_url):
    """从网页 HTML 里找 PDF 地址。返回 (url, 依据) 或 (None, "")。

    先认学术网站通用的 `<meta name="citation_pdf_url">`（这一条最靠得住），
    再退到页面里第一个 `.pdf` 链接。
    """
    for pat in (
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']',
        r'<link[^>]+type=["\']application/pdf["\'][^>]*href=["\']([^"\']+)',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return urllib.parse.urljoin(base_url, m.group(1).strip()), "网页声明的 PDF 地址"
    m = re.search(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html, re.I)
    if m:
        return urllib.parse.urljoin(base_url, m.group(1).strip()), "页内 .pdf 链接"
    return None, ""


def page_pdf_url(url, timeout=15):
    """②级：打开网页，找它自己声明的 PDF 地址。返回 (pdf_url, 依据) 或 (None, 原因)。"""
    try:
        req = urllib.request.Request(url, headers=_hdrs())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(400 * 1024)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, repr(e)[:60]
    found, _how = _pdf_url_in_html(raw.decode("utf-8", "ignore"), url)
    return (found, "ok") if found else (None, "网页里找不到 PDF 地址")


def browser_pdf(url, dest_dir, timeout=45, max_mb=_PDF_MAX_MB, log=None):
    """③级：浏览器兜底，只给被反爬拦下（403）的站点用。

    思路是"先过门、再要货"：用真浏览器把目标地址打开一次（过掉风控挑战、落下 cookie），
    再用**同一个浏览器上下文**去要文件。纯 HTTP 客户端模仿浏览器是过不去的。
    """
    try:
        from . import webchat as _wc
        _wc._apply_browser_path()           # 复用网页端那套内核路径配置
    except Exception:
        pass
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, f"没有可用的浏览器内核（{repr(e)[:50]}）"

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            try:
                ctx = b.new_context(user_agent=BROWSER_UA, accept_downloads=True, locale="en-US")
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except Exception:
                    pass                    # 目标是 PDF 时导航会"变成下载"，属正常，忽略
                cur = url
                try:
                    cur = page.url or url
                except Exception:
                    pass
                extra = ""
                try:
                    html = page.content() or ""
                except Exception:
                    html = ""
                if html and not html.lstrip().startswith("%PDF"):
                    found, _how = _pdf_url_in_html(html, cur)
                    # 只在确实换到了另一个地址时才改道——有些页面声明的 PDF 地址就是它自己，
                    # 改过去等于原地打转（Emerald 就是这样）。
                    if found and found not in (url, cur):
                        cur, extra = found, "（在页面里找到了 PDF 地址）"
                resp = ctx.request.get(cur, timeout=timeout * 1000, headers={"Referer": url})
                if resp.ok:
                    body = resp.body()
                    if body[:4] == b"%PDF":
                        path = _dest(dest_dir, cur)
                        with open(path, "wb") as w:
                            w.write(body[:max_mb * 1024 * 1024])
                        return path, extra
                    return None, f"浏览器取回的也不是 PDF{extra}"
                return None, f"浏览器也被拒（HTTP {resp.status}）{extra}"
            finally:
                try:
                    b.close()
                except Exception:
                    pass
    except Exception as e:
        return None, f"浏览器抓取失败：{repr(e)[:70]}"


def _why_text(note):
    """把内部的失败标记翻成用户看得懂的一句话（写进入库清单）。"""
    n = note or ""
    if not n:
        return "没有可用的开放获取链接"
    if "HTTP 403" in n:
        return "被出版商反爬拦下（403）"
    if "网页而非 PDF" in n:
        return "拿到的都是网页，页面里没有公开的 PDF 地址"
    if "找不到 PDF" in n:
        return "落地页里没有公开的 PDF 地址"
    if "浏览器也被拒" in n or "浏览器取回" in n:
        return "反爬或付费墙，浏览器也拿不到"
    return n


def fetch_fulltext(rec, dest_dir, allow_browser=True, log=None):
    """抓一篇文献的开放全文（三级递进）。返回：

    {ok, path, how, reason, final_url, tried}
      how ∈ "直链" / "落地页解析" / "浏览器兜底"；失败时 reason 给用户看得懂的原因。

    注意命名：**不能叫 `fetch_pdf`**——`import_records()` 的形参就叫 `fetch_pdf`（bool），
    同名会被参数遮蔽，调用时直接 TypeError。
    """
    cands = [u for u in (rec.get("oa_urls") or []) if u]
    if not cands and rec.get("oa_url"):
        cands = [rec["oa_url"]]
    if not cands:
        return {"ok": False, "path": None, "how": "", "final_url": "", "tried": 0,
                "reason": "OpenAlex 没给开放获取链接（多半没有公开版本）"}

    tried, blocked, last = [], False, ""

    # ① 直接当 PDF 下
    for u in cands[:4]:
        tried.append(u)
        path, why = download_pdf(u, dest_dir)
        if path:
            return {"ok": True, "path": path, "how": "直链", "final_url": u,
                    "reason": "", "tried": len(tried)}
        last = why
        if why.startswith("HTTP 403"):
            blocked = True

    # ② 当成网页，读它自己声明的 PDF 地址
    for u in cands[:3]:
        pdf_url, note = page_pdf_url(u)
        if not pdf_url:
            last = note
            if note.startswith("HTTP 403"):
                blocked = True
            continue
        tried.append(pdf_url)
        path, why = download_pdf(pdf_url, dest_dir, referer=u)
        if path:
            return {"ok": True, "path": path, "how": "落地页解析", "final_url": pdf_url,
                    "reason": "", "tried": len(tried)}
        # 页面明明声明了 PDF 地址、拿回来却是网页 —— 典型的"要订阅/登录"。
        # 这句必须写清楚，否则用户会以为是我们没找着地址（Emerald 实测就是这样）。
        last = ("页面声明的 PDF 地址实际返回网页（多半需要订阅或登录）"
                if why == "网页而非 PDF" else why)
        if why.startswith("HTTP 403"):
            blocked = True

    # ③ 只在"被明确拒绝"时才动用浏览器——它慢，不做无差别尝试
    if allow_browser and blocked:
        for u in cands[:2]:
            tried.append(u)
            path, why = browser_pdf(u, dest_dir, log=log)
            if path:
                return {"ok": True, "path": path, "how": "浏览器兜底", "final_url": u,
                        "reason": "", "tried": len(tried)}
            last = why
        return {"ok": False, "path": None, "how": "", "final_url": "", "tried": len(tried),
                "reason": "反爬或付费墙：" + _why_text(last)}

    return {"ok": False, "path": None, "how": "", "final_url": "", "tried": len(tried),
            "reason": _why_text(last)}


# ---------- 检索式生成（LLM） ----------

_GEN_SYSTEM = (
    "你是科技文献检索专家，熟悉 OpenAlex 与 arXiv 的布尔检索语法。"
    "你只输出检索式本身，不输出任何解释、编号或 Markdown 标记。"
)

_GEN_PROMPT = """研究方向：{topic}

请写出 {n} 条英文布尔检索式，用于 OpenAlex 的 title_and_abstract.search（同一条会被翻译成 arXiv 语法复用）。

要求：
1. 一条一行，直接输出检索式本身——不要编号、不要项目符号、不要解释、不要空行。
2. 支持 AND / OR / NOT；多词短语用英文双引号包起来。
3. 每条都要含该方向公认的英文专有短语作为必需项，不要用过于宽泛的单词（如 model、method、data、study）。
4. {n} 条各聚焦不同侧面（例如方法、任务、评测、应用场景），彼此独立、合起来覆盖该方向；
   不要把全部内容 OR 成一条超长式，也不要写成彼此几乎相同的样子。
5. 只用英文。"""


def _clean_queries(text, n, strict=True):
    """把模型输出整理成检索式列表。

    模型习惯在前后加寒暄、编号、项目符号、代码围栏，所以逐行清洗：
      1. 反复剥掉叠加的编号与项目符号（如 "2. - xxx"、"* 3) xxx"）；
      2. 去掉行首尾的反引号 / 围栏；
      3. strict 模式下只保留"像检索式"的行——含双引号短语或布尔算子；
         实测模型爱写 "Here are the queries:" 这类引导句，不放行会污染检索式。
         strict=False 是兜底：宁松勿空，避免过滤过狠导致一条都不剩。
    """
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("```"):
            continue
        for _ in range(3):                               # 编号与项目符号可能叠加
            s2 = re.sub(r"^\d+\s*[.)、:：]\s*", "", s)
            s2 = re.sub(r"^[-*\u2022]\s*", "", s2)
            if s2 == s:
                break
            s = s2
        s = s.strip().strip("`").strip()
        if not s or not re.search(r"[A-Za-z]", s):        # 丢掉纯中文/空的残行
            continue
        if strict and not (('"' in s) or re.search(r"\b(ANDNOT|AND|OR|NOT)\b", s, re.I)):
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= max(1, int(n)):
            break
    return out


def _html_to_text(h):
    """网页端给的是回答 HTML 片段，转成纯文本再交给 _clean_queries（与 domain.py 同做法）。"""
    import html as _html
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", h or "", flags=re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)


def generate_queries(topic, count=4, backend="webchat", log=None):
    """按研究方向生成布尔检索式。

    **默认走网页端**（`WebChatTextSession`：驱动已登录的浏览器会话，**零 API 费用**，
    会打开一个浏览器窗口）；只有显式传 `backend="api"` 才走「设置」里的模型 API。
    与 `domain.py` 的造包、`longdoc.py` 的长文精读保持同一套做法。

    只产出文本：不落盘、不检索、不改 Zotero。失败时抛异常，由调用方转成可读提示。
    """
    say = log or (lambda m: None)
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("请先填写研究方向")
    n = max(2, min(int(count or 4), 8))
    prompt = _GEN_PROMPT.format(topic=topic, n=n)
    mode = (backend or "webchat").strip().lower()

    if mode == "api":
        from .read import _llm_chat          # 延迟导入：本模块其余功能不依赖 openai
        say("正在通过模型 API 生成检索式…")
        text = _llm_chat(prompt, system=_GEN_SYSTEM, timeout=90)
    else:
        from .webchat import WebChatTextSession
        say("正在通过网页端生成检索式（零 API 费用；会打开一个浏览器窗口自动操作，请勿关闭它）…")
        s = WebChatTextSession(headless=False, log=say)
        s.open()
        try:
            text = _html_to_text(s.ask(prompt, timeout=600))
        finally:
            try:
                s.close()
            except Exception:
                pass

    qs = _clean_queries(text, n)
    return qs or _clean_queries(text, n, strict=False)


# ---------- 入库 ----------

def _csv_cell(v):
    return '"' + str("" if v is None else v).replace('"', '""') + '"'


def write_import_manifest(rows, collection=""):
    """把一次入库的结果写成 CSV 清单，落到 reports/。返回文件名；写不成返回 ""。

    这份清单是给**人**看的：哪些进了库、哪些抓到了全文、没抓到的是卡在哪一级、为什么。
    用 utf-8-sig（带 BOM），Excel 双击打开中文不乱码。
    """
    if not rows:
        return ""
    try:
        from . import tools as _tools
        d = _tools.reports_dir()
        os.makedirs(d, exist_ok=True)
    except Exception:
        return ""
    head = ["标题", "年份", "作者", "来源", "DOI", "结果", "PDF", "取到方式",
            "未抓到原因", "尝试次数", "最终链接", "入库分类"]
    keys = ("title", "year", "authors", "source", "doi", "status", "pdf", "pdf_how",
            "pdf_reason", "pdf_tried", "pdf_final_url", "collection")
    lines = [",".join(_csv_cell(h) for h in head)]
    lines += [",".join(_csv_cell(r.get(k)) for k in keys) for r in rows]
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", (collection or "未分类")).strip("_")[:24] or "未分类"
    name = f"文献入库_{safe}_{time.strftime('%Y-%m-%d_%H%M')}.csv"
    try:
        with open(os.path.join(d, name), "w", encoding="utf-8-sig", newline="") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except Exception:
        return ""
    return name


def import_records(records, collection="", tags=None, fetch_pdf=False, log=None,
                   allow_browser=True):
    """把选中的检索结果写进 Zotero：建条目 →（可选）抓 OA 全文挂附件 →（可选）进分类。

    只写调用方明确传入的记录；不碰库里已有内容。
    `fetch_pdf=True` 时按「直链 → 落地页解析 → 浏览器兜底」三级抓全文（见 `fetch_fulltext`），
    跑完在 reports/ 落一份 CSV 清单，逐条写明抓到没有、卡在哪一级、为什么。
    """
    import tempfile
    from core import zotero_io

    tags = [t for t in (tags or []) if t and t.strip()]
    out = {"ok": True, "created": 0, "skipped": 0, "pdf_ok": 0, "pdf_fail": 0,
           "pdf_by": {"直链": 0, "落地页解析": 0, "浏览器兜底": 0}, "items": [], "manifest": ""}
    rows = []          # 清单行：含跳过的，比 out["items"] 更全

    def note(rec, status, pdf=None, attempted=True):
        if not fetch_pdf or not attempted:
            pdf_cell = "未尝试"
        elif pdf and pdf.get("ok"):
            pdf_cell = "已抓到"
        else:
            pdf_cell = "未抓到"
        rows.append({
            "title": (rec.get("title") or "").strip()[:140],
            "year": rec.get("year") or "",
            "authors": "; ".join((rec.get("authors") or [])[:4]),
            "source": "arXiv" if rec.get("source") == "arxiv" else "OpenAlex",
            "doi": rec.get("doi") or "",
            "status": status,
            "pdf": pdf_cell,
            "pdf_how": (pdf or {}).get("how") or "",
            "pdf_reason": (pdf or {}).get("reason") or "",
            "pdf_tried": (pdf or {}).get("tried") or 0,
            "pdf_final_url": (pdf or {}).get("final_url") or "",
            "collection": (collection or "").strip(),
        })

    col_key = None
    if (collection or "").strip():
        try:
            col_key = zotero_io.ensure_collection(collection.strip())
            out["collection"] = {"name": collection.strip(), "key": col_key}
            if log:
                log(f"目标分类：{collection.strip()}（{col_key}）")
        except Exception as e:
            hint = zotero_io.write_error_hint(e)
            return {"ok": False,
                    "error": f"分类准备失败：{repr(e)[:150]}" + (f"\n\n{hint}" if hint else "")}

    idx = {"dois": set(), "titles": set()}
    try:
        idx = zotero_io.library_index()
    except Exception:
        pass

    pdf_dir = os.path.join(tempfile.gettempdir(), "lf_litsearch_pdf")
    for rec in records or []:
        title = (rec.get("title") or "").strip()
        if not title:
            out["skipped"] += 1
            note(rec, "无标题，跳过", attempted=False)
            continue
        doi = (rec.get("doi") or "").strip().lower()
        tk = _title_key(title)
        if (doi and doi in idx["dois"]) or (tk and tk in idx["titles"]):
            out["skipped"] += 1
            out["items"].append({"title": title[:70], "status": "已在库，跳过"})
            note(rec, "已在库，跳过", attempted=False)
            if log:
                log(f"跳过（已在库）：{title[:50]}")
            continue
        try:
            key = zotero_io.create_item_from_meta(
                rec, collection_keys=[col_key] if col_key else None, tags=tags)
        except Exception as e:
            hint = zotero_io.write_error_hint(e)
            if hint and not out.get("hint"):
                out["hint"] = hint          # 鉴权类错误只提示一次，不刷屏
            out["skipped"] += 1
            out["items"].append({"title": title[:70], "status": f"建条目失败：{repr(e)[:80]}"})
            note(rec, f"建条目失败：{repr(e)[:80]}", attempted=False)
            if hint:
                out["ok"] = False
                break                       # 权限问题后面每条都会失败，直接停
            continue
        out["created"] += 1
        if doi:
            idx["dois"].add(doi)
        if tk:
            idx["titles"].add(tk)
        row = {"title": title[:70], "key": key, "status": "已入库"}
        pdf = None
        if fetch_pdf:
            pdf = fetch_fulltext(rec, pdf_dir, allow_browser=allow_browser, log=log)
            if pdf.get("ok"):
                r = zotero_io.attach_pdf(key, pdf["path"], filename=os.path.basename(pdf["path"]))
                _rm(pdf["path"])
                if r.get("ok"):
                    out["pdf_ok"] += 1
                    out["pdf_by"][pdf["how"]] = out["pdf_by"].get(pdf["how"], 0) + 1
                    row["status"] += f" + 全文（{pdf['how']}）"
                else:
                    out["pdf_fail"] += 1
                    row["status"] += f"（全文挂载失败：{r.get('error')}）"
                    pdf = dict(pdf, ok=False, reason=f"挂载失败：{r.get('error')}")
            else:
                out["pdf_fail"] += 1
                row["status"] += f"（未抓到全文：{pdf['reason']}）"
        out["items"].append(row)
        note(rec, row["status"], pdf)
        if log:
            log(f"✓ {row['status']}：{title[:50]}")
    if fetch_pdf:
        out["manifest"] = write_import_manifest(rows, collection)
        if out["manifest"] and log:
            log(f"入库清单：reports/{out['manifest']}")
    return out
