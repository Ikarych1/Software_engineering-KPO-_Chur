"""Native search integration, live user rules and authenticated feedback."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest

import support
from support import AppTestCase
from models import MediaItem
from recommendation_catalog import CatalogIndex
from recommendation_service import RecommendationService, UserContext
from recommendation_store import RecommendationStore


def row(id_, genres, year=2000, duration=100, kind='movie'):
    return dict(film=MediaItem(id_, str(id_), 'Description', genres, year, 'US', 8., duration, '12+', kind=kind),
                art={}, torrent=False, episodes=[])


class CatalogTests(unittest.TestCase):
    def test_native_neighbors_match_independent_distance_with_filters_and_ties(self):
        rows = [row(1, ['a', 'b'], 2000, 100), row(2, ['a'], 2000, 100),
                row(3, ['a', 'c'], 2010, 120), row(4, [], 2020, 150)]
        snapshot = CatalogIndex().get(rows)
        transform = snapshot.report()['transform']
        expected = []
        for other in rows[1:]:
            numeric = sum(abs((a-m)/s - (b-m)/s) for a,b,m,s in zip(
                [2000, 100], [other['film'].year, other['film'].duration_min], transform['medians'], transform['scales']))
            a,b = {'a','b'}, set(other['film'].genres)
            distance = (numeric + len(a ^ b)/len(a | b))/3  # kind was constant and removed
            expected.append((other['film'].id, distance))
        expected.sort(key=lambda x: (x[1], x[0]))
        actual = snapshot.search_by_id(1)['items']
        self.assertEqual([i for i, _ in actual], [i for i, _ in expected])
        for (_, a), (_, b) in zip(actual, expected):
            self.assertAlmostEqual(a, b, places=14)
        filtered = snapshot.search_by_id(1, allowed_ids=[3])['items']
        self.assertEqual([i for i, _ in filtered], [3])
        self.assertAlmostEqual(filtered[0][1], expected[1][1], places=14)

    def test_cache_reused_loaded_and_invalidated_on_geometry_change(self):
        rows = [row(1, ['a']), row(2, ['b'], 2010)]
        with tempfile.TemporaryDirectory() as d:
            index = CatalogIndex(d)
            first = index.get(rows, 'db')
            self.assertIs(index.get(rows, 'db'), first)
            self.assertEqual(index.builds, 1)
            loaded = CatalogIndex(d)
            self.assertEqual(loaded.get(rows, 'db').stats(), first.stats())
            self.assertEqual(loaded.builds, 0)
            rows[1]['film'].genres = ['a', 'b']
            self.assertNotEqual(index.get(rows, 'db').stats()['snapshot_id'], first.stats()['snapshot_id'])
            self.assertEqual(index.builds, 2)

    def test_empty_degenerate_and_missing_numeric_catalogs(self):
        service = RecommendationService()
        self.assertEqual(service.recommend([], {}).items, [])
        rows = [row(1, ['a'], 0, 0), row(2, ['a'], 0, 0)]
        result = service.recommend(rows, {}, UserContext(watchlist=[1]))
        self.assertFalse(result.personalized)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(service.similar(rows, 1), [])


class RecommendationTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.accounts()
        self.repo = RecommendationStore(self.store)

    def get(self, **params):
        response = self.viewer.get('/api/recommendations', query_string=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json

    def test_cold_start_public_cards_limit_and_guest_no_impressions(self):
        data = self.get()
        self.assertFalse(data['personalized'])
        self.assertEqual(len(data['items']), 10)
        self.assertIn('Общая подборка', data['html'])
        self.assertNotIn('card-reason', data['html'])
        self.assertTrue(any(not i['available'] for i in data['items']))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impressions').fetchone()[0], 0)
        guest = self.app.test_client().get('/api/recommendations').json
        self.assertIsNone(guest['request_id'])
        self.assertNotIn('data-recommendation-dismiss', guest['html'])

    def test_ratings_and_preferences_are_current_without_rebuilding(self):
        first = self.get()
        builds = self.app.extensions['recommendations'].catalog.builds
        self.post(self.viewer, '/api/profile/state', json={'ratings': {'1':10, '2':10}})
        data = self.get()
        self.assertTrue(data['personalized'])
        self.assertNotIn(1, [i['id'] for i in data['items']])
        self.assertEqual(self.app.extensions['recommendations'].catalog.builds, builds)
        self.assertNotEqual([i['id'] for i in first['items']], [i['id'] for i in data['items']])
        with self.store.connect() as db:
            db.execute('INSERT INTO user_preferences(user_id,genres,updated_at) VALUES(?,?,?)', (2, '["комедия"]', time.time()))
        self.assertEqual(self.repo.context(2, self.store.films()).preferences['genres'], ['комедия'])
        preferred = self.get()
        rows = {r['film'].id: r['film'] for r in self.store.films()}
        result = self.app.extensions['recommendations'].recommend(
            self.store.films(), self.store.film_stats(), self.repo.context(2, self.store.films()),
            namespace=str(self.store.path.resolve()))
        self.assertTrue(any(item['components']['preferences'] > 0 for item in result.items))
        for item in result.items:
            genres = {g.strip().casefold() for g in rows[item['id']].genres}
            expected = len(genres & {'комедия'}) / len(genres | {'комедия'})
            self.assertAlmostEqual(item['components']['preferences'], expected)
        self.assertTrue(preferred['personalized'])
        self.assertEqual(self.app.extensions['recommendations'].catalog.builds, builds)

    def test_declines_hidden_and_completed_never_leak_through_fallback(self):
        ids = [i['id'] for i in self.get()['items']]
        self.post(self.viewer, '/api/recommendations/interest', json={'movie_id':ids[0], 'not_interested':True})
        film = self.store.film(ids[1]); data = asdict(film['film']); data['is_hidden'] = True
        self.store.save_film(data, film['art'], movie_id=ids[1])
        self.store.save_progress(2, ids[2], 100, 100, 100)
        result = {i['id'] for i in self.get()['items']}
        self.assertFalse(result & set(ids[:3]))
        self.assertNotIn(ids[0], {i['id'] for i in self.get(rewatch=1)['items']})

    def test_one_series_episode_is_not_the_whole_series(self):
        id_ = self.store.save_film(asdict(row(50, ['Drama'], kind='series')['film']), {},
                                   episodes=[dict(file_id=0), dict(file_id=1)])
        self.store.save_progress(2, id_, 100, 100, 100, file_id=0, next_file_id=1)
        context = self.repo.context(2, self.store.films())
        self.assertNotIn(id_, context.completed)
        # Even jumping directly to the last episode is not proof all episodes were seen.
        with self.store.connect() as db:
            db.execute('DELETE FROM episode_progress WHERE user_id=2 AND movie_id=?', (id_,))
        self.store.save_progress(2, id_, 100, 100, 100, file_id=1)
        self.assertNotIn(id_, self.repo.context(2, self.store.films()).completed)
        self.store.save_progress(2, id_, 100, 100, 100, file_id=0, next_file_id=1)
        self.assertIn(id_, self.repo.context(2, self.store.films()).completed)

    def test_feedback_is_owned_validated_and_idempotent(self):
        data = self.get(); request_id = data['request_id']; id_ = data['items'][0]['id']
        url = f'/api/recommendations/{request_id}/feedback'
        for _ in range(2):
            self.assertEqual(self.post(self.viewer, url, json={'movie_ids':[id_], 'action':'click'}).status_code, 200)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impressions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM clicks').fetchone()[0], 1)
        self.assertEqual(self.post(self.admin, url, json={'movie_ids':[id_]}).status_code, 404)
        self.assertEqual(self.post(self.viewer, url, json={'movie_ids':[999]}).status_code, 400)
        self.assertEqual(self.viewer.post(url, json={'movie_ids':[id_]}).status_code, 400)
        self.assertEqual(self.post(self.viewer, url, json={'movie_ids':[True]}).status_code, 400)

    def test_empty_result_is_explained_and_inputs_validated(self):
        for r in self.store.films():
            self.repo.decline(2, r['film'].id, True)
        data = self.get()
        self.assertEqual(data['items'], [])
        self.assertIn('Подходящих рекомендаций пока нет', data['html'])
        for params in ({'limit':0}, {'limit':11}, {'limit':'x'}, {'surface':'other'}, {'rewatch':'yes'}):
            self.assertEqual(self.viewer.get('/api/recommendations', query_string=params).status_code, 400)

    def test_missing_sample_uploads_use_existing_images_or_placeholder(self):
        item = self.store.film(1)
        art = dict(item['art'], poster='uploads/missing-poster.jpg', hero_video='uploads/missing-hero.mp4',
                   slug='not-in-this-copy', tmdb_poster_url='')
        self.store.save_film(asdict(item['film']), art, movie_id=1)
        page = self.viewer.get('/movie/1').text
        self.assertNotIn('uploads/missing-poster.jpg', page)
        self.assertIn('/static/images/placeholder.svg', page)
        self.assertNotIn('uploads/missing-hero.mp4', self.viewer.get('/').text)

    def test_similar_and_parallel_policy_queries_use_mixdex(self):
        rows = self.store.films(); service = self.app.extensions['recommendations']
        ctx, stats = self.repo.context(2, rows), self.store.film_stats()
        ctx.ratings = {1:dict(score=10,updated_at=100)}
        def query(_):
            return service.recommend(rows, stats, ctx, namespace=str(self.store.path.resolve())).items
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(query, range(12)))
        self.assertTrue(all(v == results[0] for v in results))
        self.assertTrue(service.similar(rows, 1))
        self.assertTrue(service.catalog.get(rows).stats()['informative_features'])


if __name__ == '__main__':
    unittest.main()
