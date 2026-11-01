"""Smoke test of the streaming engine with the REAL libtorrent (run it on your computer).

    .venv/Scripts/python.exe tests/smoke_engine.py

A second, local libtorrent session seeds tests/fixtures/torrent-test.mp4. The
engine connects to it directly, streams the file in random-access order (like a
browser that seeks) and checks every byte. Also verifies resume from the disk cache
and the sliding disk window: a 96 MiB film streamed start to end keeps only the part
around the viewer on disk. No public network is needed.
"""
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import libtorrent as lt  # noqa: E402

from torrent_stream import EngineConfig, StreamEngine  # noqa: E402
import fakelt  # noqa: E402  (make_torrent only)
from test_torrent_stream import allocated  # noqa: E402

MiB = 1024 * 1024

FIXTURES = ROOT / 'tests' / 'fixtures'


def main():
    data = (FIXTURES / 'torrent-test.torrent').read_bytes()
    payload = (FIXTURES / 'torrent-test.mp4').read_bytes()
    seeder = lt.session({'listen_interfaces': '127.0.0.1:0', 'enable_dht': False, 'enable_lsd': False,
                         'enable_upnp': False, 'enable_natpmp': False})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(lt.bdecode(data))
    params.save_path = str(FIXTURES)
    params.flags |= lt.torrent_flags.seed_mode
    seeder.add_torrent(params)
    port = seeder.listen_port()
    print(f'libtorrent {lt.__version__}, local seeder on port {port}')

    temp = Path(tempfile.mkdtemp(prefix='kinosfera-smoke-'))
    engine = StreamEngine(EngineConfig(temp / 'cache', listen_port=0, extra_trackers=False, piece_timeout=30))
    try:
        started = time.perf_counter()
        torrent = engine.open(data, 0)
        engine.add_peer(torrent, ('127.0.0.1', port))
        first = b''.join(engine.read(torrent, 0, 0, 64 * 1024 - 1, 'smoke'))
        print(f'first 64 KiB after {time.perf_counter() - started:.2f} s')
        assert first == payload[:64 * 1024], 'head bytes differ'
        size = len(payload)
        rng = random.Random(7)
        for _ in range(25):                     # seeks like a browser
            start = rng.randrange(0, size - 1)
            end = min(size - 1, start + rng.randrange(1, 200_000))
            chunk = b''.join(engine.read(torrent, 0, start, end, 'smoke'))
            assert chunk == payload[start:end + 1], f'bytes {start}-{end} differ'
        whole = b''.join(engine.read(torrent, 0, 0, size - 1, 'smoke'))
        assert whole == payload, 'complete file differs'
        stats = engine.stats(torrent, 0)
        print(f'all bytes verified in {time.perf_counter() - started:.2f} s; stats: {stats}')

        engine.close_torrent(torrent.key)
        deadline = time.time() + 10
        while torrent.key in engine.torrents and time.time() < deadline:
            time.sleep(0.1)
        assert (temp / 'cache' / f'{torrent.key}.resume').exists(), 'resume data not saved'
        again = engine.open(data, 0)
        time.sleep(1.5)
        reopened = b''.join(engine.read(again, 0, size - 1000, size - 1, 'smoke'))
        assert reopened == payload[-1000:]
        print('resume from cache: OK')
        print('SMOKE TEST PASSED')
    finally:
        engine.close()
        shutil.rmtree(temp, ignore_errors=True)


def window_check():
    temp = Path(tempfile.mkdtemp(prefix='kinosfera-window-'))
    seed = temp / 'seed'
    seed.mkdir()
    payload = os.urandom(96 * MiB)
    (seed / 'movie.mp4').write_bytes(payload)
    data = fakelt.make_torrent([seed / 'movie.mp4'], seed, piece_length=512 * 1024)
    seeder = lt.session({'listen_interfaces': '127.0.0.1:0', 'enable_dht': False, 'enable_lsd': False,
                         'enable_upnp': False, 'enable_natpmp': False, 'upload_rate_limit': 12 * MiB})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(lt.bdecode(data))
    params.save_path = str(seed)
    params.flags |= lt.torrent_flags.seed_mode
    seeder.add_torrent(params)
    engine = StreamEngine(EngineConfig(temp / 'cache', listen_port=0, extra_trackers=False, piece_timeout=30,
                                       min_window=8 * MiB, readahead_seconds=4, keep_behind_seconds=4, trim_step=16 * MiB))
    try:
        torrent = engine.open(data, 0)
        engine.add_peer(torrent, ('127.0.0.1', seeder.listen_port()))
        path, peak = torrent.views[0].path, 0
        for position in range(0, len(payload), 4 * MiB):        # "watch" the film start to end
            end = min(len(payload), position + 4 * MiB) - 1
            assert b''.join(engine.read(torrent, 0, position, end, 'viewer')) == payload[position:end + 1]
            engine.hint(torrent, 'viewer', 0, end)
            peak = max(peak, allocated(path))
            time.sleep(0.4)
        time.sleep(6)
        print(f'window: peak on disk {peak / MiB:.0f} MiB, after the film {allocated(path) / MiB:.0f} MiB of 96 MiB')
        assert allocated(path) < 48 * MiB, 'watched part was not released'
        again = b''.join(engine.read(torrent, 0, 10 * MiB, 12 * MiB, 'viewer'))
        assert again == payload[10 * MiB:12 * MiB + 1], 'released part downloaded again differs'
        print('window: rewind into a released part downloads it again: OK')
        engine.hint(torrent, 'viewer', 0, 12 * MiB)
        time.sleep(2)
        old = (11 * MiB) // torrent.piece_length
        jump = b''.join(engine.read(torrent, 0, 70 * MiB, 72 * MiB, 'viewer'))  # seek far ahead
        assert jump == payload[70 * MiB:72 * MiB + 1]
        started = time.time()
        while torrent.have[old] and time.time() - started < 30:
            engine.hint(torrent, 'viewer', 0, 72 * MiB)
            time.sleep(0.5)
        assert not torrent.have[old], 'the place left by a seek stays on disk'
        print(f'window: place left by a seek released after {time.time() - started:.0f} s '
              f'(grace {engine.config.seek_grace:.0f} s); on disk {allocated(path) / MiB:.0f} MiB')
    finally:
        engine.close()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == '__main__':
    main()
    window_check()
    print('WINDOW CHECK PASSED')
