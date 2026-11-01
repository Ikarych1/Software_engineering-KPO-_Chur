"""Authenticated playback sessions over the shared streaming engine.

Endpoints (all JSON unless noted):
  POST /api/play/<movie_id>/start      {client_id, room?}   -> session state
  GET  /api/play/s/<sid>/status                              -> state + live stats
  POST /api/play/s/<sid>/heartbeat     {position, playing, buffering, duration}
  POST /api/play/s/<sid>/end
  GET  /api/play/s/<sid>/video          (HTTP Range, original bytes)
  GET  /api/play/s/<sid>/hls/index.m3u8, /hls/<n>.ts
  GET  /internal/media/<key>            (loopback FFmpeg/FFprobe only)
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import hmac
import ipaddress
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import threading
import time

from flask import Blueprint, Response, abort, current_app, g, jsonify, request, send_file

from accounts import require_csrf
import series
import torrent_hls
from torrent_hls import HlsService, INPUT_FORMATS, audio_label, playback_plan, run_probe, summarize
import torrent_meta
from torrent_stream import EngineConfig, StreamEngine, StreamError, parse_range

log = logging.getLogger('kinosfera.playback')
play_api = Blueprint('play_api', __name__)
MAX_PER_USER = 4
MAX_TOTAL = 48
PROBE_RETRY = 3.0          # seconds before a failed analysis may be retried


@dataclass
class Lease:
    id: str
    user_id: int
    client_id: str
    movie_id: int
    revision: str
    media_key: str
    file_id: int
    torrent: object
    room: str | None = None
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    position: float = 0.0
    playing: bool = False
    buffering: bool = False
    duration: float = 0.0
    viewed: bool = False
    last_progress: float = 0.0
    mode: str = ''
    episode: dict | None = None          # series: dict(file_id, season, episode, title)
    next_file_id: int | None = None


class Media:
    """Per media key (info-hash:file): probe once, share between every lease."""

    def __init__(self, key, file_name, size):
        self.key = key
        self.file_name = file_name
        self.size = size
        self.info = None
        self.plan = None
        self.error = None
        self.failed_at = 0.0
        self.state = 'preparing'
        self.thread = None


class Playback:
    def __init__(self, app, store, engine: StreamEngine, hls: HlsService, ttl=120):
        self.app = app
        self.store = store
        self.engine = engine
        self.hls = hls
        self.ttl = ttl
        self.internal_token = secrets.token_urlsafe(32)
        self.port = None
        self._lock = threading.RLock()
        self.leases: dict[str, Lease] = {}
        self.media: dict[str, Media] = {}
        self.torrents: dict[str, tuple] = {}    # media_key -> (torrent, file_id)
        self._closed = threading.Event()
        threading.Thread(target=self._expire, daemon=True, name='playback-leases').start()

    # ------------------------------------------------------------------ sources
    def _source(self, movie_id):
        row = self.store.film(movie_id)
        if not row or not row['torrent']:
            raise LookupError('Видео пока не добавлено в библиотеку.')
        return row

    def source_bytes(self, row, source):
        """The torrent of one source of a card: 0 is the card's own, others are added seasons."""
        if source == 0:
            return row['torrent']
        data = self.store.source_torrent(row['film'].id, source)
        if data is None:
            raise LookupError('Раздача этого сезона удалена.')
        return data

    @staticmethod
    def episode_of(row, file_id=None):
        """Series: the requested episode (or the first); films: None."""
        episodes = row.get('episodes') or []
        if row['film'].kind != 'series' or not episodes:
            if file_id is not None and file_id != row['file_id']:
                raise LookupError('Такой серии нет.')
            return None
        if file_id is None:
            return episodes[0]
        episode = series.find(episodes, file_id)
        if episode is None:
            raise LookupError('Такой серии нет в сериале.')
        return episode

    def _open(self, row, file_id=None):
        """Open the torrent of a film / an episode (combined id, see series.py) and its file."""
        source, file_id = series.split(row['file_id'] if file_id is None else file_id)
        torrent = self.engine.open(self.source_bytes(row, source), file_id)
        key = f"{torrent.key}:{file_id}"
        view = torrent.views[file_id]
        with self._lock:
            self.torrents[key] = (torrent, file_id)
            media = self.media.get(key)
            if media is None:
                media = self.media[key] = Media(key, view.name, view.size)
            if media.error is not None and time.monotonic() - media.failed_at > PROBE_RETRY:
                media.error, media.state = None, 'preparing'      # «Повторить» analyses the video again
            if media.info is None and media.error is None and (media.thread is None or not media.thread.is_alive()):
                cached = self.store.media_info(key)
                if cached:
                    self._apply_info(media, torrent, cached)
                else:
                    media.state = 'preparing'
                    media.thread = threading.Thread(target=self._probe, args=(media,), daemon=True, name='probe')
                    media.thread.start()
            elif media.info is not None:
                self.engine.set_bitrate(torrent, media.info.get('bitrate'))
        return torrent, key

    def _apply_info(self, media, torrent, info):
        media.info = info
        media.plan = playback_plan(info)
        media.state = 'ready'
        self.engine.set_bitrate(torrent, info.get('bitrate'))
        fmt = INPUT_FORMATS.get(info.get('container') or Path(media.file_name).suffix.lower(), 'matroska')
        video = info.get('video') or {}
        self.hls.register(media.key, info['duration'], fmt, height=video.get('height'), width=video.get('width'),
                          codec=video.get('codec'), audio_tracks=len(info.get('audio_tracks') or []) or 1)

    def _probe(self, media):
        with self._lock:
            torrent, _ = self.torrents.get(media.key, (None, None))
        if torrent is not None:     # the torrent must not close for idleness while FFprobe reads it
            self.engine.pin(torrent, 'probe-pin', 600)
        try:
            suffix = Path(media.file_name).suffix.lower()
            fmt = INPUT_FORMATS.get(suffix)
            if fmt is None:
                raise ValueError('Формат видео не поддерживается.')
            if not self.hls.ffprobe:
                raise RuntimeError('Установите FFmpeg и FFprobe (в PATH) и перезапустите сайт.')
            url, headers = self.media_url(media.key, 'probe')
            last_error = None
            for _attempt in range(3):
                try:
                    raw = run_probe(self.hls.ffprobe, url, fmt, headers)
                    info = summarize(raw, media.file_name, media.size)
                    break
                except (RuntimeError, ValueError) as exc:
                    last_error = exc
                    time.sleep(1)
            else:
                raise last_error
            self.store.save_media_info(media.key, info)
            with self._lock:
                torrent, _ = self.torrents.get(media.key, (None, None))
                if torrent is not None:
                    self._apply_info(media, torrent, info)
                else:
                    media.info, media.plan, media.state = info, playback_plan(info), 'ready'
        except Exception as exc:  # noqa: BLE001 - surfaced to the player
            log.warning('probe failed for %s: %s', media.key, exc)
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError, StreamError)) else 'Не удалось подготовить видео.'
            if 'Server returned' in message or 'Connection refused' in message:
                message = 'Не удалось прочитать начало видео из раздачи. Нажмите «Повторить».'
            media.error, media.failed_at, media.state = message, time.monotonic(), 'error'
        finally:
            if torrent is not None:
                self.engine.forget(torrent, 'probe-pin')

    def media_url(self, key, owner):
        port = self.port or int(os.environ.get('CINEMA_PORT', 5050))
        return (f'http://127.0.0.1:{port}/internal/media/{key.replace(":", "/")}?owner={owner}',
                f'X-Internal-Token: {self.internal_token}\r\n')

    # ------------------------------------------------------------------ leases
    def start(self, user_id, client_id, movie_id, room=None, file_id=None):
        if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', client_id):
            raise ValueError('Обновите страницу плеера.')
        row = self._source(movie_id)
        episode = self.episode_of(row, file_id)
        with self._lock:
            old = [l for l in self.leases.values() if l.user_id == user_id and l.client_id == client_id]
        for lease in old:
            self.end(lease.id)
        with self._lock:
            mine = sorted((l for l in self.leases.values() if l.user_id == user_id), key=lambda l: l.touched)
            while len(mine) >= MAX_PER_USER:
                self.end(mine.pop(0).id)    # the oldest tab of this user gives way
            if len(self.leases) >= MAX_TOTAL:
                raise RuntimeError('Сервер сейчас обслуживает слишком много зрителей. Повторите позже.')
        file_id = episode['file_id'] if episode else row['file_id']
        torrent, key = self._open(row, file_id)
        lease = Lease(secrets.token_hex(16), user_id, client_id, movie_id, row['revision'], key,
                      int(key.rsplit(':', 1)[1]), torrent, room)
        if episode:
            lease.episode = episode
            following = series.neighbour(row['episodes'], file_id)
            lease.next_file_id = following['file_id'] if following else None
            progress = self.store.episode_progress(user_id, movie_id).get(file_id)
        else:
            progress = self.store.progress(user_id, movie_id)
        if progress and not progress['completed'] and progress['position'] > 30 and not room:
            lease.position = progress['position']
        with self._lock:
            self.leases[lease.id] = lease
        self.engine.hint(torrent, lease.id, lease.file_id, 0)
        return self.state(lease)

    def get(self, sid, user_id=None):
        with self._lock:
            lease = self.leases.get(sid)
        if lease is None or (user_id is not None and lease.user_id != user_id):
            raise LookupError('Сеанс просмотра завершён. Запустите фильм снова.')
        return lease

    def torrent_for(self, lease):
        torrent = lease.torrent
        if torrent.closed:
            row = self._source(lease.movie_id)
            torrent, _ = self._open(row, lease.episode['file_id'] if lease.episode else lease.file_id)
            lease.torrent = torrent
        return torrent

    def state(self, lease, stats=True):
        media = self.media.get(lease.media_key)
        result = dict(session_id=lease.id, movie_id=lease.movie_id, state=media.state if media else 'preparing',
                      resume_position=round(lease.position, 1), room=lease.room,
                      file_id=lease.episode['file_id'] if lease.episode else lease.file_id,
                      episode=lease.episode, next_file_id=lease.next_file_id)
        if media and media.error:
            result.update(state='error', message=media.error)
        elif media and media.plan:
            info = media.info
            base = f'/api/play/s/{lease.id}'
            result.update(duration=info['duration'], plan=media.plan,
                          direct_url=base + '/video', hls_url=base + '/hls/index.m3u8', hls_base=base + '/hls',
                          **self.variants(media),
                          media=dict(width=info['video'].get('width'), height=info['video'].get('height'),
                                     video=info['video'].get('codec'), audio=(info.get('audio') or {}).get('codec'),
                                     size=info['size'], bitrate=info['bitrate']))
            result['message'] = 'Видео готово.'
        else:
            result['message'] = 'Подключаемся к участникам раздачи и читаем начало фильма…'
        if stats:
            result['stats'] = self.stats(lease)
        return result

    def variants(self, media):
        """Quality and audio choices for the player menu."""
        info = media.info
        source = (info.get('video') or {}).get('height') or 0
        heights = self.hls.heights(media.key) if media.key in self.hls.media else []
        qualities = [dict(id=str(h), height=h, label=f'{h}p') for h in heights]
        if media.plan and media.plan.get('mode') == 'direct':
            qualities.insert(0, dict(id='source', height=source, label=f'Оригинал · {source}p' if source else 'Оригинал'))
        tracks = info.get('audio_tracks') or []
        audio = [dict(index=i, label=audio_label(t, i), language=(t.get('language') or '').lower(), default=bool(t.get('default')))
                 for i, t in enumerate(tracks)]
        default_audio = next((t['index'] for t in audio if t['default']), 0)
        default_height = self.hls.default_height(media.key) if heights else None
        return dict(qualities=qualities, audio_tracks=audio, default_audio=default_audio,
                    default_quality='source' if qualities and qualities[0]['id'] == 'source' else str(default_height or ''))

    def stats(self, lease):
        torrent = lease.torrent
        data = self.engine.stats(torrent, lease.file_id)
        media = self.media.get(lease.media_key)
        bitrate = (media.info or {}).get('bitrate') if media and media.info else None
        bitrate = bitrate or torrent.bitrate
        position_bytes = int(lease.position * bitrate)
        ahead = self.engine.buffered_ahead(torrent, lease.file_id, position_bytes)
        data['buffer_seconds'] = round(ahead / max(1, bitrate), 1)
        data['buffer_bytes'] = ahead
        return data

    def heartbeat(self, lease, data):
        lease.touched = time.monotonic()
        now = time.monotonic()
        position = data.get('position')
        if isinstance(position, (int, float)) and 0 <= position < 12 * 3600:
            lease.position = float(position)
        if isinstance(data.get('playing'), bool):
            lease.playing = data['playing']
        if isinstance(data.get('buffering'), bool):
            lease.buffering = data['buffering']
        if isinstance(data.get('mode'), str):
            lease.mode = data['mode'][:12]
        duration = data.get('duration')
        if isinstance(duration, (int, float)) and 0 < duration < 12 * 3600:
            lease.duration = float(duration)
        media = self.media.get(lease.media_key)
        torrent = self.torrent_for(lease)
        bitrate = (media.info or {}).get('bitrate') if media and media.info else torrent.bitrate
        self.engine.hint(torrent, lease.id, lease.file_id, int(lease.position * (bitrate or torrent.bitrate)))
        if lease.playing and lease.position > 5 and not lease.viewed:
            lease.viewed = True
            self.store.add_view(lease.user_id, lease.movie_id, lease.room)
        if lease.duration and (now - lease.last_progress > 8 or not lease.playing):
            delta = min(30.0, now - lease.last_progress) if lease.playing and lease.last_progress else 0.0
            lease.last_progress = now
            completed = self.store.save_progress(lease.user_id, lease.movie_id, lease.position, lease.duration, delta,
                                                 file_id=lease.episode['file_id'] if lease.episode else None,
                                                 next_file_id=lease.next_file_id)
            if completed:
                self.store.update_state(lease.user_id, lambda s: s['watched'].append(lease.movie_id)
                                        if lease.movie_id not in s.get('watched', []) else None)
        return self.state(lease)

    def end(self, sid):
        with self._lock:
            lease = self.leases.pop(sid, None)
        if lease is not None:
            self.engine.forget(lease.torrent, lease.id)

    def end_user(self, user_id):
        with self._lock:
            ids = [l.id for l in self.leases.values() if l.user_id == user_id]
        for sid in ids:
            self.end(sid)

    def end_movie(self, movie_id):
        with self._lock:
            ids = [l.id for l in self.leases.values() if l.movie_id == movie_id]
        for sid in ids:
            self.end(sid)

    def prewarm(self, movie_id, owner, seconds=600, file_id=None):
        """Connect to peers and fetch the head/tail before anyone presses play."""
        try:
            row = self._source(movie_id)
            episode = self.episode_of(row, file_id)
            torrent, _ = self._open(row, episode['file_id'] if episode else None)
            self.engine.pin(torrent, owner, seconds)
            return True
        except (LookupError, StreamError, ValueError, RuntimeError) as exc:
            log.info('prewarm skipped: %s', exc)
            return False

    def make_clip(self, movie_id, start, length, target, file_id=None, height=720, timeout=900):
        """Cut a short muted H.264 loop from the film itself (hero slider). Blocking: run in a thread."""
        row = self._source(movie_id)
        episode = self.episode_of(row, file_id)
        owner = 'clip-' + secrets.token_hex(4)
        torrent, key = self._open(row, episode['file_id'] if episode else None)
        self.engine.pin(torrent, owner, timeout)
        try:
            deadline = time.monotonic() + 240
            media = self.media[key]
            while media.info is None and media.error is None and time.monotonic() < deadline:
                time.sleep(0.5)
            if media.error:
                raise RuntimeError(media.error)
            if media.info is None:
                raise RuntimeError('Видео ещё не готово: участники раздачи не найдены.')
            duration = media.info['duration']
            if start >= duration:
                raise ValueError(f'Фильм длится {int(duration // 60)} мин — выберите более раннее начало.')
            length = min(length, duration - start)
            fmt = INPUT_FORMATS.get(media.info.get('container') or Path(media.file_name).suffix.lower(), 'matroska')
            url, headers = self.media_url(key, owner)
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix('.part.mp4')
            args = [self.hls.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-rw_timeout', '120000000', '-protocol_whitelist', 'http,tcp', '-headers', headers,
                    '-ss', f'{start:.3f}', '-f', fmt, '-i', url, '-t', f'{length:.3f}',
                    '-map', '0:v:0', '-an', '-sn', '-dn',
                    '-vf', f"scale=w=-2:h='min({height},ih)':flags=bicubic,fps=30,format=yuv420p",
                    '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '4', '-crf', '24', '-profile:v', 'high',
                    '-movflags', '+faststart', str(partial)]
            if not self.hls.ffmpeg:
                raise RuntimeError('Установите FFmpeg, чтобы вырезать видео из фильма.')
            try:
                process = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired as exc:
                partial.unlink(missing_ok=True)
                raise RuntimeError('Фрагмент готовился слишком долго: раздача медленная.') from exc
            if process.returncode or not partial.is_file() or partial.stat().st_size < 1024:
                partial.unlink(missing_ok=True)
                detail = process.stderr.decode('utf-8', 'replace').strip()[-300:]
                raise RuntimeError('FFmpeg не смог вырезать фрагмент. ' + detail)
            os.replace(partial, target)
            return dict(length=round(length, 1), size=target.stat().st_size)
        finally:
            self.engine.forget(torrent, owner)

    def sessions(self):
        with self._lock:
            leases = list(self.leases.values())
        users = {u['id']: u['username'] for u in self.store.users()} if leases else {}
        return [dict(id=l.id[:8], user=users.get(l.user_id, l.user_id), movie_id=l.movie_id, position=round(l.position),
                     playing=l.playing, buffering=l.buffering, room=l.room, mode=l.mode,
                     age=int(time.monotonic() - l.created)) for l in leases]

    def _expire(self):
        while not self._closed.wait(5):
            now = time.monotonic()
            with self._lock:
                stale = [l.id for l in self.leases.values() if now - l.touched > self.ttl]
            for sid in stale:
                self.end(sid)

    def close(self):
        self._closed.set()
        with self._lock:
            ids = list(self.leases)
        for sid in ids:
            self.end(sid)
        self.hls.close()
        self.engine.close()


