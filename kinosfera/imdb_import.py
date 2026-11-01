"""IMDb catalog import from the publisher's non-commercial TSV datasets.

No HTML scraping, API key or third-party metadata service. Large gzip datasets
are cached on disk and scanned sequentially; only requested titles stay in RAM.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import gzip
import io
import json
from pathlib import Path
import re
import secrets
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_LIST_BYTES = 512 * 1024
MAX_TITLES = 500
MAX_DATASET_BYTES = 1024 ** 3
DATASETS = {
    'basics': ('title.basics.tsv.gz', {'tconst', 'titleType', 'primaryTitle', 'originalTitle', 'startYear', 'runtimeMinutes', 'genres'}),
    'ratings': ('title.ratings.tsv.gz', {'tconst', 'averageRating', 'numVotes'}),
    'crew': ('title.crew.tsv.gz', {'tconst', 'directors'}),
    'principals': ('title.principals.tsv.gz', {'tconst', 'nconst', 'category'}),
    'names': ('name.basics.tsv.gz', {'nconst', 'primaryName'}),
    'akas': ('title.akas.tsv.gz', {'titleId', 'title', 'region', 'language'}),
}
GENRES = dict(Action='Боевик', Adventure='Приключения', Animation='Мультфильм', Biography='Биография',
              Comedy='Комедия', Crime='Криминал', Documentary='Документальный', Drama='Драма',
              Family='Семейный', Fantasy='Фэнтези', Film_Noir='Нуар', History='История', Horror='Ужасы',
              Music='Музыка', Musical='Мюзикл', Mystery='Детектив', Romance='Мелодрама',
              Sci_Fi='Фантастика', Sport='Спорт', Thriller='Триллер', War='Военный', Western='Вестерн')
KINDS = {'movie': 'movie', 'tvMovie': 'movie', 'tvSeries': 'series', 'tvMiniSeries': 'series'}


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def imdb_id(value):
    value = value.strip()
    if re.fullmatch(r'tt\d{7,12}', value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme in ('https', 'http') and parsed.hostname in ('imdb.com', 'www.imdb.com', 'm.imdb.com'):
        match = re.fullmatch(r'/title/(tt\d{7,12})/?', parsed.path)
        if match and not parsed.username and not parsed.password:
            return match[1]
    raise ValueError('Укажите IMDb ID вида tt0816692 или ссылку https://www.imdb.com/title/tt0816692/.')


@dataclass(frozen=True)
class Entry:
    line: int
    imdb_id: str = ''
    title: str = ''
    year: int | None = None


def read_list(data):
    if not data or len(data) > MAX_LIST_BYTES:
        raise ValueError('Загрузите CSV размером до 512 КБ.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Сохраните CSV в кодировке UTF-8.') from exc
    if '\x00' in text:
        raise ValueError('Файл должен содержать текст CSV.')
    if not text.strip():
        raise ValueError('В файле нет фильмов.')
    delimiter = ';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ','
    aliases = {'const':'imdb_id', 'imdb':'imdb_id', 'imdb_id':'imdb_id', 'imdb_url':'imdb_id',
               'title':'title', 'name':'title', 'название':'title', 'year':'year', 'год':'year'}
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        fields = [aliases.get(normalized(field)) for field in reader.fieldnames or []]
        if not fields or None in fields or len(fields) != len(set(fields)) or not ({'imdb_id', 'title'} & set(fields)):
            raise ValueError('Заголовки CSV: imdb_id,title,year. Достаточно imdb_id или title; year необязателен.')
        reader.fieldnames = fields
        result = []
        for row in reader:
            if None in row:
                raise ValueError(f'Строка {reader.line_num}: лишние столбцы. Название с запятой заключите в кавычки.')
            row = {k:(v or '').strip() for k,v in row.items()}
            if not any(row.values()):
                continue
            key, title, year = row.get('imdb_id', ''), row.get('title', ''), row.get('year', '')
            if len(title) > 300:
                raise ValueError(f'Строка {reader.line_num}: название длиннее 300 символов.')
            if not key and not title:
                raise ValueError(f'Строка {reader.line_num}: укажите IMDb ID или название.')
            if year and (not re.fullmatch(r'\d{4}', year) or not 1888 <= int(year) <= 2200):
                raise ValueError(f'Строка {reader.line_num}: некорректный год.')
            try:
                key = imdb_id(key) if key else ''
            except ValueError as exc:
                raise ValueError(f'Строка {reader.line_num}: {exc}') from exc
            result.append(Entry(reader.line_num, key, title, int(year) if year else None))
            if len(result) > MAX_TITLES:
                raise ValueError('За один импорт можно добавить не больше 500 строк.')
    except csv.Error as exc:
        raise ValueError('Не удалось прочитать CSV. Проверьте разделители и кавычки.') from exc
    if not result:
        raise ValueError('В файле нет фильмов.')
    return result


class Cancelled(Exception):
    pass


class DatasetCache:
    def __init__(self, root, progress=lambda **kwargs: None, cancelled=lambda: False):
        self.root = Path(root)
        self.progress, self.cancelled = progress, cancelled

    def checkpoint(self):
        if self.cancelled():
            raise Cancelled('Импорт отменён.')

    def ensure(self, key, refresh=False):
        name, _ = DATASETS[key]
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        if path.is_file() and not refresh:
            return path
        temporary = self.root / (name + '.' + secrets.token_hex(6) + '.part')
        self.progress(stage='download', message=f'Загружаем {name} с IMDb…', dataset=name, downloaded=0, total=0)
        try:
            req = Request('https://datasets.imdbws.com/' + name,
                          headers={'User-Agent':'KinoSfera-IMDb-Importer/1.0', 'Accept-Encoding':'identity'})
            with urlopen(req, timeout=45) as response, temporary.open('xb') as output:
                if urlsplit(response.geturl()).hostname != 'datasets.imdbws.com':
                    raise ValueError('IMDb вернул неожиданный адрес набора данных.')
                size, updated = 0, 0
                total = int(response.headers.get('Content-Length', '0'))
                if total > MAX_DATASET_BYTES:
                    raise ValueError('Набор IMDb превышает лимит загрузки 1 ГБ.')
                while block := response.read(256 * 1024):
                    self.checkpoint()
                    size += len(block)
                    if size > MAX_DATASET_BYTES:
                        raise ValueError('Набор IMDb превышает лимит загрузки 1 ГБ.')
                    output.write(block)
                    if time.monotonic() - updated > .4:
                        updated = time.monotonic()
                        self.progress(downloaded=size, total=total)
                if total and size != total:
                    raise ValueError('Набор IMDb загрузился не полностью. Повторите импорт.')
            with gzip.open(temporary, 'rt', encoding='utf-8') as sample:
                header = sample.readline(16384).rstrip('\r\n').split('\t')
                if not DATASETS[key][1].issubset(header):
                    raise ValueError('В наборе IMDb изменился формат столбцов.')
            temporary.replace(path)
        except (HTTPError, URLError, TimeoutError, OSError, EOFError) as exc:
            raise ValueError(f'Не удалось загрузить {name} с IMDb. Проверьте интернет и повторите импорт.') from exc
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def rows(self, key):
        name, required = DATASETS[key]
        self.progress(stage='scan', dataset=name, message=f'Читаем {name}…', scanned=0, downloaded=0, total=0)
        try:
            with gzip.open(self.root / name, 'rt', encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream, delimiter='\t', quoting=csv.QUOTE_NONE)
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError(f'Некорректные столбцы в {name}. Обновите данные IMDb.')
                for count, row in enumerate(reader, 1):
                    if count % 5000 == 0:
                        self.checkpoint()
                    if count % 250000 == 0:
                        self.progress(scanned=count)
                    yield row
                self.checkpoint()
        except (OSError, EOFError, UnicodeError, csv.Error) as exc:
            raise ValueError(f'Не удалось прочитать {name}. Включите обновление данных и повторите импорт.') from exc


def number(value, cast=int):
    try:
        return cast(value)
    except (ValueError, TypeError):
        return None


def basic_record(row):
    return dict(imdb_id=row['tconst'], title=row['primaryTitle'], original=row['originalTitle'],
                year=number(row['startYear']), duration=number(row['runtimeMinutes']),
                genres=[GENRES.get(g.replace('-', '_'), g) for g in row['genres'].split(',') if g != r'\N'],
                adult=row.get('isAdult') == '1', kind=KINDS[row['titleType']], rating=None, votes=None,
                directors=[], actors=[])


def resolve(entries, cache, refresh=False):
    requested = {entry.imdb_id for entry in entries if entry.imdb_id}
    by_name = {}
    for entry in entries:
        if not entry.imdb_id:
            by_name.setdefault(normalized(entry.title), []).append(entry)
    found, matches = {}, {entry.line:[] for entry in entries}
    wanted_names = set(by_name)
    for row in cache.rows('basics'):
        if row.get('titleType') not in KINDS:
            continue
        key = row.get('tconst')
        names = {normalized(row.get('primaryTitle')), normalized(row.get('originalTitle'))} if wanted_names else set()
        hit = names & wanted_names
        if key not in requested and not hit:
            continue
        record = basic_record(row)
        if key in requested:
            found[key] = record
        for name in hit:
            for entry in by_name[name]:
                if entry.year is None or entry.year == record['year']:
                    candidates = matches[entry.line]
                    if len(candidates) < 11 and not any(c['imdb_id'] == key for c in candidates):
                        candidates.append(record)
        if not wanted_names and requested <= found.keys():
            break
    # Russian titles are in the official alternative-title dataset, not basics.
    localized = {name:values for name,values in by_name.items()
                 if re.search('[а-яё]', name) and any(not matches[e.line] for e in values)}
    overflow = set()
    if localized:
        cache.ensure('akas', refresh)
        aliases = {name:set() for name in localized}
        count = 0
        for row in cache.rows('akas'):
            if row['region'] not in ('RU', 'SU') and row['language'] != 'ru':
                continue
            name = normalized(row['title'])
            if name not in aliases or name in overflow:
                continue
            key = row['titleId']
            if key not in aliases[name]:
                if len(aliases[name]) >= 1000 or count >= 20000:
                    overflow.add(name)
                else:
                    aliases[name].add(key); count += 1
        by_id = {}
        for name,ids in aliases.items():
            if name not in overflow:
                for key in ids:
                    by_id.setdefault(key, []).extend(localized[name])
        unseen = set(by_id)
        if unseen:
            for row in cache.rows('basics'):
                key = row.get('tconst')
                if key not in by_id:
                    continue
                unseen.discard(key)
                if row.get('titleType') in KINDS:
                    record = basic_record(row)
                    for entry in by_id[key]:
                        choices = matches[entry.line]
                        if ((entry.year is None or entry.year == record['year']) and len(choices) < 11
                                and not any(c['imdb_id'] == key for c in choices)):
                            choices.append(record)
                if not unseen:
                    break
    results, selected = [], {}
    for entry in entries:
        choices = [found[entry.imdb_id]] if entry.imdb_id in found else matches[entry.line]
        result = dict(line=entry.line, input=entry.imdb_id or entry.title)
        if not entry.imdb_id and normalized(entry.title) in overflow:
            result.update(status='ambiguous', message='Слишком много совпадений русского названия. Укажите IMDb ID.')
        elif not choices:
            result.update(status='not_found', message='Фильм или сериал не найден. Проверьте IMDb ID, оригинальное название и год.')
        elif len(choices) != 1:
            result.update(status='ambiguous', message='Найдено несколько совпадений. Укажите IMDb ID.',
                          candidates=[dict(imdb_id=c['imdb_id'], title=c['title'], year=c['year']) for c in choices[:10]])
        elif entry.year is not None and choices[0]['year'] != entry.year:
            result.update(status='mismatch', message=f'Год IMDb ({choices[0]["year"] or "не указан"}) не совпадает с CSV.')
        elif not choices[0]['year'] or not choices[0]['duration']:
            result.update(status='incomplete', message='IMDb не указал год или длительность. Добавьте карточку вручную, чтобы не сохранять выдуманные значения.')
        else:
            record = choices[0]
            selected[record['imdb_id']] = record
            result.update(status='resolved', imdb_id=record['imdb_id'], title=entry.title or record['title'])
        results.append(result)
    return results, selected


def enrich(selected, cache, credits=False, refresh=False):
    if not selected:
        return
    cache.ensure('ratings', refresh)
    remaining = set(selected)
    for row in cache.rows('ratings'):
        if row['tconst'] in selected:
            selected[row['tconst']].update(rating=number(row['averageRating'], float), votes=number(row['numVotes']))
            remaining.discard(row['tconst'])
            if not remaining:
                break
    if not credits:
        return
    for key in ('crew', 'principals', 'names'):
        cache.ensure(key, refresh)
    directors, actors, names = {}, {}, set()
    for row in cache.rows('crew'):
        if row['tconst'] in selected:
            ids = [n for n in row['directors'].split(',') if re.fullmatch(r'nm\d+', n)][:10]
            directors[row['tconst']] = ids
            names.update(ids)
    for row in cache.rows('principals'):
        if row['tconst'] in selected and row['category'] in ('actor', 'actress'):
            ids = actors.setdefault(row['tconst'], [])
            if len(ids) < 15 and row['nconst'] not in ids:
                ids.append(row['nconst']); names.add(row['nconst'])
    people = {}
    if names:
        for row in cache.rows('names'):
            if row['nconst'] in names:
                people[row['nconst']] = row['primaryName']
                if names <= people.keys():
                    break
    for key, record in selected.items():
        record['directors'] = [people[n] for n in directors.get(key, []) if n in people]
        record['actors'] = [people[n] for n in actors.get(key, []) if n in people]


def run_import(store, entries, cache, publish=True, credits=False, refresh=False):
    cache.ensure('basics', refresh)
    results, selected = resolve(entries, cache, refresh)
    enrich(selected, cache, credits, refresh)
    cache.checkpoint()
    cache.progress(stage='save', message='Добавляем карточки в каталог…', scanned=0)
    # One transaction for all valid entries: a failure leaves the catalog unchanged.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for result in results:
            cache.checkpoint()
            if result['status'] != 'resolved':
                continue
            record = dict(selected[result['imdb_id']])
            record['display_title'] = result['title']
            movie_id, action = store.import_imdb(db, record, publish=publish, credits=credits)
            message = ('Добавлен в каталог.' if action == 'added' else
                       'Найдено несколько карточек с этим названием и годом. Устраните дубликаты в библиотеке.' if action == 'conflict' else
                       'Карточка уже была в каталоге; данные IMDb обновлены.')
            result.update(movie_id=movie_id, status=action, message=message)
    return results


class ImportJobs:
    def __init__(self, cache_root):
        self.cache_root = Path(cache_root)
        self.lock = threading.RLock()
        self.jobs = {}

    def start(self, store, entries, **options):
        with self.lock:
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise ValueError('Другой импорт IMDb уже выполняется. Дождитесь завершения или отмените его.')
            token = secrets.token_hex(16)
            job = dict(id=token, state='running', stage='start', message='Подготавливаем импорт…',
                       count=len(entries), started=time.time(), downloaded=0, total=0, scanned=0,
                       results=[], cancel=False)
            self.jobs[token] = job
            while len(self.jobs) > 20:
                del self.jobs[next(iter(self.jobs))]

        def progress(**values):
            with self.lock:
                job.update(values)

        def work():
            cache = DatasetCache(self.cache_root, progress, lambda: job['cancel'])
            try:
                results = run_import(store, entries, cache, **options)
                counts = {key:sum(r['status'] == key for r in results) for key in ('added','updated','not_found','ambiguous','mismatch')}
                progress(state='done', stage='done', message=f'Импорт завершён: добавлено {counts["added"]}, обновлено {counts["updated"]}, пропущено {len(results)-counts["added"]-counts["updated"]}.', results=results, counts=counts)
            except Cancelled:
                progress(state='cancelled', message='Импорт отменён. Каталог не изменён.')
            except Exception as exc:
                progress(state='error', message=str(exc) if isinstance(exc, ValueError) else 'Ошибка импорта IMDb. Проверьте журнал сервера.')
                import logging
                logging.getLogger('kinosfera.imdb').exception('IMDb import failed')
        threading.Thread(target=work, daemon=True, name='imdb-import').start()
        return token

    def status(self, token):
        with self.lock:
            job = self.jobs.get(token)
            return {k:v for k,v in job.items() if k != 'cancel'} if job else None

    def cancel(self, token):
        with self.lock:
            if token in self.jobs:
                self.jobs[token]['cancel'] = True


EXAMPLE = 'imdb_id,title,year\ntt0816692,Интерстеллар,2014\ntt3659388,Марсианин,2015\ntt2582802,Одержимость,2014\n'
