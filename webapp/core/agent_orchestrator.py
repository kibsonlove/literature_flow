# -*- coding: utf-8 -*-
"""智能体编排器（P1）：自然语言任务 → 计划 → 确定性执行 → 危险步骤挂起确认 → 总结。

形态：**粗粒度规划**（见 `docs/智能体化改造方案.md` 第 1 节）。
    开头一次 LLM 出计划 → 中间由确定性代码逐步执行 → 只有**失败**时才回问 LLM
    （每任务最多 2 次）→ 结束一次 LLM 总结。正常路径 2 次 LLM 调用。
    之所以不做「逐步决策紧循环」：网页端 LLM 通道每次要开浏览器、约 30 秒+，开销不可承受。

安全边界（硬约束，见方案第 3 节）：
    - 写库工具（`agent_tools` 里 danger=True）**一律挂起**，等用户在面板上确认，agent 无静默写权限。
    - 任务状态落盘 `<数据目录>/agent_tasks/<id>.json`（可再生），支持关掉页面后续跑。
    - LLM 通道默认走网页端 webchat（零 API 费用），`backend="api"` 才用「设置」里的模型。

对外接口：
    create_task(text, backend, log)   —— 只生成计划，不执行（阻塞，内部调 LLM）
    advance(task_id, log)             —— 执行到「下一个危险闸口」或结束（阻塞）
    approve(task_id, step_id, log)    —— 放行某个危险步骤并继续
    skip_step(task_id, step_id, log)  —— 跳过某一步并继续
    abort(task_id)                    —— 中止任务
    get_task(task_id) / list_tasks()  —— 查询
"""
import json
import os
import re
import threading
import time

from . import agent_tools as A

MAX_STEPS = 12          # 单任务步骤上限（含回炉追加），防跑飞
MAX_RECOVER = 2         # 失败回问 LLM 的次数上限
MAX_TASKS_KEEP = 40     # agent_tasks/ 里保留的任务文件数（超出删最旧）

_LOCK = threading.RLock()
_TASK_LOCKS = {}
_LOG_TAIL = 400


# ---------------------------------------------------------------- 状态落盘

def _tasks_dir():
    from .config import data_path
    d = data_path("agent_tasks")
    os.makedirs(d, exist_ok=True)
    return d


def _task_file(task_id):
    return os.path.join(_tasks_dir(), f"{re.sub(r'[^0-9a-zA-Z_-]', '', task_id or '')}.json")


def _lock_for(task_id):
    with _LOCK:
        if task_id not in _TASK_LOCKS:
            _TASK_LOCKS[task_id] = threading.RLock()
        return _TASK_LOCKS[task_id]


def _save(t):
    """原子写任务文件。

    ⚠ Windows 上 `os.replace` 会因**并发访问**（前端正轮询 GET 同一个 .json、或杀软扫描）
    报 `WinError 5 拒绝访问`，导致执行中途崩掉。所以这里：① 只在持有该任务锁时调用；
    ② 仍对 OSError 做重试；③ 最后兜底直接覆写，宁可非原子也不能丢进度。
    """
    t["updated_at"] = _now()
    p = _task_file(t["id"])
    tmp = p + ".tmp"
    data = json.dumps(t, ensure_ascii=False, indent=2)
    for i in range(4):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, p)
            return t
        except OSError:
            time.sleep(0.15 * (i + 1))
    try:
        with open(p, "w", encoding="utf-8") as f:      # 兜底：原地覆写
            f.write(data)
    except OSError:
        pass
    return t


def get_task(task_id):
    """读任务。与 `_save` 共用该任务的锁，避免"边写边读"在 Windows 上互相锁死。"""
    p = _task_file(task_id)
    with _lock_for(task_id):
        if not os.path.isfile(p):
            return None
        for i in range(3):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                time.sleep(0.1 * (i + 1))
        return None


def list_tasks(limit=20):
    _reap_stale()          # 顺手把"卡死"的任务标成失败，别让它们永远停在 planning
    d = _tasks_dir()
    rows = []
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                t = json.load(f)
            rows.append({"id": t.get("id"), "task": (t.get("task") or "")[:120],
                         "status": t.get("status"), "created_at": t.get("created_at"),
                         "updated_at": t.get("updated_at"),
                         "steps_total": len(t.get("steps") or []),
                         "steps_done": sum(1 for s in (t.get("steps") or [])
                                           if s.get("status") in ("done", "skipped"))})
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:max(1, int(limit or 20))]


def _prune():
    d = _tasks_dir()
    files = []
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            p = os.path.join(d, fn)
            files.append((os.path.getmtime(p), p))
    files.sort(reverse=True)
    for _, p in files[MAX_TASKS_KEEP:]:
        try:
            os.remove(p)
        except Exception:
            pass


