"""Chat stays separate from scored moves and from the judge's hidden answer."""
import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.agent import AgentError
from server.app import create_app
from test_app import FixtureJudge, PEOPLE, move, start


class ChatJudge(FixtureJudge):
    def __init__(self):
        super().__init__()
        self.chat_calls = []
        self.chat_fail = False
        self.chat_override = None

    async def chat(self, context, history, text):
        self.chat_calls.append(copy.deepcopy({'context': context, 'history': history, 'text': text}))
        if self.chat_fail:
            raise AgentError('temporary chat failure')
        # Let concurrent requests overlap unless the application serializes them.
        await asyncio.sleep(0.005)
        if self.chat_override is not None:
            return copy.deepcopy(self.chat_override)
        return {
            'text': '可以按史籍记载继续讨论，也可以调整下一局的范围。',
            'suggested_scope': {'preset': 'sanguozhi', 'scope': '女性人物'}
            if context['mode'] == 'scope' else None,
        }


@pytest.fixture
def chat_setup(tmp_path):
    data = tmp_path / 'characters.json'
    data.write_text(json.dumps(PEOPLE, ensure_ascii=False))
    db = tmp_path / 'chat.sqlite3'
    judge = ChatJudge()
    return create_app(db, judge, data), judge, db, data


def send_chat(client, text='能帮我缩小人物范围吗？', request_id='chat-request-0001', **kwargs):
    return client.post('/api/chat', json={'text': text, 'request_id': request_id, **kwargs})


def read_chat(client, game_id=None):
    return client.get('/api/chat', params={'game_id': game_id} if game_id else {})


def assert_messages(payload, rounds, mode, game_id=None):
    assert payload['mode'] == mode
    assert payload['game_id'] == game_id
    messages = payload['messages']
    assert len(messages) == rounds * 2
    assert [message['role'] for message in messages] == ['user', 'assistant'] * rounds
    assert len({message['id'] for message in messages}) == len(messages)
    for message in messages:
        assert message['text'] and isinstance(message['text'], str)
        assert message['created_at']
        assert 'suggested_scope' in message
        suggestion = message['suggested_scope']
        if suggestion is not None:
            assert set(suggestion) == {'preset', 'scope'}
            assert suggestion['preset'] in ('broad', 'sanguozhi')
            assert isinstance(suggestion['scope'], str) and len(suggestion['scope']) <= 500
        if message['role'] == 'user':
            assert suggestion is None
    return messages


def nested_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from nested_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_keys(item)


