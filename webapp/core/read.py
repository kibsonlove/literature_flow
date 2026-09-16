# -*- coding: utf-8 -*-
"""精读：由领域包驱动的提示词 + BYO-key 大模型出笔记。
后端可切换：read_paper（DeepSeek API）/ read_paper_webchat（网页端 LLM，省 token）。
"""
import re

from openai import OpenAI

from .config import load_settings
from .prompt import SYSTEM_PROMPT


def _strip_fence(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def read_paper(text, title="", extra_system="", system=None):
    """API 后端：把全文发给 DeepSeek，返回 Markdown 笔记。"""
    s = load_settings()
    if not s.get("api_key"):
        raise RuntimeError("未配置 API key，请先在设置页填写")
    client = OpenAI(
        api_key=s["api_key"],
        base_url=s.get("base_url") or "https://api.deepseek.com",
    )
    model = s.get("model") or "deepseek-chat"
    user = (
        f"论文标题：{title}\n\n全文（按 PDF 页码标记）：\n{text}\n\n"
        f"请按上述模板产出 Markdown 精读笔记。"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": (system or SYSTEM_PROMPT) + extra_system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return _strip_fence(resp.choices[0].message.content)


def read_paper_webchat(pdf_path, title="", headless=False, timeout=300, url=None, log=None,
                       extra_prompt="", system=None):
    """WebChat 后端：用 Playwright 控制已登录浏览器，把 PDF 上传到网页端 LLM，
    发送精读指令，抓取**回答的 HTML 结构**（保留标题/表格/列表）。不消耗 API token。

    url: 目标站点地址（默认 https://chat.deepseek.com），可自定义。
    log: 可选进度回调 str->None。
    返回：HTML 片段字符串（由 run_webchat 清洗 + 剥寒暄）。
    """
    from .webchat import run_webchat

    return run_webchat(
        pdf_path, (system or SYSTEM_PROMPT) + extra_prompt, title=title, headless=headless,
        timeout=timeout, url=url, log=log,
    )


def _llm_chat(prompt, system="You are a helpful assistant.", timeout=120):
    """通用一次性对话（用 settings.json 的 API 配置），供领域包生成等内部用途。"""
    s = load_settings()
    if not s.get("api_key"):
        raise RuntimeError("未配置 API key（设置页）")
    client = OpenAI(api_key=s["api_key"], base_url=s.get("base_url") or "https://api.deepseek.com")
    resp = client.chat.completions.create(
        model=s.get("model") or "deepseek-chat",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=0.2, timeout=timeout,
    )
    return _strip_fence(resp.choices[0].message.content)
