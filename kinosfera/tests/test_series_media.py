"""Series from multi-file torrents, quality / dubbing variants and slider videos."""
import io
import os
from pathlib import Path
import threading
import time
import unittest

import support  # noqa: F401  (paths, emulator)
from support import AppTestCase
import fakelt
import series
import torrent_hls
import torrent_meta
from torrent_hls import HlsService, audio_label, quality_heights

# File list of a real season torrent (only the names matter here).
SAUL = [f'Better Call Saul/Better.Call.Saul.S01E{n:02d}.WEB-DL.2160p.SDR.HEVC.2xRus.Eng.mkv' for n in range(1, 11)]


def entries(names):
    return [dict(id=i, name=name, size=1000, offset=0, pad=False, supported=True) for i, name in enumerate(names)]


class EpisodeDetectionTests(unittest.TestCase):
    def test_real_season_is_recognised(self):
        found = series.detect(entries(SAUL))
        self.assertEqual([(e['season'], e['episode']) for e in found], [(1, n) for n in range(1, 11)])
        self.assertEqual(found[0]['file_id'], 0)

    def test_common_naming_styles(self):
        cases = {'Show/Season 2/Episode 05 - Pilot.mkv': (2, 5, 'Pilot'), 'Шоу/Сезон 1/Серия 3.avi': (1, 3, ''),
                 'show.1x07.The.Title.720p.mkv': (1, 7, 'The Title'), 'Show S02E10 Finale [1080p].mp4': (2, 10, 'Finale'),
                 'Show/S03/04. Название серии.mkv': (3, 4, 'Название серии'), 'Show.s01.e02.mkv': (1, 2, '')}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(series.parse_episode(name), expected)
        self.assertIsNone(series.parse_episode('random movie.mkv'))

    def test_unnumbered_files_are_appended_in_name_order(self):
        found = series.detect(entries(['Show/S01E01.mkv', 'Show/bonus b.mkv', 'Show/bonus a.mkv']))
        self.assertEqual([(e['file_id'], e['episode']) for e in found], [(0, 1), (2, 2), (1, 3)])

    def test_admin_input_is_validated(self):
        files = entries(['a.mkv', 'b.mkv'])
        with self.assertRaises(ValueError):
            series.validate([dict(file_id=0, season=1, episode=1), dict(file_id=1, season=1, episode=1)], files)
        with self.assertRaises(ValueError):
            series.validate([dict(file_id=5, season=1, episode=1)], files)
        with self.assertRaises(ValueError):
            series.validate([], files)
        result = series.validate([dict(file_id=1, season=2, episode=1, title='  Финал  '), dict(file_id=0, season=1, episode=3)], files)
        self.assertEqual([(e['season'], e['title']) for e in result], [(1, ''), (2, 'Финал')])


