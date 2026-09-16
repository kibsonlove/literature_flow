# -*- coding: utf-8 -*-
"""WebChat 后端：用 Playwright 控制已登录浏览器，把 PDF 上传到网页端 LLM
（chat.deepseek.com），发送六维精读指令，抓取回答文本。

参照 lfz 的 WebChat 思路：不消耗 API token，改用网页端额度/免费额度。
首次运行建议 headless=False，让用户手动登录一次（登录态存于持久化 profile，
之后可 headless=True）。

注意：网页端 DOM 可能随版本变化；本模块带"自适应探测"，不硬依赖固定选择器。
"""
import os
import re
import time
import json

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # webapp/

def _paths_cfg():
    """读取 config/paths.json（本机配置，不入库），允许覆盖 webchat 相关目录。"""
    try:
        with open(os.path.join(_APP_DIR, "config", "paths.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

_CFG = _paths_cfg()
# 优先级：环境变量 > config/paths.json > 项目内默认位置
BROWSER_DIR = (os.environ.get("PLAYWRIGHT_BROWSERS_DIR")
               or _CFG.get("playwright_browsers_dir")
               or os.path.join(_APP_DIR, "cache", "playwright_browsers"))
PROFILE_DIR = (os.environ.get("WEBCHAT_PROFILE_DIR")
               or _CFG.get("webchat_profile_dir")
               or os.path.join(_APP_DIR, "cache", "webchat_profile"))
DEEPSEEK_URL = "https://chat.deepseek.com"

def _apply_browser_path():
    """仅当配置的浏览器目录里确实装过内核（chromium-* 子目录）时才覆盖
    Playwright 的查找路径；否则保留默认位置——这样新用户直接
    `playwright install chromium` 装到系统默认目录即可使用。"""
    try:
        if os.path.isdir(BROWSER_DIR) and any(
            d.startswith("chromium") and os.path.isdir(os.path.join(BROWSER_DIR, d))
            for d in os.listdir(BROWSER_DIR)
        ):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSER_DIR
    except Exception:
        pass


def run_webchat(pdf_path, prompt, title="", headless=False, timeout=300, log=None, url=None):
    """打开网页端 LLM，上传 PDF、发送 prompt，返回抓取到的 Markdown/文本回答。

    log: 可选回调函数(str)，用于回传进度（webapp 可显示给前端）。
    url: 目标站点地址；默认 https://chat.deepseek.com，可自定义其它网页端 LLM。
    """
    from playwright.sync_api import sync_playwright

    target_url = (url or DEEPSEEK_URL).strip() or DEEPSEEK_URL

    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not os.path.exists(pdf_path):
        raise RuntimeError(f"找不到 PDF：{pdf_path}")

    os.makedirs(PROFILE_DIR, exist_ok=True)
    _apply_browser_path()

    with sync_playwright() as p:
        say("启动浏览器（复用登录态 profile）…")
        # 关键：用持久化上下文，登录态（cookie/localStorage）落盘到 PROFILE_DIR，
        # 之后各次运行自动复用，无需重复登录。
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
                ignore_default_args=["--enable-automation"],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                locale="zh-CN",
                viewport=None,
            )
        except Exception as e:
            raise RuntimeError(
                f"启动浏览器失败（若已有 WebChat 窗口在运行，请先关闭再试）：{e}"
            )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        say(f"已打开 {target_url}")

        _ensure_logged_in(page, headless=headless, say=say)

        # 删除上一次单篇精读留下的对话（URL 记录在 cache 文件，跨次生效）
        last_url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "cache", "webchat_last_url.txt")
        prev_url = None
        try:
            prev_url = open(last_url_file, encoding="utf-8").read().strip() or None
        except Exception:
            prev_url = None
        if prev_url:
            _delete_previous_chat(page, say=say, prev_url=prev_url)

        # 新对话（确定性）：软点击后再校验，仍停留在某个会话页里就硬导航回首页，
        # 保证上传/发送发生在全新的空对话中（杜绝旧对话残留附件/上下文导致的错配）
        _maybe_new_chat(page, say=say)
        try:
            if "/a/chat/s/" in (page.url or ""):
                say("仍在旧会话页，强制回首页开启新对话")
                page.goto(target_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
        except Exception:
            pass
        # 洁净守卫：确保当前是全新空对话，否则中止（绝不把指令发进带旧附件的对话）
        if not _ensure_fresh_chat(page, target_url, say=say):
            try:
                context.close()
            except Exception:
                pass
            raise RuntimeError(
                "无法进入全新对话（网页端未响应或页面结构变化）。已中止，未发送指令，"
                "避免读到旧对话里的附件。请在网页端手动新建对话后重试。")

        # 开启"深度思考"模式（若已开则不动）
        _enable_deep_think(page, say=say)

        # 上传 PDF（先点附件按钮让 file input 出现）
        n_before = _composer_pdf_count(page)
        _upload_pdf(page, pdf_path, say=say)
        _verify_attachment(page, pdf_path, n_before=n_before, say=say)

        # 输入指令并发送（优先点发送按钮，回退 Enter）
        _send_prompt(page, prompt, say=say)

        # 等待回答完成并抓取（抓页面 HTML 结构，保留标题/表格/列表）
        answer_html = _wait_answer_html(page, timeout=timeout, say=say)

        # 收尾：删除本轮对话（跑完即删，避免侧栏堆积）
        try:
            cur = page.url.split("?")[0] if "/a/chat/s/" in (page.url or "") else None
            if cur:
                os.makedirs(os.path.dirname(last_url_file), exist_ok=True)
                open(last_url_file, "w", encoding="utf-8").write(cur)  # 删失败时的兜底记录
                _delete_previous_chat(page, say=say, prev_url=cur)
                open(last_url_file, "w", encoding="utf-8").write("")
        except Exception:
            pass

        context.close()
        return answer_html


def _ensure_logged_in(page, headless=False, say=lambda m: None):
    """检测是否已登录；未登录且非 headless 时等待用户手动登录。"""
    # 登录态标志：出现输入框（textarea）或附件按钮；未登录则是登录按钮/二维码
    try:
        page.wait_for_selector("textarea", timeout=8000)
        say("已登录（检测到输入框）")
        return
    except Exception:
        pass
    # 未检测到输入框 → 可能未登录
    if headless:
        raise RuntimeError(
            "网页端未登录（headless 模式无法手动登录）。请先在命令行运行 "
            "`python login_deepseek.py`，用可见浏览器登录一次；登录态会保存并复用。"
        )
    say("未检测到登录态，请在打开的浏览器中手动登录 DeepSeek，登录后程序会自动继续…")
    # 轮询等待输入框出现（最多 timeout）
    for _ in range(120):
        try:
            page.wait_for_selector("textarea", timeout=2000)
            say("登录成功（检测到输入框）")
            return
        except Exception:
            page.wait_for_timeout(1000)
    raise RuntimeError("等待登录超时，请检查 DeepSeek 账号状态。")


def _chat_sid(url):
    """从会话 URL 取 session id（最后一段）；非会话页返回空串。"""
    u = (url or "").split("?")[0].rstrip("/")
    if "/a/chat/s/" not in u:
        return ""
    sid = u.split("/")[-1].strip()
    return sid if len(sid) >= 8 else ""


def delete_chat_by_sid(page, sid, say=lambda m: None):
    """删除侧栏中指定 session id 的对话。返回 bool。选择器多路兜底。"""
    loc = page.locator(f'a[href*="{sid}"]')
    if loc.count() == 0:
        say("侧栏未找到该对话条目，跳过删除")
        return False
    item = loc.first
    try:
        item.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        item.hover()
    except Exception:
        pass
    page.wait_for_timeout(500)

    # 1) 条目内的「更多」按钮
    opened = False
    for sel in ["div[role=button]", "button", "[class*='more']", "[class*='action']"]:
        try:
            b = item.locator(sel)
            if b.count():
                b.first.click()
                opened = True
                break
        except Exception:
            continue
    if not opened:
        say("找不到条目操作按钮，跳过删除")
        return False
    page.wait_for_timeout(600)

    # 2) 菜单里的删除项
    menu_hit = False
    for sel in [".ds-dropdown-menu-option:has-text('删除')",
                "[role=menuitem]:has-text('删除')",
                "[class*='dropdown'] :text('删除')",
                "text=删除"]:
        try:
            opt = page.locator(sel)
            if opt.count():
                opt.first.click()
                menu_hit = True
                break
        except Exception:
            continue
    if not menu_hit:
        say("菜单中无删除项，跳过删除")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    page.wait_for_timeout(700)

    # 3) 确认弹窗
    for sel in ['.ds-button:has-text("删除该对话")',
                'button:has-text("删除该对话")',
                '[role=button]:has-text("删除该对话")',
                '.ds-button:has-text("删除")']:
        try:
            cf = page.locator(sel)
            if cf.count():
                cf.first.click()
                page.wait_for_timeout(1000)
                gone = page.locator(f'a[href*="{sid}"]').count() == 0
                say("已删除该对话" + ("" if gone else "（已点确认，列表待刷新）"))
                return True
        except Exception:
            continue
    say("未找到确认删除按钮")
    return False


def _delete_previous_chat(page, say=lambda m: None, prev_url=None):
    """删除上一轮创建的对话（按 session id 匹配侧栏条目）。

    **坑（2026-09-15 定位）**：侧栏条目 href 是**相对路径**（/a/chat/s/<uuid>），
    page.url 却是绝对 URL——早期用 a[href="<绝对URL>"] 精确匹配永远命中 0，
    导致单篇/批量/长文精读的对话全部删不掉，侧栏越堆越多。
    现改为取 session id 后 `a[href*="<sid>"]` 模糊匹配。
    """
    sid = _chat_sid(prev_url)
    if not sid:
        return  # 无法识别会话 → 不删，避免误删用户手动对话
    try:
        delete_chat_by_sid(page, sid, say=say)
    except Exception as e:
        say(f"删除对话失败（已忽略）：{repr(e)[:80]}")


def _maybe_new_chat(page, say=lambda m: None, base_url=None):
    """确保每篇从全新对话开始：优先点「开启新对话」，找不到入口就直接回首页（等于新对话）。

    注意：DeepSeek 页面无原生 <button>，老版本用 button 选择器从未命中过，导致整批共用一个对话。
    """
    for sel in ["text=开启新对话", "[aria-label*='new' i]", "[class*='new-chat']", "[class*='new_chat']"]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                say("已开新对话")
                return
        except Exception:
            continue
    if base_url:
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            say("已回首页开启新对话（兜底）")
        except Exception:
            pass


def _upload_pdf(page, pdf_path, say=lambda m: None):
    """点击附件按钮并选择 PDF 文件。"""
    # 先尝试直接定位隐藏的 file input
    fi = page.query_selector("input[type=file]")
    if fi is None:
        # 点击附件按钮（常见：带 '附件'/'上传' 文字或 paperclip 图标）
        for sel in ["button:has-text('附件')", "button[aria-label*='attach' i]",
                    "button:has-text('上传')", "[class*='attach']"]:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(600)
                fi = page.query_selector("input[type=file]")
                if fi:
                    break
    if fi is None:
        raise RuntimeError("找不到文件上传入口，无法上传 PDF。")
    fi.set_input_files(pdf_path)
    say(f"已上传 PDF：{os.path.basename(pdf_path)}")
    # 等附件上传/处理完成（未完成时点发送会被忽略）
    page.wait_for_timeout(3000)


def _enable_deep_think(page, say=lambda m: None):
    """确保「深度思考」开启。DeepSeek 用 div.ds-toggle-button，
    激活态 class 含 `ds-toggle-button--selected`。"""
    sels = [
        "div.ds-toggle-button:has-text('深度思考')",
        "[class*='ds-toggle-button']:has-text('深度思考')",
    ]
    for sel in sels:
        try:
            btn = page.query_selector(sel)
        except Exception:
            btn = None
        if not btn:
            continue
        try:
            cls = btn.get_attribute("class") or ""
            if "ds-toggle-button--selected" in cls:
                say("深度思考已开启")
            else:
                btn.click()
                page.wait_for_timeout(600)
                say("已开启深度思考")
            return True
        except Exception:
            continue
    say("未找到「深度思考」开关（页面结构可能不同）")
    return False


def _click_send(page, say=lambda m: None):
    """点击发送按钮，返回是否成功。

    DeepSeek 是自定义 div 组件（非原生 button）：发送键为
    `div.ds-button--primary.ds-button--circle`（蓝色填充圆形）。
    """
    sels = [
        "div.ds-button--primary.ds-button--circle",
        "div[class*='ds-button--primary'][class*='ds-button--circle']",
        "button[aria-label*='发送']",
        "button[aria-label*='send' i]",
        "button[type='submit']",
    ]
    for sel in sels:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False


def _send_prompt(page, prompt, say=lambda m: None):
    """把指令填入输入框并发送。

    发送成功的判据 = **聊天区用户消息数增加**（DeepSeek 发送后会保留输入框内容，
    不能用"输入框清空"判断）。确认未发出才重试，最多一次，避免误点生成中的「停止」。
    """
    box = page.query_selector("textarea")
    if box is None:
        raise RuntimeError("找不到输入框，无法发送指令。")

    def _msg_count():
        try:
            return page.evaluate("() => document.querySelectorAll('.ds-message').length")
        except Exception:
            return -1

    before = _msg_count()

    def _sent(seconds=8.0):
        end = time.time() + seconds
        while time.time() < end:
            if _msg_count() > before:
                return True
            page.wait_for_timeout(300)
        return False

    try:
        box.click()
    except Exception:
        pass
    box.fill(prompt)
    page.wait_for_timeout(600)

    if _click_send(page, say=say):
        say("已点击发送按钮")
    else:
        box.press("Enter")
        say("未找到发送按钮，改用 Enter 发送")

    if _sent():
        say("已发送（检测到新消息），等待网页端生成…")
        return

    say("首次发送未检测到新消息，用 Enter 重试一次…")
    try:
        box.press("Enter")
    except Exception:
        pass
    if _sent():
        say("已发送（重试成功），等待网页端生成…")
    else:
        say("警告：未检测到新消息，可能发送失败（继续等待回答）")


# 正式回答容器（DeepSeek）：.ds-markdown.ds-assistant-message-main-content
# 注意：深度思考的思维链是 .ds-markdown（无 assistant-main-content），**绝不能回退到它**，
# 否则思考阶段会抓到思维链。只认带 assistant-main-content 的容器。
_ANSWER_SELS = [
    ".ds-assistant-message-main-content",
    "[class*='assistant-message-main-content']",
]


def _answer_nodes(page):
    """按优先级返回当前匹配到的回答节点列表（取第一个命中的选择器）。"""
    for sel in _ANSWER_SELS:
        nodes = page.query_selector_all(sel)
        if nodes:
            return nodes
    return []


def _answer_node(page):
    """返回最后一个正式回答容器（可能为 None）。"""
    nodes = _answer_nodes(page)
    return nodes[-1] if nodes else None


def _clean_answer_html(h):
    """清洗回答 HTML：去掉脚本/样式/按钮/svg 等杂质。"""
    if not h:
        return h
    h = re.sub(r"<script[\s\S]*?</script>", "", h, flags=re.I)
    h = re.sub(r"<style[\s\S]*?</style>", "", h, flags=re.I)
    h = re.sub(r"<button[\s\S]*?</button>", "", h, flags=re.I)
    h = re.sub(r"<svg[\s\S]*?</svg>", "", h, flags=re.I)
    return h.strip()


_GREET_RE = re.compile(
    r"^(好的|当然|没问题|以下是|如下是|下面(是|为)|为您|我已经|我帮|我们(来看|可以)|根据)"
)
_TAIL_RE_HTML = re.compile(
    r"^(希望|如需|如果需要|还需要我|需要我|要不要我|以上[是为]|如有其他|如有别的|如有更多|欢迎|随时)"
)


def _strip_html_greeting(h):
    """删除 HTML 开头的寒暄段落与结尾的客套段落。"""
    if not h:
        return h
    # 开头：最多删 3 个寒暄 <p>
    for _ in range(3):
        m = re.match(r"^\s*<p[^>]*>([\s\S]*?)</p>", h, flags=re.I)
        if not m:
            break
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if _GREET_RE.match(text) and len(text) < 60:
            h = h[m.end():].lstrip()
        else:
            break
    # 结尾：删客套 <p>
    for _ in range(5):
        m = re.search(r"<p[^>]*>([\s\S]*?)</p>\s*$", h, flags=re.I)
        if not m:
            break
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if _TAIL_RE_HTML.match(text) and len(text) < 80:
            h = h[: m.start()].rstrip()
        else:
            break
    return h


def _wait_answer_html(page, timeout=300, say=lambda m: None, stop_check=None):
    """等待并返回网页端**正式回答**的 HTML（保留标题/表格/列表）。

    基准：记录"发送前最后一个回答容器的 HTML"，之后只认与基准不同且非空的内容，
    避免把历史回答或深度思考的思维链误当本次答案。
    stop_check: 可选回调，返回 True 时立即中断（每 1.5s 检查一次）。
    """
    page.wait_for_timeout(1000)
    base = ""
    node0 = _answer_node(page)
    if node0:
        try:
            base = node0.inner_html() or ""
        except Exception:
            base = ""

    deadline = time.time() + timeout
    html = ""
    stable = 0
    while time.time() < deadline:
        if stop_check and stop_check():
            say("收到停止指令，中断等待")
            if html:
                return _strip_html_greeting(_clean_answer_html(html))
            raise RuntimeError("已停止（用户中断）")
        node = _answer_node(page)
        h = ""
        if node:
            try:
                h = node.inner_html() or ""
            except Exception:
                h = ""
        if h and h != base and len(h) > len(html):
            html = h
            stable = 0
        elif html and h == html:
            stable += 1
        if html and stable >= 3:
            say("网页端回答已稳定（HTML）")
            return _strip_html_greeting(_clean_answer_html(html))
        page.wait_for_timeout(1500)
    if html:
        say("达到超时但已抓到部分回答")
        return _strip_html_greeting(_clean_answer_html(html))
    raise RuntimeError("未能抓到网页端回答（可能未生成或选择器失效）。")



def _chat_msg_count(page):
    """当前对话中的消息块数量（空对话为 0）。"""
    try:
        return len(page.locator(".ds-markdown").all())
    except Exception:
        return 0


def _ensure_fresh_chat(page, target_url, say=lambda m: None):
    """确保处于全新空对话；非空则硬导航回首页重建，最多重试 3 次。"""
    for _ in range(3):
        if _chat_msg_count(page) == 0:
            return True
        say("检测到当前对话已有消息，强制回首页重建新对话…")
        try:
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception:
            pass
    return _chat_msg_count(page) == 0


def _lcs_len(a, b):
    """最长公共子串长度（短串 DP，够用）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _name_core(name):
    """文件名核心：去扩展名、去空白/连字符/下划线。"""
    import os as _os
    import re as _re
    base = _os.path.splitext(name or "")[0]
    for pre in ("lfupload_", "upload_"):
        if base.startswith(pre):
            base = base[len(pre):]
    return _re.sub(r"[\s\-_.,，。·]+", "", base)


def _composer_pdf_count(page):
    """输入区（composer）里出现的 .pdf 附件标记数量；-1 表示无法判断。

    只扫输入区容器，不扫整页——否则侧栏/历史对话里的文件名会造成误判。
    """
    import re as _re
    for sel in ('form', 'div[class*="composer"]', 'div[class*="chat-input"]', 'textarea'):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                txt = loc.inner_text()
                if txt is None:
                    continue
                n = len(_re.findall(r"\.pdf", txt, _re.I))
                if n > 0:
                    return n
        except Exception:
            continue
    # 输入区里没看到：回退到"整页 .pdf 计数"作为下限参考
    try:
        return 0
    except Exception:
        return -1


def _page_pdf_count(page):
    """整页 .pdf 计数（仅作弱信号参考）。"""
    import re as _re
    try:
        return len(_re.findall(r"\.pdf", page.inner_text("body"), _re.I))
    except Exception:
        return -1


def _verify_attachment(page, pdf_path, n_before=None, say=lambda m: None):
    """上传生效性「体检」——只记录，不中止（2026-09-15 第三次修订）。

    教训：此前用整页文件名相似度比对做硬校验，网页端重命名/侧栏残留/选择器变化
    都会误杀，导致单篇与批量被静默跳过、还得人工介入。现在的中止条件只剩两个
    硬条件：①对话非空（_ensure_fresh_chat）②上传动作本身报错（_upload_pdf 抛错）。
    本函数只在日志里说明附件状态，便于排查，不阻断流程。
    """
    import os as _os
    parts = []
    if n_before is not None and n_before >= 0:
        after = _composer_pdf_count(page)
        if after >= 0:
            parts.append(f"输入区附件 {n_before}→{after}")
            if after > n_before:
                say("附件校验：已挂载本篇 PDF（输入区计数增加）")
                return True
    w_before, w_after = -1, -1
    w_after = _page_pdf_count(page)
    parts.append(f"页面 .pdf 计数 {w_after}")
    exp = _os.path.basename(pdf_path)[:40]
    say(f"附件校验（仅记录，不阻断）：{' / '.join(parts)}；目标文件 {exp}")
    return True



class WebChatSession:
    """复用同一个浏览器上下文处理多篇（避免反复启停导致登录态丢失 / 被风控）。

    用法：
        s = WebChatSession(headless=True, log=print)
        s.open()                       # 启动 + 登录检查（一次）
        html = s.ask(pdf_path, prompt, timeout=1800, stop_check=...)   # 可多次
        s.close()
    """

    def __init__(self, url=None, headless=True, log=None):
        self.url = (url or DEEPSEEK_URL).strip() or DEEPSEEK_URL
        self.headless = headless
        self.log = log or (lambda m: None)
        self._pw = None
        self._ctx = None
        self.page = None
        self.last_chat_url = None  # 上一轮批量会话的 URL（供删除用）

    def open(self):
        from playwright.sync_api import sync_playwright

        os.makedirs(PROFILE_DIR, exist_ok=True)
        _apply_browser_path()
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                ignore_default_args=["--enable-automation"],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                locale="zh-CN",
                viewport=None,
            )
        except Exception as e:
            raise RuntimeError(f"启动浏览器失败（若已有 WebChat 窗口在运行，请先关闭再试）：{e}")
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.log(f"已打开 {self.url}")
        _ensure_logged_in(self.page, headless=self.headless, say=self.log)
        return self

    def ask(self, pdf_path, prompt, title="", timeout=1800, stop_check=None):
        p = self.page
        prev_url, self.last_chat_url = self.last_chat_url, None
        _delete_previous_chat(p, say=self.log, prev_url=prev_url)
        _maybe_new_chat(p, say=self.log, base_url=self.url)
        # 洁净守卫（与单篇一致）：软开新对话后验证对话为空，否则硬导航重建
        if not _ensure_fresh_chat(p, self.url, say=self.log):
            raise RuntimeError("无法进入全新对话，跳过本篇（避免读到旧对话的附件）")
        _enable_deep_think(p, say=self.log)
        n_before = _composer_pdf_count(p)
        _upload_pdf(p, pdf_path, say=self.log)
        _verify_attachment(p, pdf_path, n_before=n_before, say=self.log)
        _send_prompt(p, prompt, say=self.log)
        html = _wait_answer_html(p, timeout=timeout, say=self.log, stop_check=stop_check)
        # 记录本轮会话 URL，供下一轮删除
        try:
            if "/a/chat/s/" in (p.url or ""):
                self.last_chat_url = p.url.split("?")[0]
        except Exception:
            pass
        return html

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None


# ==================== 纯文本会话（不上传文件，长文分章用） ====================

class WebChatTextSession:
    """只发文本、不上传文件的网页端会话：可在同一对话内多轮问答。

    用法：
        s = WebChatTextSession(headless=False, log=print)
        s.open()                    # 启动 + 登录检查 + 新对话
        html = s.ask("一段文本问题")  # 可多次，同一对话内累积上下文
        s.close()                   # 关闭浏览器
    """

    def __init__(self, url=None, headless=False, log=None):
        self.url = (url or DEEPSEEK_URL).strip() or DEEPSEEK_URL
        self.headless = headless
        self.log = log or (lambda m: None)
        self._pw = None
        self._ctx = None
        self.page = None

    def open(self, fresh=True):
        from playwright.sync_api import sync_playwright

        os.makedirs(PROFILE_DIR, exist_ok=True)
        _apply_browser_path()
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                ignore_default_args=["--enable-automation"],
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                locale="zh-CN", viewport=None,
            )
        except Exception as e:
            raise RuntimeError(f"启动浏览器失败（若已有 WebChat 窗口在运行，请先关闭再试）：{e}")
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.goto(self.url, wait_until="domcontentloaded")
        _ensure_logged_in(self.page, headless=self.headless, say=self.log)
        if fresh:
            _maybe_new_chat(self.page, say=self.log, base_url=self.url)
            _ensure_fresh_chat(self.page, self.url, say=self.log)
        return self

    def ask(self, text, timeout=600):
        """在同一对话内发送一段文本，返回回答 HTML。"""
        _send_prompt(self.page, text, say=self.log)
        return _wait_answer_html(self.page, timeout=timeout, say=self.log)

    def summarize_here(self, prompt, timeout=900):
        """在本对话内追加一条总结请求（与 ask 等价，语义区分）。"""
        return self.ask(prompt, timeout=timeout)

    def close(self, delete_chat=True):
        try:
            if delete_chat and self.page:
                cur = self.page.url.split("?")[0] if "/a/chat/s/" in (self.page.url or "") else None
                if cur:
                    _delete_previous_chat(self.page, say=self.log, prev_url=cur)
        except Exception:
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
