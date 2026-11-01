"""Media plans and the shared HLS job service (real FFmpeg, local HTTP Range server)."""
import http.server
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

import support
import torrent_hls
from torrent_hls import HlsJob, HlsService, build_vod_playlist, detect_accel, playback_plan, summarize


def playlist_segments(text):
    result, duration = [], None
    for line in text.splitlines():
        if line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif line and not line.startswith('#'):
            result.append((duration, line))
            duration = None
    return result


class VodPlaylistTests(unittest.TestCase):
    def test_playlist_exposes_the_complete_seekable_timeline(self):
        text = build_vod_playlist(13.25, segment_seconds=6)
        self.assertIn('#EXT-X-PLAYLIST-TYPE:VOD', text)
        self.assertTrue(text.rstrip().endswith('#EXT-X-ENDLIST'))
        segments = playlist_segments(text)
        self.assertEqual([d for d, _ in segments], [6, 6, 1.25])
        self.assertEqual([uri for _, uri in segments], ['segment/0.ts', 'segment/1.ts', 'segment/2.ts'])

    def test_exact_multiple_does_not_create_an_empty_final_segment(self):
        self.assertEqual(len(playlist_segments(build_vod_playlist(12))), 2)

    def test_custom_uri_template(self):
        self.assertIn('\n3.ts\n', build_vod_playlist(20, uri='{index}.ts'))

    def test_invalid_or_nonfinite_durations_are_rejected(self):
        for duration in (0, -1, float('nan'), float('inf'), 13 * 3600):
            with self.assertRaises(ValueError):
                build_vod_playlist(duration)


def probe(**streams):
    video = dict(codec_type='video', codec_name='h264', profile='High', level=41, pix_fmt='yuv420p', width=1920, height=1080)
    audio = dict(codec_type='audio', codec_name='aac', channels=2)
    video.update(streams.get('video', {}))
    audio.update(streams.get('audio', {}))
    return dict(format=dict(duration='5400.5', bit_rate='8000000'), streams=[video, audio])


class PlaybackPlanTests(unittest.TestCase):
    def plan(self, name, **streams):
        return playback_plan(summarize(probe(**streams), name, 5 * 1024 ** 3))

    def test_browser_native_files_are_streamed_without_transcoding(self):
        plan = self.plan('film.mp4')
        self.assertEqual(plan['mode'], 'direct')
        self.assertEqual(plan['codecs'], 'avc1.640029, mp4a.40.2')
        webm = self.plan('film.webm', video=dict(codec_name='vp9'), audio=dict(codec_name='opus'))
        self.assertEqual((webm['mode'], webm['mime']), ('direct', 'video/webm'))

    def test_matroska_h264_is_direct_on_chromium_with_hls_fallback(self):
        plan = self.plan('film.mkv')
        self.assertEqual(plan['mode'], 'direct')
        self.assertTrue(plan['chromium_only'])
        self.assertEqual(plan['fallback'], 'hls')

    def test_unsupported_codecs_are_transcoded(self):
        for name, streams in (('film.avi', {}), ('film.mkv', dict(video=dict(codec_name='hevc'))),
                              ('film.mkv', dict(audio=dict(codec_name='ac3'))), ('film.mp4', dict(video=dict(pix_fmt='yuv420p10le')))):
            with self.subTest(name=name, streams=streams):
                self.assertEqual(self.plan(name, **streams)['mode'], 'hls')

    def test_summary_reports_quality_and_bitrate(self):
        info = summarize(probe(), 'film.mkv', 5 * 1024 ** 3)
        self.assertEqual(info['video']['height'], 1080)
        self.assertEqual(info['bitrate'], 1_000_000)
        self.assertAlmostEqual(info['duration'], 5400.5)


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Minimal HTTP Range server standing in for the internal torrent stream."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = Path(self.directory) / self.path.split('?')[0].lstrip('/')
        data = path.read_bytes()
        header = self.headers.get('Range')
        start, end = 0, len(data) - 1
        if header:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', header)
            start = int(match.group(1))
            end = int(match.group(2) or end)
        self.send_response(206 if header else 200)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        if header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
        self.end_headers()
        try:
            self.wfile.write(data[start:end + 1])
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class HlsServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.media = support.make_video(Path(cls.temp.name) / 'media' / 'film.avi', seconds=40, codec='avi')
        handler = lambda *a, **k: RangeHandler(*a, directory=str(cls.media.parent), **k)  # noqa: E731
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp.cleanup()

    def setUp(self):
        url = f'http://127.0.0.1:{self.server.server_address[1]}/film.avi'
        self.service = HlsService(Path(self.temp.name) / f'hls-{self._testMethodName}', lambda key, job: (url, 'X-Test: 1\r\n'), height=180, preset='ultrafast')
        self.service.register('media:0', 40.0, 'avi')

    def tearDown(self):
        self.service.close()

    def keyframes(self, path):
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries', 'packet=pts_time,flags',
                              '-of', 'json', str(path)], capture_output=True, check=True).stdout
        packets = json.loads(out)['packets']
        return float(packets[0]['pts_time']), [p for p in packets if 'K' in p['flags']]

    def test_segments_start_on_the_exact_grid_with_one_keyframe(self):
        first = self.service.segment('media:0', 0)
        start, keys = self.keyframes(first)
        self.assertAlmostEqual(start, 0.0, delta=0.1)
        self.assertEqual(len(keys), 1)
        later = self.service.segment('media:0', 2)
        start, keys = self.keyframes(later)
        self.assertAlmostEqual(start, 12.0, delta=0.1)

    def test_far_seek_starts_a_new_job_and_segments_are_reused(self):
        self.service.segment('media:0', 0)
        far = self.service.segment('media:0', 5)
        start, _ = self.keyframes(far)
        self.assertAlmostEqual(start, 30.0, delta=0.1)
        self.assertLessEqual(len(self.service.jobs_for('media:0')), 2)
        again = self.service.segment('media:0', 5)
        self.assertEqual(far, again)

    def test_gpu_failure_falls_back_to_the_processor(self):
        # Intel Quick Sync is absent here (or cannot read this MPEG-4 file): the job must still deliver.
        self.service.register('media:gpu', 40.0, 'avi', height=180, width=320, codec='h264')
        self.service.accel = 'qsv'
        first = self.service.segment('media:gpu', 0)
        start, keys = self.keyframes(first)
        self.assertAlmostEqual(start, 0.0, delta=0.1)
        self.assertEqual(len(keys), 1)
        if self.service.media['media:gpu']['gpu_failed']:
            self.assertEqual(self.service.profile_for('media:gpu'), 'software')

    def test_out_of_range_segments_never_spawn_ffmpeg(self):
        with self.assertRaises(LookupError):
            self.service.segment('media:0', math.ceil(40 / 6))
        with self.assertRaises(LookupError):
            self.service.segment('unknown', 0)
        self.assertEqual(self.service.jobs_for('media:0'), [])