def reap_inflight_on_startup(stale_min=5):
    """服务启动时调用：把明显"无主"的任务标成失败。

    刚起来的进程里不可能有执行线程，所以还停在 planning / running 的任务，
    多半是上次进程被关掉时留下的孤儿。但**不能无条件回收**——同一份 cache 有可能
    被另一个进程同时用着（排查时会另起一个端口），那种任务其实还活着。
    因此加一道保险：只有**超过 stale_min 分钟没有任何更新**的才动手。
    （正常执行会频繁刷任务文件：每个步骤边界都存盘，长步骤也有日志限频落盘。）

    不处理的话，这些孤儿会永远显示"正在生成计划…/执行中"，用户没有任何出口。
    """
    n = 0
    for fn in os.listdir(_tasks_dir()):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(_tasks_dir(), fn)
        try:
            with open(p, encoding="utf-8") as f:
                t = json.load(f)
        except Exception:
            continue
        if t.get("status") not in ("planning", "running"):
            continue
        if _age_min(t) < stale_min:
            continue
        with _lock_for(t["id"]):
            for s in t.get("steps") or []:
                if s.get("status") == "running":
                    s["status"] = "pending"
                    s["summary"] = "服务重启导致中断，未确认是否完成；继续执行会重跑这一步"
            t["status"] = "failed"
            t["error"] = ("这件任务在上次服务关闭时中断了（后台线程已不在）。"
                          "点「继续执行」可以从断点接着跑，或点「重新生成计划」重排。")
            _log(t, t["error"])
            _save(t)
        n += 1
    return n


def _age_min(t):
    ts = t.get("updated_at") or t.get("created_at") or ""
    try:
        return (time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) / 60.0
    except Exception:
        return 0.0


def _reap_stale(plan_min=10, run_min=30):
    """回收"卡死"的任务，避免它们永远停在「正在生成计划…」这种没有出口的状态。

    什么算卡死：状态是 planning / running，但**没有任何执行线程在跑**（不在 `_RUNNING` 里），
    且已经过去足够久。踩过的坑：前端连点「生成计划」，被 409 挡掉的那几次
    仍然在磁盘上留下了任务记录，于是界面上永远转圈、用户以为"点了没反应"。

    只改状态、不删记录——用户能看到失败原因，自己决定「重新生成计划」还是删掉。
    """
    with _LOCK:
        running = set(_RUNNING)
    n = 0
    for fn in os.listdir(_tasks_dir()):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(_tasks_dir(), fn)
        try:
            with open(p, encoding="utf-8") as f:
                t = json.load(f)
        except Exception:
            continue
        if (t.get("id") or "") in running:
            continue
        st, age = t.get("status"), _age_min(t)
        reason = None
        if st == "planning" and age > plan_min:
            reason = ("生成计划时中断了（多半是被并发提交挡掉、或浏览器通道卡住）。"
                      "点「重新生成计划」可以重试。")
        elif st == "running" and age > run_min:
            reason = "任务状态异常中断（后台执行线程已经不在了），请重新生成计划后重试。"
        if reason:
            with _lock_for(t["id"]):
                t["status"] = "failed"
                t["error"] = reason
                _log(t, reason)
                _save(t)
            n += 1
    return n


# ---------------------------------------------------------------- 小工具

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uid():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + format(int(time.time() * 1000) % 1000, "03d")


def _log(t, msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    t.setdefault("log", []).append(line)
    if len(t["log"]) > _LOG_TAIL:
        t["log"] = t["log"][-int(_LOG_TAIL / 2):]
    return line


def _html_to_text(h):
    import html as _html
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", h or "", flags=re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)


def _strip_fence(s):
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _first_json_object(text):
    """从模型输出里抠出第一个完整的 JSON 对象（容忍前后寒暄、代码围栏）。"""
    s = _strip_fence(text)
    i = s.find("{")
    while i != -1:
        depth, in_str, esc = 0, False, False
        for j in range(i, len(s)):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[i:j + 1])
                    except Exception:
                        break
        i = s.find("{", i + 1)
    return None


def _llm_text(prompt, backend="webchat", log=None, timeout=420, system=None):
    """统一的 LLM 文本通道。默认网页端（零 API 费用），backend="api" 才走 API。"""
    say = log or (lambda m: None)
    backend = (backend or "webchat").strip().lower()
    if backend == "api":
        from .read import _llm_chat
        say("正在通过模型 API 生成…")
        return _llm_chat(prompt, system=system or _SYS, timeout=timeout)
    from .webchat import WebChatTextSession
    say("正在通过网页端生成（零 API 费用；会打开一个浏览器窗口自动操作，请勿关闭它）…")
    s = WebChatTextSession(headless=False, log=say)
    s.open()
    try:
        return _html_to_text(s.ask(prompt, timeout=timeout))
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 计划提示词

_SYS = ("你是 literature_flow 文献管线的任务调度器。你只做一件事：把用户的自然语言任务拆成"
        "一份可以按顺序执行的步骤计划，并只输出一个 JSON 对象。不要解释、不要客套。")

