"""End-to-end check in a real browser against an emulated BitTorrent swarm.

    python tests/e2e_browser.py                 # bundled Playwright Chromium
    python tests/e2e_browser.py --channel msedge  # installed Edge (H.264 → also checks HLS in the browser)

Creates a temporary database and cache, generates its own test videos with FFmpeg,
registers an administrator and a viewer, publishes a film through the admin form,
then checks: single playback, a watch-party room (play/pause/seek sync, chat,
reactions, presence), HLS transcoding over HTTP, profile progress and screenshots.
Real accounts, library and cache are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
ARTIFACTS = ROOT / 'tests' / 'artifacts'
TMP = Path(tempfile.mkdtemp(prefix='kinosfera-e2e-'))
os.environ['CINEMA_CACHE_DIR'] = str(TMP / 'cache')

import fakelt  # noqa: E402
import torrent_meta  # noqa: E402


def make_media(directory):
    directory.mkdir(parents=True, exist_ok=True)
    jobs = {
        'film.webm': ['-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '700k', '-g', '50', '-c:a', 'libopus', '-b:a', '64k'],
        'film.avi': ['-c:v', 'mpeg4', '-q:v', '6', '-g', '50', '-c:a', 'mp3'],
    }
    for name, codec in jobs.items():
        target = directory / name
        if not target.exists():
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=25',
                            '-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000', '-t', '90', *codec, str(target)], check=True)
    return directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', default=None, help='msedge / chrome (default: bundled Chromium)')
    parser.add_argument('--speed', type=float, default=3.0, help='emulated swarm speed, MiB/s')
    parser.add_argument('--port', type=int, default=5077)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    media = make_media(TMP / 'seed')
    torrents = {}
    for name in ('film.webm', 'film.avi'):
        data = fakelt.make_torrent([media / name], media, piece_length=256 * 1024)
        meta = torrent_meta.parse_torrent(data)
        fakelt.FAKE.seeds[meta.info_hash] = media
        (TMP / f'{name}.torrent').write_bytes(data)
        torrents[name] = data
    fakelt.FAKE.speed = args.speed * 1024 * 1024
    fakelt.FAKE.peers = 7

    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    from app import app
    from database import Store
    import storage
    from ui_data import ART
    from werkzeug.serving import make_server

    store = Store(TMP / 'test.sqlite3')
    store.seed(storage.CATALOG, ART)
    app.extensions['store'] = store
    playback = app.extensions['playback']
    playback.store = store
    playback.engine._lt = fakelt.module()
    playback.engine.available = True
    playback.port = args.port
    class Quiet(__import__('werkzeug.serving').serving.WSGIRequestHandler):
        def log_request(self, *a, **k):
            if '/control' in self.path or '/start' in self.path:
                super().log_request(*a, **k)
    server = make_server('127.0.0.1', args.port, app, threaded=True, request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{args.port}'
    checks = []

    def check(name, condition, detail=''):
        checks.append((name, bool(condition), detail))
        print(('PASS ' if condition else 'FAIL ') + name + (f' — {detail}' if detail else ''), flush=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=not args.headed,
                                    args=['--autoplay-policy=no-user-gesture-required'])
        host_ctx = browser.new_context(viewport={'width': 1440, 'height': 960}, locale='ru-RU')
        guest_ctx = browser.new_context(viewport={'width': 1280, 'height': 900}, locale='ru-RU')
        host, guest = host_ctx.new_page(), guest_ctx.new_page()
        for page in (host, guest):
            page.on('pageerror', lambda error: print('JS error:', error))

        def register(page, email, name):
            page.goto(base + '/auth/register')
            page.fill('#username', name)
            page.fill('#email', email)
            page.fill('#password', 'e2e-password-123')
            page.fill('#confirm', 'e2e-password-123')
            page.click('.account-card button[type=submit]')
            page.wait_for_load_state('networkidle')

        register(host, 'host@example.test', 'Анна Хост')
        check('first account is administrator', '/admin' in host.url, host.url)
        register(guest, 'guest@example.test', 'Михаил Гость')

        # Publish a film through the admin form.
        host.goto(base + '/admin/films/new')
        host.fill('#title', 'Тестовый фильм WebM')
        host.fill('#description', 'Сгенерированное видео для проверки потокового просмотра.')
        host.fill('#genres', 'Фантастика, Драма')
        host.fill('#tagline', 'Проверка скорости и синхронизации')
        host.set_input_files('#torrent', str(TMP / 'film.webm.torrent'))
        host.check('input[name=published]')
        host.click('[data-film-editor] button[type=submit]')
        host.wait_for_load_state('networkidle')
        movie_id = int(host.url.rstrip('/').split('/')[-1])
        check('admin publishes .torrent', 'Источник подключён' in host.content(), f'movie {movie_id}')
        avi_id = store.save_film(dict(title='Тестовый AVI', description='AVI для проверки HLS.', genres=['Драма'], year=2026,
                                      country='', rating=7.0, duration_min=2, age_rating='12+', is_hidden=False),
                                 dict(slug='placeholder'), torrents['film.avi'], 'film.avi.torrent',
                                 *torrent_meta.inspect_torrent(torrents['film.avi']), info_hash=torrent_meta.info_hash(torrents['film.avi']))

        # Single viewer playback.
        t0 = time.time()
        host.goto(f'{base}/watch/{movie_id}')
        host.click('#watch-start')
        host.wait_for_function("document.getElementById('torrent-video').currentTime > 1.5", timeout=60000)
        check('single playback starts', True, f'{time.time() - t0:.1f} s to the first frames (emulated {args.speed} MiB/s)')
        host.wait_for_timeout(4500)
        stats = host.inner_text('#stream-stats')
        check('live torrent stats shown', 'Оригинал' in stats, stats.replace('\n', ' '))
        host.evaluate("document.getElementById('torrent-video').currentTime = 55")
        host.wait_for_function("(() => { const v = document.getElementById('torrent-video'); return v.currentTime > 56 && !v.seeking; })()", timeout=60000)
        check('seek far ahead works', True)
        host.screenshot(path=str(ARTIFACTS / 'e2e-player.png'))
        host.evaluate("document.getElementById('torrent-video').pause()")
        host.wait_for_timeout(5000)   # heartbeat stores progress
        host.goto(f'{base}/movie/{movie_id}')
        check('resume offered on the film page', 'Продолжить с' in host.content())
        host.screenshot(path=str(ARTIFACTS / 'e2e-movie.png'), full_page=False)

        # Watch-party room.
        host.click('button:has-text("Смотреть вместе")')
        host.wait_for_url('**/room/**')
        code = host.url.rstrip('/').split('/')[-1]
        check('room created', len(code) == 6, code)
        guest.goto(base + '/rooms')
        guest.fill('#room-code', code.lower())
        guest.click('.join-form button')
        guest.wait_for_url(f'**/room/{code}')
        host.wait_for_function("document.querySelectorAll('#member-list .member.is-online').length === 2", timeout=15000)
        check('presence shows both members', True)
        for page in (host, guest):
            page.click('#room-connect')
        for page in (host, guest):
            page.wait_for_function("document.getElementById('room-video').readyState >= 2", timeout=60000)
        host.evaluate("document.getElementById('room-video').play()")
        guest.wait_for_function("!document.getElementById('room-video').paused", timeout=15000)
        host.wait_for_timeout(6000)
        positions = [page.evaluate("document.getElementById('room-video').currentTime") for page in (host, guest)]
        check('guest follows host play', abs(positions[0] - positions[1]) < 1.0, f'host {positions[0]:.2f} s, guest {positions[1]:.2f} s')
        host.evaluate("document.getElementById('room-video').currentTime = 40")
        guest.wait_for_function("Math.abs(document.getElementById('room-video').currentTime - 40) < 4", timeout=30000)
        host.wait_for_timeout(5000)
        positions = [page.evaluate("document.getElementById('room-video').currentTime") for page in (host, guest)]
        check('seek is synchronised', abs(positions[0] - positions[1]) < 1.0, f'host {positions[0]:.2f} s, guest {positions[1]:.2f} s')
        guest.evaluate("document.getElementById('room-video').currentTime = 10")   # guest may not control
        guest.wait_for_timeout(2500)
        drift = abs(guest.evaluate("document.getElementById('room-video').currentTime") - host.evaluate("document.getElementById('room-video').currentTime"))
        check('guest without rights is pulled back', drift < 1.5, f'drift {drift:.2f} s')
        guest.fill('#chat-input', 'Привет из второго браузера!')
        guest.press('#chat-input', 'Enter')
        host.wait_for_selector('#chat-messages >> text=Привет из второго браузера!', timeout=5000)
        check('chat delivered', True)
        guest.click('#reaction-bar button >> nth=4')
        host.wait_for_selector('#reaction-stage .floating-reaction', timeout=5000)
        check('reaction delivered', True)
        host.wait_for_timeout(700)
        host.screenshot(path=str(ARTIFACTS / 'e2e-room-host.png'))
        host.evaluate("document.getElementById('room-video').pause()")
        guest.wait_for_function("document.getElementById('room-video').paused", timeout=10000)
        check('pause is synchronised', True)
        guest.click('[data-pane=people]')
        guest.wait_for_timeout(500)
        guest.screenshot(path=str(ARTIFACTS / 'e2e-room-guest.png'))
        host.click('[data-host-tools]')
        host.check('#host-locked') if not host.is_checked('#host-locked') else None
        host.wait_for_timeout(500)
        host.screenshot(path=str(ARTIFACTS / 'e2e-room-settings.png'))

        # HLS over HTTP (bundled Chromium has no H.264, so check the stream itself).
        result = guest.evaluate("""async (id) => {
            const csrf = document.querySelector('meta[name=csrf-token]').content;
            const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
            let s = await (await fetch(`/api/play/${id}/start`, {method: 'POST', headers, body: JSON.stringify({client_id: 'e2ehlsclient0000001'})})).json();
            for (let i = 0; i < 120 && s.state !== 'ready'; i++) { await new Promise(r => setTimeout(r, 500)); s = await (await fetch(`/api/play/s/${s.session_id}/status`)).json(); }
            const playlist = await (await fetch(s.hls_url)).text();
            const t0 = performance.now();
            const seg0 = await fetch(s.hls_url.replace('index.m3u8', '0.ts'));
            const first = performance.now() - t0;
            const b0 = new Uint8Array(await seg0.arrayBuffer());
            const t1 = performance.now();
            const seg9 = await fetch(s.hls_url.replace('index.m3u8', '9.ts'));
            const far = performance.now() - t1;
            const b9 = new Uint8Array(await seg9.arrayBuffer());
            return {plan: s.plan, playlist, first, far, size0: b0.length, size9: b9.length, sync0: b0[0], sync9: b9[0]};
        }""", avi_id)
        check('AVI gets an HLS plan', result['plan']['mode'] == 'hls', json.dumps(result['plan'], ensure_ascii=False))
        check('HLS playlist covers the film', result['playlist'].count('#EXTINF') in (15, 16), f"{result['playlist'].count('#EXTINF')} segments")
        check('HLS segments are MPEG-TS', result['sync0'] == 0x47 and result['sync9'] == 0x47,
              f"first segment {result['first']:.0f} ms, far seek segment {result['far']:.0f} ms")

        # Profile & admin snapshots.
        host.goto(base + '/profile?section=history')
        check('history lists the film', 'Тестовый фильм WebM' in host.content())
        host.screenshot(path=str(ARTIFACTS / 'e2e-profile-history.png'))
        host.goto(base + '/profile')
        host.screenshot(path=str(ARTIFACTS / 'e2e-profile.png'))
        host.goto(base + '/admin?tab=engine')
        host.wait_for_timeout(2500)
        check('admin engine panel lists the torrent', 'film.webm' in host.content())
        host.screenshot(path=str(ARTIFACTS / 'e2e-admin-engine.png'))
        host.goto(base + '/admin')
        host.wait_for_timeout(2200)
        host.screenshot(path=str(ARTIFACTS / 'e2e-admin.png'))
        host.goto(base + '/')
        host.screenshot(path=str(ARTIFACTS / 'e2e-home.png'))
        host.goto(base + '/rooms')
        host.screenshot(path=str(ARTIFACTS / 'e2e-rooms.png'))
        mobile = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, storage_state=guest_ctx.storage_state()).new_page()
        mobile.goto(f'{base}/room/{code}')
        mobile.wait_for_timeout(1500)
        mobile.screenshot(path=str(ARTIFACTS / 'e2e-room-mobile.png'), full_page=True)
        browser.close()
    server.shutdown()
    playback.close()
    shutil.rmtree(TMP, ignore_errors=True)
    failed = [c for c in checks if not c[1]]
    print(f'\n{len(checks) - len(failed)}/{len(checks)} checks passed. Screenshots: {ARTIFACTS}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
