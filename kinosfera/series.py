"""Series support: episodes detected from torrent file names, edited by the administrator.

An episode is one video file of one of the series' torrents: ``dict(file_id, season, episode, title)``.
A series may have several torrents (usually one per season). ``file_id`` is then a combined id:
``source * SOURCE_STRIDE + file index`` — source 0 is the card's main torrent, so ids of a
single-torrent series are plain file indexes, as before.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePosixPath
import re

MAX_SEASON = 99
MAX_EPISODE = 999
SOURCE_STRIDE = 1_000_000          # > torrent_meta.MAX_FILES


def uid(source, file_index):
    return source * SOURCE_STRIDE + file_index


def split(episode_id):
    """(source, file index inside that torrent)."""
    return divmod(int(episode_id), SOURCE_STRIDE)


def combined(sources):
    """Files of several torrents as one list whose ``id`` is the combined episode id."""
    return [dict(entry, id=uid(source, entry['id']), source=source) for source, files in sources for entry in files]


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def summary(episodes):
    """'1 сезон: 1–10 · 2 сезон: 1–13' for previews and messages."""
    parts = []
    for season, items in seasons(episodes).items():
        numbers = [e['episode'] for e in items]
        span = f'{min(numbers)}–{max(numbers)}' if len(numbers) > 1 else str(numbers[0])
        parts.append(f'{season} сезон: {span}')
    return ' · '.join(parts)

# Most specific first. Each returns (season, episode).
_PATTERNS = [
    re.compile(r'(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)', re.I),                 # S01E02, s1.e2
    re.compile(r'(?<![a-z0-9])(\d{1,2})x(\d{1,3})(?!\d)', re.I),                           # 1x02
    re.compile(r'(?:season|сезон)[ ._-]*(\d{1,2}).*?(?:episode|ep|серия|эпизод)[ ._-]*(\d{1,3})', re.I),
]
_SEASON = re.compile(r'(?:season|сезон|(?<![a-z0-9])s)[ ._-]*(\d{1,2})(?!\d)', re.I)
_EPISODE = re.compile(r'(?:episode|серия|эпизод|(?<![a-z0-9])ep?)[ ._-]*(\d{1,3})(?!\d)', re.I)
_NUMBER = re.compile(r'(?<!\d)(\d{1,3})(?!\d)')
_TAGS = re.compile(r'\b(?:web[ ._-]?(?:dl|rip)|hdtv|bdrip|bluray|hdrip|dvdrip|\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|'
                   r'sdr|hdr|aac|ac3|dts|\d+xrus|rus|eng|ukr|sub|proper|repack|amzn|nf)\b.*$', re.I)


def _clean_title(text):
    """Episode title from what follows SxxEyy: 'The.Name.WEB-DL.2160p' -> 'The Name'."""
    text = _TAGS.sub('', text.replace('.', ' ').replace('_', ' ')).strip(' -–—[]()')
    return re.sub(r'\s+', ' ', text)[:120]


def parse_episode(name):
    """(season, episode, title) from one file path, or None when nothing looks like an episode."""
    path = PurePosixPath(name)
    stem = path.stem
    for pattern in _PATTERNS:
        match = pattern.search(stem)
        if match:
            return int(match.group(1)), int(match.group(2)), _clean_title(stem[match.end():])
    folder_season = None
    for part in reversed(path.parts[:-1]):
        match = _SEASON.search(part)
        if match:
            folder_season = int(match.group(1))
            break
    match = _EPISODE.search(stem)
    if match:
        return folder_season or 1, int(match.group(1)), _clean_title(stem[match.end():])
    if folder_season is not None:
        match = _NUMBER.search(stem)
        if match:
            return folder_season, int(match.group(1)), _clean_title(stem[match.end():].lstrip('. '))
    return None


def detect(entries):
    """Episodes of the playable files; files without numbers are numbered in name order."""
    videos = sorted((e for e in entries if e.get('supported') and not e.get('pad')), key=lambda e: e['name'].lower())
    episodes, unknown, used = [], [], set()
    for entry in videos:
        parsed = parse_episode(entry['name'])
        if parsed and 1 <= parsed[0] <= MAX_SEASON and 1 <= parsed[1] <= MAX_EPISODE and parsed[:2] not in used:
            used.add(parsed[:2])
            episodes.append(dict(file_id=entry['id'], season=parsed[0], episode=parsed[1], title=parsed[2]))
        else:
            unknown.append(entry)
    season = max((e['season'] for e in episodes), default=1)
    number = max((e['episode'] for e in episodes if e['season'] == season), default=0)
    for entry in unknown:
        number += 1
        episodes.append(dict(file_id=entry['id'], season=season, episode=number, title=''))
    return ordered(episodes)


def ordered(episodes):
    return sorted(episodes, key=lambda e: (e['season'], e['episode']))


def validate(episodes, entries):
    """Clean administrator input; raises ValueError with a message for the form."""
    playable = {e['id'] for e in entries if e.get('supported')}
    seen, files, result = set(), set(), []
    for item in episodes:
        try:
            file_id, season, number = int(item['file_id']), int(item['season']), int(item['episode'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Проверьте номера сезонов и серий.')
        if file_id not in playable or file_id in files:
            raise ValueError('В списке серий есть файл, которого нет в раздаче.')
        if not (1 <= season <= MAX_SEASON and 1 <= number <= MAX_EPISODE):
            raise ValueError(f'Сезон должен быть от 1 до {MAX_SEASON}, серия — от 1 до {MAX_EPISODE}.')
        if (season, number) in seen:
            raise ValueError(f'Серия {number} сезона {season} указана дважды.')
        seen.add((season, number))
        files.add(file_id)
        result.append(dict(file_id=file_id, season=season, episode=number, title=str(item.get('title') or '').strip()[:120]))
    if not result:
        raise ValueError('Отметьте хотя бы одну серию.')
    return ordered(result)


def seasons(episodes):
    """OrderedDict season -> episodes, for templates."""
    result = OrderedDict()
    for item in ordered(episodes):
        result.setdefault(item['season'], []).append(item)
    return result


def label(item, short=False):
    """'S1 · E3' / '1 сезон, 3 серия · Title'."""
    if short:
        return f"S{item['season']} · E{item['episode']}"
    text = f"{item['season']} сезон, {item['episode']} серия"
    return f"{text} · {item['title']}" if item.get('title') else text


def find(episodes, file_id):
    return next((e for e in episodes if e['file_id'] == file_id), None)


def neighbour(episodes, file_id, step=1):
    items = ordered(episodes)
    for index, item in enumerate(items):
        if item['file_id'] == file_id:
            other = index + step
            return items[other] if 0 <= other < len(items) else None
    return None
