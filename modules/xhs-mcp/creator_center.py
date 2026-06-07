#!/usr/bin/env python3
"""
小红书创作者中心 — 可复用模块

功能：
  1. 持久化 Chrome 实例管理（登录态跨服务重启保持）
  2. 短信验证码登录（CDP 鼠标事件模拟，绕过反爬）
  3. 笔记数据采集（Network 拦截，绕过 API 签名校验）
  4. 登录状态持久化标记

原理：
  XHS 创作者中心（creator.xiaohongshu.com）与主站（www.xiaohongshu.com）认证独立，
  仅支持短信登录。API 有签名校验，直接 fetch() 返回 code=-1。
  解决方案：CDP Network 拦截页面自身发起的已签名请求。

用法：
  from creator_center import CreatorCenter

  cc = CreatorCenter(user_data_dir='~/data/creator-center-chrome')
  port, err = cc.ensure_chrome()
  result, err = cc.start_login(phone='13800138000')
  result = cc.submit_code('123456')
  data, err = cc.fetch_views(['noteId1', 'noteId2'])
  logged_in = cc.is_logged_in()

依赖：
  - websocket-client（pip install websocket-client）— 仅 fetch_views 的 Network 拦截备用方案需要
  - Chromium 二进制（rod 自带或系统安装）
"""

import json
import os
import re
import subprocess
import time


# ── WebSocket 工具函数 ─────────────────────────────────────────────────────

def _ws_read_bytes(sock, n):
    """从 socket 精确读取 n 字节"""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('WebSocket 连接中断')
        buf += chunk
    return buf


class CdpConn:
    """持久 CDP WebSocket 连接，支持多次命令调用（无 Origin 头，绕过 Chrome 来源检查）"""

    def __init__(self, host, port, path):
        import socket as _socket, base64 as _b64, random as _rnd
        self._sock = _socket.create_connection((host, port), timeout=15)
        self._sock.settimeout(15)
        self._cmd_id = 0
        key = _b64.b64encode(bytes([_rnd.randint(0, 255) for _ in range(16)])).decode()
        handshake = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'\r\n'
        ).encode()
        self._sock.sendall(handshake)
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError('WebSocket 握手失败')
            resp += chunk
        if b'101' not in resp:
            raise ConnectionError('WebSocket 升级失败')

    def call(self, method, params=None, timeout=15):
        import struct as _st, random as _rnd
        self._cmd_id += 1
        cmd_id = self._cmd_id
        msg = json.dumps({'id': cmd_id, 'method': method, 'params': params or {}}).encode()
        mask = bytes([_rnd.randint(0, 255) for _ in range(4)])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
        n = len(msg)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0xFE]) + _st.pack('>H', n)
        else:
            header = bytes([0x81, 0xFF]) + _st.pack('>Q', n)
        self._sock.sendall(header + mask + masked)

        deadline = time.time() + timeout
        while time.time() < deadline:
            self._sock.settimeout(max(0.5, deadline - time.time()))
            hdr = _ws_read_bytes(self._sock, 2)
            op = hdr[0] & 0x0F
            plen = hdr[1] & 0x7F
            if plen == 126:
                plen = _st.unpack('>H', _ws_read_bytes(self._sock, 2))[0]
            elif plen == 127:
                plen = _st.unpack('>Q', _ws_read_bytes(self._sock, 8))[0]
            payload = _ws_read_bytes(self._sock, plen)
            if op == 8:
                raise ConnectionError('WebSocket closed')
            if op == 9:  # Ping → Pong
                self._sock.sendall(bytes([0x8A, 0x80, 0, 0, 0, 0]))
                continue
            if op not in (1, 0):
                continue
            try:
                data = json.loads(payload.decode('utf-8'))
            except Exception:
                continue
            if data.get('id') == cmd_id:
                return data
        raise TimeoutError(f'CDP 响应超时 ({timeout}s)')

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# ── Chrome 查找 ─────────────────────────────────────────────────────────────

def find_chromium():
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


# ── CreatorCenter 主类 ───────────────────────────────────────────────────────

