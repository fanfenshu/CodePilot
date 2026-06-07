#!/usr/bin/env python3
"""
ChatGPT Bridge — 将 ChatGPT Pro 订阅转为 OpenAI 兼容 API

原理：用 ChatGPT 登录后的 access_token 调用 ChatGPT 内部 backend-api，
对外暴露标准 OpenAI /v1/chat/completions 接口，供 LLM Gateway 调用。

所有请求走 ChatGPT Pro 订阅额度，不产生 API 计费。

用法:
  python3 chatgpt_bridge.py --port 9019
  python3 chatgpt_bridge.py --port 9019 --token-file /path/to/chatgpt_token.json
"""

import http.server
import json
import os
import sys
import time
import uuid
import argparse
import threading
from urllib.parse import urlparse, parse_qs
from socketserver import ThreadingMixIn

# ===== 配置 =====

DEFAULT_PORT = 9019
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'chatgpt_token.json')
_runtime_token_file = [TOKEN_FILE]  # 运行时可覆盖
CHATGPT_BASE = 'https://chatgpt.com'
BACKEND_API = f'{CHATGPT_BASE}/backend-api'

# ChatGPT Pro 可用模型映射（bridge model_id → ChatGPT 内部 model slug）
MODEL_MAP = {
    # GPT-5 系列（ChatGPT Pro 订阅）
    'chatgpt-5.4-pro': 'gpt-5.4-pro',       # 最强，Pro专属，更多计算
    'chatgpt-5.4': 'gpt-5.4',               # 旗舰推理（Thinking）
    'chatgpt-5.3': 'gpt-5.3-chat-latest',   # 默认对话（Instant）
    'chatgpt-5.3-codex': 'gpt-5.3-codex',   # 编码专用
    'chatgpt-5.2': 'gpt-5.2',               # Legacy推理，6月退役
    # GPT-4 系列
    'chatgpt-4o-pro': 'gpt-4o',
    'chatgpt-o3-pro': 'o3',
    'chatgpt-o4-mini-pro': 'o4-mini',
    'chatgpt-gpt4.1-pro': 'gpt-4.1',
    # 直接传 slug 也行
    'gpt-5.4-pro': 'gpt-5.4-pro',
    'gpt-5.4': 'gpt-5.4',
    'gpt-5.3': 'gpt-5.3-chat-latest',
    'gpt-5.3-codex': 'gpt-5.3-codex',
    'gpt-5.2': 'gpt-5.2',
    'gpt-4o': 'gpt-4o',
    'o3': 'o3',
    'o4-mini': 'o4-mini',
    'gpt-4.1': 'gpt-4.1',
    'gpt-4.1-mini': 'gpt-4.1-mini',
    'gpt-4.1-nano': 'gpt-4.1-nano',
}

# ===== Token 管理 =====

_token_lock = threading.Lock()
_token_cache = {
    'access_token': None,
    'expires_at': 0,
    'loaded_from': None,
}


def _load_token(token_file=None):
    """从文件加载 ChatGPT access_token。"""
    tf = token_file or TOKEN_FILE
    if not os.path.exists(tf):
        return None
    try:
        with open(tf) as f:
            data = json.load(f)
        token = data.get('access_token') or data.get('accessToken')
        expires = data.get('expires_at', 0) or data.get('expiresAt', 0)
        if token:
            with _token_lock:
                _token_cache['access_token'] = token
                _token_cache['expires_at'] = expires
                _token_cache['loaded_from'] = tf
            print(f'[INFO] ChatGPT token loaded from {tf}')
            if expires > 0:
                remaining = expires - time.time()
                if remaining > 0:
                    print(f'[INFO] Token expires in {int(remaining/3600)}h {int(remaining%3600/60)}m')
                else:
                    print(f'[WARN] Token appears expired, will attempt to use anyway')
        return token
    except Exception as e:
        print(f'[ERROR] Failed to load token: {e}')
        return None


