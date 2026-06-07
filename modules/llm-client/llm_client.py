"""LLM Gateway 客户端 SDK

所有 AI 系统通过此模块调用 LLM，不再直接调用各 provider API。
零外部依赖（仅 stdlib），约 80 行核心代码。

用法:
    from llm_client import LLMClient
    client = LLMClient(system='vickslide')
    result = client.call('你好', system_prompt='你是助手', work_type='generate')
    print(result.content)       # 文本内容
    print(result.model_id)      # 实际使用的模型
    print(result.cost_yuan)     # 本次调用成本（元）
    print(result.tokens)        # {'input': N, 'output': M}
"""

import json
import time
import urllib.request
import urllib.error


class LLMResult:
    """LLM 调用结果，统一各系统的返回值格式。"""
    __slots__ = ('content', 'model_id', 'tokens', 'cost_yuan', 'latency_ms', 'fallback_from', 'error')

    def __init__(self, content='', model_id='', tokens=None, cost_yuan=0,
                 latency_ms=0, fallback_from=None, error=None):
        self.content = content
        self.model_id = model_id
        self.tokens = tokens or {'input': 0, 'output': 0}
        self.cost_yuan = cost_yuan
        self.latency_ms = latency_ms
        self.fallback_from = fallback_from
        self.error = error

    def __bool__(self):
        return bool(self.content)

    def __str__(self):
        return self.content


class LLMClient:
    """LLM Gateway 客户端。

    Args:
        system: 系统标识（如 'vickslide', 'codeaudit', 'content-factory', 'vickclaw'）
        gateway_url: Gateway 地址（默认 http://127.0.0.1:9016）
        emergency_key: 可选的紧急 GLM API Key，Gateway 完全不可用时降级使用
    """

    def __init__(self, system, gateway_url='http://127.0.0.1:9016', emergency_key=None):
        self.system = system
        self.gateway_url = gateway_url.rstrip('/')
        self.emergency_key = emergency_key
        # 健康检查缓存（F10: Gateway 瞬断时避免每次调用都超时）
        self._health = {'fail_until': 0}

    def call(self, prompt, system_prompt='', work_type='default',
             model_id='auto', agent_id=None, role=None,
             max_tokens=4000, temperature=0.7, timeout=120) -> LLMResult:
        """统一调用入口。

        Args:
            prompt: 用户 prompt（必填）
            system_prompt: 系统 prompt
            work_type: 功能类型，用于路由策略匹配
            model_id: 模型 ID（'auto' 走路由策略）
            agent_id: Agent 标识（VickClaw 多 Agent 场景）
            role: 角色标识（VickClaw 角色路由）
            max_tokens: 最大输出 token 数
            temperature: 采样温度
            timeout: 超时秒数
        """
        # 健康检查：Gateway 近期失败则直接降级
        now = time.time()
        if now < self._health['fail_until']:
            return self._emergency_call(prompt, system_prompt, max_tokens, temperature, timeout)

        payload = {
            'system': self.system,
            'work_type': work_type,
            'model_id': model_id,
            'prompt': prompt,
            'system_prompt': system_prompt,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'timeout': timeout,
        }
        if agent_id:
            payload['agent_id'] = agent_id
        if role:
            payload['role'] = role

        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                f'{self.gateway_url}/api/call',
                data=data,
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                result = json.loads(resp.read().decode('utf-8'))

            # 成功，重置健康状态
            self._health['fail_until'] = 0

            if result.get('error'):
                return LLMResult(error=result['error'], model_id=result.get('model_id', ''))

            return LLMResult(
                content=result.get('content', ''),
                model_id=result.get('model_id', ''),
                tokens=result.get('tokens', {'input': 0, 'output': 0}),
                cost_yuan=result.get('cost_yuan', 0),
                latency_ms=result.get('latency_ms', 0),
                fallback_from=result.get('fallback_from'),
            )

        except Exception as e:
            print(f'[LLMClient] Gateway call failed: {e}')
            # 标记 Gateway 不可用 30s
            self._health['fail_until'] = time.time() + 30
            return self._emergency_call(prompt, system_prompt, max_tokens, temperature, timeout)

    def _emergency_call(self, prompt, system_prompt, max_tokens, temperature, timeout):
        """紧急降级：直接调 GLM API（需要 emergency_key）。"""
        if not self.emergency_key:
            return LLMResult(error='Gateway unavailable and no emergency_key configured')

        try:
            payload = {
                'model': 'glm-4-plus',
                'messages': [
                    {'role': 'system', 'content': system_prompt or '你是一位专业的AI助手。'},
                    {'role': 'user', 'content': prompt},
                ],
                'max_tokens': max_tokens,
                'temperature': temperature,
            }
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                'https://open.bigmodel.cn/api/paas/v4/chat/completions',
                data=data,
                headers={
                    'Authorization': f'Bearer {self.emergency_key}',
                    'Content-Type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            content = result['choices'][0]['message']['content']
            usage = result.get('usage', {})
            return LLMResult(
                content=content,
                model_id='glm-4-plus(emergency)',
                tokens={
                    'input': usage.get('prompt_tokens', 0),
                    'output': usage.get('completion_tokens', 0),
                },
            )
        except Exception as e:
            return LLMResult(error=f'Emergency GLM call also failed: {e}')

    def health(self) -> bool:
        """检查 Gateway 是否可用。"""
        try:
            req = urllib.request.Request(f'{self.gateway_url}/api/models')
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def models(self, enabled_only=True) -> list:
        """获取可用模型列表。"""
        try:
            url = f'{self.gateway_url}/api/models'
            if enabled_only:
                url += '?status=enabled'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data.get('models', [])
        except Exception:
            return []
