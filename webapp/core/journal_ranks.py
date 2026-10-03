# -*- coding: utf-8 -*-
"""期刊分区：把用户自备的**授权数据**（中科院 / JCR / 新锐）当本地映射用起来。

数据源 = `webapp/config/journal_ranks.csv`，由用户提供的 xlsx（四张表）合并而来，
**属授权数据，只留本机、不入库**。

为什么要做归一：四张源表的刊名写法各不相同——
  新锐  去空格紧凑   `NatureCommunications`
  JCR   全大写      `CA-A CANCER JOURNAL FOR CLINICIANS`
  中科院 正常空格     `Academy of Management Annals`（大小写混排）
所以统一 key = 去掉所有非字母数字并转大写；能拿到 ISSN 时**优先用 ISSN 精确匹配**
（刊名只是兜底——去掉空格后仍有极小概率撞名，见下方 `_AMBIGUOUS_NOTE`）。

用法：
    from core import journal_ranks as JR
    r = JR.lookup(name="Nature Communications", issn="2041-1723")
    JR.summary(r)          # '中科院1区 · 新锐1区 · JCR Q1 · IF 15.7'
    JR.attach(records)     # 给一批检索结果逐条挂上 rank / rank_label
"""
import csv
import os
import re
import threading

# 对外暴露的分区字段（也决定展示顺序）
FIELDS = ("新锐分区", "新锐TOP", "新锐学科", "中科院2025", "中科院2023", "中科院TOP",
          "中科院OA", "JCR分区", "JIF", "JIF百分位", "JCR学科")

_AMBIGUOUS_NOTE = (
    "合并时已做过消歧：同一张源表内「去掉空格后相同」但原始刊名不同的记录（实测 18 组，"
    "如 Science & Education 与 SCIENCE EDUCATION 是两本不同的刊）不按刊名合并，只按 ISSN。"
)

_LOCK = threading.Lock()
_CACHE = {"mtime": None, "by_name": {}, "by_issn": {}, "rows": []}


def path():
    """分区表文件路径（一律走 config 的函数，别 import 模块级常量）。"""
    from .config import journal_ranks_path
    return journal_ranks_path()


def available():
    """本机有没有这份表。没有时所有 lookup 都会返回空 dict，调用方按"没有分区数据"处理即可。"""
    return os.path.isfile(path())


def norm_name(s):
    """刊名 key：去掉所有非字母数字 + 转大写。

    这样 `NatureCommunications` / `Nature Communications` / `NATURE COMMUNICATIONS`
    会落到同一个 key 上——三套源表的写法差异就靠这一步抹平。
    """
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def norm_issn(s):
    v = re.sub(r"[^0-9Xx]", "", str(s or "")).upper()
    return v if len(v) == 8 else ""


def _load():
    """按 mtime 缓存整表（23k 行，解析一次约 0.2 秒，别每次查都重读）。"""
    p = path()
    if not os.path.isfile(p):
        return
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return
    with _LOCK:
        if _CACHE["mtime"] == mt:
            return
        by_name, by_issn, rows = {}, {}, []
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
                k = norm_name(r.get("刊名"))
                if k:
                    by_name.setdefault(k, r)
                for fld in ("ISSN", "eISSN"):
                    i = norm_issn(r.get(fld))
                    if i:
                        by_issn.setdefault(i, r)
        _CACHE.update({"mtime": mt, "by_name": by_name, "by_issn": by_issn, "rows": rows})


def _brief(row):
    return {k: row.get(k, "") for k in FIELDS if (row.get(k) or "").strip()}


def lookup(name="", issn=""):
    """查一本刊的分区。返回只含非空字段的 dict；查不到返回 {}（不是 None，调用方少一层判断）。

    先按 ISSN（可靠），再按刊名 key（兜底）。
    """
    _load()
    i = norm_issn(issn)
    if i:
        hit = _CACHE["by_issn"].get(i)
        if hit:
            return _brief(hit)
    k = norm_name(name)
    if k:
        hit = _CACHE["by_name"].get(k)
        if hit:
            return _brief(hit)
    # 有些刊名带着尾注（如 'Proceedings of the ACM on software engineering.'）
    # 或缩写括号，去掉后再试一次
    k2 = norm_name(re.sub(r"\([^)]*\)", "", str(name or "")))
    if k2 and k2 != k:
        hit = _CACHE["by_name"].get(k2)
        if hit:
            return _brief(hit)
    return {}


def summary(rank):
    """一行给人看的分区摘要：『中科院1区 · 新锐1区 · JCR Q1 · IF 9.4』。"""
    if not rank:
        return ""
    bits = []
    if rank.get("中科院2025"):
        bits.append("中科院%s区" % rank["中科院2025"])
    if rank.get("新锐分区"):
        bits.append("新锐%s" % rank["新锐分区"])
    if rank.get("JCR分区"):
        bits.append("JCR %s" % rank["JCR分区"])
    if rank.get("JIF"):
        bits.append("IF %s" % rank["JIF"])
    return " · ".join(bits)


def top_label(rank):
    """给表格用的一小格：'1区/Q1' 这种。"""
    if not rank:
        return ""
    cas = ("%s区" % rank["中科院2025"]) if rank.get("中科院2025") else ""
    jcr = rank.get("JCR分区") or ""
    return "/".join([x for x in (cas, jcr) if x])


def cas_tier(rank):
    """中科院分区取成整数（1-4）；取不到返回 0。用于排序 / 筛选。"""
    try:
        return int(re.sub(r"\D", "", str((rank or {}).get("中科院2025") or "")) or 0)
    except Exception:
        return 0


def attach(records, log=None):
    """给一批检索结果逐条挂 `rank` / `rank_label`。原地改并返回，方便链式调用。"""
    n = 0
    for r in records or []:
        rk = lookup(name=r.get("venue"), issn=r.get("issn"))
        r["rank"] = rk
        r["rank_label"] = summary(rk)
        if rk:
            n += 1
    if log and records:
        log("分区匹配：%d/%d 条命中（repository/会议类本来就不在期刊分区表里，属正常）"
            % (n, len(records)))
    return records


def stats():
    """表的基本情况（设置页 / 体检用）。"""
    _load()
    rows = _CACHE["rows"]
    return {"available": bool(rows), "path": path(), "entries": len(rows),
            "with_issn": sum(1 for r in rows if norm_issn(r.get("ISSN"))),
            "all_four": sum(1 for r in rows if r.get("来源") == "cas+cmp+jcr+xr"),
            "note": _AMBIGUOUS_NOTE}