def _save_token(access_token, expires_at=0, token_file=None):
    """保存 token 到文件。"""
    tf = token_file or TOKEN_FILE
    os.makedirs(os.path.dirname(tf), exist_ok=True)
    data = {
        'access_token': access_token,
        'expires_at': expires_at,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(tf, 'w') as f:
        json.dump(data, f, indent=2)
    os.chmod(tf, 0o600)
    with _token_lock:
        _token_cache['access_token'] = access_token
        _token_cache['expires_at'] = expires_at
    print(f'[INFO] Token saved to {tf}')


def _get_token():
    """获取当前 access_token。"""
    with _token_lock:
        return _token_cache.get('access_token')


# ===== ChatGPT Backend API 调用 =====

def _call_chatgpt_backend(model_slug, messages, max_tokens=4096, temperature=0.7):
    """调用 ChatGPT backend-api/conversation，返回完整回复文本。

    Args:
        model_slug: ChatGPT 内部模型标识（如 'gpt-4o', 'o3'）
        messages: OpenAI 格式的 messages 列表
        max_tokens: 最大生成 token 数
        temperature: 温度

    Returns:
        dict: {'content': str, 'model': str, 'usage': dict} 或 {'error': str}
    """
    import urllib.request
    import urllib.error

    token = _get_token()
    if not token:
        return {'error': 'ChatGPT access_token not configured. Run session refresh first.'}

    # 组装 ChatGPT conversation 请求
    # 将 OpenAI messages 格式转为 ChatGPT backend-api 格式
    chatgpt_messages = []
    system_prompt = None

    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        if role == 'system':
            system_prompt = content
            continue
        chatgpt_messages.append({
            'id': str(uuid.uuid4()),
            'author': {'role': role},
            'content': {'content_type': 'text', 'parts': [content]},
            'metadata': {},
        })

    if not chatgpt_messages:
        return {'error': 'No user message provided'}

    payload = {
        'action': 'next',
        'messages': chatgpt_messages,
        'model': model_slug,
        'parent_message_id': str(uuid.uuid4()),
        'conversation_mode': {'kind': 'primary_assistant'},
        'force_paragen': False,
        'force_paragen_model_slug': '',
        'force_rate_limit': False,
        'reset_rate_limits': False,
        'websocket_request_id': str(uuid.uuid4()),
    }

    # 如果有 system prompt，通过 system_hints 传递
    if system_prompt:
        payload['system_hints'] = [system_prompt]

    url = f'{BACKEND_API}/conversation'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Origin': CHATGPT_BASE,
        'Referer': f'{CHATGPT_BASE}/',
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            # 解析 SSE 流，提取最终回复
            full_text = ''
            model_used = model_slug
            finish_reason = 'stop'
            conversation_id = None

            for line in resp:
                line = line.decode('utf-8', errors='replace').strip()
                if not line.startswith('data: '):
                    continue
                data_str = line[6:]
                if data_str == '[DONE]':
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # 提取回复内容
                msg = event.get('message', {})
                if not msg:
                    continue

                author_role = msg.get('author', {}).get('role', '')
                if author_role != 'assistant':
                    continue

                content = msg.get('content', {})
                parts = content.get('parts', [])
                if parts and isinstance(parts[0], str):
                    full_text = parts[0]  # ChatGPT 每次返回完整文本，不是增量

                # 记录 conversation_id 用于后续清理
                if not conversation_id:
                    conversation_id = event.get('conversation_id')

                # 检查完成状态
                status = msg.get('status', '')
                if status == 'finished_successfully':
                    finish_reason = 'stop'
                    meta = msg.get('metadata', {})
                    model_used = meta.get('model_slug', model_slug)

            # 异步删除对话（避免污染 ChatGPT 历史记录）
            if conversation_id:
                _cleanup_conversation(conversation_id, token)

            if not full_text:
                return {'error': 'Empty response from ChatGPT'}

            return {
                'content': full_text,
                'model': model_used,
                'usage': {
                    'prompt_tokens': sum(len(m.get('content', '')) for m in messages) // 4,  # 估算
                    'completion_tokens': len(full_text) // 4,
                },
                'finish_reason': finish_reason,
            }

    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f'[ERROR] ChatGPT backend API error ({e.code}): {body}')
        if e.code in (401, 403):
            print('[WARN] Token may be expired, need re-login')
            with _token_lock:
                _token_cache['expires_at'] = 0
        return {'error': f'ChatGPT API error {e.code}: {body[:200]}'}
    except Exception as e:
        print(f'[ERROR] ChatGPT call failed: {e}')
        return {'error': str(e)}


def _cleanup_conversation(conversation_id, token):
    """异步删除刚创建的对话，保持 ChatGPT 历史干净。"""
    def _do_delete():
        import urllib.request
        try:
            url = f'{BACKEND_API}/conversation/{conversation_id}'
            payload = json.dumps({'is_visible': False}).encode()
            req = urllib.request.Request(url, data=payload, method='PATCH',
                                        headers={
                                            'Authorization': f'Bearer {token}',
                                            'Content-Type': 'application/json',
                                        })
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # 清理失败无所谓

    threading.Thread(target=_do_delete, daemon=True).start()


# ===== HTTP Server =====

