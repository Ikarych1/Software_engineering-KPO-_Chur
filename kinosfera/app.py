"""КиноСфера — локальный онлайн-кинотеатр с потоковым BitTorrent-просмотром и комнатами."""
from pathlib import Path
import os
import threading
import time

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for

from accounts import init_accounts, login_required, store
from onboarding import init_onboarding
from pagination import paginate
from database import Store
from library import init_library
from imdb_admin import init_imdb
from tmdb_admin import init_tmdb
from playback import init_playback
from rooms import init_rooms
import series
import services
import recommendation_web
import storage
from torrent_meta import info_hash
from ui_data import ART, EXTRA_FILMS

app = Flask(__name__)
app.config.setdefault('LIBTORRENT_MODULE', None)
storage.CATALOG.extend(m for m in EXTRA_FILMS if not any(x.id == m.id for x in storage.CATALOG))
app.extensions['store'] = Store(Path(os.environ.get('CINEMA_DATABASE') or Path(app.root_path) / '.local/cinema.sqlite3'))
app.extensions['store'].seed(storage.CATALOG, ART)
init_accounts(app)
init_onboarding(app)
init_library(app)
recommendation_web.init_recommendations(app)
init_imdb(app)
init_tmdb(app)
playback = init_playback(app)
init_rooms(app)


# ----------------------------------------------------------------- per-request data
def catalog_rows(include_hidden=False):
    key = '_rows_all' if include_hidden else '_rows'
    if key not in g:
        setattr(g, key, store().films(include_hidden=include_hidden))
    return getattr(g, key)


def rows_by_id():
    if '_by_id' not in g:
        g._by_id = {row['film'].id: row for row in catalog_rows(include_hidden=True)}
    return g._by_id


def film_stats():
    if '_stats' not in g:
        g._stats = store().film_stats()
    return g._stats


def my_progress():
    if '_progress' not in g:
        g._progress = store().progress(g.user.id) if g.get('user') else []
    return g._progress


def available_catalog():
    return [row['film'] for row in catalog_rows()]


_ASSET_VERSIONS = {}


def asset_url(filename):
    """Static URL with a content version so browsers can cache it for a year."""
    path = Path(app.static_folder) / filename
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    version = _ASSET_VERSIONS.get((filename, mtime))
    if version is None:
        version = _ASSET_VERSIONS[(filename, mtime)] = format(mtime, 'x')
    return url_for('static', filename=filename, v=version)


def film_image(movie_id, backdrop=False):
    row = rows_by_id().get(movie_id)
    info = row['art'] if row else {}
    if backdrop and info.get('backdrop_upload') and static_file_exists(info['backdrop_upload']):
        return url_for('static', filename=info['backdrop_upload'])
    if backdrop and info.get('backdrop') and static_file_exists(f"images/{info['backdrop']}.jpg"):
        return url_for('static', filename=f"images/{info['backdrop']}.jpg")
    if backdrop and info.get('tmdb_backdrop_url'):
        return info['tmdb_backdrop_url']
    if info.get('poster') and static_file_exists(info['poster']):
        return url_for('static', filename=info['poster'])
    slug = info.get('slug', 'placeholder')
    if static_file_exists('images/' + slug + ('.svg' if slug == 'placeholder' else '.jpg')) and slug != 'placeholder':
        return url_for('static', filename='images/' + slug + '.jpg')
    if info.get('tmdb_poster_url'):
        return info['tmdb_poster_url']
    return url_for('static', filename='images/placeholder.svg')


def static_file_exists(filename):
    root = Path(app.static_folder).resolve()
    path = (root / filename).resolve()
    return path.is_relative_to(root) and path.is_file()


def series_info(movie_id):
    """Seasons and episodes of a series card (None for films)."""
    row = rows_by_id().get(movie_id)
    if not row or row['film'].kind != 'series':
        return None
    episodes = row.get('episodes') or []
    by_season = series.seasons(episodes)
    count, seasons_n = len(episodes), len(by_season)
    text = f"{seasons_n} {plural(seasons_n, 'сезон', 'сезона', 'сезонов')} · {count} {plural(count, 'серия', 'серии', 'серий')}"
    return dict(episodes=episodes, by_season=by_season, count=count, seasons=seasons_n, text=text,
                first=episodes[0] if episodes else None)


