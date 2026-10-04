"""Interface regression checks. Run: python -m unittest discover -s tests -v."""

import copy
import re
import unittest
from html.parser import HTMLParser
from urllib.parse import urlencode

from app import app
import storage
from ui_data import ARCHIVE


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
        cls.client = app.test_client()

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
        previous = film.is_hidden
        try:
            film.is_hidden = True
            ids, _ = self.catalog(genre=['Фантастика'])
            self.assertNotIn(film.id, ids)
            self.assertEqual(self.client.get(f'/movie/{film.id}').status_code, 404)
            self.assertEqual(self.client.get(f'/watch/{film.id}').status_code, 404)
        finally:
            film.is_hidden = previous

    def test_browsing_detail_and_player_does_not_change_viewing_history(self):
        before = copy.deepcopy((storage.USER_WATCHED, storage.USER_RATINGS, storage.REVIEWS))
        for film in storage.CATALOG:
            for path in [f'/movie/{film.id}', f'/watch/{film.id}']:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
        after = (storage.USER_WATCHED, storage.USER_RATINGS, storage.REVIEWS)
        self.assertEqual(after, before)

    def test_detail_opens_separate_player(self):
        response = self.client.get('/movie/9')
        markup = response.get_data(as_text=True)
        links = [a for a in PageElements(markup).anchors if a.get('href') == '/watch/9']
        self.assertTrue(links)
        self.assertTrue(any(a.get('target') == '_blank' and 'noopener' in a.get('rel', '') for a in links))
        self.assertNotIn('data-player ', markup)
        self.assertIn('Кинопоиск', markup)
        self.assertIn('IMDb', markup)

    def test_pages_and_their_local_assets_are_available(self):
        routes = ['/', '/?view=catalog', '/rooms', '/room/ROOM-359', '/room/ROOM-359?movie=9']
        routes.extend(['/rooms/archive/local', '/rooms/archive/local?room=ROOM-359&movie=2&session=ROOM-359%3A2'])
        routes.extend(f'/movie/{film.id}' for film in storage.CATALOG)
        routes.extend(f'/watch/{film.id}' for film in storage.CATALOG)
        routes.extend('/profile?section=' + section for section in [
            'overview', 'watchlist', 'watched', 'ratings', 'clips', 'friends',
            'rooms', 'subscription', 'settings',
        ])
        routes.extend('/rooms/archive/' + room['code'] for room in ARCHIVE)
        assets = set()
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                self.assertIn('<html lang="ru">', markup)
                title = re.search(r'<title>(.*?)</title>', markup, re.DOTALL)
                self.assertIsNotNone(title)
                self.assertNotIn('<script', title.group(1).lower())
                if route == '/rooms' or route.startswith('/rooms/archive/') or route == '/profile?section=rooms':
                    self.assertIn('<script src="/static/js/room.js" defer>', markup)
                assets.update(PageElements(markup).static_urls)
        for asset in assets:
            with self.subTest(asset=asset):
                with self.client.get(asset) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertGreater(len(response.data), 0)

    def test_missing_content_returns_404(self):
        for route in ['/movie/9999', '/watch/9999', '/room/not-a-room', '/rooms/archive/not-a-session']:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404)


if __name__ == '__main__':
    unittest.main()
