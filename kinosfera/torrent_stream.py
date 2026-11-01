"""Shared BitTorrent streaming engine.

Design goals (speed first):

* **One libtorrent session for the whole server.** DHT, peer lists, port mapping and
  connections are shared; a film watched by several people (or a watch-party room)
  is downloaded once.
* **One torrent handle per info-hash**, kept alive while anyone reads it and resumed
  instantly from resume data later (no re-hash, no re-download).
* **Streaming-aware piece picking.** The pieces right after each playhead are
  time-critical (``set_piece_deadline``), a bitrate-sized read-ahead window follows
  with high priority. Head and tail pieces are fetched first so containers (MP4 ``moov``,
  MKV cues, AVI index) open immediately.
* **Sliding disk window.** By default only the part around each viewer is kept: watched
  pieces are released from disk (sparse-file holes) and marked missing in libtorrent. That
  re-adds the torrent, so it is done rarely and in calm moments. ``background=True`` instead
  downloads and keeps the whole file.
* **Zero-copy-ish reads.** Verified pieces are read directly from disk and checked
  against the SHA-1 from the .torrent; ``read_piece`` is only a fallback.

Nothing is started on import. The engine starts lazily on first use.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import logging
import errno
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time

import torrent_meta
from torrent_meta import validate_magnet  # noqa: F401  (re-exported for compatibility)

log = logging.getLogger('kinosfera.engine')
MiB = 1024 * 1024


def parse_range(value: str | None, size: int) -> tuple[int, int]:
    """Parse one RFC 9110 byte range; malformed/unsatisfiable ranges raise ValueError."""
    if size < 0:
        raise ValueError('Invalid file size')
    if value is None:
        return (0, size - 1)
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if not match or size == 0:
        raise ValueError('Invalid byte range')
    left, right = match.groups()
    if not left and not right:
        raise ValueError('Empty byte range')
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
        if start >= size or start > end:
            raise ValueError('Unsatisfiable byte range')
        return start, end
    length = int(right)
    if length <= 0:
        raise ValueError('Unsatisfiable suffix range')
    return max(0, size - length), size - 1


def punch_holes(path, ranges):
    """Release disk space of byte ranges ``[(start, end), ...]``; the file size stays the same.

    Returns False when the platform cannot do it; raises OSError when the file system
    refuses (unsupported, or the file is still open elsewhere)."""
    ranges = [(int(a), int(b)) for a, b in ranges if b > a]
    if os.name == 'nt':                 # the sparse flag is set even without ranges: later gaps take no disk
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                             wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # GENERIC_READ | GENERIC_WRITE, share read/write/delete, OPEN_EXISTING
        handle = kernel32.CreateFileW(str(path), 0xC0000000, 7, None, 3, 0x80, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            returned = wintypes.DWORD()
            if not kernel32.DeviceIoControl(handle, 0x900C4, None, 0, None, 0, ctypes.byref(returned), None):  # FSCTL_SET_SPARSE
                raise ctypes.WinError(ctypes.get_last_error())
            for start, end in ranges:
                zero = (ctypes.c_longlong * 2)(start, end)   # FILE_ZERO_DATA_INFORMATION
                if not kernel32.DeviceIoControl(handle, 0x980C8, zero, 16, None, 0, ctypes.byref(returned), None):  # FSCTL_SET_ZERO_DATA
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
        return True
    if not ranges:
        return True
    if sys.platform.startswith('linux'):  # files are sparse by default here
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
        libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        fd = os.open(path, os.O_RDWR)
        try:
            for start, end in ranges:
                if libc.fallocate(fd, 0x01 | 0x02, start, end - start) != 0:  # KEEP_SIZE | PUNCH_HOLE
                    code = ctypes.get_errno()
                    raise OSError(code, os.strerror(code), str(path))
        finally:
            os.close(fd)
        return True
    return False


def _hole_unsupported(exc):
    """The file system cannot make sparse holes at all (as opposed to a temporary lock)."""
    return getattr(exc, 'winerror', None) in (1, 50) or exc.errno in (errno.EOPNOTSUPP, getattr(errno, 'ENOTSUP', -1))


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class EngineConfig:
    cache_dir: Path
    cache_limit: int = 20 * 1024 ** 3          # disk cache for all films (LRU by last use)
    readahead_seconds: float = 120.0           # how far ahead of each playhead to prefetch
    critical_seconds: float = 12.0             # time-critical part of the window (deadlines)
    min_window: int = 24 * MiB
    max_window: int = 512 * MiB
    background: bool = False                   # True: download and keep the whole file on disk
    keep_behind_seconds: float = 30.0          # window mode: watched part kept on disk for rewinds
    trim_step: int = 512 * MiB                 # window mode: release in chunks this big, in a calm moment...
    trim_force: int = 1536 * MiB               # ...or regardless at this size (each release reconnects peers)
    seek_grace: float = 10.0                   # window mode: a position nobody reads any more (or the
                                               # place a viewer jumped from) stays on disk this long
    listen_port: int = 6881
    upload_limit: int = 0                      # bytes/s, 0 = unlimited (helps tit-for-tat)
    download_limit: int = 0
    idle_seconds: float = 180.0                # stop a torrent nobody reads for this long
    piece_timeout: float = 90.0                # max wait for one piece before an HTTP read fails
    extra_trackers: bool = True
    ram_cache: int = 32 * MiB                  # per torrent, verified pieces shared by viewers

    @classmethod
    def from_env(cls, cache_dir):
        return cls(Path(os.environ.get('CINEMA_CACHE_DIR', cache_dir)),
                   cache_limit=int(_env_float('CINEMA_CACHE_LIMIT_GB', 20) * 1024 ** 3),
                   readahead_seconds=_env_float('CINEMA_READAHEAD_SECONDS', 120),
                   background=os.environ.get('CINEMA_BACKGROUND_DOWNLOAD', '0') != '0',
                   keep_behind_seconds=_env_float('CINEMA_KEEP_BEHIND_SECONDS', 30),
                   trim_step=int(_env_float('CINEMA_TRIM_STEP_MB', 512) * MiB),
                   trim_force=int(_env_float('CINEMA_TRIM_FORCE_MB', 1536) * MiB),
                   seek_grace=_env_float('CINEMA_SEEK_GRACE_SECONDS', 10),
                   listen_port=int(_env_float('CINEMA_LISTEN_PORT', 6881)),
                   upload_limit=int(_env_float('CINEMA_UPLOAD_LIMIT_KBPS', 0) * 1024),
                   download_limit=int(_env_float('CINEMA_DOWNLOAD_LIMIT_KBPS', 0) * 1024),
                   extra_trackers=os.environ.get('CINEMA_EXTRA_TRACKERS', '1') != '0')


@dataclass
class FileView:
    file_id: int
    name: str
    offset: int          # byte offset inside the torrent
    size: int
    path: Path
    first_piece: int
    last_piece: int
    mime: str


@dataclass
class Reader:
    """One HTTP response (browser or FFmpeg) reading a file sequentially."""
    id: str
    owner: str
    file_id: int
    position: int
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    waiting: int | None = None
    cancelled: bool = False


class Torrent:
    def __init__(self, key, handle, info, save_path, lock):
        self.key = key
        self.handle = handle
        self.info = info
        self.save_path = Path(save_path)
        self.condition = threading.Condition(lock)
        self.piece_length = info.piece_length()
        self.num_pieces = info.num_pieces()
        self.views: dict[int, FileView] = {}
        self.readers: dict[str, Reader] = {}
        self.hints: dict[str, tuple] = {}            # owner -> (file_id, byte position, monotonic)
        self.pins: dict[str, float] = {}             # prewarm owner -> expiry
        self.recent: dict[str, tuple] = {}           # owner -> (file_id, byte position, monotonic): kept on disk
        self.trim: dict | None = None                # window mode: release in progress (handle is re-added)
        self.trim_after = 0.0                        # monotonic time of the next release check
        self.trim_disabled = False                   # file system cannot make sparse holes
        self.close_requested = False                 # close asked for while a release was running
        self.resume_have: list | None = None         # pieces claimed by the resume data at add time
        self.check_priorities = True                 # compare libtorrent's piece priorities with ours
        self.known_peers: OrderedDict[tuple, float] = OrderedDict()   # endpoints seen connected
        self.reconnected = 0.0                       # monotonic time of the last forced reconnect
        self.have = bytearray(self.num_pieces)
        self.applied: dict[int, int] = {}            # piece -> priority we set above base
        self.deadlines: set[int] = set()
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.cache_bytes = 0
        self.read_pending: set[int] = set()
        self.read_errors: dict[int, str] = {}
        self.bitrate = 1.2 * MiB                     # bytes/s, refined by the media probe
        self.error: str | None = None
        self.closing = False
        self.closed = False
        self.created = time.monotonic()
        self.last_used = time.monotonic()
        self.dirty = True
        self.stats_cache = (0.0, {})


class StreamError(RuntimeError):
    pass


class StreamEngine:
    def __init__(self, config: EngineConfig, lt_module=None):
        self.config = config
        self._lt = lt_module
        self.available = lt_module is not None or importlib.util.find_spec('libtorrent') is not None
        self._lock = threading.RLock()
        self._session = None
        self._thread = None
        self._closed = threading.Event()
        self.torrents: dict[str, Torrent] = {}
        self._resolvers: dict[str, dict] = {}
        self._last_tick = 0.0
        self.started_at = None

    # ------------------------------------------------------------------ session
    def _settings(self, lt):
        mask = 0
        for name in ('error', 'storage', 'status', 'piece_progress', 'tracker', 'port_mapping'):
            for source, attr in ((getattr(lt, 'alert_category', None), name),
                                 (getattr(getattr(lt, 'alert', None), 'category_t', None), name + '_notification')):
                if source is not None and hasattr(source, attr):
                    mask |= int(getattr(source, attr))
                    break
        port = self.config.listen_port
        return {
            'user_agent': 'KinoSfera/2.0 libtorrent',
            'listen_interfaces': f'0.0.0.0:{port},[::]:{port}',
            'max_retry_port_bind': 20,
            'enable_dht': True, 'enable_lsd': True, 'enable_upnp': True, 'enable_natpmp': True,
            'dht_bootstrap_nodes': 'dht.libtorrent.org:25401,router.bittorrent.com:6881,'
                                   'router.utorrent.com:6881,dht.transmissionbt.com:6881,dht.aelitis.com:6881',
            'connections_limit': 200,
            'connection_speed': 80,             # outgoing connection attempts per second
            'torrent_connect_boost': 50,        # connect to this many peers immediately on add
            'smooth_connects': False,
            'peer_connect_timeout': 4,
            'min_reconnect_time': 1,
            'request_timeout': 8,               # re-request a stalled block from a faster peer sooner
            'piece_timeout': 6,
            'peer_timeout': 60,
            'max_out_request_queue': 1500,
            'max_allowed_in_request_queue': 2000,
            'request_queue_time': 2,
            'whole_pieces_threshold': 4,
            'strict_end_game_mode': False,
            'announce_to_all_trackers': True, 'announce_to_all_tiers': True,
            'prefer_udp_trackers': True,
            'max_peerlist_size': 4000,
            'unchoke_slots_limit': 16,
            # Upload buffers are per peer: big ones made the process hold hundreds of MB while seeding.
            'send_buffer_watermark': 1 * MiB,
            'send_buffer_watermark_factor': 50,
            'aio_threads': 4, 'hashing_threads': 2,
            # Off: with it libtorrent announces upload-only once the window is downloaded and seeds
            # drop us; the next window then waits for peers to come back.
            'close_redundant_connections': False,
            'max_metadata_size': torrent_meta.MAX_TORRENT_BYTES * 4,
            'upload_rate_limit': self.config.upload_limit,
            'download_rate_limit': self.config.download_limit,
            'alert_mask': mask,
            'alert_queue_size': 20000,
        }

    def _ensure(self):
        if self._session is not None:
            return
        if self._closed.is_set():
            raise StreamError('Торрент-движок остановлен.')
        if not self.available:
            raise StreamError('Установите libtorrent (pip install libtorrent) и перезапустите сайт.')
        if self._lt is None:
            try:
                import libtorrent
            except ImportError as exc:
                raise StreamError('Не удалось загрузить libtorrent. Проверьте установку.') from exc
            self._lt = libtorrent
        lt = self._lt
        root = self.config.cache_dir
        if root.is_symlink():
            raise StreamError('Папка кэша не может быть символической ссылкой.')
        root.mkdir(parents=True, exist_ok=True)
        settings = self._settings(lt)
        session = None
        state_file = root / 'session.state'
        if state_file.is_file() and hasattr(lt, 'read_session_params'):
            try:  # restores the DHT routing table: peers are found in seconds after a restart
                params = lt.read_session_params(state_file.read_bytes())
                session = lt.session(params)
            except Exception:  # noqa: BLE001 - corrupted state is not fatal
                session = None
        if session is None:
            session = lt.session({'alert_mask': settings['alert_mask']})
        for key, value in settings.items():   # one by one: unknown keys on older builds are skipped
            try:
                session.apply_settings({key: value})
            except Exception:  # noqa: BLE001
                log.debug('libtorrent setting %s is not supported', key)
        self._session = session
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._dispatch, name='torrent-alerts', daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ torrents
    def _flag(self, name):
        lt = self._lt
        return int(getattr(lt.torrent_flags, name, 0)) if hasattr(lt, 'torrent_flags') else 0

    def open(self, torrent_bytes: bytes, file_id: int, owner=None) -> Torrent:
        """Get (or add) the shared torrent for these metadata and select ``file_id``."""
        key = torrent_meta.info_hash(torrent_bytes)
        with self._lock:
            self._ensure()
            torrent = self.torrents.get(key)
            if torrent is not None and torrent.closing and not torrent.closed:
                torrent.closing = False          # someone came back: cancel the idle shutdown
                torrent.handle.resume()
            if torrent is None or torrent.closed:
                torrent = self._add(key, torrent_bytes)
            if file_id not in torrent.views:
                self._add_view(torrent, file_id)
            torrent.last_used = time.monotonic()
            self._reprioritize(torrent)
            return torrent

    def _add(self, key, torrent_bytes):
        lt = self._lt
        info = lt.torrent_info(lt.bdecode(torrent_bytes))
        save_path = self.config.cache_dir / key
        save_path.mkdir(parents=True, exist_ok=True)
        params = None
        resume = self.config.cache_dir / f'{key}.resume'
        if resume.is_file() and hasattr(lt, 'read_resume_data'):
            try:
                params = lt.read_resume_data(resume.read_bytes())
            except Exception:  # noqa: BLE001 - fall back to a fresh add (files are re-checked)
                params = None
        resume_have = None if params is None else [bool(x) for x in (getattr(params, 'have_pieces', None) or [])]
        if params is None:
            params = lt.add_torrent_params()
        params.ti = info
        params.save_path = str(save_path)
        params.file_priorities = [0] * info.files().num_files()
        params.flags |= self._flag('paused')
        params.flags &= ~self._flag('auto_managed')
        params.flags &= ~self._flag('upload_mode')
        try:
            handle = self._session.add_torrent(params)
        except RuntimeError as exc:
            raise StreamError('Не удалось запустить раздачу. Проверьте .torrent-файл.') from exc
        torrent = Torrent(key, handle, info, save_path, self._lock)
        torrent.resume_have = resume_have
        for piece in range(torrent.num_pieces):
            if handle.have_piece(piece):
                torrent.have[piece] = 1
        if self.config.extra_trackers:
            for tier, url in enumerate(torrent_meta.DEFAULT_TRACKERS):
                try:
                    handle.add_tracker({'url': url, 'tier': 10 + tier})
                except Exception:  # noqa: BLE001
                    pass
        self.torrents[key] = torrent
        self._touch_meta(torrent)
        return torrent

    def _add_view(self, torrent, file_id):
        files = torrent.info.files()
        if not 0 <= file_id < files.num_files() or files.file_size(file_id) <= 0:
            raise StreamError('Видеофайл не найден в раздаче.')
        offset, size = files.file_offset(file_id), files.file_size(file_id)
        name = files.file_path(file_id).replace('\\', '/')
        view = FileView(file_id, name, offset, size, Path(files.file_path(file_id, str(torrent.save_path))),
                        offset // torrent.piece_length, (offset + size - 1) // torrent.piece_length,
                        torrent_meta.SUPPORTED.get(Path(name).suffix.lower(), 'application/octet-stream'))
        torrent.views[file_id] = view
        torrent.dirty = True
        self._prepare_file(torrent, view)
        if not torrent.trim:            # otherwise applied when the handle is re-added
            self._apply_views(torrent)

    def _prepare_file(self, torrent, view):
        """Window mode: the video file is sparse before libtorrent writes into it, so gaps take no disk.

        Without the flag NTFS fills everything before the written tail (container index)
        with zeros and a film takes its full size at once."""
        if self.config.background or torrent.trim_disabled:
            return
        try:
            ranges = []
            if not view.path.exists():
                view.path.parent.mkdir(parents=True, exist_ok=True)
                view.path.touch()
            elif torrent.resume_have is not None:
                # Zero-filled gaps of an old non-sparse file: only pieces the resume data does not claim.
                known = torrent.resume_have
                missing = [p for p in range(view.first_piece, view.last_piece + 1)
                           if not (p < len(known) and known[p]) and not torrent.have[p]]
                ranges = self._hole_ranges(torrent, missing).get(view.path, [])
            if not punch_holes(view.path, ranges):
                torrent.trim_disabled = True
        except OSError as exc:
            if _hole_unsupported(exc):
                torrent.trim_disabled = True
                log.warning('Файловая система кэша не поддерживает разреженные файлы: фильм будет храниться целиком.')
            else:
                log.debug('cannot prepare sparse file %s: %s', view.path, exc)

    def _apply_views(self, torrent):
        handle = torrent.handle
        for view in torrent.views.values():
            handle.file_priority(view.file_id, 1)  # >0 so data lands in the real file, not the part file
        base = 1 if self.config.background else 0
        priorities = [torrent.applied.get(p, 0) for p in range(torrent.num_pieces)]
        for view in torrent.views.values():
            for piece in range(view.first_piece, view.last_piece + 1):
                priorities[piece] = max(priorities[piece], base)
        handle.prioritize_pieces(priorities)
        handle.resume()

    def set_bitrate(self, torrent, bytes_per_second):
        if bytes_per_second and math.isfinite(bytes_per_second):
            with self._lock:
                torrent.bitrate = min(12 * MiB, max(160 * 1024, float(bytes_per_second)))
                torrent.dirty = True

    def pin(self, torrent, owner, seconds):
        """Keep the head of the film hot (room prewarm) even before anyone presses play."""
        with self._lock:
            torrent.pins[owner] = time.monotonic() + seconds
            torrent.last_used = time.monotonic()
            self._reprioritize(torrent)

    def hint(self, torrent, owner, file_id, position):
        """Playback position reported by a client (seconds mapped to bytes by the caller)."""
        with self._lock:
            view = torrent.views.get(file_id)
            if view is None:
                return
            torrent.hints[owner] = (file_id, max(0, min(int(position), view.size - 1)), time.monotonic())
            torrent.last_used = time.monotonic()
            torrent.dirty = True

    def add_peer(self, torrent, endpoint):
        """Connect to a known peer directly (local seeders, tests, LAN)."""
        with self._lock:
            if not torrent.trim:
                torrent.handle.connect_peer(tuple(endpoint))

    def forget(self, torrent, owner):
        with self._lock:
            torrent.hints.pop(owner, None)
            torrent.pins.pop(owner, None)
            for reader in torrent.readers.values():
                if reader.owner == owner:
                    reader.cancelled = True
            torrent.dirty = True
            torrent.condition.notify_all()

    # ------------------------------------------------------------------ priorities
    def _window(self, torrent):
        bitrate = torrent.bitrate
        critical = bitrate * self.config.critical_seconds
        window = min(self.config.max_window, max(self.config.min_window, bitrate * self.config.readahead_seconds))
        crit_n = max(2, math.ceil(critical / torrent.piece_length))
        return crit_n, max(crit_n + 1, math.ceil(window / torrent.piece_length))

    def _positions(self, torrent, now):
        result = []
        for reader in torrent.readers.values():
            if not reader.cancelled:
                result.append((reader.file_id, reader.position, 1.0))
                self._remember_position(torrent, reader.owner, reader.file_id, reader.position, now)
        for owner, (file_id, position, seen) in list(torrent.hints.items()):
            if now - seen > 45:
                del torrent.hints[owner]
            else:
                result.append((file_id, position, 0.5))
                self._remember_position(torrent, owner, file_id, position, now)
        for owner, expiry in list(torrent.pins.items()):
            if expiry < now:
                del torrent.pins[owner]
            else:
                for view in torrent.views.values():
                    result.append((view.file_id, 0, 0.5))
                    torrent.recent[f'{owner}:{view.file_id}'] = (view.file_id, 0, now)
        return result

    def _remember_position(self, torrent, owner, file_id, position, now):
        """Positions kept on disk. After a seek the old place stays a moment (seek_grace) as a ghost."""
        previous = torrent.recent.get(owner)
        if previous is not None and now - previous[2] <= self.config.seek_grace:
            _, win_n = self._window(torrent)
            if previous[0] != file_id or abs(position - previous[1]) > win_n * torrent.piece_length:
                torrent.recent[f'{owner}~{now:.3f}'] = (previous[0], previous[1], now)
        torrent.recent[owner] = (file_id, position, now)

    @staticmethod
    def _header_pieces(view, plen):
        """Container headers: first pieces and the last pieces (MP4 moov / MKV cues / AVI idx1)."""
        head_n = max(2, math.ceil(2 * MiB / plen))
        tail_n = max(1, math.ceil(1 * MiB / plen))
        head = range(view.first_piece, min(view.last_piece, view.first_piece + head_n - 1) + 1)
        tail = range(view.last_piece, max(view.first_piece, view.last_piece - tail_n + 1) - 1, -1)
        return head, tail

    def _reprioritize(self, torrent):
        if torrent.closing or torrent.closed or torrent.trim:
            return
        now = time.monotonic()
        torrent.dirty = False
        crit_n, win_n = self._window(torrent)
        plen = torrent.piece_length
        desired: dict[int, int] = {}
        deadlines: dict[int, int] = {}
        ms_per_piece = plen / torrent.bitrate * 1000

        def want(piece, prio, deadline=None):
            if torrent.have[piece]:
                return
            if desired.get(piece, 0) < prio:
                desired[piece] = prio
            if deadline is not None:
                deadlines[piece] = min(deadlines.get(piece, deadline), deadline)

        for view in torrent.views.values():
            head, tail = self._header_pieces(view, plen)
            for k, piece in enumerate(head):
                want(piece, 7, k * 100)
            for k, piece in enumerate(tail):
                want(piece, 7, 150 + k * 100)
        for file_id, position, weight in self._positions(torrent, now):
            view = torrent.views.get(file_id)
            if view is None:
                continue
            start = (view.offset + position) // plen
            span = win_n if weight >= 1 else max(crit_n, win_n // 2)
            for k in range(span):
                piece = start + k
                if piece > view.last_piece:
                    break
                if k < crit_n:
                    want(piece, 7, int(k * ms_per_piece * 0.5))
                else:
                    want(piece, 6 if k < span // 2 else 5)
        handle = torrent.handle
        base = 1 if self.config.background else 0
        in_view = lambda p: any(v.first_piece <= p <= v.last_piece for v in torrent.views.values())  # noqa: E731
        try:
            for piece, prio in desired.items():
                if torrent.applied.get(piece) != prio:
                    handle.piece_priority(piece, prio)
                    torrent.applied[piece] = prio
            for piece in [p for p in torrent.applied if p not in desired]:
                del torrent.applied[piece]
                if not torrent.have[piece]:
                    handle.piece_priority(piece, base if in_view(piece) else 0)
            for piece, deadline in deadlines.items():
                if piece not in torrent.deadlines:
                    handle.set_piece_deadline(piece, max(0, deadline))
                    torrent.deadlines.add(piece)
            for piece in [p for p in torrent.deadlines if p not in deadlines]:
                torrent.deadlines.discard(piece)
                if not torrent.have[piece]:
                    handle.reset_piece_deadline(piece)
        except RuntimeError as exc:  # handle became invalid
            torrent.error = str(exc)

    # ------------------------------------------------------------------ reading
    def readers_for(self, torrent, owner):
        return [r for r in torrent.readers.values() if r.owner == owner and not r.cancelled]

    def read(self, torrent, file_id, start, end, owner, chunk=256 * 1024):
        """Yield bytes [start, end] of a file. Blocks for missing pieces (bounded by timeouts)."""
        with self._lock:
            view = torrent.views.get(file_id)
            if view is None or torrent.closed:
                raise StreamError('Видеопоток закрыт. Запустите фильм снова.')
            reader = Reader(secrets.token_hex(6), owner, file_id, start)
            # A new request from the same viewer supersedes requests stuck far away (old seeks).
            crit_n, win_n = self._window(torrent)
            new_piece = (view.offset + start) // torrent.piece_length
            for old in self.readers_for(torrent, owner):
                old_piece = (view.offset + old.position) // torrent.piece_length
                if old.waiting is not None and not 0 <= old_piece - new_piece <= win_n:
                    old.cancelled = True
            own = sorted(self.readers_for(torrent, owner), key=lambda r: r.touched)
            for old in own[:-2]:
                old.cancelled = True
            torrent.readers[reader.id] = reader
            torrent.last_used = time.monotonic()
            self._reprioritize(torrent)
            torrent.condition.notify_all()
        return self._iterate(torrent, view, reader, start, end, chunk)

    def _iterate(self, torrent, view, reader, start, end, chunk):
        plen = torrent.piece_length
        try:
            position = start
            while position <= end:
                absolute = view.offset + position
                piece, offset = divmod(absolute, plen)
                reader.position = position
                reader.touched = time.monotonic()
                data = self._piece(torrent, piece, reader)
                length = min(len(data) - offset, end - position + 1)
                if length <= 0:
                    raise StreamError('Некорректная длина части видео.')
                block = memoryview(data)[offset:offset + length]
                for index in range(0, length, chunk):
                    yield bytes(block[index:index + chunk])
                    reader.touched = time.monotonic()
                position += length
        finally:
            with self._lock:
                torrent.readers.pop(reader.id, None)
                torrent.last_used = time.monotonic()
                torrent.dirty = True

    def _piece(self, torrent, piece, reader):
        deadline = time.monotonic() + self.config.piece_timeout
        with self._lock:
            reprioritized_for = None
            while True:
                if reader.cancelled or torrent.closed or self._closed.is_set():
                    raise StreamError('Чтение остановлено: зритель перемотал или закрыл плеер.')
                if torrent.error:
                    raise StreamError(torrent.error)
                cached = torrent.cache.get(piece)
                if cached is not None:
                    torrent.cache.move_to_end(piece)
                    reader.waiting = None
                    if reprioritized_for != piece and piece + 1 <= torrent.num_pieces:
                        torrent.dirty = True
                    return cached
                if piece in torrent.read_errors:
                    raise StreamError(torrent.read_errors.pop(piece))
                if torrent.have[piece] or (not torrent.trim and torrent.handle.have_piece(piece)):
                    torrent.have[piece] = 1
                    reader.waiting = None
                    break
                if reader.waiting != piece or reprioritized_for != piece:
                    reader.waiting = piece
                    reprioritized_for = piece
                    self._reprioritize(torrent)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StreamError('Часть видео не загрузилась вовремя. Дождитесь участников раздачи.')
                torrent.condition.wait(min(remaining, 0.5))
        data = self._load(torrent, piece, reader, deadline)
        with self._lock:
            self._remember(torrent, piece, data)
            torrent.dirty = True
        return data

    def _remember(self, torrent, piece, data):
        if piece in torrent.cache:
            torrent.cache.move_to_end(piece)
            return
        torrent.cache[piece] = data
        torrent.cache_bytes += len(data)
        while torrent.cache_bytes > self.config.ram_cache and len(torrent.cache) > 1:
            _, removed = torrent.cache.popitem(last=False)
            torrent.cache_bytes -= len(removed)

    def _load(self, torrent, piece, reader, deadline):
        start = piece * torrent.piece_length
        size = torrent.info.piece_size(piece)
        expected = None
        try:
            expected = bytes(torrent.info.hash_for_piece(piece))
        except Exception:  # noqa: BLE001 - v2-only torrents have no SHA-1 piece hashes
            expected = None
        view = next((v for v in torrent.views.values() if v.offset <= start and start + size <= v.offset + v.size), None)
        if view is not None and expected and len(expected) == 20:
            try:
                with open(view.path, 'rb') as file:
                    file.seek(start - view.offset)
                    data = file.read(size)
                if len(data) == size and hashlib.sha1(data).digest() == expected:
                    return data
            except OSError:
                pass
        # Fallback: libtorrent reads (and has verified) the piece for us.
        with self._lock:
            if piece not in torrent.read_pending:
                torrent.read_pending.add(piece)
                if not torrent.trim:        # otherwise requested again once the handle is re-added
                    torrent.handle.read_piece(piece)
            while True:
                if reader.cancelled or torrent.closed:
                    raise StreamError('Чтение остановлено.')
                data = torrent.cache.get(piece)
                if data is not None:
                    return data
                if piece in torrent.read_errors:
                    raise StreamError(torrent.read_errors.pop(piece))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    torrent.read_pending.discard(piece)
                    raise StreamError('Не удалось прочитать загруженную часть видео.')
                torrent.condition.wait(min(remaining, 0.5))

    # ------------------------------------------------------------------ status
    def buffered_ahead(self, torrent, file_id, position):
        """Contiguous downloaded bytes from ``position`` (for the buffer indicator)."""
        with self._lock:
            view = torrent.views.get(file_id)
            if view is None:
                return 0
            plen = torrent.piece_length
            piece = (view.offset + position) // plen
            count = 0
            while piece + count <= view.last_piece and torrent.have[piece + count]:
                count += 1
                if count > 4096:
                    break
            if not count:
                return 0
            return max(0, min(view.offset + view.size, (piece + count) * plen) - (view.offset + position))

    def stats(self, torrent, file_id=None):
        now = time.monotonic()
        with self._lock:
            cached_at, cached = torrent.stats_cache
            if (now - cached_at < 0.7 or torrent.trim) and cached and cached.get('file_id') == file_id:
                return dict(cached)
            try:
                status = torrent.handle.status()
                peers, seeds = status.num_peers, status.num_seeds
                down, up = status.download_payload_rate, status.upload_payload_rate
                state = str(status.state).rsplit('.', 1)[-1]
            except RuntimeError:
                peers = seeds = down = up = 0
                state = 'closed'
            view = torrent.views.get(file_id) if file_id is not None else next(iter(torrent.views.values()), None)
            progress = 0.0
            if view is not None:
                total = view.last_piece - view.first_piece + 1
                progress = sum(torrent.have[view.first_piece:view.last_piece + 1]) / max(1, total)
            result = dict(file_id=file_id, peers=peers, seeds=seeds, download_rate=down, upload_rate=up,
                          progress=round(progress, 4), state=state, readers=len(torrent.readers),
                          viewers=len({r.owner for r in torrent.readers.values()} | set(torrent.hints)),
                          bitrate=int(torrent.bitrate), error=torrent.error)
            torrent.stats_cache = (now, result)
            return dict(result)

    def overview(self):
        with self._lock:
            items = []
            for torrent in self.torrents.values():
                stats = self.stats(torrent)
                view = next(iter(torrent.views.values()), None)
                items.append(dict(stats, key=torrent.key, name=view.name if view else torrent.key,
                                  size=view.size if view else 0, age=int(time.monotonic() - torrent.created)))
            totals = dict(download_rate=sum(i['download_rate'] for i in items),
                          upload_rate=sum(i['upload_rate'] for i in items),
                          peers=sum(i['peers'] for i in items))
            return dict(running=self._session is not None, available=self.available, torrents=items,
                        totals=totals, cache=self.cache_usage(), port=self.config.listen_port)

    # ------------------------------------------------------------------ magnets
    def resolve_magnet(self, magnet, timeout=90.0):
        """Fetch metadata for a magnet link and return complete .torrent bytes."""
        details = torrent_meta.magnet_info(magnet)
        with self._lock:
            self._ensure()
            existing = self.torrents.get(details['info_hash'] or '')
            if existing is not None:
                trackers = [t['url'] for t in existing.handle.trackers()]
                return torrent_meta.build_torrent(existing.info.info_section(), trackers or details['trackers'])
            lt = self._lt
            params = lt.parse_magnet_uri(magnet)
            path = self.config.cache_dir / '_metadata' / secrets.token_hex(6)
            path.mkdir(parents=True, exist_ok=True)
            params.save_path = str(path)
            params.flags &= ~self._flag('auto_managed')
            params.flags &= ~self._flag('paused')
            handle = self._session.add_torrent(params)
            if self.config.extra_trackers:
                for tier, url in enumerate(torrent_meta.DEFAULT_TRACKERS):
                    try:
                        handle.add_tracker({'url': url, 'tier': 10 + tier})
                    except Exception:  # noqa: BLE001
                        pass
            job = dict(handle=handle, event=threading.Event(), error=None)
            token = secrets.token_hex(6)
            self._resolvers[token] = job
        try:
            end = time.monotonic() + timeout
            while not job['event'].wait(0.5):
                with self._lock:
                    if handle.status().has_metadata:
                        break
                if time.monotonic() > end:
                    raise StreamError('Не удалось получить метаданные: участники раздачи не найдены. '
                                      'Попробуйте позже или загрузите .torrent-файл.')
            if job['error']:
                raise StreamError(job['error'])
            with self._lock:
                info = handle.torrent_file()
                trackers = [t['url'] for t in handle.trackers()]
                data = torrent_meta.build_torrent(info.info_section(), trackers or details['trackers'])
            return data
        finally:
            with self._lock:
                self._resolvers.pop(token, None)
                try:
                    self._session.remove_torrent(handle, int(getattr(self._lt.options_t, 'delete_files', 1)))
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ alerts & housekeeping
    def _find(self, handle):
        for torrent in self.torrents.values():
            if torrent.handle == handle:
                return torrent
        return None

    def _dispatch(self):
        while not self._closed.is_set():
            try:
                session = self._session
                if session is None:
                    return
                alerts = session.pop_alerts() if session.wait_for_alert(250) else []
                with self._lock:
                    for alert in alerts:
                        self._on_alert(alert)
                    now = time.monotonic()
                    for torrent in list(self.torrents.values()):
                        if torrent.dirty and not torrent.closing:
                            self._reprioritize(torrent)
                        if torrent.check_priorities and not torrent.closing and not torrent.trim:
                            self._enforce_priorities(torrent)
                    if now - self._last_tick >= 1.0:
                        self._last_tick = now
                        self._tick(now)
            except Exception:  # noqa: BLE001 - keep the dispatcher alive
                log.exception('torrent dispatcher error')
                time.sleep(0.2)

    def _on_alert(self, alert):
        name = type(alert).__name__
        name = name[:-6] if name.endswith('_alert') else name
        handle = getattr(alert, 'handle', None)
        if name == 'metadata_received' or name == 'metadata_failed':
            for job in self._resolvers.values():
                if job['handle'] == handle:
                    if name == 'metadata_failed':
                        job['error'] = 'Метаданные раздачи повреждены.'
                    job['event'].set()
            return
        torrent = self._find(handle) if handle is not None else None
        if torrent is None:
            return
        if name == 'piece_finished':
            piece = int(alert.piece_index)
            if 0 <= piece < torrent.num_pieces:
                if not self.config.background and piece not in torrent.applied:
                    torrent.check_priorities = True     # a piece we never asked for: libtorrent reset them
                torrent.have[piece] = 1
                torrent.deadlines.discard(piece)
                torrent.applied.pop(piece, None)
                torrent.dirty = True
            torrent.condition.notify_all()
        elif name == 'read_piece':
            piece = int(alert.piece)
            torrent.read_pending.discard(piece)
            error = getattr(alert, 'error', None)
            if error is not None and error.value():
                torrent.read_errors[piece] = 'Не удалось прочитать загруженную часть видео.'
            else:
                data = bytes(alert.buffer)
                try:
                    expected = bytes(torrent.info.hash_for_piece(piece))
                except Exception:  # noqa: BLE001 - v2-only torrents
                    expected = b''
                if len(data) == torrent.info.piece_size(piece) and (len(expected) != 20 or hashlib.sha1(data).digest() == expected):
                    self._remember(torrent, piece, data)
                else:
                    torrent.read_errors[piece] = 'Часть видео не прошла проверку целостности.'
            torrent.condition.notify_all()
        elif name in ('torrent_error', 'file_error'):
            torrent.error = 'Ошибка раздачи: ' + str(getattr(alert, 'message', lambda: '')() or 'диск или файл недоступен')
            torrent.condition.notify_all()
        elif name == 'save_resume_data':
            trimming = torrent.trim is not None and torrent.trim['stage'] == 'saving'
            if trimming:   # the file on disk never claims pieces that are about to be released
                self._drop_from_params(torrent, alert.params)
            data = None
            try:  # alert.params lives inside the alert: serialize now, it is freed on the next pop_alerts()
                data = self._lt.write_resume_data_buf(alert.params)
                (self.config.cache_dir / f'{torrent.key}.resume').write_bytes(data)
            except Exception:  # noqa: BLE001
                pass
            if trimming:
                if data is None:
                    self._abort_trim(torrent)
                else:
                    self._remove_for_trim(torrent, data)
            elif torrent.closing:
                self._finalize_close(torrent)
        elif name == 'save_resume_data_failed':
            if torrent.trim is not None and torrent.trim['stage'] == 'saving':
                self._abort_trim(torrent)
            elif torrent.closing:
                self._finalize_close(torrent)
        elif name == 'torrent_removed':
            if torrent.trim is not None and torrent.trim['stage'] == 'removing':
                self._finish_trim(torrent)

    def _sync_have(self, torrent):
        """Pieces restored from resume data do not produce piece_finished alerts.
        Compared as whole byte strings (C speed): series torrents have tens of thousands of pieces."""
        try:
            flags = int(getattr(self._lt.torrent_handle, 'query_pieces', 0))
            pieces = torrent.handle.status(flags).pieces
            if pieces is None or len(pieces) != torrent.num_pieces:
                return
            current = bytearray(pieces)
            if current != torrent.have:
                torrent.have[:] = bytes(a | b for a, b in zip(current, torrent.have)) if torrent.trim else current
                torrent.dirty = True
                torrent.condition.notify_all()
        except Exception:  # noqa: BLE001
            pass

    def _tick(self, now):
        for torrent in list(self.torrents.values()):
            if torrent.trim:
                stage, since = torrent.trim['stage'], torrent.trim['since']
                if stage == 'saving' and now - since > 10:
                    self._abort_trim(torrent)
                elif stage == 'removing' and now - since > 3:   # torrent_removed alert did not come
                    self._finish_trim(torrent)
                continue
            second = int(now)
            if not torrent.closing and second % (2 if now - torrent.created < 60 else 10) == 0:
                self._sync_have(torrent)
            if not torrent.closing:
                if second % 6 == 0:
                    torrent.check_priorities = True
                if second % 10 == 0:
                    self._remember_peers(torrent)
                if second % 2 == 0:
                    self._revive(torrent, now)
            if torrent.closing:
                if now - torrent.last_used > 15:
                    self._finalize_close(torrent)
                continue
            busy = torrent.readers or torrent.hints or any(exp > now for exp in torrent.pins.values())
            if busy:
                torrent.last_used = now
            elif now - torrent.last_used > self.config.idle_seconds:
                self._begin_close(torrent)
                continue
            if not self.config.background and not torrent.trim_disabled and now >= torrent.trim_after:
                torrent.trim_after = now + 2
                self._maybe_trim(torrent, now)

    def _enforce_priorities(self, torrent):
        """libtorrent re-derives piece priorities from file priorities when a torrent finishes
        loading; without this the whole file would download although nobody watches it."""
        torrent.check_priorities = False
        try:
            current = list(torrent.handle.get_piece_priorities())
        except Exception:  # noqa: BLE001
            return
        if len(current) != torrent.num_pieces:
            return
        base = 1 if self.config.background else 0
        expected = bytearray(torrent.num_pieces)
        for view in torrent.views.values():
            expected[view.first_piece:view.last_piece + 1] = bytes([base]) * (view.last_piece - view.first_piece + 1)
        for piece, priority in torrent.applied.items():
            expected[piece] = priority
        if any(c != e for c, e, h in zip(current, expected, torrent.have) if not h):
            torrent.handle.prioritize_pieces(list(expected))

    def _remember_peers(self, torrent):
        try:
            for peer in torrent.handle.get_peer_info():
                endpoint = tuple(peer.ip)
                torrent.known_peers.pop(endpoint, None)
                torrent.known_peers[endpoint] = time.monotonic()
        except Exception:  # noqa: BLE001
            return
        while len(torrent.known_peers) > 300:
            torrent.known_peers.popitem(last=False)

    def _revive(self, torrent, now):
        """A viewer waits but peers are gone (seeds drop us while every wanted piece is done):
        call the remembered peers back and announce again instead of waiting for libtorrent."""
        waiting = any(r.waiting is not None and not r.cancelled for r in torrent.readers.values())
        if not waiting or now - torrent.reconnected < 5:
            return
        try:
            status = torrent.handle.status()
            if status.num_peers >= 3 and status.download_payload_rate > 0:
                return
            torrent.reconnected = now
            for endpoint in list(torrent.known_peers)[-100:]:
                torrent.handle.connect_peer(endpoint)
            for method in ('force_reannounce', 'force_dht_announce'):
                if hasattr(torrent.handle, method):
                    getattr(torrent.handle, method)()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ disk window
    def _keep(self, torrent, now):
        """Pieces worth keeping on disk: headers and a window around every recent position."""
        plen = torrent.piece_length
        _, win_n = self._window(torrent)
        behind_n = math.ceil(torrent.bitrate * self.config.keep_behind_seconds / plen)
        keep = set()
        for view in torrent.views.values():
            for part in self._header_pieces(view, plen):
                keep.update(part)
        for owner, (file_id, position, seen) in list(torrent.recent.items()):
            if now - seen > self.config.seek_grace:     # nobody reads there any more
                del torrent.recent[owner]
                continue
            view = torrent.views.get(file_id)
            if view is not None:
                start = (view.offset + position) // plen
                # ahead: up to three windows — what libtorrent fetched a bit early is needed in a moment
                keep.update(range(max(view.first_piece, start - behind_n), min(view.last_piece, start + 3 * win_n) + 1))
        return keep

    def _calm(self, torrent, now):
        """Every viewer already has the part it asked for on disk: nothing urgent is downloading."""
        crit_n, win_n = self._window(torrent)
        for file_id, position, weight in self._positions(torrent, now):
            view = torrent.views.get(file_id)
            if view is None:
                continue
            span = win_n if weight >= 1 else max(crit_n, win_n // 2)     # what _reprioritize asks for
            start = (view.offset + position) // torrent.piece_length
            end = min(view.last_piece, start + span - 1)
            if torrent.have[start:end + 1].count(0):
                return False
        return True

    def _maybe_trim(self, torrent, now):
        """Release watched pieces: the torrent is re-added without them (see _finish_trim).

        Re-adding drops every peer connection, and peers unchoke us again only after 10-60 s,
        so it happens rarely (big steps) and in calm moments, when the read-ahead is on disk.
        There is no safe way to release in place: libtorrent cannot forget a piece, it would
        keep offering the holes to other peers (tested: add_piece / upload limits / unchoke
        slots either upload zeros or stall our own download)."""
        keep = self._keep(torrent, now)
        drop = [p for p, had in enumerate(torrent.have) if had and p not in keep]
        size = len(drop) * torrent.piece_length
        if not drop or size < self.config.trim_step:
            return
        if size < self.config.trim_force and not self._calm(torrent, now):
            return          # the window is still downloading: wait for a calm moment
        try:   # reconnect the same peers right after re-adding: no wait for trackers / DHT
            peers = [tuple(peer.ip) for peer in torrent.handle.get_peer_info()][:200]
        except Exception:  # noqa: BLE001
            peers = []
        torrent.trim = dict(drop=drop, stage='saving', since=now, resume=None, peers=peers)
        try:
            torrent.handle.pause()
            torrent.handle.save_resume_data(self._resume_flags())
        except Exception:  # noqa: BLE001
            log.exception('cannot start releasing disk for %s', torrent.key)
            self._abort_trim(torrent)

    def _drop_from_params(self, torrent, params):
        have = [bool(x) for x in (getattr(params, 'have_pieces', None) or [])]
        if len(have) != torrent.num_pieces:      # seeds may report an empty bitfield
            have = [bool(x) for x in torrent.have]
        for piece in torrent.trim['drop']:
            have[piece] = False
        params.have_pieces = have

    def _remove_for_trim(self, torrent, resume):
        torrent.trim.update(stage='removing', since=time.monotonic(), resume=resume)
        try:
            self._session.remove_torrent(torrent.handle)
        except Exception:  # noqa: BLE001
            log.exception('cannot remove torrent %s to release disk', torrent.key)
            self._finish_trim(torrent)

    def _abort_trim(self, torrent):
        torrent.trim = None
        torrent.trim_after = time.monotonic() + 60
        torrent.dirty = True
        try:
            torrent.handle.resume()
        except Exception:  # noqa: BLE001
            pass
        torrent.condition.notify_all()

    def _hole_ranges(self, torrent, drop):
        """File-relative byte ranges of the dropped pieces, merged, for every selected file."""
        plen = torrent.piece_length
        spans = []
        for piece in sorted(drop):
            start, end = piece * plen, piece * plen + torrent.info.piece_size(piece)
            if spans and spans[-1][1] == start:
                spans[-1][1] = end
            else:
                spans.append([start, end])
        result = {}
        for view in torrent.views.values():
            ranges = [(max(a, view.offset) - view.offset, min(b, view.offset + view.size) - view.offset)
                      for a, b in spans if a < view.offset + view.size and b > view.offset]
            if ranges:
                result[view.path] = ranges
        return result

    def _punch(self, torrent, drop):
        freed = 0
        for path, ranges in self._hole_ranges(torrent, drop).items():
            for attempt in range(20):
                try:
                    if not path.exists():
                        break
                    if not punch_holes(path, ranges):
                        torrent.trim_disabled = True
                        log.warning('На этой ОС нельзя освобождать место в файле: фильм будет храниться целиком.')
                        break
                    freed += sum(b - a for a, b in ranges)
                    break
                except OSError as exc:
                    if _hole_unsupported(exc):
                        torrent.trim_disabled = True
                        log.warning('Файловая система кэша не поддерживает разреженные файлы: фильм будет храниться целиком.')
                        break
                    if attempt == 19:
                        log.warning('Не удалось освободить место в %s: %s', path, exc)
                    else:
                        time.sleep(0.05)        # libtorrent may close the file a moment later
        return freed

    def _finish_trim(self, torrent):
        """The handle is gone: punch holes, then add the torrent again without the dropped pieces."""
        trim = torrent.trim
        if trim is None or trim['stage'] != 'removing':
            return
        trim['stage'] = 'adding'
        drop = trim['drop']
        freed = self._punch(torrent, drop)
        try:
            params = self._lt.read_resume_data(trim['resume'])
            params.ti = torrent.info
            params.save_path = str(torrent.save_path)
            if hasattr(params, 'piece_priorities'):
                params.piece_priorities = []
            params.flags &= ~self._flag('paused')
            params.flags &= ~self._flag('auto_managed')
            params.flags &= ~self._flag('upload_mode')
            torrent.handle = self._session.add_torrent(params)
        except Exception as exc:  # noqa: BLE001
            log.exception('cannot add torrent %s back', torrent.key)
            torrent.error = 'Ошибка раздачи: ' + str(exc)
            torrent.trim = None
            torrent.condition.notify_all()
            self._begin_close(torrent)
            return
        for piece in drop:
            torrent.have[piece] = 0
        torrent.applied.clear()
        torrent.deadlines.clear()
        torrent.trim = None
        torrent.trim_after = time.monotonic() + 10
        self._apply_views(torrent)
        torrent.check_priorities = True
        torrent.reconnected = 0.0
        for endpoint in trim.get('peers', []):
            try:
                torrent.handle.connect_peer(endpoint)
            except Exception:  # noqa: BLE001
                pass
        for method in ('force_reannounce', 'force_dht_announce'):   # more peers right away, not in minutes
            try:
                getattr(torrent.handle, method)()
            except Exception:  # noqa: BLE001
                pass
        for piece in list(torrent.read_pending):
            if torrent.have[piece]:
                torrent.handle.read_piece(piece)
            else:
                torrent.read_pending.discard(piece)
                torrent.read_errors[piece] = 'Часть видео выгружена с диска. Повторите воспроизведение.'
        torrent.dirty = True
        self._reprioritize(torrent)
        torrent.condition.notify_all()
        self._touch_meta(torrent)
        name = next(iter(torrent.views.values())).name if torrent.views else torrent.key
        log.info('Кэш «%s»: освобождено %d МиБ', name, freed // MiB)
        if torrent.close_requested:
            torrent.close_requested = False
            self._begin_close(torrent)

    def _resume_flags(self):
        return int(getattr(self._lt.torrent_handle, 'save_info_dict', 0)) | int(getattr(self._lt.torrent_handle, 'flush_disk_cache', 0))

    def _begin_close(self, torrent):
        if torrent.trim:
            torrent.close_requested = True
            return
        torrent.closing = True
        torrent.last_used = time.monotonic()
        torrent.condition.notify_all()
        try:
            torrent.handle.pause()
            torrent.handle.save_resume_data(self._resume_flags())
        except Exception:  # noqa: BLE001
            self._finalize_close(torrent)

    def _finalize_close(self, torrent):
        if torrent.closed:
            return
        torrent.closed = True
        torrent.condition.notify_all()
        self._touch_meta(torrent)
        if self.torrents.get(torrent.key) is torrent:
            del self.torrents[torrent.key]
        try:
            self._session.remove_torrent(torrent.handle)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self.enforce_cache_limit, daemon=True).start()

    def close_torrent(self, key):
        with self._lock:
            torrent = self.torrents.get(key)
            if torrent is not None:
                for reader in torrent.readers.values():
                    reader.cancelled = True
                self._begin_close(torrent)

    # ------------------------------------------------------------------ disk cache
    def _touch_meta(self, torrent):
        try:
            have = sum(torrent.have)
            meta = dict(last_used=time.time(), bytes=have * torrent.piece_length,
                        name=next(iter(torrent.views.values())).name if torrent.views else torrent.key)
            (self.config.cache_dir / f'{torrent.key}.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass

    def cache_usage(self):
        root = self.config.cache_dir
        entries = []
        if root.is_dir():
            for meta in root.glob('*.json'):
                key = meta.stem
                if not re.fullmatch(r'[0-9a-f]{40}', key):
                    continue
                try:
                    data = json.loads(meta.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    data = {}
                active = key in self.torrents
                size = data.get('bytes', 0)
                if active:
                    torrent = self.torrents[key]
                    size = sum(torrent.have) * torrent.piece_length
                entries.append(dict(key=key, name=data.get('name', key), bytes=int(size), last_used=data.get('last_used', 0), active=active))
        entries.sort(key=lambda e: e['last_used'], reverse=True)
        return dict(limit=self.config.cache_limit, used=sum(e['bytes'] for e in entries), entries=entries)

    def enforce_cache_limit(self):
        usage = self.cache_usage()
        used = usage['used']
        for entry in sorted(usage['entries'], key=lambda e: e['last_used']):
            if used <= self.config.cache_limit:
                break
            if entry['active']:
                continue
            self.evict(entry['key'])
            used -= entry['bytes']

    def evict(self, key):
        if not re.fullmatch(r'[0-9a-f]{40}', key or ''):
            raise ValueError('Некорректный ключ кэша.')
        with self._lock:
            if key in self.torrents:
                raise ValueError('Фильм сейчас смотрят — кэш нельзя удалить.')
        root = self.config.cache_dir
        shutil.rmtree(root / key, ignore_errors=True)
        for suffix in ('.resume', '.json'):
            try:
                (root / f'{key}{suffix}').unlink()
            except OSError:
                pass

    def close(self):
        with self._lock:
            if self._session is None:
                self._closed.set()
                return
            for torrent in list(self.torrents.values()):
                for reader in torrent.readers.values():
                    reader.cancelled = True
                if not torrent.closing:
                    self._begin_close(torrent)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            with self._lock:
                if not self.torrents:
                    break
            time.sleep(0.1)
        with self._lock:
            try:
                if hasattr(self._lt, 'write_session_params_buf'):
                    data = self._lt.write_session_params_buf(self._session.session_state())
                    (self.config.cache_dir / 'session.state').write_bytes(data)
            except Exception:  # noqa: BLE001
                pass
            for torrent in list(self.torrents.values()):
                self._finalize_close(torrent)
            self._closed.set()
            try:
                self._session.pause()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._session = None
