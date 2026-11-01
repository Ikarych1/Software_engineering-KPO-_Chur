-- Выполняйте запросы по одному в SQL-редакторе SQLite.
-- 1. Количества строк основных сущностей.
SELECT
    (SELECT COUNT(*) FROM users) AS users,
    (SELECT COUNT(*) FROM movies) AS movies,
    (SELECT COUNT(*) FROM ratings) AS ratings;

-- 2. Карточки произведений и внешние рейтинги.
SELECT movie_id, title, content_type, year, imdb_rating, tmdb_rating, kinopoisk_rating
FROM movies
ORDER BY imdb_rating DESC, movie_id
LIMIT 20;

-- 3. Произведения с наибольшим числом пользовательских оценок.
SELECT m.movie_id, m.title, s.rating_count, s.rating_mean
FROM movie_stats AS s
JOIN movies AS m ON m.movie_id = s.movie_id
ORDER BY s.rating_count DESC, m.movie_id
LIMIT 20;

-- 4. История оценок одного пользователя. При желании замените подзапрос на 'ml32m_...'.
SELECT r.user_id, m.title, r.score,
       datetime(r.rated_at, 'unixepoch') AS rated_at_utc
FROM ratings AS r
JOIN movies AS m ON m.movie_id = r.movie_id
WHERE r.user_id = (SELECT user_id FROM users ORDER BY user_id LIMIT 1)
ORDER BY r.rated_at DESC
LIMIT 50;

-- 5. Последние действия этого пользователя. Поля *_ms хранят миллисекунды.
SELECT e.event_type, m.title,
       datetime(e.event_at_ms / 1000, 'unixepoch') AS event_at_utc,
       e.payload
FROM events AS e
LEFT JOIN movies AS m ON m.movie_id = e.movie_id
WHERE e.user_id = (SELECT user_id FROM users ORDER BY user_id LIMIT 1)
ORDER BY e.event_at_ms DESC
LIMIT 50;

-- 6. Активные закладки этого пользователя.
SELECT w.user_id, m.title, datetime(w.added_at_ms / 1000, 'unixepoch') AS added_at_utc
FROM watchlist AS w
JOIN movies AS m ON m.movie_id = w.movie_id
WHERE w.user_id = (SELECT user_id FROM users ORDER BY user_id LIMIT 1)
ORDER BY w.added_at_ms DESC;

-- 7. Чтение JSON-массива жанров.
SELECT m.movie_id, m.title, g.value AS genre
FROM movies AS m, json_each(m.genres) AS g
WHERE g.value = 'фантастика'
ORDER BY m.imdb_rating DESC
LIMIT 30;

-- 8. Число начатых и завершённых сеансов по типу контента.
SELECT m.content_type, COUNT(*) AS sessions,
       SUM(w.completed) AS completed_sessions,
       ROUND(100.0 * SUM(w.completed) / COUNT(*), 2) AS completion_percent
FROM watch_sessions AS w
JOIN movies AS m ON m.movie_id = w.movie_id
GROUP BY m.content_type;

-- 9. Количества участников комнат.
SELECT r.room_id, r.title, m.title AS movie_title, COUNT(*) AS members
FROM rooms AS r
JOIN movies AS m ON m.movie_id = r.movie_id
JOIN room_members AS rm ON rm.room_id = r.room_id
GROUP BY r.room_id, r.title, m.title
ORDER BY members DESC, r.room_id
LIMIT 20;

-- 10. Физическая структура таблиц.
SELECT name, sql
FROM sqlite_schema
WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
ORDER BY name;
