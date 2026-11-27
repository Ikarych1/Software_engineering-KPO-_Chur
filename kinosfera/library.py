"""Administrator area: catalog, sources (.torrent / magnet), posters, users, reviews, rooms, engine."""
from pathlib import Path
import secrets
import threading
import time

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from accounts import admin_required, require_csrf, store
import series
from series import plural
from torrent_meta import MAX_TORRENT_BYTES, inspect_torrent, info_hash, magnet_info, parse_torrent, validate_magnet

admin = Blueprint('admin', __name__, url_prefix='/admin')
IMAGE_TYPES = {b'\xff\xd8\xff': '.jpg', b'\x89PNG\r\n\x1a\n': '.png'}
MAX_IMAGE = 6 * 1024 * 1024
MAX_HERO_VIDEO = 80 * 1024 * 1024
HERO_CLIP_MAX = 45             # seconds cut from the film for the slider
CLIP_JOBS = {}                 # movie_id -> dict(state, message, started)
CLIP_LOCK = threading.Lock()
SOURCE_JOBS = {}               # token -> read .torrent / resolved magnet waiting to be saved
SOURCE_LOCK = threading.Lock()
SOURCE_TTL = 3600


def describe_source(torrent, name, kind):
    """What the administrator sees right after choosing a torrent: files, size, episodes found."""
    entries, file_id = inspect_torrent(torrent)
    meta = parse_torrent(torrent)
    videos = [e for e in entries if e.get('supported')]
    result = dict(name=name, title=meta.name, files=len(entries), videos=len(videos), size=sum(e['size'] for e in entries),
                  selected=next((e['name'] for e in entries if e['id'] == file_id), None), warnings=[])
    if not videos:
        result['warnings'].append('В раздаче нет видео, которое умеет показывать сайт.')
    if kind == 'series':
        episodes = series.detect(entries)
        named = {e['id']: e['name'] for e in entries}
        result.update(episodes=[dict(e, name=named.get(e['file_id'], '')) for e in episodes],
                      seasons=series.summary(episodes) if episodes else '')
        unnumbered = sum(1 for e in videos if series.parse_episode(e['name']) is None)
        if unnumbered:
            result['warnings'].append(f'{unnumbered} файл(ов) без номера серии в имени — пронумерованы по порядку, проверьте таблицу.')
    return entries, file_id, result


def source_job(token):
    with SOURCE_LOCK:
        now = time.time()
        for key in [k for k, job in SOURCE_JOBS.items() if now - job['started'] > SOURCE_TTL]:
            del SOURCE_JOBS[key]
        return SOURCE_JOBS.get(token or '')


def take_source(token):
    """A finished preview job: (torrent, name, magnet) — None when there is none."""
    job = source_job(token)
    if job is None:
        return None
    if job['state'] != 'done':
        raise ValueError('Раздача ещё читается — дождитесь сообщения «Раздача прочитана».' if job['state'] == 'running'
                         else job['message'])
    return job['torrent'], job['name'], job.get('magnet')


def public_job(job, token):
    return dict(token=token, state=job['state'], message=job['message'], meta=job.get('meta'),
                elapsed=round(time.time() - job['started']))


def init_library(app):
    app.register_blueprint(admin)
    # Migrate the already supplied source once, without starting network activity.
    row = app.extensions['store'].film(1, include_hidden=True)
    metadata = Path(app.root_path) / '.local/torrents/interstellar.torrent'
    if row and not row['torrent'] and metadata.is_file():
        try:
            data = metadata.read_bytes()
            entries, file_id = inspect_torrent(data)
            app.extensions['store'].set_source(1, data, metadata.name, entries, file_id, info_hash(data))
        except ValueError:
            pass


def _playback():
    return current_app.extensions.get('playback')


