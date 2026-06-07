#!/usr/bin/env python3
"""
小红书 MCP 集成模块 — 可复用的 MCP 通信、登录管理、搜索与发布

通过 xiaohongshu-mcp 服务（Streamable HTTP MCP 协议）实现：
1. MCP 通信层：curl Popen 流式 SSE 读取，session 缓存与自动重试
2. 进程管理：自动下载二进制、启动/健康检查/清理 MCP 进程
3. 搜索功能：搜索小红书账号，获取粉丝数，生成 Markdown 表格
4. 发布功能：通过 MCP 发布小红书笔记（图文），完整的登录检查与错误处理

依赖：
  - curl 命令行工具
  - xiaohongshu-mcp Go 二进制（可自动下载）

用法：
  from xhs_mcp import XhsMcp

  mcp = XhsMcp(port=18060)

  # 调用任意 MCP 工具
  result = mcp.call('check_login_status')

  # 搜索并构建表格
  table_md, glm_context = mcp.search_accounts(['跨境电商私域'])

  # 发布笔记
  ok, msg = mcp.publish(title='标题', content='正文', images=['/path/to/img.jpg'])
"""

import json
import os
import re
import subprocess
import threading
import time
import concurrent.futures


# ── MCP 进程管理全局状态 ─────────────────────────────────────────────────────

_session_ids = {}   # {port: session_id}
_processes = {}     # {port: subprocess.Popen}
_status_cache = {}  # {port: {'result': dict, 'time': float, 'checking': bool}}

# MCP 二进制路径：优先 ~/bin/，回退到本模块目录下 bin/
_BIN_CANDIDATES = [
    os.path.expanduser('~/bin/xiaohongshu-mcp'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'xiaohongshu-mcp'),
]
_BIN = next((p for p in _BIN_CANDIDATES if os.path.isfile(p) and os.access(p, os.X_OK)), _BIN_CANDIDATES[-1])
_COOKIES_DIR = os.path.expanduser('~/.xhs-cookies')


# ═══════════════════════════════════════════════════════════════════════════════
# 低层 MCP 通信
# ═══════════════════════════════════════════════════════════════════════════════

def mcp_post(url, body_dict, session_id=None, timeout=60):
    """向 MCP 服务发送 POST，返回 (parsed_json, new_session_id)。
    使用 curl --no-buffer + Popen 流式读取，SSE 拿到第一个 data: 结果即返回。
    """
    cmd = [
        'curl', '-s', '-N',  # -N = --no-buffer
        '-D', '/dev/stderr',  # headers → stderr, body → stdout
        '-X', 'POST', url,
        '-H', 'Content-Type: application/json',
        '-H', 'Accept: application/json, text/event-stream',
        '-d', json.dumps(body_dict),
        '-m', str(timeout),
    ]
    if session_id:
        cmd += ['-H', f'Mcp-Session-Id: {session_id}']

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # stderr 异步读取 headers（线程避免死锁）
        stderr_lines = []
        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()

        # stdout 流式读取 body
        body_lines = []
        result_json = None
        deadline = time.time() + timeout

        for line in proc.stdout:
            body_lines.append(line)
            stripped = line.strip()

            # SSE data: 行
            if stripped.startswith('data:'):
                ds = stripped[5:].strip()
                if ds and ds != '[DONE]':
                    try:
                        result_json = json.loads(ds)
                        break
                    except Exception:
                        pass

            # 纯 JSON 响应（非 SSE）
            if stripped.startswith('{') and stripped.endswith('}'):
                try:
                    result_json = json.loads(stripped)
                    break
                except Exception:
                    pass

            if time.time() > deadline:
                break

        # 立即杀掉 curl（不等 SSE 连接关闭）
        try:
            proc.kill()
        except Exception:
            pass
        t.join(timeout=2)

        # 提取 session ID
        new_sid = None
        for hline in stderr_lines:
            if hline.lower().startswith('mcp-session-id:'):
                new_sid = hline.split(':', 1)[1].strip().rstrip('\r')
                break

        if result_json:
            return result_json, new_sid

        full_body = ''.join(body_lines).strip()
        if not full_body:
            return None, new_sid
        return json.loads(full_body), new_sid

    except json.JSONDecodeError as e:
        raise ConnectionError(f'MCP 返回无效 JSON: {e}')
    except Exception as e:
        raise ConnectionError(str(e))
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def mcp_ensure_session(url, port):
    """确保 MCP 握手完成（initialize + notifications/initialized），返回 session_id。"""
    sid = _session_ids.get(port)
    if sid:
        return sid
    # Step 1: initialize
    _, new_sid = mcp_post(url, {
        'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'xhs-mcp-module', 'version': '2.0'},
        },
    })
    if new_sid:
        _session_ids[port] = new_sid
        sid = new_sid
    # Step 2: notifications/initialized
    mcp_post(url, {
        'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {},
    }, session_id=sid)
    return sid


