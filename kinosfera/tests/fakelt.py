"""In-process emulator of the libtorrent API surface used by torrent_stream.StreamEngine.

It "downloads" pieces from local seed files at a configurable speed, honouring piece
priorities and deadlines like libtorrent's picker does, writes them to the save path
(sparse files, like libtorrent) and posts the same alerts. Used by offline tests only.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
import types

import bencode
import torrent_meta


class _Flags:
    paused = 1 << 0
    auto_managed = 1 << 1
    upload_mode = 1 << 2
    seed_mode = 1 << 3
    default_flags = 0


class _Category:
    error = 1; storage = 2; status = 4; piece_progress = 8; tracker = 16; port_mapping = 32  # noqa: E702


class _Hash:
    def __init__(self, raw):
        self.raw = raw


class _Decoded(dict):
    def __init__(self, data):
        super().__init__(bencode.decode(data))
        self.data = data


class file_storage:
    flag_pad_file = 1
    flag_symlink = 2

    def __init__(self, meta):
        self._files = meta['all_files']

    def num_files(self):
        return len(self._files)

    def file_offset(self, i):
        return self._files[i]['offset']

    def file_size(self, i):
        return self._files[i]['size']

    def file_flags(self, i):
        return self.flag_pad_file if self._files[i]['pad'] else 0

    def file_path(self, i, save_path=''):
        name = self._files[i]['name']
        return os.path.join(save_path, *name.split('/')) if save_path else name


class torrent_info:
    def __init__(self, decoded):
        data = decoded.data if isinstance(decoded, _Decoded) else decoded
        self.data = data
        self.meta = torrent_meta.parse_torrent(data)
        self._fs = file_storage(self.meta)

    def files(self):
        return self._fs

    def num_pieces(self):
        return self.meta['num_pieces']

    def piece_length(self):
        return self.meta['piece_length']

    def piece_size(self, piece):
        if piece == self.num_pieces() - 1:
            return self.meta['total_size'] - piece * self.piece_length()
        return self.piece_length()

    def hash_for_piece(self, piece):
        return self.meta['piece_hashes'][piece * 20:piece * 20 + 20]

    def info_section(self):
        return self.meta['raw_info']

    def total_size(self):
        return self.meta['total_size']


class add_torrent_params:
    def __init__(self):
        self.ti = None
        self.save_path = ''
        self.file_priorities = []
        self.flags = 0
        self.info_hash = None
        self.trackers = []
        self.have_pieces = []
        self.piece_priorities = []


class _Status:
    pass


class _Alert:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def message(self):
        return self.__dict__.get('msg', '')


def _alert_class(name):
    return type(name, (_Alert,), {})


piece_finished_alert = _alert_class('piece_finished_alert')
read_piece_alert = _alert_class('read_piece_alert')
save_resume_data_alert = _alert_class('save_resume_data_alert')
metadata_received_alert = _alert_class('metadata_received_alert')
torrent_removed_alert = _alert_class('torrent_removed_alert')


class _Error:
    def __init__(self, value=0):
        self._v = value

    def value(self):
        return self._v


class torrent_handle:
    query_pieces = 1
    save_info_dict = 2
    flush_disk_cache = 4

    def __init__(self, session, params):
        self.s = session
        self.ti = params.ti
        self.save_path = params.save_path
        self.paused = bool(params.flags & _Flags.paused)
        self.trackers_list = []
        self.removed = False
        self.metadata_hash = params.info_hash
        if self.ti is not None:
            self._init_pieces(params)

    def _init_pieces(self, params=None):
        n = self.ti.num_pieces()
        self.have = [False] * n
        self.prio = [4] * n
        self.deadline = {}
        if params is not None and params.have_pieces:
            self.have = [bool(x) for x in params.have_pieces][:n] + [False] * max(0, n - len(params.have_pieces))
        fprio = (params.file_priorities if params is not None else None) or [4] * self.ti.files().num_files()
        self.fprio = list(fprio)
        self._apply_file_prio()

    def _apply_file_prio(self):
        fs = self.ti.files()
        plen = self.ti.piece_length()
        self.prio = [0] * self.ti.num_pieces()
        for i in range(fs.num_files()):
            if fs.file_size(i) == 0:
                continue
            first = fs.file_offset(i) // plen
            last = (fs.file_offset(i) + fs.file_size(i) - 1) // plen
            for p in range(first, last + 1):
                self.prio[p] = max(self.prio[p], self.fprio[i])

    # --- API
    def is_valid(self):
        return not self.removed

    def have_piece(self, p):
        with self.s.lock:
            return bool(self.ti and self.have[p])

    def piece_priority(self, p, prio=None):
        with self.s.lock:
            if prio is None:
                return self.prio[p]
            self.prio[p] = prio
            self.s.priority_calls += 1

    def prioritize_pieces(self, prios):
        with self.s.lock:
            self.prio = list(prios)

    def get_piece_priorities(self):
        with self.s.lock:
            return list(self.prio)

    def file_priority(self, i, prio=None):
        with self.s.lock:
            if prio is None:
                return self.fprio[i]
            self.fprio[i] = prio
            fs = self.ti.files()
            plen = self.ti.piece_length()
            for p in range(fs.file_offset(i) // plen, (fs.file_offset(i) + fs.file_size(i) - 1) // plen + 1):
                self.prio[p] = max(self.prio[p], prio) if prio else self.prio[p]

    def set_piece_deadline(self, p, ms, flags=0):
        with self.s.lock:
            self.deadline[p] = time.monotonic() + ms / 1000
            if self.prio[p] == 0:
                self.prio[p] = 7

    def reset_piece_deadline(self, p):
        with self.s.lock:
            self.deadline.pop(p, None)

    def clear_piece_deadlines(self):
        with self.s.lock:
            self.deadline.clear()

    def resume(self):
        if self.paused and not getattr(self, 'loaded', False) and self.ti is not None:
            self.loaded = True
            def loaded():     # like libtorrent after checking resume data: file priorities win
                with self.s.lock:
                    self._apply_file_prio()
            threading.Timer(0.05, loaded).start()
        self.paused = False

    def pause(self, flags=0):
        self.paused = True

    def add_tracker(self, entry):
        self.trackers_list.append(dict(entry))

    def trackers(self):
        return list(self.trackers_list)

    def torrent_file(self):
        return self.ti

    def read_piece(self, p):
        def deliver():
            data = self.s.piece_bytes(self.ti, p)
            self.s.post(read_piece_alert(handle=self, piece=p, buffer=data, error=_Error(0)))
        threading.Timer(0.01, deliver).start()

    def save_resume_data(self, flags=0):
        params = add_torrent_params()
        params.have_pieces = list(self.have)
        self.s.post(save_resume_data_alert(handle=self, params=params))

    def status(self, flags=0):
        st = _Status()
        with self.s.lock:
            st.num_peers = 0 if self.paused else self.s.peers
            st.num_seeds = st.num_peers
            st.download_payload_rate = self.s.rate_now.get(id(self), 0)
            st.upload_payload_rate = 0
            st.state = 'downloading'
            st.has_metadata = self.ti is not None
            st.pieces = list(self.have) if self.ti is not None else []
        return st

    def connect_peer(self, endpoint, source=0):
        self.s.peers += 1


    def get_peer_info(self):
        return []


class session:
    def __init__(self, settings=None):
        self.lock = threading.RLock()
        self.settings = dict(settings or {})
        self.alerts = []
        self.alert_event = threading.Event()
        self.handles = []
        self.rate_now = {}
        self.priority_calls = 0
        self.peers = FAKE.peers
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def apply_settings(self, settings):
        self.settings.update(settings)

    def post(self, alert):
        with self.lock:
            self.alerts.append(alert)
            self.alert_event.set()

    def wait_for_alert(self, ms):
        if self.alert_event.wait(ms / 1000):
            return True
        return None

    def pop_alerts(self):
        with self.lock:
            alerts, self.alerts = self.alerts, []
            self.alert_event.clear()
            return alerts

    def add_torrent(self, params):
        handle = torrent_handle(self, params)
        if params.ti is None and params.info_hash:
            data = FAKE.magnets.get(params.info_hash)

            def resolve():
                if data is not None:
                    handle.ti = torrent_info(_Decoded(data))
                    handle._init_pieces()
                    self.post(metadata_received_alert(handle=handle))
            threading.Timer(0.2, resolve).start()
        with self.lock:
            self.handles.append(handle)
        FAKE.added.append(handle)
        return handle

    def remove_torrent(self, handle, option=0):
        with self.lock:
            handle.removed = True
            if handle in self.handles:
                self.handles.remove(handle)
        self.post(torrent_removed_alert(handle=handle))

    def session_state(self, flags=0):
        return {}

    def pause(self):
        self.closed = True

    def piece_bytes(self, ti, piece):
        seed = FAKE.seeds[ti.meta['info_hash']]
        start = piece * ti.piece_length()
        size = ti.piece_size(piece)
        out = bytearray()
        for f in ti.meta['all_files']:
            lo, hi = f['offset'], f['offset'] + f['size']
            if hi <= start or lo >= start + size or f['size'] == 0:
                continue
            a, b = max(lo, start), min(hi, start + size)
            if f['pad']:
                out += bytes(b - a)
            else:
                with open(Path(seed) / f['name'], 'rb') as fh:
                    fh.seek(a - lo)
                    out += fh.read(b - a)
        return bytes(out)

    def _write(self, handle, piece, data):
        ti = handle.ti
        start = piece * ti.piece_length()
        for i, f in enumerate(ti.meta['all_files']):
            lo, hi = f['offset'], f['offset'] + f['size']
            if hi <= start or lo >= start + len(data) or f['pad'] or handle.fprio[i] == 0:
                continue
            a, b = max(lo, start), min(hi, start + len(data))
            path = Path(handle.save_path) / f['name']
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = 'r+b' if path.exists() else 'w+b'
            with open(path, mode) as fh:
                fh.seek(a - lo)
                fh.write(data[a - start:b - start])

    def _pick(self, handle):
        best = None
        now = time.monotonic()
        for p, prio in enumerate(handle.prio):
            if prio <= 0 or handle.have[p]:
                continue
            deadline = handle.deadline.get(p)
            key = (0 if deadline is not None else 1, deadline or 0, -prio, p)
            if best is None or key < best[0]:
                best = (key, p)
        return None if best is None else best[1]

    def _run(self):
        progress = {}
        last = time.monotonic()
        while not self.closed:
            time.sleep(0.02)
            now = time.monotonic()
            dt = max(0.001, now - last)
            budget = FAKE.speed * dt
            last = now
            with self.lock:
                active = [h for h in self.handles if h.ti is not None and not h.paused and not h.removed]
            if not active:
                continue
            share = budget / len(active)
            for handle in active:
                spent = 0
                got = 0
                while spent < share:
                    with self.lock:
                        piece = self._pick(handle)
                    if piece is None:
                        break
                    size = handle.ti.piece_size(piece)
                    have = progress.get((id(handle), piece), 0) + (share - spent)
                    if have < size:
                        progress[(id(handle), piece)] = have
                        spent = share
                        got += share
                        break
                    spent += size - progress.pop((id(handle), piece), 0)
                    got += size
                    data = self.piece_bytes(handle.ti, piece)
                    assert hashlib.sha1(data).digest() == handle.ti.hash_for_piece(piece)
                    self._write(handle, piece, data)
                    with self.lock:
                        handle.have[piece] = True
                        handle.deadline.pop(piece, None)
                    FAKE.downloaded.append(piece)
                    self.post(piece_finished_alert(handle=handle, piece_index=piece))
                self.rate_now[id(handle)] = int(0.8 * self.rate_now.get(id(handle), 0) + 0.2 * got / dt)


def bdecode(data):
    return _Decoded(data)


def bencode_(value):
    return bencode.encode(value)


def read_resume_data(buf):
    data = bencode.decode(buf)
    params = add_torrent_params()
    params.have_pieces = [bool(x) for x in data.get(b'have', [])]
    return params


def write_resume_data_buf(params):
    return bencode.encode({b'have': [int(x) for x in (params.have_pieces or [])]})


def parse_magnet_uri(uri):
    params = add_torrent_params()
    params.info_hash = torrent_meta.magnet_info(uri)['info_hash']
    return params


def write_session_params_buf(params):
    return b'state'


def read_session_params(buf):
    return {}


FAKE = types.SimpleNamespace(seeds={}, magnets={}, speed=8 * 1024 * 1024, peers=5, added=[], downloaded=[])


def module():
    """A module-like object to pass as ``StreamEngine(lt_module=...)``."""
    ns = types.SimpleNamespace(
        session=session, torrent_info=torrent_info, add_torrent_params=add_torrent_params,
        bdecode=bdecode, bencode=bencode_, torrent_flags=_Flags, alert_category=_Category,
        torrent_handle=torrent_handle, file_storage=file_storage,
        options_t=types.SimpleNamespace(delete_files=1), read_resume_data=read_resume_data,
        write_resume_data_buf=write_resume_data_buf, parse_magnet_uri=parse_magnet_uri,
        write_session_params_buf=write_session_params_buf, read_session_params=read_session_params,
    )
    return ns


def make_torrent(paths, root, piece_length=256 * 1024, name=None, trackers=()):
    """Create a v1 .torrent for files under ``root`` (single or multi-file)."""
    root = Path(root)
    paths = [Path(p) for p in paths]
    blob = b''.join(p.read_bytes() for p in paths)
    pieces = b''.join(hashlib.sha1(blob[i:i + piece_length]).digest() for i in range(0, len(blob), piece_length))
    if len(paths) == 1 and name is None:
        info = {b'name': paths[0].name.encode(), b'length': len(blob), b'piece length': piece_length, b'pieces': pieces}
    else:
        info = {b'name': (name or root.name).encode(), b'piece length': piece_length, b'pieces': pieces,
                b'files': [{b'length': p.stat().st_size, b'path': [part.encode() for part in p.relative_to(root).parts]} for p in paths]}
    data = {b'info': info}
    if trackers:
        data[b'announce'] = trackers[0].encode()
    return bencode.encode(data)