_PLAN_PROMPT = """把下面这个任务拆成一份执行计划。你只能使用给定的工具，不要编造工具名或参数。

【用户任务】
{task}

【调用原则】
1. 步骤控制在 1-4 步，够用就行，不要为了凑步骤而拆；也不要写"检查/思考/等待"这类没有工具可执行的步骤。
2. 标了【写库·需用户确认】的工具会暂停等用户点确认，只在确实需要时用，且一次任务最多出现一次。
3. 需要把上一步结果传给下一步时，必须用引用，**不要留空、也不要自己编内容**：
   - 引用写法是 `$s1.字段名`——**开头的 `$` 不能漏**，字段名必须取自下面每个工具的
     「输出字段」清单（清单里没有的字段一律不存在）。
   - 例如：`"queries": "$s1.queries"`、`"records": "$s2.results"`。
   - 从列表里抽出某个字段：`"keys": "$s1.items.key"`（把 items 里每个元素的 key 抽成字符串列表）。
   - 只允许引用**排在前面**的步骤。
   - **反例（会被打回）**：写成 `"queries": "s1.queries"`（漏 `$`）、`"records": "s2.results"`（漏 `$`）、
     或该引用上一步却写成 `"queries": []`（留空导致这一步必然失败）。
4. 参数值必须是字面量或上述 `$sN` 引用，不要写表达式。必填参数一律不能省。
   确实不知道填什么的可选参数，就**整个省略**，不要给空字符串或空数组。
5. **入库要设上限**：检索动辄几百条，用户多半不想要全部。用 `lit_import` 时给 `max` 填一个合理值
   （没特别说明就给 50），并把 `fetch_pdf` 只在你判断用户确实要全文时才开——每篇都要下载，很慢。
6. 写库步骤的参数要能一眼看懂"会发生什么"。别把检索式、条数这类细节留空让用户自己猜。

【用户的研究课题】（用于理解任务背景，可能为空）
{research}

【可用工具】
{spec}

【输出格式】
只输出一个 JSON 对象，不要代码围栏、不要多余文字：
{{"note": "一句话说明整体思路", "steps": [{{"title": "中文短标题", "tool": "工具名", "args": {{...}}, "why": "这一步做什么"}}]}}

【示例】
任务：找一些"可解释人工智能 × 数据可视化"2023 年以来的新论文，存进 Zotero 的「XAI」分类
输出：
{{"note": "先根据方向生成检索式，再检索，最后经确认后入库。", "steps": [
  {{"title": "生成检索式", "tool": "lit_generate_queries", "args": {{"topic": "explainable AI 与数据可视化、人机交互的交叉", "count": 4}}, "why": "得到覆盖不同侧面的布尔检索式"}},
  {{"title": "检索文献", "tool": "lit_search", "args": {{"queries": "$s1.queries", "since": "2023", "per_page": 50}}, "why": "在 OpenAlex / arXiv 找候选"}},
  {{"title": "入库到 XAI 分类", "tool": "lit_import", "args": {{"records": "$s2.results", "max": 50, "collection": "XAI", "fetch_pdf": false}}, "why": "取前 50 条写进 Zotero（会等用户确认）"}}
]}}"""

_PLAN_FIX = """

【上一次的计划有下面这些问题，请重新出一份完整计划修正它们】
{issues}

注意：输出的仍然是一份**完整**计划（同样的 JSON 格式，包含所有步骤），不是只给改动的那几步。
选填参数如果确实不知道就整个省略；需要上一步结果的参数必须写成 `$sN.字段名`（别漏 `$`、别留空）。"""

_RECOVER_PROMPT = """任务执行中某一步失败了，请判断如何补救。

【用户任务】
{task}

【已完成的步骤】
{done}

【失败的步骤】
第 {idx} 步「{title}」（工具 {tool}）
参数：{args}
错误：{error}

【可用工具】
{spec}

请只输出一个 JSON 对象：
- 有可行的补救办法：{{"note": "说明", "steps": [{{"title": "...", "tool": "...", "args": {{...}}, "why": "..."}}]}}
  只写**还没做的事**，已经成功的不必重来；最多 2 步。
- 无法补救：{{"note": "说明原因", "steps": []}}
不要代码围栏、不要多余文字。"""

_SUM_PROMPT = """把下面这次自动化任务的结果，用中文写成 3-6 行的简短总结给用户。

【任务】{task}
【步骤与结果】
{steps}

要求：说清楚做了什么、结果如何（关键数字要写出来）、有没有需要用户注意的地方。
直接给总结正文，不要标题、不要客套、不要 Markdown 表格。"""


# ---------------------------------------------------------------- 引用解析

_REF_RE = re.compile(r"^\$s(\d+)(?:\.(.+))?$")
# 兜底：模型漏写 `$` 时的写法（`s2.results`）。要求**必须带点**，
# 免得把一个普通字面量（比如某个分类名就叫 "s2"）误当成引用。
_REF_RE_LOOSE = re.compile(r"^s(\d+)\.(.+)$")


def _nav(obj, parts):
    """按路径取值；路径段遇到列表时，对每个元素应用剩余路径（用于 $s1.items.key）。"""
    if not parts:
        return obj
    if isinstance(obj, list):
        return [_nav(el, parts) for el in obj]
    if isinstance(obj, dict):
        if parts[0] not in obj:
            return None
        return _nav(obj.get(parts[0]), parts[1:])
    return None