def mcp_call(tool_name, arguments=None, port=18060, timeout=60):
    """调用 MCP 工具，自动握手 + session 缓存 + 过期重试。"""
    url = f'http://localhost:{port}/mcp'
    try:
        sid = mcp_ensure_session(url, port)
    except ConnectionError as e:
        raise ConnectionError(f'无法连接到 xiaohongshu-mcp 服务（端口 {port}）: {e}')

    try:
        result, new_sid = mcp_post(url, {
            'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': arguments or {}},
        }, session_id=sid, timeout=timeout)
        if new_sid:
            _session_ids[port] = new_sid
        # session 过期 → 清缓存重试
        if result and result.get('error'):
            _session_ids.pop(port, None)
            sid = mcp_ensure_session(url, port)
            result, new_sid = mcp_post(url, {
                'jsonrpc': '2.0', 'id': 1,
                'method': 'tools/call',
                'params': {'name': tool_name, 'arguments': arguments or {}},
            }, session_id=sid, timeout=timeout)
            if new_sid:
                _session_ids[port] = new_sid
        return result
    except ConnectionError as e:
        raise ConnectionError(f'无法连接到 xiaohongshu-mcp 服务（端口 {port}）: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# 结果解析
# ═══════════════════════════════════════════════════════════════════════════════

def mcp_text(result):
    """从 MCP 工具结果中提取文本内容。"""
    if not result:
        return ''
    items = (result.get('result') or {}).get('content', [])
    return ' '.join(c.get('text', '') for c in items if c.get('type') == 'text')


def mcp_image(result):
    """从 MCP 工具结果中提取第一个图片（base64, mimeType）。"""
    if not result:
        return None, None
    items = (result.get('result') or {}).get('content', [])
    for c in items:
        if c.get('type') == 'image':
            return c.get('data'), c.get('mimeType', 'image/png')
    return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# 进程管理
# ═══════════════════════════════════════════════════════════════════════════════

def is_port_listening(port):
    """检查本地端口是否已有进程在监听。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except OSError:
            return False


def health_check(port, timeout=8):
    """检查 MCP 是否能响应 initialize 请求。返回 True=健康。"""
    try:
        body = json.dumps({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                           'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                      'clientInfo': {'name': 'healthcheck', 'version': '0.1'}}})
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', f'http://127.0.0.1:{port}/mcp',
             '-H', 'Content-Type: application/json',
             '-H', 'Accept: application/json, text/event-stream',
             '-d', body, '-m', str(timeout)],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return '"result"' in r.stdout
    except Exception:
        return False


def kill_processes(port):
    """强制杀掉 MCP 进程及其子进程树。"""
    proc = _processes.pop(port, None)
    if proc:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
    try:
        subprocess.run(['fuser', '-k', f'{port}/tcp'], timeout=5, capture_output=True)
    except Exception:
        pass
    _session_ids.pop(port, None)
    time.sleep(1)


def cleanup_after_publish(port):
    """发布完成后杀掉 MCP + Chromium 释放内存（1.6GB 服务器友好）。"""
    print(f'[MCP-CLEANUP] 发布完成，清理 MCP 和 Chromium 释放内存')
    kill_processes(port)
    for pattern in ['rod/browser/chromium', 'leakless']:
        try:
            subprocess.run(['pkill', '-9', '-f', pattern], timeout=5, capture_output=True)
        except Exception:
            pass
    time.sleep(1)
    try:
        mem = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=5)
        if mem.stdout:
            print(f'[MCP-CLEANUP] 内存状态:\n{mem.stdout.strip()}')
    except Exception:
        pass


def _download_url():
    """根据系统/架构返回 GitHub 下载 URL。"""
    import platform
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == 'darwin':
        arch = 'arm64' if ('arm' in machine or 'aarch' in machine) else 'amd64'
        fname = f'xiaohongshu-mcp-darwin-{arch}.tar.gz'
    elif system == 'linux':
        arch = 'arm64' if ('arm' in machine or 'aarch' in machine) else 'amd64'
        fname = f'xiaohongshu-mcp-linux-{arch}.tar.gz'
    else:
        return None, f'暂不支持 {platform.system()} 系统'
    return f'https://github.com/xpzouying/xiaohongshu-mcp/releases/latest/download/{fname}', None


def download_binary():
    """自动下载 xiaohongshu-mcp 二进制。先尝试代理，失败则直连。"""
    import tarfile
    import urllib.request
    url, err = _download_url()
    if not url:
        return False, err

    os.makedirs(os.path.dirname(_BIN), exist_ok=True)
    tmp_tgz = _BIN + '.tar.gz'

    def _fetch(proxy_url=None):
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.79'})
        with opener.open(req, timeout=120) as resp, open(tmp_tgz, 'wb') as f:
            f.write(resp.read())

    proxy_candidates = []
    sys_proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')
    if sys_proxy:
        proxy_candidates.append(sys_proxy)
    if 'http://127.0.0.1:7890' not in proxy_candidates:
        proxy_candidates.append('http://127.0.0.1:7890')
    proxy_candidates.append(None)

    last_err = None
    for proxy in proxy_candidates:
        try:
            _fetch(proxy)
            with tarfile.open(tmp_tgz, 'r:gz') as tar:
                binary_member = next(
                    (m for m in tar.getmembers()
                     if os.path.basename(m.name).startswith('xiaohongshu-mcp')
                     and 'login' not in os.path.basename(m.name)
                     and m.isfile()),
                    None
                )
                if not binary_member:
                    raise RuntimeError('tar.gz 中未找到 xiaohongshu-mcp 二进制')
                src = tar.extractfile(binary_member)
                with open(_BIN, 'wb') as dst:
                    dst.write(src.read())
            os.chmod(_BIN, 0o755)
            os.remove(tmp_tgz)
            print(f'[MCP] 下载完成: {_BIN}（via {proxy or "direct"}）')
            return True, None
        except Exception as e:
            last_err = e
            if os.path.exists(tmp_tgz):
                try: os.remove(tmp_tgz)
                except: pass

    return False, f'下载失败（已尝试代理+直连）: {last_err}'


def ensure_running(port, cookies_path=None):
    """确保 MCP 进程在指定端口运行。返回 (ok, error_msg)。"""
    proc = _processes.get(port)
    if proc is not None and proc.poll() is None:
        return True, None

    if is_port_listening(port):
        print(f'[MCP] 端口 {port} 已在监听，跳过启动')
        return True, None

    if not os.path.isfile(_BIN):
        print(f'[MCP] 二进制不存在，开始自动下载…')
        ok, err = download_binary()
        if not ok:
            return False, f'自动下载 xiaohongshu-mcp 失败：{err}'

    if not os.access(_BIN, os.X_OK):
        os.chmod(_BIN, 0o755)

    # cookies 路径处理
    if not cookies_path:
        cookies_path = os.path.join(_COOKIES_DIR, 'default.json')
    expanded = os.path.expanduser(cookies_path)
    if expanded.startswith('/Users/') and not os.path.exists('/Users'):
        expanded = os.path.join(_COOKIES_DIR, os.path.basename(expanded))
    os.makedirs(os.path.dirname(os.path.abspath(expanded)), exist_ok=True)

    env = os.environ.copy()
    env['COOKIES_PATH'] = expanded

    # 登录持久化机制：MCP 通过 COOKIES_PATH 环境变量管理登录态——
    # 登录成功后自动将 cookies 保存到该文件，重启时从文件加载恢复登录。
    # -rod dir=... 仅指定 Chromium 用户数据目录，对登录持久化无实际作用。
    _profile_dir = os.path.expanduser('~/data/xhs-mcp/browser-profile')
    os.makedirs(_profile_dir, exist_ok=True)

    try:
        new_proc = subprocess.Popen(
            [_BIN, f'--port=:{port}', '-rod', f'dir={_profile_dir}'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _processes[port] = new_proc
        time.sleep(1.5)
        if new_proc.poll() is not None:
            return False, f'MCP 进程启动后立即退出（退出码 {new_proc.poll()}），端口 {port} 可能被占用'
        return True, None
    except Exception as e:
        return False, str(e)


# ═══════════════════════════════════════════════════════════════════════════════
# 状态查询（带缓存，防轮询堆积）
# ═══════════════════════════════════════════════════════════════════════════════

def check_status(port=18060, cache_seconds=8):
    """检查 MCP 运行状态和登录状态。带缓存防止轮询堆积。
    返回 {'running': bool, 'loggedIn': bool, 'message': str}
    """
    if not is_port_listening(port):
        return {'running': False, 'loggedIn': False, 'message': 'MCP 未运行'}

    # 检查缓存
    cache = _status_cache.get(port, {})
    now = time.time()
    if cache.get('result') and (now - cache.get('time', 0)) < cache_seconds:
        return cache['result']

    # 并发保护
    if cache.get('checking'):
        return cache.get('result') or {'running': True, 'loggedIn': False, 'message': '正在检查登录状态...'}

    _status_cache[port] = {'result': cache.get('result'), 'time': cache.get('time', 0), 'checking': True}

    try:
        result = mcp_call('check_login_status', port=port, timeout=30)
        text = mcp_text(result)
        logged_in = any(w in text for w in ['已登录', '登录状态', 'logged in', 'true', 'True', '登录成功', 'login: true'])
        resp = {'running': True, 'loggedIn': logged_in, 'message': text}
        _status_cache[port] = {'result': resp, 'time': time.time(), 'checking': False}
        return resp
    except Exception as e:
        resp = {'running': True, 'loggedIn': False, 'message': f'查询中: {e}'}
        _status_cache[port] = {'result': resp, 'time': time.time(), 'checking': False}
        return resp


# ═══════════════════════════════════════════════════════════════════════════════
# 发布功能
# ═══════════════════════════════════════════════════════════════════════════════

def publish(title, content, images, tags=None, port=18060, cookies_path=None,
            on_status=None, cleanup_on_success=True):
    """通过 MCP 发布小红书笔记。

    核心原则：MCP 用 rod 驱动 Chromium，登录会话在浏览器进程中。
    杀掉 MCP = 丢失登录。所以复用已有 MCP，仅在未运行时启动新的。

    Args:
        title: 笔记标题（最多20字符）
        content: 笔记正文（最多1000字符）
        images: 图片路径列表（至少1张）
        tags: 标签列表（可选）
        port: MCP 端口
        cookies_path: cookies 文件路径
        on_status: 状态回调 fn(msg)，用于更新进度
        cleanup_on_success: 成功后是否清理 Chromium 释放内存

    Returns:
        (success: bool, message: str, need_login: bool)
    """
    def _status(msg):
        if on_status:
            on_status(msg)
        print(f'[MCP-PUBLISH] {msg}')

    title = (title or '')[:20]
    content = (content or '')[:1000]
    tags = [t.lstrip('#').strip() for t in (tags or []) if t.strip()]

    if not images:
        return False, '小红书要求至少1张图片', False

    # 1. 确保 MCP 运行（复用已有进程，不杀登录会话）
    if not is_port_listening(port):
        _status('正在启动 MCP 服务...')
        ok, err = ensure_running(port, cookies_path)
        if not ok:
            return False, f'MCP 启动失败: {err}', False
        for _ in range(15):
            if is_port_listening(port):
                break
            time.sleep(1)
        else:
            return False, 'MCP 启动超时', False
        time.sleep(3)  # Chromium 初始化
    elif not health_check(port):
        _status('MCP 无响应，正在重启...')
        kill_processes(port)
        time.sleep(1)
        ok, err = ensure_running(port, cookies_path)
        if not ok:
            return False, f'MCP 重启失败: {err}', False
        for _ in range(15):
            if is_port_listening(port):
                break
            time.sleep(1)
        time.sleep(3)

    # 清掉可能过期的 session
    _session_ids.pop(port, None)

    # 2. 检查登录状态
    _status('正在检查小红书登录状态...')
    try:
        login_result = mcp_call('check_login_status', port=port, timeout=30)
        login_text = mcp_text(login_result)
        print(f'[MCP-PUBLISH] 登录状态: {login_text[:100]}')
        if '未登录' in login_text or 'not logged' in login_text.lower():
            return False, '小红书未登录，请先扫码登录后立即发布', True
    except Exception as e:
        return False, f'MCP 连接失败: {e}', False

    # 3. 调用 publish_content
    args = {'title': title, 'content': content, 'images': images}
    if tags:
        args['tags'] = tags

    _status('正在通过 MCP 发布（上传图片+填写内容，约需30秒）...')
    try:
        result = mcp_call('publish_content', args, port=port, timeout=180)
    except Exception as e:
        return False, str(e), False

    text = mcp_text(result)
    print(f'[MCP-PUBLISH] result text={text!r}')

    # 4. 解析结果
    if result and 'error' in result:
        err_msg = (result['error'] or {}).get('message', '发布失败')
        need_login = any(w in err_msg for w in ['未登录', 'not logged', 'login', '登录'])
        return False, err_msg, need_login

    if any(w in text for w in ['未登录', '请先登录', 'not logged']):
        return False, text, True

    success = any(w in text for w in ['成功', 'success', 'published', '发布完成', '发布成功'])

    # 5. 成功后清理 Chromium 释放内存
    if success and cleanup_on_success:
        cleanup_after_publish(port)

    return success, text or ('发布成功！' if success else '发布结果未知'), False


# ═══════════════════════════════════════════════════════════════════════════════
# 搜索功能
# ═══════════════════════════════════════════════════════════════════════════════

def _get_followers_batch(users_to_query, port=18060):
    """用单个 MCP session 串行查询多个用户的粉丝数。
    users_to_query: [(nickname, user_id, xsec_token), ...]
    返回 {nickname: fans_count_str, ...}
    """
    url = f'http://localhost:{port}/mcp'

    # 初始化 session
    try:
        sid = mcp_ensure_session(url, port)
    except Exception:
        print('[WARN] _get_followers_batch: session init failed')
        return {}

    result_map = {}
    for nick, uid, xsec in users_to_query:
        try:
            result = mcp_call('user_profile', {'user_id': uid, 'xsec_token': xsec},
                              port=port, timeout=15)
            text = mcp_text(result)
            if not text:
                continue
            try:
                profile = json.loads(text)
            except Exception:
                continue
            for item in profile.get('interactions', []):
                if item.get('type') == 'fans':
                    fans_count = item.get('count', '')
                    if fans_count:
                        result_map[nick] = fans_count
                        print(f'[INFO] Got fans for @{nick}: {fans_count}')
                    break
        except Exception as e:
            print(f'[WARN] user_profile failed for @{nick}({uid}): {e}')

    return result_map


def search_accounts(keywords, max_accounts=10, max_follower_queries=7,
                    section_title='行业标杆账号分析', port=18060):
    """搜索小红书并构建 Markdown 表格（不经过 LLM，防数据篡改）。

    Args:
        keywords: 搜索关键词列表
        max_accounts: 表格最多展示的账号数
        max_follower_queries: 查询粉丝数的账号数
        section_title: Markdown 标题
        port: MCP 端口

    Returns:
        (table_md, glm_context) — 表格和供 LLM 分析的简要文本
    """
    unique_kw = list(dict.fromkeys(keywords))
    print(f'[INFO] XHS search keywords: {unique_kw}')

    all_notes = []

    def _search(kw):
        try:
            result = mcp_call('search_feeds', {'keyword': kw}, port=port)
            text = mcp_text(result)
            if text:
                try:
                    data = json.loads(text)
                    if 'feeds' in data:
                        return [f for f in data['feeds']
                                if f.get('modelType') == 'note'
                                and f.get('noteCard', {}).get('user', {}).get('nickname')]
                except Exception:
                    pass
        except Exception as e:
            print(f'[WARN] XHS search "{kw}" failed: {e}')
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_search, kw): kw for kw in unique_kw}
        for f in concurrent.futures.as_completed(futures):
            all_notes.extend(f.result())

    if not all_notes:
        print('[WARN] XHS search: no notes found')
        return '', ''

    def _likes(n):
        try:
            return int(n.get('noteCard', {}).get('interactInfo', {}).get('likedCount', '0'))
        except (ValueError, TypeError):
            return 0

    all_notes.sort(key=_likes, reverse=True)

    seen_users = {}
    for n in all_notes:
        nick = n['noteCard']['user']['nickname']
        if nick not in seen_users:
            seen_users[nick] = n

    top_users = list(seen_users.items())[:max_accounts]

    # 查询粉丝数
    follower_map = {}
    users_to_query = []
    for nick, note in top_users[:max_follower_queries]:
        uid = note['noteCard']['user']['userId']
        xsec = note.get('xsecToken', '')
        users_to_query.append((nick, uid, xsec))

    if users_to_query:
        follower_map = _get_followers_batch(users_to_query, port=port)
    print(f'[INFO] Follower data: {len(follower_map)} of {len(users_to_query)} queried')

    # Markdown 表格
    lines = [f'## {section_title}\n']
    lines.append('### 标杆账号矩阵\n')
    lines.append('> 以下数据来自小红书真实搜索API，非人工编造\n')
    lines.append('| @账号名 | 粉丝数 | 代表内容 | 点赞 | 评论 | 收藏 | 类型 | 小红书主页 |')
    lines.append('|---------|--------|---------|------|------|------|------|----------|')

    for nick, note in top_users:
        card = note['noteCard']
        uid = card.get('user', {}).get('userId', '')
        interact = card.get('interactInfo', {})
        disp_title = (card.get('displayTitle', '') or '(无标题)').replace('|', '\\|')[:40]
        safe_nick = nick.replace('|', '\\|')
        likes = interact.get('likedCount', '0')
        comments = interact.get('commentCount', '0')
        collects = interact.get('collectedCount', '0')
        note_type = '视频' if card.get('type') == 'video' else '图文'
        fans = follower_map.get(nick, '-')
        profile_url = f'https://www.xiaohongshu.com/user/profile/{uid}' if uid else ''
        link_cell = f'[查看]({profile_url})' if profile_url else '-'
        lines.append(f'| @{safe_nick} | {fans} | {disp_title} | {likes} | {comments} | {collects} | {note_type} | {link_cell} |')

    table_md = '\n'.join(lines)

    glm_context = '以下是小红书搜索到的真实账号（数据表格已由系统生成，你不需要重复）：\n'
    for nick, note in top_users:
        t = note['noteCard'].get('displayTitle', '')
        glm_context += f'- @{nick}：「{t}」\n'

    print(f'[INFO] XHS search complete: {len(top_users)} accounts, {len(follower_map)} with follower data')
    return table_md, glm_context


# ═══════════════════════════════════════════════════════════════════════════════
# 面向对象封装（可选）
# ═══════════════════════════════════════════════════════════════════════════════

class XhsMcp:
    """小红书 MCP 集成封装，绑定到特定端口。"""

    def __init__(self, port=18060, cookies_path=None):
        self.port = port
        self.cookies_path = cookies_path or os.path.join(_COOKIES_DIR, 'default.json')

    def call(self, tool_name, arguments=None, timeout=60):
        return mcp_call(tool_name, arguments, port=self.port, timeout=timeout)

    def text(self, result):
        return mcp_text(result)

    def image(self, result):
        return mcp_image(result)

    def status(self, cache_seconds=8):
        return check_status(self.port, cache_seconds)

    def ensure_running(self):
        return ensure_running(self.port, self.cookies_path)

    def health_check(self):
        return health_check(self.port)

    def publish(self, title, content, images, tags=None, on_status=None, cleanup_on_success=True):
        return publish(title, content, images, tags=tags, port=self.port,
                       cookies_path=self.cookies_path, on_status=on_status,
                       cleanup_on_success=cleanup_on_success)

    def search_accounts(self, keywords, **kwargs):
        return search_accounts(keywords, port=self.port, **kwargs)

    def qrcode(self):
        """获取登录二维码，返回 (base64_data, mime_type) 或 (None, error_msg)。"""
        try:
            result = self.call('get_login_qrcode')
            data, mime = mcp_image(result)
            if data:
                return data, mime
            return None, mcp_text(result) or '二维码获取失败'
        except Exception as e:
            return None, str(e)
