
import unittest
from unittest.mock import Mock
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from models import MediaItem
from recommendation_service import RecommendationService, UserContext, jaccard, preference_score


def movie(movie_id, *, genres=None, rating=8.0, hidden=False):
    return {'film': MediaItem(id=movie_id, title=f'Film {movie_id}', description='Test film',
                             genres=['Drama'] if genres is None else genres, year=2020,
                             country='US', rating=rating, duration_min=100,
                             age_rating='12+', is_hidden=hidden), 'art': {}}


def ids(result):
    return [item['id'] for item in result.items]


def snapshot(*, informative=True):
    """Заглушка индекса с фиксированным соседом; реальный поиск не выполняется."""
    index = Mock(spec=['stats', 'search_by_id'])
    index.stats.return_value = {'snapshot_id': 'lab6-snapshot', 'informative_features': informative}
    index.search_by_id.return_value = {'items': [(2, 0.25)]}
    return index


class SimilarityTests(unittest.TestCase):

    def test_jaccard_identical(self):
        """UT-01 Совпадающие наборы жанров."""
        # Arrange
        first, second = ['Drama'], ['Drama']
        # Act
        self.actual = jaccard(first, second)
        # Assert
        self.assertEqual(self.actual, 1.0)

    def test_jaccard_disjoint(self):
        """UT-02 Жанры без пересечений."""
        # Arrange
        first, second = ['Drama'], ['Comedy']
        # Act
        self.actual = jaccard(first, second)
        # Assert
        self.assertEqual(self.actual, 0.0)

    def test_jaccard_partial(self):
        """UT-03 Частичное совпадение жанров."""
        # Arrange
        first, second = ['Drama', 'Comedy'], ['Drama', 'Action']
        # Act
        self.actual = jaccard(first, second)
        # Assert
        self.assertAlmostEqual(self.actual, 1 / 3)

    def test_jaccard_empty(self):
        """UT-04 Пустые наборы жанров."""
        # Arrange
        first, second = [], []
        # Act
        self.actual = jaccard(first, second)
        # Assert
        self.assertEqual(self.actual, 0.0)

    def test_preferences_empty(self):
        """UT-05 Предпочтения не заданы."""
        # Arrange
        row, preferences = movie(1), {}
        # Act
        self.actual = preference_score(row, preferences)
        # Assert
        self.assertEqual(self.actual, 0.0)

    def test_preferences_normalization(self):
        """UT-06 Регистр и пробелы в жанрах."""
        # Arrange
        row = movie(1, genres=[' Drama ', 'COMEDY'])
        preferences = {'genres': ['drama']}
        # Act
        self.actual = preference_score(row, preferences)
        # Assert
        self.assertEqual(self.actual, 0.5)

    def test_preferences_categories(self):
        """UT-07 Усреднение категорий предпочтений."""
        # Arrange
        row = movie(1)
        row['art'] = {'original_language': 'en'}
        preferences = {'genres': ['Drama'], 'content_types': ['series'], 'languages': ['en']}
        # Act
        self.actual = preference_score(row, preferences)
        # Assert
        self.assertAlmostEqual(self.actual, 2 / 3)

    def test_preferences_people(self):
        """UT-08 Совпадение актёра и режиссёра."""
        # Arrange
        row = movie(1)
        row['art'] = {'cast': [{'person_id': 7}], 'directors': [{'person_id': 9}]}
        preferences = {'actor_ids': ['7'], 'director_ids': [9]}
        # Act
        self.actual = preference_score(row, preferences)
        # Assert
        self.assertEqual(self.actual, 1.0)


