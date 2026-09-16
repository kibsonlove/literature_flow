# -*- coding: utf-8 -*-
"""领域包（domain pack）：精读提示词与界面文案全部由配置驱动，代码零领域绑定。

目录结构：
    config/domain.json          当前激活的领域包（向后兼容；不存在时用库中第一个）
    config/domains/<name>.json  领域包库（多领域可复用）

领域包字段（缺失时用通用默认值）：
    name          包标识（slug）
    label         展示名，如「植物考古（六维）」
    domain        领域身份描述（拼入助手身份句）
    app_title     webapp 顶栏标题
    dimensions    证据维度名列表
    sites_md / dim_detail_md / dialogue_md / tags_md  章节模板片段
    keywords      领域关键词（用于自动匹配文献）
    auto_generated 是否由模型自动生成（便于人工审阅）
    created       生成时间
"""
import json
import os
import re
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.normpath(os.path.join(_DIR, "..", "config"))
DOMAIN_JSON = os.path.join(CONFIG_DIR, "domain.json")       # 激活包
DOMAIN_DIR = os.path.join(CONFIG_DIR, "domains")            # 领域包库

_GENERIC = {
    "name": "generic",
    "label": "通用维度",
    "domain": "学术文献",
    "app_title": "literature_flow",
    "dimensions": ["对象", "方法", "材料", "核心发现", "量化", "对话"],
    "sites_md": "## 3. 研究对象背景（可简表）\n| 对象 | 属性 | 规模/性质 |\n|---|---|---|",
    "dim_detail_md": "## 4. 领域核心维度（中等）\n- 本领域最关键的判定/发现及其判据：\n- 若文章未涉及，明确写出\"本文未涉及\"。",
    "dialogue_md": "## 6. 学术对话（重点，具名文献与大叙事立场）\n- 支持 / 挑战了哪些既有观点：\n- 对本领域主流大叙事的具体立场：",
    "tags_md": "（按领域自定义标签维度，如对象/年代/方法/地区）\n- 维度一：\n- 维度二：\n- 维度三：\n- 维度四：",
    "keywords": [],
}

# ---------------------------------------------------------------- 读取

def _read_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None

def _fill(cfg):
    d = dict(_GENERIC)
    d.update({k: v for k, v in (cfg or {}).items() if v})
    return d

def list_domains():
    """列出领域包库里所有包（含激活包）。"""
    os.makedirs(DOMAIN_DIR, exist_ok=True)
    out = []
    seen = set()
    act = _read_json(DOMAIN_JSON)
    if act:
        name = act.get("name") or "active"
        out.append({"name": name, "label": act.get("label") or act.get("domain") or name,
                    "active": True, "auto_generated": bool(act.get("auto_generated"))})
        seen.add(name)
    for fn in sorted(os.listdir(DOMAIN_DIR)):
        if not fn.endswith(".json"):
            continue
        cfg = _read_json(os.path.join(DOMAIN_DIR, fn))
        if not cfg:
            continue
        name = cfg.get("name") or fn[:-5]
        if name in seen:
            continue
        out.append({"name": name, "label": cfg.get("label") or cfg.get("domain") or name,
                    "active": False, "auto_generated": bool(cfg.get("auto_generated"))})
    return out

def load_domain(name=None):
    """加载领域包：name=None 用激活包；找不到则回退通用维度。"""
    if not name:
        cfg = _read_json(DOMAIN_JSON)
        if cfg:
            return _fill(cfg)
        # 激活包缺失 → 用库里第一个
        for fn in sorted(os.listdir(DOMAIN_DIR)) if os.path.isdir(DOMAIN_DIR) else []:
            if fn.endswith(".json"):
                cfg = _read_json(os.path.join(DOMAIN_DIR, fn))
                if cfg:
                    return _fill(cfg)
        return _fill(None)
    for path in (os.path.join(DOMAIN_DIR, f"{name}.json"), os.path.join(DOMAIN_DIR, name)):
        cfg = _read_json(path)
        if cfg:
            return _fill(cfg)
    return _fill(None)