@admin.get('')
@admin.get('/')
@admin_required
def dashboard():
    tab = request.args.get('tab', 'overview')
    if tab not in ('overview', 'films', 'users', 'reviews', 'rooms', 'engine'):
        tab = 'overview'
    films = store().films(include_hidden=True)
    stats = store().film_stats()
    query = request.args.get('q', '').strip()[:80]
    days = request.args.get('days', type=int)
    days = days if days in (1, 7, 30, 90) else None
    context = dict(days=days, tab=tab, films=films, stats=stats, query=query, numbers=store().dashboard(days),
                   film_filter=request.args.get('filter', 'all'))
    if tab == 'users':
        from pagination import paginate
        paging = paginate(store().users_count(query), size=50)
        context['pagination'] = paging
        context['users'] = store().users(query, limit=50, offset=paging['offset'])
    elif tab == 'overview':
        context['users'] = store().users(limit=5, offset=max(0, context['numbers']['users']-5))
    if tab == 'reviews':
        context['reviews'] = store().reviews(limit=300)
    if tab in ('rooms', 'overview'):
        context['rooms'] = store().active_rooms()
    if tab in ('engine', 'overview'):
        playback = _playback()
        context['sessions'] = playback.sessions() if playback else []
    titles = {row['film'].id: row['film'].title for row in films}
    context['titles'] = titles
    return render_template('admin.html', **context)


@admin.get('/api/engine')
@admin_required
def engine_api():
    playback = _playback()
    if not playback:
        return jsonify(running=False, torrents=[], sessions=[])
    data = playback.engine.overview()
    data['sessions'] = playback.sessions()
    data['hls_jobs'] = sum(1 for jobs in list(playback.hls.jobs.values()) for j in jobs if j.alive())
    data['ffmpeg'] = playback.hls.available
    return jsonify(data)


@admin.post('/engine/evict')
@admin_required
def engine_evict():
    try:
        _playback().engine.evict(request.form.get('key', ''))
        flash('Кэш фильма удалён с диска.')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin.dashboard', tab='engine'))


@admin.post('/engine/stop')
@admin_required
def engine_stop():
    key = request.form.get('key', '')
    playback = _playback()
    for lease_id, lease in list(playback.leases.items()):
        if lease.torrent.key == key:
            playback.end(lease_id)
    playback.engine.close_torrent(key)
    flash('Раздача остановлена. Данные сохранены в кэше.')
    return redirect(url_for('admin.dashboard', tab='engine'))


@admin.post('/films/<int:movie_id>/warm')
@admin_required
def warm(movie_id):
    data = request.get_json(silent=True) or {}
    episode = data.get('episode') if isinstance(data, dict) and type(data.get('episode')) is int else None
    ok = _playback().prewarm(movie_id, f'admin-{g.user.id}', 300, file_id=episode)
    if request.accept_mimetypes.best == 'application/json' or request.is_json:
        return jsonify(ok=ok)
    flash('Прогрев начат: движок подключается к раздаче и загружает начало фильма.' if ok else 'У фильма нет источника.')
    return redirect(url_for('admin.edit_film', movie_id=movie_id))


@admin.get('/films/<int:movie_id>/health')
@admin_required
def health(movie_id):
    """Where the source stands, step by step: peers, the start of the video, its analysis."""
    playback = _playback()
    row = store().film(movie_id, include_hidden=True)
    if not row or not row['torrent']:
        return jsonify(error='Источник не добавлен.'), 404
    episode = request.args.get('ep', type=int)
    source, file_id = series.split(row['file_id'] if episode is None else episode)
    try:
        key = info_hash(playback.source_bytes(row, source))
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    torrent = playback.engine.torrents.get(key)
    if torrent is None or file_id not in torrent.views:
        return jsonify(state='idle', message='Раздача не запущена. Нажмите «Проверить раздачу».')
    stats = playback.engine.stats(torrent, file_id)
    view = torrent.views[file_id]
    stats.update(file=view.name.rsplit('/', 1)[-1], size=view.size,
                 head=min(view.size, playback.engine.buffered_ahead(torrent, file_id, 0)),
                 age=round(time.monotonic() - torrent.created))
    media = playback.media.get(f"{key}:{file_id}")
    stats['media'] = media.info if media and media.info else None
    stats['plan'] = media.plan if media and media.plan else None
    stats['probe'] = media.state if media else 'idle'
    stats['probe_error'] = media.error if media and media.error else None
    return jsonify(stats)


def metadata_text(name, label, limit):
    value = request.form.get(name, '').strip()
    if len(value) > limit:
        raise ValueError(f'Поле «{label}» допускает до {limit} символов.')
    return value


