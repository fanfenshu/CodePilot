#!/usr/bin/env python3
"""
wx_session_refresh.py — 微信公众平台会话自动续期模块

通用模块，可集成到任何需要维持微信 MP 平台登录状态的系统中。

原理：
  微信 slave_sid cookie 约4天过期，服务端存在约4天的续签窗口：
  超过该窗口后，普通页面访问不再签发新 slave_sid。

  策略：
  Phase 1: 常规续期（注入 cookies + 多页面访问）
  Phase 2: 如果 Phase 1 失败，通过登录页入口重新触发 session 刷新

使用方式：
  1. 独立脚本运行：
     python3 wx_session_refresh.py --config /path/to/config.yaml

  2. 作为模块导入：
     from wx_session_refresh import WxSessionKeepAlive
     keeper = WxSessionKeepAlive(config_path="/path/to/config.yaml")
     success = keeper.refresh()

  3. 与 WeRSS 集成（容器内运行）：
     python3 wx_session_refresh.py --werss

配置文件格式（config.yaml）：
  cookie_file: /path/to/cookies.json     # JSON 格式的 cookies 存储
  token_file: /path/to/token.yaml        # YAML 格式的 token 存储
  browser_type: webkit                    # webkit / firefox / chromium
  browsers_path: /path/to/browsers       # Playwright 浏览器安装目录（可选）

crontab 部署：
  # 每6小时执行一次（4天有效期，6h刷新 = 16次安全余量）
  0 */6 * * * python3 /path/to/wx_session_refresh.py --config /path/to/config.yaml >> /path/to/refresh.log 2>&1
"""

import sys
import os
import time
import json
import re
import argparse
from datetime import datetime
from pathlib import Path


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [wx-refresh] {msg}")


