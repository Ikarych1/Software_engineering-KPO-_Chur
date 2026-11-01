"""Additive storage for recommendation research; no recommendation algorithm.

The application's accounts, film JSON, ratings and personal collections remain
authoritative. Episode metadata are independent of playable torrent file IDs.
"""

SCHEMA = '''
CREATE INDEX IF NOT EXISTS views_user ON views(user_id,started_at);
CREATE INDEX IF NOT EXISTS ratings_movie_score ON ratings(movie_id,score);
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    genres TEXT NOT NULL DEFAULT '[]', content_types TEXT NOT NULL DEFAULT '[]',
    languages TEXT NOT NULL DEFAULT '[]', duration_min TEXT NOT NULL DEFAULT '[]',
    actor_ids TEXT NOT NULL DEFAULT '[]', director_ids TEXT NOT NULL DEFAULT '[]',
    locale TEXT NOT NULL DEFAULT 'ru-RU', timezone TEXT NOT NULL DEFAULT 'UTC',
    updated_at REAL NOT NULL,
    CHECK(json_valid(genres) AND json_valid(content_types) AND json_valid(languages)
          AND json_valid(duration_min) AND json_valid(actor_ids) AND json_valid(director_ids)));
CREATE TABLE IF NOT EXISTS not_interested (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    reason TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(user_id,movie_id));
CREATE INDEX IF NOT EXISTS not_interested_movie ON not_interested(movie_id);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    season INTEGER NOT NULL, episode INTEGER NOT NULL, title TEXT NOT NULL,
    duration REAL NOT NULL, file_id INTEGER,
    UNIQUE(movie_id,season,episode));
CREATE TABLE IF NOT EXISTS recommendation_requests (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, surface TEXT NOT NULL, policy TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS recommendation_requests_user ON recommendation_requests(user_id,created_at);
CREATE TABLE IF NOT EXISTS impressions (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES recommendation_requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL CHECK(rank>0), shown_at REAL NOT NULL,
    clicked INTEGER NOT NULL DEFAULT 0 CHECK(clicked IN (0,1)),
    UNIQUE(request_id,rank), UNIQUE(request_id,movie_id));
CREATE INDEX IF NOT EXISTS impressions_user ON impressions(user_id,shown_at);
CREATE INDEX IF NOT EXISTS impressions_movie ON impressions(movie_id);
CREATE TABLE IF NOT EXISTS clicks (
    id TEXT PRIMARY KEY, impression_id TEXT NOT NULL UNIQUE REFERENCES impressions(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL REFERENCES recommendation_requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE, clicked_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS clicks_request ON clicks(request_id);
CREATE INDEX IF NOT EXISTS clicks_user ON clicks(user_id,clicked_at);
CREATE INDEX IF NOT EXISTS clicks_movie ON clicks(movie_id);
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query TEXT NOT NULL, query_type TEXT NOT NULL, searched_at REAL NOT NULL,
    selected_movie_id INTEGER REFERENCES films(id) ON DELETE SET NULL);
CREATE INDEX IF NOT EXISTS searches_user ON searches(user_id,searched_at);
CREATE INDEX IF NOT EXISTS searches_movie ON searches(selected_movie_id);
CREATE TABLE IF NOT EXISTS watch_sessions (
    id TEXT PRIMARY KEY, view_id INTEGER NOT NULL UNIQUE REFERENCES views(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
    room_code TEXT REFERENCES rooms(code) ON DELETE SET NULL,
    request_id TEXT REFERENCES recommendation_requests(id) ON DELETE SET NULL,
    started_at REAL NOT NULL, ended_at REAL NOT NULL, duration REAL NOT NULL,
    start_position REAL NOT NULL, end_position REAL NOT NULL, watched_seconds REAL NOT NULL,
    paused_seconds REAL NOT NULL, buffered_seconds REAL NOT NULL,
    completed INTEGER NOT NULL CHECK(completed IN (0,1)),
    start_kind TEXT NOT NULL, view_number INTEGER NOT NULL, stop_reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS watch_sessions_user ON watch_sessions(user_id,movie_id,started_at);
CREATE INDEX IF NOT EXISTS watch_sessions_movie ON watch_sessions(movie_id);
CREATE INDEX IF NOT EXISTS watch_sessions_episode ON watch_sessions(episode_id) WHERE episode_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS watch_sessions_room ON watch_sessions(room_code) WHERE room_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS watch_sessions_request ON watch_sessions(request_id) WHERE request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS bookmark_events (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK(action IN ('add','remove')),
    created_at REAL NOT NULL, collection TEXT NOT NULL DEFAULT 'watchlist');
CREATE INDEX IF NOT EXISTS bookmark_events_user ON bookmark_events(user_id,movie_id,created_at);
CREATE INDEX IF NOT EXISTS bookmark_events_movie ON bookmark_events(movie_id);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    tag TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tags_user ON tags(user_id);
CREATE INDEX IF NOT EXISTS tags_movie ON tags(movie_id);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER REFERENCES films(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, created_at REAL NOT NULL,
    session_id TEXT REFERENCES watch_sessions(id) ON DELETE SET NULL,
    request_id TEXT REFERENCES recommendation_requests(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)));
CREATE INDEX IF NOT EXISTS events_user ON events(user_id,created_at);
CREATE INDEX IF NOT EXISTS events_movie ON events(movie_id,created_at);
CREATE INDEX IF NOT EXISTS events_type ON events(event_type,created_at);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_request ON events(request_id) WHERE request_id IS NOT NULL;
CREATE VIEW IF NOT EXISTS episode_activity AS
SELECT user_id,movie_id,episode_id,file_id,position,duration,watched_seconds,completed,updated_at
FROM (
    SELECT s.user_id,s.movie_id,s.episode_id,e.file_id,
           s.end_position AS position,s.duration,
           SUM(s.watched_seconds) OVER (PARTITION BY s.user_id,s.movie_id,s.episode_id) AS watched_seconds,
           MAX(s.completed) OVER (PARTITION BY s.user_id,s.movie_id,s.episode_id) AS completed,
           s.ended_at AS updated_at,
           ROW_NUMBER() OVER (PARTITION BY s.user_id,s.movie_id,s.episode_id ORDER BY s.ended_at DESC,s.id DESC) AS latest
    FROM watch_sessions s JOIN episodes e ON e.id=s.episode_id
) WHERE latest=1;
'''
