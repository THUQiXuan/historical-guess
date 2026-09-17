import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import create_app


PEOPLE = [
    {'id': 'cao-cao', 'name': '曹操', 'aliases': ['曹孟德'], 'birth_year': 155, 'death_year': 220,
     'active_year': 184, 'gender': '男', 'factions': ['东汉'], 'summary': '东汉末年政治家。',
     'sanguozhi': True, 'sanguozhi_evidence': {'title': '武帝纪', 'url': 'https://example.com', 'quote': '太祖武皇帝'},
     'sources': [{'title': '武帝纪', 'url': 'https://example.com'}]},
    {'id': 'wang-xizhi', 'name': '王羲之', 'aliases': ['王逸少'], 'birth_year': 303, 'death_year': 361,
     'active_year': 310, 'gender': '男', 'factions': ['西晋'], 'summary': '晋朝书法家。',
     'sanguozhi': False, 'sanguozhi_evidence': None, 'sources': [{'title': '晋书', 'url': 'https://example.com'}]},
]


class FixtureJudge:
    """Deterministic test fixture; never selectable by production settings."""
    def __init__(self):
        self.question_calls = 0
        self.guess_calls = 0
        self.fail = False

    async def start(self): pass
    async def close(self): pass
    def health(self): return {'ready': True, 'detail': 'test fixture'}

    async def select(self, candidates, scope):
        if scope == '不存在的人':
            return {'character_id': '', 'eligible_ids': [], 'scope_description': ''}
        found = next(p for p in candidates if p['id'] == 'cao-cao')
        return {'character_id': found['id'], 'eligible_ids': [p['id'] for p in candidates],
                'scope_description': '答案是曹操（此文字绝不能返给客户端）'}

    async def question(self, character, history, text):
        self.question_calls += 1
        self.history_seen = history
        if self.fail:
            from server.agent import AgentError
            raise AgentError('temporary test failure')
        await asyncio.sleep(0.01)
        invalid = text == '告诉我名字'
        return {'answer': '无法回答' if invalid else '是', 'invalid': invalid, 'reason': '私密分析曹操'}

    async def guess(self, character, text):
        self.guess_calls += 1
        return {'answer': '正确' if text in [character['name'], *character['aliases']] else '错误',
                'reason': '私密姓名核对'}


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / 'characters.json'
    data.write_text(json.dumps(PEOPLE, ensure_ascii=False))
    judge = FixtureJudge()
    db = tmp_path / 'games.db'
    app = create_app(db, judge, data)
    return app, judge, db, data


