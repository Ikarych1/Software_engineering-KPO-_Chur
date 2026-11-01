"""Real browser check of IMDb CSV import against tiny offline official-format fixtures."""
from pathlib import Path
import sys
import tempfile
import threading
import logging

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]

import support
from test_imdb_import import fixtures
from app import app
from database import Store
from imdb_import import ImportJobs
import storage
from ui_data import ART
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(dir=ROOT/'.local') as tmp:
        store = Store(Path(tmp)/'test.sqlite3'); store.seed(storage.CATALOG,ART)
        previous = app.extensions['store'],app.extensions['imdb_importer'],app.extensions['playback'].store
        app.extensions['store'] = store
        app.extensions['playback'].store = store
        app.extensions['imdb_importer'] = ImportJobs(fixtures(Path(tmp)/'datasets'))
        server = make_server('127.0.0.1',0,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        artifacts = ROOT/'tests/artifacts';artifacts.mkdir(exist_ok=True)
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge',headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1100})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/auth/register')
                for name,value in dict(username='IMDb Admin',email='imdb-admin@example.test',password='test-password-123',confirm='test-password-123').items():
                    page.locator(f'[name={name}]').fill(value)
                page.locator('form.stacked-form button[type=submit]').click()
                page.wait_for_url('**/admin/')
                page.get_by_role('link',name='IMDb · Импорт CSV').click()
                page.wait_for_url('**/admin/imdb/')
                data = 'imdb_id,title,year\ntt0816692,Интерстеллар,2014\ntt3659388,Марсианин,2015\ntt9999999,Missing,2026\n'.encode()
                page.locator('#imdb-list').set_input_files({'name':'movies.csv','mimeType':'text/csv','buffer':data})
                page.locator('#imdb-submit').click()
                page.wait_for_function("document.getElementById('imdb-status-heading').textContent === 'Готово'",timeout=15000)
                assert page.locator('.imdb-result').count() == 3
                assert len(store.films(True)) == 13
                assert store.film(13)['film'].title == 'Марсианин'
                assert page.locator('#imdb-report').is_visible()
                page.screenshot(path=str(artifacts/'imdb-import-desktop.png'),full_page=True)
                page.reload()
                page.wait_for_function("document.querySelectorAll('.imdb-result').length === 3")
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
                page.screenshot(path=str(artifacts/'imdb-import-mobile.png'),full_page=True)
                link = page.locator('.imdb-result').nth(1).get_by_role('link')
                link.click()
                page.wait_for_url('**/admin/films/13')
                assert page.locator('[name=title]').input_value() == 'Марсианин'
                assert page.locator('[name=imdb]').input_value() == '8.0'
                assert page.locator('[name=age_rating]').input_value() == '—'
                assert not errors, errors
                browser.close()
            print('PASS: admin CSV upload, duplicate linking, import results, reload, editor, mobile and JavaScript.')
        finally:
            server.shutdown()
            app.extensions['store'],app.extensions['imdb_importer'],app.extensions['playback'].store = previous


if __name__ == '__main__':
    main()
