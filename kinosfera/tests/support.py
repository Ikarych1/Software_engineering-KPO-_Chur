"""Shared helpers for offline tests: temporary database, emulated swarm, generated media."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ.setdefault('CINEMA_CACHE_DIR', tempfile.mkdtemp(prefix='kinosfera-cache-'))
# Media analysis of a test without its own HTTP server must never reach a real site on 5050.
os.environ['CINEMA_PORT'] = '9'
# Hardware transcoding is probed by its own test; the rest of the suite runs the same everywhere.
os.environ.setdefault('CINEMA_HWACCEL', 'off')

import fakelt  # noqa: E402
import torrent_meta  # noqa: E402


def make_video(path, seconds=30, codec='webm'):
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    args = {'webm': ['-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '400k', '-g', '25', '-c:a', 'libopus'],
            'avi': ['-c:v', 'mpeg4', '-q:v', '8', '-g', '25', '-c:a', 'mp3'],
            'mp4': ['-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25', '-c:a', 'aac']}[codec]
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                    '-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000', '-t', str(seconds), *args, str(path)], check=True)
    return path


def seeded_torrent(path, piece_length=64 * 1024):
    """A .torrent for ``path`` whose pieces the emulated swarm can serve."""
    path = Path(path)
    data = fakelt.make_torrent([path], path.parent, piece_length=piece_length)
    fakelt.FAKE.seeds[torrent_meta.parse_torrent(data).info_hash] = path.parent
    return data


class AppTestCase(unittest.TestCase):
    """Flask app on a temporary SQLite database with the libtorrent emulator."""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        app.testing = True
        playback = app.extensions['playback']
        playback.engine._lt = fakelt.module()
        playback.engine.available = True

    def setUp(self):
        from database import Store
        import storage
        from ui_data import ART
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.store.seed(storage.CATALOG, ART)
        self.previous = self.app.extensions['store']
        self.app.extensions['store'] = self.store
        self.playback = self.app.extensions['playback']
        self.previous_pb_store = self.playback.store
        self.playback.store = self.store
        self.app.extensions['rooms'].rooms.clear()
        self.admin, self.viewer = self.app.test_client(), self.app.test_client()

    def tearDown(self):
        for lease_id in list(self.playback.leases):
            self.playback.end(lease_id)
        engine = self.playback.engine
        with engine._lock:
            for torrent in list(engine.torrents.values()):
                for reader in torrent.readers.values():
                    reader.cancelled = True
                engine._finalize_close(torrent)
        self.playback.media.clear()
        self.playback.torrents.clear()
        self.app.extensions['store'] = self.previous
        self.playback.store = self.previous_pb_store
        self.temp.cleanup()

    def csrf(self, client):
        with client.session_transaction() as session:
            token = session.get('csrf')
        if token:
            return token
        client.get('/')
        with client.session_transaction() as session:
            return session['csrf']

    def register(self, client, email, name='Test Viewer'):
        return client.post('/auth/register', data=dict(email=email, username=name, password='test-password-123',
                                                       confirm='test-password-123', csrf_token=self.csrf(client)))

    def post(self, client, url, **kwargs):
        return client.post(url, headers={'X-CSRF-Token': self.csrf(client)}, **kwargs)

    def accounts(self):
        self.assertEqual(self.register(self.admin, 'owner@example.test', 'Owner Admin').status_code, 302)
        self.assertEqual(self.register(self.viewer, 'viewer@example.test', 'Test Viewer').status_code, 302)
