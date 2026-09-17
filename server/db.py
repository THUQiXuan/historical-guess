"""SQLite storage. Secret answers and judge notes never leave this module's public view."""
import json
import os
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path, characters: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                    preset TEXT NOT NULL, scope TEXT NOT NULL, scope_description TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL, character TEXT NOT NULL,
                    question_limit INTEGER, guess_limit INTEGER, timer_enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL, ended_at TEXT,
                    question_count INTEGER NOT NULL DEFAULT 0,
                    guess_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS games_owner_date ON games(owner,created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL REFERENCES games(id),
                    request_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    answer TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(game_id,request_id)
                );
                CREATE TABLE IF NOT EXISTS corrections (
                    event_id INTEGER PRIMARY KEY REFERENCES events(id),
                    answer TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL,
                    request_id TEXT NOT NULL, input_json TEXT NOT NULL,
                    user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
                    suggested_scope TEXT, user_created_at TEXT NOT NULL,
                    assistant_created_at TEXT NOT NULL,
                    UNIQUE(owner,target,request_id)
                );
                CREATE INDEX IF NOT EXISTS chat_owner_target ON chat_turns(owner,target,user_created_at);
                PRAGMA user_version=3;
            """)
            db.executemany(
                "INSERT INTO characters(id,name,record) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,record=excluded.record",
                [(p['id'], p['name'], json.dumps(p, ensure_ascii=False)) for p in characters],
            )
        os.chmod(path, 0o600)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def get(self, game_id: str, owner: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM games WHERE id=? AND owner=?", (game_id, owner)).fetchone()
        return dict(row) if row else None

    def event(self, game_id: str, request_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE game_id=? AND request_id=?", (game_id, request_id)).fetchone()
        return dict(row) if row else None

    def public(self, row: dict, include_events=True):
        result = {k: v for k, v in row.items() if k not in ('owner', 'character')}
        result['timer_enabled'] = bool(result['timer_enabled'])
        result['answer'] = json.loads(row['character']) if row['status'] != 'active' else None
        result['events'] = []
        if include_events:
            with self.connect() as db:
                for event in db.execute('''
                    SELECT e.id,e.kind,e.text,e.answer,e.created_at,c.event_id AS correction_id,
                           c.answer AS revised_answer,c.note AS review_note
                    FROM events e LEFT JOIN corrections c ON c.event_id=e.id
                    WHERE e.game_id=? ORDER BY e.id''', (row['id'],)):
                    item = dict(event)
                    corrected = item.pop('correction_id') is not None
                    revised = item.pop('revised_answer')
                    if corrected:
                        item['original_answer'] = item['answer']
                        item['corrected'] = True
                        item['withdrawn'] = revised is None
                        if revised is not None:
                            item['answer'] = revised
                    else:
                        item.pop('review_note')
                    result['events'].append(item)
        return result

    def create(self, game: dict):
        columns = ','.join(game)
        with self.connect() as db:
            db.execute(f"INSERT INTO games({columns}) VALUES({','.join('?' for _ in game)})", tuple(game.values()))

    def history(self, owner: str, offset: int, limit: int):
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM games WHERE owner=?', (owner,)).fetchone()[0]
            rows = db.execute('SELECT * FROM games WHERE owner=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                              (owner, limit, offset)).fetchall()
        return {'items': [self.public(dict(r), False) for r in rows], 'total': count}

    def record(self, game: dict, request_id: str, kind: str, text: str, answer: str, reason: str, now: str):
        qcount = game['question_count'] + int(kind == 'question' and answer != '无法回答')
        gcount = game['guess_count'] + int(kind == 'guess')
        status = game['status']
        if kind == 'guess':
            if answer == '正确':
                status = 'won'
            elif game['guess_limit'] is not None and gcount >= game['guess_limit']:
                status = 'lost'
        with self.connect() as db:
            db.execute('INSERT INTO events(game_id,request_id,kind,text,answer,reason,created_at) VALUES(?,?,?,?,?,?,?)',
                       (game['id'], request_id, kind, text, answer, reason, now))
            db.execute('UPDATE games SET question_count=?,guess_count=?,status=?,ended_at=? WHERE id=?',
                       (qcount, gcount, status, now if status != 'active' else None, game['id']))

    def abandon(self, game_id: str, now: str):
        with self.connect() as db:
            db.execute("UPDATE games SET status='abandoned',ended_at=? WHERE id=? AND status='active'", (now, game_id))

    def correct_event(self, event_id: int, answer: str | None, note: str, now: str):
        """Offline, evidence-backed maintenance only; stop the server first.

        Preserve the original verdict. None withdraws an unsupported verdict and
        refunds the valid-question count. Public notes must not reveal identity.
        There is deliberately no player-accessible correction endpoint.
        """
        if answer not in ('是', '否', None):
            raise ValueError('A correction must be yes/no, or a withdrawal')
        with self.connect() as db:
            event = db.execute('SELECT game_id,kind FROM events WHERE id=?', (event_id,)).fetchone()
            if not event or event['kind'] != 'question':
                raise ValueError('Only existing questions can be reviewed')
            db.execute('INSERT INTO corrections(event_id,answer,note,created_at) VALUES(?,?,?,?)',
                       (event_id, answer, note, now))
            count = db.execute('''SELECT COUNT(*) FROM events e
                LEFT JOIN corrections c ON c.event_id=e.id
                WHERE e.game_id=? AND e.kind='question'
                AND CASE WHEN c.event_id IS NULL THEN e.answer ELSE c.answer END
                    IN ('是','否')''', (event['game_id'],)).fetchone()[0]
            db.execute('UPDATE games SET question_count=? WHERE id=?', (count, event['game_id']))

    def chat_turn(self, owner: str, target: str, request_id: str):
        with self.connect() as db:
            row = db.execute('SELECT * FROM chat_turns WHERE owner=? AND target=? AND request_id=?',
                             (owner, target, request_id)).fetchone()
        return dict(row) if row else None

    def correct_guess(self, event_id: int, note: str, now: str):
        """Offline repair of a verified false-positive guess; keep the audit.

        The answer was already revealed, so an incorrectly won game becomes
        an ended/revealed game rather than resuming with a known answer.
        """
        with self.connect() as db:
            event = db.execute('SELECT game_id,kind,answer FROM events WHERE id=?', (event_id,)).fetchone()
            if not event or event['kind'] != 'guess' or event['answer'] != '正确':
                raise ValueError('Only a verified false-positive guess can be repaired')
            db.execute('INSERT INTO corrections(event_id,answer,note,created_at) VALUES(?,?,?,?)',
                       (event_id, '错误', note, now))
            db.execute("UPDATE games SET status='abandoned',ended_at=COALESCE(ended_at,?) WHERE id=? AND status='won'",
                       (now, event['game_id']))

    def chat_messages(self, owner: str, target: str):
        messages = []
        with self.connect() as db:
            rows = db.execute('SELECT * FROM chat_turns WHERE owner=? AND target=? ORDER BY rowid', (owner, target))
            for turn in rows:
                messages.append({'id': turn['id'] + '-user', 'role': 'user', 'text': turn['user_text'],
                                 'created_at': turn['user_created_at'], 'suggested_scope': None})
                messages.append({'id': turn['id'] + '-assistant', 'role': 'assistant', 'text': turn['assistant_text'],
                                 'created_at': turn['assistant_created_at'],
                                 'suggested_scope': json.loads(turn['suggested_scope']) if turn['suggested_scope'] else None})
        return messages

    def save_chat(self, turn_id, owner, target, request_id, input_json, text, reply, suggestion, started_at, ended_at):
        # Persist both halves together. A failed model call leaves no half-turn
        # and can be retried with the same request ID without duplicating text.
        with self.connect() as db:
            db.execute('''INSERT INTO chat_turns(id,owner,target,request_id,input_json,user_text,
                assistant_text,suggested_scope,user_created_at,assistant_created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn_id, owner, target, request_id, input_json, text, reply,
                 json.dumps(suggestion, ensure_ascii=False) if suggestion else None, started_at, ended_at))

    def decision_notes(self, game_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT id AS event_id,reason FROM events WHERE game_id=? ORDER BY id', (game_id,))]
