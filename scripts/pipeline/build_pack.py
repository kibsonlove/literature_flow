# -*- coding: utf-8 -*-
"""生成论文写作素材包：每篇笔记的浓缩版（标题/年份/作者/标签/开头1200字）+ 参考文献列表"""
import sys, re, json, os, traceback

# 项目根 = 本脚本（scripts/pipeline/）的上两级；webapp 与输出都相对它定位
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "webapp"))

OUT_MD = os.path.join(_ROOT, "notes", "material_pack.md")
OUT_REFS = os.path.join(_ROOT, "archive", "refs_list.md")

def fmt_refs(authors, title, date):
    year = (date or "")[:4] or "n.d."
    a = "、".join(authors[:3]) + ("等" if len(authors) > 3 else "")
    return f"{a}. {title}. {year}."

def main():
    # 输出目录可能尚不存在（notes/、archive/ 内容不入库），先建好
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    os.makedirs(os.path.dirname(OUT_REFS), exist_ok=True)
    from core.zotero_io import _client
    z = _client()
    print("step1: notes", flush=True)
    notes = [n for n in z.items(itemType="note", limit=500)
             if ("六维速览" in (n["data"].get("note") or "") or "六维精读" in (n["data"].get("note") or ""))]
    all_items = z.items(limit=500)
    by_key = {it["data"].get("key"): it["data"] for it in all_items}
    print("step2: indexed", len(by_key), flush=True)

    lines = ["# 论文素材包（每篇笔记浓缩）", ""]
    refs = []
    for n in notes:
        d = n["data"]
        p = by_key.get(d.get("parentItem")) or {}
        title = p.get("title", "(无题)")
        date = p.get("date", "")
        authors = []
        for c in p.get("creators", [])[:5]:
            nm = c.get("name") or (str(c.get("lastName", "")) + str(c.get("firstName", "")))
            if nm: authors.append(nm)
        tags = [t["tag"] for t in p.get("tags", [])][:10]
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d.get("note") or "")).strip()
        lines.append(f"## {title}")
        lines.append(f"- 年份: {date[:10]} | 作者: {'、'.join(authors[:3])} | 标签: {' '.join(tags)}")
        lines.append(f"- 内容: {text[:1200]}")
        lines.append("")
        refs.append(fmt_refs(authors, title, date))

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))
    open(OUT_REFS, "w", encoding="utf-8").write("# 参考文献原始列表\n\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(refs)))
    print("step3: done", len(refs), "refs", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        _err_dir = os.path.join(_ROOT, "archive")
        os.makedirs(_err_dir, exist_ok=True)
        open(os.path.join(_err_dir, "pack_error.txt"), "w", encoding="utf-8").write(traceback.format_exc())
        raise
