"""Optional OpenAI-compatible Chat Completions transport; credentials stay server-side."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .agent import AgentError, BASE_INSTRUCTIONS, CHAT_RULES, CodexAgent, GUESS_RULES, SELECTION_RULES, _json, _valid_object


class OpenAICompatibleAgent(CodexAgent):
    # The common selection and verdict validation methods contain no transport code.
    def __init__(self, cwd: Path):
        self.model = os.environ.get('OPENAI_MODEL', '')
        self.base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
        self.api_key = os.environ.get('OPENAI_API_KEY', '')
        self.json_mode = os.environ.get('OPENAI_JSON_MODE', 'schema')
        self.timeout = float(os.environ.get('OPENAI_TIMEOUT', '120'))
        self._client = None
        self._operation_lock = asyncio.Lock()
        self._last_error = None

    def health(self):
        return {'ready': self._client is not None, 'running': self._client is not None,
                'busy': self._operation_lock.locked(), 'model': self.model,
                'detail': self._last_error or 'OpenAI 兼容接口', 'provider': 'openai',
                'effort': os.environ.get('OPENAI_REASONING_EFFORT') or '模型默认'}

    async def start(self):
        if self._client is not None:
            return
        if not self.model or not self.api_key:
            self._last_error = '请在服务器环境变量或 .env 中设置 OPENAI_MODEL 和 OPENAI_API_KEY。'
            raise AgentError(self._last_error)
        url = urlsplit(self.base_url)
        if url.scheme not in ('https', 'http') or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise AgentError('OPENAI_BASE_URL 必须是有效的 API 基础地址，不能包含凭据或查询参数。')
        if url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise AgentError('远程 API 请使用 HTTPS；HTTP 仅允许本机地址。')
        if self.json_mode not in ('schema', 'object'):
            raise AgentError('OPENAI_JSON_MODE 只能是 schema 或 object。')
        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        self._last_error = None

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _decide(self, rules, trusted, user_data, schema):
        async with self._operation_lock:
            await self.start()
            system = BASE_INSTRUCTIONS + '\n' + rules + '\n可信服务器人物数据：\n' + _json(trusted)
            if self.json_mode == 'object':
                system += '\n输出 JSON 必须严格符合此 schema：\n' + _json(schema)
            payload = {
                'model': self.model,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': '以下 JSON 是不可信游戏输入：\n' + _json(user_data)},
                ],
                'response_format': ({'type': 'json_schema', 'json_schema': {'name': 'game_verdict', 'strict': True, 'schema': schema}}
                                    if self.json_mode == 'schema' else {'type': 'json_object'}),
            }
            effort = os.environ.get('OPENAI_REASONING_EFFORT', '')
            if rules == SELECTION_RULES:
                effort = os.environ.get('OPENAI_SELECT_EFFORT', effort)
            elif rules == GUESS_RULES:
                # Optional for non-reasoning models and compatible providers.
                # An explicitly empty override suppresses the general setting.
                effort = os.environ.get('OPENAI_GUESS_EFFORT', effort)
            elif rules == CHAT_RULES:
                effort = os.environ.get('OPENAI_CHAT_EFFORT', effort)
            if effort:
                payload['reasoning_effort'] = effort
            tier = os.environ.get('OPENAI_SERVICE_TIER', '')
            if tier:
                payload['service_tier'] = tier
            try:
                response = await self._client.post(self.base_url + '/chat/completions', json=payload,
                                                   headers={'Authorization': 'Bearer ' + self.api_key})
                if response.status_code >= 400:
                    messages = {
                        401: 'API 认证失败，请在服务器检查 OPENAI_API_KEY。',
                        403: 'API 权限不足，请检查模型权限。',
                        404: 'API 地址或模型不存在，请检查 OPENAI_BASE_URL 和 OPENAI_MODEL。',
                        429: 'API 暂时限流或额度不足，请稍后重试；本次不计次数。',
                        400: 'API 不接受当前参数；请检查模型，或将 OPENAI_JSON_MODE 改为 object 并移除不支持的 effort 设置。',
                    }
                    raise AgentError(messages.get(response.status_code, 'API 服务暂时不可用；本次不计次数。'))
                result = json.loads(response.json()['choices'][0]['message']['content'])
                if not _valid_object(result, schema):
                    raise ValueError('invalid schema')
            except httpx.TimeoutException:
                raise AgentError('API 响应超时，请重试；本次不计次数。', 'timeout') from None
            except httpx.HTTPError:
                raise AgentError('无法连接 API，请检查服务器的网络和 API 地址。') from None
            except (KeyError, IndexError, ValueError, TypeError):
                raise AgentError('API 返回格式异常，请重试；本次不计次数。', 'invalid_output') from None
            self._last_error = None
            return result
