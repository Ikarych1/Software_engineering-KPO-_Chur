"""Read current user signals and persist recommendation feedback in existing tables."""
import json
import time
import uuid

from recommendation_service import POLICY, UserContext


class RecommendationStore:
    def __init__(self, store):
        self.store = store

    def context(self, user_id, rows):
        if user_id is None:
            return UserContext()
        by_id = {r['film'].id: r for r in rows}
        with self.store.connect() as db:
            ratings = {r['movie_id']: dict(score=r['score'], updated_at=r['updated_at']) for r in db.execute(
                'SELECT movie_id,score,updated_at FROM ratings WHERE user_id=?', (user_id,))}
            progress = [dict(r) for r in db.execute('SELECT * FROM progress WHERE user_id=?', (user_id,))]
            declined = {r['movie_id'] for r in db.execute(
                'SELECT movie_id FROM not_interested WHERE user_id=? AND is_active=1', (user_id,))}
            prefs = db.execute('SELECT * FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
            preferences = {}
            if prefs:
                for key in ('genres', 'content_types', 'languages', 'actor_ids', 'director_ids', 'liked_movie_ids'):
                    value = json.loads(prefs[key])
                    preferences[key] = value if isinstance(value, list) else []
            state_row = db.execute('SELECT data FROM user_state WHERE user_id=?', (user_id,)).fetchone()
            state = json.loads(state_row['data']) if state_row else {}
            episode_files = {}
            for row in db.execute('SELECT movie_id,file_id FROM episode_progress WHERE user_id=? AND completed=1', (user_id,)):
                episode_files.setdefault(row['movie_id'], set()).add(row['file_id'])
            complete_series = {r['movie_id'] for r in db.execute('''
                SELECT a.movie_id FROM episode_activity a
                WHERE a.user_id=? GROUP BY a.movie_id
                HAVING SUM(CASE WHEN a.completed=1 THEN 1 ELSE 0 END) =
                       (SELECT COUNT(*) FROM episodes e WHERE e.movie_id=a.movie_id)
                AND (SELECT COUNT(*) FROM episodes e WHERE e.movie_id=a.movie_id)>0''', (user_id,))}
        completed = {p['movie_id'] for p in progress if p['completed']
                     and p['movie_id'] in by_id and by_id[p['movie_id']]['film'].kind != 'series'}
        # Rating UI also adds IDs to watched: that flag alone cannot prove series completion.
        completed.update(i for i in state.get('watched', []) if i in by_id and i not in ratings
                         and by_id[i]['film'].kind != 'series')
        for movie_id, row in by_id.items():
            if row['film'].kind != 'series':
                continue
            expected = {e['file_id'] for e in row.get('episodes', []) if e.get('file_id') is not None}
            if movie_id in complete_series or expected and expected <= episode_files.get(movie_id, set()):
                completed.add(movie_id)
        return UserContext(ratings, progress, state.get('watchlist', []), completed, declined, preferences)

    def popularity(self, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as db:
            return {r['movie_id']: r['n'] for r in db.execute('''
                SELECT movie_id,COUNT(DISTINCT user_id) AS n FROM watch_sessions
                WHERE completed=1 AND ended_at>=? AND ended_at<=? GROUP BY movie_id''', (now-14*86400, now))}

    def record(self, user_id, surface, result):
        if user_id is None:
            return
        request_id, now = uuid.uuid4().hex, time.time()
        payload = dict(snapshot_id=result.snapshot_id, personalized=result.personalized, items=result.items)
        with self.store.connect() as db:
            db.execute('INSERT INTO recommendation_requests VALUES(?,?,?,?,?)', (request_id, user_id, now, surface, POLICY))
            db.execute('INSERT INTO events(id,user_id,event_type,created_at,request_id,payload) VALUES(?,?,?,?,?,?)',
                       (uuid.uuid4().hex, user_id, 'recommendation_served', now, request_id,
                        json.dumps(payload, ensure_ascii=False)))
        result.request_id = request_id

    def feedback(self, user_id, request_id, movie_ids, *, clicked=False):
        """Only issued IDs belong to a request; retries reuse the same impression."""
        with self.store.connect() as db:
            request = db.execute('SELECT user_id FROM recommendation_requests WHERE id=?', (request_id,)).fetchone()
            if not request or request['user_id'] != user_id:
                raise LookupError('request not found')
            event = db.execute("SELECT payload FROM events WHERE request_id=? AND event_type='recommendation_served'", (request_id,)).fetchone()
            issued = json.loads(event['payload'])['items'] if event else []
            ranks = {item['id']: rank for rank, item in enumerate(issued, 1)}
            if any(i not in ranks for i in movie_ids):
                raise ValueError('movie was not issued in this request')
            now = time.time()
            for movie_id in movie_ids:
                if not db.execute('SELECT 1 FROM films WHERE id=?', (movie_id,)).fetchone():
                    continue
                impression_id = f'{request_id}:{movie_id}'
                db.execute('''INSERT OR IGNORE INTO impressions(id,request_id,user_id,movie_id,rank,shown_at)
                              VALUES(?,?,?,?,?,?)''', (impression_id, request_id, user_id, movie_id, ranks[movie_id], now))
                if clicked:
                    db.execute('INSERT OR IGNORE INTO clicks VALUES(?,?,?,?,?,?)',
                               (uuid.uuid4().hex, impression_id, request_id, user_id, movie_id, now))
                    db.execute('UPDATE impressions SET clicked=1 WHERE id=?', (impression_id,))

    def decline(self, user_id, movie_id, active):
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM films WHERE id=?', (movie_id,)).fetchone():
                raise LookupError('movie not found')
            db.execute('''INSERT INTO not_interested VALUES(?,?,?,?,?)
                          ON CONFLICT(user_id,movie_id) DO UPDATE SET
                          is_active=excluded.is_active,reason=excluded.reason,updated_at=excluded.updated_at''',
                       (user_id, movie_id, int(active), 'user', time.time()))