def test_scope_chat_returns_structured_suggestion_without_creating_game(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        initial = read_chat(client)
        assert initial.status_code == 200
        assert_messages(initial.json(), 0, 'scope')
        response = send_chat(client, preset='sanguozhi', scope='文臣')
        assert response.status_code == 200, response.text
        messages = assert_messages(response.json(), 1, 'scope')
        assert messages[1]['suggested_scope'] == {'preset': 'sanguozhi', 'scope': '女性人物'}
        assert messages[1]['text'] not in ('是', '否', '无法回答', '正确', '错误')
        assert read_chat(client).json() == response.json()
        assert client.get('/api/games').json()['total'] == 0
        context = judge.chat_calls[0]['context']
        assert context['mode'] == 'scope'
        serialized = json.dumps(context, ensure_ascii=False)
        assert 'sanguozhi' in serialized and '文臣' in serialized
        assert judge.chat_calls[0]['history'] == []


def test_active_chat_hides_judge_secrets_and_does_not_spend_limited_moves(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        game = start(client, question_limit=1, guess_limit=1, scope='男性人物')
        question = move(client, game, 'questions', '是男性吗？').json()
        assert question['question_count'] == 1
        for number in range(16):
            response = send_chat(client, text=f'继续解释规则，第{number + 1}次。',
                                 request_id=f'chat-long-{number:04}', game_id=game['id'])
            assert response.status_code == 200, response.text
        assert_messages(response.json(), 16, 'active', game['id'])
        latest = client.get(f"/api/games/{game['id']}").json()
        assert latest['question_count'] == 1 and latest['guess_count'] == 0
        assert latest['events'] == question['events']
        assert latest['status'] == 'active' and latest['answer'] is None
        assert judge.question_calls == 1 and judge.guess_calls == 0
        for call in judge.chat_calls:
            context = call['context']
            assert context['mode'] == 'active'
            forbidden_keys = {'character', 'aliases', 'hidden_snapshot', 'character_snapshot',
                              'decision_notes', 'reason', 'reasons', 'events'}
            assert not (forbidden_keys & set(nested_keys(context)))
            encoded = json.dumps(context, ensure_ascii=False)
            for secret in ('曹操', '曹孟德', 'cao-cao', '私密分析', '此文字绝不能返给客户端'):
                assert secret not in encoded
            assert '男性人物' in encoded and 'candidate_count' in encoded
        # History means this conversation only, and excludes the pending user turn.
        assert len(judge.chat_calls[-1]['history']) == 30


@pytest.mark.parametrize('ending', ['won', 'lost', 'abandoned'])
def test_finished_game_chat_receives_answer_notes_and_events_for_review(chat_setup, ending):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        game = start(client, guess_limit=1)
        move(client, game, 'questions', '是男性吗？')
        if ending == 'abandoned':
            finished = client.post(f"/api/games/{game['id']}/give-up").json()
        else:
            finished = move(client, game, 'guesses', '曹操' if ending == 'won' else '刘备',
                            'finish-request-0001').json()
        assert finished['status'] == ending
        response = send_chat(client, '复盘刚才的问题及判断依据。', game_id=game['id'])
        assert response.status_code == 200, response.text
        assert_messages(response.json(), 1, 'review', game['id'])
        context = judge.chat_calls[-1]['context']
        assert context['mode'] == 'review'
        assert context['character']['name'] == '曹操'
        assert context['character']['aliases'] == ['曹孟德']
        assert context['game']['events'] == finished['events']
        notes = json.dumps(context['decision_notes'], ensure_ascii=False)
        assert '私密分析曹操' in notes
        assert client.get(f"/api/games/{game['id']}").json() == finished


def test_idempotency_conflict_and_failure_retry_are_atomic(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        game = start(client, question_limit=1, guess_limit=1)
        judge.chat_fail = True
        failed = send_chat(client, game_id=game['id'])
        assert failed.status_code == 503
        assert_messages(read_chat(client, game['id']).json(), 0, 'active', game['id'])
        assert client.get(f"/api/games/{game['id']}").json() == game
        judge.chat_fail = False
        first = send_chat(client, game_id=game['id'])
        assert first.status_code == 200, first.text
        second = send_chat(client, game_id=game['id'])
        assert second.status_code == 200 and second.json() == first.json()
        assert len(judge.chat_calls) == 2  # One failure and one successful inference.
        conflict = send_chat(client, '不同的内容。', game_id=game['id'])
        assert conflict.status_code == 409
        assert len(judge.chat_calls) == 2
        assert_messages(read_chat(client, game['id']).json(), 1, 'active', game['id'])


def test_session_ownership_and_scope_and_game_threads_are_isolated(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as owner:
        first_game = start(owner)
        second_game = start(owner)
        # Identical request IDs belong to separate conversation threads.
        assert send_chat(owner, '局前范围标记。').status_code == 200
        assert send_chat(owner, '第一局聊天标记。', game_id=first_game['id']).status_code == 200
        assert send_chat(owner, '第二局聊天标记。', game_id=second_game['id']).status_code == 200
        assert all(call['history'] == [] for call in judge.chat_calls)
        send_chat(owner, '局前继续。', request_id='scope-follow-0001')
        history = json.dumps(judge.chat_calls[-1]['history'], ensure_ascii=False)
        assert '局前范围标记' in history
        assert '第一局聊天标记' not in history and '第二局聊天标记' not in history
        assert 'character' not in set(nested_keys(judge.chat_calls[-1]['context']))
        for game, marker, other in ((first_game, '第一局聊天标记', '第二局聊天标记'),
                                    (second_game, '第二局聊天标记', '第一局聊天标记')):
            payload = read_chat(owner, game['id']).json()
            assert_messages(payload, 1, 'active', game['id'])
            encoded = json.dumps(payload, ensure_ascii=False)
            assert marker in encoded and other not in encoded and '局前范围标记' not in encoded
        with TestClient(app) as stranger:
            assert_messages(read_chat(stranger).json(), 0, 'scope')
            previous_calls = len(judge.chat_calls)
            assert read_chat(stranger, first_game['id']).status_code == 404
            assert send_chat(stranger, game_id=first_game['id']).status_code == 404
            assert len(judge.chat_calls) == previous_calls
            assert send_chat(stranger, '另一浏览器范围标记。').status_code == 200
            assert '另一浏览器范围标记' not in json.dumps(read_chat(owner).json(), ensure_ascii=False)


def test_chat_history_survives_app_restart(chat_setup):
    app, _, db, data = chat_setup
    with TestClient(app) as client:
        game = start(client)
        before_scope = send_chat(client, '保存局前聊天。').json()
        before_game = send_chat(client, '保存局内聊天。', game_id=game['id']).json()
        cookies = dict(client.cookies)
    judge = ChatJudge()
    restarted = create_app(db, judge, data)
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        assert read_chat(client).json() == before_scope
        assert read_chat(client, game['id']).json() == before_game
        response = send_chat(client, '重启后继续。', request_id='restart-chat-0002', game_id=game['id'])
        assert response.status_code == 200, response.text
        assert_messages(response.json(), 2, 'active', game['id'])
        history = judge.chat_calls[0]['history']
        assert len(history) == 2
        assert '保存局内聊天' in json.dumps(history, ensure_ascii=False)
        assert '保存局前聊天' not in json.dumps(history, ensure_ascii=False)


def test_idempotency_includes_draft_preset_and_scope(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        first = send_chat(client, preset='sanguozhi', scope='文臣')
        assert first.status_code == 200, first.text
        replay = send_chat(client, preset='sanguozhi', scope='文臣')
        assert replay.status_code == 200 and replay.json() == first.json()
        for changed in ({'preset': 'broad', 'scope': '文臣'},
                        {'preset': 'sanguozhi', 'scope': '武将'}):
            conflict = send_chat(client, **changed)
            assert conflict.status_code == 409, conflict.text
        assert len(judge.chat_calls) == 1
        assert_messages(read_chat(client).json(), 1, 'scope')


@pytest.mark.parametrize('invalid', [
    {'text': ''}, {'text': '   \n\t'}, {'text': '问' * 10001},
    {'request_id': 'short'}, {'request_id': 'bad request id'}, {'request_id': 'r' * 101},
    {'preset': 'unknown'}, {'scope': '人' * 501},
])
def test_chat_rejects_invalid_input_without_writing_messages(chat_setup, invalid):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        body = {'text': '讨论范围。', 'request_id': 'valid-request-0001', **invalid}
        response = client.post('/api/chat', json=body)
        assert response.status_code == 422, response.text
        assert judge.chat_calls == []
        assert_messages(read_chat(client).json(), 0, 'scope')


def test_chat_accepts_long_messages_and_defaults(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        response = send_chat(client, '史' * 10000)
        assert response.status_code == 200, response.text
        assert len(response.json()['messages'][0]['text']) == 10000
        assert 'broad' in json.dumps(judge.chat_calls[0]['context'], ensure_ascii=False)


def test_model_context_window_does_not_discard_persisted_chat_history(chat_setup):
    app, judge, _, _ = chat_setup
    with TestClient(app) as client:
        game = start(client)
        for number in range(7):
            response = send_chat(client, str(number) + '史' * 9999,
                                 request_id=f'large-history-{number:04}', game_id=game['id'])
            assert response.status_code == 200, response.text
        messages = assert_messages(read_chat(client, game['id']).json(), 7, 'active', game['id'])
        assert messages[0]['text'] == '0' + '史' * 9999
        history = judge.chat_calls[-1]['history']
        assert sum(len(message['text']) for message in history) <= 48000
        assert history[0]['role'] == 'user' and history[-1]['role'] == 'assistant'
        assert judge.chat_calls[-1]['context']['history_omitted_count'] > 0
        assert client.get(f"/api/games/{game['id']}").json()['question_count'] == 0


@pytest.mark.parametrize('suggestion', [
    '女性人物', {'preset': 'unknown', 'scope': ''},
    {'preset': 'broad', 'scope': ['女性人物']}, {'preset': 'broad', 'scope': '人' * 501},
])
def test_invalid_agent_scope_suggestions_do_not_persist_partial_turns(chat_setup, suggestion):
    app, judge, _, _ = chat_setup
    judge.chat_override = {'text': '范围建议。', 'suggested_scope': suggestion}
    with TestClient(app) as client:
        response = send_chat(client)
        assert response.status_code == 503, response.text
        assert_messages(read_chat(client).json(), 0, 'scope')


@pytest.mark.asyncio
async def test_concurrent_chat_retries_write_one_turn(chat_setup):
    app, judge, _, _ = chat_setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        await client.get('/api/meta')
        body = {'text': '解释一下范围。', 'request_id': 'concurrent-chat-0001'}
        responses = await asyncio.gather(*[client.post('/api/chat', json=body) for _ in range(5)])
        assert all(response.status_code == 200 for response in responses)
        assert len(judge.chat_calls) == 1
        payload = (await client.get('/api/chat')).json()
        assert_messages(payload, 1, 'scope')
        assert all(response.json() == payload for response in responses)