def save_domain(cfg, set_active=False):
    """保存领域包到库（可选设为激活）。返回包名。"""
    os.makedirs(DOMAIN_DIR, exist_ok=True)
    name = cfg.get("name") or re.sub(r"[^a-z0-9_-]", "", (cfg.get("domain") or "domain").lower()[:24]) or f"pack{int(time.time())}"
    cfg["name"] = name
    cfg.setdefault("created", time.strftime("%Y-%m-%d %H:%M"))
    json.dump(cfg, open(os.path.join(DOMAIN_DIR, f"{name}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if set_active:
        json.dump(cfg, open(DOMAIN_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return name

# ---------------------------------------------------------------- 匹配

def match_domain(text, threshold=2):
    """用文献标题/正文片段匹配领域包：命中关键词数最高者胜出。

    返回 (包名, 得分)；全部低于阈值则返回 (None, 最高分)。
    """
    t = (text or "").lower()
    if not t.strip():
        return None, 0
    best, best_score = None, 0
    for info in list_domains():
        cfg = load_domain(info["name"])
        kws = [k.lower() for k in (cfg.get("keywords") or [])]
        # 领域名/维度名本身也算弱信号
        kws += [w.lower() for w in re.split(r"[（(]/", str(cfg.get("label") or ""))[:1] if w]
        score = sum(1 for k in kws if k and k in t)
        if score > best_score:
            best, best_score = info["name"], score
    return (best if best_score >= threshold else None), best_score

# ---------------------------------------------------------------- 自动造包

_PROMPT = """下面是一篇学术文献的标题与正文开头。请判断它属于哪个研究领域，并为该领域设计一套「精读维度」。

要求：
1. 维度 4–6 个，彼此不重叠，是**该领域判断一篇论文好坏/贡献的证据骨架**；
2. 每个维度给一句判据（读这篇论文时该看什么）；
3. 标签体系给出 4 组维度名（如 对象/材料/年代地区/方法），每组给 6–10 个示例标签；
4. 输出**严格 JSON**，不要解释、不要代码块围栏，字段如下：
{
 "name": "领域 slug（英文小写连字符）",
 "label": "领域展示名（中文，含维度数，如 陶瓷工艺研究（五维））",
 "domain": "领域身份描述（用于'你是一位…文献的精读助手'这句）",
 "dimensions": ["维一","维二","维三","维四"],
 "dim_detail_md": "## 4. 领域核心维度（中等）\\n- 维一：判据…\\n- 维二：判据…",
 "sites_md": "## 3. 研究背景（可简表）\\n| 对象 | 属性 | 规模/性质 |\\n|---|---|---|",
 "dialogue_md": "## 6. 学术对话（重点，具名文献与大叙事立场）\\n- 支持 / 挑战了哪些既有观点：\\n- 对该领域主流叙事的具体立场：",
 "tags_md": "（四组示例标签）\\n- 组一：…\\n- 组二：…",
 "keywords": ["用于自动匹配该领域的关键词", "中英文都要有", "8-15 个"]
}

文献标题：__TITLE__

正文开头：
__SAMPLE__
"""

def _html_to_text(h):
    """把网页端回答的 HTML 片段剥成纯文本（提取 JSON 用）。"""
    import html as _html
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", h or "", flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|tr|h[1-6]|pre|table)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)

def _domain_backend():
    """领域包生成后端：settings.json 可选 domain_backend=api|webchat；
    缺省 auto——配了 API key 走 API，否则走网页端（零 API 费）。"""
    try:
        from .config import load_settings
        s = load_settings()
    except Exception:
        s = {}
    b = (s.get("domain_backend") or "auto").strip().lower()
    if b in ("api", "webchat"):
        return b
    return "api" if s.get("has_key") else "webchat"

def generate_domain_pack(title, sample, log=lambda m: None, backend=None):
    """调用 LLM 生成领域包。backend=api（settings.json 里的 API）或
    webchat（网页端 LLM，零 API 费，复用已登录的 DeepSeek 浏览器）。
    缺省按 _domain_backend() 自动选。返回 cfg 或抛异常。"""
    backend = (backend or _domain_backend()).lower()
    prompt = _PROMPT.replace("__TITLE__", title[:200]).replace("__SAMPLE__", (sample or "")[:2500])
    if backend == "webchat":
        log("领域未识别，正在通过网页端 LLM（零 API 费）生成新领域包…")
        from .webchat import WebChatTextSession
        s = WebChatTextSession(headless=False, log=log)
        s.open()
        try:
            raw = _html_to_text(s.ask(prompt))
        finally:
            s.close()
    else:
        from .read import _llm_chat          # 复用 settings.json 里的 API 配置
        log("领域未识别，正在让模型生成新的领域包…")
        raw = _llm_chat(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise RuntimeError("模型未返回 JSON")
    cfg = json.loads(m.group(0))
    for k in ("name", "label", "domain", "dimensions", "dim_detail_md"):
        if not cfg.get(k):
            raise RuntimeError(f"领域包缺少字段 {k}")
    cfg["auto_generated"] = True
    cfg["created"] = time.strftime("%Y-%m-%d %H:%M")
    log(f"已生成领域包：{cfg['label']}（维度 {'/'.join(cfg['dimensions'])}）")
    return cfg

def pick_domain(title, sample, auto_generate=True, log=lambda m: None):
    """为一篇文献挑领域包：匹配 → 无则自动生成并落盘 → 兜底通用。

    返回 (包名, cfg, 来源说明)
    """
    text = f"{title}\n{sample or ''}"[:4000]
    name, score = match_domain(text)
    if name:
        log(f"领域匹配：{name}（命中 {score} 个关键词）")
        return name, load_domain(name), f"匹配已有包（{score} 个关键词）"
    if auto_generate:
        try:
            cfg = generate_domain_pack(title, sample, log=log)
            name = save_domain(cfg, set_active=False)
            return name, load_domain(name), "自动生成新包"
        except Exception as e:
            log(f"自动造包失败（{repr(e)[:80]}），回退激活包")
    active = _read_json(DOMAIN_JSON)
    if active:
        nm = active.get("name") or "active"
        return nm, load_domain(None), "未匹配到领域→激活包"
    return None, _fill(None), "通用维度兜底"

# ---------------------------------------------------------------- 提示词

def app_title():
    return load_domain()["app_title"]

def system_prompt(domain_name=None):
    d = load_domain(domain_name)
    dim_rows = "\n".join(f"| {x} | |" for x in d["dimensions"])
    return f"""你是一位{d['domain']}文献的精读助手。\
你的任务是为一篇论文产出结构化精读笔记，重心放在"文章得出了什么结论、怎么论证的"，而非平铺方法数字。

请严格按以下 Markdown 模板输出（用中文）：

# <标题>（<年份>）
- Zotero key:（未知则留空） | PDF:（未知则留空）
- 期刊/出处： | DOI：

## 维度速览（供 paper-set 批量比对）
| 维度 | 一句话 |
|---|---|
{dim_rows}

## 核心结论（3–5 条，最重要；短文可 2 条）
1.
2.
3.

## 讨论要点（本文最值得记的论证）
- 改变了什么认识：
- 机制：
- 分寸（有限修正 / 全盘否定 / 留作未定）：
- 对话对象（本文想说服谁 / 立场多强）：

## 1. 材料与证据（从简，除非直接支撑结论）
- 证据类型：
- 样本量 / 出处（一行带过，明细回原页）：
- 年代与测年/断代方法（区分"直接测年" vs "据背景推定"）：

## 2. 鉴定与分析方法（从简，重点写可信度限制）
- 方法：
- 可信度限制：

{d['sites_md']}

{d['dim_detail_md']}

## 5. 量化数据（只列支撑结论的关键数字）
- 仅保留支撑核心结论/讨论的数字，逐条标注页码（如 [p3]）。
- 涉及表格的数字若全文未给出精确值，写"见原页 Table/Figure，需回看核对"，不要编造。

{d['dialogue_md']}

## 与我课题的相关性
- 暂无真实课题（未指定研究问题，略去针对性相关性分析）。

## 建议标签
{d['tags_md']}

写作要求：
- 重点展开核心结论、讨论要点、学术对话的分寸；中等篇幅写领域核心维度与研究对象背景；从简或省略不支撑结论的明细数据。
- 量化数据只在支撑结论时引用并标页码；表格数字若全文未给精确值，必须写"需回看核对"，绝不可编造全文没有的数字。
- 页码用 [pN] 表示 PDF 第 N 页。
- "与我课题的相关性"固定写上面的占位行（用户暂无论题）。
"""
