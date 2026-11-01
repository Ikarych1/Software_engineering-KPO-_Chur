"""Interface regression checks. Run: python -m unittest discover -s tests -v."""

import copy
import re
import unittest
import tempfile
from pathlib import Path
from dataclasses import asdict
from html.parser import HTMLParser
from urllib.parse import urlencode

import support  # noqa: F401
from app import app
import storage
from ui_data import ART
from database import Store


class PageElements(HTMLParser):
    """Read rendered links and cards without a third-party HTML dependency."""

    def __init__(self, markup):
        super().__init__()
        self.movie_ids = []
        self.static_urls = set()
        self.anchors = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'article' and 'movie-card' in attributes.get('class', '').split():
            self.movie_ids.append(int(attributes['data-movie-id']))
        if tag == 'a':
            self.anchors.append(attributes)
        for name in ('src', 'href'):
            value = attributes.get(name, '')
            if value.startswith('/static/'):
                self.static_urls.add(value)
        self.static_urls.update(re.findall(r'/static/[\w/.-]+', attributes.get('style', '')))


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_store = app.extensions['store']
        cls.store = Store(Path(cls.temp.name) / 'test.sqlite3')
        cls.store.seed(storage.CATALOG, ART)
        app.extensions['store'] = cls.store
        cls.store.register('owner@example.test', 'Owner', 'unused-test-hash')   # first account = admin
        user_id = cls.store.register('interface@example.test', 'Viewer', 'unused-test-hash')
        cls.client = app.test_client()
        with cls.client.session_transaction() as session:
            session.update(user_id=user_id, session_version=1)
        cls.client.get('/auth/preferences')
        with cls.client.session_transaction() as session:
            token = session['csrf']
        rows = cls.store.films()
        genres = sorted({g for r in rows for g in r['film'].genres})[:3]
        assert cls.client.post('/auth/preferences', data=dict(csrf_token=token, genres=genres,
                               movie_ids=[r['film'].id for r in rows[:3]])).status_code == 302

    @classmethod
    def tearDownClass(cls):
        app.extensions['store'] = cls.previous_store
        cls.temp.cleanup()

    def catalog(self, **filters):
        response = self.client.get('/?' + urlencode({'view': 'catalog', **filters}, doseq=True))
        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        return PageElements(markup).movie_ids, markup

    def test_multiple_genres_use_intersection(self):
        ids, markup = self.catalog(genre=['Фантастика', 'Драма'])
        self.assertEqual(set(ids), {1, 9, 10, 11})
        self.assertNotIn(2, ids)  # Only science fiction: excluded by an AND selection.
        self.assertNotIn(3, ids)  # Only drama: must not appear in an AND selection.
        self.assertIn('Фильм должен подходить под все выбранные жанры', markup)

    def test_search_and_genres_combine_and_keep_filter_state(self):
        ids, markup = self.catalog(genre=['Драма'], q='  ЛИНГВИСТ  ', sort='year')
        self.assertEqual(ids, [10])
        self.assertIn('value="лингвист"', markup)
        self.assertRegex(markup, r'value="Драма"\s+checked')
        self.assertRegex(markup, r'value="year"\s+selected')

    def test_rating_and_year_sort_filtered_results(self):
        for mode, field in [('rating', 'rating'), ('year', 'year')]:
            with self.subTest(mode=mode):
                ids, _ = self.catalog(genre=['Фантастика'], sort=mode)
                films = {film.id: film for film in storage.CATALOG}
                values = [getattr(films[film_id], field) for film_id in ids]
                self.assertEqual(values, sorted(values, reverse=True))
                self.assertEqual(set(ids), {1, 2, 7, 9, 10, 11})

    def test_empty_result_offers_reset(self):
        ids, markup = self.catalog(genre=['Фантастика', 'Комедия'])
        self.assertEqual(ids, [])
        self.assertIn('Пока ничего не нашлось', markup)
        reset = [a for a in PageElements(markup).anchors if a.get('href') == '/?view=catalog']
        self.assertTrue(reset)
        search_ids, _ = self.catalog(q='несуществующий фильм 999')
        self.assertEqual(search_ids, [])

    def test_hidden_film_is_excluded_from_catalog(self):
        film = storage.CATALOG[0]
        previous = self.store.film(film.id, include_hidden=True)
        try:
            data = asdict(film)
            data['is_hidden'] = True
            self.store.save_film(data, previous['art'], file_id=previous['file_id'], movie_id=film.id)
            ids, _ = self.catalog(genre=['Фантастика'])
            self.assertNotIn(film.id, ids)
            self.assertEqual(self.client.get(f'/movie/{film.id}').status_code, 404)
            self.assertEqual(self.client.get(f'/watch/{film.id}').status_code, 404)
        finally:
            self.store.save_film(asdict(film), previous['art'], file_id=previous['file_id'], movie_id=film.id)

    def test_browsing_detail_and_player_does_not_change_viewing_history(self):
        before = self.store.state(1)
        for film in storage.CATALOG:
            for path in [f'/movie/{film.id}', f'/watch/{film.id}']:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
        after = self.store.state(1)
        self.assertEqual(after, before)

    def test_detail_links_to_the_player_and_rooms(self):
        markup = self.client.get('/movie/9').get_data(as_text=True)
        self.assertTrue([a for a in PageElements(markup).anchors if a.get('href') == '/watch/9'] or 'Источник ещё не добавлен' in markup)
        self.assertIn('Кинопоиск', markup)
        self.assertIn('Впечатления зрителей', markup)
        self.assertIn('Похожие фильмы', markup)

    def test_search_covers_actors_and_directors(self):
        ids, _ = self.catalog(q='вильнёв')
        self.assertEqual(set(ids), {9, 10, 11})
        ids, _ = self.catalog(q='Киану')
        self.assertEqual(ids, [7])

    def test_pages_and_their_local_assets_are_available(self):
        routes = ['/', '/?view=catalog', '/rooms']
        routes.extend(f'/movie/{film.id}' for film in storage.CATALOG)
        routes.extend(f'/watch/{film.id}' for film in storage.CATALOG)
        routes.extend('/profile?section=' + section for section in [
            'overview', 'history', 'watchlist', 'watched', 'ratings', 'reviews', 'clips', 'company',
            'rooms', 'subscription', 'settings', 'friends'])
        assets = set()
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                self.assertIn('<html lang="ru">', markup)
                title = re.search(r'<title>(.*?)</title>', markup, re.DOTALL)
                self.assertIsNotNone(title)
                assets.update(PageElements(markup).static_urls)
        for asset in assets:
            with self.subTest(asset=asset):
                with self.client.get(asset) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertGreater(len(response.data), 0)

    def test_versioned_static_assets_are_cached_and_pages_are_not(self):
        markup = self.client.get('/').get_data(as_text=True)
        css = re.search(r'href="(/static/css/style\.css\?v=[0-9a-f]+)"', markup).group(1)
        with self.client.get(css) as response:
            self.assertIn('immutable', response.headers['Cache-Control'])
        self.assertEqual(self.client.get('/profile').headers['Cache-Control'], 'no-store')

    def test_missing_content_returns_404(self):
        for route in ['/movie/9999', '/watch/9999', '/room/NOPE00', '/rooms/NOPE00/archive', '/nothing-here']:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404)

    def test_player_has_no_user_source_inputs(self):
        markup = self.client.get('/watch/1').get_data(as_text=True)
        self.assertIn('<video id="torrent-video" playsinline controls', markup)
        self.assertIn('/static/js/player.js', markup)
        self.assertIn('/static/js/torrent-player.js', markup)
        self.assertNotIn('name="magnet"', markup)
        self.assertNotIn('type="file"', markup)

if __name__ == '__main__':
    unittest.main()