class CreatorCenter:
    """小红书创作者中心管理器 — 持久化 Chrome 实例 + SMS 登录 + 数据采集"""

    def __init__(self, user_data_dir=None, cookies_path=None):
        """
        Args:
            user_data_dir: Chrome 用户数据目录（持久化登录态），默认 ~/data/creator-center-chrome
            cookies_path: XHS cookie 文件路径（可选，用于注入主站 cookies 辅助登录）
        """
        self.user_data_dir = os.path.expanduser(user_data_dir or '~/data/creator-center-chrome')
        self.cookies_path = cookies_path
        self._login_flag_path = os.path.join(self.user_data_dir, '.login_status.json')
        self._chrome_proc = None
        self._cdp_port = None

    # ── 登录状态持久化 ───────────────────────────────────────────────────────

    def save_login_flag(self, logged_in):
        """持久化保存登录状态"""
        try:
            os.makedirs(os.path.dirname(self._login_flag_path), exist_ok=True)
            with open(self._login_flag_path, 'w') as f:
                from datetime import datetime
                json.dump({'loggedIn': logged_in, 'updatedAt': datetime.now().isoformat()}, f)
        except Exception:
            pass

    def read_login_flag(self):
        """读取持久化的登录状态"""
        try:
            with open(self._login_flag_path, 'r') as f:
                data = json.load(f)
                return data.get('loggedIn', False)
        except Exception:
            return False

    def is_logged_in(self):
        """检查是否已登录（读持久化标记，不启动 Chrome）"""
        return self.read_login_flag()

    # ── Chrome 生命周期 ──────────────────────────────────────────────────────

    def _port_alive(self, port):
        """检查 CDP 端口是否存活"""
        import http.client
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            conn.request('GET', '/json/version')
            conn.getresponse().read()
            conn.close()
            return True
        except Exception:
            return False

    def ensure_chrome(self):
        """确保创作者中心专用 Chrome 实例正在运行。
        优先复用已有进程（服务重启后不丢失登录态）。
        Returns: (cdp_port, error_str)
        """
        # 1. 检查已知端口是否存活
        if self._cdp_port and self._port_alive(self._cdp_port):
            return self._cdp_port, None

        # 2. 扫描已存在的 creator-center Chrome（服务重启后复用）
        try:
            result = subprocess.run(
                ['pgrep', '-a', '-f', f'{os.path.basename(self.user_data_dir)}.*remote-debugging-port'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                m = re.search(r'--remote-debugging-port=(\d+)', line)
                if m:
                    port = int(m.group(1))
                    if self._port_alive(port):
                        self._cdp_port = port
                        print(f'[CreatorCenter] 复用已有 Chrome，CDP 端口: {port}')
                        return port, None
        except Exception:
            pass

        # 3. 启动新 Chrome（脱离 systemd cgroup，服务重启不杀它）
        chrome = find_chromium()
        if not chrome:
            return None, 'Chromium 未找到'

        os.makedirs(self.user_data_dir, exist_ok=True)

        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.bind(('', 0))
        port = s.getsockname()[1]
        s.close()

        self._chrome_proc = subprocess.Popen(
            [chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
             f'--remote-debugging-port={port}',
             f'--user-data-dir={self.user_data_dir}',
             '--remote-allow-origins=*',
             '--disable-background-timer-throttling',
             '--disable-backgrounding-occluded-windows',
             'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True  # 脱离父进程 process group
        )
        self._cdp_port = port

        for _ in range(15):
            if self._port_alive(port):
                print(f'[CreatorCenter] Chrome 已启动，CDP 端口: {port}')
                return port, None
            time.sleep(0.5)

        return None, 'Chrome 启动超时'

    def _get_page_ws(self, cdp_port):
        """获取 Chrome 第一个 page 标签页的 WebSocket 路径"""
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', cdp_port, timeout=5)
        conn.request('GET', '/json/list')
        tabs = json.loads(conn.getresponse().read())
        conn.close()
        for tab in tabs:
            if tab.get('type') == 'page' and 'webSocketDebuggerUrl' in tab:
                ws = tab['webSocketDebuggerUrl']
                return ws.split(f':{cdp_port}', 1)[-1]
        return None

    def _inject_cookies(self, cdp):
        """向 Chrome 注入 XHS cookies（辅助主站会话建立）"""
        if not self.cookies_path or not os.path.exists(self.cookies_path):
            return
        with open(self.cookies_path, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
        cdp.call('Network.enable')
        for c in cookies:
            name = c.get('name', c.get('Name', ''))
            value = c.get('value', c.get('Value', ''))
            domain = c.get('domain', c.get('Domain', '.xiaohongshu.com'))
            if name:
                cdp.call('Network.setCookie', {
                    'name': name, 'value': value, 'domain': domain,
                    'path': '/', 'secure': True,
                }, timeout=3)

    # ── SMS 登录流程 ─────────────────────────────────────────────────────────

    def start_login(self, phone=None):
        """启动创作者中心登录。
        如果传入 phone，自动填入手机号并用 CDP 鼠标事件发送验证码（绕过反爬）。
        Returns: (result_dict, error_str)
        """
        cdp_port, err = self.ensure_chrome()
        if not cdp_port:
            return None, err

        page_ws = self._get_page_ws(cdp_port)
        if not page_ws:
            return None, '无法获取浏览器页面'

        cdp = CdpConn('127.0.0.1', cdp_port, page_ws)
        try:
            cdp.call('Emulation.setDeviceMetricsOverride', {
                'width': 1280, 'height': 800, 'deviceScaleFactor': 1, 'mobile': False,
            })
            cdp.call('Page.enable')

            # 检查当前是否已登录
            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')
            if 'creator.xiaohongshu.com' in url and '/login' not in url:
                return {'loggedIn': True, 'url': url}, None

            # 导航到创作者中心
            if 'creator.xiaohongshu.com' not in url:
                cdp.call('Page.navigate', {'url': 'https://creator.xiaohongshu.com/'})
                time.sleep(5)

            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')
            if 'creator.xiaohongshu.com' in url and '/login' not in url:
                return {'loggedIn': True, 'url': url}, None

            # 在登录页：如果传入手机号，填入并发送验证码
            if phone:
                js_fill = """
                (function(){
                    var inputs = document.querySelectorAll('input');
                    var phoneInput = null;
                    for(var i=0;i<inputs.length;i++){
                        if(inputs[i].placeholder && inputs[i].placeholder.indexOf('手机号') >= 0){
                            phoneInput = inputs[i];
                            break;
                        }
                    }
                    if(!phoneInput) return 'no_phone_input';
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(phoneInput, '%s');
                    phoneInput.dispatchEvent(new Event('input', {bubbles: true}));
                    phoneInput.dispatchEvent(new Event('change', {bubbles: true}));
                    return 'phone_filled';
                })()
                """ % phone
                r = cdp.call('Runtime.evaluate', {'expression': js_fill, 'returnByValue': True})
                fill_result = r.get('result', {}).get('result', {}).get('value', '')
                print(f'[CreatorCenter] 填入手机号: {fill_result}')

                time.sleep(0.5)

                # CDP 鼠标事件模拟点击「发送验证码」（JS .click() 被反爬拦截）
                js_find_btn = """
                (function(){
                    var all = document.querySelectorAll('*');
                    var best = null;
                    for(var i=0;i<all.length;i++){
                        var t = all[i].textContent.trim();
                        if(t === '发送验证码' || t === '获取验证码'){
                            if(!best || all[i].getBoundingClientRect().width < best.getBoundingClientRect().width){
                                best = all[i];
                            }
                        }
                    }
                    if(!best) return JSON.stringify({found: false});
                    var r = best.getBoundingClientRect();
                    return JSON.stringify({found: true, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), text: best.textContent.trim()});
                })()
                """
                r = cdp.call('Runtime.evaluate', {'expression': js_find_btn, 'returnByValue': True})
                btn_info = json.loads(r.get('result', {}).get('result', {}).get('value', '{}'))
                if btn_info.get('found'):
                    bx, by = btn_info['x'], btn_info['y']
                    cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1})
                    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1})
                    send_result = f'mouse_clicked: {btn_info.get("text", "")} at ({bx},{by})'
                else:
                    send_result = 'no_send_btn'
                print(f'[CreatorCenter] 发送验证码: {send_result}')

                return {'loggedIn': False, 'step': 'code_sent', 'phone': phone, 'sendResult': send_result}, None

            return {'loggedIn': False, 'step': 'need_phone'}, None
        finally:
            cdp.close()

    def submit_code(self, code):
        """提交短信验证码完成登录。
        Returns: dict with loggedIn, url, error
        """
        cdp_port = self._cdp_port
        if not cdp_port:
            return {'loggedIn': False, 'error': '浏览器未启动'}

        page_ws = self._get_page_ws(cdp_port)
        if not page_ws:
            return {'loggedIn': False, 'error': '浏览器页面不可用'}

        cdp = CdpConn('127.0.0.1', cdp_port, page_ws)
        try:
            # 填入验证码
            js_code_fill = """
            (function(){
                var inputs = document.querySelectorAll('input');
                var codeInput = null;
                for(var i=0;i<inputs.length;i++){
                    var ph = inputs[i].placeholder || '';
                    if(ph.indexOf('验证码') >= 0 || ph.indexOf('code') >= 0){
                        codeInput = inputs[i];
                        break;
                    }
                }
                if(!codeInput){
                    if(inputs.length >= 2) codeInput = inputs[1];
                }
                if(!codeInput) return 'no_code_input';
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(codeInput, '%s');
                codeInput.dispatchEvent(new Event('input', {bubbles: true}));
                codeInput.dispatchEvent(new Event('change', {bubbles: true}));
                return 'code_filled';
            })()
            """ % code
            r = cdp.call('Runtime.evaluate', {'expression': js_code_fill, 'returnByValue': True})
            fill_r = r.get('result', {}).get('result', {}).get('value', '')
            print(f'[CreatorCenter] 填入验证码: {fill_r}')

            time.sleep(0.3)

            # CDP 鼠标事件模拟点击「登录」按钮
            js_find_login = """
            (function(){
                var btns = document.querySelectorAll('button, div');
                for(var i=0;i<btns.length;i++){
                    var t = btns[i].textContent.trim();
                    if(t === '登录' || t === '登 录'){
                        var r = btns[i].getBoundingClientRect();
                        if(r.width > 0 && r.height > 0){
                            return JSON.stringify({found: true, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), text: t});
                        }
                    }
                }
                return JSON.stringify({found: false});
            })()
            """
            r = cdp.call('Runtime.evaluate', {'expression': js_find_login, 'returnByValue': True})
            btn_info = json.loads(r.get('result', {}).get('result', {}).get('value', '{}'))
            if btn_info.get('found'):
                bx, by = btn_info['x'], btn_info['y']
                cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1})
                cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1})

            time.sleep(5)

            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')
            logged_in = 'creator.xiaohongshu.com' in url and '/login' not in url
            print(f'[CreatorCenter] 登录后 URL: {url}, logged_in={logged_in}')

            if logged_in:
                self.save_login_flag(True)

            return {'loggedIn': logged_in, 'url': url}
        except Exception as e:
            return {'loggedIn': False, 'error': str(e)}
        finally:
            cdp.close()

    def check_login(self):
        """检查创作者中心登录状态（通过 Chrome URL 判断）"""
        cdp_port = self._cdp_port
        if not cdp_port:
            self.ensure_chrome()
            cdp_port = self._cdp_port
        if not cdp_port:
            return {'loggedIn': False}

        page_ws = self._get_page_ws(cdp_port)
        if not page_ws:
            return {'loggedIn': False}

        cdp = CdpConn('127.0.0.1', cdp_port, page_ws)
        try:
            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')
            if 'creator.xiaohongshu.com' in url and '/login' not in url:
                return {'loggedIn': True, 'url': url}
            if '/login' in url:
                return {'loggedIn': False}
            return {'loggedIn': False, 'hasBrowser': True}
        except Exception:
            return {'loggedIn': False}
        finally:
            cdp.close()

    # ── 数据采集 ─────────────────────────────────────────────────────────────

    def fetch_views(self, note_ids):
        """获取笔记浏览量等数据。
        策略：先尝试 DOM/Vue 提取，失败后用 Network 拦截。
        Returns: (dict{noteId: {views, likes, comments, collects, shares}}, error_str)
        """
        cdp_port, err = self.ensure_chrome()
        if not cdp_port:
            return None, err

        page_ws = self._get_page_ws(cdp_port)
        if not page_ws:
            return None, '浏览器页面不可用'

        cdp = CdpConn('127.0.0.1', cdp_port, page_ws)
        try:
            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')

            # 导航到笔记管理页
            cdp.call('Page.enable')
            if 'note-manager' in url:
                cdp.call('Page.reload')
                time.sleep(6)
            else:
                cdp.call('Page.navigate', {'url': 'https://creator.xiaohongshu.com/new/note-manager'})
                time.sleep(8)

            # 检查是否被重定向到登录页
            url_r = cdp.call('Runtime.evaluate', {'expression': 'window.location.href', 'returnByValue': True})
            url = url_r.get('result', {}).get('result', {}).get('value', '')
            if '/login' in url:
                return None, '创作者中心未登录'

            # 尝试 Vue/DOM 提取
            js_extract = r'''
            (function(){
                var noteIds = %s;
                var results = {};
                var vueRoot = document.querySelector('#app') || document.querySelector('[id*=app]');
                if (vueRoot) {
                    var vueApp = vueRoot.__vue_app__ || vueRoot.__vue__;
                    if (vueApp) {
                        var walkVue = function(node, depth) {
                            if (depth > 10) return;
                            var data = node.data || node.$data || node.setupState || {};
                            var lists = data.noteList || data.notes || data.list || [];
                            if (lists && lists.length) {
                                for (var k = 0; k < lists.length; k++) {
                                    var n = lists[k];
                                    var nid = n.id || n.note_id || n.noteId || '';
                                    if (noteIds.indexOf(nid) >= 0) {
                                        results[nid] = {
                                            views: n.view_count || n.viewCount || n.imp_count || 0,
                                            likes: n.likes || n.like_count || n.likeCount || 0,
                                            comments: n.comments_count || n.comment_count || n.commentCount || 0,
                                            collects: n.collected_count || n.collect_count || n.collectCount || 0,
                                            shares: n.shared_count || n.share_count || n.shareCount || 0,
                                            source: 'vue_data'
                                        };
                                    }
                                }
                            }
                            var children = node.subTree ? [node.subTree] : (node.children || []);
                            if (node.component) children.push(node.component);
                            for (var c = 0; c < children.length; c++) {
                                if (children[c]) walkVue(children[c], depth + 1);
                            }
                        };
                        try { walkVue(vueApp._instance || vueApp, 0); } catch(e) {}
                    }
                }
                return JSON.stringify(results);
            })()
            ''' % json.dumps(note_ids)

            resp = cdp.call('Runtime.evaluate', {
                'expression': js_extract,
                'returnByValue': True,
            }, timeout=20)

            value = resp.get('result', {}).get('result', {}).get('value', '{}')
            result = json.loads(value) if isinstance(value, str) else value
            print(f'[CreatorCenter] DOM 提取结果: {json.dumps(result, ensure_ascii=False)[:500]}')

            clean = {}
            for k, v in result.items():
                if not k.startswith('_') and isinstance(v, dict) and 'views' in v:
                    clean[k] = v

            if clean:
                return clean, None

            # Vue 提取失败，用 Network 拦截
            return self._fetch_views_network(note_ids, cdp_port)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return None, str(e)
        finally:
            cdp.close()

    def _fetch_views_network(self, note_ids, cdp_port):
        """备用方案：用 websocket-client 库连接 CDP，拦截 Network 响应获取数据"""
        try:
            import websocket as _wslib
        except ImportError:
            return None, 'websocket-client 未安装'

        page_ws = self._get_page_ws(cdp_port)
        if not page_ws:
            return None, '浏览器页面不可用'

        ws_url = f'ws://127.0.0.1:{cdp_port}{page_ws}'
        ws = _wslib.create_connection(ws_url, suppress_origin=True)
        msg_id = 100
        try:
            def send_cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                ws.send(json.dumps({'id': msg_id, 'method': method, 'params': params or {}}))
                return msg_id

            send_cmd('Network.enable')
            send_cmd('Page.enable')
            time.sleep(0.3)
            # Drain pending messages
            ws.settimeout(0.5)
            try:
                while True:
                    ws.recv()
            except Exception:
                pass

            # Navigate to note manager
            send_cmd('Page.navigate', {'url': 'https://creator.xiaohongshu.com/new/note-manager'})

            # Listen for posted API response
            posted_req_id = None
            deadline = time.time() + 15
            ws.settimeout(1)
            while time.time() < deadline:
                try:
                    msg = json.loads(ws.recv())
                    if msg.get('method') == 'Network.responseReceived':
                        resp_url = msg.get('params', {}).get('response', {}).get('url', '')
                        if 'posted' in resp_url and 'galaxy' in resp_url:
                            posted_req_id = msg['params']['requestId']
                            print(f'[CreatorCenter] Network 捕获: {resp_url}')
                            time.sleep(1)
                            break
                except Exception:
                    pass

            if not posted_req_id:
                return None, '未能捕获笔记列表 API'

            # Get response body
            send_cmd('Network.getResponseBody', {'requestId': posted_req_id})
            ws.settimeout(5)
            body_str = '{}'
            deadline2 = time.time() + 5
            while time.time() < deadline2:
                try:
                    resp = json.loads(ws.recv())
                    if resp.get('result', {}).get('body'):
                        body_str = resp['result']['body']
                        break
                except Exception:
                    pass

            api_data = json.loads(body_str)
            notes = api_data.get('data', {}).get('notes', [])
            print(f'[CreatorCenter] Network 获取 {len(notes)} 条笔记')

            note_ids_set = set(note_ids)
            results = {}
            for note in notes:
                nid = note.get('id', '')
                if nid in note_ids_set:
                    results[nid] = {
                        'views': note.get('view_count', 0),
                        'likes': note.get('likes', 0),
                        'comments': note.get('comments_count', 0),
                        'collects': note.get('collected_count', 0),
                        'shares': note.get('shared_count', 0),
                        'source': 'network_intercept'
                    }
            return results, None
        except Exception as e:
            return None, str(e)
        finally:
            ws.close()


