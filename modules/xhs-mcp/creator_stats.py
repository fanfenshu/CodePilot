#!/usr/bin/env python3
"""
小红书创作者中心数据采集 — 获取笔记浏览量等创作者专属数据

原理：
  XHS 创作者中心 API（creator.xiaohongshu.com）需要浏览器级别的认证，
  普通 cookie 请求会返回 401。通过 CDP 控制 Chromium：
  1. 启动 headless Chromium（复用 rod 自带的二进制）
  2. 注入 cookies（从 MCP 的 cookie 文件加载）
  3. 先访问 www.xiaohongshu.com 建立会话
  4. 再访问 creator.xiaohongshu.com 获取创作者数据
  5. 通过 JavaScript 调用创作者 API 并返回结果

依赖：
  - websocket-client（pip install websocket-client）
  - Chromium 二进制（rod 自带或系统安装）
"""

import json
import os
import subprocess
import socket
import time

def _find_chromium():
    """查找可用的 Chromium 二进制"""
    candidates = [
        os.path.expanduser('~/.cache/rod/browser/chromium-1321438/chrome'),
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/usr/bin/google-chrome',
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _load_cookies(cookies_path):
    """从 MCP cookie 文件加载 cookies"""
    with open(cookies_path, 'r', encoding='utf-8') as f:
        cookies = json.load(f)
    # 转换为 CDP Network.setCookie 格式
    cdp_cookies = []
    for c in cookies:
        name = c.get('name', c.get('Name', ''))
        value = c.get('value', c.get('Value', ''))
        domain = c.get('domain', c.get('Domain', '.xiaohongshu.com'))
        if not name:
            continue
        cdp_cookies.append({
            'name': name,
            'value': value,
            'domain': domain,
            'path': c.get('path', c.get('Path', '/')),
            'secure': c.get('secure', c.get('Secure', True)),
            'httpOnly': c.get('httpOnly', c.get('HttpOnly', False)),
        })
    return cdp_cookies


def _cdp_call(ws, method, params=None, timeout=15):
    """通过 WebSocket 发送 CDP 命令并等待响应"""
    import websocket
    _cdp_call._id = getattr(_cdp_call, '_id', 0) + 1
    msg = json.dumps({'id': _cdp_call._id, 'method': method, 'params': params or {}})
    ws.send(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.settimeout(min(2, deadline - time.time()))
            resp = json.loads(ws.recv())
            if resp.get('id') == _cdp_call._id:
                return resp
        except:
            pass
    return None


def fetch_creator_note_stats(note_ids, cookies_path=None, timeout=30):
    """
    获取笔记的创作者数据（浏览量等）

    Args:
        note_ids: 笔记 ID 列表
        cookies_path: cookie 文件路径，默认使用环境变量 COOKIES_PATH
        timeout: 超时秒数

    Returns:
        dict: {noteId: {views, likes, comments, collects, shares, ...}}
              或 None（失败时）
    """
    import websocket

    if not cookies_path:
        cookies_path = os.environ.get('COOKIES_PATH',
                       os.path.expanduser('~/.xhs-cookies/acc_5f90a7.json'))

    chrome = _find_chromium()
    if not chrome:
        print('[CreatorStats] Chromium not found')
        return None

    if not os.path.exists(cookies_path):
        print(f'[CreatorStats] Cookie file not found: {cookies_path}')
        return None

    cdp_port = _find_free_port()
    user_data = f'/tmp/xhs-creator-{os.getpid()}'

    # 启动 headless Chromium
    proc = subprocess.Popen(
        [chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
         f'--remote-debugging-port={cdp_port}',
         f'--user-data-dir={user_data}',
         '--remote-allow-origins=*',
         '--disable-background-timer-throttling',
         '--disable-backgrounding-occluded-windows',
         'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    result = None
    try:
        # 等待 CDP 端口就绪
        for _ in range(20):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(('127.0.0.1', cdp_port))
                s.close()
                break
            except:
                time.sleep(0.3)

        # 获取 WebSocket URL
        import urllib.request
        tabs_resp = urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json', timeout=5)
        tabs = json.loads(tabs_resp.read())
        ws_url = tabs[0]['webSocketDebuggerUrl']

        # 连接 CDP
        ws = websocket.create_connection(ws_url, timeout=10)

        # 注入 cookies
        _cdp_call(ws, 'Network.enable')
        cookies = _load_cookies(cookies_path)
        for cookie in cookies:
            _cdp_call(ws, 'Network.setCookie', cookie, timeout=3)

        # 先访问 xiaohongshu.com 建立会话
        _cdp_call(ws, 'Page.enable')
        _cdp_call(ws, 'Page.navigate', {'url': 'https://www.xiaohongshu.com/'})
        time.sleep(5)

        # 检查实际 cookie 状态
        cookie_resp = _cdp_call(ws, 'Network.getCookies', {'urls': ['https://www.xiaohongshu.com/', 'https://creator.xiaohongshu.com/']})
        if cookie_resp and 'result' in cookie_resp:
            browser_cookies = cookie_resp['result'].get('cookies', [])
            print(f'[CreatorStats] Browser has {len(browser_cookies)} cookies:')
            for bc in browser_cookies:
                print(f'  {bc["name"]}: domain={bc.get("domain","")}, value={str(bc.get("value",""))[:20]}...')

        # 检查当前页面是否已登录
        login_check = _cdp_call(ws, 'Runtime.evaluate', {
            'expression': 'document.title + " | " + window.location.href',
            'returnByValue': True,
        })
        if login_check and 'result' in login_check:
            title = login_check['result'].get('result', {}).get('value', '')
            print(f'[CreatorStats] Page: {title}')

        # 导航到 creator.xiaohongshu.com
        _cdp_call(ws, 'Page.navigate', {'url': 'https://creator.xiaohongshu.com/'})
        time.sleep(5)

        # 检查创作者中心页面状态
        creator_check = _cdp_call(ws, 'Runtime.evaluate', {
            'expression': 'document.title + " | " + window.location.href',
            'returnByValue': True,
        })
        if creator_check and 'result' in creator_check:
            title = creator_check['result'].get('result', {}).get('value', '')
            print(f'[CreatorStats] Creator page: {title}')

        # 通过 JavaScript fetch 调用创作者 API（现在是同源请求）
        js_code = '''
        (async function() {
            try {
                const noteIds = %s;
                const results = {};

                // 测试多个 API 端点
                const endpoints = [
                    '/api/galaxy/creator/home/personal_info',
                    '/api/galaxy/creator/data/overview',
                    '/api/galaxy/creator/note/list?page=1&page_size=10',
                ];

                for (const ep of endpoints) {
                    try {
                        const resp = await fetch(ep, {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        const text = await resp.text();
                        results['_' + ep.split('/').pop().split('?')[0]] = {
                            status: resp.status,
                            body: text.substring(0, 500)
                        };
                    } catch(e) {
                        results['_' + ep.split('/').pop().split('?')[0]] = {error: e.message};
                    }
                }

                // 单个笔记数据
                for (const noteId of noteIds) {
                    const tryEndpoints = [
                        '/api/galaxy/creator/data/note_detail?noteId=' + noteId,
                        '/api/galaxy/creator/data/note_stats?noteId=' + noteId + '&days=7',
                    ];
                    for (const ep of tryEndpoints) {
                        try {
                            const resp = await fetch(ep, {
                                credentials: 'include',
                                headers: {'Accept': 'application/json'}
                            });
                            const data = await resp.json();
                            if (data.success !== false && data.code !== -1) {
                                results[noteId] = data;
                                break;
                            }
                            results[noteId + '_' + ep.split('/').pop().split('?')[0]] = data;
                        } catch(e) {
                            results[noteId + '_err'] = e.message;
                        }
                    }
                }

                return JSON.stringify(results);
            } catch(e) {
                return JSON.stringify({error: e.message, stack: e.stack});
            }
        })()
        ''' % json.dumps(note_ids)

        resp = _cdp_call(ws, 'Runtime.evaluate', {
            'expression': js_code,
            'awaitPromise': True,
            'timeout': timeout * 1000,
        }, timeout=timeout + 5)

        if resp and 'result' in resp:
            value = resp['result'].get('result', {}).get('value', '{}')
            result = json.loads(value) if isinstance(value, str) else value

        ws.close()
    except Exception as e:
        print(f'[CreatorStats] Error: {e}')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except:
            proc.kill()
        # 清理临时 user-data
        subprocess.run(['rm', '-rf', user_data], capture_output=True)

    return result


if __name__ == '__main__':
    import sys
    note_ids = sys.argv[1:] or ['69ac6c46000000001a03257b']
    cookies = os.environ.get('COOKIES_PATH', os.path.expanduser('~/.xhs-cookies/acc_5f90a7.json'))
    print(f'Fetching creator stats for {note_ids}...')
    stats = fetch_creator_note_stats(note_ids, cookies)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