def _resolve_refs(value, steps, seen=None):
    """把 args 里的 "$sN.xxx" 引用替换成前面步骤的实际输出。

    引用不到时抛 ValueError——宁可让这一步明确失败、报出可读原因，
    也不要静默传 None 进去（那会让后面的工具给出莫名其妙的报错）。
    """
    if isinstance(value, str):
        m = _REF_RE.match(value.strip()) or _REF_RE_LOOSE.match(value.strip())
        if not m:
            return value
        n = int(m.group(1))
        path = (m.group(2) or "").strip()
        if n < 1 or n > len(steps):
            raise ValueError(f"引用 $s{n} 不存在（计划里只有 {len(steps)} 步）")
        st = steps[n - 1]
        if st.get("status") not in ("done", "skipped"):
            raise ValueError(f"引用 $s{n} 时该步还没执行成功（当前状态：{st.get('status')}）")
        data = st.get("result")
        out = _nav(data, path.split(".")) if path else data
        if out is None:
            raise ValueError(f"引用 $s{n}.{path or '(整体)'} 取不到值")
        return out
    if isinstance(value, list):
        return [_resolve_refs(v, steps, seen) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_refs(v, steps, seen) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- 计划

def _normalize_steps(raw, task, already):
    """校验并归一模型给的步骤；不合规的直接丢掉（不猜、不补）。"""
    out = []
    seq = already
    for s in (raw or []):
        if not isinstance(s, dict):
            continue
        tool = (s.get("tool") or "").strip()
        spec = A.get(tool)
        if not spec:
            continue
        args = s.get("args")
        if not isinstance(args, dict):
            args = {}
        args = _fix_refs(args)
        seq += 1
        out.append({
            "id": f"s{seq}",
            "title": (str(s.get("title") or spec.get("title") or tool))[:80],
            "tool": tool,
            "tool_title": spec.get("title", tool),
            "args": args,
            "why": (str(s.get("why") or ""))[:300],
            "danger": A.is_danger(tool, args),
            "status": "pending",
            "approved": False,
            "summary": "",
            "error": "",
            "warning": "",
            "impact": A.impact(tool, args) if A.is_danger(tool, args) else "",
            "result": None,
            "elapsed": None,
        })
        if len(out) >= MAX_STEPS:
            break
    return out, seq


# ---------------------------------------------------------------- 计划体检

# 模型写引用时经常漏掉 `$`（实测：把 `$s2.results` 写成 `" s2.results"`），
# 于是引用不被识别 → 参数原样变成字符串传进工具 → 那一步必然失败。这里统一补回来。
_REF_LOOSE = re.compile(r"^\$?\s*s(\d+)\s*((?:\.\w+)*)$")


def _fix_refs(obj):
    """递归把 args 里形如 `s2.results` / `$ s2.results` 的引用归一成 `$s2.results`。"""
    if isinstance(obj, str):
        m = _REF_LOOSE.match(obj.strip())
        if m:
            return f"$s{m.group(1)}{m.group(2)}"
        return obj
    if isinstance(obj, list):
        return [_fix_refs(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _fix_refs(v) for k, v in obj.items()}
    return obj


def _is_blank(v):
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def _plan_issues(steps):
    """体检：挑出"按这个计划跑必然会失败"的地方，返回 [(步骤 id, 问题说明)]。

    只看两类硬伤：① 必填参数空着；② 引用指向不存在或更靠后的步骤。
    目的不是追求完美计划，而是别让用户点下"开始执行"才发现参数是空的。
    """
    issues = []
    for idx, s in enumerate(steps):
        spec = A.get(s["tool"]) or {}
        params = spec.get("params") or {}
        for k, p in params.items():
            if not p.get("required"):
                continue
            if _is_blank(s["args"].get(k)):
                issues.append((s["id"], f"必填参数 {k} 是空的"))
        for k, v in (s["args"] or {}).items():
            for raw in (v if isinstance(v, list) else [v]):
                if not isinstance(raw, str):
                    continue
                m = re.match(r"^\$s(\d+)(?:\.(\w+))?", raw.strip())
                if not m:
                    continue
                n = int(m.group(1))
                if n < 1 or n >= idx + 1:      # 只能引用**前面**的步骤
                    issues.append((s["id"], f"参数 {k} 引用了 $s{n}，但前面没有这一步"))
                else:
                    field = m.group(2)
                    if field:
                        allowed = A.RETURN_FIELDS.get(steps[n - 1]["tool"])
                        if allowed and field not in allowed:
                            issues.append((s["id"],
                                           f"参数 {k} 引用了 $s{n}.{field}，但第 {n} 步"
                                           f"（{steps[n-1]['tool']}）只输出：{'、'.join(allowed)}"))
    return issues


def new_task(text, backend="webchat"):
    """建档：只登记任务（状态 planning），不调用 LLM。便于路由先返回任务号再后台出计划。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("请先输入任务内容")
    t = {
        "id": _uid(), "task": text, "backend": (backend or "webchat").lower(),
        "created_at": _now(), "updated_at": _now(), "status": "planning",
        "note": "", "steps": [], "log": [], "summary": "", "error": "",
        "recover_count": 0, "seq": 0,
    }
    _prune()
    with _lock_for(t["id"]):
        _save(t)
    return t


def plan_task(task_id, log=None):
    """让 LLM 出一条计划并写回任务（阻塞）。失败时把原因写进任务，不抛给调用方。"""
    from .config import research_question
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        _log(t, f"正在为任务生成计划（{t.get('backend')}）…")
        _save(t)
        try:
            prompt = _PLAN_PROMPT.format(task=t["task"], research=(research_question() or "（未填写）"),
                                         spec=A.spec_text())
            raw = _llm_text(prompt, backend=t.get("backend", "webchat"),
                            log=lambda m: _log(t, m), system=_SYS)
            # 等模型的这几十秒里用户可能点了「中止」→ 那就别再把手里的计划写回去
            fresh = get_task(task_id)
            if fresh and fresh.get("status") == "aborted":
                _log(fresh, "已中止，这次生成的计划作废")
                _save(fresh)
                with _LOCK:
                    _ABORT_FLAG.discard(task_id)
                return fresh
            obj = _first_json_object(raw)
            if not obj:
                raise ValueError("没有从模型输出里解析出 JSON 计划")
            steps, seq = _normalize_steps(obj.get("steps"), t, 0)
            if not steps:
                raise ValueError("模型给出的计划里没有可用步骤（工具名或参数不合法）")

            # 计划体检：有硬伤就回问一次重出（方案里承诺的"不合规直接回问一次"）。
            # 实测模型会漏掉引用的 `$`、或把该引用上一步的数组参数留空——
            # 这类计划看起来正常，点执行才发现那一步必然失败。
            issues = _plan_issues(steps)
            if issues:
                _log(t, "计划有 " + str(len(issues)) + " 处问题，回问模型修正一次："
                        + "；".join(f"{i[0]} {i[1]}" for i in issues[:4]))
                try:
                    raw2 = _llm_text(_PLAN_PROMPT.format(task=t["task"],
                                                         research=(research_question() or "（未填写）"),
                                                         spec=A.spec_text())
                                     + _PLAN_FIX.format(issues="\n".join(
                                         f"- 第 {i[0]} 步：{i[1]}" for i in issues)),
                                     backend=t.get("backend", "webchat"),
                                     log=lambda m: _log(t, m), system=_SYS)
                    steps2, seq2 = _normalize_steps((_first_json_object(raw2) or {}).get("steps"), t, 0)
                    if steps2 and len(_plan_issues(steps2)) <= len(issues):
                        steps, seq = steps2, seq2
                        obj = _first_json_object(raw2) or obj
                except Exception as e:
                    _log(t, f"回问修正失败（沿用原计划）：{str(e)[:150]}")

            # 仍有问题的步骤挂个 warning，前端直接提示"去改参数补上"
            left = {}
            for sid, msg in _plan_issues(steps):
                left.setdefault(sid, []).append(msg)
            for s in steps:
                if s["id"] in left:
                    s["warning"] = "；".join(left[s["id"]])[:200]

            t["steps"] = steps
            t["seq"] = seq
            t["note"] = (str(obj.get("note") or ""))[:500]
            t["status"] = "planned"
            _log(t, f"计划已生成，共 {len(steps)} 步"
                    + (f"；其中 {sum(1 for s in steps if s['danger'])} 步需要你确认后才能执行"
                       if any(s["danger"] for s in steps) else ""))
            if left:
                _log(t, "注意：有步骤的参数可能不完整（已在卡片上标出），建议先点「改参数」补上再执行")
        except Exception as e:
            t["status"] = "failed"
            t["error"] = f"生成计划失败：{str(e)[:300]}"
            _log(t, t["error"])
        return _save(t)


def create_task(text, backend="webchat", log=None):
    """建档 + 出计划（阻塞）。同步调用方的便捷入口；路由侧用 new_task + plan_task 后台执行。"""
    t = new_task(text, backend)
    return plan_task(t["id"], log)


# ---------------------------------------------------------------- 执行

def _settle(step, res):
    step["result"] = res.get("data")
    step["summary"] = (res.get("summary") or "")[:600]
    step["error"] = (res.get("error") or "")[:600]
    step["elapsed"] = res.get("elapsed")


def _one_batch_guard(task, step):
    """一次任务最多安排一个批量精读（浏览器通道只能串行）。"""
    if step.get("tool") != "batch_notes":
        return None
    for s in task["steps"]:
        if s["id"] != step["id"] and s.get("tool") == "batch_notes" \
                and s.get("status") in ("done", "running", "awaiting_confirm"):
            return "本任务已有一个批量精读（同一浏览器通道只能串行），这一步已跳过"
    return None


_RUNNING = set()
_ABORT_FLAG = set()          # 协作式中止：后台线程每轮循环都来问一次


def advance(task_id, log=None):
    """执行（或继续执行）任务，直到「需要用户确认的危险步骤」「完成」「中止」或「失败」。

    ⚠ 格外注意：**不能**在整个执行期间持有任务锁。一次执行可能几分钟（要开浏览器），
    而前端要不断轮询读状态——持锁会把所有轮询请求堵死（曾踩：页面看着像卡死）。
    所以这里只用 `_RUNNING` 集合做"同一任务不并发执行"的互斥，文件读写各自走短锁。

    既然是被显式调用来执行的，这里就把上一次的"中止请求"清掉——**中止过的任务可以继续跑**。
    """
    with _LOCK:
        if task_id in _RUNNING:
            return get_task(task_id) or {}
        _RUNNING.add(task_id)
        _ABORT_FLAG.discard(task_id)
    try:
        return _advance_inner(task_id, log)
    finally:
        with _LOCK:
            _RUNNING.discard(task_id)


def _take_abort(task_id):
    """取出（并清除）中止请求。"""
    with _LOCK:
        if task_id in _ABORT_FLAG:
            _ABORT_FLAG.discard(task_id)
            return True
    return False


def _stop_now(t):
    """就地收尾：把任务置为已中止，正在跑的那一步退回 pending（结果不确定）。

    之所以要在这里再写一次：步骤跑完时的 `_save(t)` 会把内存里的 "running"
    覆盖掉 `abort()` 刚写下的 "aborted"，所以发现中止请求后必须由执行方来落这个状态。
    """
    t["status"] = "aborted"
    for s in t["steps"]:
        if s["status"] == "running":
            s["status"] = "pending"
            s["summary"] = "上次执行到这里被打断，未确认是否完成；继续执行会重跑这一步"
    return _save(t)


def _advance_inner(task_id, log=None):
    t = get_task(task_id)
    if not t:
        raise ValueError("任务不存在")
    if t["status"] == "done":
        return t
    t["status"] = "running"
    _save(t)

    while True:
        if _take_abort(task_id):
            return _stop_now(t)
        step = next((s for s in t["steps"] if s["status"] in ("pending", "awaiting_confirm")), None)
        if not step:
            break
        # 危险步骤：挂起等确认（先把参数里的引用解析出来，好让用户看到准确影响面）
        if A.is_danger(step["tool"], step["args"]) and not step.get("approved"):
            step["impact"] = _safe_impact(step, t["steps"])
            gi = _gate_issues(step, t["steps"])
            if gi:
                step["warning"] = "；".join(gi)[:200]
            step["status"] = "awaiting_confirm"
            t["status"] = "awaiting_confirm"
            _log(t, f"⚠ 第 {step['id']} 步「{step['title']}」会改动数据，已暂停等你确认：{step.get('impact')}")
            if gi:
                _log(t, "   注意：这一步的参数现在有问题——" + "；".join(gi)
                        + "。建议先点「先改参数」补上，别直接确认。")
            _save(t)
            return t

        guard = _one_batch_guard(t, step)
        if guard:
            step["status"] = "skipped"
            step["summary"] = guard
            _log(t, guard)
            _save(t)
            continue

        step["status"] = "running"
        step["started_at"] = _now()
        _save(t)
        _log(t, f"▶ 第 {step['id']} 步「{step['title']}」（{step['tool']}）开始")
        try:
            args = _resolve_refs(step["args"], t["steps"])
        except Exception as e:
            _settle(step, {"ok": False, "error": f"参数引用解析失败：{e}"})
            step["status"] = "failed"
            _save(t)
            _log(t, f"✗ 第 {step['id']} 步失败：{step['error']}")
        else:
            # 长步骤（比如一次入库几百条）会持续往日志里写。任务文件原本只在"步骤切换"时才落盘，
            # 于是这几分钟里界面上什么都看不到、像死了。这里让日志回调**限频落盘**（最多 2 秒一次），
            # 前端轮询就能实时看到进度，出问题时也能看到卡在哪一条。
            _last = [0.0]

            def _step_log(msg, _t=t, _last=_last):
                _log(_t, msg)
                now = time.time()
                if now - _last[0] > 2:
                    _last[0] = now
                    try:
                        _save(_t)
                    except Exception:
                        pass

            ctx = A.Ctx(log=_step_log, task_id=t["id"], backend=t["backend"])
            res = A.run(step["tool"], args, ctx)
            _settle(step, res)
            step["status"] = "done" if res.get("ok") else "failed"
            _save(t)
            _log(t, ("✓ " if step["status"] == "done" else "✗ ")
                    + f"第 {step['id']} 步{'完成' if step['status'] == 'done' else '失败'}："
                    + (step["summary"] or step["error"]))

        # 这一步跑完了：如果用户在这期间点了「中止」，就此打住，别再往下走
        # （尤其别再去触发"失败回炉"那次额外的 LLM 调用）
        if _take_abort(task_id):
            return _stop_now(t)

        if step["status"] == "failed":
            if not _try_recover(t, step):
                t["status"] = "failed"
                t["error"] = t.get("error") or f"第 {step['id']} 步失败且无法自动补救：{step['error']}"
                _save(t)
                return t
            _save(t)
            continue
    # 全部步骤结束
    if any(s["status"] == "failed" for s in t["steps"]):
        t["status"] = "failed"
    else:
        t["status"] = "done"
        _log(t, "全部步骤执行完毕，正在生成总结…")
        _save(t)
        _make_summary(t)
    _save(t)
    return t


def _try_recover(t, failed):
    """失败回问 LLM（每任务最多 MAX_RECOVER 次）。返回 True 表示已追加补救步骤。"""
    if t.get("recover_count", 0) >= MAX_RECOVER:
        return False
    if len(t["steps"]) >= MAX_STEPS:
        return False
    t["recover_count"] = t.get("recover_count", 0) + 1
    idx = t["steps"].index(failed) + 1
    done = "\n".join(f"- {s['id']} {s['title']}：{s.get('summary') or s.get('status')}"
                     for s in t["steps"][:idx - 1] if s.get("status") in ("done", "skipped"))
    _log(t, f"↻ 第 {failed['id']} 步失败，回问模型补救（第 {t['recover_count']}/{MAX_RECOVER} 次）…")
    try:
        raw = _llm_text(_RECOVER_PROMPT.format(
            task=t["task"], done=(done or "（无）"), idx=idx, title=failed["title"],
            tool=failed["tool"], args=json.dumps(failed["args"], ensure_ascii=False)[:600],
            error=failed["error"], spec=A.spec_text()),
            backend=t["backend"], log=lambda m: _log(t, m), system=_SYS, timeout=300)
        obj = _first_json_object(raw) or {}
        steps, seq = _normalize_steps(obj.get("steps"), t, t.get("seq", 0))
        if not steps:
            _log(t, "模型认为无法自动补救")
            return False
        t["seq"] = seq
        # 保留已完成的步骤与失败步骤（作为历史），用补救步骤替换其后未执行的剩余步骤
        failed["retried"] = True      # 前端据此标注"这步没做成，已由下面的补救步骤接替"
        t["steps"] = [s for s in t["steps"] if s["status"] in ("done", "skipped")] + [failed] + steps
        _log(t, f"第 {failed['id']} 步没做成，已自动补 {len(steps)} 步重做（只补没做成的事，成功过的不重来）："
                + "；".join(s["title"] for s in steps))
        return True
    except Exception as e:
        _log(t, f"补救失败：{str(e)[:200]}")
        return False


def _make_summary(t):
    steps_txt = "\n".join(
        f"- {s['id']} {s['title']}（{s['tool']}）："
        + (f"{s.get('summary')}" if s.get("status") == "done"
           else f"{s.get('status')} {s.get('error') or ''}")
        for s in t["steps"])
    try:
        txt = _llm_text(_SUM_PROMPT.format(task=t["task"], steps=steps_txt),
                        backend=t["backend"], log=lambda m: _log(t, m), timeout=300)
        t["summary"] = (_strip_fence(txt) or "").strip()[:2000]
    except Exception as e:
        _log(t, f"生成总结失败（不影响结果）：{str(e)[:150]}")
    if not t["summary"]:
        lines = [f"任务「{t['task'][:60]}」共 {len(t['steps'])} 步："]
        for s in t["steps"]:
            mark = {"done": "✓", "skipped": "⤼", "failed": "✗"}.get(s["status"], "·")
            lines.append(f"{mark} {s['title']}：{s.get('summary') or s.get('error') or s['status']}")
        t["summary"] = "\n".join(lines)


def approve(task_id, step_id, log=None):
    """放行一个危险步骤（用户已确认），随后继续执行。"""
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        step = next((s for s in t["steps"] if s["id"] == step_id), None)
        if not step:
            raise ValueError("步骤不存在")
        step["approved"] = True
        step["status"] = "pending"
        _log(t, f"用户已确认第 {step['id']} 步「{step['title']}」，继续执行")
        _save(t)
    return advance(task_id, log)


def skip_step(task_id, step_id, log=None):
    """跳过某一步（含危险步骤）。

    只有在任务**正在执行**（running / awaiting_confirm）时才"跳过并继续"；
    如果任务还停在计划上（planned / aborted / failed），就只把这一步划掉、**不启动执行**——
    否则用户在计划上按个「跳过」，整个任务会莫名其妙地跑起来。
    """
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        prev = t["status"]
        step = next((s for s in t["steps"] if s["id"] == step_id), None)
        if not step:
            raise ValueError("步骤不存在")
        if step["status"] in ("done", "failed"):
            raise ValueError("这一步已经执行过了，不能跳过")
        step["status"] = "skipped"
        step["summary"] = step.get("summary") or "已被用户跳过"
        _log(t, f"用户跳过了第 {step['id']} 步「{step['title']}」")
        _save(t)
    if prev in ("running", "awaiting_confirm"):
        return advance(task_id, log)
    return t


def update_steps(task_id, patches):
    """修改计划里**还没执行**的步骤（参数 / 标题）。

    patches: [{"id": "s2", "args": {...}, "title": "…"}, …]
    已执行（done/failed）的步骤一律不许改——改了会让日志与结果对不上。
    改完会重算这一步的「是否需要确认」与「影响面」（比如把维护脚本的 apply 打开，
    它就从"只读"变成"写库，需要确认"）。
    """
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        if t["status"] in ("running", "planning"):
            raise ValueError("任务正在执行，请先「中止」再改计划")
        by_id = {s["id"]: s for s in t["steps"]}
        changed = []
        for p in (patches or []):
            if not isinstance(p, dict):
                continue
            s = by_id.get((p.get("id") or "").strip())
            if not s:
                continue
            if s["status"] in ("done", "failed", "skipped"):
                raise ValueError(f"第 {s['id']} 步已经执行过，不能修改")
            before = json.dumps(s.get("args") or {}, ensure_ascii=False, sort_keys=True)
            if isinstance(p.get("args"), dict):
                s["args"] = _fix_refs(p["args"])     # 用户手填 s2.results 也帮忙补上 $

            if (p.get("title") or "").strip():
                s["title"] = str(p["title"]).strip()[:80]
            after = json.dumps(s.get("args") or {}, ensure_ascii=False, sort_keys=True)
            if before != after:
                # 参数改过 → 之前那次「确认」作废。否则"改了入库分类，却沿用上一次的确认"
                # 就等于用户确认的是另一件事，这在写库步骤上是不可接受的。
                s["approved"] = False
            s["danger"] = A.is_danger(s["tool"], s["args"])
            s["impact"] = _safe_impact(s, t["steps"]) if s["danger"] else ""
            s["warning"] = ""                        # 人工改过就不再用模型的预警来烦人
            if not s["danger"] and s["status"] == "awaiting_confirm":
                s["status"] = "pending"
            changed.append(s["id"])
        if not changed:
            return t
        # 改完可能已经没有"待确认"的步骤了 → 任务从 awaiting_confirm 退回 planned
        if t["status"] == "awaiting_confirm" and \
                not any(s["status"] == "awaiting_confirm" for s in t["steps"]):
            t["status"] = "planned"
        _log(t, "已修改步骤：" + "、".join(changed))
        return _save(t)


def _gate_issues(step, steps):
    """挂起等确认之前，先确认"这一步现在到底跑不跑得起来"。

    与 `_plan_issues` 的区别：这里**真的去解析引用**，所以能抓到"第 N 步的输出里根本没有那个字段"
    这种只有跑起来才暴露的问题。目的：别让用户点了「确认并执行」才发现参数是空的，
    白烧一轮"失败回炉"（那要多开一次浏览器）。

    参数有问题**不阻止挂起**——用户可以在卡片上点「先改参数」补好再确认。
    """
    out = []
    spec = A.get(step["tool"]) or {}
    for k, p in (spec.get("params") or {}).items():
        if p.get("required") and _is_blank(step["args"].get(k)):
            out.append(f"必填参数 {k} 是空的")
    try:
        _resolve_refs(step["args"], steps)
    except Exception as e:
        out.append(str(e)[:140])
    return out


def _safe_impact(step, steps):
    """算危险步骤的影响面。

    参数里带 `$sN` 引用时尽量解析；解析不了（比如引用的字段实际不存在）**必须说清楚
    "预估不了"**——宁可显示得保守，也不能因为回退成空参数而把影响面缩小成一个假数字，
    用户在确认界面上就是靠这个数字判断"要不要放行"的。
    """
    try:
        return A.impact(step["tool"], _resolve_refs(step["args"], steps))
    except Exception:
        return A.impact(step["tool"], {}) + "（提示：参数里的引用暂时取不到值，实际影响面可能更大，请自行核对）"


def replan(task_id, log=None):
    """用原任务文本重新出一份计划（覆盖现有步骤）。改不动计划时的"推倒重来"。"""
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        if task_id in _RUNNING:
            raise ValueError("任务正在执行，请先「中止」再重新生成计划")
        t["steps"] = []
        t["seq"] = 0
        t["recover_count"] = 0
        t["note"] = ""
        t["summary"] = ""
        t["error"] = ""
        t["log"] = []
        t["status"] = "planning"
        _log(t, "正在按原任务重新生成计划…")
        _save(t)
    return plan_task(task_id, log)


def abort(task_id):
    """中止任务。

    **保留未执行步骤的状态**（不要把它们标成 skipped！否则用户没法从断点续跑——
    这是踩过的坑）。只有当前正在跑的那一步会被退回 pending，因为它的结果不确定。
    真正的停止靠 `_ABORT_FLAG` 与后台线程协作：它在每轮循环开头问一次，然后自己退出。
    """
    with _lock_for(task_id):
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        if t["status"] in ("done",):
            return t
        t["status"] = "aborted"
        for s in t["steps"]:
            if s["status"] == "running":
                s["status"] = "pending"
                s["summary"] = "上次执行到这里被打断，未确认是否完成；继续执行会重跑这一步"
        _log(t, "任务已中止（未执行的步骤已保留，可点「继续执行」接着跑）")
        _save(t)
    with _LOCK:
        _ABORT_FLAG.add(task_id)
    return t


def delete_task(task_id):
    """删除任务文件。

    不用 `os.path.isfile` 先判断：Windows 上杀软扫描刚写出的文件时会有瞬时锁，
    `isfile` 会短暂返回 False，导致"删了却没删掉"。这里直接删 + 重试。
    """
    p = _task_file(task_id)
    for _ in range(4):
        try:
            os.remove(p)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            time.sleep(0.2)
    return not os.path.exists(p)
