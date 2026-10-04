from flask import Flask, render_template, request, redirect, url_for, jsonify
import time
import storage
from models import Review, PaymentRecord
from services import RoomService
from ui_data import ART, EXTRA_FILMS, RAILS, PEOPLE, ARCHIVE

app = Flask(__name__)
storage.CATALOG.extend(m for m in EXTRA_FILMS if not any(x.id == m.id for x in storage.CATALOG))


@app.context_processor
def interface_context():
    return dict(art=ART, catalog=[m for m in storage.CATALOG if not m.is_hidden], people=PEOPLE, archive=ARCHIVE,
                demo_ratings=storage.USER_RATINGS.get(storage.CURRENT_USER.id, {}))

# ----------------- СТРАНИЦЫ ИНТЕРФЕЙСА -----------------

@app.route('/')
def index():
    """Главная страница: каталог фильмов, поиск, фильтры и персональные рекомендации."""
    genre_filters = request.args.getlist('genre')
    query = request.args.get('q', '').lower().strip()

    films = [m for m in storage.CATALOG if not m.is_hidden]

    if genre_filters:
        films = [m for m in films if all(g in m.genres for g in genre_filters)]
    if query:
        films = [m for m in films if query in m.title.lower() or query in m.description.lower()]

    sort = request.args.get('sort', 'popular')
    if sort == 'rating':
        films.sort(key=lambda m: m.rating, reverse=True)
    elif sort == 'year':
        films.sort(key=lambda m: m.year, reverse=True)
    rails = [dict(section, films=[m for mid in section['ids'] for m in storage.CATALOG if m.id == mid and not m.is_hidden]) for section in RAILS]
    all_genres = sorted({g for m in storage.CATALOG if not m.is_hidden for g in m.genres})

    return render_template(
        'index.html',
        films=films,
        rails=rails,
        genres=all_genres,
        active_genres=genre_filters,
        sort=sort,
        filtered=bool(genre_filters or query or request.args.get('view') == 'catalog' or sort != 'popular'),
        featured=[storage.CATALOG[0], next(m for m in storage.CATALOG if m.id == 9)],
        query=query,
        user=storage.CURRENT_USER
    )

@app.route('/movie/<int:movie_id>')
def movie_detail(movie_id: int):
    """Страница отдельного фильма с плеером, описанием и отзывами."""
    film = next((m for m in storage.CATALOG if m.id == movie_id and not m.is_hidden), None)
    if not film:
        return "Фильм не найден", 404

    film_reviews = [r for r in storage.REVIEWS if r.media_id == film.id]
    user_score = storage.USER_RATINGS.get(storage.CURRENT_USER.id, {}).get(film.id, None)

    return render_template(
        'movie.html',
        film=film,
        reviews=film_reviews,
        user_score=user_score,
        user=storage.CURRENT_USER
    )


@app.route('/watch/<int:movie_id>')
def player(movie_id):
    film = next((m for m in storage.CATALOG if m.id == movie_id and not m.is_hidden), None)
    if not film:
        return 'Фильм не найден', 404
    return render_template('player.html', film=film, user=storage.CURRENT_USER)


@app.route('/rooms')
def rooms():
    return render_template('rooms.html', user=storage.CURRENT_USER)


@app.route('/rooms/archive/<code>')
def archived_room(code):
    session = next((r for r in ARCHIVE if r['code'] == code), None)
    if code == 'local':
        session = dict(code='local', title='Сохранённый чат', date='', members='', movie_id=1, messages=[])
    if session is None:
        return 'Сеанс не найден', 404
    return render_template('archive.html', session=session, user=storage.CURRENT_USER)

@app.route('/room/<room_code>')
def watch_room(room_code: str):
    """Страница комнаты совместного просмотра."""
    room = RoomService.get_room(room_code)
    if not room:
        return "Комната не найдена или была закрыта", 404

    is_host = (room.host_id == storage.CURRENT_USER.id)
    movie_id = request.args.get('movie', type=int)
    film = next((m for m in storage.CATALOG if m.id == movie_id and not m.is_hidden), room.media)
    return render_template('room.html', room=room, film=film, user=storage.CURRENT_USER, is_host=is_host)

@app.route('/profile')
def profile():
    """Страница профиля пользователя, тарифов подписки и истории платежей."""
    watched_films = [m for m in storage.CATALOG if m.id in storage.USER_WATCHED.get(storage.CURRENT_USER.id, [])]
    return render_template(
        'profile.html',
        user=storage.CURRENT_USER,
        payments=storage.PAYMENT_HISTORY,
        watched=watched_films,
        section=request.args.get('section', 'overview')
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
