"""Real-browser recommendation flow on a temporary database and local server."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path)
    parser.add_argument('--channel', default=None)
    parser.add_argument('--artifacts', type=Path, default=ROOT/'tests/artifacts/recommendations/browser')
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kinosfera-browser-') as d:
        directory = Path(d); db_path = directory/'working.sqlite3'
        if args.database:
            shutil.copyfile(args.database, db_path)
        os.environ.update(CINEMA_DATABASE=str(db_path), CINEMA_CACHE_DIR=str(directory/'media'),
                          CINEMA_RECOMMENDATION_CACHE=str(directory/'index'), CINEMA_HWACCEL='off')
        from app import app
        from werkzeug.serving import make_server
        from playwright.sync_api import sync_playwright, expect
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel=args.channel, headless=True)
                context = browser.new_context(viewport=dict(width=1440, height=1000))
                # Poster fixtures isolate interaction checks from external image servers.
                context.route('https://image.tmdb.org/**', lambda route: route.fulfill(path=str(ROOT/'static/images/placeholder.svg'), content_type='image/svg+xml'))
                page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base+'/auth/register')
                page.locator('#username').fill('Проверка рекомендаций')
                page.locator('#email').fill('recommendations@example.test')
                page.locator('#password').fill('browser-test-password-123')
                page.locator('#confirm').fill('browser-test-password-123')
                page.get_by_role('button', name='Зарегистрироваться').click()
                page.wait_for_url(base+'/')
                rail = page.locator('[data-recommendations]')
                expect(rail).to_contain_text('Общая подборка')
                expect(rail.locator('.movie-card')).to_have_count(10)
                expect(rail.locator('.card-reason')).to_have_count(0)
                rail.scroll_into_view_if_needed()
                page.wait_for_function("""async () => {
                    const section=document.querySelector('[data-recommendations]');
                    return !!section && [...section.querySelectorAll('.movie-card')].some(e => e.getBoundingClientRect().top < innerHeight);
                }""")
                first = rail.locator('.movie-card').first.get_attribute('data-movie-id')
                old_request = rail.get_attribute('data-recommendation-request')
                with page.expect_response(lambda r: '/api/recommendations?' in r.url and r.status == 200):
                    rail.locator('[data-recommendation-dismiss]').first.click()
                expect(rail.locator(f'[data-movie-id="{first}"]')).to_have_count(0)
                expect(rail).not_to_have_attribute('data-recommendation-request', old_request)
                expect(rail.locator('.movie-card')).to_have_count(10)
                page.reload()
                expect(rail.locator(f'[data-movie-id="{first}"]')).to_have_count(0)
                with page.expect_response(lambda r: '/api/recommendations?' in r.url and r.status == 200):
                    rail.locator('[data-bookmark]').first.click()
                expect(rail).to_contain_text('В вашем вкусе')
                expect(rail.locator('.card-reason')).to_have_count(0)

                page.goto(base+'/movie/1')
                with page.expect_response(lambda r: r.url.endswith('/api/profile/state') and r.request.method == 'POST'):
                    page.locator('[data-rate="10"]').click()
                page.goto(base+'/')
                rail = page.locator('[data-recommendations]')
                expect(rail).to_contain_text('В вашем вкусе')
                expect(rail.locator('[data-movie-id="1"]')).to_have_count(0)
                expect(rail.locator('.card-reason')).to_have_count(0)
                rail.scroll_into_view_if_needed()
                with page.expect_response(lambda r: '/api/recommendations?' in r.url and 'rewatch=1' in r.url):
                    rail.locator('[data-recommendations-rewatch]').check()
                expect(rail.locator('[data-movie-id="1"]')).to_have_count(1)
                page.screenshot(path=str(args.artifacts/'desktop.png'), full_page=False)

                click_id = rail.locator('.movie-card').first.get_attribute('data-movie-id')
                with page.expect_response(lambda r: r.url.endswith('/feedback') and 'click' in (r.request.post_data or '')):
                    rail.locator('.movie-card .card-title').first.click()
                page.wait_for_url(base+f'/movie/{click_id}')
                page.goto(base+'/profile')
                expect(page.locator('[data-recommendations] .movie-card')).to_have_count(8)
                expect(page.locator('[data-recommendations] .card-reason')).to_have_count(0)
                page.set_viewport_size(dict(width=390, height=844))
                page.goto(base+'/')
                page.locator('[data-recommendations]').scroll_into_view_if_needed()
                expect(page.locator(f'[data-recommendations] [data-movie-id="{first}"]')).to_have_count(0)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal page overflow'
                page.screenshot(path=str(args.artifacts/'mobile.png'), full_page=False)

                # Save from another client without sending a page refresh event.
                state = context.request.get(base+'/api/profile/state').json()
                excluded = page.locator('[data-recommendations] .movie-card').first.get_attribute('data-movie-id')
                state['ratings'][excluded] = 1
                token = page.locator('meta[name=csrf-token]').get_attribute('content')
                with page.expect_response(lambda r: '/api/recommendations?' in r.url and r.status == 200, timeout=60000):
                    saved = context.request.post(base+'/api/profile/state', data=state, headers={'X-CSRF-Token':token})
                    assert saved.ok, saved.text()
                expect(page.locator(f'[data-recommendations] [data-movie-id="{excluded}"]')).to_have_count(0)
                expect(page.locator('[data-recommendations] .movie-card')).to_have_count(10)
                expect(page.locator('[data-recommendations] .card-reason')).to_have_count(0)
                context.close(); browser.close()
            with app.extensions['store'].connect() as db:
                uid = db.execute("SELECT id FROM users WHERE email='recommendations@example.test'").fetchone()[0]
                impressions = db.execute('SELECT COUNT(*) FROM impressions WHERE user_id=?', (uid,)).fetchone()[0]
                clicks = db.execute('SELECT COUNT(*) FROM clicks WHERE user_id=?', (uid,)).fetchone()[0]
                assert impressions > 0 and clicks == 1
            assert not errors, errors
            report = dict(passed=True, javascript_errors=errors, impressions=impressions, clicks=clicks,
                          checks=['registration', 'cold start', 'dismiss and refresh', 'decline persists after reload',
                                  'watchlist refresh', 'rating and personalization', 'rewatch', 'click', 'profile',
                                  'mobile overflow', 'no card explanations', 'periodic refresh within 60 seconds'],
                          remote_posters='fixture')
            (args.artifacts/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    main()