# ── 模块级便捷函数（单例模式）─────────────────────────────────────────────────

_default_instance = None


def get_default(user_data_dir=None, cookies_path=None):
    """获取默认的 CreatorCenter 单例"""
    global _default_instance
    if _default_instance is None:
        _default_instance = CreatorCenter(user_data_dir=user_data_dir, cookies_path=cookies_path)
    return _default_instance


if __name__ == '__main__':
    import sys
    cc = CreatorCenter()
    if len(sys.argv) > 1 and sys.argv[1] == 'login':
        phone = sys.argv[2] if len(sys.argv) > 2 else None
        result, err = cc.start_login(phone=phone)
        print(json.dumps(result or {'error': err}, indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == 'code':
        code = sys.argv[2]
        result = cc.submit_code(code)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == 'status':
        result = cc.check_login()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        note_ids = sys.argv[1:] or []
        if note_ids:
            data, err = cc.fetch_views(note_ids)
            print(json.dumps(data or {'error': err}, indent=2, ensure_ascii=False))
        else:
            print('用法:')
            print('  python creator_center.py login [phone]    — 启动登录')
            print('  python creator_center.py code <code>      — 提交验证码')
            print('  python creator_center.py status           — 检查登录状态')
            print('  python creator_center.py <noteId> ...     — 获取笔记数据')