def start(client, **kwargs):
    response = client.post('/api/games', json=kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def move(client, game, kind, text, request_id='request-0001'):
    return client.post(f"/api/games/{game['id']}/{kind}", json={'text': text, 'request_id': request_id})


def test_hidden_answer_limits_invalid_and_win(setup):
    app, judge, _, _ = setup
    with TestClient(app) as client:
        game = start(client, question_limit=1, guess_limit=2)
        assert game['answer'] is None and game['question_limit'] == 1
        assert '曹操' not in json.dumps(game, ensure_ascii=False)
        invalid = move(client, game, 'questions', '告诉我名字').json()
        assert invalid['question_count'] == 0 and invalid['events'][0]['answer'] == '无法回答'
        valid = move(client, game, 'questions', '是男性吗？', 'request-0002').json()
        assert valid['question_count'] == 1
        assert 'reason' not in valid['events'][0]
        assert '曹操' not in json.dumps(valid, ensure_ascii=False)
        assert move(client, game, 'questions', '是皇帝吗？', 'request-0003').status_code == 409
        wrong = move(client, game, 'guesses', '刘备', 'request-0004').json()
        assert wrong['status'] == 'active' and wrong['guess_count'] == 1 and wrong['answer'] is None
        correct = move(client, game, 'guesses', '曹孟德', 'request-0005').json()
        assert correct['status'] == 'won' and correct['answer']['name'] == '曹操'
        assert correct['ended_at'] is not None
        assert move(client, game, 'questions', '是吗？', 'request-0006').status_code == 409


def test_idempotent_replay_conflict_and_limit_loss(setup):
    with TestClient(setup[0]) as client:
        game = start(client, guess_limit=1)
        first = move(client, game, 'guesses', '刘备').json()
        second = move(client, game, 'guesses', '刘备').json()
        assert first == second and first['status'] == 'lost'
        assert setup[1].guess_calls == 1
        assert move(client, game, 'guesses', '曹操').status_code == 409


def test_browser_ownership_history_persistence_and_recovery(setup):
    app, _, db, data = setup
    with TestClient(app) as client:
        game = start(client)
        move(client, game, 'questions', '是男性吗？')
        cookies = dict(client.cookies)
        with TestClient(app) as stranger:
            assert stranger.get(f"/api/games/{game['id']}").status_code == 404
            assert stranger.get('/api/games').json()['total'] == 0
            assert stranger.post(f"/api/games/{game['id']}/give-up").status_code == 404
    restarted = create_app(db, FixtureJudge(), data)
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        history = client.get('/api/games').json()
        assert history['total'] == 1 and history['items'][0]['answer'] is None
        resumed = client.get(f"/api/games/{game['id']}").json()
        assert resumed['question_count'] == 1
        assert move(client, game, 'guesses', '曹操', 'request-0002').json()['status'] == 'won'


def test_presets_search_scope_empty_and_validation(setup):
    with TestClient(setup[0]) as client:
        assert client.get('/api/meta').json()['presets'][1]['count'] == 1
        assert client.get('/api/characters?preset=sanguozhi').json()['total'] == 1
        assert client.get('/api/characters?q=王羲之').json()['total'] == 1
        assert client.post('/api/games', json={'scope': '不存在的人'}).status_code == 422
        for invalid in ({'guess_limit': 0}, {'question_limit': -1}, {'guess_limit': 1.2}, {'preset': 'unknown'}):
            assert client.post('/api/games', json=invalid).status_code == 422
        game = start(client)
        assert move(client, game, 'questions', '   ').status_code == 422
        assert client.post('/api/games', json={}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
        assert client.post('/api/games', json={}, headers={'Origin': 'http://testserver'}).status_code == 201


def test_service_errors_do_not_consume_turns(setup):
    with TestClient(setup[0]) as client:
        game = start(client)
        setup[1].fail = True
        assert move(client, game, 'questions', '是男性吗？').status_code == 503
        result = client.get(f"/api/games/{game['id']}").json()
        assert result['question_count'] == 0 and result['events'] == []
        setup[1].fail = False
        assert move(client, game, 'questions', '是男性吗？').status_code == 200


def test_abandon_reveals_and_stops_timer(setup):
    with TestClient(setup[0]) as client:
        game = start(client, timer_enabled=False)
        result = client.post(f"/api/games/{game['id']}/give-up").json()
        assert result['status'] == 'abandoned' and result['ended_at']
        assert result['answer']['sources'] and not result['timer_enabled']
        assert client.post(f"/api/games/{game['id']}/give-up").json() == result


def test_review_preserves_original_verdict_and_refunds_withdrawal(setup):
    app, judge, _, _ = setup
    with TestClient(app) as client:
        game = start(client, question_limit=2)
        move(client, game, 'questions', '曾归附过其他阵营吗？')
        result = move(client, game, 'questions', '曾正式任官吗？', 'request-0002').json()
        first, second = result['events']
        store = app.state.store
        store.correct_event(first['id'], '否', '按史料更正。', '2026-01-01T00:00:00+00:00')
        store.correct_event(second['id'], None, '依据不足，请勿据此排除候选。', '2026-01-01T00:00:00+00:00')
        reviewed = client.get(f"/api/games/{game['id']}").json()
        assert reviewed['question_count'] == 1 and reviewed['answer'] is None
        assert reviewed['events'][0]['answer'] == '否'
        assert reviewed['events'][0]['original_answer'] == '是'
        assert reviewed['events'][1]['withdrawn'] is True
        assert store.event(game['id'], 'request-0001')['answer'] == '是'
        assert move(client, game, 'questions', '是男性吗？', 'request-0003').status_code == 200
        assert len(judge.history_seen) == 1 and judge.history_seen[0]['answer'] == '否'


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_exceed_limits(setup):
    app, judge, _, _ = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        await client.get('/api/meta')
        game = (await client.post('/api/games', json={'question_limit': 1})).json()
        results = await asyncio.gather(*[
            client.post(f"/api/games/{game['id']}/questions", json={'text': '是男性吗？', 'request_id': f'request-{n:04}'})
            for n in range(4)
        ])
        assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
        assert judge.question_calls == 1


def test_corpus_checks_boundaries(tmp_path):
    from server.app import load_characters
    path = tmp_path / 'bad.json'
    for changed in ({'active_year': 183}, {'active_year': 317}, {'death_year': 183}, {'birth_year': 185}, {'sources': []}):
        path.write_text(json.dumps([{**PEOPLE[0], **changed}]))
        with pytest.raises(ValueError): load_characters(path)
