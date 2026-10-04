from flask import Flask, render_template, request, redirect, url_for, jsonify
import time
import storage
from models import Review, PaymentRecord
from services import RecommendationService, RoomService

app = Flask(__name__)

# ----------------- СТРАНИЦЫ ИНТЕРФЕЙСА -----------------

@app.route('/')
def index():
    """Главная страница: каталог фильмов, поиск, фильтры и персональные рекомендации."""
    genre_filter = request.args.get('genre', '')
    query = request.args.get('q', '').lower().strip()

    films = [m for m in storage.CATALOG if not m.is_hidden]

    if genre_filter:
        films = [m for m in films if genre_filter in m.genres]
    if query:
        films = [m for m in films if query in m.title.lower() or query in m.description.lower()]

    recommendations = RecommendationService.get_recommendations(storage.CURRENT_USER.id)
    all_genres = sorted(list({g for m in storage.CATALOG for g in m.genres}))

    return render_template(
        'index.html',
        films=films,
        recommendations=recommendations,
        genres=all_genres,
        active_genre=genre_filter,
        query=query,
        user=storage.CURRENT_USER
    )

@app.route('/movie/<int:movie_id>')
def movie_detail(movie_id: int):
    """Страница отдельного фильма с плеером, описанием и отзывами."""
    film = next((m for m in storage.CATALOG if m.id == movie_id), None)
    if not film:
        return "Фильм не найден", 404

    # Фиксируем просмотр в истории
    if film.id not in storage.USER_WATCHED.setdefault(storage.CURRENT_USER.id, []):
        storage.USER_WATCHED[storage.CURRENT_USER.id].append(film.id)

    film_reviews = [r for r in storage.REVIEWS if r.media_id == film.id]
    user_score = storage.USER_RATINGS.get(storage.CURRENT_USER.id, {}).get(film.id, None)

    return render_template(
        'movie.html',
        film=film,
        reviews=film_reviews,
        user_score=user_score,
        user=storage.CURRENT_USER
    )

@app.route('/room/<room_code>')
def watch_room(room_code: str):
    """Страница комнаты совместного просмотра."""
    room = RoomService.get_room(room_code)
    if not room:
        return "Комната не найдена или была закрыта", 404

    if storage.CURRENT_USER.username not in room.members:
        room.members.append(storage.CURRENT_USER.username)
        room.add_message("Система", f"Пользователь {storage.CURRENT_USER.username} вошел в комнату.")

    is_host = (room.host_id == storage.CURRENT_USER.id)
    return render_template('room.html', room=room, user=storage.CURRENT_USER, is_host=is_host)

@app.route('/profile')
def profile():
    """Страница профиля пользователя, тарифов подписки и истории платежей."""
    watched_films = [m for m in storage.CATALOG if m.id in storage.USER_WATCHED.get(storage.CURRENT_USER.id, [])]
    return render_template(
        'profile.html',
        user=storage.CURRENT_USER,
        payments=storage.PAYMENT_HISTORY,
        watched=watched_films
    )

# ----------------- ДЕЙСТВИЯ И ДЕМО-ОПЕРАЦИИ -----------------

@app.route('/movie/<int:movie_id>/rate', methods=['POST'])
def add_review(movie_id: int):
    """Добавление оценки и отзыва."""
    score = int(request.form.get('score', 10))
    comment = request.form.get('comment', '').strip()

    storage.USER_RATINGS.setdefault(storage.CURRENT_USER.id, {})[movie_id] = score
    if movie_id not in storage.USER_WATCHED.setdefault(storage.CURRENT_USER.id, []):
        storage.USER_WATCHED[storage.CURRENT_USER.id].append(movie_id)

    # Удаляем предыдущий отзыв этого пользователя, если был (п. 4.1.1 ТЗ)
    storage.REVIEWS = [r for r in storage.REVIEWS if not (r.media_id == movie_id and r.user_name == storage.CURRENT_USER.username)]

    if comment:
        ts = time.strftime("%Y-%m-%d %H:%M")
        new_rev = Review(len(storage.REVIEWS) + 1, storage.CURRENT_USER.username, movie_id, score, comment, ts)
        storage.REVIEWS.insert(0, new_rev)

    return redirect(url_for('movie_detail', movie_id=movie_id))

@app.route('/room/create/<int:movie_id>', methods=['POST'])
def create_room_action(movie_id: int):
    """Создание новой комнаты и переход в нее."""
    room = RoomService.create_room(storage.CURRENT_USER, movie_id)
    return redirect(url_for('watch_room', room_code=room.room_code))

@app.route('/subscription/pay', methods=['POST'])
def mock_payment():
    """Демо-оплата учебного тарифа без реальных списаний (п. 4.1.1 ТЗ)."""
    tariff = request.form.get('tariff', 'Премиум Плюс')
    price = 299.0 if tariff == 'Премиум Плюс' else 0.0

    storage.CURRENT_USER.is_subscribed = True
    record = PaymentRecord(
        id=len(storage.PAYMENT_HISTORY) + 101,
        tariff_name=tariff,
        amount=price,
        status="Успешно (Имитация)",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S")
    )
    storage.PAYMENT_HISTORY.insert(0, record)
    return redirect(url_for('profile'))

# ----------------- REST API ДЛЯ AJAX-СИНХРОНИЗАЦИИ -----------------

@app.route('/api/room/<room_code>/sync', methods=['GET'])
def api_room_sync(room_code: str):
    """Синхронизация прогресса воспроизведения и чата для участников."""
    room = RoomService.get_room(room_code)
    if not room:
        return jsonify({"error": "not found"}), 404

    # Продвижение таймлайна, если воспроизведение активно
    if room.state.is_playing:
        delta = time.time() - room.state.last_update_epoch
        room.state.current_time_sec += delta
        room.state.last_update_epoch = time.time()

    return jsonify({
        "is_playing": room.state.is_playing,
        "current_time_sec": round(room.state.current_time_sec, 1),
        "members": room.members,
        "chat": [msg.__dict__ for msg in room.chat_history[-15:]]
    })

@app.route('/api/room/<room_code>/playback', methods=['POST'])
def api_room_playback(room_code: str):
    """Управление синхронным плеером (Play / Pause / Seek)."""
    room = RoomService.get_room(room_code)
    if not room:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    room.update_playback(bool(data.get('is_playing')), float(data.get('time_sec', 0.0)))
    return jsonify({"status": "ok"})

@app.route('/api/room/<room_code>/chat', methods=['POST'])
def api_room_chat(room_code: str):
    """Отправка сообщения в комнату."""
    room = RoomService.get_room(room_code)
    if not room:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    room.add_message(storage.CURRENT_USER.username, data.get('text', ''))
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    print(">>> Прототип запущен: http://127.0.0.1:5000/")
    app.run(debug=True, port=5000)