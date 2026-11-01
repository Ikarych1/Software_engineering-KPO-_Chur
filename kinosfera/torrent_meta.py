"""Inspection and validation of .torrent metadata and magnet links.

Works without libtorrent (pure-Python bencode) so the library and tests do not
depend on the native engine. When libtorrent is installed it is preferred for the
file list, so file indices always match what the engine will use.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit

import bencode

MAX_TORRENT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 ** 3
MAX_FILES = 5000
MAX_PIECE_BYTES = 32 * 1024 ** 2
SUPPORTED = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/mp4', '.webm': 'video/webm',
             '.avi': 'video/x-msvideo', '.mkv': 'video/x-matroska', '.ts': 'video/mp2t', '.m2ts': 'video/mp2t'}
BROWSER_CONTAINERS = {'.mp4', '.m4v', '.mov', '.webm'}
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}

# Public trackers appended to every source: more peer discovery = faster start.
DEFAULT_TRACKERS = (
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.demonii.com:1337/announce',
    'udp://open.stealth.si:80/announce',
    'udp://tracker.torrent.eu.org:451/announce',
    'udp://exodus.desync.com:6969/announce',
    'udp://tracker.dler.org:6969/announce',
    'udp://explodie.org:6969/announce',
    'https://tracker.tamersunion.org:443/announce',
)


class TorrentMeta(dict):
    """Plain dict with attribute access: name, info_hash, piece_length, files, trackers…"""
    __getattr__ = dict.get


def _component(value):
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Торрент содержит пути не в UTF-8.') from exc
    if (not isinstance(value, str) or value in ('', '.', '..')
            or any(ch in value for ch in ('/', '\\', ':', '\x00')) or value.rstrip(' .') != value
            or value.split('.', 1)[0].upper() in RESERVED):
        raise ValueError('Торрент содержит небезопасные пути.')
    return value


def _entry(file_id, name, size, offset, pad=False):
    suffix = PurePosixPath(name).suffix.lower()
    return dict(id=file_id, name=name, size=size, offset=offset, pad=pad,
                mime=SUPPORTED.get(suffix, mimetypes.guess_type(name)[0] or 'application/octet-stream'),
                supported=suffix in SUPPORTED and not pad,
                requires_transcode=suffix not in BROWSER_CONTAINERS)


def _raw_info(data):
    """Return the exact bytes of the info dictionary (its SHA-1 is the v1 info-hash)."""
    if not data.startswith(b'd'):
        raise ValueError('Не удалось прочитать .torrent-файл.')
    i = 1
    view = memoryview(data)
    while i < len(data) and data[i:i + 1] != b'e':
        key, i = bencode._decode(view, i, 0)
        start = i
        _, i = bencode._decode(view, i, 0)
        if key == b'info':
            return data[start:i]
    raise ValueError('В .torrent-файле нет раздела info.')


def parse_torrent(data: bytes) -> TorrentMeta:
    if not data or len(data) > MAX_TORRENT_BYTES:
        raise ValueError('Выберите .torrent-файл размером не более 2 МБ.')
    try:
        root = bencode.decode(data)
        raw = _raw_info(data)
    except bencode.BencodeError as exc:
        raise ValueError('Не удалось прочитать .torrent-файл.') from exc
    info = root.get(b'info') if isinstance(root, dict) else None
    if not isinstance(info, dict):
        raise ValueError('В .torrent-файле нет раздела info.')
    piece_length = info.get(b'piece length')
    if not isinstance(piece_length, int) or not 16 * 1024 <= piece_length <= MAX_PIECE_BYTES:
        raise ValueError('Размер части торрента должен быть от 16 КБ до 32 МБ.')
    name = _component(info.get(b'name.utf-8', info.get(b'name', b'')))
    for key in (b'attr',):
        if b'l' in info.get(key, b''):
            raise ValueError('Символические ссылки в торренте не поддерживаются.')
    files = []
    if b'files' in info:
        offset = 0
        if not isinstance(info[b'files'], list):
            raise ValueError('Некорректный список файлов торрента.')
        for index, item in enumerate(info[b'files']):
            if not isinstance(item, dict) or not isinstance(item.get(b'length'), int) or item[b'length'] < 0:
                raise ValueError('Некорректный список файлов торрента.')
            attr = item.get(b'attr', b'')
            if b'l' in attr or b'symlink path' in item:
                raise ValueError('Символические ссылки в торренте не поддерживаются.')
            parts = item.get(b'path.utf-8', item.get(b'path'))
            if not isinstance(parts, list) or not parts:
                raise ValueError('Некорректный путь в торренте.')
            path = '/'.join([name] + [_component(part) for part in parts])
            files.append(_entry(index, path, item[b'length'], offset, pad=b'p' in attr))
            offset += item[b'length']
    elif isinstance(info.get(b'length'), int):
        files.append(_entry(0, name, info[b'length'], 0))
    elif b'file tree' in info:
        raise ValueError('Торренты только версии 2 поддерживаются при установленном libtorrent.')
    else:
        raise ValueError('В торренте нет файлов.')
    total = sum(f['size'] for f in files)
    if not 0 < len(files) <= MAX_FILES or not 0 < total <= MAX_TOTAL_BYTES:
        raise ValueError('Торрент должен содержать до 5000 файлов и до 200 ГБ данных.')
    pieces = info.get(b'pieces', b'')
    num_pieces = (total + piece_length - 1) // piece_length
    if not isinstance(pieces, bytes) or len(pieces) != num_pieces * 20:
        raise ValueError('Хеши частей торрента повреждены.')
    trackers = []
    if isinstance(root.get(b'announce'), bytes):
        trackers.append(root[b'announce'].decode('utf-8', 'replace'))
    for tier in root.get(b'announce-list', []) if isinstance(root.get(b'announce-list'), list) else []:
        for url in tier if isinstance(tier, list) else []:
            if isinstance(url, bytes):
                trackers.append(url.decode('utf-8', 'replace'))
    return TorrentMeta(name=name, info_hash=hashlib.sha1(raw).hexdigest(), piece_length=piece_length,
                       num_pieces=num_pieces, total_size=total, all_files=files, piece_hashes=pieces,
                       files=[f for f in files if f['size'] > 0 and not f['pad']],
                       trackers=list(dict.fromkeys(trackers)), raw_info=raw)


def inspect_with_libtorrent(data):
    """File list exactly as libtorrent indexes it (handles v2/hybrid torrents)."""
    import libtorrent as lt
    info = lt.torrent_info(lt.bdecode(data))
    fs = info.files()
    result = []
    for file_id in range(fs.num_files()):
        flags = fs.file_flags(file_id)
        if flags & lt.file_storage.flag_symlink:
            raise ValueError('Символические ссылки в торренте не поддерживаются.')
        name = fs.file_path(file_id).replace('\\', '/')
        if not name or ':' in name or '\x00' in name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')):
            raise ValueError('Торрент содержит небезопасные пути.')
        for part in name.split('/'):
            _component(part)
        if flags & lt.file_storage.flag_pad_file or fs.file_size(file_id) <= 0:
            continue
        result.append(_entry(file_id, name, fs.file_size(file_id), fs.file_offset(file_id)))
    return result


def inspect_torrent(data):
    """Return (files, best_file_id) for the admin library. Raises ValueError for bad input."""
    meta = parse_torrent(data) if not _is_v2_only(data) else None
    files = None
    try:
        files = inspect_with_libtorrent(data)
    except (ImportError, AttributeError):   # no (or a broken) native module: use the pure-Python parser
        if meta is None:
            raise ValueError('Для торрентов версии 2 установите libtorrent.')
    except (RuntimeError, TypeError, KeyError) as exc:
        raise ValueError('Не удалось прочитать .torrent-файл.') from exc
    if files is None:
        files = meta.files
    playable = [entry for entry in files if entry['supported']]
    if not playable:
        raise ValueError('В торренте нет видео MP4, MOV, WebM, AVI, MKV или TS.')
    return files, max(playable, key=lambda entry: entry['size'])['id']


def _is_v2_only(data):
    try:
        root = bencode.decode(data)
    except bencode.BencodeError:
        return False
    info = root.get(b'info', {}) if isinstance(root, dict) else {}
    return isinstance(info, dict) and b'file tree' in info and b'pieces' not in info


def info_hash(data):
    return hashlib.sha1(_raw_info(data)).hexdigest()


def build_torrent(raw_info: bytes, trackers=()):
    """Assemble a .torrent from an exact info dict (hash preserved) and tracker list."""
    trackers = [t for t in dict.fromkeys(trackers) if t]
    parts = [b'd']
    if trackers:
        parts.append(bencode.encode(b'announce') + bencode.encode(trackers[0]))
        parts.append(bencode.encode(b'announce-list') + bencode.encode([[t] for t in trackers]))
    parts.append(bencode.encode(b'info') + raw_info + b'e')
    return b''.join(parts)


def validate_magnet(value: str) -> str:
    value = (value or '').strip()
    if len(value) > 16384:
        raise ValueError('Magnet-ссылка слишком длинная.')
    parsed = urlsplit(value)
    if parsed.scheme.lower() != 'magnet' or parsed.netloc or parsed.path or parsed.fragment:
        raise ValueError('Укажите magnet-ссылку вида magnet:?xt=urn:btih:…')
    query = parse_qs(parsed.query, keep_blank_values=True)
    hashes = query.get('xt', [])
    if not hashes or not all(re.fullmatch(r'urn:btih:(?:[a-fA-F0-9]{40}|[a-zA-Z2-7]{32})|urn:btmh:1220[a-fA-F0-9]{64}', h) for h in hashes):
        raise ValueError('В magnet-ссылке отсутствует корректный BitTorrent info hash.')
    # These extensions can cause remote URL fetches or force arbitrary peer addresses.
    if any(key in query for key in ('xs', 'as', 'ws', 'x.pe')):
        raise ValueError('Ссылки на внешние файлы внутри magnet не поддерживаются.')
    trackers = query.get('tr', [])
    if len(trackers) > 50:
        raise ValueError('Слишком много трекеров в magnet-ссылке.')
    for tracker in trackers:
        url = urlsplit(tracker)
        if url.scheme not in ('https', 'http', 'udp') or not url.hostname or url.username or url.password:
            raise ValueError('В magnet-ссылке указан неподдерживаемый трекер.')
    return value


def magnet_info(value: str):
    value = validate_magnet(value)
    query = parse_qs(urlsplit(value).query)
    digest = None
    for xt in query.get('xt', []):
        token = xt.rsplit(':', 1)[-1]
        if xt.startswith('urn:btih:'):
            digest = token.lower() if len(token) == 40 else base64.b32decode(token.upper()).hex()
            break
    return dict(info_hash=digest, name=(query.get('dn') or [''])[0][:255], trackers=query.get('tr', []))
