"""Bounded pages and aggregate cache invalidation across SQLite connections."""
from dataclasses import asdict
from contextlib import closing
import sqlite3
from support import AppTestCase


class CatalogPerformanceTests(AppTestCase):
    def test_cache_reused_for_profile_writes_and_invalidated_by_external_rating_edit(self):
        self.accounts()
        with self.store.connect() as db:
            db.execute('INSERT INTO ratings VALUES(?,?,?,?)', (1,1,8,100))
        first = self.store.film_stats()
        cache = self.store._stats_cache
        self.store.update_state(2, lambda s: s['watchlist'].append(2))
        self.assertEqual(self.store.film_stats(), first)
        self.assertIs(self.store._stats_cache, cache)
        with closing(sqlite3.connect(self.store.path)) as external:
            external.execute('UPDATE ratings SET score=4 WHERE user_id=1 AND movie_id=1')
            external.commit()
        self.assertEqual(self.store.film_stats()[1]['avg'], 4)
        self.assertIsNot(self.store._stats_cache, cache)

    def test_catalog_pages_retain_filters_and_do_not_overlap(self):
        self.accounts()
        sample = self.store.film(1)
        for i in range(100,180):
            data = asdict(sample['film']); data.update(id=i, title=f'Pagination film {i}', genres=['Фантастика','Драма'])
            self.store.save_film(data, sample['art'])
        from test_interface import PageElements
        first = self.viewer.get('/?view=catalog&genre=Фантастика&genre=Драма&page=1').text
        second = self.viewer.get('/?view=catalog&genre=Фантастика&genre=Драма&page=2').text
        a, b = PageElements(first).movie_ids, PageElements(second).movie_ids
        self.assertEqual(len(a), 36)
        self.assertEqual(len(b), 36)
        self.assertFalse(set(a)&set(b))
        self.assertIn('page=2', first)

    def test_admin_users_are_paginated(self):
        self.accounts()
        for i in range(65):
            self.store.register(f'person{i}@example.test', f'Person {i}', 'unused-hash')
        page = self.admin.get('/admin/?tab=users').text
        self.assertEqual(page.count('class="panel admin-user"'), 50)
        self.assertIn('page=2', page)