def film_meta(movie_id):
    """Availability, community rating and personal progress for cards."""
    row = rows_by_id().get(movie_id)
    stats = film_stats().get(movie_id, {})
    progress = next((p for p in my_progress() if p['movie_id'] == movie_id), None)
    fraction, episode = None, None
    if progress and not progress['completed'] and not progress['hidden']:
        if progress.get('file_id') is not None and row and row['film'].kind == 'series':
            episode = series.find(row.get('episodes') or [], progress['file_id'])
            if episode:
                fraction = min(1.0, progress['position'] / progress['duration']) if progress['duration'] else 0.0
        elif progress['duration'] and progress['position'] > 30:
            fraction = min(1.0, progress['position'] / progress['duration'])
    return dict(available=bool(row and row['torrent']), avg=stats.get('avg'), votes=stats.get('votes', 0),
                views=stats.get('views', 0), progress=fraction, episode=episode, resuming=fraction is not None,
                position=progress['position'] if fraction is not None else 0)


def in_progress(item):
    """A film to continue: started (30 s+) or a series with its next episode ready."""
    return not item['completed'] and not item['hidden'] and (item['position'] > 30 or item.get('file_id') is not None)


def fmt_duration(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'


def fmt_size(value):
    value = float(value or 0)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if value < 1024 or unit == 'ТБ':
            return f'{value:.0f} {unit}' if unit in ('Б', 'КБ') else f'{value:.1f} {unit}'
        value /= 1024


def fmt_date(epoch, with_time=True):
    if not epoch:
        return ''
    months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
    t = time.localtime(float(epoch))
    text = f'{t.tm_mday} {months[t.tm_mon - 1]}'
    if t.tm_year != time.localtime().tm_year:
        text += f' {t.tm_year}'
    return text + (f', {t.tm_hour:02d}:{t.tm_min:02d}' if with_time else '')


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


app.jinja_env.globals.update(film_image=film_image, film_meta=film_meta, asset_url=asset_url, plural=plural,
                             static_file_exists=static_file_exists,
                             series_info=series_info, episode_label=series.label)
app.jinja_env.filters.update(duration=fmt_duration, filesize=fmt_size, date=fmt_date)


@app.context_processor
def interface_context():
    rows = catalog_rows()
    state = store().state(g.user.id) if g.get('user') else {}
    return dict(art={row['film'].id: row['art'] for row in rows_by_id().values()},
                catalog=[row['film'] for row in rows],
                demo_ratings={int(key): value for key, value in state.get('ratings', {}).items()},
                user=g.get('user'))


def media_summary(row):
    if not row or not row.get('torrent'):
        return None
    torrent = row['torrent'] if isinstance(row['torrent'], bytes) else None
    digest = row.get('info_hash') or (info_hash(torrent) if torrent else None)
    if not digest:
        return None
    return store().media_info(f"{digest}:{row['file_id']}")


# ----------------------------------------------------------------- pages
@app.route('/')
def index():
    genre_filters = request.args.getlist('genre')
    query = request.args.get('q', '').lower().strip()[:100]
    kind = request.args.get('kind') if request.args.get('kind') in ('movie', 'series') else None
    films = available_catalog()
    if kind:
        films = [m for m in films if m.kind == kind]
    if genre_filters:
        films = [m for m in films if all(gen in m.genres for gen in genre_filters)]
    if query:
        films = [m for m in films if query in m.title.lower() or query in m.description.lower()
                 or query in rows_by_id()[m.id]['art'].get('original', '').lower()
                 or any(query in a.lower() for a in rows_by_id()[m.id]['art'].get('actors', []))
                 or query in rows_by_id()[m.id]['art'].get('director', '').lower()]
    sort = request.args.get('sort', 'popular')
    stats = film_stats()
    if sort == 'rating':
        films.sort(key=lambda m: stats.get(m.id, {}).get('avg') or m.rating, reverse=True)
    elif sort == 'year':
        films.sort(key=lambda m: m.year, reverse=True)
    elif sort == 'available':
        films.sort(key=lambda m: (not rows_by_id()[m.id]['torrent'], -m.rating))
    else:
        films.sort(key=lambda m: (-(stats.get(m.id, {}).get('views', 0)), -m.rating))
    filtered = bool(genre_filters or query or kind or request.args.get('view') == 'catalog' or sort != 'popular')
    rows = catalog_rows()
    rails = []
    state = store().state(g.user.id) if g.user else {}
    if g.user:
        progress = [p for p in my_progress() if in_progress(p)]
        cont = [rows_by_id()[p['movie_id']]['film'] for p in progress if p['movie_id'] in rows_by_id()
                and not rows_by_id()[p['movie_id']]['film'].is_hidden][:12]
        if cont:
            rails.append(dict(id='continue', title='Продолжить просмотр', subtitle='С того места, где вы остановились', films=cont, label='Ваш вечер'))
    available = [row['film'] for row in rows if row['torrent']]
    if available:
        rails.append(dict(id='library', title='Можно смотреть прямо сейчас', subtitle='Фильмы и сериалы с подключённым источником — запуск за секунды', films=available))
    shows = [row['film'] for row in rows if row['torrent'] and row['film'].kind == 'series']
    if shows:
        rails.append(dict(id='series', title='Сериалы', subtitle='Сезоны целиком — серия за серией, с того места, где остановились', films=shows, label='Сериалы'))
    if not filtered:
        result = recommendation_web.recommend(rows, stats)
        rails.append(recommendation_web.rail_context(result, rows))
    popular = sorted((row['film'] for row in rows if stats.get(row['film'].id, {}).get('views')),
                     key=lambda m: -stats[m.id]['views'])
    if len(popular) >= 3:
        rails.append(dict(id='popular', title='Популярно в КиноСфере', subtitle='Чаще всего смотрят на этой неделе', films=popular[:12]))
    rated = sorted((row['film'] for row in rows if stats.get(row['film'].id, {}).get('votes')),
                   key=lambda m: -(stats[m.id]['avg'] or 0))
    if len(rated) >= 3:
        rails.append(dict(id='community', title='Высоко оценили зрители', subtitle='Средняя оценка пользователей КиноСферы', films=rated[:12]))
    by_genre = {}
    for film in available_catalog():
        for genre in film.genres:
            by_genre.setdefault(genre, []).append(film)
    for genre, items in sorted(by_genre.items(), key=lambda kv: -len(kv[1]))[:2]:
        if len(items) >= 4:
            rails.append(dict(id=f'genre-{len(rails)}', title=genre, subtitle='Подборка по жанру', films=items[:12]))
    featured = sorted(rows, key=lambda r: (not r['torrent'], not r['art'].get('hero_video'),
                                           not (r['art'].get('backdrop') or r['art'].get('backdrop_upload')), -r['film'].rating))
    featured = [row['film'] for row in featured if row['art'].get('backdrop') or row['art'].get('backdrop_upload') or row['art'].get('hero_video')][:4] or [r['film'] for r in rows[:1]]
    all_genres = sorted({gen for m in available_catalog() for gen in m.genres})
    pagination = paginate(len(films), anchor='catalog')
    total_films = len(films)
    has_series = any(film.kind == 'series' for film in films)
    films = films[pagination['offset']:pagination['offset']+pagination['size']]
    return render_template('index.html', films=films, rails=rails, genres=all_genres, active_genres=genre_filters,
                           sort=sort, filtered=filtered, featured=featured, query=query, user=g.user, kind=kind,
                           pagination=pagination, total_films=total_films, has_series=has_series)


@app.route('/movie/<int:movie_id>')
def movie_detail(movie_id: int):
    row = rows_by_id().get(movie_id)
    if not row or row['film'].is_hidden and not (g.user and g.user.role == 'ADMIN'):
        return render_template('error.html', code=404, message='Фильм не найден или снят с публикации.'), 404
    film = row['film']
    reviews = store().reviews(movie_id)
    my_review = next((r for r in reviews if g.user and r['user_id'] == g.user.id), None)
    state = store().state(g.user.id) if g.user else {}
    user_score = state.get('ratings', {}).get(str(film.id)) if g.user else None
    progress = store().progress(g.user.id, movie_id) if g.user else None
    full = store().film(movie_id, include_hidden=True)
    media = media_summary(full)
    episodes_done = store().episode_progress(g.user.id, movie_id) if g.user and film.kind == 'series' else {}
    current = None
    if film.kind == 'series' and row.get('episodes'):
        current = series.find(row['episodes'], progress['file_id']) if progress and progress.get('file_id') is not None else None
    return render_template('movie.html', film=film, row=row, reviews=reviews, my_review=my_review, user_score=user_score,
                           progress=progress, stats=film_stats().get(movie_id, {}), media=media,
                           episodes_done=episodes_done, current_episode=current,
                           similar=[r['film'] for r in recommendation_web.similar(catalog_rows(), film.id)],
                           active_rooms=[r for r in store().active_rooms() if r['movie_id'] == movie_id][:3],
                           user=g.user)


@app.post('/movie/<int:movie_id>/review')
@login_required
def save_review(movie_id):
    if movie_id not in rows_by_id():
        abort(404)
    try:
        score = int(request.form.get('score', ''))
        if not 1 <= score <= 10:
            raise ValueError
    except ValueError:
        flash('Выберите оценку от 1 до 10.', 'error')
        return redirect(url_for('movie_detail', movie_id=movie_id) + '#reviews')
    text = request.form.get('text', '').strip()[:3000]
    if text and len(text) < 10:
        flash('Отзыв должен содержать хотя бы 10 символов.', 'error')
        return redirect(url_for('movie_detail', movie_id=movie_id) + '#reviews')

    def apply(state):
        state.setdefault('ratings', {})[str(movie_id)] = score
        if movie_id not in state.setdefault('watched', []):
            state['watched'].append(movie_id)
    store().update_state(g.user.id, apply)
    if text:
        store().save_review(g.user.id, movie_id, score, text)
        flash('Спасибо! Отзыв опубликован.')
    else:
        flash(f'Оценка {score}/10 сохранена.')
    return redirect(url_for('movie_detail', movie_id=movie_id) + '#reviews')


@app.post('/movie/<int:movie_id>/review/delete')
@login_required
def delete_review(movie_id):
    try:
        review_id = int(request.form.get('review_id', ''))
    except ValueError:
        abort(400)
    store().delete_review(review_id, None if g.user.role == 'ADMIN' else g.user.id)
    flash('Отзыв удалён.')
    return redirect(url_for('movie_detail', movie_id=movie_id) + '#reviews')


@app.route('/watch/<int:movie_id>')
@login_required
def player(movie_id):
    row = rows_by_id().get(movie_id)
    if not row or (row['film'].is_hidden and g.user.role != 'ADMIN'):
        return render_template('error.html', code=404, message='Фильм не найден.'), 404
    progress = store().progress(g.user.id, movie_id)
    episode, episodes_done = None, {}
    if row['film'].kind == 'series' and row.get('episodes'):
        episodes = row['episodes']
        wanted = request.args.get('ep', type=int)
        if wanted is None and progress and progress.get('file_id') is not None:
            wanted = progress['file_id']
        episode = series.find(episodes, wanted) or episodes[0]
        episodes_done = store().episode_progress(g.user.id, movie_id)
        done = episodes_done.get(episode['file_id'])
        resume = done['position'] if done and not done['completed'] and done['position'] > 30 else 0
    else:
        resume = progress['position'] if progress and not progress['completed'] and progress['position'] > 30 else 0
    if row['torrent']:   # connect to peers while the page loads: the first frame comes sooner
        threading.Thread(target=playback.prewarm, args=(movie_id, f'page-{g.user.id}', 120),
                         kwargs=dict(file_id=episode['file_id'] if episode else None), daemon=True).start()
    return render_template('player.html', film=row['film'], row=row, user=g.user, has_source=bool(row['torrent']),
                           resume=resume, episode=episode, episodes_done=episodes_done,
                           next_episode=series.neighbour(row['episodes'], episode['file_id']) if episode else None,
                           prev_episode=series.neighbour(row['episodes'], episode['file_id'], -1) if episode else None,
                           similar=[r['film'] for r in recommendation_web.similar(catalog_rows(), row['film'].id, 8)])


@app.post('/progress/<int:movie_id>/hide')
@login_required
def hide_progress(movie_id):
    store().hide_progress(g.user.id, movie_id)
    if request.is_json:
        return jsonify(ok=True)
    return redirect(request.referrer if (request.referrer or '').startswith(request.host_url) else url_for('profile', section='history'))


PROFILE_SECTIONS = ('overview', 'history', 'watched', 'ratings', 'watchlist', 'reviews', 'clips', 'company', 'rooms', 'subscription', 'settings')


@app.route('/profile')
@login_required
def profile():
    section = request.args.get('section', 'overview')
    if section == 'friends':
        section = 'company'
    if section not in PROFILE_SECTIONS:
        section = 'overview'
    state = store().state(g.user.id)
    progress = my_progress()
    reviews = store().user_reviews(g.user.id)
    by_id = rows_by_id()
    stats = services.profile_stats(by_id, state, progress, reviews)
    context = dict(section=section, stats=stats, progress=[p for p in progress if p['movie_id'] in by_id],
                   reviews=reviews, account=store().account(user_id=g.user.id))
    watched_ids = set(state.get('watched', [])) | {p['movie_id'] for p in progress if p['completed']}
    watchlist_ids = set(state.get('watchlist', []))
    rating_ids = {int(i) for i in state.get('ratings', {})}
    client_ids = watched_ids | watchlist_ids | rating_ids | {c['movieId'] for c in state.get('clips', [])}
    context['catalog_client'] = [by_id[i]['film'] for i in sorted(client_ids) if i in by_id]
    context['watchlist_preview'] = [by_id[i]['film'] for i in state.get('watchlist', []) if i in by_id][:12]
    if section in ('watchlist', 'watched', 'ratings'):
        ids = {'watchlist':watchlist_ids, 'watched':watched_ids, 'ratings':rating_ids}[section]
        collection = [r['film'] for r in catalog_rows() if r['film'].id in ids]
        paging = paginate(len(collection))
        context['pagination'] = paging
        context['collection_films'] = collection[paging['offset']:paging['offset']+paging['size']]
    context['watched'] = [by_id[i]['film'] for i in watched_ids if i in by_id]
    context['continue_items'] = [p for p in progress if in_progress(p) and p['movie_id'] in by_id][:8]
    if section in ('overview', 'rooms'):
        context['rooms'] = store().user_rooms(g.user.id)
    if section in ('overview', 'company'):
        people = store().co_viewers(g.user.id)
        ratings = store().ratings_of([p['id'] for p in people] + [g.user.id])
        mine = ratings.get(g.user.id, {})
        for person in people:
            person['match'] = services.taste_match(mine, ratings.get(person['id'], {}))
            common = sorted(set(mine) & set(ratings.get(person['id'], {})))
            person['common'] = [by_id[m]['film'].title for m in common if m in by_id][:3]
        context['people'] = people
    if section == 'subscription':
        context['payments'] = store().payments(g.user.id)
    if section in ('overview', 'watchlist'):
        rows = catalog_rows()
        result = recommendation_web.recommend(rows, film_stats(), surface='profile', limit=8)
        context['recommendation_rail'] = recommendation_web.rail_context(result, rows, 'profile')
    return render_template('profile.html', **context)


@app.post('/subscription/pay')
@login_required
def mock_payment():
    """Демо-оплата учебного тарифа без реальных списаний."""
    tariff = request.form.get('tariff', 'plus')
    if tariff not in ('plus', 'free'):
        abort(400)
    if tariff == 'plus':
        store().set_plan(g.user.id, 'plus', 30)
        store().add_payment(g.user.id, 'КиноСфера Плюс · 30 дней', 299.0, 'Успешно (демо)')
        flash('Подписка «Плюс» активирована на 30 дней (демо-оплата, без списаний).')
    else:
        store().set_plan(g.user.id, 'free')
        store().add_payment(g.user.id, 'Переход на бесплатный тариф', 0.0, 'Выполнено')
        flash('Вы перешли на бесплатный тариф.')
    return redirect(url_for('profile', section='subscription'))


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api/'):
        return jsonify(error='Не найдено.'), 404
    return render_template('error.html', code=404, message='Такой страницы нет. Возможно, ссылка устарела.'), 404


@app.errorhandler(403)
def forbidden(error):
    if request.path.startswith('/api/'):
        return jsonify(error=getattr(error, 'description', None) or 'Доступ запрещён.'), 403
    return render_template('error.html', code=403, message='Недостаточно прав для этой страницы.'), 403


if __name__ == '__main__':
    import argparse
    import ipaddress
    import logging

    parser = argparse.ArgumentParser(description='Локальный онлайн-кинотеатр')
    parser.add_argument('--port', type=int, default=int(os.environ.get('CINEMA_PORT', 5050)))
    parser.add_argument('--host', default='127.0.0.1',
                        help='0.0.0.0 — открыть сайт для друзей в локальной сети (для комнат совместного просмотра)')
    parser.add_argument('--torrent', type=Path, help='Однократно заменить источник фильма № 1')
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s: %(message)s')
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    if arguments.torrent:
        if not arguments.torrent.is_file():
            parser.error('Указанный .torrent-файл не найден.')
        from torrent_meta import inspect_torrent
        metadata = arguments.torrent.read_bytes()
        entries, selected = inspect_torrent(metadata)
        app.extensions['store'].set_source(1, metadata, arguments.torrent.name, entries, selected, info_hash(metadata))
    playback.port = arguments.port
    print('>>> Подготовка статистики и индекса рекомендаций…', flush=True)
    recommendation_web.warmup(app)
    print(f'>>> Сайт запущен: http://127.0.0.1:{arguments.port}/')
    try:
        if not ipaddress.ip_address(arguments.host).is_loopback:
            print('>>> Сайт доступен в локальной сети. Друзья могут открыть его по IP этого компьютера.')
    except ValueError:
        pass
    app.run(host=arguments.host, port=arguments.port, debug=False, threaded=True)

