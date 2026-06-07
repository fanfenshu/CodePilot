#!/usr/bin/env python3
"""
ChatGPT Session Manager — 管理和刷新 ChatGPT Pro 订阅的 access_token

功能:
  1. --init: 首次登录，通过 Playwright 打开 ChatGPT 登录页面（非 headless），用户手动登录后自动提取 token
  2. --refresh: 用已有 cookies 刷新 access_token（headless，可 cron 自动运行）
  3. --status: 查看当前 token 状态

用法:
  python3 chatgpt_session.py --init                    # 首次登录（会打开浏览器窗口）
  python3 chatgpt_session.py --refresh                 # 刷新 token（headless）
  python3 chatgpt_session.py --status                  # 查看 token 状态
  python3 chatgpt_session.py --refresh --config /path/to/config.yaml
"""

import json
import os
import sys
import time
import argparse
import yaml

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(MODULE_DIR, 'data')
DEFAULT_TOKEN_FILE = os.path.join(DEFAULT_DATA_DIR, 'chatgpt_token.json')
DEFAULT_COOKIES_FILE = os.path.join(DEFAULT_DATA_DIR, 'chatgpt_cookies.json')

CHATGPT_BASE = 'https://chatgpt.com'
SESSION_URL = f'{CHATGPT_BASE}/api/auth/session'


def _log(msg):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}')


def _ensure_data_dir(data_dir=None):
    d = data_dir or DEFAULT_DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _load_cookies(cookies_file=None):
    """加载已保存的 Playwright cookies。"""
    cf = cookies_file or DEFAULT_COOKIES_FILE
    if not os.path.exists(cf):
        return None
    try:
        with open(cf) as f:
            cookies = json.load(f)
        _log(f'Loaded {len(cookies)} cookies from {cf}')
        return cookies
    except Exception as e:
        _log(f'ERROR: Failed to load cookies: {e}')
        return None


def _save_cookies(cookies, cookies_file=None):
    """保存 Playwright cookies。"""
    cf = cookies_file or DEFAULT_COOKIES_FILE
    _ensure_data_dir(os.path.dirname(cf))
    with open(cf, 'w') as f:
        json.dump(cookies, f, indent=2)
    _log(f'Saved {len(cookies)} cookies to {cf}')


def _save_token(access_token, expires_at=0, token_file=None):
    """保存 access_token。"""
    tf = token_file or DEFAULT_TOKEN_FILE
    _ensure_data_dir(os.path.dirname(tf))
    data = {
        'access_token': access_token,
        'expires_at': expires_at,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(tf, 'w') as f:
        json.dump(data, f, indent=2)
    os.chmod(tf, 0o600)
    _log(f'Token saved to {tf}')


def _load_token(token_file=None):
    """加载已保存的 token。"""
    tf = token_file or DEFAULT_TOKEN_FILE
    if not os.path.exists(tf):
        return None
    try:
        with open(tf) as f:
            return json.load(f)
    except Exception:
        return None


def init_login(data_dir=None, token_file=None, cookies_file=None):
    """首次登录：打开浏览器让用户手动登录 ChatGPT，登录成功后提取 token。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log('ERROR: playwright not installed. Run: pip3 install playwright && playwright install chromium')
        return False

    _ensure_data_dir(data_dir)
    tf = token_file or DEFAULT_TOKEN_FILE
    cf = cookies_file or DEFAULT_COOKIES_FILE

    _log('Starting browser for ChatGPT login...')
    _log('Please log in to ChatGPT in the browser window.')
    _log('After login, the token will be extracted automatically.')

    with sync_playwright() as p:
        # 非 headless，让用户手动登录
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            locale='en-US',
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
        )
        page = context.new_page()

        # 导航到 ChatGPT
        page.goto(CHATGPT_BASE, wait_until='domcontentloaded', timeout=60000)
        _log('Browser opened. Waiting for login...')

        # 等待用户登录完成（检测到 chatgpt.com 上的对话页面）
        max_wait = 300  # 最多等 5 分钟
        logged_in = False
        for i in range(max_wait):
            time.sleep(1)
            current_url = page.url
            # 登录成功后 URL 通常是 chatgpt.com/ 或 chatgpt.com/c/xxx
            if 'chatgpt.com' in current_url and '/auth' not in current_url and 'login' not in current_url:
                # 尝试获取 session
                try:
                    resp = page.goto(SESSION_URL, wait_until='domcontentloaded', timeout=15000)
                    if resp and resp.status == 200:
                        body = page.inner_text('body')
                        session_data = json.loads(body)
                        if session_data.get('accessToken'):
                            logged_in = True
                            break
                except Exception:
                    pass
            if i > 0 and i % 30 == 0:
                _log(f'Still waiting for login... ({i}s)')

        if not logged_in:
            _log('ERROR: Login timed out (5 minutes). Please try again.')
            browser.close()
            return False

        # 提取 token
        body = page.inner_text('body')
        session_data = json.loads(body)
        access_token = session_data.get('accessToken')
        expires = session_data.get('expires')

        if not access_token:
            _log('ERROR: Could not extract access_token from session')
            browser.close()
            return False

        # 计算过期时间
        expires_at = 0
        if expires:
            try:
                from datetime import datetime
                # expires 格式: "2024-07-15T12:00:00.000Z"
                dt = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                expires_at = dt.timestamp()
            except Exception:
                expires_at = time.time() + 86400 * 14  # 默认 14 天

        # 保存 token
        _save_token(access_token, expires_at, tf)

        # 保存 cookies
        cookies = context.cookies()
        _save_cookies(cookies, cf)

        remaining_hours = (expires_at - time.time()) / 3600
        _log(f'Login successful! Token expires in {remaining_hours:.1f} hours')
        _log(f'Token: {access_token[:20]}...{access_token[-10:]}')

        browser.close()
        return True


def refresh_token(data_dir=None, token_file=None, cookies_file=None):
    """用已有 cookies 刷新 access_token（headless 模式）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log('ERROR: playwright not installed')
        return False

    tf = token_file or DEFAULT_TOKEN_FILE
    cf = cookies_file or DEFAULT_COOKIES_FILE

    # 加载已有 cookies
    cookies = _load_cookies(cf)
    if not cookies:
        _log('ERROR: No cookies found. Run --init first.')
        return False

    # 记录刷新前的 token
    old_token_data = _load_token(tf)
    old_expires = old_token_data.get('expires_at', 0) if old_token_data else 0

    _log('Refreshing ChatGPT token (headless)...')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale='en-US',
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        )

        # 注入 cookies
        # 先访问目标域名建立上下文
        page = context.new_page()
        page.goto(CHATGPT_BASE, wait_until='domcontentloaded', timeout=30000)
        time.sleep(2)

        # 注入 cookies
        context.add_cookies(cookies)
        _log(f'Injected {len(cookies)} cookies')

        # 多页面访问（触发 cookie 刷新）
        pages_to_visit = [
            CHATGPT_BASE,
            f'{CHATGPT_BASE}/',
        ]
        for url in pages_to_visit:
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                time.sleep(3)
            except Exception as e:
                _log(f'WARN: Failed to visit {url}: {e}')

        # 获取 session
        try:
            resp = page.goto(SESSION_URL, wait_until='domcontentloaded', timeout=15000)
            if not resp or resp.status != 200:
                _log(f'ERROR: Session endpoint returned {resp.status if resp else "no response"}')
                # 检查是否被重定向到登录页
                if 'login' in page.url or 'auth' in page.url:
                    _log('ERROR: Session expired, redirected to login. Run --init to re-login.')
                browser.close()
                return False

            body = page.inner_text('body')
            session_data = json.loads(body)
            access_token = session_data.get('accessToken')

            if not access_token:
                _log('ERROR: No accessToken in session response')
                _log(f'Response: {body[:200]}')
                browser.close()
                return False

            # 计算过期时间
            expires = session_data.get('expires')
            expires_at = 0
            if expires:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                    expires_at = dt.timestamp()
                except Exception:
                    expires_at = time.time() + 86400 * 14

            # 验证 token 是否真正刷新了
            if old_expires > 0 and expires_at <= old_expires:
                _log(f'WARNING: Token expiry not extended (old={old_expires}, new={expires_at})')
                _log('Token may not have been refreshed. Will save anyway.')

            # 保存新 token
            _save_token(access_token, expires_at, tf)

            # 更新 cookies
            new_cookies = context.cookies()
            _save_cookies(new_cookies, cf)

            remaining = (expires_at - time.time()) / 3600
            _log(f'Token refreshed! Expires in {remaining:.1f} hours')

            browser.close()
            return True

        except Exception as e:
            _log(f'ERROR: Failed to get session: {e}')
            browser.close()
            return False


