-- Текущая общая схема проекта и совместимой тестовой базы.
SELECT (SELECT COUNT(*) FROM users) AS users,
       (SELECT COUNT(*) FROM films) AS films,
       (SELECT COUNT(*) FROM ratings) AS ratings;

-- Карточки хранятся в штатных JSON-полях films.data и films.art.
SELECT id, json_extract(data,'$.title') AS title,
       json_extract(data,'$.year') AS year,
       json_extract(art,'$.imdb') AS imdb,
       json_extract(art,'$.tmdb_rating') AS tmdb
FROM films ORDER BY CAST(json_extract(art,'$.imdb') AS REAL) DESC LIMIT 20;

-- Оценки одного существующего пользователя: user_id INTEGER, дата в секундах.
SELECT u.id, u.username, json_extract(f.data,'$.title') AS title,
       r.score, datetime(r.updated_at,'unixepoch') AS rated_at_utc
FROM ratings r JOIN users u ON u.id=r.user_id JOIN films f ON f.id=r.movie_id
WHERE r.user_id=(SELECT MIN(id) FROM users)
ORDER BY r.updated_at DESC LIMIT 30;

-- Штатный список «буду смотреть» из user_state, без отдельной таблицы watchlist.
SELECT s.user_id, json_extract(f.data,'$.title') AS title
FROM user_state s, json_each(s.data,'$.watchlist') w
JOIN films f ON f.id=w.value
WHERE s.user_id=(SELECT MIN(id) FROM users);

-- Клипы из штатного JSON, с ключами интерфейса.
SELECT s.user_id, json_extract(c.value,'$.movieTitle') AS movie,
       json_extract(c.value,'$.title') AS title,
       json_extract(c.value,'$.start') AS start_seconds,
       json_extract(c.value,'$.end') AS end_seconds,
       json_extract(c.value,'$.pinned') AS pinned
FROM user_state s, json_each(s.data,'$.clips') c LIMIT 20;

-- Предпочтения сохраняются независимо от обычных настроек интерфейса.
SELECT u.id,u.username,p.genres,p.content_types,p.languages,p.duration_min
FROM users u JOIN user_preferences p ON p.user_id=u.id LIMIT 20;

-- Явные отрицательные предпочтения.
SELECT u.username,json_extract(f.data,'$.title') AS title,n.reason
FROM not_interested n JOIN users u ON u.id=n.user_id JOIN films f ON f.id=n.movie_id
WHERE n.is_active=1 LIMIT 20;

-- История сеансов и их штатные записи views обозначают одни просмотры.
SELECT s.user_id,json_extract(f.data,'$.title') AS title,s.start_kind,s.view_number,
       s.watched_seconds,s.completed,s.stop_reason,datetime(s.started_at,'unixepoch') AS started_at
FROM watch_sessions s JOIN films f ON f.id=s.movie_id
WHERE s.user_id=(SELECT MIN(id) FROM users)
ORDER BY s.started_at DESC LIMIT 30;

-- Весь прогресс серий, включая серии без видеофайла.
SELECT a.user_id,json_extract(f.data,'$.title') AS title,e.season,e.episode,
       a.position,a.watched_seconds,a.completed,a.file_id
FROM episode_activity a JOIN episodes e ON e.id=a.episode_id JOIN films f ON f.id=a.movie_id
LIMIT 30;

-- Совпадение пользователю/произведению/позиции между показом и кликом.
SELECT i.user_id,json_extract(f.data,'$.title') AS title,i.rank,i.clicked,
       datetime(i.shown_at,'unixepoch') AS shown_at,datetime(c.clicked_at,'unixepoch') AS clicked_at
FROM impressions i JOIN films f ON f.id=i.movie_id LEFT JOIN clicks c ON c.impression_id=i.id
WHERE i.user_id=(SELECT MIN(id) FROM users)
ORDER BY i.shown_at,i.rank;

-- Архив комнат в действующем формате code + JSON state/settings.
SELECT r.code,r.title,u.username AS host,COUNT(m.user_id) AS members,
       datetime(r.created_at,'unixepoch') AS created_at,datetime(r.ended_at,'unixepoch') AS ended_at
FROM rooms r JOIN users u ON u.id=r.host_id JOIN room_members m ON m.code=r.code
GROUP BY r.code ORDER BY r.created_at DESC LIMIT 20;

-- Общий журнал: даты теперь seconds REAL, ключ пользователя целочисленный.
SELECT e.user_id,e.event_type,datetime(e.created_at,'unixepoch') AS event_at,e.payload
FROM events e WHERE e.user_id=(SELECT MIN(id) FROM users)
ORDER BY e.created_at DESC LIMIT 30;