def number(name, low, high, cast=int):
    label = {'year': 'Год', 'duration_min': 'Длительность', 'rating': 'Рейтинг'}.get(name, name)
    try:
        value = cast(request.form.get(name, ''))
    except (ValueError, TypeError):
        raise ValueError(f'Введите число в поле «{label}».')
    if not low <= value <= high:
        raise ValueError(f'Поле «{label}» должно быть от {low} до {high}.')
    return value


def save_image(upload, prefix):
    data = upload.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise ValueError('Изображение должно быть не больше 6 МБ.')
    suffix = next((ext for magic, ext in IMAGE_TYPES.items() if data.startswith(magic)), None)
    if suffix is None and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        suffix = '.webp'
    if suffix is None:
        raise ValueError('Загрузите изображение JPEG, PNG или WebP.')
    folder = Path(current_app.static_folder) / 'uploads'
    folder.mkdir(parents=True, exist_ok=True)
    name = f'{prefix}-{secrets.token_hex(6)}{suffix}'
    (folder / name).write_bytes(data)
    return 'uploads/' + name


def save_video(upload, prefix):
    """MP4 / WebM for the slider, validated by signature, not by the name."""
    data = upload.read(MAX_HERO_VIDEO + 1)
    if len(data) > MAX_HERO_VIDEO:
        raise ValueError('Видео для слайдера должно быть не больше 80 МБ. Лучше 10–30 секунд.')
    if data[4:8] == b'ftyp':
        suffix = '.mp4'
    elif data[:4] == b'\x1a\x45\xdf\xa3':
        suffix = '.webm'
    else:
        raise ValueError('Загрузите видео MP4 (H.264) или WebM.')
    folder = Path(current_app.static_folder) / 'uploads'
    folder.mkdir(parents=True, exist_ok=True)
    name = f'{prefix}-{secrets.token_hex(6)}{suffix}'
    (folder / name).write_bytes(data)
    return 'uploads/' + name


def drop_upload(relative):
    """Delete an earlier uploaded file of a card (only inside static/uploads)."""
    if relative and relative.startswith('uploads/') and '/' not in relative[8:] and '..' not in relative:
        (Path(current_app.static_folder) / relative).unlink(missing_ok=True)


def seconds(text):
    """'1:02:03', '12:30' or '95' -> seconds."""
    parts = (text or '').strip().split(':')
    try:
        if not 1 <= len(parts) <= 3 or any(not p.strip().isdigit() for p in parts):
            raise ValueError
        value = 0
        for part in parts:
            value = value * 60 + int(part)
        return value
    except ValueError:
        raise ValueError('Время укажите как 1:02:30, 12:30 или в секундах.') from None


@admin.post('/films/<int:movie_id>/hero-clip')
@admin_required
def hero_clip(movie_id):
    row = store().film(movie_id, include_hidden=True)
    if not row:
        abort(404)
    playback = _playback()
    try:
        if not row['torrent'] or playback is None:
            raise ValueError('Сначала подключите источник видео.')
        start = seconds(request.form.get('start', '0'))
        length = seconds(request.form.get('length', '20'))
        if not 3 <= length <= HERO_CLIP_MAX:
            raise ValueError(f'Длина фрагмента — от 3 до {HERO_CLIP_MAX} секунд.')
        episode = request.form.get('episode', type=int)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with CLIP_LOCK:
        job = CLIP_JOBS.get(movie_id)
        if job and job['state'] == 'running':
            return jsonify(error='Фрагмент уже готовится.'), 409
        job = CLIP_JOBS[movie_id] = dict(state='running', message='Подключаемся к раздаче…', started=time.time())
    app = current_app._get_current_object()
    name = f'hero-{movie_id}-{secrets.token_hex(6)}.mp4'
    target = Path(app.static_folder) / 'uploads' / name

    def work():
        try:
            job['message'] = 'Загружаем нужный кусок фильма и кодируем…'
            result = playback.make_clip(movie_id, start, length, target, file_id=episode)
            with app.app_context():
                fresh = app.extensions['store'].film(movie_id, include_hidden=True)
                art = dict(fresh['art'])
                old = art.get('hero_video')
                art.update(hero_video='uploads/' + name, hero_clip=dict(start=start, length=result['length']))
                app.extensions['store'].set_art(movie_id, art)
                drop_upload(old)
            job.update(state='done', message=f'Готово: {result["length"]:.0f} с, {result["size"] / 1048576:.1f} МБ.',
                       url='/static/uploads/' + name)
        except Exception as exc:  # noqa: BLE001 - shown in the editor
            target.unlink(missing_ok=True)
            job.update(state='error', message=str(exc) if isinstance(exc, (ValueError, RuntimeError, LookupError)) else 'Не удалось вырезать фрагмент.')
    threading.Thread(target=work, daemon=True, name='hero-clip').start()
    return jsonify(job), 202