def show_status(token_file=None):
    """显示当前 token 状态。"""
    tf = token_file or DEFAULT_TOKEN_FILE
    data = _load_token(tf)

    if not data:
        _log(f'No token file found at {tf}')
        _log('Run: python3 chatgpt_session.py --init')
        return

    token = data.get('access_token', '')
    expires_at = data.get('expires_at', 0)
    updated_at = data.get('updated_at', 'unknown')

    _log(f'Token file: {tf}')
    _log(f'Token: {token[:20]}...{token[-10:]}' if len(token) > 30 else f'Token: {token}')
    _log(f'Updated at: {updated_at}')

    if expires_at > 0:
        remaining = expires_at - time.time()
        if remaining > 0:
            hours = remaining / 3600
            days = hours / 24
            _log(f'Expires in: {days:.1f} days ({hours:.1f} hours)')
            _log(f'Status: ACTIVE')
        else:
            _log(f'Status: EXPIRED (expired {abs(remaining)/3600:.1f} hours ago)')
    else:
        _log(f'Expires at: unknown')
        _log(f'Status: UNKNOWN (no expiry info)')


def main():
    parser = argparse.ArgumentParser(description='ChatGPT Session Manager')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--init', action='store_true', help='首次登录（打开浏览器）')
    group.add_argument('--refresh', action='store_true', help='刷新 token（headless）')
    group.add_argument('--status', action='store_true', help='查看 token 状态')

    parser.add_argument('--config', help='配置文件路径（YAML）')
    parser.add_argument('--data-dir', help='数据目录')
    parser.add_argument('--token-file', help='Token 文件路径')
    parser.add_argument('--cookies-file', help='Cookies 文件路径')

    args = parser.parse_args()

    # 从 config 文件读取配置
    data_dir = args.data_dir
    token_file = args.token_file
    cookies_file = args.cookies_file

    if args.config and os.path.exists(args.config):
        try:
            with open(args.config) as f:
                cfg = yaml.safe_load(f)
            data_dir = data_dir or cfg.get('data_dir')
            token_file = token_file or cfg.get('token_file')
            cookies_file = cookies_file or cfg.get('cookies_file')
        except Exception as e:
            _log(f'WARN: Failed to load config: {e}')

    if args.init:
        success = init_login(data_dir, token_file, cookies_file)
        sys.exit(0 if success else 1)
    elif args.refresh:
        success = refresh_token(data_dir, token_file, cookies_file)
        sys.exit(0 if success else 1)
    elif args.status:
        show_status(token_file)


if __name__ == '__main__':
    main()
