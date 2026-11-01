"""Browser regression for TMDB import; isolated database and deterministic API responses."""
import logging
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
import support
from app import app
from database import Store
from tmdb_import import ImportJobs, Settings
from test_tmdb_import import FakeClient
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(dir=ROOT/'.local') as tmp:
        store = Store(Path(tmp)/'test.sqlite3')
        settings = Settings(Path(tmp)/'settings.json'); settings.save('browser-test-token')
        jobs = ImportJobs(store,settings,FakeClient)
        previous = app.extensions['store'],app.extensions['tmdb_importer'],app.extensions['playback'].store
        app.extensions['store'],app.extensions['tmdb_importer'],app.extensions['playback'].store = store,jobs,store
        server = make_server('127.0.0.1',0,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        artifacts = ROOT/'tests/artifacts'; artifacts.mkdir(exist_ok=True)
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge',headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1100})
                # No public image requests in a deterministic UI test.
                page.route('https://image.tmdb.org/**',lambda route:route.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="500" height="750"><rect width="500" height="750" fill="#21323e"/></svg>'))
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/auth/register')
                for name,value in dict(username='TMDB Admin',email='tmdb-admin@example.test',password='test-password-123',confirm='test-password-123').items():
                    page.locator(f'[name={name}]').fill(value)
                page.locator('form.stacked-form button[type=submit]').click()
                page.wait_for_url('**/admin/')
                page.get_by_role('link',name='TMDB · Импорт CSV').click()
                page.wait_for_url('**/admin/tmdb/')
                assert page.locator('#tmdb-credential').input_value() == ''
                page.screenshot(path=str(artifacts/'tmdb-import-desktop.png'),full_page=True)
                data = ('imdb_id,title,year\ntt3659388,Марсианин,2015\n'+'tt9999999,Не найден,2026\n'*104).encode()
                page.locator('#tmdb-list').set_input_files({'name':'movies.csv','mimeType':'text/csv','buffer':data})
                page.locator('#tmdb-submit').click()
                page.wait_for_function("document.getElementById('tmdb-status-heading').textContent === 'Готово'",timeout=20000)
                assert page.locator('.imdb-result').count() == 100
                assert page.locator('#tmdb-next').is_enabled()
                page.locator('#tmdb-next').click()
                page.wait_for_function("document.querySelectorAll('.imdb-result').length === 5")
                assert len(store.films(True)) == 1
                assert page.locator('#tmdb-report').is_visible()
                page.reload()
                page.wait_for_function("document.querySelectorAll('.imdb-result').length === 100")
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Mobile overflow'
                page.screenshot(path=str(artifacts/'tmdb-import-mobile.png'),full_page=True)
                page.locator('.imdb-result').first.get_by_role('link').click()
                page.wait_for_url('**/admin/films/1')
                assert page.locator('[name=title]').input_value() == 'Марсианин'
                assert page.locator('[name=description]').input_value().startswith('История астронавта')
                assert page.locator('[name=imdb]').input_value() == '—'
                assert page.locator('[data-preview=poster]').get_attribute('src').startswith('https://image.tmdb.org/')
                page.get_by_role('link',name='Открыть карточку',exact=True).click()
                page.wait_for_url('**/movie/1')
                assert page.locator('.external-ratings').inner_text().strip() == '7.7\nTMDB'
                assert page.locator('.movie-backdrop img').get_attribute('src').endswith('/backdrop.jpg')
                page.screenshot(path=str(artifacts/'tmdb-movie-mobile.png'),full_page=True)
                page.get_by_role('link',name='Источники данных',exact=True).click()
                page.wait_for_url('**/about/data')
                assert page.locator('.tmdb-logo').evaluate('(img) => img.complete && img.naturalWidth > 0')
                assert not errors,errors
                browser.close()
            print('PASS: TMDB upload, pagination, reload, editor, images, ratings, attribution, mobile and JavaScript.')
        finally:
            server.shutdown()
            app.extensions['store'],app.extensions['tmdb_importer'],app.extensions['playback'].store = previous


if __name__ == '__main__':
    main()
