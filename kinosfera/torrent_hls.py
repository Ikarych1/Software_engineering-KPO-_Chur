"""Media analysis and adaptive delivery of torrent video to the browser.

* ``probe()`` runs FFprobe once per (info-hash, file) and the result is cached in
  SQLite, so later sessions start instantly.
* ``playback_plan()`` decides how the browser receives the film:
  - ``direct``: the original bytes over HTTP Range (MP4/WebM with browser codecs) —
    no CPU cost, native seeking, the fastest start;
  - ``direct`` with ``fallback`` (MKV with H.264/AAC etc.): Chromium plays Matroska
    natively; any other browser or a decode error falls back to HLS;
  - ``hls``: on-the-fly H.264/AAC transcoding.
* HLS uses **one continuous FFmpeg process per film**, shared by every viewer and
  watch-party room, writing 6-second segments with keyframes forced on an exact
  6-second grid. Far seeks restart the job at the requested segment; segments
  already produced are reused by everyone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

log = logging.getLogger('kinosfera.media')
SEGMENT_SECONDS = 6
MAX_DURATION = 12 * 60 * 60
INPUT_FORMATS = {'.avi': 'avi', '.mkv': 'matroska', '.webm': 'matroska', '.mp4': 'mov', '.m4v': 'mov',
                 '.mov': 'mov', '.ts': 'mpegts', '.m2ts': 'mpegts'}
H264_PROFILES = {'Baseline': '42E0', 'Constrained Baseline': '42E0', 'Main': '4D40', 'High': '6400'}
QUALITY_HEIGHTS = (1080, 720, 480, 360)          # transcoded variants offered to the viewer
MAX_RATES = {1080: 8000, 720: 5000, 480: 2500, 360: 1200}   # kbit/s video cap per height
LANGUAGES = {'rus': 'Русский', 'ru': 'Русский', 'eng': 'English', 'en': 'English', 'ukr': 'Українська', 'uk': 'Українська',
             'jpn': '日本語', 'ja': '日本語', 'ger': 'Deutsch', 'deu': 'Deutsch', 'fre': 'Français', 'fra': 'Français',
             'spa': 'Español', 'ita': 'Italiano', 'kor': '한국어', 'chi': '中文', 'zho': '中文'}


def _duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Не удалось определить длительность видео.') from exc
    if not math.isfinite(value) or not 0 < value <= MAX_DURATION:
        raise ValueError('Длительность видео должна быть от 0 до 12 часов.')
    return value


def build_vod_playlist(duration, segment_seconds=SEGMENT_SECONDS, uri='segment/{index}.ts'):
    duration = _duration(duration)
    segment_seconds = float(segment_seconds)
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise ValueError('Некорректная длительность сегмента.')
    count = math.ceil(duration / segment_seconds - 1e-9)
    lines = ['#EXTM3U', '#EXT-X-VERSION:3',
             f'#EXT-X-TARGETDURATION:{math.ceil(min(segment_seconds, duration))}',
             '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-PLAYLIST-TYPE:VOD', '#EXT-X-INDEPENDENT-SEGMENTS']
    for index in range(count):
        length = min(segment_seconds, duration - index * segment_seconds)
        lines.extend((f'#EXTINF:{length:.6f},', uri.format(index=index)))
    lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines) + '\n'


def binary(name, env):
    return shutil.which(os.environ.get(env) or name)


GPU_CODECS = {'h264', 'hevc', 'vp9', 'av1'}      # decoded by the video card on current GPUs
ACCELS = ('amf', 'qsv', 'nvenc')                 # AMD, Intel, NVIDIA — probed in this order
CPU_THREADS = max(2, min(6, (os.cpu_count() or 4) // 4))
_ACCEL_CACHE: dict = {}
if os.name == 'nt':
    LOW_PRIORITY = dict(creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0x4000))
else:
    LOW_PRIORITY = dict(preexec_fn=lambda: os.nice(5))
_ACCEL_LOCK = threading.Lock()


def _even(value):
    return max(2, int(round(value / 2)) * 2)


def video_chain(profile, height, width_src, height_src, preset='veryfast', rate=5000, threads=CPU_THREADS):
    """FFmpeg arguments (before -i, after -i) that turn the video into H.264 of ``height``.

    GPU profiles keep 4K frames in video memory from decoding to encoding: the processor
    and RAM only see the small encoded stream. Software is limited to a few threads (and runs
    with a lower priority, see ``_start``), so it never takes the whole machine."""
    h = min(height, height_src) if height_src else height
    w = _even(width_src * h / height_src) if width_src and height_src else None
    bitrate = ['-b:v', f'{rate * 3 // 4}k', '-maxrate', f'{rate}k', '-bufsize', f'{rate * 2}k', '-profile:v', 'high']
    if profile == 'amf' and w:
        return (['-hwaccel', 'amf', '-hwaccel_output_format', 'amf'],
                ['-vf', f'vpp_amf=w={w}:h={h}:format=nv12', '-c:v', 'h264_amf', '-quality', 'speed', '-rc', 'vbr_peak',
                 # forced key frames must become IDR, or segments would not start on the 6-second grid
                 '-forced_idr', '1', '-max_b_frames', '0', '-bf', '0', *bitrate])
    if profile == 'qsv' and w:
        return (['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv'],
                ['-vf', f'vpp_qsv=w={w}:h={h}:format=nv12', '-c:v', 'h264_qsv', '-preset', 'veryfast',
                 '-forced_idr', '1', '-bf', '0', *bitrate])
    if profile == 'nvenc':
        return (['-hwaccel', 'cuda', '-threads', '2'],
                ['-vf', f"scale=w=-2:h='min({height},ih)':flags=bilinear,format=nv12",
                 '-c:v', 'h264_nvenc', '-preset', 'p2', '-rc', 'vbr', '-forced-idr', '1', '-bf', '0', *bitrate])
    return (['-threads', str(threads), '-filter_threads', '2'],
            ['-vf', f"scale=w=-2:h='min({height},ih)':flags=bilinear,format=yuv420p",
             '-c:v', 'libx264', '-preset', preset, '-threads', str(threads), '-profile:v', 'high', '-crf', '21',
             '-maxrate', f'{rate}k', '-bufsize', f'{rate * 2}k'])


def detect_accel(ffmpeg, wanted='auto'):
    """The first hardware profile that really works on this computer (a 1-second test), else 'software'."""
    wanted = (wanted or 'auto').lower()
    if not ffmpeg or wanted in ('off', 'no', 'none', 'software', 'cpu', '0'):
        return 'software'
    candidates = ACCELS if wanted == 'auto' else tuple(a for a in ACCELS if a == wanted)
    key = (ffmpeg, candidates)
    with _ACCEL_LOCK:
        if key in _ACCEL_CACHE:
            return _ACCEL_CACHE[key]
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        sample = Path(tempfile.gettempdir()) / 'kinosfera-accel-test.mkv'     # shared, outside the cache
        result = 'software'
        try:
            if not sample.is_file():
                subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=25',
                                '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', str(sample)],
                               capture_output=True, timeout=60, creationflags=flags, check=True)
            for name in candidates:
                before, after = video_chain(name, 360, 1280, 720)
                run = subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', *before, '-i', str(sample), '-an', *after,
                                      '-force_key_frames', 'expr:gte(t,n_forced*0.5)', '-f', 'null', '-'],
                                     capture_output=True, timeout=60, creationflags=flags)
                if run.returncode == 0:
                    result = name
                    break
        except (OSError, subprocess.SubprocessError):
            result = 'software'
        log.info('Перекодирование видео: %s', {'amf': 'видеокарта AMD (AMF)', 'qsv': 'видеокарта Intel (Quick Sync)',
                                                'nvenc': 'видеокарта NVIDIA (NVENC)'}.get(result, 'процессор (libx264)'))
        _ACCEL_CACHE[key] = result
        return result


def summarize(probe_json, file_name, file_size):
    """Reduce raw FFprobe JSON to what the player and the catalog need."""
    streams = probe_json.get('streams', [])
    fmt = probe_json.get('format', {})
    video = next((s for s in streams if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')), None)
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    subs = [s for s in streams if s.get('codec_type') == 'subtitle']
    durations = [fmt.get('duration')] + [s.get('duration') for s in streams]
    duration = None
    for value in durations:
        try:
            duration = max(duration or 0, _duration(value))
        except ValueError:
            continue
    if duration is None:
        raise ValueError('Не удалось определить длительность видео.')
    if video is None:
        raise ValueError('В файле нет видеодорожки.')
    try:
        bitrate = int(fmt.get('bit_rate') or 0) / 8
    except ValueError:
        bitrate = 0
    if not bitrate and duration:
        bitrate = file_size / duration
    audio = audios[0] if audios else None
    return dict(
        duration=round(duration, 3), size=file_size, bitrate=int(bitrate), container=Path(file_name).suffix.lower(),
        video=dict(codec=video.get('codec_name'), profile=video.get('profile'), level=video.get('level'),
                   pix_fmt=video.get('pix_fmt'), width=video.get('width'), height=video.get('height')),
        audio=dict(codec=audio.get('codec_name'), channels=audio.get('channels'), profile=audio.get('profile')) if audio else None,
        audio_tracks=[dict(index=i, codec=a.get('codec_name'), language=(a.get('tags') or {}).get('language'),
                           title=((a.get('tags') or {}).get('title') or '')[:80], channels=a.get('channels'),
                           default=bool((a.get('disposition') or {}).get('default'))) for i, a in enumerate(audios)][:12],
        subtitles=[dict(codec=s.get('codec_name'), language=(s.get('tags') or {}).get('language')) for s in subs][:20],
    )


def audio_label(track, position=None):
    """'Русский · Дубляж · 5.1' from probe data."""
    parts = []
    language = (track.get('language') or '').lower()
    if language and language != 'und':
        parts.append(LANGUAGES.get(language, language.upper()))
    title = (track.get('title') or '').strip()
    if title and title.lower() not in (language, LANGUAGES.get(language, '').lower()):
        parts.append(title)
    channels = track.get('channels') or 0
    if channels:
        parts.append({1: 'моно', 2: 'стерео', 6: '5.1', 8: '7.1'}.get(channels, f'{channels} кан.'))
    if not parts or (len(parts) == 1 and channels):
        parts.insert(0, f'Дорожка {(position if position is not None else track.get("index", 0)) + 1}')
    return ' · '.join(parts)


def quality_heights(source_height):
    """Transcoded heights for a source: never upscale, always offer at least one."""
    source = int(source_height or 0)
    heights = [h for h in QUALITY_HEIGHTS if not source or h <= source]
    return heights or [QUALITY_HEIGHTS[-1]]


def codecs_string(info):
    video, audio = info['video'], info.get('audio')
    parts = []
    if video['codec'] == 'h264':
        prefix = H264_PROFILES.get(video.get('profile') or '', '6400')
        level = int(video.get('level') or 40)
        parts.append(f'avc1.{prefix}{level:02X}')
    elif video['codec'] in ('vp8', 'vp9', 'av1'):
        parts.append({'vp8': 'vp8', 'vp9': 'vp9', 'av1': 'av01.0.08M.08'}[video['codec']])
    if audio:
        parts.append({'aac': 'mp4a.40.2', 'mp3': 'mp4a.40.34', 'opus': 'opus', 'vorbis': 'vorbis', 'flac': 'flac'}.get(audio['codec'], audio['codec'] or ''))
    return ', '.join(p for p in parts if p)


def playback_plan(info):
    """Return {'mode': 'direct'|'hls', 'mime', 'codecs', 'fallback'} for the client."""
    video, audio = info['video'], info.get('audio')
    container = info['container']
    vcodec = video.get('codec')
    eight_bit = (video.get('pix_fmt') or 'yuv420p') in ('yuv420p', 'yuvj420p')
    acodec = audio['codec'] if audio else None
    stereo_ok = not audio or (audio.get('channels') or 2) <= 8
    if container in ('.mp4', '.m4v', '.mov') and ((vcodec == 'h264' and eight_bit) or vcodec in ('av1', 'vp9')) \
            and acodec in (None, 'aac', 'mp3', 'opus', 'flac') and stereo_ok:
        return dict(mode='direct', mime='video/mp4', codecs=codecs_string(info), fallback='hls')
    if container == '.webm' and vcodec in ('vp8', 'vp9', 'av1') and acodec in (None, 'opus', 'vorbis'):
        return dict(mode='direct', mime='video/webm', codecs=codecs_string(info), fallback='hls')
    if container == '.mkv' and ((vcodec == 'h264' and eight_bit) or vcodec in ('vp8', 'vp9', 'av1')) \
            and acodec in (None, 'aac', 'mp3', 'opus', 'vorbis', 'flac'):
        # Chromium's Matroska demuxer plays this when labelled as WebM; others use HLS.
        return dict(mode='direct', mime='video/webm', codecs=codecs_string(info), fallback='hls', chromium_only=True)
    return dict(mode='hls', mime='application/vnd.apple.mpegurl', codecs='avc1.640028, mp4a.40.2', fallback=None)


def run_probe(ffprobe, url, input_format, headers, timeout=120):
    args = [ffprobe, '-v', 'error', '-rw_timeout', '60000000', '-protocol_whitelist', 'http,tcp',
            '-probesize', '8000000', '-analyzeduration', '10000000', '-headers', headers,
            '-f', input_format, '-show_format', '-show_streams', '-of', 'json', url]
    process = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode:
        raise RuntimeError('FFprobe не смог прочитать начало видео: ' +
                           process.stderr.decode('utf-8', 'replace').strip()[-300:])
    return json.loads(process.stdout or b'{}')


@dataclass
class HlsJob:
    key: str                 # stream key: media@height a audio
    start: int
    directory: Path
    height: int = 720
    audio: int = 0
    profile: str = 'software'
    process: subprocess.Popen | None = None
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    started: float = field(default_factory=time.monotonic)
    requested: float = field(default_factory=time.monotonic)
    last_index: int = 0
    error: str | None = None
    stderr: list = field(default_factory=list)
    produced: int = -1          # last segment written in a row (cached: deleted old ones do not matter)
    killed: bool = False

    def __post_init__(self):
        self.produced = self.start - 1

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def frontier(self):
        """Last segment this job has written. Continues from the cached value, so segments the
        cleanup removed behind the viewer do not make a running job look idle (that used to start
        a second FFmpeg) and only one file is checked per call."""
        index = self.produced + 1
        while (self.directory / f'seg_{index}.ts').exists():
            index += 1
        self.produced = index - 1
        return self.produced


class HlsService:
    """Shared HLS transcoding jobs keyed by stream: media (info-hash + file) + height + audio track."""

    def __init__(self, root, url_for_media, max_jobs=None, height=None, preset=None, accel=None):
        self.root = Path(root)
        self.url_for_media = url_for_media     # callable(media_key, job_id) -> (url, headers)
        self.ffmpeg = binary('ffmpeg', 'FFMPEG_PATH')
        self.ffprobe = binary('ffprobe', 'FFPROBE_PATH')
        self.available = bool(self.ffmpeg and self.ffprobe)
        self.height = int(height or os.environ.get('CINEMA_HLS_HEIGHT', 720))
        self.preset = preset or os.environ.get('CINEMA_HLS_PRESET', 'veryfast')
        self.max_jobs = int(max_jobs or os.environ.get('CINEMA_HLS_MAX_JOBS', 3))
        self.accel = 'software'
        wanted = accel or os.environ.get('CINEMA_HWACCEL', 'auto')
        if self.ffmpeg and wanted.lower() not in ('off', 'software', 'cpu', 'none', '0'):
            def probe_accel():
                self.accel = detect_accel(self.ffmpeg, wanted)
            threading.Thread(target=probe_accel, daemon=True, name='hls-accel').start()
        self.jobs: dict[str, list[HlsJob]] = {}   # stream key -> jobs
        self.media: dict[str, dict] = {}       # media_key -> dict(duration, format, height, audio_tracks)
        self.served: dict[str, dict] = {}      # stream key -> {segment index: monotonic of last request}
        # Same switch as the torrent engine: by default only segments around viewers stay on disk.
        self.keep_all = os.environ.get('CINEMA_BACKGROUND_DOWNLOAD', '0') != '0'
        self.keep_behind = math.ceil(float(os.environ.get('CINEMA_KEEP_BEHIND_SECONDS', 30)) / SEGMENT_SECONDS)
        self.grace = float(os.environ.get('CINEMA_SEEK_GRACE_SECONDS', 10))
        # How far FFmpeg may transcode ahead of the viewer before it is stopped (and restarted later).
        self.ahead = max(10, int(float(os.environ.get('CINEMA_HLS_AHEAD_SECONDS', 240)) // SEGMENT_SECONDS))
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._janitor = threading.Thread(target=self._clean_loop, daemon=True, name='hls-janitor')
        self._janitor.start()

    def register(self, media_key, duration, input_format, height=None, audio_tracks=1, width=None, codec=None):
        with self._lock:
            previous = self.media.get(media_key) or {}
            self.media[media_key] = dict(duration=_duration(duration), format=input_format,
                                         height=int(height or 0), width=int(width or 0), codec=(codec or '').lower(),
                                         audio_tracks=max(1, int(audio_tracks or 1)),
                                         gpu_failed=previous.get('gpu_failed', False))

    def profile_for(self, media_key):
        """Hardware for codecs the video card decodes; software for the rest or after a GPU failure."""
        media = self.media[media_key]
        if self.accel == 'software' or media.get('gpu_failed') or media.get('codec') not in GPU_CODECS:
            return 'software'
        if self.accel in ('amf', 'qsv') and not (media.get('width') and media.get('height')):
            return 'software'
        return self.accel

    def heights(self, media_key):
        return quality_heights(self.media[media_key].get('height'))

    def default_height(self, media_key):
        heights = self.heights(media_key)
        return next((h for h in heights if h <= self.height), heights[-1])

    def stream_key(self, media_key, height=None, audio=0):
        """Validated key of one variant; LookupError for a quality or track the file does not have."""
        with self._lock:
            media = self.media.get(media_key)
            if media is None:
                raise LookupError('Видео ещё не подготовлено.')
            if height is None:
                height = self.default_height(media_key)
            elif height not in self.heights(media_key):
                raise LookupError('Такого качества у этого видео нет.')
            if not 0 <= int(audio) < media['audio_tracks']:
                raise LookupError('Такой звуковой дорожки нет.')
            return f'{media_key}@{height}a{int(audio)}'

    @staticmethod
    def parse_stream(stream):
        media_key, _, variant = stream.rpartition('@')
        height, _, audio = variant.partition('a')
        return media_key, int(height), int(audio)

    def playlist(self, media_key, uri='segment/{index}.ts'):
        with self._lock:
            media = self.media.get(media_key)
        if media is None:
            raise LookupError('Видео ещё не подготовлено.')
        return build_vod_playlist(media['duration'], uri=uri)

    def _dir(self, media_key):
        safe = re.sub(r'[^0-9a-zA-Z_-]', '_', media_key)
        return self.root / safe

    def segment_count(self, media_key):
        return math.ceil(self.media[media_key]['duration'] / SEGMENT_SECONDS - 1e-9)

    def segment(self, media_key, index, timeout=100.0, height=None, audio=0):
        """Return the path of a finished segment of one variant, starting/reusing a job as needed."""
        if not self.available:
            raise RuntimeError('Для этого формата установите FFmpeg и FFprobe и перезапустите сайт.')
        with self._lock:
            if self._closed:
                raise RuntimeError('Сервис видео остановлен.')
            if media_key not in self.media:
                raise LookupError('Видео ещё не подготовлено.')
            if not 0 <= index < self.segment_count(media_key):
                raise LookupError('Сегмент видео не найден.')
            stream = self.stream_key(media_key, height, audio)
            directory = self._dir(stream)
            path = directory / f'seg_{index}.ts'
            deadline = time.monotonic() + timeout
            job = None
            self.served.setdefault(stream, {})[index] = time.monotonic()
            while True:
                if path.exists():
                    for candidate in self.jobs.get(stream, []):
                        if candidate.start <= index <= candidate.frontier() + 1:
                            candidate.requested = time.monotonic()
                            candidate.last_index = max(candidate.last_index, index)
                    try:
                        os.utime(path)
                    except OSError:
                        pass
                    return path
                jobs = [j for j in self.jobs.get(stream, []) if j.alive()]
                self.jobs[stream] = jobs
                job = next((j for j in jobs if j.start <= index <= j.frontier() + 4), None)
                if job is None:
                    job = self._start(stream, index, directory)
                job.requested = time.monotonic()
                job.last_index = max(job.last_index, index)
                if job.error and not job.alive():
                    raise RuntimeError(job.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Сегмент готовится дольше обычного — данные ещё загружаются.')
                self._cond.wait(min(0.25, remaining))
                if not job.alive() and not path.exists():
                    if job.process is not None and job.process.returncode not in (0, None):
                        detail = b''.join(job.stderr).decode('utf-8', 'replace')[-300:]
                        jobs_left = [j for j in self.jobs.get(stream, []) if j is not job]
                        self.jobs[stream] = jobs_left
                        if job.profile != 'software' and not job.killed:
                            # The video card cannot handle this file: the same segment in software.
                            log.warning('GPU transcoding failed for %s, using the processor: %s', media_key, detail.strip()[-160:])
                            self.media[media_key]['gpu_failed'] = True
                            continue
                        raise RuntimeError('Не удалось подготовить фрагмент видео. ' + detail.strip())
                    if job.frontier() < index:
                        # Job ended before this segment (e.g. killed); try again with a new one.
                        self.jobs[stream] = [j for j in self.jobs.get(stream, []) if j is not job]

    def jobs_for(self, media_key):
        """Every job of a media, across its variants."""
        with self._lock:
            return [j for stream, jobs in self.jobs.items() if stream.rpartition('@')[0] == media_key for j in jobs]

    def ffmpeg_args(self, url, headers, input_format, index, height, audio, directory, job_id,
                    profile='software', width_src=0, height_src=0):
        start = index * SEGMENT_SECONDS
        rate = MAX_RATES.get(height, 5000)
        before, video = video_chain(profile, height, width_src, height_src, self.preset, rate)
        return [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-rw_timeout', '90000000', '-protocol_whitelist', 'http,tcp', '-headers', headers,
                '-probesize', '8000000', '-analyzeduration', '8000000', *before,
                '-ss', f'{start:.3f}', '-f', input_format, '-i', url,
                '-map', '0:v:0', '-map', f'0:a:{audio}?', '-sn', '-dn', *video,
                '-force_key_frames', f'expr:gte(t,n_forced*{SEGMENT_SECONDS})', '-sc_threshold', '0',
                '-c:a', 'aac', '-b:a', '160k', '-ac', '2', '-ar', '48000',
                '-output_ts_offset', f'{start:.3f}', '-muxdelay', '0', '-muxpreload', '0',
                '-f', 'hls', '-hls_time', str(SEGMENT_SECONDS), '-hls_list_size', '0',
                '-hls_flags', 'temp_file', '-start_number', str(index),
                '-hls_segment_filename', str(directory / 'seg_%d.ts'), str(directory / f'job_{job_id}.m3u8')]

    def _drop_job(self, job):
        self._kill(job)
        for values in self.jobs.values():
            if job in values:
                values.remove(job)

    def _start(self, stream, index, directory):
        media_key, height, audio = self.parse_stream(stream)
        now = time.monotonic()
        # The viewer seeked or switched quality / dubbing: jobs of this media nobody asked for lately stop now.
        for job in self.jobs_for(media_key):
            if now - job.requested > 8:
                self._drop_job(job)
        jobs = self.jobs.setdefault(stream, [])
        total = sum(len(v) for v in self.jobs.values())
        if len(jobs) >= 2 or total >= self.max_jobs:
            pool = jobs if len(jobs) >= 2 else [j for v in self.jobs.values() for j in v]
            self._drop_job(min(pool, key=lambda j: j.requested))
        directory.mkdir(parents=True, exist_ok=True)
        media = self.media[media_key]
        profile = self.profile_for(media_key)
        job = HlsJob(stream, index, directory, height, audio, profile)
        url, headers = self.url_for_media(media_key, job.id)
        args = self.ffmpeg_args(url, headers, media['format'], index, height, audio, directory, job.id,
                                profile, media.get('width'), media.get('height'))
        try:   # below-normal priority: the browser, the torrent engine and other apps come first
            job.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                           **LOW_PRIORITY)
        except OSError as exc:
            raise RuntimeError('Не удалось запустить FFmpeg. Проверьте установку.') from exc

        def drain():
            with job.process.stderr:
                for line in job.process.stderr:
                    job.stderr.append(line)
                    del job.stderr[:-10]
            job.process.wait()
            with self._lock:
                self._cond.notify_all()
        threading.Thread(target=drain, daemon=True).start()
        jobs.append(job)
        log.info('HLS job %s for %s (%sp, audio %s, %s) from segment %s', job.id, media_key, height, audio + 1, profile, index)
        return job

    def job_ids(self, media_key):
        return {j.id for j in self.jobs_for(media_key) if j.alive()}

    def _kill(self, job):
        """Stop FFmpeg without waiting under the lock: the drain thread reaps the process."""
        job.killed = True
        if job.process is not None and job.process.poll() is None:
            try:
                job.process.kill()
            except OSError:
                pass

    def forget(self, media_key, delete=False):
        with self._lock:
            streams = [s for s in set(self.jobs) | set(self.served) if s.rpartition('@')[0] == media_key]
            for stream in streams:
                for job in self.jobs.pop(stream, []):
                    self._kill(job)
                self.served.pop(stream, None)
            self.media.pop(media_key, None)
            self._cond.notify_all()
        if delete:
            for stream in streams:
                shutil.rmtree(self._dir(stream), ignore_errors=True)

    def _clean_loop(self):
        """Stop jobs nobody requests and keep the segment cache bounded."""
        limit = int(float(os.environ.get('CINEMA_HLS_CACHE_GB', 4)) * 1024 ** 3)
        while not self._closed:
            time.sleep(5)
            now = time.monotonic()
            with self._lock:
                for key, jobs in list(self.jobs.items()):
                    for job in list(jobs):
                        # Ran far ahead of every viewer or nobody asked for a while: stop it.
                        if job.alive() and (now - job.requested > 30 or job.frontier() - job.last_index > self.ahead):
                            self._kill(job)
                        if not job.alive() and now - job.requested > 5:
                            jobs.remove(job)
                    self._cond.notify_all()
            if not self.keep_all:
                self._drop_watched(now)
            try:
                files = sorted(self.root.glob('*/seg_*.ts'), key=lambda p: p.stat().st_mtime)
                total = sum(p.stat().st_size for p in files)
                for path in files:
                    if total <= limit:
                        break
                    if time.time() - path.stat().st_mtime < 600:
                        continue
                    total -= path.stat().st_size
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _protected(self, media_key, now, current=True):
        """Segment indices to keep: around the viewer's current place (the latest request, also while
        paused) and, for ``grace`` seconds, around places just left by a seek. Ahead: what the running
        job has prepared. Everything the viewer has passed goes."""
        keep = set()
        served = self.served.get(media_key, {})
        # Only the variant being watched keeps its place for long; one switched away from — 10 s.
        latest = max(served, key=served.get) if served and current else None
        for index, seen in list(served.items()):
            if index != latest and now - seen > self.grace:
                del served[index]
            elif index != latest or now - seen <= 1800:
                keep.update(range(index - self.keep_behind, index + self.ahead + 1))
        for job in self.jobs.get(media_key, []):
            keep.update(range(job.last_index - self.keep_behind, max(job.frontier(), job.last_index) + 2))
        return keep

    def _drop_watched(self, now):
        """Delete segments far behind every viewer; untouched for a minute so nothing in use goes away."""
        with self._lock:
            streams = set(self.served) | set(self.jobs)
            newest = {}          # media -> stream with the most recent request
            for stream in self.served:
                media, stamp = stream.rpartition('@')[0], max(self.served[stream].values(), default=0)
                if stamp >= newest.get(media, ('', -1))[1]:
                    newest[media] = (stream, stamp)
            protected = {self._dir(stream).name: self._protected(stream, now, newest.get(stream.rpartition('@')[0], ('',))[0] == stream)
                         for stream in streams}
        try:
            directories = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return
        for directory in directories:
            keep = protected.get(directory.name, set())
            for path in directory.glob('seg_*.ts'):
                try:
                    index = int(path.stem[4:])
                    if index not in keep and time.time() - path.stat().st_mtime > 15:
                        path.unlink()
                except (OSError, ValueError):
                    continue
            if directory.name not in protected:      # a variant nobody watches: drop its leftovers too
                try:
                    if not any(directory.glob('seg_*.ts')):
                        shutil.rmtree(directory, ignore_errors=True)
                except OSError:
                    pass

    def close(self):
        with self._lock:
            self._closed = True
            for jobs in self.jobs.values():
                for job in jobs:
                    self._kill(job)
            self.jobs.clear()
            self._cond.notify_all()
