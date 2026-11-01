-- Количество пользователей, произведений и оценок.
SELECT 'users' AS entity, COUNT(*) AS records FROM users
UNION ALL SELECT 'films', COUNT(*) FROM films
UNION ALL SELECT 'ratings', COUNT(*) FROM ratings;

-- Оценки с именем пользователя и названием произведения.
SELECT u.id, u.username, json_extract(f.data,'$.title') AS title,
       r.score, datetime(r.updated_at,'unixepoch') AS rated_at
FROM ratings r JOIN users u ON u.id=r.user_id JOIN films f ON f.id=r.movie_id
ORDER BY r.updated_at DESC LIMIT 30;

-- Количество оценок и средняя оценка внутри выборки.
SELECT f.id, json_extract(f.data,'$.title') AS title,
       COUNT(r.user_id) AS votes, ROUND(AVG(r.score),2) AS average
FROM films f LEFT JOIN ratings r ON r.movie_id=f.id
GROUP BY f.id ORDER BY votes DESC LIMIT 20;

-- Закладки «буду смотреть» в штатном JSON-профиле.
SELECT u.id, u.username, json_extract(f.data,'$.title') AS title
FROM user_state s JOIN users u ON u.id=s.user_id,
     json_each(s.data,'$.watchlist') item
JOIN films f ON f.id=CAST(item.value AS INTEGER)
ORDER BY u.id LIMIT 30;

-- Показы и клики с исходной позицией в выдаче.
SELECT i.user_id,i.movie_id,i.rank,i.shown_at,c.id AS click_id
FROM impressions i LEFT JOIN clicks c ON c.impression_id=i.id
ORDER BY i.shown_at DESC LIMIT 30;

-- Сеансы: доля просмотра, досмотр и причина остановки.
SELECT user_id,movie_id,episode_id,view_number,watched_seconds,
       ROUND(100.0*end_position/NULLIF(duration,0),1) AS position_percent,
       completed,stop_reason
FROM watch_sessions ORDER BY started_at DESC LIMIT 30;

-- Прогресс серий, включая повторные просмотры.
SELECT * FROM episode_activity ORDER BY updated_at DESC LIMIT 20;

-- Комнаты и участники.
SELECT r.code,r.title,u.username AS host,COUNT(m.user_id) AS participants
FROM rooms r JOIN users u ON u.id=r.host_id
LEFT JOIN room_members m ON m.code=r.code
GROUP BY r.code ORDER BY r.code;

-- Типы событий.
SELECT event_type,COUNT(*) AS records FROM events GROUP BY event_type ORDER BY records DESC;

-- Обе проверки должны завершиться без ошибок; foreign_key_check не возвращает строк.
PRAGMA integrity_check;
PRAGMA foreign_key_check;
