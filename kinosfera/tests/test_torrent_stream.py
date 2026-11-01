"""Streaming engine: HTTP ranges, piece scheduling, shared torrents, cache and magnets (offline)."""
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

import support  # noqa: F401  (paths, emulator)
import fakelt
import torrent_meta
from torrent_stream import EngineConfig, StreamEngine, StreamError, parse_range, punch_holes

MiB = 1024 * 1024


def allocated(path):
    """Bytes the file really occupies on disk (sparse holes excluded)."""
    if os.name == 'nt':
        high = ctypes.c_ulong()
        low = ctypes.windll.kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
        return (high.value << 32) + low
    return os.stat(path).st_blocks * 512


class RangeParsingTests(unittest.TestCase):
    def test_missing_range_selects_the_complete_file(self):
        self.assertEqual(parse_range(None, 100), (0, 99))

    def test_single_ranges_are_inclusive_and_support_open_ends(self):
        cases = {'bytes=0-0': (0, 0), 'bytes=0-99': (0, 99), 'bytes=13-27': (13, 27), 'bytes=13-': (13, 99),
                 'bytes=99-': (99, 99), 'bytes=-1': (99, 99), 'bytes=-25': (75, 99), 'bytes=-100': (0, 99),
                 'bytes=-150': (0, 99), 'bytes=75-150': (75, 99)}
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_range(header, 100), expected)

    def test_invalid_or_unsatisfiable_ranges_are_rejected(self):
        for header in ('bytes=100-', 'bytes=100-101', 'bytes=40-39', 'bytes=-0', 'bytes=', 'bytes=-', 'bytes=abc-def',
                       'items=0-1', 'bytes=0', 'bytes=0-1,4-6', 'bytes=--5', 'bytes=1--2'):
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    parse_range(header, 100)

    def test_empty_file_cannot_satisfy_a_byte_range(self):
        for header in ('bytes=0-0', 'bytes=0-', 'bytes=-1'):
            with self.assertRaises(ValueError):
                parse_range(header, 0)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / 'Pack'
        (root / 'extras').mkdir(parents=True)
        (root / 'movie.mkv').write_bytes(os.urandom(300_000))
        (root / 'extras' / 'trailer.mp4').write_bytes(os.urandom(50_000))
        (root / 'readme.txt').write_bytes(b'hello')
        self.files = [root / 'movie.mkv', root / 'extras' / 'trailer.mp4', root / 'readme.txt']
        self.data = fakelt.make_torrent(self.files, root, piece_length=32 * 1024, name='Pack', trackers=['udp://t.example:1/a'])

    def tearDown(self):
        self.temp.cleanup()

    def test_multi_file_torrent_is_parsed_and_the_largest_video_selected(self):
        meta = torrent_meta.parse_torrent(self.data)
        self.assertEqual([f['name'] for f in meta.files], ['Pack/movie.mkv', 'Pack/extras/trailer.mp4', 'Pack/readme.txt'])
        self.assertEqual(meta.trackers, ['udp://t.example:1/a'])
        files, best = torrent_meta.inspect_torrent(self.data)
        self.assertEqual(best, 0)
        self.assertFalse(next(f for f in files if f['name'].endswith('.txt'))['supported'])

    def test_rebuilt_torrent_keeps_the_info_hash(self):
        meta = torrent_meta.parse_torrent(self.data)
        rebuilt = torrent_meta.build_torrent(meta.raw_info, ['udp://other.example:2/a'])
        self.assertEqual(torrent_meta.info_hash(rebuilt), meta.info_hash)

    def test_unsafe_or_broken_metadata_is_rejected(self):
        import bencode
        for bad in (b'', b'not a torrent', b'd4:infod4:name3:../e' + b'e'):
            with self.assertRaises(ValueError):
                torrent_meta.parse_torrent(bad)
        evil = bencode.encode({b'info': {b'name': b'x', b'piece length': 16384, b'pieces': b'\0' * 20,
                                         b'files': [{b'length': 1, b'path': [b'..', b'evil.mp4']}]}})
        with self.assertRaises(ValueError):
            torrent_meta.parse_torrent(evil)

    def test_magnet_validation(self):
        good = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Film&tr=udp%3A%2F%2Ft.example%3A1'
        self.assertEqual(torrent_meta.magnet_info(good)['info_hash'], 'a' * 40)
        for bad in ('http://x', 'magnet:?dn=x', 'magnet:?xt=urn:btih:' + 'a' * 40 + '&xs=http://evil'):
            with self.assertRaises(ValueError):
                torrent_meta.validate_magnet(bad)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.seed = root / 'seed'
        self.seed.mkdir()
        self.file = self.seed / 'movie.mp4'
        self.payload = os.urandom(4 * MiB + 12345)
        self.file.write_bytes(self.payload)
        self.data = fakelt.make_torrent([self.file], self.seed, piece_length=256 * 1024)
        fakelt.FAKE.seeds[torrent_meta.parse_torrent(self.data).info_hash] = self.seed
        fakelt.FAKE.speed = 6 * MiB
        fakelt.FAKE.downloaded.clear()
        fakelt.FAKE.added.clear()
        self.engine = StreamEngine(EngineConfig(root / 'cache', background=False, min_window=MiB, piece_timeout=10),
                                   lt_module=fakelt.module())

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_reads_are_byte_exact_and_verified(self):
        torrent = self.engine.open(self.data, 0)
        out = b''.join(self.engine.read(torrent, 0, 1000, 2_000_000, 'viewer'))
        self.assertEqual(out, self.payload[1000:2_000_001])
        tail = b''.join(self.engine.read(torrent, 0, len(self.payload) - 10, len(self.payload) - 1, 'viewer'))
        self.assertEqual(tail, self.payload[-10:])

    def test_head_and_tail_are_fetched_first(self):
        fakelt.FAKE.speed = 1 * MiB
        torrent = self.engine.open(self.data, 0)
        deadline = time.time() + 5
        while len(fakelt.FAKE.downloaded) < 3 and time.time() < deadline:
            time.sleep(0.05)
        first = fakelt.FAKE.downloaded[:3]
        self.assertIn(0, first)
        self.assertIn(torrent.num_pieces - 1, fakelt.FAKE.downloaded[:4])

    def test_one_torrent_is_shared_by_every_viewer(self):
        a = self.engine.open(self.data, 0)
        b = self.engine.open(self.data, 0)
        self.assertIs(a, b)
        self.assertEqual(len(fakelt.FAKE.added), 1)

    def test_seek_moves_deadlines_to_the_new_position(self):
        fakelt.FAKE.speed = 64 * 1024          # slow swarm: the read waits
        torrent = self.engine.open(self.data, 0)
        reader = self.engine.read(torrent, 0, 3 * MiB, 3 * MiB + 10, 'viewer')
        import threading

        def consume():
            try:
                list(reader)
            except StreamError:
                pass
        threading.Thread(target=consume, daemon=True).start()
        time.sleep(0.3)
        target = (3 * MiB) // torrent.piece_length
        self.assertIn(target, torrent.deadlines)
        handle = fakelt.FAKE.added[0]
        self.assertEqual(handle.prio[target], 7)

    def test_without_background_mode_far_pieces_are_not_downloaded(self):
        fakelt.FAKE.speed = 32 * MiB
        torrent = self.engine.open(self.data, 0)
        b''.join(self.engine.read(torrent, 0, 0, 1000, 'viewer'))
        time.sleep(0.5)
        handle = fakelt.FAKE.added[0]
        middle = torrent.num_pieces // 2
        self.assertEqual(handle.prio[middle], 0)

    def test_superseded_reader_is_cancelled_on_far_seek(self):
        fakelt.FAKE.speed = 1024
        self.engine.config.readahead_seconds = 1
        torrent = self.engine.open(self.data, 0)
        self.engine.set_bitrate(torrent, 1)          # tiny window: piece 16 is "far"
        old = self.engine.read(torrent, 0, 4 * MiB, 4 * MiB + 1, 'viewer')
        import threading
        errors = []

        def consume():
            try:
                list(old)
            except StreamError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.4)
        self.engine.read(torrent, 0, 0, 10, 'viewer')
        thread.join(3)
        self.assertTrue(errors and 'перемотал' in errors[0])

    def test_buffered_ahead_and_stats(self):
        torrent = self.engine.open(self.data, 0)
        b''.join(self.engine.read(torrent, 0, 0, 2 * MiB, 'viewer'))
        self.assertGreaterEqual(self.engine.buffered_ahead(torrent, 0, 0), 2 * MiB)
        stats = self.engine.stats(torrent, 0)
        self.assertGreater(stats['progress'], 0.3)
        self.assertEqual(stats['peers'], fakelt.FAKE.peers)

    def test_idle_torrent_is_closed_and_resumed_from_cache(self):
        torrent = self.engine.open(self.data, 0)
        b''.join(self.engine.read(torrent, 0, 0, MiB, 'viewer'))
        self.engine.close_torrent(torrent.key)
        deadline = time.time() + 3
        while torrent.key in self.engine.torrents and time.time() < deadline:
            time.sleep(0.05)
        self.assertNotIn(torrent.key, self.engine.torrents)
        self.assertTrue((self.engine.config.cache_dir / f'{torrent.key}.resume').exists())
        before = len(fakelt.FAKE.downloaded)
        again = self.engine.open(self.data, 0)
        self.assertEqual(b''.join(self.engine.read(again, 0, 0, 1000, 'viewer')), self.payload[:1001])
        self.assertEqual(len(fakelt.FAKE.downloaded), before)   # no re-download of known pieces
        usage = self.engine.cache_usage()
        self.assertEqual(usage['entries'][0]['key'], torrent.key)

    def test_cache_limit_evicts_inactive_films(self):
        torrent = self.engine.open(self.data, 0)
        b''.join(self.engine.read(torrent, 0, 0, MiB, 'viewer'))
        self.engine.close_torrent(torrent.key)
        time.sleep(0.6)
        self.engine.config.cache_limit = 0
        self.engine.enforce_cache_limit()
        self.assertFalse((self.engine.config.cache_dir / torrent.key).exists())

    def test_magnet_metadata_is_resolved_into_a_torrent(self):
        meta = torrent_meta.parse_torrent(self.data)
        fakelt.FAKE.magnets[meta.info_hash] = self.data
        data = self.engine.resolve_magnet(f'magnet:?xt=urn:btih:{meta.info_hash}&dn=movie', timeout=5)
        self.assertEqual(torrent_meta.info_hash(data), meta.info_hash)