class SeriesFixture(AppTestCase):
    """A three-episode, two-season series torrent served by the emulator."""

    def setUp(self):
        super().setUp()
        seed = Path(self.temp.name) / 'seed'
        show = seed / 'Show'
        self.files = []
        for season, episode in ((1, 1), (1, 2), (2, 1)):
            path = support.make_video(show / f'Season {season}' / f'Show.S0{season}E0{episode}.webm', seconds=6)
            self.files.append(path)
        (show / 'Show.nfo').write_text('info', encoding='utf-8')
        self.torrent = fakelt.make_torrent(self.files + [show / 'Show.nfo'], show)
        fakelt.FAKE.seeds[torrent_meta.parse_torrent(self.torrent).info_hash] = seed
        self.playback.port = None
        # Page loads and rooms prewarm / analyse in background threads that would outlive the
        # temporary files of a test; record the calls instead.
        self.warmed = []
        for name, stub in (('prewarm', lambda movie, owner, seconds=600, file_id=None: self.warmed.append((movie, file_id))),
                           ('_probe', lambda media: None)):
            self.addCleanup(setattr, self.playback, name, getattr(self.playback, name))
            setattr(self.playback, name, stub)

    def create_series(self):
        self.accounts()
        form = dict(csrf_token=self.csrf(self.admin), kind='series', title='Шоу', description='Сериал для проверки сезонов.',
                    genres='Драма', year='2015', duration_min='45', country='США', rating='8.5', age_rating='16+',
                    published='on', torrent=(io.BytesIO(self.torrent), 'show.torrent'))
        response = self.admin.post('/admin/films/new', data=form, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        movie_id = int(response.location.split('/admin/films/')[1].split('?')[0].split('#')[0])
        return movie_id, self.store.film(movie_id)

    def start(self, client, movie_id, episode=None, client_id='e' * 20):
        return self.post(client, f'/api/play/{movie_id}/start', json={'client_id': client_id, 'episode': episode})


class SeriesTests(SeriesFixture):
    def test_admin_upload_detects_seasons_and_edits_episodes(self):
        movie_id, row = self.create_series()
        self.assertEqual(row['film'].kind, 'series')
        self.assertEqual([(e['season'], e['episode']) for e in row['episodes']], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(row['file_id'], row['episodes'][0]['file_id'])
        page = self.admin.get(f'/admin/films/{movie_id}').get_data(as_text=True)
        self.assertIn('КАРТОЧКА СЕРИАЛА', page)
        self.assertEqual(page.count('class="episode-row" role="row"'), 3)     # the .nfo is not a video
        first, second, third = (e['file_id'] for e in row['episodes'])
        form = dict(csrf_token=self.csrf(self.admin), kind='series', title='Шоу', description='Сериал для проверки сезонов.',
                    genres='Драма', year='2015', duration_min='45', rating='8', age_rating='16+', published='on')
        form.update({f'ep-{first}-on': 'on', f'ep-{first}-season': '1', f'ep-{first}-number': '1', f'ep-{first}-title': 'Пилот',
                     f'ep-{second}-season': '1', f'ep-{second}-number': '2',
                     f'ep-{third}-on': 'on', f'ep-{third}-season': '1', f'ep-{third}-number': '2'})
        self.assertEqual(self.admin.post(f'/admin/films/{movie_id}', data=form).status_code, 302)
        episodes = self.store.film(movie_id)['episodes']
        self.assertEqual([(e['file_id'], e['season'], e['episode'], e['title']) for e in episodes],
                         [(first, 1, 1, 'Пилот'), (third, 1, 2, '')])
        form[f'ep-{third}-number'] = '1'                                      # duplicate number: rejected
        page = self.admin.post(f'/admin/films/{movie_id}', data=form).get_data(as_text=True)
        self.assertIn('указана дважды', page)

    def test_series_pages_show_seasons_and_episode_counts(self):
        movie_id, row = self.create_series()
        catalog = self.viewer.get('/?view=catalog&kind=series').get_data(as_text=True)
        self.assertIn('Сериал</span>', catalog)
        self.assertIn('3 серии', catalog)
        self.assertNotIn('Интерстеллар', catalog)
        page = self.viewer.get(f'/movie/{movie_id}').get_data(as_text=True)
        self.assertIn('2 сезона · 3 серии', page)
        self.assertIn('data-season-tab="2"', page)
        self.assertEqual(page.count('class="episode-item'), 3)
        second = row['episodes'][1]['file_id']
        watch = self.viewer.get(f'/watch/{movie_id}?ep={second}').get_data(as_text=True)
        self.assertIn('S1 · E2', watch)
        self.assertIn(f'data-episode="{second}"', watch)
        self.assertIn('data-next-url', watch)

    def test_episode_playback_and_progress_move_to_the_next_episode(self):
        movie_id, row = self.create_series()
        first, second, third = (e['file_id'] for e in row['episodes'])
        state = self.start(self.viewer, movie_id, second).json
        self.assertEqual(state['file_id'], second)
        self.assertEqual(state['next_file_id'], third)
        self.assertEqual(state['episode']['episode'], 2)
        lease = self.playback.leases[state['session_id']]
        self.assertEqual(lease.media_key.split(':')[1], str(second))
        self.assertEqual(self.start(self.viewer, movie_id, 99).status_code, 404)
        sid = state['session_id']
        self.post(self.viewer, f'/api/play/s/{sid}/heartbeat', json={'position': 5.9, 'playing': False, 'duration': 6})
        progress = self.store.progress(2, movie_id)
        self.assertEqual(progress['file_id'], third)                 # continue with the next episode
        self.assertFalse(progress['completed'])
        self.assertTrue(self.store.episode_progress(2, movie_id)[second]['completed'])
        page = self.viewer.get(f'/movie/{movie_id}').get_data(as_text=True)
        self.assertIn('Смотреть · S2 · E1', page)
        home = self.viewer.get('/').get_data(as_text=True)
        self.assertIn('Продолжить просмотр', home)
        last = self.start(self.viewer, movie_id, third, client_id='f' * 20).json['session_id']
        self.post(self.viewer, f'/api/play/s/{last}/heartbeat', json={'position': 6, 'playing': False, 'duration': 6})
        self.assertTrue(self.store.progress(2, movie_id)['completed'])
        self.assertIn(movie_id, self.store.state(2)['watched'])

    def test_failed_analysis_is_retried_and_restarted_torrent_is_served(self):
        movie_id, row = self.create_series()
        third = row['episodes'][2]['file_id']
        calls = []
        self.playback._probe = lambda media: calls.append(media.key)
        state = self.start(self.viewer, movie_id, third).json
        key = self.playback.leases[state['session_id']].media_key
        self.playback.media[key].thread.join(2)
        self.assertEqual(calls, [key])
        media = self.playback.media[key]
        media.error, media.state, media.failed_at = 'Server returned 4XX', 'error', time.monotonic()
        self.assertEqual(self.start(self.viewer, movie_id, third).json['state'], 'error')    # no retry storm
        media.failed_at -= 60
        retried = self.start(self.viewer, movie_id, third).json             # «Повторить» analyses again
        self.assertNotEqual(retried['state'], 'error')
        media.thread.join(2)
        self.assertEqual(calls, [key, key])
        # The torrent is stopped and opened again elsewhere: FFprobe still gets the bytes.
        old, file_id = self.playback.torrents[key]
        self.playback.engine.close_torrent(old.key)
        deadline = time.time() + 5
        while not old.closed and time.time() < deadline:
            time.sleep(0.05)
        live = self.playback.engine.open(self.torrent, file_id)
        self.assertIsNot(live, old)
        response = self.viewer.get(f"/internal/media/{key.replace(':', '/')}",
                                   headers={'X-Internal-Token': self.playback.internal_token, 'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, self.files[2].read_bytes()[:4])
        self.assertIs(self.playback.torrents[key][0], live)

    def test_rooms_keep_the_episode(self):
        movie_id, row = self.create_series()
        second = row['episodes'][1]['file_id']
        warmed = self.warmed
        response = self.admin.post('/rooms/create', data=dict(csrf_token=self.csrf(self.admin), movie_id=movie_id, episode=second))
        code = response.location.rstrip('/').rsplit('/', 1)[1]
        room = self.app.extensions['rooms'].get(code)
        self.assertEqual(room.state['episode'], second)
        page = self.admin.get(f'/room/{code}').get_data(as_text=True)
        self.assertIn(f'data-episode="{second}"', page)
        self.assertIn('id="host-episode"', page)
        third = row['episodes'][2]['file_id']
        self.post(self.admin, f'/api/rooms/{code}/control', json={'action': 'play', 'position': 3})
        self.assertEqual(room.state['episode'], second)
        self.post(self.admin, f'/api/rooms/{code}/movie', json={'movie_id': movie_id, 'episode': third})
        self.assertEqual(room.state['episode'], third)
        deadline = time.time() + 3
        while len(warmed) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(warmed, [(movie_id, second), (movie_id, third)])    # the chosen episode is prewarmed


class SeasonSourceTests(SeriesFixture):
    """Torrent preview, seasons from separate torrents and the step-by-step source check."""

    def season_torrent(self, folder, names):
        seed = Path(self.temp.name) / folder
        files = [support.make_video(seed / 'Show' / name, seconds=4) for name in names]
        data = fakelt.make_torrent(files, seed / 'Show')
        fakelt.FAKE.seeds[torrent_meta.parse_torrent(data).info_hash] = seed
        return data

    def inspect(self, data=None, magnet=None, kind='series'):
        form = dict(csrf_token=self.csrf(self.admin), kind=kind)
        if data is not None:
            form['torrent'] = (io.BytesIO(data), 'season.torrent')
        if magnet:
            form['magnet'] = magnet
        return self.admin.post('/admin/sources/inspect', data=form, content_type='multipart/form-data')

    def test_preview_shows_episodes_and_the_card_is_saved_from_it(self):
        self.accounts()
        response = self.inspect(self.torrent)
        self.assertEqual(response.status_code, 200)
        job = response.json
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['meta']['videos'], 3)
        self.assertEqual(job['meta']['seasons'], '1 сезон: 1–2 · 2 сезон: 1')
        self.assertEqual(len(job['meta']['episodes']), 3)
        form = dict(csrf_token=self.csrf(self.admin), kind='series', title='Шоу', description='Сериал для проверки сезонов.',
                    genres='Драма', year='2015', duration_min='45', rating='8', age_rating='16+', published='on',
                    source_token=job['token'])
        response = self.admin.post('/admin/films/new', data=form)
        self.assertEqual(response.status_code, 302)
        self.assertIn('check=', response.location)                       # the source check starts by itself
        movie_id = int(response.location.split('/admin/films/')[1].split('?')[0])
        self.assertEqual(len(self.store.film(movie_id)['episodes']), 3)
        self.assertEqual(self.inspect(b'not a torrent').status_code, 400)

    def test_magnet_metadata_arrives_in_the_background(self):
        self.accounts()
        meta = torrent_meta.parse_torrent(self.torrent)
        release = threading.Event()

        def slow_resolve(magnet, timeout=90):
            release.wait(5)
            return self.torrent
        engine = self.playback.engine
        original = engine.resolve_magnet
        engine.resolve_magnet = slow_resolve
        self.addCleanup(setattr, engine, 'resolve_magnet', original)
        response = self.inspect(magnet=f'magnet:?xt=urn:btih:{meta.info_hash}&dn=Show')
        self.assertEqual(response.status_code, 202)
        token = response.json['token']
        self.assertEqual(self.admin.get(f'/admin/sources/inspect/{token}').json['state'], 'running')
        release.set()
        deadline = time.time() + 5
        while self.admin.get(f'/admin/sources/inspect/{token}').json['state'] == 'running' and time.time() < deadline:
            time.sleep(0.05)
        job = self.admin.get(f'/admin/sources/inspect/{token}').json
        self.assertEqual(job['state'], 'done')
        self.assertTrue(job['meta']['name'].endswith('(magnet)'))

    def test_seasons_from_other_torrents_join_the_card_and_play(self):
        movie_id, row = self.create_series()
        season3 = self.season_torrent('season3', ['Show.S03E01.webm', 'Show.S03E02.webm'])
        token = self.inspect(season3).json['token']
        response = self.admin.post(f'/admin/films/{movie_id}/sources', data=dict(csrf_token=self.csrf(self.admin), season_token=token))
        self.assertEqual(response.status_code, 302)
        episodes = self.store.film(movie_id)['episodes']
        self.assertEqual([(e['season'], e['episode']) for e in episodes], [(1, 1), (1, 2), (2, 1), (3, 1), (3, 2)])
        added = [e['file_id'] for e in episodes if e['season'] == 3]
        self.assertTrue(all(series.split(uid)[0] == 1 for uid in added))   # second torrent of the card
        self.assertIn(f'check={added[0]}', response.location)
        page = self.viewer.get(f'/movie/{movie_id}').get_data(as_text=True)
        self.assertIn('3 сезона · 5 серий', page)
        # the same torrent again, and a clash of season numbers
        again = self.admin.post(f'/admin/films/{movie_id}/sources', data=dict(
            csrf_token=self.csrf(self.admin), season_token=self.inspect(season3).json['token']), follow_redirects=True)
        self.assertIn('уже подключена', again.get_data(as_text=True))
        clash = self.season_torrent('clash', ['Show.S01E01.webm'])
        response = self.admin.post(f'/admin/films/{movie_id}/sources', data=dict(
            csrf_token=self.csrf(self.admin), season_token=self.inspect(clash).json['token']), follow_redirects=True)
        self.assertIn('уже есть в карточке', response.get_data(as_text=True))
        self.admin.post(f'/admin/films/{movie_id}/sources', data=dict(
            csrf_token=self.csrf(self.admin), season_token=self.inspect(clash).json['token'], season_number='4'))
        self.assertEqual(sorted({e['season'] for e in self.store.film(movie_id)['episodes']}), [1, 2, 3, 4])
        # an episode of the added season plays from its own torrent
        state = self.start(self.viewer, movie_id, added[1]).json
        lease = self.playback.leases[state['session_id']]
        self.assertEqual(lease.media_key, f'{torrent_meta.info_hash(season3)}:{series.split(added[1])[1]}')
        self.assertEqual(state['file_id'], added[1])
        payload = (Path(self.temp.name) / 'season3' / 'Show' / 'Show.S03E02.webm').read_bytes()
        response = self.viewer.get(f"/api/play/s/{state['session_id']}/video", headers={'Range': 'bytes=0-15'})
        self.assertEqual(response.data, payload[:16])
        health = self.admin.get(f'/admin/films/{movie_id}/health?ep={added[1]}').json
        self.assertEqual(health['file'], 'Show.S03E02.webm')
        self.assertIn('head', health)
        # the editor groups the table by torrent; removing the season drops its episodes only
        editor = self.admin.get(f'/admin/films/{movie_id}').get_data(as_text=True)
        self.assertEqual(editor.count('class="episode-source"'), 3)
        self.assertIn(f'name="ep-{added[0]}-on"', editor)
        self.admin.post(f'/admin/films/{movie_id}/sources/1/delete', data=dict(csrf_token=self.csrf(self.admin)))
        self.assertEqual(sorted({e['season'] for e in self.store.film(movie_id)['episodes']}), [1, 2, 4])
        self.assertIsNone(self.store.source_torrent(movie_id, 1))


class VariantTests(unittest.TestCase):
    def test_heights_never_upscale(self):
        self.assertEqual(quality_heights(2160), [1080, 720, 480, 360])
        self.assertEqual(quality_heights(720), [720, 480, 360])
        self.assertEqual(quality_heights(240), [360])

    def test_audio_labels(self):
        self.assertEqual(audio_label({'language': 'rus', 'title': 'Дубляж', 'channels': 6}), 'Русский · Дубляж · 5.1')
        self.assertEqual(audio_label({'language': 'eng', 'channels': 2}), 'English · стерео')
        self.assertEqual(audio_label({'channels': 2}, 1), 'Дорожка 2 · стерео')

    def test_stream_keys_and_ffmpeg_arguments(self):
        with self.subTest('service'):
            service = HlsService(Path(os.environ['CINEMA_CACHE_DIR']) / 'variants', lambda key, job: ('http://x', ''), height=720)
            self.addCleanup(service.close)
            service.register('m:1', 600, 'matroska', height=2160, audio_tracks=3)
            self.assertEqual(service.stream_key('m:1'), 'm:1@720a0')
            self.assertEqual(service.stream_key('m:1', 480, 2), 'm:1@480a2')
            with self.assertRaises(LookupError):
                service.stream_key('m:1', 2160)
            with self.assertRaises(LookupError):
                service.stream_key('m:1', 720, 3)
            args = service.ffmpeg_args('http://x', '', 'matroska', 10, 480, 2, Path('out'), 'job')
            self.assertIn('0:a:2?', args)
            self.assertIn("scale=w=-2:h='min(480,ih)':flags=bilinear,format=yuv420p", args)
            self.assertEqual(args[args.index('-threads') + 1], str(torrent_hls.CPU_THREADS))     # bounded CPU use
            self.assertNotIn('-readrate', args)        # pacing would delay the first frames after a seek
            self.assertIn('2500k', args)
            self.assertEqual(args[args.index('-ss') + 1], '60.000')


class VariantApiTests(AppTestCase):
    def test_state_lists_qualities_and_dubbing_and_rejects_unknown_variants(self):
        self.accounts()
        video = support.make_video(Path(self.temp.name) / 'seed' / 'film.webm', seconds=12)
        torrent = support.seeded_torrent(video)
        files, file_id = torrent_meta.inspect_torrent(torrent)
        self.store.set_source(2, torrent, 'film.torrent', files, file_id, torrent_meta.info_hash(torrent))
        key = f'{torrent_meta.info_hash(torrent)}:{file_id}'
        info = dict(duration=12.0, size=video.stat().st_size, bitrate=100000, container='.mkv',
                    video=dict(codec='hevc', height=2160, width=3840, pix_fmt='yuv420p'),
                    audio=dict(codec='ac3', channels=6),
                    audio_tracks=[dict(index=0, language='rus', title='Дубляж', channels=6, default=True),
                                  dict(index=1, language='rus', title='MVO', channels=2, default=False),
                                  dict(index=2, language='eng', title='', channels=6, default=False)])
        self.store.save_media_info(key, info)
        state = self.post(self.viewer, '/api/play/2/start', json={'client_id': 'g' * 20}).json
        self.assertEqual(state['state'], 'ready')
        self.assertEqual([q['id'] for q in state['qualities']], ['1080', '720', '480', '360'])   # HEVC: transcoded only
        self.assertEqual(state['default_quality'], '720')
        self.assertEqual([t['label'] for t in state['audio_tracks']], ['Русский · Дубляж · 5.1', 'Русский · MVO · стерео', 'English · 5.1'])
        sid = state['session_id']
        self.assertEqual(self.viewer.get(f'/api/play/s/{sid}/hls/480-2/index.m3u8').status_code, 200)
        self.assertEqual(self.viewer.get(f'/api/play/s/{sid}/hls/2160-0/index.m3u8').status_code, 404)
        self.assertEqual(self.viewer.get(f'/api/play/s/{sid}/hls/720-7/index.m3u8').status_code, 404)


class HeroVideoTests(AppTestCase):
    MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 2048

    def edit_form(self, **extra):
        form = dict(csrf_token=self.csrf(self.admin), title='Интерстеллар', description='Команда исследователей отправляется сквозь червоточину.',
                    genres='Фантастика', year='2014', duration_min='169', rating='8.6', age_rating='12+', published='on')
        form.update(extra)
        return form

    def test_upload_validate_show_and_remove(self):
        self.accounts()
        bad = self.admin.post('/admin/films/1', data=self.edit_form(hero_video_file=(io.BytesIO(b'not a video' * 10), 'x.mp4')),
                              content_type='multipart/form-data')
        self.assertIn('MP4 (H.264) или WebM', bad.get_data(as_text=True))
        ok = self.admin.post('/admin/films/1', data=self.edit_form(hero_video_file=(io.BytesIO(self.MP4), 'clip.mp4')),
                             content_type='multipart/form-data')
        self.assertEqual(ok.status_code, 302)
        path = self.store.film(1, include_hidden=True)['art']['hero_video']
        stored = Path(self.app.static_folder) / path
        self.addCleanup(stored.unlink, missing_ok=True)
        self.assertTrue(stored.is_file())
        home = self.viewer.get('/').get_data(as_text=True)
        self.assertIn('data-hero-video', home)
        self.assertIn(path, home)
        self.admin.post('/admin/films/1', data=self.edit_form(hero_video_remove='on'))
        self.assertNotIn('hero_video', self.store.film(1, include_hidden=True)['art'])
        self.assertFalse(stored.exists())

    def test_clip_is_cut_from_the_film_in_the_background(self):
        self.accounts()
        self.assertEqual(self.post(self.admin, '/admin/films/1/hero-clip', data={'start': '10', 'length': '20'}).status_code, 400)
        video = support.make_video(Path(self.temp.name) / 'seed' / 'film.webm', seconds=6)
        torrent = support.seeded_torrent(video)
        files, file_id = torrent_meta.inspect_torrent(torrent)
        self.store.set_source(1, torrent, 'film.torrent', files, file_id, torrent_meta.info_hash(torrent))
        calls = []

        def fake_clip(movie_id, start, length, target, file_id=None):
            calls.append((movie_id, start, length))
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(self.MP4)
            return dict(length=float(length), size=len(self.MP4))
        original = self.playback.make_clip
        self.playback.make_clip = fake_clip
        self.addCleanup(setattr, self.playback, 'make_clip', original)
        self.assertEqual(self.post(self.admin, '/admin/films/1/hero-clip', data={'start': '1:10:05', 'length': '99'}).status_code, 400)
        self.assertEqual(self.post(self.viewer, '/admin/films/1/hero-clip', data={'start': '10', 'length': '20'}).status_code, 403)
        response = self.post(self.admin, '/admin/films/1/hero-clip', data={'start': '1:10:05', 'length': '20'})
        self.assertEqual(response.status_code, 202)
        deadline = time.time() + 5
        while self.admin.get('/admin/films/1/hero-clip').json['state'] == 'running' and time.time() < deadline:
            time.sleep(0.05)
        job = self.admin.get('/admin/films/1/hero-clip').json
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(calls, [(1, 4205, 20)])
        art = self.store.film(1, include_hidden=True)['art']
        self.addCleanup((Path(self.app.static_folder) / art['hero_video']).unlink, missing_ok=True)
        self.assertEqual(art['hero_clip'], dict(start=4205, length=20.0))


if __name__ == '__main__':
    unittest.main()