@admin.get('/films/<int:movie_id>/hero-clip')
@admin_required
def hero_clip_status(movie_id):
    return jsonify(CLIP_JOBS.get(movie_id) or dict(state='idle'))


@admin.post('/sources/inspect')
@admin_required
def inspect_source():
    """Read a .torrent at once, or start getting a magnet's metadata from peers in the background."""
    request.max_content_length = MAX_TORRENT_BYTES + 64 * 1024
    kind = 'series' if request.form.get('kind') == 'series' else 'movie'
    upload = request.files.get('torrent')
    magnet = request.form.get('magnet', '').strip()
    token = secrets.token_urlsafe(12)
    job = dict(state='running', message='Читаем раздачу…', started=time.time(), kind=kind)
    try:
        if upload and upload.filename:
            if not upload.filename.lower().endswith('.torrent'):
                raise ValueError('Источник должен быть файлом .torrent.')
            torrent = upload.read(MAX_TORRENT_BYTES + 1)
            name = Path(upload.filename.replace('\\', '/')).name[:255]
            _, _, meta = describe_source(torrent, name, kind)
            job.update(state='done', torrent=torrent, name=name, meta=meta, message='Раздача прочитана.')
        elif magnet:
            validate_magnet(magnet)
            playback = _playback()
            if playback is None or not playback.engine.available:
                raise ValueError('Для magnet-ссылок нужен установленный libtorrent.')
            job.update(message='Ищем участников раздачи и получаем список файлов…', magnet=magnet)

            def resolve():
                try:
                    torrent = playback.engine.resolve_magnet(magnet, timeout=120)
                    name = ((magnet_info(magnet)['name'] or parse_torrent(torrent).name or 'magnet')[:240] + ' (magnet)')
                    _, _, meta = describe_source(torrent, name, kind)
                    job.update(state='done', torrent=torrent, name=name, meta=meta, message='Раздача прочитана.')
                except Exception as exc:  # noqa: BLE001 - shown in the editor
                    job.update(state='error', message=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Не удалось получить раздачу.')
            threading.Thread(target=resolve, daemon=True, name='magnet').start()
        else:
            raise ValueError('Выберите .torrent-файл или вставьте magnet-ссылку.')
    except ValueError as exc:
        return jsonify(state='error', message=str(exc)), 400
    with SOURCE_LOCK:
        SOURCE_JOBS[token] = job
        while len(SOURCE_JOBS) > 30:
            SOURCE_JOBS.pop(next(iter(SOURCE_JOBS)))
    return jsonify(public_job(job, token)), 200 if job['state'] == 'done' else 202


@admin.get('/sources/inspect/<token>')
@admin_required
def inspect_status(token):
    job = source_job(token)
    if job is None:
        return jsonify(state='error', message='Раздача не найдена — выберите её заново.'), 404
    return jsonify(public_job(job, token))


@admin.post('/films/<int:movie_id>/sources')
@admin_required
def add_season(movie_id):
    """Another torrent for a series (a new season): its episodes join the card."""
    row = store().film(movie_id, include_hidden=True)
    if not row:
        abort(404)
    try:
        if row['film'].kind != 'series' or not row['torrent']:
            raise ValueError('Сезоны добавляются к сериалу с уже подключённой раздачей.')
        taken = take_source(request.form.get('season_token'))
        if taken is None:
            upload = request.files.get('season_torrent')
            if not (upload and upload.filename):
                raise ValueError('Выберите .torrent-файл нового сезона.')
            if not upload.filename.lower().endswith('.torrent'):
                raise ValueError('Источник должен быть файлом .torrent.')
            taken = (upload.read(MAX_TORRENT_BYTES + 1), Path(upload.filename.replace('\\', '/')).name[:255], None)
        torrent, name, magnet = taken
        entries, _, _ = describe_source(torrent, name, 'series')
        season = request.form.get('season_number', '').strip()
        if season and not (season.isdigit() and 1 <= int(season) <= series.MAX_SEASON):
            raise ValueError(f'Номер сезона — от 1 до {series.MAX_SEASON}.')

        def merged(source):
            found = series.detect(series.combined([(source, entries)]))
            if not found:
                raise ValueError('В раздаче нет видео, которое умеет показывать сайт.')
            if season:
                found = [dict(e, season=int(season)) for e in found]
            existing = {(e['season'], e['episode']) for e in row['episodes']}
            clash = sorted({e['season'] for e in found if (e['season'], e['episode']) in existing})
            if clash:
                raise ValueError(f'Серии {", ".join(map(str, clash))} сезона уже есть в карточке. Укажите номер нового сезона '
                                 'или удалите старую раздачу.')
            return series.ordered(row['episodes'] + found)
        source = store().add_source(movie_id, torrent, name, entries, info_hash(torrent), merged, magnet)
        added = [e for e in store().film(movie_id, include_hidden=True)['episodes'] if series.split(e['file_id'])[0] == source]
        count = len(added)
        flash(f'Сезон добавлен: {series.summary(added)} ({count} {plural(count, "серия", "серии", "серий")}). Проверяем раздачу…')
        return redirect(url_for('admin.edit_film', movie_id=movie_id, check=added[0]['file_id']) + '#source')
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin.edit_film', movie_id=movie_id) + '#seasons')


@admin.post('/films/<int:movie_id>/sources/<int:source>/delete')
@admin_required
def delete_season(movie_id, source):
    row = store().film(movie_id, include_hidden=True)
    if not row or source < 1:
        abort(404)
    left = [e for e in row['episodes'] if series.split(e['file_id'])[0] != source]
    store().delete_source(movie_id, source, left)
    playback = _playback()
    if playback:
        playback.end_movie(movie_id)
    flash('Раздача сезона отключена, её серии убраны из карточки.')
    return redirect(url_for('admin.edit_film', movie_id=movie_id) + '#seasons')


@admin.route('/films/new', methods=['GET', 'POST'])
@admin.route('/films/<int:movie_id>', methods=['GET', 'POST'])
@admin_required
def edit_film(movie_id=None):
    row = store().film(movie_id, include_hidden=True) if movie_id else None
    if movie_id and not row:
        abort(404)
    error = None
    if request.method == 'POST':
        request.max_content_length = MAX_HERO_VIDEO + 24 * 1024 * 1024
        try:
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            genres = list(dict.fromkeys(g.strip().capitalize() for g in request.form.get('genres', '').split(',') if g.strip()))
            if not 1 <= len(title) <= 160:
                raise ValueError('Название должно содержать от 1 до 160 символов.')
            if not 10 <= len(description) <= 6000:
                raise ValueError('Описание должно содержать от 10 до 6000 символов.')
            if not genres or len(genres) > 10 or any(len(genre) > 40 for genre in genres):
                raise ValueError('Укажите от 1 до 10 жанров через запятую.')
            data = dict(title=title, description=description, genres=genres,
                        year=number('year', 1888, 2100), country=metadata_text('country', 'Страна', 100),
                        rating=number('rating', 0, 10, float), duration_min=number('duration_min', 1, 720),
                        age_rating=request.form.get('age_rating', '16+'), is_hidden=request.form.get('published') != 'on',
                        kind='series' if request.form.get('kind') == 'series' else 'movie')
            if data['age_rating'] not in ('—', '0+', '6+', '12+', '16+', '18+'):
                raise ValueError('Выберите возрастное ограничение.')
            art = dict(row['art']) if row else dict(slug='placeholder')
            actors = [a.strip() for a in request.form.get('actors', '').split(',') if a.strip()]
            if len(actors) > 30 or any(len(name) > 100 for name in actors):
                raise ValueError('Укажите до 30 актёров. Каждое имя может содержать до 100 символов.')
            art.update(original=metadata_text('original', 'Оригинальное название', 160),
                       director=metadata_text('director', 'Режиссёр', 160),
                       tagline=metadata_text('tagline', 'Слоган', 200),
                       actors=actors,
                       kp=metadata_text('kp', 'Кинопоиск', 6) or '—',
                       imdb=metadata_text('imdb', 'IMDb', 6) or '—',
                       trailer=metadata_text('trailer', 'Трейлер', 300))
            for field, prefix in (('poster', 'poster'), ('backdrop_file', 'backdrop')):
                upload = request.files.get(field)
                if upload and upload.filename:
                    art['poster' if field == 'poster' else 'backdrop_upload'] = save_image(upload, prefix)
            video = request.files.get('hero_video_file')
            if video and video.filename:
                old = art.get('hero_video')
                art['hero_video'] = save_video(video, 'hero')
                art.pop('hero_clip', None)
                drop_upload(old)
            elif request.form.get('hero_video_remove') == 'on' and art.get('hero_video'):
                drop_upload(art.pop('hero_video'))
                art.pop('hero_clip', None)
            upload = request.files.get('torrent')
            magnet = request.form.get('magnet', '').strip()
            torrent, name, entries, magnet_value, digest = None, None, None, None, None
            file_id = row['file_id'] if row else None
            taken = take_source(request.form.get('source_token'))
            if taken is not None:          # already read (and for a magnet resolved) by the preview
                torrent, name, magnet_value = taken
                entries, file_id = inspect_torrent(torrent)
                digest = info_hash(torrent)
            elif upload and upload.filename:
                if not upload.filename.lower().endswith('.torrent'):
                    raise ValueError('Источник должен быть файлом .torrent.')
                torrent = upload.read(MAX_TORRENT_BYTES + 1)
                entries, file_id = inspect_torrent(torrent)
                name = Path(upload.filename.replace('\\', '/')).name[:255]
                digest = info_hash(torrent)
            elif magnet and (not row or magnet != (row.get('magnet') or '')):
                validate_magnet(magnet)
                playback = _playback()
                if playback is None or not playback.engine.available:
                    raise ValueError('Для magnet-ссылок нужен установленный libtorrent.')
                torrent = playback.engine.resolve_magnet(magnet, timeout=75)
                entries, file_id = inspect_torrent(torrent)
                meta = magnet_info(magnet)
                name = (meta['name'] or parse_torrent(torrent).name or 'magnet')[:255] + ' (magnet)'
                magnet_value = magnet
                digest = info_hash(torrent)
            elif row and row['torrent']:
                try:
                    file_id = int(request.form.get('file_id', row['file_id']))
                except (ValueError, TypeError):
                    raise ValueError('Выберите видео из раздачи.')
                if not any(entry['id'] == file_id and entry['supported'] for entry in row['files']):
                    raise ValueError('Выбранный видеофайл не поддерживается.')
            if not row and torrent is None and not data['is_hidden']:
                raise ValueError('Чтобы опубликовать фильм, добавьте .torrent-файл или magnet-ссылку (или сохраните черновик).')
            episodes = []
            if data['kind'] == 'series':
                episodes = episodes_from_form(row, entries)
                if episodes:
                    file_id = episodes[0]['file_id']
            saved = store().save_film(data, art, torrent, name, entries, file_id, movie_id, magnet=magnet_value,
                                      info_hash=digest, episodes=episodes)
            if torrent is not None or (row and file_id != row['file_id']):
                playback = _playback()
                if playback:
                    playback.end_movie(saved)
            found = f' Найдено серий: {len(episodes)} ({series.summary(episodes)}).' if episodes and torrent is not None else ''
            flash('Карточка сохранена.' + (' Источник подключён — проверяем раздачу.' if torrent is not None else '') + found
                  if not data['is_hidden'] else 'Черновик сохранён.' + found)
            if torrent is not None:
                return redirect(url_for('admin.edit_film', movie_id=saved, check=file_id) + '#source')
            return redirect(url_for('admin.edit_film', movie_id=saved))
        except ValueError as exc:
            error = str(exc)
        except RuntimeError as exc:
            error = str(exc)
    stats = store().film_stats().get(movie_id, {}) if movie_id else {}
    return render_template('admin_film.html', row=row, error=error, stats=stats, episode_rows=episode_rows(row))


def card_sources(row, main_files=None):
    """[(source, files)] of a card: its own torrent (0) and the seasons added later."""
    if not row:
        return [(0, main_files or [])], []
    extra = store().sources(row['film'].id)
    files = main_files if main_files is not None else (row['files'] if row['torrent'] else [])
    return [(0, files)] + [(s['source'], s['files']) for s in extra], extra


def episode_rows(row):
    """The series editor table, grouped by torrent (posted values win after an error)."""
    if not row or not row['torrent']:
        return []
    sources, extra = card_sources(row)
    files = series.combined(sources)
    current = {e['file_id']: e for e in (row['episodes'] or series.detect(files))}
    posted = request.method == 'POST' and any(key.startswith('ep-') for key in request.form)
    names = {0: row['torrent_name']} | {s['source']: s['name'] for s in extra}
    groups = []
    for source, _ in sources:
        rows = []
        for entry in (e for e in files if e['source'] == source and e.get('supported')):
            item, prefix = current.get(entry['id']) or {}, f"ep-{entry['id']}-"
            value = (lambda key, default: request.form.get(prefix + key, '')) if posted else (lambda key, default: default)
            rows.append(dict(id=entry['id'], name=entry['name'], size=entry['size'],
                             on=(request.form.get(prefix + 'on') == 'on') if posted else bool(item),
                             season=value('season', item.get('season', 1)), number=value('number', item.get('episode', '')),
                             title=value('title', item.get('title', ''))))
        found = [current[r['id']] for r in rows if r['id'] in current]
        groups.append(dict(source=source, name=names.get(source) or 'Раздача', rows=rows,
                           summary=series.summary(found) if found else 'серии не выбраны'))
    return groups


def episodes_from_form(row, main_files=None):
    """Episodes of a series card over all its torrents: a new main torrent is detected afresh
    (added seasons stay), otherwise the table as edited (or detected again on request)."""
    sources, _ = card_sources(row, main_files)
    files = series.combined(sources)
    if not files:
        return []
    previous = row['episodes'] if row else []
    if main_files is not None:
        kept = [e for e in previous if series.split(e['file_id'])[0] != 0]
        return series.validate(series.detect(series.combined(sources[:1])) + kept, files)
    if request.form.get('redetect') == 'on':
        return series.detect(files)
    if not any(key.startswith('ep-') for key in request.form):
        return previous or series.detect(files)
    chosen = []
    for entry in files:
        prefix = f"ep-{entry['id']}-"
        if entry.get('supported') and request.form.get(prefix + 'on') == 'on':
            chosen.append(dict(file_id=entry['id'], season=request.form.get(prefix + 'season'),
                               episode=request.form.get(prefix + 'number'), title=request.form.get(prefix + 'title', '')))
    return series.validate(chosen, files)


@admin.post('/films/<int:movie_id>/delete')
@admin_required
def delete_film(movie_id):
    row = store().film(movie_id, include_hidden=True)
    if not row:
        abort(404)
    if request.form.get('confirm') != row['film'].title:
        flash('Для удаления введите точное название фильма.', 'error')
        return redirect(url_for('admin.edit_film', movie_id=movie_id))
    playback = _playback()
    if playback:
        playback.end_movie(movie_id)
    store().delete_film(movie_id)
    flash(f'Фильм «{row["film"].title}» удалён из библиотеки.')
    return redirect(url_for('admin.dashboard', tab='films'))


@admin.post('/users/<int:user_id>')
@admin_required
def edit_user(user_id):
    role = request.form.get('role')
    if role not in (None, 'USER'):
        abort(403)
    try:
        store().set_user_active(user_id, request.form.get('active') == 'on')
        playback = _playback()
        if playback:
            playback.end_user(user_id)
        flash('Статус пользователя обновлён.')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin.dashboard', tab='users', q=request.args.get('q', '')))


@admin.post('/users/<int:user_id>/sessions')
@admin_required
def reset_sessions(user_id):
    store().reset_sessions(user_id)
    playback = _playback()
    if playback:
        playback.end_user(user_id)
    flash('Все сеансы пользователя завершены.')
    return redirect(url_for('admin.dashboard', tab='users'))


@admin.post('/reviews/<int:review_id>/delete')
@admin_required
def delete_review(review_id):
    store().delete_review(review_id)
    flash('Отзыв удалён.')
    return redirect(request.form.get('back') if (request.form.get('back') or '').startswith('/') else url_for('admin.dashboard', tab='reviews'))


@admin.post('/rooms/<code>/end')
@admin_required
def end_room(code):
    hub = current_app.extensions['rooms']
    room = hub.get(code)
    if room:
        hub.end(room, g.user)
        flash(f'Комната {code} закрыта.')
    return redirect(url_for('admin.dashboard', tab='rooms'))
