"""Persistent accounts, catalog, sources, reviews, progress, rooms and personal collections."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import sqlite3
import time
import threading

from models import MediaItem, User
from engagement_schema import SCHEMA as ENGAGEMENT_SCHEMA
from film_stats_cache import SCHEMA as STATS_CACHE_SCHEMA

EMPTY_STATE = dict(watched=[], ratings={}, watchlist=[], clips=[], settings={}, rooms={}, friends=[])

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('ADMIN','USER')),
    active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS films (
    id INTEGER PRIMARY KEY, data TEXT NOT NULL, art TEXT NOT NULL,
    torrent BLOB, torrent_name TEXT, file_id INTEGER,
    files TEXT NOT NULL DEFAULT '[]', revision TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY, failures INTEGER NOT NULL, last_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS password_resets_user ON password_resets(user_id, created_at);
CREATE TABLE IF NOT EXISTS ratings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 10), updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, movie_id));
CREATE INDEX IF NOT EXISTS ratings_movie ON ratings(movie_id);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 10),
    text TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(user_id, movie_id));
CREATE INDEX IF NOT EXISTS reviews_movie ON reviews(movie_id, updated_at);
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL,
    position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
    watched_seconds REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, movie_id));
CREATE INDEX IF NOT EXISTS progress_recent ON progress(user_id, updated_at);
CREATE TABLE IF NOT EXISTS film_sources (
    movie_id INTEGER NOT NULL, source INTEGER NOT NULL, torrent BLOB NOT NULL, name TEXT NOT NULL,
    files TEXT NOT NULL, info_hash TEXT NOT NULL, magnet TEXT, added_at REAL NOT NULL,
    PRIMARY KEY(movie_id, source));
CREATE TABLE IF NOT EXISTS episode_progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
    position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, movie_id, file_id));
CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, movie_id INTEGER NOT NULL,
    room_code TEXT, started_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS views_movie ON views(movie_id);
CREATE TABLE IF NOT EXISTS media_info (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tariff TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rooms (
    code TEXT PRIMARY KEY, movie_id INTEGER NOT NULL, host_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL, settings TEXT NOT NULL, state TEXT NOT NULL,
    created_at REAL NOT NULL, ended_at REAL);
CREATE INDEX IF NOT EXISTS rooms_active ON rooms(ended_at);
CREATE TABLE IF NOT EXISTS room_members (
    code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id),
    joined_at REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(code, user_id));
CREATE INDEX IF NOT EXISTS room_members_user ON room_members(user_id);
CREATE TABLE IF NOT EXISTS room_messages (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    user_id INTEGER, name TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS room_messages_code ON room_messages(code, id);
'''

FILM_COLUMNS = {'magnet': 'TEXT', 'info_hash': 'TEXT', 'episodes': "TEXT NOT NULL DEFAULT '[]'", 'imdb_id': 'TEXT',
                'tmdb_id': 'INTEGER', 'tmdb_kind': 'TEXT'}
USER_COLUMNS = {'last_seen': 'REAL', 'plan': "TEXT NOT NULL DEFAULT 'free'", 'plan_until': 'REAL'}
PROGRESS_COLUMNS = {'file_id': 'INTEGER'}      # series: the episode the film's progress row points to
PREFERENCE_COLUMNS = {
    'liked_movie_ids': "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(liked_movie_ids))",
    'onboarding_completed_at': 'REAL',
}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stats_lock = threading.Lock()
        self._stats_version = None
        self._stats_cache = None
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            for table, columns in (('films', FILM_COLUMNS), ('users', USER_COLUMNS), ('progress', PROGRESS_COLUMNS)):
                existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                for name, kind in columns.items():
                    if name not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
            self._migrate_ratings(db)
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS films_imdb_id ON films(imdb_id) WHERE imdb_id IS NOT NULL')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS films_tmdb_id ON films(tmdb_kind,tmdb_id) WHERE tmdb_id IS NOT NULL')
            db.executescript(ENGAGEMENT_SCHEMA)
            db.executescript(STATS_CACHE_SCHEMA)
            existing = {row['name'] for row in db.execute('PRAGMA table_info(user_preferences)')}
            for name, kind in PREFERENCE_COLUMNS.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE user_preferences ADD COLUMN {name} {kind}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=NORMAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _migrate_ratings(self, db):
        """Ratings used to live only inside the per-user JSON; mirror them into a table once."""
        if db.execute('SELECT 1 FROM ratings LIMIT 1').fetchone():
            return
        for row in db.execute('SELECT user_id, data FROM user_state').fetchall():
            try:
                ratings = json.loads(row['data']).get('ratings', {})
            except ValueError:
                continue
            for movie_id, score in ratings.items():
                if str(movie_id).isdigit() and type(score) is int and 1 <= score <= 10:
                    db.execute('INSERT OR IGNORE INTO ratings VALUES(?,?,?,?)', (row['user_id'], int(movie_id), score, time.time()))

    def seed(self, catalog, art):
        with self.connect() as db:
            for film in catalog:
                db.execute('INSERT OR IGNORE INTO films(id,data,art,revision) VALUES(?,?,?,?)',
                           (film.id, json.dumps(asdict(film), ensure_ascii=False),
                            json.dumps(art[film.id], ensure_ascii=False), secrets.token_hex(12)))

    # ------------------------------------------------------------------ accounts
    def accounts_exist(self):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is not None

    def account(self, *, user_id=None, email=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE ' + ('id=?' if user_id is not None else 'email=?'),
                             (user_id if user_id is not None else email,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def user(row):
        if not row:
            return None
        plan = row.get('plan') if isinstance(row, dict) else row['plan']
        until = row.get('plan_until') if isinstance(row, dict) else row['plan_until']
        return User(row['id'], row['email'], row['username'], row['role'],
                    is_subscribed=plan == 'plus' and (until or 0) > time.time())

    def register(self, email, username, password_hash):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            role = 'USER' if db.execute('SELECT 1 FROM users LIMIT 1').fetchone() else 'ADMIN'
            cursor = db.execute('INSERT INTO users(email,username,password_hash,role) VALUES(?,?,?,?)',
                                (email, username, password_hash, role))
            return cursor.lastrowid

    def users(self, query='', limit=None, offset=0):
        sql = '''SELECT u.id,u.email,u.username,u.role,u.active,u.created_at,u.last_seen,u.plan,
                 (SELECT COUNT(*) FROM views v WHERE v.user_id=u.id) AS views,
                 (SELECT COUNT(*) FROM reviews r WHERE r.user_id=u.id) AS reviews
                 FROM users u'''
        args = ()
        if query:
            sql += ' WHERE u.email LIKE ? OR u.username LIKE ?'
            args = (f'%{query}%', f'%{query}%')
        sql += ' ORDER BY u.id'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            args = (*args, limit, offset)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def users_count(self, query=''):
        with self.connect() as db:
            if query:
                return db.execute('SELECT COUNT(*) FROM users WHERE email LIKE ? OR username LIKE ?',
                                  (f'%{query}%', f'%{query}%')).fetchone()[0]
            return db.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    def touch_user(self, user_id):
        with self.connect() as db:
            db.execute('UPDATE users SET last_seen=? WHERE id=?', (time.time(), user_id))

    def change_role(self, user_id, role, active):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise ValueError('Пользователь не найден.')
            if row['role'] == 'ADMIN' and row['active'] and (role != 'ADMIN' or not active):
                count = db.execute("SELECT COUNT(*) FROM users WHERE role='ADMIN' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError('Нельзя отключить последнего администратора.')
            db.execute('UPDATE users SET role=?,active=?,session_version=session_version+1 WHERE id=?',
                       (role, int(active), user_id))

    def reset_sessions(self, user_id):
        with self.connect() as db:
            db.execute('UPDATE users SET session_version=session_version+1 WHERE id=?', (user_id,))

    def change_password(self, user_id, password_hash):
        with self.connect() as db:
            db.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',
                       (password_hash, user_id))
            db.execute('DELETE FROM password_resets WHERE user_id=?', (user_id,))

    def create_password_reset(self, user_id, token_hash, ttl, cooldown):
        """Store a reset token hash; False if the previous letter was sent less than ``cooldown`` seconds ago."""
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM password_resets WHERE expires_at<?', (now,))
            if db.execute('SELECT 1 FROM password_resets WHERE user_id=? AND created_at>?',
                          (user_id, now - cooldown)).fetchone():
                return False
            db.execute('INSERT INTO password_resets VALUES(?,?,?,?)', (token_hash, user_id, now, now + ttl))
            return True

    def password_reset(self, token_hash):
        """Active account that owns a valid, unexpired reset token."""
        with self.connect() as db:
            row = db.execute('''SELECT u.* FROM password_resets r JOIN users u ON u.id=r.user_id
                                WHERE r.token_hash=? AND r.expires_at>? AND u.active=1''',
                             (token_hash, time.time())).fetchone()
            return dict(row) if row else None

    def reset_password(self, token_hash, password_hash):
        """Use a token once: set the password, revoke sessions and every other reset link of the account."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT u.id FROM password_resets r JOIN users u ON u.id=r.user_id
                                WHERE r.token_hash=? AND r.expires_at>? AND u.active=1''',
                             (token_hash, time.time())).fetchone()
            if not row:
                return None
            db.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',
                       (password_hash, row['id']))
            db.execute('DELETE FROM password_resets WHERE user_id=?', (row['id'],))
            return row['id']

    def set_plan(self, user_id, plan, days=30):
        until = time.time() + days * 86400 if plan == 'plus' else None
        with self.connect() as db:
            db.execute('UPDATE users SET plan=?,plan_until=? WHERE id=?', (plan, until, user_id))

    def add_payment(self, user_id, tariff, amount, status):
        with self.connect() as db:
            db.execute('INSERT INTO payments(user_id,tariff,amount,status,created_at) VALUES(?,?,?,?,?)',
                       (user_id, tariff, amount, status, time.time()))

    def payments(self, user_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM payments WHERE user_id=? ORDER BY id DESC', (user_id,))]

    # ------------------------------------------------------------------ films
    def films(self, include_hidden=False):
        with self.connect() as db:
            rows = db.execute('''SELECT id,data,art,torrent IS NOT NULL AS has_torrent,torrent_name,file_id,files,
                                 revision,updated_at,magnet,info_hash,episodes,imdb_id,tmdb_id,tmdb_kind FROM films ORDER BY id''').fetchall()
        result = [self.decode_film(row) for row in rows]
        return [r for r in result if include_hidden or not r['film'].is_hidden]

    @staticmethod
    def decode_film(row):
        result = dict(row)
        result['film'] = MediaItem(**json.loads(result.pop('data')))
        result['art'] = json.loads(result['art'])
        result['files'] = json.loads(result['files'])
        result['episodes'] = json.loads(result.get('episodes') or '[]')
        if 'has_torrent' in result:
            result['torrent'] = bool(result.pop('has_torrent'))
        return result

    def film(self, movie_id, include_hidden=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM films WHERE id=?', (movie_id,)).fetchone()
        if not row:
            return None
        result = self.decode_film(row)
        return result if include_hidden or not result['film'].is_hidden else None

    def save_film(self, data, art, torrent=None, torrent_name=None, files=None, file_id=None, movie_id=None,
                  magnet=None, info_hash=None, episodes=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM films WHERE id=?', (movie_id,)).fetchone() if movie_id else None
            if movie_id and not previous:
                raise ValueError('Фильм не найден.')
            if not movie_id:
                movie_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM films').fetchone()[0]
            data['id'] = movie_id
            revision = secrets.token_hex(12)
            if torrent is None and previous:
                torrent, torrent_name = previous['torrent'], previous['torrent_name']
                files = json.loads(previous['files']) if files is None else files
                magnet = previous['magnet'] if magnet is None else magnet
                info_hash = previous['info_hash'] if info_hash is None else info_hash
                if episodes is None:
                    episodes = json.loads(previous['episodes'] or '[]')
                if previous['file_id'] == file_id:
                    revision = previous['revision']   # metadata-only edit keeps playback sessions
            db.execute('''INSERT INTO films(id,data,art,torrent,torrent_name,file_id,files,revision,magnet,info_hash,episodes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,art=excluded.art,
                torrent=excluded.torrent,torrent_name=excluded.torrent_name,file_id=excluded.file_id,
                files=excluded.files,revision=excluded.revision,magnet=excluded.magnet,info_hash=excluded.info_hash,
                episodes=excluded.episodes,updated_at=CURRENT_TIMESTAMP''',
                (movie_id, json.dumps(data, ensure_ascii=False), json.dumps(art, ensure_ascii=False),
                 torrent, torrent_name, file_id, json.dumps(files or [], ensure_ascii=False), revision, magnet, info_hash,
                 json.dumps(episodes or [], ensure_ascii=False)))
        return movie_id

    def import_imdb(self, db, record, publish=True, credits=False):
        """Import metadata inside the caller's transaction, preserving sources and edits."""
        from imdb_import import normalized
        key = record['imdb_id']
        row = db.execute('SELECT * FROM films WHERE imdb_id=?', (key,)).fetchone()
        if row is None:
            aliases = {normalized(record['title']), normalized(record['original']), normalized(record['display_title'])}
            matches = []
            for candidate in db.execute('SELECT * FROM films WHERE imdb_id IS NULL'):
                data, art = json.loads(candidate['data']), json.loads(candidate['art'])
                if (data['year'] == record['year'] and data.get('kind', 'movie') == record['kind']
                        and {normalized(data['title']), normalized(art.get('original', ''))} & aliases):
                    matches.append(candidate)
            if len(matches) > 1:
                return None, 'conflict'
            row = matches[0] if matches else None
        stamp = time.time()
        imdb_art = dict(imdb_id=key, imdb_url=f'https://www.imdb.com/title/{key}/',
                        imdb=str(record['rating']) if record['rating'] is not None else '—',
                        imdb_votes=record['votes'], imdb_updated_at=stamp, imdb_source='IMDb Non-Commercial Datasets')
        if row:
            data, art = json.loads(row['data']), json.loads(row['art'])
            art.update(imdb_art)
            if record['rating'] is not None:
                data['rating'] = record['rating']
            if not art.get('original'):
                art['original'] = record['original']
            if credits and record['directors'] and not art.get('director'):
                art['director'] = ', '.join(record['directors'])
            if credits and record['actors'] and not art.get('actors'):
                art['actors'] = record['actors']
            db.execute('UPDATE films SET data=?,art=?,imdb_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), json.dumps(art, ensure_ascii=False), key, row['id']))
            return row['id'], 'updated'
        movie_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM films').fetchone()[0]
        data = dict(id=movie_id, title=record['display_title'], description='Описание пока не добавлено.',
                    genres=record['genres'], year=record['year'] or 0, country='', rating=record['rating'] or 0,
                    duration_min=record['duration'] or 0, age_rating='—', is_hidden=not publish, kind=record['kind'])
        art = dict(slug='placeholder', original=record['original'], kp='—', tagline='',
                   director=', '.join(record['directors']), actors=record['actors'], **imdb_art)
        db.execute('INSERT INTO films(id,data,art,revision,imdb_id) VALUES(?,?,?,?,?)',
                   (movie_id, json.dumps(data, ensure_ascii=False), json.dumps(art, ensure_ascii=False), secrets.token_hex(12), key))
        return movie_id, 'added'

    def delete_film(self, movie_id):
        with self.connect() as db:
            db.execute('DELETE FROM films WHERE id=?', (movie_id,))
            db.execute('DELETE FROM reviews WHERE movie_id=?', (movie_id,))
            db.execute('DELETE FROM ratings WHERE movie_id=?', (movie_id,))
            db.execute('DELETE FROM progress WHERE movie_id=?', (movie_id,))
            db.execute('DELETE FROM episode_progress WHERE movie_id=?', (movie_id,))
            db.execute('DELETE FROM film_sources WHERE movie_id=?', (movie_id,))

    # ------------------------------------------------------------------ extra torrents of a series
    def sources(self, movie_id):
        """Extra torrents (source >= 1) without their bytes: dict(source, name, files, info_hash, magnet, added_at)."""
        with self.connect() as db:
            rows = db.execute('SELECT source,name,files,info_hash,magnet,added_at FROM film_sources WHERE movie_id=? ORDER BY source',
                              (movie_id,)).fetchall()
        return [dict(dict(r), files=json.loads(r['files'])) for r in rows]

    def source_torrent(self, movie_id, source):
        with self.connect() as db:
            row = db.execute('SELECT torrent FROM film_sources WHERE movie_id=? AND source=?', (movie_id, source)).fetchone()
        return row['torrent'] if row else None

    def add_source(self, movie_id, torrent, name, files, info_hash, episodes, magnet=None):
        """Store one more torrent and the merged episode list in one transaction; returns its number."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM film_sources WHERE movie_id=? AND info_hash=?', (movie_id, info_hash)).fetchone() or \
                    db.execute('SELECT 1 FROM films WHERE id=? AND info_hash=?', (movie_id, info_hash)).fetchone():
                raise ValueError('Эта раздача уже подключена к сериалу.')
            source = db.execute('SELECT COALESCE(MAX(source),0)+1 FROM film_sources WHERE movie_id=?', (movie_id,)).fetchone()[0]
            db.execute('INSERT INTO film_sources VALUES(?,?,?,?,?,?,?,?)',
                       (movie_id, source, torrent, name, json.dumps(files, ensure_ascii=False), info_hash, magnet, time.time()))
            db.execute('UPDATE films SET episodes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(episodes(source), ensure_ascii=False), movie_id))
            return source

    def delete_source(self, movie_id, source, episodes):
        with self.connect() as db:
            db.execute('DELETE FROM film_sources WHERE movie_id=? AND source=?', (movie_id, source))
            db.execute('UPDATE films SET episodes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(episodes, ensure_ascii=False), movie_id))

    def set_art(self, movie_id, art):
        """Presentation data only (hero video etc.): playback sessions keep running."""
        with self.connect() as db:
            db.execute('UPDATE films SET art=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(art, ensure_ascii=False), movie_id))

    def set_source(self, movie_id, torrent, name, files, file_id, info_hash=None):
        with self.connect() as db:
            db.execute('UPDATE films SET torrent=?,torrent_name=?,files=?,file_id=?,revision=?,info_hash=? WHERE id=?',
                       (torrent, name, json.dumps(files, ensure_ascii=False), file_id, secrets.token_hex(12), info_hash, movie_id))

    def film_stats(self):
        """Reuse aggregates until SQLite reports a rating, view or review change."""
        with self._stats_lock, self.connect() as db:
            db.execute('BEGIN')
            version = db.execute("SELECT version FROM cache_versions WHERE name='film_stats'").fetchone()[0]
            if self._stats_version == version and self._stats_cache is not None:
                return {i: dict(value) for i, value in self._stats_cache.items()}
            ratings = {r['movie_id']: (r['avg'], r['n']) for r in db.execute(
                'SELECT movie_id, AVG(score) AS avg, COUNT(*) AS n FROM ratings GROUP BY movie_id')}
            views = {r['movie_id']: r['n'] for r in db.execute('SELECT movie_id, COUNT(*) AS n FROM views GROUP BY movie_id')}
            reviews = {r['movie_id']: r['n'] for r in db.execute('SELECT movie_id, COUNT(*) AS n FROM reviews GROUP BY movie_id')}
            ids = set(ratings) | set(views) | set(reviews)
            result = {i: dict(avg=round(ratings[i][0], 1) if i in ratings else None, votes=ratings.get(i, (0, 0))[1],
                            views=views.get(i, 0), reviews=reviews.get(i, 0)) for i in ids}
            self._stats_version, self._stats_cache = version, result
            return {i: dict(value) for i, value in result.items()}

    # ------------------------------------------------------------------ media info
    def media_info(self, key):
        with self.connect() as db:
            row = db.execute('SELECT data FROM media_info WHERE key=?', (key,)).fetchone()
        return json.loads(row['data']) if row else None

    def save_media_info(self, key, data):
        with self.connect() as db:
            db.execute('INSERT INTO media_info VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                       (key, json.dumps(data, ensure_ascii=False), time.time()))

    # ------------------------------------------------------------------ personal state
    def state(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM user_state WHERE user_id=?', (user_id,)).fetchone()
            return json.loads(row['data']) if row else json.loads(json.dumps(EMPTY_STATE))

    def save_state(self, user_id, state):
        with self.connect() as db:
            db.execute('INSERT INTO user_state(user_id,data) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',
                       (user_id, json.dumps(state, ensure_ascii=False)))
            name = str(state.get('settings', {}).get('name', '')).strip()[:60]
            if len(name) >= 2:
                db.execute('UPDATE users SET username=? WHERE id=?', (name, user_id))
            ratings = {int(k): v for k, v in state.get('ratings', {}).items() if str(k).isdigit()}
            existing = {r['movie_id']: r['score'] for r in db.execute('SELECT movie_id, score FROM ratings WHERE user_id=?', (user_id,))}
            now = time.time()
            for movie_id in set(existing) - set(ratings):
                db.execute('DELETE FROM ratings WHERE user_id=? AND movie_id=?', (user_id, movie_id))
            for movie_id, score in ratings.items():
                if existing.get(movie_id) != score:
                    db.execute('INSERT INTO ratings VALUES(?,?,?,?) ON CONFLICT(user_id,movie_id) DO UPDATE SET score=excluded.score,updated_at=excluded.updated_at',
                               (user_id, movie_id, score, now))

    def update_state(self, user_id, mutator):
        state = self.state(user_id)
        mutator(state)
        self.save_state(user_id, state)
        return state

    # ------------------------------------------------------------------ reviews
    def reviews(self, movie_id=None, limit=200):
        sql = '''SELECT r.*, u.username, u.id AS author_id FROM reviews r JOIN users u ON u.id=r.user_id'''
        args = []
        if movie_id is not None:
            sql += ' WHERE r.movie_id=?'
            args.append(movie_id)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql + ' ORDER BY r.updated_at DESC LIMIT ?', (*args, limit))]

    def user_reviews(self, user_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM reviews WHERE user_id=? ORDER BY updated_at DESC', (user_id,))]

    def save_review(self, user_id, movie_id, score, text):
        now = time.time()
        with self.connect() as db:
            db.execute('''INSERT INTO reviews(user_id,movie_id,score,text,created_at,updated_at) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(user_id,movie_id) DO UPDATE SET score=excluded.score,text=excluded.text,updated_at=excluded.updated_at''',
                       (user_id, movie_id, score, text, now, now))

    def delete_review(self, review_id, user_id=None):
        with self.connect() as db:
            if user_id is None:
                db.execute('DELETE FROM reviews WHERE id=?', (review_id,))
            else:
                db.execute('DELETE FROM reviews WHERE id=? AND user_id=?', (review_id, user_id))

    # ------------------------------------------------------------------ progress & views
    def save_progress(self, user_id, movie_id, position, duration, watched_delta=0.0, file_id=None, next_file_id=None):
        """Films: one row per film. Series (``file_id`` given): a row per episode, and the film row
        points at the episode to continue — the next one once this episode is finished.
        Returns True when the film (or the last episode of a series) is completed."""
        position = max(0.0, float(position))
        duration = max(0.0, float(duration))
        completed = int(duration > 0 and position >= duration * 0.93)
        now = time.time()
        with self.connect() as db:
            if file_id is None:
                db.execute('''INSERT INTO progress(user_id,movie_id,position,duration,completed,watched_seconds,updated_at)
                              VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,movie_id) DO UPDATE SET
                              position=excluded.position,duration=MAX(progress.duration,excluded.duration),
                              completed=MAX(progress.completed,excluded.completed),
                              watched_seconds=progress.watched_seconds+excluded.watched_seconds,
                              updated_at=excluded.updated_at,hidden=0''',
                           (user_id, movie_id, position, duration, completed, max(0.0, watched_delta), now))
                return completed
            db.execute('''INSERT INTO episode_progress(user_id,movie_id,file_id,position,duration,completed,updated_at)
                          VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,movie_id,file_id) DO UPDATE SET
                          position=excluded.position,duration=MAX(episode_progress.duration,excluded.duration),
                          completed=MAX(episode_progress.completed,excluded.completed),updated_at=excluded.updated_at''',
                       (user_id, movie_id, file_id, position, duration, completed, now))
            finished = bool(completed and next_file_id is None)
            if completed and next_file_id is not None:      # continue with the next episode
                row = db.execute('SELECT position,duration,completed FROM episode_progress WHERE user_id=? AND movie_id=? AND file_id=?',
                                 (user_id, movie_id, next_file_id)).fetchone()
                fresh = not row or row['completed']
                file_id = next_file_id
                position, duration = (0.0, row['duration'] if row else 0.0) if fresh else (row['position'], row['duration'])
            db.execute('''INSERT INTO progress(user_id,movie_id,position,duration,completed,watched_seconds,updated_at,file_id)
                          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,movie_id) DO UPDATE SET
                          position=excluded.position,duration=excluded.duration,completed=excluded.completed,
                          watched_seconds=progress.watched_seconds+excluded.watched_seconds,
                          updated_at=excluded.updated_at,hidden=0,file_id=excluded.file_id''',
                       (user_id, movie_id, position, duration, int(finished), max(0.0, watched_delta), now, file_id))
        return finished

    def episode_progress(self, user_id, movie_id):
        with self.connect() as db:
            return {r['file_id']: dict(r) for r in db.execute(
                'SELECT * FROM episode_progress WHERE user_id=? AND movie_id=?', (user_id, movie_id))}

    def progress(self, user_id, movie_id=None):
        with self.connect() as db:
            if movie_id is not None:
                row = db.execute('SELECT * FROM progress WHERE user_id=? AND movie_id=?', (user_id, movie_id)).fetchone()
                return dict(row) if row else None
            return [dict(r) for r in db.execute('SELECT * FROM progress WHERE user_id=? ORDER BY updated_at DESC', (user_id,))]

    def hide_progress(self, user_id, movie_id):
        with self.connect() as db:
            db.execute('UPDATE progress SET hidden=1 WHERE user_id=? AND movie_id=?', (user_id, movie_id))

    def add_view(self, user_id, movie_id, room_code=None):
        with self.connect() as db:
            db.execute('INSERT INTO views(user_id,movie_id,room_code,started_at) VALUES(?,?,?,?)',
                       (user_id, movie_id, room_code, time.time()))

    def dashboard(self):
        day = time.time() - 86400
        with self.connect() as db:
            one = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
            return dict(
                users=one('SELECT COUNT(*) FROM users'),
                users_day=one('SELECT COUNT(*) FROM users WHERE last_seen>?', day),
                views=one('SELECT COUNT(*) FROM views'), views_day=one('SELECT COUNT(*) FROM views WHERE started_at>?', day),
                hours=round(one('SELECT COALESCE(SUM(watched_seconds),0) FROM progress') / 3600, 1),
                minutes=int(one('SELECT COALESCE(SUM(watched_seconds),0) FROM progress') // 60),
                reviews=one('SELECT COUNT(*) FROM reviews'),
                rooms=one('SELECT COUNT(*) FROM rooms'), rooms_active=one('SELECT COUNT(*) FROM rooms WHERE ended_at IS NULL'),
                top=[dict(r) for r in db.execute('SELECT movie_id, COUNT(*) AS n FROM views GROUP BY movie_id ORDER BY n DESC LIMIT 5')],
                daily=[dict(r) for r in db.execute(
                    "SELECT date(started_at,'unixepoch','localtime') AS day, COUNT(*) AS n FROM views "
                    "WHERE started_at>? GROUP BY day ORDER BY day", (time.time() - 14 * 86400,))],
            )

    # ------------------------------------------------------------------ rooms
    def create_room(self, code, movie_id, host_id, title, settings, state):
        with self.connect() as db:
            db.execute('INSERT INTO rooms(code,movie_id,host_id,title,settings,state,created_at) VALUES(?,?,?,?,?,?,?)',
                       (code, movie_id, host_id, title, json.dumps(settings), json.dumps(state), time.time()))
            db.execute('INSERT OR IGNORE INTO room_members VALUES(?,?,?,?)', (code, host_id, time.time(), time.time()))

    def room(self, code):
        with self.connect() as db:
            row = db.execute('SELECT r.*, u.username AS host_name FROM rooms r JOIN users u ON u.id=r.host_id WHERE code=?', (code,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['settings'] = json.loads(result['settings'])
        result['state'] = json.loads(result['state'])
        return result

    def save_room(self, code, **fields):
        allowed = {'settings', 'state', 'movie_id', 'ended_at', 'host_id', 'title'}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(key)
            sets.append(f'{key}=?')
            args.append(json.dumps(value) if key in ('settings', 'state') else value)
        with self.connect() as db:
            db.execute(f'UPDATE rooms SET {",".join(sets)} WHERE code=?', (*args, code))

    def join_room(self, code, user_id):
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO room_members VALUES(?,?,?,?) ON CONFLICT(code,user_id) DO UPDATE SET last_seen=excluded.last_seen',
                       (code, user_id, now, now))

    def room_members(self, code):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT m.*, u.username FROM room_members m JOIN users u ON u.id=m.user_id WHERE code=? ORDER BY joined_at', (code,))]

    def is_member(self, code, user_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM room_members WHERE code=? AND user_id=?', (code, user_id)).fetchone() is not None

    def user_rooms(self, user_id, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                '''SELECT r.code,r.movie_id,r.title,r.host_id,r.created_at,r.ended_at,u.username AS host_name,
                   (SELECT COUNT(*) FROM room_members m2 WHERE m2.code=r.code) AS members,
                   (SELECT COUNT(*) FROM room_messages x WHERE x.code=r.code AND x.kind='chat') AS messages
                   FROM rooms r JOIN room_members m ON m.code=r.code JOIN users u ON u.id=r.host_id
                   WHERE m.user_id=? ORDER BY COALESCE(r.ended_at, 9e18) DESC, r.created_at DESC LIMIT ?''', (user_id, limit))]

    def active_rooms(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                '''SELECT r.code,r.movie_id,r.title,r.created_at,u.username AS host_name,
                   (SELECT COUNT(*) FROM room_members m WHERE m.code=r.code) AS members
                   FROM rooms r JOIN users u ON u.id=r.host_id WHERE r.ended_at IS NULL ORDER BY r.created_at DESC''')]

    def add_message(self, code, user_id, name, kind, text):
        with self.connect() as db:
            cursor = db.execute('INSERT INTO room_messages(code,user_id,name,kind,text,created_at) VALUES(?,?,?,?,?,?)',
                                (code, user_id, name, kind, text, time.time()))
            return cursor.lastrowid

    def messages(self, code, limit=200, after_id=0):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM room_messages WHERE code=? AND id>? ORDER BY id DESC LIMIT ?', (code, after_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def co_viewers(self, user_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                '''SELECT u.id,u.username,COUNT(DISTINCT m2.code) AS rooms, MAX(r.created_at) AS last_room
                   FROM room_members m1 JOIN room_members m2 ON m1.code=m2.code AND m2.user_id<>m1.user_id
                   JOIN users u ON u.id=m2.user_id JOIN rooms r ON r.code=m1.code
                   WHERE m1.user_id=? GROUP BY u.id ORDER BY rooms DESC, last_room DESC LIMIT 30''', (user_id,))]

    def ratings_of(self, user_ids):
        if not user_ids:
            return {}
        marks = ','.join('?' * len(user_ids))
        result = {}
        with self.connect() as db:
            for row in db.execute(f'SELECT user_id,movie_id,score FROM ratings WHERE user_id IN ({marks})', tuple(user_ids)):
                result.setdefault(row['user_id'], {})[row['movie_id']] = row['score']
        return result
