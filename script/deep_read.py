# -*- coding: utf-8 -*-
"""使用 Playwright 读取网页完整 HTML。

运行示例：
    python deep_read.py --HTML_PAGE "https://www.example.com/"
"""

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# 登录页常见关键词。只做启发式判断，避免依赖具体站点实现。
LOGIN_KEYWORDS = (
    "login",
    "signin",
    "sign-in",
    "auth",
    "passport",
    "sso",
    "oauth",
    "account/login",
    "user/login",
    "登录",
    "登陆",
    "登入",
    "注册",
    "sign in",
)


def configure_stdio() -> None:
    """将标准输出/错误尽量切到 UTF-8，避免中文或页面内容乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def eprint(message: str) -> None:
    """统一向标准错误输出中文提示。"""
    print(message, file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="读取网页并打印最终页面完整 HTML")
    parser.add_argument("--HTML_PAGE", dest="html_page", help="需要读取的网页地址，必须是 http/https URL")
    args = parser.parse_args()

    if not args.html_page:
        eprint('错误：缺少必需参数 --HTML_PAGE，例如：python deep_read.py --HTML_PAGE "https://www.example.com/"')
        sys.exit(2)

    parsed = urlparse(args.html_page)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        eprint("错误：--HTML_PAGE 必须是有效的 http/https URL。")
        sys.exit(2)

    return args


def find_edge_executable() -> str | None:
    """在 Windows 常见位置查找系统自带 Microsoft Edge。"""
    candidate_paths = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]

    for candidate in candidate_paths:
        if candidate.is_file():
            return str(candidate)
    return None


def launch_edge_context(playwright, user_data_dir: Path, headless: bool):
    """启动 Edge 持久化上下文；优先使用系统 Edge 路径，找不到再使用 Playwright channel。"""
    launch_options = {
        "headless": headless,
        # 使用独立用户数据目录，避免影响用户日常 Edge 配置。
        "args": ["--disable-blink-features=AutomationControlled"],
    }

    edge_executable = find_edge_executable()
    if edge_executable:
        launch_options["executable_path"] = edge_executable
    else:
        launch_options["channel"] = "msedge"

    try:
        return playwright.chromium.launch_persistent_context(str(user_data_dir), **launch_options)
    except PlaywrightError as exc:
        raise RuntimeError(
            "无法启动系统 Microsoft Edge。请确认系统已安装 Edge，且 Playwright 可以调用该浏览器。"
        ) from exc


def get_first_page(context):
    """获取上下文中的第一个页面，没有则新建页面。"""
    if context.pages:
        return context.pages[0]
    return context.new_page()


def normalize_host(url: str) -> str:
    """标准化域名，去掉常见的 www. 前缀。"""
    host = urlparse(url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def contains_login_keyword(value: str) -> bool:
    """检查文本中是否包含登录相关关键词。"""
    lowered = (value or "").lower()
    return any(keyword in lowered for keyword in LOGIN_KEYWORDS)


def safe_locator_count(page, selector: str) -> int:
    """安全统计元素数量，页面跳转或选择器异常时返回 0。"""
    try:
        return page.locator(selector).count()
    except PlaywrightError:
        return 0


def safe_title(page) -> str:
    """安全获取页面标题。"""
    try:
        return page.title()
    except PlaywrightError:
        return ""


def is_login_page(page, target_url: str) -> bool:
    """启发式判断当前页面是否仍处于登录/鉴权状态。"""
    current_url = page.url or ""
    title = safe_title(page)
    target_host = normalize_host(target_url)
    current_host = normalize_host(current_url)
    host_changed = bool(target_host and current_host and target_host != current_host)

    url_has_login_keyword = contains_login_keyword(current_url)
    title_has_login_keyword = contains_login_keyword(title)

    # 密码框是最明确的登录页信号。
    if safe_locator_count(page, "input[type='password']") > 0:
        return True

    # 跳转到其他域名且 URL 明显带登录/鉴权关键词时，通常代表需要用户登录。
    if host_changed and url_has_login_keyword:
        return True

    # URL 或标题带登录关键词，同时页面有表单输入控件时，也按登录页处理。
    input_count = safe_locator_count(page, "input")
    if (url_has_login_keyword or title_has_login_keyword) and input_count > 0:
        return True

    # 兼容部分站点使用账号/手机号/邮箱输入框但暂不展示密码框的多步登录。
    account_input_selector = (
        "input[name*='user' i], input[name*='email' i], input[name*='login' i], "
        "input[name*='account' i], input[name*='phone' i], input[id*='user' i], "
        "input[id*='email' i], input[id*='login' i], input[id*='account' i], "
        "input[id*='phone' i]"
    )
    if (url_has_login_keyword or title_has_login_keyword or host_changed) and safe_locator_count(page, account_input_selector) > 0:
        return True

    return False


def navigate_and_wait(page, url: str) -> None:
    """打开页面并尽量等待页面稳定；网络长连接页面超时不视为失败。"""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        # 很多现代页面存在长连接或持续请求，无法进入 networkidle，继续后续逻辑即可。
        pass


def read_html_without_login(playwright, user_data_dir: Path, url: str) -> tuple[bool, str | None]:
    """先用无头模式读取页面；返回 (是否需要登录, HTML)。"""
    context = launch_edge_context(playwright, user_data_dir, headless=True)
    try:
        page = get_first_page(context)
        page.set_default_timeout(3_000)
        navigate_and_wait(page, url)
        if is_login_page(page, url):
            return True, None
        return False, page.content()
    finally:
        context.close()


def read_html_after_manual_login(playwright, user_data_dir: Path, url: str, timeout_seconds: int = 60) -> str:
    """打开可见浏览器，等待用户手动完成登录后读取 HTML。"""
    eprint("检测到页面可能需要登录，已打开浏览器窗口。请在 1 分钟内完成登录鉴权。")
    context = launch_edge_context(playwright, user_data_dir, headless=False)
    try:
        page = get_first_page(context)
        page.set_default_timeout(3_000)
        navigate_and_wait(page, url)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1_000)
            except PlaywrightTimeoutError:
                pass

            if not is_login_page(page, url):
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass
                return page.content()

            time.sleep(1)

        raise TimeoutError("登录鉴权超时：超过 1 分钟仍未检测到登录成功，已终止后续逻辑。")
    finally:
        context.close()


def main() -> int:
    """脚本入口。"""
    configure_stdio()
    args = parse_args()

    # 所有临时浏览器用户数据都放在工作空间的 .codex_temp 下，符合项目约束。
    workspace = Path(__file__).resolve().parent
    user_data_dir = workspace / ".codex_temp" / "deep_read_edge_profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright:
            need_login, html = read_html_without_login(playwright, user_data_dir, args.html_page)
            if need_login:
                html = read_html_after_manual_login(playwright, user_data_dir, args.html_page)

        sys.stdout.write(html or "")
        sys.stdout.flush()
        return 0
    except TimeoutError as exc:
        eprint(f"错误：{exc}")
        return 1
    except PlaywrightTimeoutError as exc:
        eprint(f"错误：页面加载超时，无法读取 HTML。详情：{exc}")
        return 1
    except PlaywrightError as exc:
        eprint(f"错误：Playwright 执行失败，无法读取 HTML。详情：{exc}")
        return 1
    except RuntimeError as exc:
        eprint(f"错误：{exc}")
        return 1
    except KeyboardInterrupt:
        eprint("错误：用户中断执行。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