class _BridgeHandlerBase:
    """ChatGPT Bridge HTTP handler，暴露 OpenAI 兼容 API。"""

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _send_cors(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def do_OPTIONS(self):
        self._send_cors()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/health':
            token = _get_token()
            self._send_json(200, {
                'status': 'ok' if token else 'no_token',
                'has_token': bool(token),
                'token_expires_at': _token_cache.get('expires_at', 0),
                'available_models': list(MODEL_MAP.keys()),
            })
            return

        if path == '/v1/models':
            models = []
            for mid, slug in MODEL_MAP.items():
                if mid.startswith('chatgpt-'):
                    models.append({
                        'id': mid,
                        'object': 'model',
                        'owned_by': 'openai',
                        'permission': [],
                        'meta': {'slug': slug, 'access': 'chatgpt-pro-subscription'},
                    })
            self._send_json(200, {'object': 'list', 'data': models})
            return

        self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/v1/chat/completions':
            self._handle_chat_completions()
            return

        if path == '/v1/token':
            self._handle_set_token()
            return

        self._send_json(404, {'error': 'not found'})

    def _handle_chat_completions(self):
        """处理 /v1/chat/completions 请求，转发到 ChatGPT backend-api。"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as e:
            self._send_json(400, {'error': f'Invalid request body: {e}'})
            return

        model_id = body.get('model', 'chatgpt-4o-pro')
        messages = body.get('messages', [])
        max_tokens = body.get('max_tokens', 4096)
        temperature = body.get('temperature', 0.7)

        if not messages:
            self._send_json(400, {'error': 'messages is required'})
            return

        # 映射模型
        model_slug = MODEL_MAP.get(model_id, model_id)
        print(f'[INFO] ChatGPT Bridge: {model_id} → {model_slug}, messages={len(messages)}')

        # 调用 ChatGPT
        t0 = time.time()
        result = _call_chatgpt_backend(model_slug, messages, max_tokens, temperature)
        latency = int((time.time() - t0) * 1000)

        if 'error' in result:
            self._send_json(502, {
                'error': {'message': result['error'], 'type': 'chatgpt_bridge_error'},
            })
            return

        # 返回 OpenAI 兼容格式
        usage = result.get('usage', {})
        response = {
            'id': f'chatcmpl-bridge-{uuid.uuid4().hex[:12]}',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': result.get('model', model_slug),
            'choices': [{
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': result['content'],
                },
                'finish_reason': result.get('finish_reason', 'stop'),
            }],
            'usage': {
                'prompt_tokens': usage.get('prompt_tokens', 0),
                'completion_tokens': usage.get('completion_tokens', 0),
                'total_tokens': usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0),
            },
            'system_fingerprint': f'chatgpt-bridge-{latency}ms',
        }
        print(f'[INFO] ChatGPT Bridge response: {len(result["content"])} chars, {latency}ms')
        self._send_json(200, response)

    def _handle_set_token(self):
        """POST /v1/token — 手动设置 access_token。"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._send_json(400, {'error': 'Invalid body'})
            return

        token = body.get('access_token') or body.get('token')
        if not token:
            self._send_json(400, {'error': 'access_token is required'})
            return

        expires_at = body.get('expires_at', time.time() + 86400 * 14)
        _save_token(token, expires_at, _runtime_token_file[0])
        self._send_json(200, {'status': 'ok', 'message': 'Token saved'})


class _BridgeHandler(_BridgeHandlerBase, http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ===== CLI =====

def main():
    parser = argparse.ArgumentParser(description='ChatGPT Bridge — Pro 订阅转 API')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'服务端口 (default: {DEFAULT_PORT})')
    parser.add_argument('--host', default='127.0.0.1', help='监听地址 (default: 127.0.0.1)')
    parser.add_argument('--token-file', default=TOKEN_FILE, help='Token 文件路径')
    args = parser.parse_args()

    # 用命令行参数覆盖默认 token 文件路径
    _runtime_token_file[0] = args.token_file

    # 加载 token
    _load_token(args.token_file)
    if not _get_token():
        print(f'[WARN] No token found at {args.token_file}')
        print(f'[WARN] Set token via: POST http://127.0.0.1:{args.port}/v1/token')
        print(f'[WARN] Or run: python3 chatgpt_session.py --init')

    server = ThreadedHTTPServer((args.host, args.port), _BridgeHandler)
    print(f'[INFO] ChatGPT Bridge started on {args.host}:{args.port}')
    print(f'[INFO] API endpoint: http://127.0.0.1:{args.port}/v1/chat/completions')
    print(f'[INFO] Health check: http://127.0.0.1:{args.port}/health')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[INFO] Shutting down...')
        server.shutdown()


if __name__ == '__main__':
    main()