class GpuTranscodingTests(unittest.TestCase):
    """The hardware profile this computer really has (skipped without a supported video card)."""

    @classmethod
    def setUpClass(cls):
        cls.accel = detect_accel(torrent_hls.binary('ffmpeg', 'FFMPEG_PATH'))
        if cls.accel == 'software':
            raise unittest.SkipTest('no hardware H.264 transcoding on this computer')
        cls.temp = tempfile.TemporaryDirectory()
        cls.media = support.make_video(Path(cls.temp.name) / 'media' / 'film.mp4', seconds=30, codec='mp4')
        handler = lambda *a, **k: RangeHandler(*a, directory=str(cls.media.parent), **k)  # noqa: E731
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp.cleanup()

    def test_segments_from_the_video_card_keep_the_grid(self):
        url = f'http://127.0.0.1:{self.server.server_address[1]}/film.mp4'
        service = HlsService(Path(self.temp.name) / 'hls', lambda key, job: (url, 'X-Test: 1\r\n'), height=180, accel='off')
        self.addCleanup(service.close)
        service.accel = self.accel
        service.register('media:0', 30.0, 'mov', height=180, width=320, codec='h264')
        self.assertEqual(service.profile_for('media:0'), self.accel)
        for index, expected in ((0, 0.0), (2, 12.0)):
            out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries', 'packet=pts_time,flags',
                                  '-of', 'json', str(service.segment('media:0', index))], capture_output=True, check=True).stdout
            packets = json.loads(out)['packets']
            self.assertAlmostEqual(float(packets[0]['pts_time']), expected, delta=0.1)
            self.assertEqual(len([p for p in packets if 'K' in p['flags']]), 1)
        self.assertFalse(service.media['media:0']['gpu_failed'])
        self.assertTrue(all(job.profile == self.accel for job in service.jobs_for('media:0')))