class RecommendationPolicyTests(unittest.TestCase):
    def setUp(self):
        # Новые зависимости перед каждым тестом исключают влияние порядка запуска.
        self.catalog = Mock(spec=['get'])
        self.catalog.get.return_value = None
        self.service = RecommendationService(catalog=self.catalog)

    def test_catalog_empty(self):
        """UT-09 Каталог пуст."""
        # Arrange
        rows = []
        # Act
        result = self.service.recommend(rows, {})
        self.actual = {'ids': ids(result), 'personalized': result.personalized}
        # Assert
        self.assertEqual(self.actual, {'ids': [], 'personalized': False})

    def test_hidden_excluded(self):
        """UT-10 Скрытый фильм исключён."""
        # Arrange
        rows = [movie(1, hidden=True), movie(2)]
        # Act
        self.actual = ids(self.service.recommend(rows, {}))
        # Assert
        self.assertEqual(self.actual, [2])

    def test_completed_excluded(self):
        """UT-11 Просмотренный фильм исключён."""
        # Arrange
        rows = [movie(1), movie(2)]
        context = UserContext(completed={1})
        # Act
        self.actual = ids(self.service.recommend(rows, {}, context))
        # Assert
        self.assertEqual(self.actual, [2])

    def test_rewatch_completed(self):
        """UT-12 Повторный просмотр разрешён."""
        # Arrange
        rows, context = [movie(1)], UserContext(completed={1})
        # Act
        self.actual = ids(self.service.recommend(rows, {}, context, allow_rewatch=True))
        # Assert
        self.assertEqual(self.actual, [1])

    def test_declined_excluded(self):
        """UT-13 Отказ сохраняется при повторном просмотре."""
        # Arrange
        rows, context = [movie(1)], UserContext(declined={1})
        # Act
        self.actual = ids(self.service.recommend(rows, {}, context, allow_rewatch=True))
        # Assert
        self.assertEqual(self.actual, [])

    def test_low_rating_boundary(self):
        """UT-14 Граница низкой оценки."""
        # Arrange
        self.actual = {}
        for score, expected in [(4, []), (5, [1])]:
            with self.subTest(score=score):
                context = UserContext(ratings={1: {'score': score, 'updated_at': 0}})
                # Act
                self.actual[str(score)] = ids(self.service.recommend([movie(1)], {}, context, allow_rewatch=True))
                # Assert
                self.assertEqual(self.actual[str(score)], expected)

    def test_rated_excluded(self):
        """UT-15 Оценённый фильм исключён из обычной выдачи."""
        # Arrange
        context = UserContext(ratings={1: {'score': 9, 'updated_at': 0}})
        # Act
        self.actual = ids(self.service.recommend([movie(1)], {}, context))
        # Assert
        self.assertEqual(self.actual, [])

    def test_liked_excluded(self):
        """UT-16 Начальный выбор исключён из обычной выдачи."""
        # Arrange
        context = UserContext(preferences={'liked_movie_ids': [1]})
        # Act
        self.actual = ids(self.service.recommend([movie(1)], {}, context))
        # Assert
        self.assertEqual(self.actual, [])

    def test_limit_valid(self):
        """UT-17 Допустимые границы лимита."""
        # Arrange
        rows = [movie(i) for i in range(1, 13)]
        self.actual = {}
        for limit in [1, 10]:
            with self.subTest(limit=limit):
                # Act
                self.actual[str(limit)] = len(self.service.recommend(rows, {}, limit=limit).items)
                # Assert
                self.assertEqual(self.actual[str(limit)], limit)

    def test_limit_invalid(self):
        """UT-18 Некорректные значения лимита."""
        # Arrange
        invalid = [0, 11, -1, True, 1.0, '3', None]
        self.actual = []
        for limit in invalid:
            with self.subTest(limit=limit):
                # Act и Assert: контекст проверяет ожидаемое исключение.
                with self.assertRaises(ValueError):
                    self.service.recommend([movie(1)], {}, limit=limit)
                self.actual.append('ValueError')
        self.catalog.get.assert_not_called()

    def test_fallback_rating(self):
        """UT-19 Общая выдача по рейтингу."""
        # Arrange
        rows = [movie(1, rating=6), movie(2, rating=9)]
        # Act
        result = self.service.recommend(rows, {})
        self.actual = {'ids': ids(result), 'personalized': result.personalized}
        # Assert
        self.assertEqual(self.actual, {'ids': [2, 1], 'personalized': False})

    def test_rating_tie(self):
        """UT-20 Равные рейтинги."""
        # Arrange
        rows = [movie(2), movie(1)]
        # Act
        self.actual = ids(self.service.recommend(rows, {}))
        # Assert
        self.assertEqual(self.actual, [1, 2])

    def test_genre_priority(self):
        """UT-21 Приоритет предпочитаемого жанра."""
        # Arrange
        rows = [movie(1, genres=['Comedy']), movie(2)]
        context = UserContext(preferences={'genres': ['Drama']})
        # Act
        result = self.service.recommend(rows, {}, context)
        self.actual = {'ids': ids(result), 'personalized': result.personalized}
        # Assert
        self.assertEqual(self.actual, {'ids': [2, 1], 'personalized': True})
        self.assertEqual(result.items[0]['reason'], 'Совпадает с вашими предпочтениями')

    def test_catalog_interaction(self):
        """UT-22 Параметры обращения к каталогу."""
        # Arrange
        rows = [movie(1, hidden=True), movie(2)]
        # Act
        self.service.recommend(rows, {}, namespace='lab6')
        args = self.catalog.get.call_args.args
        self.actual = {'calls': self.catalog.get.call_count, 'ids': [r['film'].id for r in args[0]], 'namespace': args[1]}
        # Assert
        self.catalog.get.assert_called_once_with([rows[1]], 'lab6')

    def test_personal_snapshot(self):
        """UT-23 Персональный поиск со снимком дублёром."""
        # Arrange
        rows = [movie(1), movie(2)]
        self.catalog.get.return_value = snapshot()
        context = UserContext(ratings={1: {'score': 9, 'updated_at': 0}})
        # Act
        result = self.service.recommend(rows, {}, context)
        self.actual = {'ids': ids(result), 'similarity': result.items[0]['components']['similarity'], 'personalized': result.personalized}
        # Assert
        self.assertEqual(ids(result), [2])
        self.assertTrue(result.personalized)
        self.assertEqual(result.snapshot_id, 'lab6-snapshot')
        self.assertAlmostEqual(self.actual['similarity'], 0.8)
        self.assertEqual(result.items[0]['reason'], 'Общие жанры с «Film 1»')

    def test_popularity_priority(self):
        """UT-24 Ранжирование по популярности."""
        # Arrange
        rows, popularity = [movie(1), movie(2)], {1: 1, 2: 10}
        # Act
        result = self.service.recommend(rows, {}, popularity=popularity)
        self.actual = {'ids': ids(result), 'reason': result.items[0]['reason']}
        # Assert
        self.assertEqual(ids(result), [2, 1])
        self.assertEqual(self.actual['reason'], 'Популярно за последние две недели')

    def test_community_rating_priority(self):
        """UT-25 Приоритет агрегированной оценки."""
        # Arrange
        rows = [movie(1, rating=9), movie(2, rating=6)]
        stats = {1: {'avg': 2}, 2: {'avg': 10}}
        # Act
        self.actual = ids(self.service.recommend(rows, stats))
        # Assert
        self.assertEqual(self.actual, [2, 1])

    def test_watched_anchor_boundary(self):
        """UT-26 Граница просмотра для опорного фильма."""
        # Arrange
        self.actual = {}
        for seconds, expected in [(79, False), (80, True)]:
            with self.subTest(seconds=seconds):
                self.catalog.get.return_value = snapshot()
                context = UserContext(progress=[{'movie_id': 1, 'updated_at': 0, 'watched_seconds': seconds, 'duration': 100, 'completed': False}])
                # Act
                self.actual[str(seconds)] = self.service.recommend([movie(1), movie(2)], {}, context).personalized
                # Assert
                self.assertEqual(self.actual[str(seconds)], expected)

    def test_similar_unknown(self):
        """UT-27 Поиск похожих для неизвестного фильма."""
        # Arrange
        rows = [movie(1)]
        # Act
        self.actual = self.service.similar(rows, 99)
        # Assert
        self.assertEqual(self.actual, [])
        self.catalog.get.assert_not_called()

    def test_similar_uninformative(self):
        """UT-28 Неинформативный снимок похожих фильмов."""
        # Arrange
        index = snapshot(informative=False)
        self.catalog.get.return_value = index
        # Act
        self.actual = self.service.similar([movie(1)], 1)
        # Assert
        self.assertEqual(self.actual, [])
        index.search_by_id.assert_not_called()

    def test_similar_success(self):
        """UT-29 Поиск похожих по заданному снимку."""
        # Arrange
        index = snapshot()
        self.catalog.get.return_value = index
        rows = [movie(1), movie(2)]
        # Act
        result = self.service.similar(rows, 1, limit=1, namespace='lab6')
        self.actual = [row['film'].id for row in result]
        # Assert
        self.assertEqual(self.actual, [2])
        self.catalog.get.assert_called_once_with(rows, 'lab6')
        index.search_by_id.assert_called_once_with(1, k=1, allowed_ids={1: rows[0], 2: rows[1]})


if __name__ == '__main__':
    unittest.main()