def init_playback(app):
    root = Path(app.root_path)
    engine = StreamEngine(EngineConfig.from_env(root / '.torrent-cache'),
                          lt_module=app.config.get('LIBTORRENT_MODULE'))
    playback = None

    def media_url(key, job_id):
        return playback.media_url(key, f'hls-{job_id}')
    hls = HlsService(engine.config.cache_dir / 'hls', media_url)
    playback = Playback(app, app.extensions['store'], engine, hls)
    app.extensions['playback'] = playback
    app.extensions['playback_hub'] = playback     # name kept for older imports
    app.register_blueprint(play_api)
    atexit.register(playback.close)
    return playback


# ---------------------------------------------------------------------- HTTP
def _pb() -> Playback:
    return current_app.extensions['playback']


@play_api.errorhandler(ValueError)
def _bad(error):
    return jsonify(error=str(error)), 400


@play_api.errorhandler(LookupError)
def _missing(error):
    return jsonify(error=str(error)), 404


@play_api.errorhandler(StreamError)
@play_api.errorhandler(RuntimeError)
def _unavailable(error):
    response = jsonify(error=str(error))
    response.status_code = 503
    response.headers['Retry-After'] = '2'
    return response


@play_api.before_request
def _guard():
    if request.endpoint == 'play_api.internal_media':
        return None
    if not g.user:
        return jsonify(error='Войдите в аккаунт.'), 401
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        abort(403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        require_csrf()
    request.max_content_length = 16 * 1024
    return None


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Некорректный запрос плеера.')
    return data


@play_api.post('/api/play/<int:movie_id>/start')
def start(movie_id):
    data = _payload()
    room = data.get('room') if isinstance(data.get('room'), str) and re.fullmatch(r'[A-Z0-9]{4,12}', data.get('room', '')) else None
    episode = data.get('episode')
    if episode is not None and (type(episode) is not int or episode < 0):
        raise ValueError('Некорректная серия.')
    return jsonify(_pb().start(g.user.id, data.get('client_id'), movie_id, room, episode))


@play_api.get('/api/play/s/<sid>/status')
def status(sid):
    response = jsonify(_pb().state(_pb().get(sid, g.user.id)))
    response.headers['Cache-Control'] = 'no-store'
    return response


@play_api.post('/api/play/s/<sid>/heartbeat')
def heartbeat(sid):
    return jsonify(_pb().heartbeat(_pb().get(sid, g.user.id), _payload()))


@play_api.post('/api/play/s/<sid>/end')
def end(sid):
    lease = _pb().get(sid, g.user.id)
    data = request.get_json(silent=True) or {}
    if isinstance(data, dict) and data.get('position') is not None:
        try:
            _pb().heartbeat(lease, dict(data, playing=False))
        except (LookupError, StreamError, ValueError):
            pass
    _pb().end(sid)
    return jsonify(ok=True)


def _range_response(torrent, file_id, owner, engine, mime, name):
    view = torrent.views[file_id]
    size = view.size
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
    try:
        start_byte, end_byte = parse_range(request.headers.get('Range'), size)
    except ValueError:
        headers['Content-Range'] = f'bytes */{size}'
        return Response(status=416, headers=headers)
    partial = request.headers.get('Range') is not None
    if partial:
        headers['Content-Range'] = f'bytes {start_byte}-{end_byte}/{size}'
    headers['Content-Length'] = str(end_byte - start_byte + 1)
    if request.method == 'HEAD':
        return Response(status=206 if partial else 200, headers=headers, mimetype=mime)
    chunks = engine.read(torrent, file_id, start_byte, end_byte, owner)
    try:
        first = next(chunks)
    except StopIteration:
        raise StreamError('Не удалось получить начало видеопотока.')

    def body():
        try:
            yield first
            yield from chunks
        except StreamError:
            return      # headers are sent: truncate; the client re-requests the range
        finally:
            chunks.close()
    return Response(body(), status=206 if partial else 200, headers=headers, mimetype=mime, direct_passthrough=True)


@play_api.route('/api/play/s/<sid>/video', methods=['GET', 'HEAD'])
def video(sid):
    pb = _pb()
    lease = pb.get(sid, g.user.id)
    lease.touched = time.monotonic()
    media = pb.media.get(lease.media_key)
    mime = (media.plan or {}).get('mime', 'video/mp4') if media and media.plan else 'video/mp4'
    torrent = pb.torrent_for(lease)
    return _range_response(torrent, lease.file_id, lease.id, pb.engine, mime, PurePosixPath(torrent.views[lease.file_id].name).name)


@play_api.route('/api/play/s/<sid>/hls/index.m3u8', methods=['GET', 'HEAD'])
def hls_playlist(sid):
    pb = _pb()
    lease = pb.get(sid, g.user.id)
    lease.touched = time.monotonic()
    pb.torrent_for(lease)
    text = pb.hls.playlist(lease.media_key, uri='{index}.ts')
    response = Response(text if request.method == 'GET' else b'', mimetype='application/vnd.apple.mpegurl')
    response.headers['Cache-Control'] = 'no-store'
    return response


@play_api.route('/api/play/s/<sid>/hls/<int:height>-<int:audio>/index.m3u8', methods=['GET', 'HEAD'])
def hls_variant_playlist(sid, height, audio):
    pb = _pb()
    lease = pb.get(sid, g.user.id)
    lease.touched = time.monotonic()
    pb.torrent_for(lease)
    pb.hls.stream_key(lease.media_key, height, audio)      # 404 for a quality / track the file lacks
    text = pb.hls.playlist(lease.media_key, uri='{index}.ts')
    response = Response(text if request.method == 'GET' else b'', mimetype='application/vnd.apple.mpegurl')
    response.headers['Cache-Control'] = 'no-store'
    return response


@play_api.route('/api/play/s/<sid>/hls/<int:height>-<int:audio>/<int:index>.ts', methods=['GET', 'HEAD'])
def hls_variant_segment(sid, height, audio, index):
    return hls_segment(sid, index, height, audio)


@play_api.route('/api/play/s/<sid>/hls/<int:index>.ts', methods=['GET', 'HEAD'])
def hls_segment(sid, index, height=None, audio=0):
    pb = _pb()
    lease = pb.get(sid, g.user.id)
    lease.touched = time.monotonic()
    pb.torrent_for(lease)
    path = pb.hls.segment(lease.media_key, index, height=height, audio=audio)
    response = send_file(path, mimetype='video/mp2t', conditional=False, max_age=3600)
    response.headers['Cache-Control'] = 'private, max-age=3600'
    return response


@play_api.route('/internal/media/<key>/<int:file_id>', methods=['GET', 'HEAD'])
def internal_media(key, file_id):
    pb = _pb()
    try:
        loopback = ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        loopback = False
    supplied = request.headers.get('X-Internal-Token', '')
    if not loopback or not hmac.compare_digest(supplied, pb.internal_token):
        abort(403)
    media_key = f'{key}:{file_id}'
    with pb._lock:
        entry = pb.torrents.get(media_key)
    if entry is None:
        abort(404)
    torrent, file_id = entry
    if torrent.closed:
        # The torrent was stopped and opened again (idle, admin, cache cleanup): use the live one.
        live = pb.engine.torrents.get(torrent.key)
        if live is None or live.closed or file_id not in live.views:
            abort(410)
        torrent = live
        with pb._lock:
            pb.torrents[media_key] = (live, file_id)
    owner = re.sub(r'[^A-Za-z0-9_-]', '', request.args.get('owner', 'internal'))[:40] or 'internal'
    view = torrent.views[file_id]
    return _range_response(torrent, file_id, owner, pb.engine, view.mime, PurePosixPath(view.name).name)
