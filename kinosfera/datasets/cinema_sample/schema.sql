CREATE TABLE episode_progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
    position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, movie_id, file_id));

CREATE TABLE film_sources (
    movie_id INTEGER NOT NULL, source INTEGER NOT NULL, torrent BLOB NOT NULL, name TEXT NOT NULL,
    files TEXT NOT NULL, info_hash TEXT NOT NULL, magnet TEXT, added_at REAL NOT NULL,
    PRIMARY KEY(movie_id, source));

CREATE TABLE films (
                    id INTEGER PRIMARY KEY, data TEXT NOT NULL, art TEXT NOT NULL,
                    torrent BLOB, torrent_name TEXT, file_id INTEGER,
                    files TEXT NOT NULL DEFAULT '[]', revision TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, magnet TEXT, info_hash TEXT, episodes TEXT NOT NULL DEFAULT '[]', imdb_id TEXT, tmdb_id INTEGER, tmdb_kind TEXT);

CREATE TABLE login_attempts (
                    key TEXT PRIMARY KEY, failures INTEGER NOT NULL, last_at REAL NOT NULL);

CREATE TABLE media_info (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE password_resets (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, expires_at REAL NOT NULL);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tariff TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL,
    position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
    watched_seconds REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, file_id INTEGER,
    PRIMARY KEY(user_id, movie_id));

CREATE TABLE ratings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, movie_id INTEGER NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 10), updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, movie_id));

CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 10),
    text TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(user_id, movie_id));

CREATE TABLE room_members (
    code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id),
    joined_at REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(code, user_id));

CREATE TABLE room_messages (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    user_id INTEGER, name TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE rooms (
    code TEXT PRIMARY KEY, movie_id INTEGER NOT NULL, host_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL, settings TEXT NOT NULL, state TEXT NOT NULL,
    created_at REAL NOT NULL, ended_at REAL);

CREATE TABLE tmdb_cache (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);

CREATE TABLE tmdb_jobs (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, total INTEGER NOT NULL,
    options TEXT NOT NULL, message TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL);

CREATE TABLE tmdb_rows (
    job_id TEXT NOT NULL REFERENCES tmdb_jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, entry TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', result TEXT,
    PRIMARY KEY(job_id, position));

CREATE TABLE user_state (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id), data TEXT NOT NULL);

CREATE TABLE users (
                    id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('ADMIN','USER')),
                    active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen REAL, plan TEXT NOT NULL DEFAULT 'free', plan_until REAL);

CREATE TABLE views (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, movie_id INTEGER NOT NULL,
    room_code TEXT, started_at REAL NOT NULL);

CREATE TABLE user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    genres TEXT NOT NULL DEFAULT '[]', content_types TEXT NOT NULL DEFAULT '[]',
    languages TEXT NOT NULL DEFAULT '[]', duration_min TEXT NOT NULL DEFAULT '[]',
    actor_ids TEXT NOT NULL DEFAULT '[]', director_ids TEXT NOT NULL DEFAULT '[]',
    locale TEXT NOT NULL DEFAULT 'ru-RU', timezone TEXT NOT NULL DEFAULT 'UTC',
    updated_at REAL NOT NULL,
    CHECK(json_valid(genres) AND json_valid(content_types) AND json_valid(languages)
          AND json_valid(duration_min) AND json_valid(actor_ids) AND json_valid(director_ids)));

CREATE TABLE not_interested (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    reason TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(user_id,movie_id));

CREATE TABLE episodes (
    id TEXT PRIMARY KEY, movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    season INTEGER NOT NULL, episode INTEGER NOT NULL, title TEXT NOT NULL,
    duration REAL NOT NULL, file_id INTEGER,
    UNIQUE(movie_id,season,episode));

CREATE TABLE recommendation_requests (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, surface TEXT NOT NULL, policy TEXT NOT NULL);

CREATE TABLE impressions (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES recommendation_requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL CHECK(rank>0), shown_at REAL NOT NULL,
    clicked INTEGER NOT NULL DEFAULT 0 CHECK(clicked IN (0,1)),
    UNIQUE(request_id,rank), UNIQUE(request_id,movie_id));

