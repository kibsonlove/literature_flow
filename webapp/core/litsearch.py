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
import json
import os
import re
import time
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


def _norm_openalex(w):
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    best = w.get("best_oa_location") or {}
    oa_url = (best.get("pdf_url") or best.get("landing_page_url")
              or (w.get("open_access") or {}).get("oa_url") or "")
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
        "oa_url": oa_url or "",
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


# ---------- OA 全文下载 ----------

def download_pdf(url, dest_dir, timeout=20, max_mb=40, total_timeout=90):
    """下载开放获取 PDF。返回本地路径；非 PDF 内容或失败返回 (None, 原因)。

    ⚠ `timeout` 只作用于**每次 socket 读写**，不限制总时长：服务端只要每 20 秒吐一点数据，
    就能把这一步无限拖住。踩过的坑：一次 347 条的入库卡在某篇 PDF 上，
    整条任务从 20:25 起再无任何动作、界面看着像死了。所以这里额外加**总时长上限**
    （`total_timeout`），到点就放弃这一篇继续下一篇。
    """
    if not url:
        return None, "无 OA 链接"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    deadline = time.time() + total_timeout
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(1024)
            if not head.startswith(b"%PDF"):
                return None, "链接不是 PDF（可能是落地页）"
            os.makedirs(dest_dir, exist_ok=True)
            name = re.sub(r"[^0-9A-Za-z._-]+", "_", url.rsplit("/", 1)[-1])[:80] or "paper.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            path = os.path.join(dest_dir, name)
            total = len(head)
            with open(path, "wb") as w:
                w.write(head)
                while True:
                    if time.time() > deadline:
                        w.close()
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                        return None, f"下载超过 {total_timeout} 秒，已跳过"
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_mb * 1024 * 1024:
                        w.close()
                        os.remove(path)
                        return None, f"超过 {max_mb}MB 上限"
                    w.write(chunk)
        return path, ""
    except Exception as e:
        return None, repr(e)[:120]


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

def import_records(records, collection="", tags=None, fetch_pdf=False, log=None):
    """把选中的检索结果写进 Zotero：建条目 →（可选）抓 OA PDF 挂附件 →（可选）进分类。

    只写调用方明确传入的记录；不碰库里已有内容。
    """
    import tempfile
    from core import zotero_io

    tags = [t for t in (tags or []) if t and t.strip()]
    out = {"ok": True, "created": 0, "skipped": 0, "pdf_ok": 0, "pdf_fail": 0, "items": []}

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
            continue
        doi = (rec.get("doi") or "").strip().lower()
        tk = _title_key(title)
        if (doi and doi in idx["dois"]) or (tk and tk in idx["titles"]):
            out["skipped"] += 1
            out["items"].append({"title": title[:70], "status": "已在库，跳过"})
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
        if fetch_pdf and rec.get("oa_url"):
            path, why = download_pdf(rec["oa_url"], pdf_dir)
            if path:
                r = zotero_io.attach_pdf(key, path, filename=os.path.basename(path))
                try:
                    os.remove(path)
                except Exception:
                    pass
                if r.get("ok"):
                    out["pdf_ok"] += 1
                    row["status"] += " + PDF"
                else:
                    out["pdf_fail"] += 1
                    row["status"] += f"（PDF 挂载失败：{r.get('error')}）"
            else:
                out["pdf_fail"] += 1
                row["status"] += f"（无 PDF：{why}）"
        out["items"].append(row)
        if log:
            log(f"✓ {row['status']}：{title[:50]}")
    return out