class HoleTests(unittest.TestCase):
    def test_punched_range_reads_as_zeros_and_frees_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'film.bin'
            payload = os.urandom(8 * MiB)
            path.write_bytes(payload)
            if not punch_holes(path, [(2 * MiB, 6 * MiB)]):
                self.skipTest('sparse files are not supported on ' + sys.platform)
            data = path.read_bytes()
            self.assertEqual(len(data), len(payload))
            self.assertEqual(data[:2 * MiB], payload[:2 * MiB])
            self.assertEqual(data[6 * MiB:], payload[6 * MiB:])
            self.assertEqual(data[2 * MiB:6 * MiB].count(0), 4 * MiB)
            self.assertLess(allocated(path), 5 * MiB)


class DiskWindowTests(unittest.TestCase):
    """Only the part around the viewer stays on disk; watched pieces are released."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        seed = root / 'seed'
        seed.mkdir()
        self.payload = os.urandom(12 * MiB)
        (seed / 'movie.mp4').write_bytes(self.payload)
        self.data = fakelt.make_torrent([seed / 'movie.mp4'], seed, piece_length=256 * 1024)
        fakelt.FAKE.seeds[torrent_meta.parse_torrent(self.data).info_hash] = seed
        fakelt.FAKE.speed = 4 * MiB
        fakelt.FAKE.downloaded.clear()
        fakelt.FAKE.added.clear()
        self.root = root

    def engine(self, **options):
        config = dict(min_window=MiB, readahead_seconds=1, critical_seconds=0.5, keep_behind_seconds=0.5,
                      trim_step=MiB, piece_timeout=10)
        config.update(options)
        engine = StreamEngine(EngineConfig(self.root / 'cache', **config), lt_module=fakelt.module())
        self.addCleanup(engine.close)
        return engine

    def tearDown(self):
        time.sleep(0.1)
        self.temp.cleanup()

    def test_watched_part_is_released_while_playing_and_can_be_downloaded_again(self):
        engine = self.engine()
        torrent = engine.open(self.data, 0)
        whole = b''.join(engine.read(torrent, 0, 0, len(self.payload) - 1, 'viewer'))
        self.assertEqual(whole, self.payload)            # byte-exact although pieces were released mid-play
        deadline = time.time() + 10
        while (sum(torrent.have) > 20 or torrent.trim) and time.time() < deadline:
            time.sleep(0.1)
        self.assertLessEqual(sum(torrent.have), 20)
        self.assertGreaterEqual(len(fakelt.FAKE.added), 2)    # re-added without the released pieces
        self.assertTrue(all(torrent.have[:8]))                # container header stays
        self.assertFalse(torrent.have[20])
        path = torrent.views[0].path
        if os.name == 'nt' or sys.platform.startswith('linux'):
            self.assertLess(allocated(path), 6 * MiB)
        middle = b''.join(engine.read(torrent, 0, 5 * MiB, 6 * MiB, 'viewer'))
        self.assertEqual(middle, self.payload[5 * MiB:6 * MiB + 1])
        resume = fakelt.read_resume_data((engine.config.cache_dir / f'{torrent.key}.resume').read_bytes())
        self.assertFalse(resume.have_pieces[30])              # restart never trusts released pieces

    def test_place_left_by_a_seek_is_released_after_the_grace_period(self):
        fakelt.FAKE.speed = 32 * MiB
        engine = self.engine(seek_grace=1.5, trim_step=512 * 1024)
        torrent = engine.open(self.data, 0)
        b''.join(engine.read(torrent, 0, 4 * MiB, 5 * MiB, 'viewer'))      # watching around 4 MiB...
        engine.hint(torrent, 'viewer', 0, 5 * MiB)
        time.sleep(0.3)
        engine.hint(torrent, 'viewer', 0, 10 * MiB)                           # ...then a jump to 10 MiB
        b''.join(engine.read(torrent, 0, 10 * MiB, 10 * MiB + 1000, 'viewer'))
        old = (4 * MiB) // torrent.piece_length + 1
        time.sleep(0.8)
        self.assertTrue(torrent.have[old])                                    # jumped-from place: still here
        deadline = time.time() + 8
        while (torrent.have[old] or torrent.trim) and time.time() < deadline:
            engine.hint(torrent, 'viewer', 0, 10 * MiB)                       # the viewer stays at the new place
            time.sleep(0.2)
        self.assertFalse(torrent.have[old])                                   # released after the grace period
        self.assertTrue(torrent.have[(10 * MiB) // torrent.piece_length])     # the current place stays
        self.assertTrue(all(torrent.have[:8]))                                # container header stays

    def test_disk_is_released_only_in_calm_moments_or_when_piled_up(self):
        fakelt.FAKE.speed = 32 * MiB
        engine = self.engine(trim_step=MiB, trim_force=4 * MiB)
        torrent = engine.open(self.data, 0)
        b''.join(engine.read(torrent, 0, 0, 1000, 'viewer'))
        time.sleep(0.3)
        plen = torrent.piece_length
        engine.config.background = True        # keep the dispatcher's own releases out of the way
        with engine._lock:
            torrent.have[:] = bytes(torrent.num_pieces)
            for piece in range(10, 18):         # 2 MiB already watched, far behind
                torrent.have[piece] = 1
            torrent.recent.clear()
            torrent.hints.clear()
            engine.hint(torrent, 'viewer', 0, 30 * plen)      # watching at piece 30: its window is not on disk yet
            now = time.monotonic()
            engine._maybe_trim(torrent, now)
            self.assertIsNone(torrent.trim)                   # mid-download: wait (a release reconnects peers)
            crit_n, win_n = engine._window(torrent)
            for piece in range(30, 30 + win_n):
                torrent.have[piece] = 1
            engine._maybe_trim(torrent, now)
            self.assertIsNotNone(torrent.trim)                # calm moment: release now
            self.assertEqual(torrent.trim['drop'], list(range(10, 18)))
            torrent.trim = None
            for piece in range(30, 30 + win_n):
                torrent.have[piece] = 0
            for piece in range(4, 27):
                torrent.have[piece] = 1
            engine._maybe_trim(torrent, now)                  # piled up past trim_force: release anyway
            self.assertIsNotNone(torrent.trim)
            torrent.trim = None
            torrent.have[30 + win_n] = 1                      # fetched a bit early, needed in a moment
            self.assertIn(30 + win_n, engine._keep(torrent, now))

    def test_background_mode_keeps_the_whole_film(self):
        fakelt.FAKE.speed = 32 * MiB
        engine = self.engine(background=True)
        torrent = engine.open(self.data, 0)
        b''.join(engine.read(torrent, 0, 0, len(self.payload) - 1, 'viewer'))
        time.sleep(2.5)
        self.assertEqual(sum(torrent.have), torrent.num_pieces)
        self.assertEqual(len(fakelt.FAKE.added), 1)


if __name__ == '__main__':
    unittest.main()
