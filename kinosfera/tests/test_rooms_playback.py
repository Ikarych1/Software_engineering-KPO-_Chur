"""Playback sessions over the emulated swarm and watch-party room rules."""
import time
from pathlib import Path
import unittest

import support
from support import AppTestCase
import torrent_meta


class PlaybackTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.video = support.make_video(Path(self.temp.name) / 'seed' / 'film.webm', seconds=12)
        self.torrent = support.seeded_torrent(self.video)
        files, file_id = torrent_meta.inspect_torrent(self.torrent)
        self.store.set_source(2, self.torrent, 'film.torrent', files, file_id, torrent_meta.info_hash(self.torrent))
        self.playback.port = None

    def start(self, client, movie_id=2, client_id='c' * 20):
        return self.post(client, f'/api/play/{movie_id}/start', json={'client_id': client_id})

    def test_session_belongs_to_its_owner(self):
        self.accounts()
        sid = self.start(self.viewer).json['session_id']
        self.assertEqual(self.viewer.get(f'/api/play/s/{sid}/status').status_code, 200)
        self.assertEqual(self.admin.get(f'/api/play/s/{sid}/status').status_code, 404)
        self.assertEqual(self.admin.get(f'/api/play/s/{sid}/video', headers={'Range': 'bytes=0-9'}).status_code, 404)
        self.assertEqual(self.post(self.admin, f'/api/play/s/{sid}/end', json={}).status_code, 404)

    def test_direct_video_supports_ranges_with_exact_bytes(self):
        self.accounts()
        sid = self.start(self.viewer).json['session_id']
        payload = self.video.read_bytes()
        response = self.viewer.get(f'/api/play/s/{sid}/video', headers={'Range': 'bytes=100-299'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers['Content-Range'], f'bytes 100-299/{len(payload)}')
        self.assertEqual(response.data, payload[100:300])
        response = self.viewer.get(f'/api/play/s/{sid}/video', headers={'Range': f'bytes={len(payload)}-'})
        self.assertEqual(response.status_code, 416)
        head = self.viewer.head(f'/api/play/s/{sid}/video')
        self.assertEqual(head.headers['Content-Length'], str(len(payload)))

    def test_one_shared_torrent_for_many_viewers(self):
        self.accounts()
        self.start(self.viewer)
        self.start(self.admin, client_id='d' * 20)
        self.assertEqual(len(self.playback.engine.torrents), 1)
        self.assertEqual(len(self.playback.leases), 2)

    def test_sessions_per_user_are_limited_and_oldest_closes(self):
        self.accounts()
        ids = [self.start(self.viewer, client_id=chr(97 + i) * 20).json['session_id'] for i in range(5)]
        self.assertNotIn(ids[0], self.playback.leases)
        self.assertIn(ids[-1], self.playback.leases)

    def test_sourceless_or_hidden_films_cannot_start(self):
        self.accounts()
        self.assertEqual(self.start(self.viewer, movie_id=5).status_code, 404)
        self.assertEqual(self.post(self.viewer, '/api/play/2/start', json={'client_id': 'bad id'}).status_code, 400)

    def test_heartbeat_records_view_and_progress(self):
        self.accounts()
        sid = self.start(self.viewer).json['session_id']
        self.post(self.viewer, f'/api/play/s/{sid}/heartbeat', json={'position': 7.5, 'playing': True, 'duration': 12})
        self.post(self.viewer, f'/api/play/s/{sid}/end', json={'position': 11.8, 'duration': 12})
        self.assertEqual(self.store.dashboard()['views'], 1)
        progress = self.store.progress(2, 2)
        self.assertTrue(progress['completed'])
        self.assertIn(2, self.store.state(2)['watched'])

    def test_internal_media_endpoint_requires_the_token(self):
        self.accounts()
        sid = self.start(self.viewer).json['session_id']
        key = self.playback.leases[sid].media_key.replace(':', '/')
        self.assertEqual(self.viewer.get(f'/internal/media/{key}').status_code, 403)
        response = self.viewer.get(f'/internal/media/{key}', headers={'X-Internal-Token': self.playback.internal_token, 'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, self.video.read_bytes()[:4])

    def test_media_is_probed_once_and_planned_for_direct_playback(self):
        self.accounts()
        import werkzeug.serving
        import threading
        server = werkzeug.serving.make_server('127.0.0.1', 0, self.app, threaded=True)
        self.playback.port = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            sid = self.start(self.viewer).json['session_id']
            deadline = time.time() + 30
            state = {}
            while time.time() < deadline:
                state = self.viewer.get(f'/api/play/s/{sid}/status').json
                if state['state'] in ('ready', 'error'):
                    break
                time.sleep(0.3)
            self.assertEqual(state['state'], 'ready', state)
            self.assertEqual(state['plan']['mode'], 'direct')
            self.assertEqual(state['plan']['mime'], 'video/webm')
            self.assertAlmostEqual(state['duration'], 12, delta=0.5)
            key = self.playback.leases[sid].media_key
            self.assertIsNotNone(self.store.media_info(key))
            self.assertIn('VP9', self.viewer.get('/movie/2').text)
        finally:
            server.shutdown()


class RoomTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.accounts()
        response = self.post(self.admin, '/rooms/create', data={'movie_id': '1'})
        self.code = response.location.rsplit('/', 1)[-1]
        self.assertEqual(self.viewer.get(f'/room/{self.code}').status_code, 200)   # joins
        self.hub = self.app.extensions['rooms']

    def api(self, client, path, **data):
        return self.post(client, f'/api/rooms/{self.code}/{path}', json=data)

    def test_join_by_code_is_case_insensitive(self):
        third = self.app.test_client()
        self.register(third, 'third@example.test', 'Third Person')
        response = self.post(third, '/rooms/join', data={'code': f' {self.code.lower()} '})
        self.assertTrue(response.location.endswith(f'/room/{self.code}'))
        self.assertEqual(self.post(third, '/rooms/join', data={'code': 'ZZZZZZ'}).status_code, 302)

    def test_only_host_controls_by_default(self):
        self.assertEqual(self.api(self.viewer, 'control', action='play', position=10).status_code, 403)
        self.assertEqual(self.api(self.admin, 'control', action='play', position=10).status_code, 200)
        room = self.hub.get(self.code)
        self.assertTrue(room.state['playing'])
        self.assertAlmostEqual(room.position_now(), 10, delta=0.5)
        self.api(self.admin, 'settings', control='all')
        self.assertEqual(self.api(self.viewer, 'control', action='pause', position=20).status_code, 200)
        self.assertFalse(room.state['playing'])
        self.assertEqual(room.state['position'], 20)

    def test_settings_are_host_only_and_validated(self):
        self.assertEqual(self.api(self.viewer, 'settings', chat=False).status_code, 403)
        self.api(self.admin, 'settings', chat=False, control='nonsense', title='Пятничное кино')
        room = self.hub.get(self.code)
        self.assertFalse(room.settings['chat'])
        self.assertEqual(room.settings['control'], 'host')
        self.assertEqual(self.api(self.viewer, 'chat', text='привет').status_code, 403)
        self.assertEqual(self.api(self.admin, 'chat', text='хост может').status_code, 200)

    def test_chat_is_persisted_and_escaped_by_templates(self):
        self.api(self.viewer, 'chat', text='<script>alert(1)</script>')
        messages = self.store.messages(self.code)
        self.assertEqual(messages[-1]['text'], '<script>alert(1)</script>')
        self.api(self.admin, 'end')
        page = self.viewer.get(f'/rooms/{self.code}/archive').text
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_wait_for_all_pauses_on_buffering_and_resumes(self):
        room = self.hub.get(self.code)
        self.api(self.admin, 'control', action='play', position=0)
        viewer = self.store.user(self.store.account(user_id=2))
        self.hub.presence(room, viewer, 'buffering', 3)
        room.members[2].buffering_since -= 5
        self.hub._evaluate_waiting(room, time.time())
        self.assertFalse(room.state['playing'])
        self.assertTrue(room.auto_paused)
        self.hub.presence(room, viewer, 'playing', 3)
        self.assertTrue(room.state['playing'])

    def test_non_members_cannot_use_the_room_api(self):
        outsider = self.app.test_client()
        self.register(outsider, 'outsider@example.test', 'Out Sider')
        self.assertEqual(self.api(outsider, 'chat', text='hi').status_code, 403)
        self.assertEqual(outsider.get(f'/api/rooms/{self.code}/events').status_code, 403)
        self.assertEqual(outsider.get(f'/rooms/{self.code}/archive').status_code, 403)

    def test_locked_room_rejects_new_members(self):
        self.api(self.admin, 'settings', locked=True)
        outsider = self.app.test_client()
        self.register(outsider, 'outsider@example.test', 'Out Sider')
        self.assertEqual(outsider.get(f'/room/{self.code}').status_code, 403)
        self.assertEqual(self.viewer.get(f'/room/{self.code}').status_code, 200)

    def test_host_transfer_and_end(self):
        self.assertEqual(self.api(self.viewer, 'end').status_code, 403)
        self.api(self.admin, 'host', user_id=2)
        self.assertEqual(self.hub.get(self.code).host_id, 2)
        self.assertEqual(self.api(self.viewer, 'end').status_code, 200)
        self.assertIsNotNone(self.store.room(self.code)['ended_at'])
        self.assertTrue(self.viewer.get(f'/room/{self.code}').location.endswith('/archive'))
        self.assertIn(self.code, self.viewer.get('/rooms').text)

    def test_event_stream_starts_with_a_snapshot(self):
        response = self.viewer.get(f'/api/rooms/{self.code}/events', buffered=False)
        first = next(response.response).decode()
        response.close()
        self.assertIn('event: snapshot', first)
        self.assertIn(self.code, first)

    def test_company_shows_co_viewers(self):
        page = self.viewer.get('/profile?section=company').text
        self.assertIn('Owner Admin', page)


if __name__ == '__main__':
    unittest.main()
