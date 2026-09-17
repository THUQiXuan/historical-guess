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
                PRAGMA user_version=1;
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
                result['events'] = [dict(e) for e in db.execute(
                    'SELECT id,kind,text,answer,created_at FROM events WHERE game_id=? ORDER BY id', (row['id'],))]
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
