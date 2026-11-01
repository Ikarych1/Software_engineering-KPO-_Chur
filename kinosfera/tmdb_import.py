"""Official TMDB API import, durable CSV jobs and conservative catalog updates.

Only JSON metadata is requested. Images stay on TMDB's CDN; no movie files are
downloaded. Job checkpoints and catalog writes share a SQLite transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from imdb_import import Cancelled, imdb_id, normalized

MAX_TITLES = 20_000
MAX_LIST_BYTES = 10 * 1024 * 1024
PAGE_SIZE = 100
EXAMPLE = 'imdb_id,title,year\ntt0816692,Интерстеллар,2014\ntt3659388,Марсианин,2015\ntt2582802,Одержимость,2014\n'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tmdb_jobs (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, total INTEGER NOT NULL,
    options TEXT NOT NULL, message TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tmdb_rows (
    job_id TEXT NOT NULL REFERENCES tmdb_jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, entry TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', result TEXT,
    PRIMARY KEY(job_id, position));
CREATE INDEX IF NOT EXISTS tmdb_rows_status ON tmdb_rows(job_id, status, position);
CREATE TABLE IF NOT EXISTS tmdb_cache (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
'''


@dataclass(frozen=True)
class Entry:
    line: int
    imdb_id: str = ''
    title: str = ''
    year: int | None = None
    tmdb_id: int | None = None
    kind: str = ''


def read_list(data):
    if not data or len(data) > MAX_LIST_BYTES:
        raise ValueError('Выберите CSV размером до 10 МБ.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Сохраните CSV в кодировке UTF-8.') from exc
    if '\x00' in text or not text.strip():
        raise ValueError('В файле нет списка фильмов в формате CSV.')
    aliases = {'imdb_id':'imdb_id', 'imdb':'imdb_id', 'imdb_url':'imdb_id', 'const':'imdb_id',
               'tmdb_id':'tmdb_id', 'tmdb_url':'tmdb_id', 'title':'title', 'name':'title',
               'название':'title', 'year':'year', 'год':'year', 'type':'kind', 'kind':'kind'}
    delimiter = ';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ','
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        fields = [aliases.get(normalized(f)) for f in reader.fieldnames or []]
        if (not fields or None in fields or len(fields) != len(set(fields))
                or not set(fields) & {'imdb_id', 'tmdb_id', 'title'}):
            raise ValueError('Столбцы CSV: imdb_id,tmdb_id,type,title,year. Достаточно ID или названия.')
        reader.fieldnames = fields
        entries = []
        for row in reader:
            if None in row:
                raise ValueError(f'Строка {reader.line_num}: лишние столбцы; название с запятой заключите в кавычки.')
            row = {k:(v or '').strip() for k,v in row.items()}
            if not any(row.values()):
                continue
            try:
                key = imdb_id(row['imdb_id']) if row.get('imdb_id') else ''
                title, year, kind = row.get('title', ''), row.get('year', ''), row.get('kind', '')
                kind = {'movie':'movie', 'series':'series', 'tv':'series', '':''}.get(kind)
                if kind is None:
                    raise ValueError('type должен быть movie или series.')
                tmdb_key = row.get('tmdb_id', '')
                if tmdb_key.startswith(('https://', 'http://')):
                    parsed = urlsplit(tmdb_key)
                    match = re.fullmatch(r'/(movie|tv)/(\d+)(?:-[\w-]+)?/?', parsed.path)
                    if (parsed.hostname not in ('themoviedb.org', 'www.themoviedb.org') or not match
                            or parsed.username or parsed.password):
                        raise ValueError('Укажите ссылку на фильм или сериал на themoviedb.org.')
                    url_kind = 'series' if match[1] == 'tv' else 'movie'
                    if kind and kind != url_kind:
                        raise ValueError('type не совпадает со ссылкой TMDB.')
                    kind, tmdb_key = url_kind, match[2]
                if tmdb_key and (not re.fullmatch(r'\d{1,10}', tmdb_key) or int(tmdb_key) <= 0):
                    raise ValueError('tmdb_id должен быть положительным числом или ссылкой TMDB.')
                if tmdb_key and not kind:
                    raise ValueError('Для числового tmdb_id укажите type: movie или series.')
                if len(title) > 160 or not (key or tmdb_key or title):
                    raise ValueError('Укажите ID или название длиной до 160 символов.')
                if year and (not re.fullmatch(r'\d{4}', year) or not 1888 <= int(year) <= 2100):
                    raise ValueError('Некорректный год.')
            except ValueError as exc:
                raise ValueError(f'Строка {reader.line_num}: {exc}') from exc
            entries.append(Entry(reader.line_num, key, title, int(year) if year else None,
                                 int(tmdb_key) if tmdb_key else None, kind))
            if len(entries) > MAX_TITLES:
                raise ValueError('За один импорт можно обработать не больше 20 000 строк.')
    except csv.Error as exc:
        raise ValueError('Проверьте разделители и кавычки в CSV.') from exc
    if not entries:
        raise ValueError('В файле нет фильмов.')
    return entries


class Settings:
    """Credentials never enter HTML, job records, URLs (Bearer), or reports."""
    def __init__(self, path):
        self.path = Path(path)

    def credential(self):
        for name in ('TMDB_READ_TOKEN', 'TMDB_API_KEY'):
            if os.environ.get(name, '').strip():
                return os.environ[name].strip()
        try:
            value = json.loads(self.path.read_text(encoding='utf-8')).get('credential', '')
            return value.strip() if isinstance(value, str) else ''
        except (OSError, ValueError, AttributeError):
            return ''

    def from_environment(self):
        return any(os.environ.get(name, '').strip() for name in ('TMDB_READ_TOKEN', 'TMDB_API_KEY'))

    def save(self, credential):
        if not credential or len(credential) > 4096 or re.search(r'\s', credential):
            raise ValueError('Введите API Read Access Token или API Key TMDB.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(6) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as out:
                out.write(json.dumps({'credential':credential}))
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


class APIError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, store, credential, checkpoint=lambda: None, progress=lambda message: None, refresh=False):
        if not credential or len(credential) > 4096 or re.search(r'\s', credential):
            raise APIError('Сначала подключите TMDB: укажите API Read Access Token в настройках импорта.')
        self.store, self.credential = store, credential
        self.checkpoint, self.progress, self.refresh = checkpoint, progress, refresh
        self.opener = build_opener(NoRedirect())
        self.last_request = 0

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.checkpoint()
            time.sleep(min(.2, max(0, until - time.monotonic())))

    def get(self, path, **params):
        self.checkpoint()
        if not re.fullmatch(r'/(?:[a-z_]+|\d+|tt\d+)(?:/(?:[a-z_]+|\d+|tt\d+))*', path):
            raise APIError('Некорректный метод TMDB.')
        key = path + '?' + urlencode(sorted(params.items()))
        with self.store.connect() as db:
            cached = db.execute('SELECT data,updated FROM tmdb_cache WHERE key=?', (key,)).fetchone()
        if cached and not self.refresh and time.time() - cached['updated'] < 7 * 86400:
            return json.loads(cached['data'])
        headers = {'Accept':'application/json', 'User-Agent':'KinoSfera-TMDB-Importer/1.0'}
        url_params = dict(params)
        if re.fullmatch(r'[a-fA-F0-9]{32}', self.credential):
            url_params['api_key'] = self.credential
        else:
            headers['Authorization'] = 'Bearer ' + self.credential
        req = Request('https://api.themoviedb.org/3' + path + '?' + urlencode(url_params), headers=headers)
        for attempt in range(4):
            self.wait(max(0, .25 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.opener.open(req, timeout=25) as response:
                    raw = response.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise APIError('TMDB вернул слишком большой ответ.')
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get('success') is False:
                    raise APIError('TMDB вернул некорректный ответ.')
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO tmdb_cache VALUES(?,?,?)',
                               (key, json.dumps(data, ensure_ascii=False), time.time()))
                return data
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                exc.close()
                if code == 404:
                    return None
                if code in (401, 403):
                    raise APIError('TMDB отклонил доступ. Проверьте ключ и доступность API с сервера.') from None
                if code not in (429, 500, 502, 503, 504):
                    raise APIError(f'TMDB вернул HTTP {code}. Сохранённый импорт можно продолжить позже.') from None
                delay = 2 ** attempt
                if retry_after:
                    try:
                        requested_delay = float(retry_after)
                    except ValueError:
                        try:
                            requested_delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            requested_delay = 0
                    if math.isfinite(requested_delay) and requested_delay > 30:
                        raise APIError('TMDB просит приостановить запросы. Прогресс сохранён; продолжите импорт позже.') from None
                    if math.isfinite(requested_delay):
                        delay = max(1, requested_delay)
            except (URLError, OSError, TimeoutError):
                delay = 2 ** attempt
            except (ValueError, UnicodeError) as exc:
                if isinstance(exc, APIError):
                    raise
                raise APIError('TMDB вернул нечитаемый JSON. Попробуйте продолжить импорт позже.') from None
            if attempt == 3:
                raise APIError('TMDB временно недоступен или ограничил запросы. Прогресс сохранён; продолжите позже.')
            self.progress('Ждём ответа TMDB и повторяем запрос…')
            self.wait(delay)


def positive(value, maximum=720):
    return int(value) if type(value) in (int, float) and math.isfinite(value) and 1 <= value <= maximum else None


def image_url(path, size):
    return 'https://image.tmdb.org/t/p/' + size + path if isinstance(path, str) and re.fullmatch(r'/[\w.-]+\.(?:jpg|png|webp)', path) else ''


def make_record(data, kind, entry):
    series = kind == 'series'
    release = data.get('first_air_date' if series else 'release_date') or ''
    year = int(release[:4]) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', release) else None
    duration = positive(data.get('runtime')) if not series else next(
        (n for value in data.get('episode_run_time', []) if (n := positive(value))), None)
    if series and not duration:
        duration = positive((data.get('last_episode_to_air') or {}).get('runtime'))
    credits = data.get('aggregate_credits' if series else 'credits') or {}
    directors = [p['name'] for p in credits.get('crew', []) if p.get('name') and
                 (p.get('job') == 'Director' or any(j.get('job') == 'Director' for j in p.get('jobs', [])))]
    certificate = []
    if series:
        certificate = [r.get('rating', '') for r in (data.get('content_ratings') or {}).get('results', []) if r.get('iso_3166_1') == 'RU']
    else:
        for region in (data.get('release_dates') or {}).get('results', []):
            if region.get('iso_3166_1') == 'RU':
                certificate.extend(r.get('certification', '') for r in region.get('release_dates', []))
    # Keep Russian certification only. MPAA categories and isAdult are not ages.
    ages = [int(m[1]) for value in certificate if (m := re.fullmatch(r'(0|6|12|16|18)\+?', value))]
    rating = data.get('vote_average')
    rating = round(rating, 1) if type(rating) in (int, float) and math.isfinite(rating) and 0 <= rating <= 10 and data.get('vote_count', 0) > 0 else None
    original = data.get('original_name' if series else 'original_title') or ''
    return dict(tmdb_id=data['id'], kind=kind, imdb_id=(data.get('external_ids') or {}).get('imdb_id') or data.get('imdb_id') or '',
                title=(entry.title or data.get('name' if series else 'title') or original)[:160], original=original[:160],
                year=year, duration=duration, description=(data.get('overview') or '')[:6000],
                genres=[g['name'] for g in data.get('genres', []) if g.get('name')][:10],
                country=', '.join(c['name'] for c in data.get('production_countries', []) if c.get('name'))[:100],
                tagline=(data.get('tagline') or '')[:200], director=', '.join(dict.fromkeys(directors))[:160],
                actors=[p['name'][:100] for p in credits.get('cast', []) if p.get('name')][:30],
                age_rating=f'{max(ages)}+' if ages else '—', rating=rating, votes=data.get('vote_count', 0),
                tmdb_poster_url=image_url(data.get('poster_path'), 'w500'),
                tmdb_backdrop_url=image_url(data.get('backdrop_path'), 'w1280'))


def resolve(entry, client):
    result = dict(line=entry.line, input=entry.imdb_id or str(entry.tmdb_id or entry.title))
    if entry.tmdb_id:
        choices = [(entry.kind, entry.tmdb_id)]
    elif entry.imdb_id:
        found = client.get('/find/' + entry.imdb_id, external_source='imdb_id', language='ru-RU') or {}
        choices = [('movie', r['id']) for r in found.get('movie_results', [])] + [('series', r['id']) for r in found.get('tv_results', [])]
    else:
        found = client.get('/search/multi', query=entry.title, language='ru-RU', include_adult='true') or {}
        choices = []
        for row in found.get('results', []):
            kind = {'movie':'movie', 'tv':'series'}.get(row.get('media_type'))
            names = {normalized(row.get(k)) for k in ('title', 'original_title', 'name', 'original_name')}
            date = row.get('release_date') or row.get('first_air_date') or ''
            if kind and normalized(entry.title) in names and (not entry.year or date.startswith(str(entry.year))):
                choices.append((kind, row['id']))
        if found.get('total_pages', 1) > 1:
            result.update(status='ambiguous', message='Поиск вернул несколько страниц. Укажите IMDb ID или TMDB ID и type.')
            return result, None
    choices = list(dict.fromkeys(pair for pair in choices if not entry.kind or pair[0] == entry.kind))
    if len(choices) != 1:
        result.update(status='ambiguous' if choices else 'not_found',
                      message='Найдено несколько совпадений. Укажите ID и type.' if choices else 'Фильм или сериал не найден в TMDB.',
                      candidates=[dict(kind=k, tmdb_id=i) for k,i in choices[:10]])
        return result, None
    kind, key = choices[0]
    append = 'aggregate_credits,content_ratings,external_ids' if kind == 'series' else 'credits,release_dates,external_ids'
    path = f'/{"tv" if kind == "series" else "movie"}/{key}'
    data = client.get(path, language='ru-RU', append_to_response=append)
    if not data:
        result.update(status='not_found', message='Карточка не найдена в TMDB.')
        return result, None
    if data.get('id') != key:
        raise APIError('TMDB вернул карточку с другим идентификатором.')
    if not data.get('overview'):
        fallback = client.get(path, language='en-US') or {}
        for field in ('overview', 'tagline'):
            if not data.get(field):
                data[field] = fallback.get(field, '')
    record = make_record(data, kind, entry)
    if entry.year and entry.year != record['year'] or entry.imdb_id and record['imdb_id'] and entry.imdb_id != record['imdb_id']:
        result.update(status='mismatch', message='Год или IMDb ID не совпадает с CSV; карточка пропущена.')
        return result, None
    if not record['imdb_id'] and entry.imdb_id:
        record['imdb_id'] = entry.imdb_id
    if not record['year'] or not 1888 <= record['year'] <= 2100 or not record['duration'] or not record['title']:
        result.update(status='incomplete', message='TMDB не указал название, допустимый год или длительность. Дополните карточку вручную.')
        return result, None
    result.update(tmdb_id=key, imdb_id=record['imdb_id'], title=record['title'])
    return result, record


class CatalogWriter:
    """One indexed title lookup per job, instead of scanning the catalog per row."""
    def __init__(self, store):
        self.store, self.aliases = store, {}
        with store.connect() as db:
            for row in db.execute('SELECT id,data,art FROM films'):
                data, art = json.loads(row['data']), json.loads(row['art'])
                for name in {normalized(data['title']), normalized(art.get('original'))} - {''}:
                    self.aliases.setdefault((data.get('kind', 'movie'), data['year'], name), set()).add(row['id'])

    def save(self, db, record, publish):
        kind, key = record['kind'], record['tmdb_id']
        rows = {r['id']:r for r in db.execute('SELECT id,data,art,imdb_id,tmdb_id,tmdb_kind FROM films WHERE (tmdb_id=? AND tmdb_kind=?) OR (imdb_id=? AND imdb_id IS NOT NULL)',
                (key, kind, record['imdb_id'] or None))}
        if len(rows) > 1:
            return None, 'conflict'
        row = next(iter(rows.values()), None)
        if row is None:
            ids = set()
            for name in (record['title'], record['original']):
                ids.update(self.aliases.get((kind, record['year'], normalized(name)), set()))
            candidates = []
            for movie_id in ids:
                candidate = db.execute('SELECT id,data,art,imdb_id,tmdb_id,tmdb_kind FROM films WHERE id=?', (movie_id,)).fetchone()
                if candidate:
                    candidates.append(candidate)
            if len(candidates) > 1:
                return None, 'conflict'
            row = candidates[0] if candidates else None
        if row and ((row['imdb_id'] and record['imdb_id'] and row['imdb_id'] != record['imdb_id'])
                    or (row['tmdb_id'] and (row['tmdb_id'], row['tmdb_kind']) != (key, kind))
                    or json.loads(row['data']).get('kind', 'movie') != kind):
            return None, 'conflict'
        new = row is None
        movie_id = row['id'] if row else db.execute('SELECT COALESCE(MAX(id),0)+1 FROM films').fetchone()[0]
        data = json.loads(row['data']) if row else dict(id=movie_id, title='', description='', genres=[], year=0,
                country='', rating=0, duration_min=0, age_rating='—', is_hidden=not publish, kind=kind)
        art = json.loads(row['art']) if row else dict(slug='placeholder', kp='—', imdb='—')
        owned = dict(art.get('tmdb_fields') or {})

        def fill(target, field, value, empty=('', None, '—'), allowed=True):
            label = ('data.' if target is data else 'art.') + field
            old = target.get(field)
            if value not in ('', None, [], '—') and allowed and (new or old in empty or label in owned and old == owned[label]):
                target[field] = value
                owned[label] = value

        for field, value in dict(title=record['title'], description=record['description'], genres=record['genres'],
                    year=record['year'], country=record['country'], duration_min=record['duration'], age_rating=record['age_rating']).items():
            fill(data, field, value, ('', None, '—', 0, [], 'Описание пока не добавлено.'))
        fill(data, 'rating', record['rating'], ('', None, 0), allowed=art.get('imdb') in (None, '', '—'))
        for field in ('original', 'director', 'actors', 'tagline'):
            fill(art, field, record[field], ('', None, '—', []))
        fill(art, 'tmdb_poster_url', record['tmdb_poster_url'], allowed=not art.get('poster') and art.get('slug', 'placeholder') == 'placeholder')
        fill(art, 'tmdb_backdrop_url', record['tmdb_backdrop_url'], allowed=not art.get('backdrop') and not art.get('backdrop_upload'))
        art.update(tmdb_id=key, tmdb_kind=kind, tmdb_url=f'https://www.themoviedb.org/{"tv" if kind == "series" else "movie"}/{key}',
                   tmdb_rating=record['rating'], tmdb_votes=record['votes'], tmdb_updated_at=time.time(), tmdb_fields=owned)
        imdb_key = (row['imdb_id'] if row else None) or record['imdb_id'] or None
        if imdb_key:
            art.update(imdb_id=imdb_key, imdb_url=f'https://www.imdb.com/title/{imdb_key}/')
        if not data['description']:
            data['description'] = 'Описание пока не добавлено.'
        if new:
            db.execute('INSERT INTO films(id,data,art,revision,imdb_id,tmdb_id,tmdb_kind) VALUES(?,?,?,?,?,?,?)',
                       (movie_id, json.dumps(data, ensure_ascii=False), json.dumps(art, ensure_ascii=False), secrets.token_hex(12), imdb_key, key, kind))
        else:
            db.execute('UPDATE films SET data=?,art=?,imdb_id=?,tmdb_id=?,tmdb_kind=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), json.dumps(art, ensure_ascii=False), imdb_key, key, kind, movie_id))
        for name in {normalized(data['title']), normalized(art.get('original'))} - {''}:
            self.aliases.setdefault((kind, data['year'], name), set()).add(movie_id)
        return movie_id, 'added' if new else 'updated'


@contextmanager
def worker_lock(path):
    """An OS lock also prevents a second web process/CLI from importing concurrently."""
    file = Path(path).open('a+b')
    acquired = False
    try:
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('Другой импорт TMDB уже выполняется.') from None
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('Другой импорт TMDB уже выполняется.') from None
        acquired = True
        yield
    finally:
        if acquired:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)
        file.close()


class ImportJobs:
    def __init__(self, store, settings, client_factory=Client):
        self.store, self.settings, self.client_factory = store, settings, client_factory
        self.lock_path = store.path.with_suffix('.tmdb.lock')
        with store.connect() as db:
            db.executescript(SCHEMA)
        self.recover()

    def recover(self):
        try:
            with worker_lock(self.lock_path), self.store.connect() as db:
                db.execute("UPDATE tmdb_jobs SET state='interrupted',message='Сервер перезапускался. Можно продолжить с сохранённого места.' WHERE state='running'")
        except ValueError:
            pass

    def start(self, entries, publish=True, refresh=False, background=True):
        credential = self.settings.credential()
        if not credential:
            raise ValueError('Сначала подключите TMDB в настройках ниже.')
        token = secrets.token_hex(16)
        lease = worker_lock(self.lock_path)
        lease.__enter__()
        try:
            with self.store.connect() as db:
                db.execute('INSERT INTO tmdb_jobs VALUES(?,?,?,?,?,0,?,?)', (token, 'running', len(entries),
                    json.dumps(dict(publish=publish, refresh=refresh)), 'Подготавливаем импорт…', time.time(), time.time()))
                db.executemany('INSERT INTO tmdb_rows(job_id,position,entry) VALUES(?,?,?)',
                    ((token,i,json.dumps(asdict(e), ensure_ascii=False)) for i,e in enumerate(entries)))
        except Exception:
            lease.__exit__(None, None, None)
            raise
        self.launch(token, credential, lease, background)
        return token

    def resume(self, token, background=True):
        credential = self.settings.credential()
        if not credential:
            raise ValueError('Подключите TMDB, чтобы продолжить импорт.')
        lease = worker_lock(self.lock_path)
        lease.__enter__()
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                job = db.execute('SELECT state FROM tmdb_jobs WHERE id=?', (token,)).fetchone()
                if not job or job['state'] == 'done':
                    raise ValueError('Этот импорт уже завершён или не найден.')
                db.execute("UPDATE tmdb_jobs SET state='running',cancel=0,message='Продолжаем импорт…',updated=? WHERE id=?", (time.time(), token))
        except Exception:
            lease.__exit__(None, None, None)
            raise
        self.launch(token, credential, lease, background)

    def launch(self, token, credential, lease, background):
        def run():
            try:
                self.work(token, credential)
            finally:
                lease.__exit__(None, None, None)
        if background:
            threading.Thread(target=run, daemon=True, name='tmdb-import').start()
        else:
            run()

    def update(self, token, state, message):
        with self.store.connect() as db:
            db.execute('UPDATE tmdb_jobs SET state=?,message=?,updated=? WHERE id=?', (state, message, time.time(), token))

    def work(self, token, credential):
        try:
            # Keep one connection open so WAL is not checkpointed and recreated
            # after every row. Never hold a write transaction across an API call.
            with self.store.connect() as db:
                def checkpoint():
                    row = db.execute('SELECT cancel FROM tmdb_jobs WHERE id=?', (token,)).fetchone()
                    if not row or row['cancel']:
                        raise Cancelled()

                def progress(message):
                    db.execute("UPDATE tmdb_jobs SET message=?,updated=? WHERE id=?", (message, time.time(), token))
                    db.commit()

                options = json.loads(db.execute('SELECT options FROM tmdb_jobs WHERE id=?', (token,)).fetchone()[0])
                pending = [(r['position'], json.loads(r['entry'])) for r in db.execute('SELECT position,entry FROM tmdb_rows WHERE job_id=? AND status=\'pending\' ORDER BY position', (token,))]
                client = self.client_factory(self.store, credential, checkpoint=checkpoint,
                           progress=progress, refresh=options['refresh'])
                writer = CatalogWriter(self.store)
                for position, values in pending:
                    checkpoint()
                    entry = Entry(**values)
                    progress(f'Получаем данные: {entry.title or entry.imdb_id or entry.tmdb_id}…')
                    result, record = resolve(entry, client)
                    checkpoint()
                    db.execute('BEGIN IMMEDIATE')
                    if record:
                        movie_id, action = writer.save(db, record, options['publish'])
                        result.update(movie_id=movie_id, status=action, message='Добавлен в каталог.' if action == 'added' else
                            'Найдены конфликтующие карточки или идентификаторы. Проверьте библиотеку.' if action == 'conflict' else
                            'Данные TMDB обновлены; ручные правки сохранены.')
                    db.execute('UPDATE tmdb_rows SET status=?,result=? WHERE job_id=? AND position=?',
                        (result['status'], json.dumps(result, ensure_ascii=False), token, position))
                    db.commit()
                checkpoint()
            self.update(token, 'done', 'Импорт завершён.')
        except Cancelled:
            self.update(token, 'cancelled', 'Импорт остановлен. Готовые карточки сохранены; оставшиеся можно импортировать позже.')
        except Exception as exc:
            self.update(token, 'error', str(exc) if isinstance(exc, ValueError) else 'Ошибка импорта. Прогресс сохранён; проверьте журнал сервера.')
            if not isinstance(exc, ValueError):
                import logging
                logging.getLogger('kinosfera.tmdb').exception('TMDB import failed')

    def status(self, token, page=1):
        with self.store.connect() as db:
            row = db.execute('SELECT id,state,total,message,created,updated FROM tmdb_jobs WHERE id=?', (token,)).fetchone()
            if not row:
                return None
            job = dict(row)
            counts = {r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM tmdb_rows WHERE job_id=? GROUP BY status', (token,))}
            job['completed'] = job['total'] - counts.pop('pending', 0)
            job['counts'] = counts
            job['pages'] = max(1, math.ceil(job['completed']/PAGE_SIZE))
            job['page'] = min(max(1, page), job['pages'])
            job['results'] = [json.loads(r[0]) for r in db.execute('SELECT result FROM tmdb_rows WHERE job_id=? AND status!=\'pending\' ORDER BY position LIMIT ? OFFSET ?',
                    (token, PAGE_SIZE, (job['page']-1)*PAGE_SIZE))]
            return job

    def recent(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,state,total,created FROM tmdb_jobs ORDER BY created DESC LIMIT 10')]

    def cancel(self, token):
        with self.store.connect() as db:
            db.execute("UPDATE tmdb_jobs SET cancel=1 WHERE id=? AND state='running'", (token,))

    def report(self, token):
        with self.store.connect() as db:
            # Bounded memory even for a 20,000-row job; no token or credentials.
            for row in db.execute('SELECT entry,status,result FROM tmdb_rows WHERE job_id=? ORDER BY position', (token,)):
                yield json.loads(row['result']) if row['result'] else dict(json.loads(row['entry']), status='pending', message='Ещё не обработан.')