CREATE TABLE clicks (
    id TEXT PRIMARY KEY, impression_id TEXT NOT NULL UNIQUE REFERENCES impressions(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL REFERENCES recommendation_requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE, clicked_at REAL NOT NULL);

CREATE TABLE searches (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query TEXT NOT NULL, query_type TEXT NOT NULL, searched_at REAL NOT NULL,
    selected_movie_id INTEGER REFERENCES films(id) ON DELETE SET NULL);

CREATE TABLE watch_sessions (
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

CREATE TABLE bookmark_events (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK(action IN ('add','remove')),
    created_at REAL NOT NULL, collection TEXT NOT NULL DEFAULT 'watchlist');

CREATE TABLE tags (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    tag TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE events (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER REFERENCES films(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, created_at REAL NOT NULL,
    session_id TEXT REFERENCES watch_sessions(id) ON DELETE SET NULL,
    request_id TEXT REFERENCES recommendation_requests(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)));

CREATE VIEW episode_activity AS
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

CREATE UNIQUE INDEX films_imdb_id ON films(imdb_id) WHERE imdb_id IS NOT NULL;

CREATE UNIQUE INDEX films_tmdb_id ON films(tmdb_kind,tmdb_id) WHERE tmdb_id IS NOT NULL;

CREATE INDEX password_resets_user ON password_resets(user_id, created_at);

CREATE INDEX progress_recent ON progress(user_id, updated_at);

CREATE INDEX ratings_movie ON ratings(movie_id);

CREATE INDEX reviews_movie ON reviews(movie_id, updated_at);

CREATE INDEX room_members_user ON room_members(user_id);

CREATE INDEX room_messages_code ON room_messages(code, id);

CREATE INDEX rooms_active ON rooms(ended_at);

CREATE INDEX tmdb_rows_status ON tmdb_rows(job_id, status, position);

CREATE INDEX views_movie ON views(movie_id);

CREATE INDEX not_interested_movie ON not_interested(movie_id);

CREATE INDEX recommendation_requests_user ON recommendation_requests(user_id,created_at);

CREATE INDEX impressions_user ON impressions(user_id,shown_at);

CREATE INDEX impressions_movie ON impressions(movie_id);

CREATE INDEX clicks_request ON clicks(request_id);

CREATE INDEX clicks_user ON clicks(user_id,clicked_at);

CREATE INDEX clicks_movie ON clicks(movie_id);

CREATE INDEX searches_user ON searches(user_id,searched_at);

CREATE INDEX searches_movie ON searches(selected_movie_id);

CREATE INDEX watch_sessions_user ON watch_sessions(user_id,movie_id,started_at);

CREATE INDEX watch_sessions_movie ON watch_sessions(movie_id);

CREATE INDEX watch_sessions_episode ON watch_sessions(episode_id) WHERE episode_id IS NOT NULL;

CREATE INDEX watch_sessions_room ON watch_sessions(room_code) WHERE room_code IS NOT NULL;

CREATE INDEX watch_sessions_request ON watch_sessions(request_id) WHERE request_id IS NOT NULL;

CREATE INDEX bookmark_events_user ON bookmark_events(user_id,movie_id,created_at);

CREATE INDEX bookmark_events_movie ON bookmark_events(movie_id);

CREATE INDEX tags_user ON tags(user_id);

CREATE INDEX tags_movie ON tags(movie_id);

CREATE INDEX events_user ON events(user_id,created_at);

CREATE INDEX events_movie ON events(movie_id,created_at);

CREATE INDEX events_type ON events(event_type,created_at);

CREATE INDEX events_session ON events(session_id) WHERE session_id IS NOT NULL;

CREATE INDEX events_request ON events(request_id) WHERE request_id IS NOT NULL;

CREATE INDEX views_user ON views(user_id,started_at);

CREATE INDEX ratings_movie_score ON ratings(movie_id,score);
