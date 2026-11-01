"""Browser verification on a temporary sample copy, including legacy and mobile sign-in."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT/'tests/artifacts/onboarding'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kinosfera-onboarding-') as directory:
        directory = Path(directory)
        db_path = directory/'working.sqlite3'
        shutil.copyfile(ROOT/'datasets/cinema_sample/cinema_sample.sqlite3', db_path)
        os.environ.update(CINEMA_DATABASE=str(db_path), CINEMA_CACHE_DIR=str(directory/'media'),
                          CINEMA_RECOMMENDATION_CACHE=str(directory/'index'), CINEMA_HWACCEL='off')
        from app import app
        from werkzeug.serving import make_server
        from werkzeug.security import generate_password_hash
        from playwright.sync_api import sync_playwright, expect
        store = app.extensions['store']
        legacy_id = store.register('legacy-browser@example.test', 'Legacy browser', generate_password_hash('browser-password-123'))
        legacy_state = dict(watched=[1], ratings={'1':9}, watchlist=[2], clips=[], settings={}, rooms={}, friends=[])
        store.save_state(legacy_id, legacy_state)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        checks = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True)
                for width, email, legacy in [(1440,'onboarding-browser@example.test',False), (390,'legacy-browser@example.test',True)]:
                    context = browser.new_context(viewport=dict(width=width,height=1000))
                    context.route('https://image.tmdb.org/**', lambda route: route.fulfill(path=str(ROOT/'static/images/placeholder.svg'), content_type='image/svg+xml'))
                    page = context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(base+('/auth/login' if legacy else '/auth/register'))
                    if not legacy:
                        page.locator('#username').fill('Browser Viewer')
                        page.locator('#confirm').fill('browser-password-123')
                    page.locator('#email').fill(email)
                    page.locator('#password').fill('browser-password-123')
                    page.locator('form.stacked-form button[type=submit]').click()
                    page.wait_for_url('**/auth/preferences**')
                    assert page.request.get(base+'/api/recommendations').status == 428
                    expect(page.locator('#taste-next')).to_be_disabled()
                    for i in range(3):page.locator('.taste-genre').nth(i).click()
                    expect(page.locator('#genre-count')).to_have_text('3 / 3')
                    page.screenshot(path=str(OUT/f'genres-{width}.png'),full_page=True)
                    page.locator('#taste-next').click()
                    for i in range(3):page.locator('#taste-movies .taste-movie').nth(i).click()
                    expect(page.locator('#movie-count')).to_have_text('3 / 3')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(OUT/f'films-grid-{width}.png'),full_page=False)
                    page.locator('#taste-query').fill('невозможный поиск 123456')
                    expect(page.locator('#taste-status')).to_contain_text('Ничего не найдено')
                    expect(page.locator('#taste-selected-inputs input')).to_have_count(3)
                    expect(page.locator('#taste-submit')).to_be_enabled()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(OUT/f'films-{width}.png'),full_page=True)
                    page.locator('#taste-submit').click()
                    page.wait_for_url(base+'/')
                    data = page.request.get(base+'/api/recommendations').json()
                    assert data['personalized'] and data['items']
                    row = store.account(email=email)
                    with store.connect() as db:
                        pref = db.execute('SELECT * FROM user_preferences WHERE user_id=?',(row['id'],)).fetchone()
                        assert len(json.loads(pref['genres']))==3 and len(json.loads(pref['liked_movie_ids']))==3
                        assert pref['onboarding_completed_at']
                        if not legacy:assert db.execute('SELECT COUNT(*) FROM ratings WHERE user_id=?',(row['id'],)).fetchone()[0]==0
                    if legacy:assert store.state(legacy_id)==legacy_state
                    page.goto(base+'/profile')
                    assert '/auth/preferences' not in page.url
                    checks.append(dict(width=width,legacy=legacy,personalized=True,preference_count=3))
                    context.close()
                browser.close()
            assert not errors,errors
            report=dict(passed=True,checks=checks,browser_errors=errors,source_database_modified=False)
            (OUT/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':main()
