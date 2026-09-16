# -*- coding: utf-8 -*-
"""文献订阅：按关键词查 OpenAlex（免费无 Key），输出新文献候选清单。
用法：python scripts/subscribe/lit_watch.py  → 生成 reports/文献订阅_候选清单.md
配置：关键词写在 lit_keywords.txt（每行一个，本地文件不入库）；SINCE 改这里。
建议配合 Windows 计划任务定期运行。"""
import json, urllib.request, urllib.parse, datetime, os, re

# 从任意工作目录运行都有效：配置文件取脚本所在目录，输出取项目根的 reports/
_BASE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_BASE))

def _load_keywords():
    try:
        kws = [l.strip() for l in open(os.path.join(_BASE, "lit_keywords.txt"), encoding="utf-8") if l.strip()]
        if kws:
            return kws
    except Exception:
        pass
    return ["your keyword one", "your keyword two"]  # 占位：请在 lit_keywords.txt 填写

KEYWORDS = _load_keywords()
SINCE = "2024-01-01"          # 只看此日期之后发表的
PER_PAGE = 15
# 重点期刊（本地文件 lit_journals.txt，每行一条正则；命中者在清单中标 ⭐）
def _load_focus():
    try:
        pats = [l.strip() for l in open(os.path.join(_BASE, "lit_journals.txt"), encoding="utf-8") if l.strip() and not l.startswith("#")]
        if pats:
            return pats
    except Exception:
        pass
    return []

JOURNAL_FOCUS = _load_focus()
OUT = os.path.join(_ROOT, "reports", "文献订阅_候选清单.md")
MAIL = ""                     # OpenAlex 建议留邮箱（ polite pool ），可留空

def fetch(keyword):
    params = {
        "search": keyword,
        "filter": f"from_publication_date:{SINCE}",
        "per-page": str(PER_PAGE),
        "sort": "publication_date:desc",
    }
    if MAIL:
        params["mailto"] = MAIL
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "lit-watch/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("results", [])

def fmt(w):
    title = (w.get("title") or "(无标题)").strip()
    year = w.get("publication_year", "")
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
    authors = ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])[:3])
    oa = "OA可下载" if w.get("open_access", {}).get("is_oa") else "付费墙"
    cited = w.get("cited_by_count", 0)
    pub = (w.get("publication_date") or str(year))
    star = "⭐" if any(re.search(p, src, re.I) for p in JOURNAL_FOCUS) else ""
    return f"{star}{title}", pub, doi, src, authors, oa, cited

def main():
    seen, blocks = set(), []
    for kw in KEYWORDS:
        try:
            results = fetch(kw)
        except Exception as e:
            blocks.append(f"## 关键词：{kw}\n\n❌ 查询失败：{e}\n")
            continue
        rows = []
        for w in results:
            title, pub, doi, src, authors, oa, cited = fmt(w)
            key = (title or doi).lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                f"| {pub} | {title[:70]} | {authors[:40]} | {src[:28]} | {oa} | {cited} |"
                + (f" doi:{doi}" if doi else " |")
            )
        blocks.append(
            f"## 关键词：{kw}\n\n命中 {len(rows)} 条（{SINCE} 之后）\n\n"
            + "| 日期 | 标题 | 作者 | 期刊 | 获取 | 被引 | DOI |\n|---|---|---|---|---|---|---|\n"
            + "\n".join(rows) + "\n"
        )
    head = (
        f"# 文献订阅候选清单\n\n> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"> 关键词：{len(KEYWORDS)} 组 | 去重后共 {len(seen)} 条\n\n"
        "> 使用说明：逐条浏览，需要的 → 在 Zotero 中用「通过标识符添加」（粘贴 DOI）入库；"
        "标 OA 的可直接从出版社页下载 PDF。入库存放建议：先入「导入」分类，读完再移动。\n\n"
    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(head + "\n".join(blocks))
    print("saved:", OUT, "| unique:", len(seen))

if __name__ == "__main__":
    main()
