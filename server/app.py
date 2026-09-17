import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import Database

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger('historical_guess')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class NewGame(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset: Literal['broad', 'sanguozhi'] = 'broad'
    scope: str = Field(default='', max_length=500)
    question_limit: int | None = Field(default=None, ge=1, le=10000, strict=True)
    guess_limit: int | None = Field(default=None, ge=1, le=10000, strict=True)
    timer_enabled: bool = True

    @field_validator('scope')
    @classmethod
    def trim_scope(cls, value):
        return value.strip()


class Move(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')

    @field_validator('text')
    @classmethod
    def trim_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('请输入内容')
        return value


def load_characters(path: Path):
    people = json.loads(path.read_text())
    ids = set()
    for p in people:
        if p['id'] in ids or not 184 <= p['active_year'] <= 316:
            raise ValueError(f"人物编号重复或在世证据超出范围: {p['id']}")
        if p.get('birth_year') is not None and p['birth_year'] > p['active_year']:
            raise ValueError(f"出生日期与在世证据冲突: {p['id']}")
        if p.get('death_year') is not None and p['death_year'] < p['active_year']:
            raise ValueError(f"去世日期与在世证据冲突: {p['id']}")
        if not p.get('sources') or (p.get('sanguozhi') and not p.get('sanguozhi_evidence')):
            raise ValueError(f"人物来源缺失: {p['id']}")
        ids.add(p['id'])
    if not people:
        raise ValueError('人物库为空')
    return people


def create_app(db_path=None, agent=None, data_path=None):
    from .agent import AgentError, CodexAgent

    characters = load_characters(Path(data_path or ROOT / 'data/characters.json'))
    references = {person['id']: person for person in characters}
    store = Database(Path(db_path or os.environ.get('GAME_DB', ROOT / 'var/games.sqlite3')), characters)
    provider = os.environ.get('AGENT_PROVIDER', 'codex')
    if provider not in ('codex', 'openai'):
        raise ValueError('AGENT_PROVIDER must be codex or openai')
    if agent is not None:
        judge = agent
    elif provider == 'openai':
        from .openai_agent import OpenAICompatibleAgent
        judge = OpenAICompatibleAgent(ROOT / 'var/agent-workspace')
    else:
        judge = CodexAgent(ROOT / 'var/agent-workspace')
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await judge.start()
        except Exception as exc:
            log.warning('Codex startup unavailable (%s); saved games remain accessible', type(exc).__name__)
        yield
        await judge.close()

    app = FastAPI(title='问古 · 历史人物猜谜', lifespan=lifespan)
    app.state.store = store
    app.state.agent = judge

    @app.middleware('http')
    async def browser_session(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and (urlsplit(origin).netloc != request.url.netloc or urlsplit(origin).scheme != request.url.scheme):
                return JSONResponse({'detail': '请从游戏页面提交请求。'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '不接受跨站请求。'}, status_code=403)
        token = request.cookies.get('history_player', '')
        is_new = not re.fullmatch(r'[a-f0-9]{64}', token)
        if is_new:
            token = secrets.token_hex(32)
        request.state.owner = hashlib.sha256(token.encode()).hexdigest()
        response = await call_next(request)
        if is_new:
            response.set_cookie('history_player', token, max_age=365 * 24 * 3600, httponly=True,
                                samesite='strict', secure=request.url.scheme == 'https')
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.exception_handler(AgentError)
    async def handle_agent_error(request, exc):
        return JSONResponse({'detail': exc.public_message}, status_code=422 if exc.code == 'scope' else 503)

    # FastAPI's default validation response is an array. Keep the frontend's error contract stable.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': '输入不合法：内容不能为空，次数须为 1–10000 的整数或留空。'}, status_code=422)

    def get_owned(game_id: str, owner: str):
        game = store.get(game_id, owner)
        if not game:
            raise HTTPException(404, '找不到这局游戏，请在创建它的浏览器中打开。')
        return game

    @app.get('/api/meta')
    async def meta():
        return {
            'character_count': len(characters),
            'presets': [
                {'id': 'broad', 'name': '广义三国', 'description': '公元 184–316 年间曾在世；含东汉末年、三国与西晋。', 'count': len(characters)},
                {'id': 'sanguozhi', 'name': '《三国志》留名', 'description': '本库中已附《三国志》正文或裴注姓名证据的人物。',
                 'count': sum(bool(p['sanguozhi']) for p in characters)},
            ],
            'agent': judge.health(),
        }

    @app.post('/api/agent/reconnect')
    async def reconnect_agent():
        await judge.start()
        return judge.health()

    @app.get('/api/characters')
    async def browse(preset: Literal['broad', 'sanguozhi'] = 'broad', q: str = Query('', max_length=100),
                     offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        matches = [p for p in characters if (preset != 'sanguozhi' or p['sanguozhi'])
                   and (not q or q.casefold() in json.dumps(p, ensure_ascii=False).casefold())]
        return {'items': matches[offset:offset + limit], 'total': len(matches)}

    @app.post('/api/games', status_code=201)
    async def new_game(body: NewGame, request: Request):
        async with locks.setdefault('owner:' + request.state.owner, asyncio.Lock()):
            candidates = [p for p in characters if body.preset != 'sanguozhi' or p['sanguozhi']]
            secrets.SystemRandom().shuffle(candidates)
            # With no custom scope, a shuffled sample is uniform over the full pool.
            # The agent still makes and validates the actual choice; avoid sending
            # hundreds of irrelevant biographies on every new game.
            choice = await judge.select(candidates if body.scope else candidates[:12], body.scope)
            allowed = {p['id']: p for p in candidates}
            eligible = choice.get('eligible_ids', [])
            if not eligible:
                raise HTTPException(422, '当前题库里没有能确认符合这个范围的人物，请调整范围。')
            if (not isinstance(eligible, list) or any(not isinstance(k, str) or k not in allowed for k in eligible)
                    or choice.get('character_id') not in eligible):
                raise HTTPException(503, '裁判未能确认人物范围，请重试。')
            description = choice.get('scope_description', '')
            if not isinstance(description, str) or len(description) > 1000:
                raise HTTPException(503, '裁判范围说明无效，请重试。')
            # Never send the agent's free text to an active game's client: it can contain a name.
            # The selected preset and original user constraints are the authoritative public scope.
            public_scope = ('广义三国（184–316）' if body.preset == 'broad' else '《三国志》留名')
            if body.scope:
                public_scope += ' · ' + body.scope
            game_id = str(uuid.uuid4())
            store.create({
                'id': game_id, 'owner': request.state.owner, 'status': 'active', 'preset': body.preset,
                'scope': body.scope, 'scope_description': public_scope,
                'candidate_count': len(set(eligible)) if body.scope else len(candidates),
                'character': json.dumps(allowed[choice['character_id']], ensure_ascii=False),
                'question_limit': body.question_limit, 'guess_limit': body.guess_limit,
                'timer_enabled': int(body.timer_enabled), 'created_at': timestamp(),
            })
            return store.public(get_owned(game_id, request.state.owner))

    @app.get('/api/games')
    async def history(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        return store.history(request.state.owner, offset, limit)

    @app.get('/api/games/{game_id}')
    async def game_view(game_id: str, request: Request):
        return store.public(get_owned(game_id, request.state.owner))

    async def play(game_id: str, body: Move, request: Request, kind: str):
        async with locks.setdefault(game_id, asyncio.Lock()):
            game = get_owned(game_id, request.state.owner)
            previous = store.event(game_id, body.request_id)
            if previous:
                if previous['text'] != body.text or previous['kind'] != kind:
                    raise HTTPException(409, '这个请求编号已用于另一条内容，请重新提交。')
                return store.public(game)
            if game['status'] != 'active':
                raise HTTPException(409, '这局游戏已经结束。')
            if kind == 'question' and game['question_limit'] is not None and game['question_count'] >= game['question_limit']:
                raise HTTPException(409, '提问次数已用完，还可以提交人物猜测。')
            person = json.loads(game['character'])
            # Preserve the saved identity and original event history, while
            # allowing published historical errata to inform an ongoing game.
            latest = references.get(person['id'])
            if latest:
                for field in ('facts', 'sources', 'factions', 'sanguozhi', 'sanguozhi_evidence'):
                    if field in latest:
                        person[field] = latest[field]
            if kind == 'question':
                events = [event for event in store.public(game)['events'] if not event.get('withdrawn')]
                verdict = await judge.question(person, events, body.text)
                answer = verdict.get('answer')
                if answer not in ('是', '否', '无法回答') or (answer == '无法回答') != (verdict.get('invalid') is True):
                    raise HTTPException(503, '裁判未给出符合规则的回答，请重试。本次不计次数。')
            else:
                verdict = await judge.guess(person, body.text)
                answer = verdict.get('answer')
                if answer not in ('正确', '错误'):
                    raise HTTPException(503, '裁判未给出有效判断，请重试。本次不计次数。')
            store.record(game, body.request_id, kind, body.text, answer, str(verdict.get('reason', '')), timestamp())
            return store.public(get_owned(game_id, request.state.owner))

    @app.post('/api/games/{game_id}/questions')
    async def question(game_id: str, body: Move, request: Request):
        return await play(game_id, body, request, 'question')

    @app.post('/api/games/{game_id}/guesses')
    async def guess(game_id: str, body: Move, request: Request):
        return await play(game_id, body, request, 'guess')

    @app.post('/api/games/{game_id}/give-up')
    async def give_up(game_id: str, request: Request):
        async with locks.setdefault(game_id, asyncio.Lock()):
            game = get_owned(game_id, request.state.owner)
            if game['status'] == 'active':
                store.abandon(game_id, timestamp())
            return store.public(get_owned(game_id, request.state.owner))

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static/index.html', headers={'Cache-Control': 'no-cache'})

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