class WxSessionKeepAlive:
    """微信公众平台会话保活器"""

    WX_BASE = "https://mp.weixin.qq.com"
    WX_HOME = "https://mp.weixin.qq.com/cgi-bin/home"

    def __init__(self, config=None, config_path=None):
        if config:
            self.config = config
        elif config_path:
            self.config = self._load_config(config_path)
        else:
            self.config = {}

        self.browser_type_name = self.config.get('browser_type', os.getenv('BROWSER_TYPE', 'webkit'))
        self.browsers_path = self.config.get('browsers_path', os.getenv('PLAYWRIGHT_BROWSERS_PATH', ''))

    @staticmethod
    def _load_config(path):
        import yaml
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}

    def load_session(self):
        """加载当前会话信息。返回 (token, cookies, expiry_info)。"""
        token = None
        cookies = None
        expiry_info = None

        token_file = self.config.get('token_file')
        if token_file and os.path.exists(token_file):
            import yaml
            with open(token_file, 'r') as f:
                data = yaml.safe_load(f)
            if data:
                token = str(data.get('token', ''))
                expiry_info = data.get('expiry', {})

        cookie_file = self.config.get('cookie_file')
        if cookie_file and os.path.exists(cookie_file):
            with open(cookie_file, 'r') as f:
                cookies = json.load(f)

        return token, cookies, expiry_info

    def save_session(self, cookies, token, expiry):
        """保存更新后的会话信息。安全检查：必须包含 slave_sid。"""
        if not self._has_slave_sid(cookies):
            log("WARNING: cookies 中无 slave_sid，跳过保存以保护现有会话")
            return

        cookie_file = self.config.get('cookie_file')
        if cookie_file:
            os.makedirs(os.path.dirname(cookie_file) or '.', exist_ok=True)
            with open(cookie_file, 'w') as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            log(f"OK: cookies 已保存到 {cookie_file}")

        token_file = self.config.get('token_file')
        if token_file:
            import yaml
            existing = {}
            if os.path.exists(token_file):
                with open(token_file, 'r') as f:
                    existing = yaml.safe_load(f) or {}

            cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies) + "; "

            existing['token'] = token
            existing['cookie'] = cookies_str
            if expiry:
                existing['expiry'] = expiry

            with open(token_file, 'w') as f:
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)
            log(f"OK: token 已保存到 {token_file}")

    def refresh(self):
        """执行会话续期。Returns: bool"""
        token, cookies, expiry_info = self.load_session()

        if not token:
            log("ERROR: 无有效 token，无法续期")
            return False

        old_expiry_ts = 0
        if expiry_info and expiry_info.get('expiry_time'):
            try:
                expire_dt = datetime.strptime(str(expiry_info['expiry_time']), '%Y-%m-%d %H:%M:%S')
                remaining = (expire_dt - datetime.now()).total_seconds() / 3600
                old_expiry_ts = float(expiry_info.get('expiry_timestamp', 0))
                log(f"当前会话: token={token}, 过期={expiry_info['expiry_time']}, 剩余={remaining:.1f}小时")
                if remaining <= 0:
                    log("CRITICAL: 会话已过期，需要重新扫码")
                    return False
            except ValueError:
                pass

        if not cookies:
            log("WARNING: 无 cookies，尝试仅用 token 续期")

        return self._do_refresh(token, cookies, old_expiry_ts)

    def _do_refresh(self, token, cookies, old_expiry_ts):
        """Playwright 浏览器刷新核心逻辑（Phase 1 + Phase 2）"""
        from playwright.sync_api import sync_playwright

        if self.browsers_path:
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = self.browsers_path

        wx_home_url = f"{self.WX_HOME}?t=home/index&lang=zh_CN&token={token}"

        log("启动 Playwright 浏览器...")
        playwright = None
        browser = None

        try:
            playwright = sync_playwright().start()

            browser_map = {
                'firefox': playwright.firefox,
                'webkit': playwright.webkit,
                'chromium': playwright.chromium
            }
            browser_type = browser_map.get(self.browser_type_name.lower(), playwright.webkit)
            log(f"使用浏览器: {self.browser_type_name}")

            browser = browser_type.launch(headless=True)
            context = browser.new_context(
                locale="zh-CN",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # 建立 domain
            log("打开微信公众平台...")
            page.goto(self.WX_BASE + "/", wait_until="domcontentloaded")
            time.sleep(2)

            # 注入 cookies
            if cookies:
                for c in cookies:
                    if 'domain' not in c:
                        c['domain'] = '.weixin.qq.com'
                    if 'path' not in c:
                        c['path'] = '/'
                context.add_cookies(cookies)
                log(f"已注入 {len(cookies)} 个 cookies")

            context.add_cookies([{
                "name": "token",
                "value": token,
                "domain": ".weixin.qq.com",
                "path": "/"
            }])

            # === Phase 1: 常规续期 ===
            log("Phase 1: 常规续期...")
            page.goto(wx_home_url, wait_until="domcontentloaded")
            time.sleep(3)

            current_url = page.url
            page_content = page.content()[:1000]

            if "login" in current_url.lower() or "使用账号登录" in page_content:
                log("CRITICAL: 会话已失效，页面跳转到了登录页。需要重新扫码。")
                return False

            log("OK: 登录状态有效")

            # 多页面交互
            extra_pages = [
                f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token={token}&lang=zh_CN",
                f"https://mp.weixin.qq.com/cgi-bin/message?t=message/list&count=20&day=7&token={token}&lang=zh_CN",
                wx_home_url,
            ]
            for i, extra_url in enumerate(extra_pages):
                try:
                    log(f"访问子页面 {i+1}/{len(extra_pages)}...")
                    page.goto(extra_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                except Exception as e:
                    log(f"WARNING: 子页面 {i+1} 访问失败: {e}")

            # 提取并检查 cookies
            new_cookies = self._extract_cookies(context)
            cookie_expiry = self._calc_expiry(new_cookies)

            phase1_success = False
            if cookie_expiry:
                new_expiry_ts = cookie_expiry.get('expiry_timestamp', 0)
                remaining_h = cookie_expiry['remaining_seconds'] / 3600
                log(f"新的过期时间: {cookie_expiry['expiry_time']} (剩余 {remaining_h:.1f} 小时)")

                if old_expiry_ts > 0 and new_expiry_ts > old_expiry_ts:
                    extended_h = (new_expiry_ts - old_expiry_ts) / 3600
                    log(f"Phase 1 OK: 过期时间已延长 {extended_h:.1f} 小时")
                    phase1_success = True
                elif old_expiry_ts > 0:
                    log(f"Phase 1 FAIL: 过期时间未延长 (旧={datetime.fromtimestamp(old_expiry_ts).strftime('%Y-%m-%d %H:%M')}, 新={cookie_expiry['expiry_time']})")
                else:
                    log("Phase 1 OK: 首次运行，已获取过期时间")
                    phase1_success = True
            else:
                log("Phase 1 FAIL: 未找到 slave_sid 过期信息")

            if phase1_success:
                new_token = token
                token_match = re.search(r'token=([^&]+)', current_url)
                if token_match:
                    new_token = token_match.group(1)

                self.save_session(new_cookies, new_token, cookie_expiry)
                log("会话续期成功！")
                return True

            # === Phase 2: 通过登录页入口尝试获取新 session ===
            log("Phase 2: 通过登录页入口重新触发 session...")

            page.goto(self.WX_BASE + "/", wait_until="networkidle", timeout=30000)
            time.sleep(5)

            redirected_url = page.url
            log(f"登录页重定向到: {redirected_url}")

            if "token=" in redirected_url:
                token_match = re.search(r'token=(\d+)', redirected_url)
                if token_match:
                    new_token = token_match.group(1)
                    log(f"获取到新 token: {new_token}")

                    new_home = f"{self.WX_HOME}?t=home/index&lang=zh_CN&token={new_token}"
                    page.goto(new_home, wait_until="domcontentloaded")
                    time.sleep(3)

                    for url in [
                        f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token={new_token}&lang=zh_CN",
                        new_home,
                    ]:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            time.sleep(2)
                        except Exception:
                            pass

                    phase2_cookies = self._extract_cookies(context)
                    phase2_expiry = self._calc_expiry(phase2_cookies)

                    if phase2_expiry and self._has_slave_sid(phase2_cookies):
                        remaining = phase2_expiry['remaining_seconds'] / 3600
                        log(f"Phase 2 OK: 新过期时间 {phase2_expiry['expiry_time']} (剩余 {remaining:.1f} 小时)")
                        self.save_session(phase2_cookies, new_token, phase2_expiry)
                        log("Phase 2 续期成功！")
                        return True

            # 两个 Phase 都失败，安全保存
            if self._has_slave_sid(new_cookies) and cookie_expiry:
                self.save_session(new_cookies, token, cookie_expiry)
                log("已保存 Phase 1 的 cookies（含 slave_sid，但未续期）")
            else:
                log("WARNING: 新 cookies 不含 slave_sid，保留原有 cookies 不覆盖")

            return False

        except Exception as e:
            log(f"ERROR: 刷新失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            try:
                if browser:
                    browser.close()
                if playwright:
                    playwright.stop()
            except:
                pass

    @staticmethod
    def _extract_cookies(context):
        """提取并去重 cookies"""
        raw_cookies = context.cookies()
        cookie_map = {}
        for c in raw_cookies:
            key = c['name']
            if key not in cookie_map or c.get('expires', 0) > cookie_map[key].get('expires', 0):
                cookie_map[key] = c
        new_cookies = list(cookie_map.values())
        log(f"提取到 {len(raw_cookies)} 个 cookies，去重后 {len(new_cookies)} 个")
        return new_cookies

    @staticmethod
    def _calc_expiry(cookies):
        """计算 slave_sid 的过期信息"""
        for c in cookies:
            if c.get('name') == 'slave_sid' and 'expires' in c:
                try:
                    expiry_time = float(c['expires'])
                    remaining = expiry_time - time.time()
                    if remaining > 0:
                        return {
                            'expiry_timestamp': expiry_time,
                            'remaining_seconds': int(remaining),
                            'expiry_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry_time))
                        }
                except (ValueError, TypeError):
                    pass
        return None

    @staticmethod
    def _has_slave_sid(cookies):
        """检查 cookies 是否包含 slave_sid"""
        return any(c.get('name') == 'slave_sid' for c in cookies)


class WeRSSSessionKeepAlive(WxSessionKeepAlive):
    """WeRSS 容器内专用的会话保活器，使用 WeRSS 内置的加密存储"""

    def __init__(self):
        super().__init__(config={
            'browser_type': os.getenv('BROWSER_TYPE', 'webkit'),
            'browsers_path': '/app/env/driver/_x86_64'
        })

    def load_session(self):
        import yaml

        wx_lic_path = '/app/data/wx.lic'
        if not os.path.exists(wx_lic_path):
            log("ERROR: wx.lic 不存在")
            return None, None, None

        with open(wx_lic_path, 'r') as f:
            wx_data = yaml.safe_load(f)

        if not wx_data or not wx_data.get('token'):
            log("ERROR: wx.lic 中无有效 token")
            return None, None, None

        token = str(wx_data['token'])
        expiry_info = wx_data.get('expiry', {})

        cookies = None
        try:
            from driver.store import Store
            cookies = Store.load()
            if cookies:
                log(f"从 key.lic 加载了 {len(cookies)} 个 cookies")
        except Exception as e:
            log(f"WARNING: 加载 key.lic 失败: {e}")

        return token, cookies, expiry_info

    def save_session(self, cookies, token, expiry):
        if not self._has_slave_sid(cookies):
            log("WARNING: cookies 中无 slave_sid，跳过保存以保护现有会话")
            return

        try:
            from driver.store import Store
            Store.save(cookies)
            log("OK: cookies 已保存到 key.lic")
        except Exception as e:
            log(f"ERROR: 保存 key.lic 失败: {e}")

        try:
            from driver.token import wx_cfg

            cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies) + "; "

            wx_cfg.config["token"] = token
            wx_cfg.config["cookie"] = cookies_str
            if expiry:
                wx_cfg.config["expiry"] = expiry
            wx_cfg.save_config()
            log("OK: wx.lic 已更新")
        except Exception as e:
            log(f"ERROR: 更新 wx.lic 失败: {e}")


def main():
    parser = argparse.ArgumentParser(description='微信公众平台会话自动续期')
    parser.add_argument('--config', help='配置文件路径（YAML）')
    parser.add_argument('--werss', action='store_true', help='WeRSS 容器内模式')
    args = parser.parse_args()

    log("=" * 50)
    log("微信会话续期开始")
    log("=" * 50)

    if args.werss:
        keeper = WeRSSSessionKeepAlive()
    elif args.config:
        keeper = WxSessionKeepAlive(config_path=args.config)
    else:
        if os.path.exists('/app/data/wx.lic'):
            keeper = WeRSSSessionKeepAlive()
        else:
            log("ERROR: 请指定 --config 或 --werss 参数")
            return 1

    success = keeper.refresh()

    if success:
        log("续期完成，会话已延长")
    else:
        log("续期失败！请尽快重新扫码登录")

    log("=" * 50)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
