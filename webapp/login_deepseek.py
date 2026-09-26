# -*- coding: utf-8 -*-
"""打开可见浏览器登录网页端 LLM（默认 chat.deepseek.com），把登录态保存到持久化 profile。

登录一次后，webapp 的「网页端 WebChat」后端会复用该登录态，无需重复登录。

用法（在 webapp 目录下执行）：
    venv\\Scripts\\python login_deepseek.py
    venv\\Scripts\\python login_deepseek.py https://chat.deepseek.com   # 也可指定其它站点

前置：
    pip install playwright
    playwright install chromium

浏览器内核与登录态位置由 core/webchat.py 统一决定（环境变量 > config/paths.json
> webapp/cache/ 下的默认目录），此处不写死路径。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.webchat import (  # noqa: E402
    DEEPSEEK_URL, profile_dir, _apply_browser_path,
)


def main():
    from playwright.sync_api import sync_playwright

    target = (sys.argv[1] if len(sys.argv) > 1 else DEEPSEEK_URL).strip() or DEEPSEEK_URL
    _apply_browser_path()
    profile = profile_dir()
    os.makedirs(profile, exist_ok=True)

    print("=" * 62)
    print("即将打开浏览器，请在弹出的窗口中完成登录。")
    print("目标站点：  ", target)
    print("登录态保存到：", profile)
    print("=" * 62)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=False,
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
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(target, wait_until="domcontentloaded")

        print("请在浏览器中完成登录…（检测到输入框后即视为登录成功）")
        logged = False
        for _ in range(600):  # 最多等约 10 分钟
            try:
                page.wait_for_selector("textarea", timeout=2000)
                print("[OK] 已检测到登录态，已保存。")
                logged = True
                break
            except Exception:
                pass
        if not logged:
            print("未检测到输入框；若你已登录，关闭窗口后登录态通常也已写入。")

        try:
            input("按回车关闭浏览器并退出…")
        except EOFError:
            pass
        ctx.close()

    print("完成。之后 webapp 使用「网页端 WebChat」即可免登录。")


if __name__ == "__main__":
    main()