class JobManagementTests(unittest.TestCase):
    def test_frontier_survives_cleanup_behind_the_viewer(self):
        with tempfile.TemporaryDirectory() as temp:
            job = HlsJob('m@720a0', 10, Path(temp))
            for index in range(10, 16):
                (Path(temp) / f'seg_{index}.ts').write_bytes(b'ts')
            self.assertEqual(job.frontier(), 15)
            for index in range(10, 14):                         # removed by the cleanup: already watched
                (Path(temp) / f'seg_{index}.ts').unlink()
            (Path(temp) / 'seg_16.ts').write_bytes(b'ts')
            self.assertEqual(job.frontier(), 16)                # still running — no second FFmpeg

    def test_seek_or_switch_stops_jobs_nobody_watches(self):
        started = []

        class FakeProcess:
            def __init__(self, args, **kwargs):
                self.args, self.returncode, self.stderr = args, None, open(os.devnull, 'rb')
                started.append(self)

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode
        original = torrent_hls.subprocess.Popen
        torrent_hls.subprocess.Popen = FakeProcess
        self.addCleanup(setattr, torrent_hls.subprocess, 'Popen', original)
        with tempfile.TemporaryDirectory() as temp:
            service = HlsService(Path(temp), lambda key, job: ('http://x', ''), accel='off')
            self.addCleanup(service.close)
            service.register('film:0', 3600, 'matroska', height=2160, width=3840, codec='hevc', audio_tracks=2)
            dubbed = service.stream_key('film:0', 720, 0)
            with service._lock:
                old = service._start(dubbed, 10, service._dir(dubbed))
                old.requested -= 20                              # the viewer left it 20 s ago
                new = service._start(service.stream_key('film:0', 720, 1), 12, service._dir(dubbed))
            self.assertEqual(old.process.returncode, -9)        # stopped, not left running for minutes
            self.assertTrue(new.alive())
            self.assertEqual(service.jobs_for('film:0'), [new])
            args = started[-1].args
            self.assertEqual(args[args.index('-threads') + 1], str(torrent_hls.CPU_THREADS))


class HlsCleanupTests(unittest.TestCase):
    def test_segments_far_behind_viewers_are_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            service = HlsService(Path(temp), lambda key, job: ('', ''))
            self.addCleanup(service.close)
            service.keep_all, service.keep_behind = False, 20
            service.register('film:0', 600, 'matroska', height=1080)
            stream = service.stream_key('film:0')
            watching = service._dir(stream)
            abandoned = service._dir('other:0@720a0')
            for directory in (watching, abandoned):
                directory.mkdir(parents=True)
                for index in range(60):
                    path = directory / f'seg_{index}.ts'
                    path.write_bytes(b'ts')
                    os.utime(path, (time.time() - 300, time.time() - 300))
            (watching / 'seg_5.ts').touch()                   # just served to someone: stays
            service.served[stream] = {50: time.monotonic()}
            service._drop_watched(time.monotonic())
            left = sorted(int(p.stem[4:]) for p in watching.glob('seg_*.ts'))
            self.assertEqual(left, [5] + list(range(30, 60)))
            self.assertEqual(list(abandoned.glob('seg_*.ts')), [])
            self.assertFalse(abandoned.exists())                 # empty leftovers of a variant go too

    def test_only_the_current_place_survives_seeks_and_variant_switches(self):
        with tempfile.TemporaryDirectory() as temp:
            service = HlsService(Path(temp), lambda key, job: ('', ''))
            self.addCleanup(service.close)
            service.keep_all, service.keep_behind, service.grace, service.ahead = False, 2, 10, 5
            service.register('film:0', 3600, 'matroska', height=1080, audio_tracks=2)
            dubbed, original = service.stream_key('film:0', 720, 0), service.stream_key('film:0', 720, 1)
            for stream in (dubbed, original):
                directory = service._dir(stream)
                directory.mkdir(parents=True)
                for index in range(100):
                    path = directory / f'seg_{index}.ts'
                    path.write_bytes(b'ts')
                    os.utime(path, (time.time() - 300, time.time() - 300))
            now = time.monotonic()
            # Watched the dubbed track from segment 10 to 20, jumped to 80 a moment ago, then the original.
            service.served[dubbed] = {**{i: now - 60 + i for i in range(10, 21)}, 80: now - 5}
            service.served[original] = {81: now - 1}
            service._drop_watched(now)
            left = lambda stream: sorted(int(p.stem[4:]) for p in service._dir(stream).glob('seg_*.ts'))
            self.assertEqual(left(original), list(range(79, 87)))           # current place: 2 behind, 5 ahead
            self.assertEqual(left(dubbed), list(range(78, 86)))             # jumped-from variant: still within 10 s
            service._drop_watched(now + 11)
            self.assertEqual(left(dubbed), [])                              # ... and gone after the grace period
            self.assertEqual(left(original), list(range(79, 87)))           # a paused viewer keeps the place


if __name__ == '__main__':
    unittest.main()
