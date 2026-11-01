"""Required first-visit preferences, also used as cold-start recommendation signals."""
import json
import time

from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

from accounts import login_required, safe_next, store

blueprint = Blueprint('onboarding', __name__)
CHOICES = 3


def preferences_for(user_id):
    with store().connect() as db:
        row = db.execute('SELECT * FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        return dict(genres=[], liked_movie_ids=[], onboarding_completed_at=None)
    return dict(genres=json.loads(row['genres']), liked_movie_ids=json.loads(row['liked_movie_ids']),
                onboarding_completed_at=row['onboarding_completed_at'])


def needs_onboarding(user_id):
    return preferences_for(user_id)['onboarding_completed_at'] is None


def init_onboarding(app):
    app.register_blueprint(blueprint)

    @app.before_request
    def require_preferences():
        if not g.get('user') or request.endpoint is None:
            return
        auth_pages = {'auth.login', 'auth.register', 'auth.logout', 'auth.forgot_password', 'auth.reset_password'}
        if request.endpoint == 'static' or request.endpoint in auth_pages or request.endpoint.startswith('onboarding.') or request.path.startswith('/internal/'):
            return
        if needs_onboarding(g.user.id):
            target = request.full_path.rstrip('?') if request.method in ('GET', 'HEAD') else url_for('index')
            destination = url_for('onboarding.preferences', next=target)
            if request.path.startswith('/api/'):
                return jsonify(error='Сначала выберите ваши предпочтения.', onboarding_required=True,
                               onboarding_url=destination), 428
            return redirect(destination)


def catalog():
    rows = store().films()
    # Mix familiar high-rated titles with every genre, rather than offering only one franchise.
    ranked = sorted(rows, key=lambda r: (-float(r['art'].get('tmdb_popularity') or 0),
                                        -float(r['film'].rating or 0), r['film'].id))
    genres = sorted({genre.strip().casefold(): genre.strip() for row in rows for genre in row['film'].genres if genre.strip()}.values())
    return ranked, genres


def save_preferences(user_id, genres, movie_ids, available_rows, available_genres):
    if len(genres) != CHOICES or len(set(genres)) != CHOICES or not set(genres) <= set(available_genres):
        raise ValueError('Выберите ровно 3 разных жанра из списка.')
    if len(movie_ids) != CHOICES or any(type(i) is not int for i in movie_ids) or len(set(movie_ids)) != CHOICES:
        raise ValueError('Выберите ровно 3 разных фильма или сериала.')
    if not set(movie_ids) <= {row['film'].id for row in available_rows}:
        raise ValueError('Одно из выбранных произведений больше недоступно. Выберите другое.')
    now = time.time()
    with store().connect() as db:
        db.execute('''INSERT INTO user_preferences(user_id,genres,liked_movie_ids,updated_at,onboarding_completed_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                      genres=excluded.genres,liked_movie_ids=excluded.liked_movie_ids,updated_at=excluded.updated_at,
                      onboarding_completed_at=COALESCE(user_preferences.onboarding_completed_at,excluded.onboarding_completed_at)''',
                   (user_id, json.dumps(genres, ensure_ascii=False), json.dumps(movie_ids), now, now))
        import uuid
        db.execute('''INSERT INTO events(id,user_id,event_type,created_at,payload) VALUES(?,?,?,?,?)''',
                   (uuid.uuid4().hex, user_id, 'onboarding_preferences', now,
                    json.dumps(dict(genres=genres, liked_movie_ids=movie_ids), ensure_ascii=False)))


@blueprint.route('/auth/preferences', methods=['GET', 'POST'])
@login_required
def preferences():
    rows, genres = catalog()
    saved = preferences_for(g.user.id)
    selected_genres, selected_movies = saved['genres'], saved['liked_movie_ids']
    error = None
    if request.method == 'POST':
        selected_genres = request.form.getlist('genres')
        try:
            selected_movies = [int(i) for i in request.form.getlist('movie_ids')]
            save_preferences(g.user.id, selected_genres, selected_movies, rows, genres)
        except (ValueError, TypeError) as exc:
            error = str(exc) if isinstance(exc, ValueError) and not str(exc).startswith('invalid literal') else 'Некорректный выбор произведений.'
        else:
            target = safe_next()
            if target.startswith('/auth/') or target.startswith('/api/'):
                target = url_for('index')
            return redirect(target)
    selected = {i for i in selected_movies if type(i) is int}
    # Keep submitted/saved choices visible even if they fall outside the initial selection.
    shown = [r for r in rows if r['film'].id in selected]
    shown += [r for r in rows if r['film'].id not in selected][:max(0, 36-len(shown))]
    return render_template('onboarding.html', genres=genres, choices=shown,
                           selected_genres=selected_genres, selected_movies=selected,
                           next_url=safe_next(), error=error, completed=saved['onboarding_completed_at'] is not None), (422 if error else 200)


@blueprint.get('/auth/preferences/films')
@login_required
def films():
    rows, _ = catalog()
    query = request.args.get('q', '').strip().casefold()[:150]
    genre = request.args.get('genre', '')
    if query:
        rows = [r for r in rows if query in r['film'].title.casefold()
                or query in str(r['art'].get('original_title') or r['art'].get('original', '')).casefold()]
    if genre:
        rows = [r for r in rows if genre in r['film'].genres]
    try:
        page = max(1, min(int(request.args.get('page', 1)), 200))
    except ValueError:
        return jsonify(error='Некорректная страница.'), 400
    start = (page-1)*36
    from app import film_image
    return jsonify(items=[dict(id=r['film'].id, title=r['film'].title, year=r['film'].year,
                               image=film_image(r['film'].id), genres=r['film'].genres)
                          for r in rows[start:start+36]], has_more=len(rows) > start+36)
